"""Core Lambdas: resolve_context (migrate and post modes), inspect_task, classify_failure, write_report."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from conftest import EXEC_ARN, TASK_ARN, FakeDms, FakeSfn, dms_task

from shared import aws as shared_aws
from shared import bundle as jobs_bundle

BUCKET = "dms-orch-artifacts-test"
REGISTRATION = {
    "schemaVersion": "1",
    "environment": "test",
    "project": "ledger",
    "taskKey": "core",
    "taskArn": TASK_ARN,
    "taskResourceId": "ABCDEF",
    "replicationInstanceArn": "arn:aws:dms:eu-west-2:111122223333:rep:RI",
    "logGroupName": "dms-tasks-ri",
    "endpointArns": ["arn:aws:dms:eu-west-2:111122223333:endpoint:SRC"],
    "connections": {
        "source": {
            "engine": "postgresql",
            "secretArn": "arn:aws:secretsmanager:eu-west-2:111122223333:secret:src",
            "host": "src.cluster.local",
            "port": 5432,
            "database": "ledger",
            "sslMode": "verify-full",
        }
    },
    "jobs": {"s3Prefix": "jobs/ledger", "defaultVersion": "v1"},
    "defaults": {"completion": "auto", "targetResetPolicy": "if-interrupted", "autoResumeMaxAttempts": 1},
}
JOBSET = {
    "schemaVersion": 1,
    "defaults": {"runtime": "fargate", "timeoutSeconds": 1800},
    "phases": {
        "pre": [
            {
                "name": "check-permissions",
                "handler": "builtin:checks/postgresql_source_grants",
                "with": {"connection": "source"},
            },
            {"name": "create-tables", "command": ["psql-run", "--connection", "target", "--file", "ddl/010.sql"]},
        ],
        "reset": [{"name": "reset-target", "command": ["psql-run", "--template", "reset/truncate.sql"]}],
        "post": [],
    },
}


def bundle_files(files: dict[str, bytes]) -> dict[str, str]:
    return {rel: jobs_bundle.digest(data) for rel, data in files.items()}


def put_bundle(s3: Any, version: str, files: dict[str, bytes]) -> str:
    """Upload a built bundle with its bundle.sha256 (as sync-jobs.sh does); returns the total."""
    for rel, data in files.items():
        s3.put_object(Bucket=BUCKET, Key=f"jobs/ledger/{version}/{rel}", Body=data)
    manifest = jobs_bundle.render(bundle_files(files)).encode()
    s3.put_object(Bucket=BUCKET, Key=f"jobs/ledger/{version}/bundle.sha256", Body=manifest)
    return jobs_bundle.total(bundle_files(files))


# Terraform pins every job set version a task may run (jobs.bundles, review finding 9).
REGISTRATION["jobs"]["bundles"] = {"v1": jobs_bundle.total(bundle_files({"jobset.json": json.dumps(JOBSET).encode()}))}


@pytest.fixture
def env(table, monkeypatch):
    monkeypatch.setenv("SSM_PREFIX", "/dms-orch/test")
    monkeypatch.setenv("ARTIFACTS_BUCKET", BUCKET)
    monkeypatch.setenv("ENVIRONMENT", "test")
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    # post reads context.json by the version migrate recorded: the bucket is versioned.
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    put_bundle(s3, "v1", {"jobset.json": json.dumps(JOBSET).encode()})
    ssm = boto3.client("ssm", region_name="eu-west-2")
    ssm.put_parameter(Name="/dms-orch/test/ledger/core", Type="String", Value=json.dumps(REGISTRATION))
    return {"s3": s3, "ssm": ssm, "table": table}


def use(dms: Any = None, sfn: Any = None, logs: Any = None) -> None:
    if dms is not None:
        shared_aws.set_client("dms", dms)
    if sfn is not None:
        shared_aws.set_client("stepfunctions", sfn)
    if logs is not None:
        shared_aws.set_client("logs", logs)


EXECUTION = {"arn": EXEC_ARN, "name": "ledger-core-20261027T100000Z-ab12", "startTime": "2026-10-27T10:00:00Z"}

# --- resolve_context ---------------------------------------------------------------


def test_resolve_context_builds_ctx_and_run_item(env, store):
    from lambdas.resolve_context.handler import handler

    out = handler({"request": {"project": "ledger", "taskKey": "core"}, "execution": EXECUTION})
    assert out["runId"] == EXECUTION["name"]
    req, ctx = out["request"], out["ctx"]
    assert req["operation"] == "migrate" and req["targetReset"] == {"policy": "if-interrupted"}
    assert req["autoResume"]["maxAttempts"] == 1 and "TRANSIENT_SOURCE_CONN" in req["autoResume"]["onlyCategories"]
    assert ctx["taskArn"] == TASK_ARN and ctx["jobsVersion"] == "v1"
    # A load that ends with tables in error fails the run by default.
    assert req["failOnTableErrors"] is True
    assert [j["name"] for j in ctx["jobs"]["pre"]] == ["check-permissions", "create-tables"]
    assert ctx["jobs"]["pre"][0]["runtime"] == "lambda" and ctx["jobs"]["pre"][1]["timeoutSeconds"] == 1800
    allowed = {"engine", "secretArn", "host", "port", "database", "sslMode", "dataApiClusterArn"}
    assert set(ctx["connections"]["source"]) <= allowed
    run = store.get_run(out["runId"])
    assert run["status"] == "RUNNING" and run["gsi1pk"] == "PROJECT#ledger"


def test_resolve_context_fail_on_table_errors_registration_and_request_override(env, store):
    from lambdas.resolve_context.handler import handler

    reg = {**REGISTRATION, "defaults": {**REGISTRATION["defaults"], "failOnTableErrors": False}}
    env["ssm"].put_parameter(Name="/dms-orch/test/ledger/core", Type="String", Value=json.dumps(reg), Overwrite=True)
    out = handler({"request": {"project": "ledger", "taskKey": "core"}, "execution": EXECUTION})
    assert out["request"]["failOnTableErrors"] is False
    req = {"project": "ledger", "taskKey": "core", "failOnTableErrors": True}
    out = handler({"request": req, "execution": {**EXECUTION, "name": EXECUTION["name"] + "x"}})
    assert out["request"]["failOnTableErrors"] is True


def test_resolve_context_rejects_invalid_request(env):
    from lambdas.resolve_context.handler import handler
    from shared.errors import InvalidRunRequest

    with pytest.raises(InvalidRunRequest):
        handler({"request": {"project": "ledger", "taskKey": "core", "operation": "nuke"}, "execution": EXECUTION})


def test_resolve_context_unknown_task(env):
    from lambdas.resolve_context.handler import handler
    from shared.errors import UnknownTask

    with pytest.raises(UnknownTask):
        handler({"request": {"project": "ledger", "taskKey": "other"}, "execution": EXECUTION})


def test_resolve_context_missing_jobset_version(env):
    from lambdas.resolve_context.handler import handler
    from shared.errors import JobSetNotFound

    register_jobs(env, bundles={**REGISTRATION["jobs"]["bundles"], "v9": "9" * 64})
    with pytest.raises(JobSetNotFound):
        handler({"request": {"project": "ledger", "taskKey": "core", "jobsVersion": "v9"}, "execution": EXECUTION})


def test_request_overrides_task_defaults(env):
    from lambdas.resolve_context.handler import handler

    out = handler(
        {
            "request": {
                "project": "ledger",
                "taskKey": "core",
                "targetReset": {"policy": "never"},
                "autoResume": {"maxAttempts": 0},
                "completion": "task-stopped",
            },
            "execution": EXECUTION,
        }
    )
    assert out["request"]["targetReset"]["policy"] == "never"
    assert out["request"]["autoResume"]["maxAttempts"] == 0
    assert out["request"]["completion"] == "task-stopped"


# --- inspect_task ------------------------------------------------------------------


def _ctx(jobs_reset: bool = True) -> dict[str, Any]:
    return {
        "taskArn": TASK_ARN,
        "project": "ledger",
        "taskKey": "core",
        "endpointArns": REGISTRATION["endpointArns"],
        "replicationInstanceArn": REGISTRATION["replicationInstanceArn"],
        "jobs": {"pre": [], "reset": [{"name": "reset-target"}] if jobs_reset else [], "post": [], "on_failure": []},
    }


def _req(**kw: Any) -> dict[str, Any]:
    base = {
        "project": "ledger",
        "taskKey": "core",
        "operation": "migrate",
        "startType": "auto",
        "completion": "auto",
        "targetReset": {"policy": "if-interrupted"},
    }
    return {**base, **kw}


def test_inspect_fresh_task(table):
    from lambdas.inspect_task.handler import handler

    task = dms_task("ready")
    task["ReplicationTaskSettings"] = json.dumps({"FullLoadSettings": {"TargetTablePrepMode": "DO_NOTHING"}})
    dms = FakeDms(task)
    dms.connections = [{"EndpointArn": "e", "EndpointIdentifier": "src", "Status": "successful"}]
    use(dms=dms)
    out = handler({"runId": "r1", "request": _req(), "ctx": _ctx(), "attempt": 1})
    assert out["state"] == "fresh"
    assert out["decision"]["action"] == "start" and out["decision"]["startType"] == "start-replication"
    assert out["completionSpec"]["mode"] == "task-stopped"
    assert out["warnings"] == []
    assert "describe_table_statistics" not in [c[0] for c in dms.calls]


def test_inspect_interrupted_task_resets_partial_tables(table):
    from lambdas.inspect_task.handler import handler

    task = dms_task("stopped", "Stop Reason NORMAL", StartDate="2026-10-26T10:00:00Z")
    tables = [
        {"SchemaName": "public", "TableName": "a", "TableState": "Table completed"},
        {"SchemaName": "public", "TableName": "b", "TableState": "Full load"},
    ]
    use(dms=FakeDms(task, tables))
    out = handler({"runId": "r1", "request": _req(), "ctx": _ctx(), "attempt": 1})
    assert out["state"] == "interrupted"
    assert out["decision"]["startType"] == "resume-processing"
    assert out["decision"]["resetTables"] == ["public.b"]


def test_inspect_rejects_running_task(table):
    from lambdas.inspect_task.handler import handler

    use(dms=FakeDms(dms_task("running")))
    out = handler({"runId": "r1", "request": _req(), "ctx": _ctx(), "attempt": 1})
    assert out["decision"] == {**out["decision"], "action": "reject", "error": "TaskAlreadyRunning"}


def test_inspect_plan_reports_destructive_prep_mode_and_warns_on_a_bad_connection(table):
    from lambdas.inspect_task.handler import handler

    task = dms_task("ready")
    task["ReplicationTaskSettings"] = json.dumps({"FullLoadSettings": {"TargetTablePrepMode": "DROP_AND_CREATE"}})
    dms = FakeDms(task)
    dms.connections = [{"EndpointIdentifier": "src", "Status": "failed", "LastFailureMessage": "timeout"}]
    use(dms=dms)
    out = handler({"runId": "r1", "request": _req(operation="plan"), "ctx": _ctx(), "attempt": 1})
    assert len(out["warnings"]) == 1 and "TargetTablePrepMode=DROP_AND_CREATE" in out["configProblems"][0]


def test_inspect_missing_task(table):
    from lambdas.inspect_task.handler import handler
    from shared.errors import DmsTaskNotFound

    use(dms=FakeDms(None))
    with pytest.raises(DmsTaskNotFound):
        handler({"runId": "r1", "request": _req(), "ctx": _ctx(), "attempt": 1})


FINISHED = dms_task("stopped", "Stop Reason FULL_LOAD_ONLY_FINISHED", StopDate="2026-10-27T10:30:00Z")


# --- classify_failure ----------------------------------------------------------------


class FakeLogs:
    def __init__(self, messages: list[str] | None = None) -> None:
        self.messages = messages or []

    def filter_log_events(self, **_kw: Any) -> dict[str, Any]:
        return {"events": [{"message": m} for m in self.messages]}


def _classify(failure, task=None, started=True, attempt=1, logs=None, auto=None):
    from lambdas.classify_failure.handler import classify

    req = {"autoResume": auto or {"maxAttempts": 1, "onlyCategories": ["TRANSIENT_SOURCE_CONN", "DMS_RESTART"]}}
    ctx = {"taskArn": TASK_ARN, "logGroupName": "g", "taskResourceId": "ABCDEF"}
    event = {"failure": failure, "ctx": ctx, "request": req, "started": started, "attempt": attempt}
    return classify(event, FakeDms(task), logs or FakeLogs())


@pytest.mark.parametrize(
    "error,message,category,retryable",
    [
        ("DmsTaskFailed", "ERROR: permission denied for table accounts", "PERMISSIONS", False),
        ("DmsTaskFailed", "FATAL: password authentication failed for user dms", "PERMISSIONS", False),
        ("DmsTaskFailed", "Error establishing connection to source endpoint: timeout", "TRANSIENT_SOURCE_CONN", True),
        ("DmsTaskFailed", "Lost connection to target database", "TRANSIENT_TARGET_CONN", True),
        ("DmsTaskFailed", "duplicate key value violates unique constraint", "DATA", False),
        ("DmsTaskFailed", "STOPPED_DUE_TO_LOW_MEMORY: x", "RESOURCE", False),
        ("DmsTaskFailed", "something nobody has seen", "UNKNOWN", False),
        ("JobFailed", "{}", "JOB_FAILED", False),
        ("WaitTimeout", "heartbeat", "WAIT_TIMEOUT", False),
        ("DmsTaskDeleted", "", "TASK_DELETED", False),
    ],
)
def test_classify_categories(error, message, category, retryable):
    out = _classify({"Error": error, "Cause": message}, task=dms_task("failed"))
    assert (out["category"], out["retryable"]) == (category, retryable)


def test_classify_external_stop_and_dms_restart():
    stopped = dms_task("stopped", "Stop Reason NORMAL")
    assert _classify({"Error": "DmsTaskStoppedUnexpectedly"}, task=stopped)["category"] == "EXTERNAL_STOP"
    recycled = dms_task("stopped", "Stop Reason RECYCLE_TASK")
    out = _classify({"Error": "DmsTaskStoppedUnexpectedly"}, task=recycled)
    assert out["category"] == "DMS_RESTART" and out["autoResume"] is True


def test_classify_uses_last_failure_message_and_log_tail():
    task = dms_task("failed", last_failure="Failed to connect to source database")
    out = _classify({"Error": "DmsTaskFailed", "Cause": "{}"}, task=task)
    assert out["category"] == "TRANSIENT_SOURCE_CONN" and out["autoResume"] is True
    logs = FakeLogs(["]E: permission denied for relation x"])
    assert _classify({"Error": "DmsTaskFailed", "Cause": "{}"}, task=dms_task("failed"), logs=logs)["category"] == (
        "PERMISSIONS"
    )


def test_classify_auto_resume_limits():
    task = dms_task("failed", last_failure="connection to source timed out")
    failure = {"Error": "DmsTaskFailed", "Cause": "x"}
    assert _classify(failure, task=task, attempt=2)["autoResume"] is False  # 1 resume already used
    assert _classify(failure, task=task, started=False)["autoResume"] is False  # never started
    out = _classify(failure, task=task, auto={"maxAttempts": 3, "onlyCategories": ["DATA"]})
    assert out["autoResume"] is False and "not in autoResume.onlyCategories" in out["notes"][0]


# --- write_report ------------------------------------------------------------------


def test_write_report_puts_json_and_markdown(env, store):
    from lambdas.write_report.handler import handler

    store.put_run("run-1", {"project": "ledger", "status": "RUNNING", "startedAt": "2026-10-27T10:00:00Z"})
    store.append_event("run-1", "sfn", "resolve", {"x": 1})
    tables = [{"SchemaName": "public", "TableName": "a", "TableState": "Table completed", "FullLoadRows": 10}]
    use(dms=FakeDms(FINISHED, tables))
    out = handler(
        {
            "runId": "run-1",
            "status": "SUCCEEDED",
            "request": {"project": "ledger", "taskKey": "core", "dryRun": False, "notify": {"onSuccess": True}},
            "ctx": {"taskArn": TASK_ARN, "environment": "test"},
            "inspect": {"state": "fresh", "decision": {"action": "start", "startType": "start-replication"}},
            "dmsOutcome": {"outcome": "COMPLETED", "completedBy": "event-handler"},
            "started": True,
            "attempt": 1,
        }
    )
    assert out["reportS3Uri"] == f"s3://{BUCKET}/runs/ledger/run-1/report.json" and out["notify"] is True
    report = json.loads(env["s3"].get_object(Bucket=BUCKET, Key="runs/ledger/run-1/report.json")["Body"].read())
    assert report["outcome"] == "COMPLETED" and report["tables"][0]["fullLoadRows"] == 10
    md = env["s3"].get_object(Bucket=BUCKET, Key="runs/ledger/run-1/report.md")["Body"].read().decode()
    assert "| public.a | Table completed | 10 | 0 |" in md
    assert store.get_run("run-1")["status"] == "SUCCEEDED"
    assert json.loads(out["message"])["status"] == "SUCCEEDED"


def test_write_report_failure_contains_classification_and_respects_notify(env, store):
    from lambdas.write_report.handler import handler

    store.put_run("run-2", {"project": "ledger"})
    use(dms=FakeDms(None))
    out = handler(
        {
            "runId": "run-2",
            "status": "FAILED",
            "request": {"project": "ledger", "taskKey": "core", "notify": {"onFailure": False}},
            "ctx": {"taskArn": TASK_ARN},
            "failure": {"Error": "DmsTaskFailed", "Cause": "boom"},
            "classification": {"category": "PERMISSIONS", "retryable": False, "reason": "permission denied"},
            "started": False,
        }
    )
    assert out["notify"] is False and "FAILED" in out["subject"]
    md = env["s3"].get_object(Bucket=BUCKET, Key="runs/ledger/run-2/report.md")["Body"].read().decode()
    assert "`PERMISSIONS`" in md


def test_inspect_on_auto_resume_waits_for_a_task_dms_restarted_itself(table):
    from lambdas.inspect_task.handler import handler

    use(dms=FakeDms(dms_task("running", StartDate="2026-10-27T10:20:00Z")))
    event = {"runId": "r1", "request": _req(), "ctx": _ctx(), "attempt": 2, "startedAt": "2026-10-27T10:00:00Z"}
    out = handler(event)
    assert out["decision"]["action"] == "wait"


def test_classify_wait_timeout_rechecks_dms_and_recovers_a_finished_task():
    from lambdas.classify_failure.handler import classify

    event = {
        "failure": {"Error": "WaitTimeout", "Cause": "heartbeat"},
        "ctx": {"taskArn": TASK_ARN},
        "request": {},
        "started": True,
        "attempt": 1,
        "completion": {"mode": "task-stopped"},
        "startedAt": "2026-10-27T10:00:00Z",
    }
    out = classify(event, FakeDms(FINISHED), FakeLogs())
    assert out["recovered"] is True and out["dmsOutcome"]["outcome"] == "COMPLETED"
    still_running = classify(event, FakeDms(dms_task("running")), FakeLogs())
    assert still_running["recovered"] is False and still_running["category"] == "WAIT_TIMEOUT"


def test_classify_untested_endpoint_is_a_config_problem_not_a_connection_drop():
    # Text observed in the sandbox (docs/dms-observations.md): StartReplicationTask
    # on endpoints that never passed TestConnection.
    message = (
        "Test connection for replication instance dms-orch-sbx and endpoint dms-orch-sbx-source "
        "should be successful for starting the replication task (Service: DatabaseMigration, Status Code: 400)"
    )
    out = _classify({"Error": "DatabaseMigration.InvalidResourceStateException", "Cause": message}, started=False)
    assert (out["category"], out["retryable"], out["autoResume"]) == ("CONFIG", False, False)


# --- sandbox findings: table errors and the DMS task log -----------------------------

OBSERVED_TABLE_ERROR_LOG = [
    "2026-10-27T13:01:34 [SOURCE_UNLOAD   ]E:  RetCode: SQL_ERROR  SqlState: 42501 NativeError: 1 Message: "
    "ERROR: permission denied for table transactions; Error while executing the query [1022502]",
    "2026-10-27T13:01:34 [TASK_MANAGER    ]W:  Table 'app'.'transactions' was errored/suspended (subtask 2 thread 0).",
]


class RecordingLogs(FakeLogs):
    def __init__(self, messages: list[str]) -> None:
        super().__init__(messages)
        self.kwargs: dict[str, Any] = {}

    def filter_log_events(self, **kw: Any) -> dict[str, Any]:
        self.kwargs = kw
        return super().filter_log_events(**kw)


def test_log_tail_filter_pattern_is_valid_cloudwatch_syntax():
    # CloudWatch rejects unquoted ':' ("Invalid character(s) in term ':'").
    logs = RecordingLogs([])
    _classify({"Error": "DmsTaskFailed", "Cause": "x"}, task=dms_task("failed"), logs=logs)
    pattern = logs.kwargs["filterPattern"]
    assert '"]E:"' in pattern and " E: " not in f" {pattern} " and "?E:" not in pattern


def test_table_errors_take_their_category_from_the_task_log():
    task = dms_task("stopped", "Stop Reason FULL_LOAD_ONLY_FINISHED", TablesErrored=1)
    out = _classify(
        {"Error": "DmsTableErrors", "Cause": "1 table(s) in error state"},
        task=task,
        logs=FakeLogs(OBSERVED_TABLE_ERROR_LOG),
    )
    assert out["category"] == "PERMISSIONS"
    plain = _classify({"Error": "DmsTableErrors", "Cause": "1 table(s) in error state"}, task=task)
    assert plain["category"] == "DATA"


class PagedLogs:
    """FilterLogEvents may return empty pages with a nextToken (observed in the sandbox)."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.calls: list[dict[str, Any]] = []

    def filter_log_events(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        if "nextToken" not in kw:
            return {"events": [], "nextToken": "p2"}
        return {"events": [{"message": m} for m in self.messages]}


def test_log_tail_follows_pages_and_starts_at_the_task_start():
    task = dms_task("stopped", "Stop Reason FULL_LOAD_ONLY_FINISHED", TablesErrored=1, StartDate="2026-10-27T13:00:00Z")
    logs = PagedLogs(OBSERVED_TABLE_ERROR_LOG)
    out = _classify({"Error": "DmsTableErrors", "Cause": "1 table(s) in error state"}, task=task, logs=logs)
    assert out["category"] == "PERMISSIONS" and len(out["logTail"]) == 2
    assert logs.calls[0]["startTime"] == int(datetime(2026, 10, 27, 13, 0, tzinfo=UTC).timestamp() * 1000) - 60_000


# --- post loads the run that migrate saved -----------------------------------

POST_EXECUTION = {
    "arn": "arn:aws:states:eu-west-2:111122223333:execution:dms-orch-test-post:run-1-post",
    "name": "run-1-post",
    "startTime": "2026-10-28T10:31:00Z",
}
SAVED_CONTEXT = {
    "runId": "run-1",
    "request": {"project": "ledger", "taskKey": "core", "operation": "migrate", "dryRun": False},
    "ctx": {"taskArn": TASK_ARN, "project": "ledger", "executionArn": EXEC_ARN, "jobs": {"post": []}},
    "inspect": {"state": "fresh", "completionSpec": {"mode": "task-stopped"}},
    "jobCtx": {"taskArn": TASK_ARN, "startType": "start-replication", "attempt": 1},
    "attempt": 1,
    "startedAt": "2026-10-28T10:00:00.000Z",
    "preflight": {"verdict": "resume", "diff": {}, "warnings": []},
}


def _pin_context(env, body: bytes, run_id: str = "run-1", project: str = "ledger") -> str:
    """What migrate does: SaveContext (versioned bucket), then RecordContextVersion on RUN#."""
    from shared.store import Store

    key = f"runs/{project}/{run_id}/context.json"
    version = env["s3"].put_object(Bucket=BUCKET, Key=key, Body=body)["VersionId"]
    Store(env["table"]).update_run(run_id, {"project": project, "contextVersionId": version})
    return version


def _save_context(env) -> None:
    _pin_context(env, json.dumps(SAVED_CONTEXT).encode())


def test_context_key_is_per_project():
    from lambdas.resolve_context.handler import context_key

    assert context_key("ledger", "run-1") == "runs/ledger/run-1/context.json"


def _post_event(run_id: str = "run-1") -> dict[str, Any]:
    outcome = {"kind": "SUCCESS", "outcome": "COMPLETED", "error": None, "cause": "full load finished", "task": {}}
    return {
        "mode": "post",
        "input": {"runId": run_id, "taskArn": TASK_ARN, "outcome": outcome, "startedBy": "event-handler"},
        "execution": POST_EXECUTION,
    }


def test_post_loads_the_pinned_version_not_a_later_overwrite(env, store):
    """Review finding 1: whatever writes context.json after migrate, post reads the version migrate recorded."""
    from lambdas.resolve_context.handler import handler

    _save_context(env)
    forged = {**SAVED_CONTEXT, "request": {**SAVED_CONTEXT["request"], "operation": "forged"}}
    env["s3"].put_object(Bucket=BUCKET, Key="runs/ledger/run-1/context.json", Body=json.dumps(forged).encode())
    assert handler(_post_event())["request"] == SAVED_CONTEXT["request"]


def test_post_refuses_a_context_without_a_recorded_version(env, store):
    from lambdas.resolve_context.handler import handler
    from shared.errors import ContextNotPinned

    env["s3"].put_object(Bucket=BUCKET, Key="runs/ledger/run-1/context.json", Body=json.dumps(SAVED_CONTEXT).encode())
    store.put_run("run-1", {"project": "ledger", "status": "STARTED"})
    with pytest.raises(ContextNotPinned, match="contextVersionId"):
        handler(_post_event())


def test_post_refuses_a_recorded_version_that_does_not_exist(env, store):
    from lambdas.resolve_context.handler import handler
    from shared.errors import ContextNotPinned

    _save_context(env)
    store.update_run("run-1", {"contextVersionId": "not-a-version"})
    with pytest.raises(ContextNotPinned):
        handler(_post_event())


def test_post_reads_the_context_of_the_project_recorded_on_the_run(env, store):
    """The project comes from RUN#, not from the post input: another project's prefix is never read."""
    from lambdas.resolve_context.handler import handler
    from shared.errors import ContextNotPinned

    _pin_context(env, json.dumps(SAVED_CONTEXT).encode(), project="cards")
    out = handler(_post_event())
    assert out["runId"] == "run-1"
    store.update_run("run-1", {"project": "ledger"})
    with pytest.raises(ContextNotPinned):
        handler(_post_event())


def test_resolve_context_post_mode_loads_the_saved_run_and_the_success(env, store):
    from lambdas.resolve_context.handler import handler

    store.put_run("run-1", {"project": "ledger", "status": "STARTED", "executionArn": EXEC_ARN})
    _save_context(env)
    outcome = {"kind": "SUCCESS", "outcome": "COMPLETED", "error": None, "cause": "full load finished", "task": {}}
    event = {
        "mode": "post",
        "input": {"runId": "run-1", "taskArn": TASK_ARN, "outcome": outcome, "startedBy": "event-handler"},
        "execution": POST_EXECUTION,
    }
    out = handler(event)
    assert out["runId"] == "run-1" and out["request"] == SAVED_CONTEXT["request"]
    assert out["jobCtx"]["startType"] == "start-replication" and out["inspect"]["state"] == "fresh"
    assert out["ctx"]["postExecutionArn"] == POST_EXECUTION["arn"]
    assert out["dmsOutcome"] == {
        "outcome": "COMPLETED",
        "cause": "full load finished",
        "dms": {},
        "completedBy": "event-handler",
    }
    assert out["failure"] is None and out["attempt"] == 1
    assert out["preflight"] == SAVED_CONTEXT["preflight"]
    run = store.get_run("run-1")
    assert run["postExecutionArn"] == POST_EXECUTION["arn"] and run["phase"] == "post" and run["status"] == "RUNNING"


def test_resolve_context_post_mode_reads_a_context_saved_as_a_json_string(env, store):
    """Seen live: Step Functions wrote context.json as a JSON string holding the JSON (a string Body
    is encoded once more by the S3 SDK integration). post decodes it instead of failing."""
    from lambdas.resolve_context.handler import handler

    body = json.dumps(json.dumps(SAVED_CONTEXT)).encode()
    _pin_context(env, body)
    outcome = {"kind": "SUCCESS", "outcome": "COMPLETED", "error": None, "cause": "full load finished", "task": {}}
    event = {
        "mode": "post",
        "input": {"runId": "run-1", "taskArn": TASK_ARN, "outcome": outcome, "startedBy": "event-handler"},
        "execution": POST_EXECUTION,
    }
    out = handler(event)
    assert out["request"] == SAVED_CONTEXT["request"] and out["ctx"]["postExecutionArn"] == POST_EXECUTION["arn"]


def test_resolve_context_post_mode_turns_a_dms_failure_into_a_failure(env, store):
    from lambdas.resolve_context.handler import handler

    _save_context(env)
    outcome = {"kind": "FAILURE", "outcome": None, "error": "DmsTaskFailed", "cause": "source gone", "task": {"x": 1}}
    out = handler(
        {
            "mode": "post",
            "input": {"runId": "run-1", "taskArn": TASK_ARN, "outcome": outcome},
            "execution": POST_EXECUTION,
        }
    )
    assert out["dmsOutcome"] is None and out["failure"]["Error"] == "DmsTaskFailed"
    assert json.loads(out["failure"]["Cause"])["message"] == "source gone"


def test_resolve_context_post_mode_without_saved_context_fails(env):
    from lambdas.resolve_context.handler import handler
    from shared.errors import InvalidRunRequest

    with pytest.raises(InvalidRunRequest):
        handler(
            {
                "mode": "post",
                "input": {"runId": "nope", "taskArn": TASK_ARN, "outcome": {}},
                "execution": POST_EXECUTION,
            }
        )


CHAINED = {
    "project": "ledger",
    "taskKey": "core",
    "operation": "resume",
    "attempt": 2,
    "parentRunId": "run-1",
    "parentStartedAt": "2026-10-28T10:00:00.000Z",
}


def test_resolve_context_returns_the_attempt_of_a_chained_resume(env, store):
    from lambdas.resolve_context.handler import handler

    store.put_run("run-1", {"project": "ledger", "resumeChildRunId": "run-1-r2"})
    out = handler({"request": dict(CHAINED), "execution": {**EXECUTION, "name": "run-1-r2"}})
    assert out["attempt"] == 2 and out["request"]["parentRunId"] == "run-1"
    assert out["ctx"]["chain"] == {"dumped": False, "dumpUri": None}
    first = handler({"request": {"project": "ledger", "taskKey": "core"}, "execution": EXECUTION})
    assert first["attempt"] == 1 and first["ctx"]["chain"] == {"dumped": False, "dumpUri": None}


@pytest.mark.parametrize(
    ("parent", "fields"),
    [
        (None, {"attempt": 2, "parentRunId": "run-1"}),
        ({"project": "ledger"}, {"attempt": 2, "parentRunId": "run-1"}),
        ({"project": "ledger", "resumeChildRunId": "run-1-r9"}, {"attempt": 2, "parentRunId": "run-1"}),
        ({"project": "ledger", "resumeChildRunId": "run-1-r2"}, {"attempt": 2}),
        ({"project": "ledger", "resumeChildRunId": "run-1-r2"}, {"parentRunId": "run-1"}),
        ({"project": "ledger", "resumeChildRunId": "run-1-r2"}, {"parentStartedAt": "2026-10-28T10:00:00.000Z"}),
        ({"project": "other", "resumeChildRunId": "run-1-r2"}, {"attempt": 2, "parentRunId": "run-1"}),
    ],
)
def test_resolve_context_rejects_chain_fields_post_did_not_hand_over(env, store, parent, fields):
    """Review finding 5: attempt / parentRunId from an operator skipped the pre-jobs and the dump.
    They are accepted only for the execution post named on the parent run (HandOverLock)."""
    from lambdas.resolve_context.handler import handler
    from shared.errors import InvalidRunRequest

    if parent:
        store.put_run("run-1", parent)
    req = {"project": "ledger", "taskKey": "core", "operation": "resume", **fields}
    with pytest.raises(InvalidRunRequest):
        handler({"request": req, "execution": {**EXECUTION, "name": "run-1-r2"}})
    assert store.get_run("run-1-r2") is None


def test_resolve_context_carries_the_dump_of_the_chain(env, store):
    from lambdas.resolve_context.handler import handler

    dump = "s3://b/dumps/ledger/core/t-run-1/"
    store.put_run("run-1", {"project": "ledger", "resumeChildRunId": "run-1-r2", "chainDumped": True, "dumpUri": dump})
    out = handler({"request": dict(CHAINED), "execution": {**EXECUTION, "name": "run-1-r2"}})
    assert out["ctx"]["chain"] == {"dumped": True, "dumpUri": dump}
    run = store.get_run("run-1-r2")
    assert run["chainDumped"] is True and run["dumpUri"] == dump and run["parentRunId"] == "run-1"


def _report(handler, status: str, notify: dict | None = None):
    return handler(
        {
            "runId": "run-1",
            "status": status,
            "request": {"project": "ledger", "taskKey": "core", "notify": notify or {}},
            "ctx": {"taskArn": TASK_ARN},
            "started": True,
            "attempt": 1,
        }
    )


def test_write_report_started_notifies_only_when_asked_and_marks_the_dms_phase(env, store):
    from lambdas.write_report.handler import handler

    store.put_run("run-1", {"project": "ledger", "executionArn": EXEC_ARN})
    use(dms=FakeDms(dms_task("running")))
    assert _report(handler, "STARTED")["notify"] is False
    run = store.get_run("run-1")
    assert run["status"] == "STARTED" and run["phase"] == "dms"
    assert _report(handler, "STARTED", {"onStart": True})["notify"] is True


def test_write_report_resumed_is_quiet_and_lists_both_executions(env, store):
    from lambdas.write_report.handler import handler

    post_arn = POST_EXECUTION["arn"]
    store.put_run("run-1", {"project": "ledger", "executionArn": EXEC_ARN, "postExecutionArn": post_arn})
    use(dms=FakeDms(FINISHED))
    out = _report(handler, "RESUMED", {"onSuccess": True, "onFailure": True})
    assert out["notify"] is False
    report = json.loads(env["s3"].get_object(Bucket=BUCKET, Key="runs/ledger/run-1/report.json")["Body"].read())
    assert report["executions"] == {"migrate": EXEC_ARN, "post": post_arn}
    assert store.get_run("run-1")["phase"] == "done"


# --- pre-flight verdict in inspect_task (ADR 0006) -------------------

MAPPING = {
    "rules": [
        {
            "rule-type": "selection",
            "rule-id": "1",
            "object-locator": {"schema-name": "app", "table-name": "%"},
            "rule-action": "include",
        }
    ]
}


def _fingerprint(rows: int, upd: int = 0) -> dict[str, Any]:
    t = {"columns": [["id", "bigint", False]], "primaryKey": ["id"], "uniques": [], "exact": True, "del": 0}
    return {
        "schemaVersion": 1,
        "schemas": ["app"],
        "tables": {"app.a": {**t, "rows": rows, "ins": rows, "upd": upd}},
        "foreignKeys": [],
        "sequences": {},
        "controlTables": [],
        "outOfScope": [],
        "expected": ["app.a"],
    }


def _put_fp(env, key: str, fp: dict[str, Any]) -> str:
    env["s3"].put_object(Bucket=BUCKET, Key=key, Body=json.dumps(fp).encode())
    return f"s3://{BUCKET}/{key}"


def _preflight(handler, *, state: str, decision: dict[str, Any], verify: Any, request: dict[str, Any] | None = None):
    return handler(
        {
            "mode": "preflight",
            "runId": "r2",
            "request": request or _req(),
            "ctx": {**_ctx(), "jobs": {**_ctx()["jobs"], "pre": [{"name": "ddl"}]}},
            "inspect": {"state": state, "decision": decision, "tables": []},
            "verify": verify,
            "iteration": 0,
        }
    )


def test_inspect_reports_the_selection_rules_of_the_task(table):
    from lambdas.inspect_task.handler import handler

    task = dms_task("ready")
    task["TableMappings"] = json.dumps(MAPPING)
    use(dms=FakeDms(task))
    out = handler({"runId": "r1", "request": _req(), "ctx": _ctx(), "attempt": 1})
    assert out["selectionRules"] == [{"schema": "app", "table": "%", "action": "include"}]


def test_preflight_fresh_task_with_rows_in_the_target_is_manual_or_reset(env, store):
    from lambdas.inspect_task.handler import handler

    uri = _put_fp(env, "runs/r2/jobs/preflight/00-target-verify/target-fingerprint.json", _fingerprint(5))
    verify = {"name": "target-verify", "status": "ok", "summary": {"fingerprintUri": uri}}
    start = {"action": "start", "startType": "start-replication", "reset": False, "resetTables": []}
    out = _preflight(handler, state="fresh", decision=start, verify=verify)
    assert out["verdict"] == "manual" and out["error"] == "TargetNotEmpty"
    assert out["fingerprint"]["uri"] == uri and out["fingerprint"]["hash"].startswith("sha256:")
    out = _preflight(handler, state="fresh", decision=start, verify=verify, request=_req(allowReset=True))
    assert out["verdict"] == "reset" and out["decision"]["resetTables"] == ["app.a"]
    assert any(e.get("phase") == "preflight" for e in store.run_items("r2", "EVENT#"))


def test_preflight_interrupted_task_compares_with_the_baseline(env, store):
    from lambdas.inspect_task.handler import handler

    base_uri = _put_fp(env, "runs/r1/jobs/baseline/00-target-verify/target-fingerprint.json", _fingerprint(5))
    store.put_target_baseline("ledger/core", run_id="r1", fingerprint_hash="sha256:x", fingerprint_uri=base_uri)
    resume = {"action": "start", "startType": "resume-processing", "reset": False, "resetTables": []}
    same = _put_fp(env, "runs/r2/a.json", _fingerprint(5))
    out = _preflight(
        handler, state="interrupted", decision=resume, verify={"status": "ok", "summary": {"fingerprintUri": same}}
    )
    assert out["verdict"] == "resume" and out["baseline"]["runId"] == "r1"
    edited = _put_fp(env, "runs/r2/b.json", _fingerprint(5, upd=2))
    out = _preflight(
        handler, state="interrupted", decision=resume, verify={"status": "ok", "summary": {"fingerprintUri": edited}}
    )
    assert out["verdict"] == "manual" and out["diff"]["rowsChanged"] == ["app.a"]


def test_preflight_after_a_restore_needs_a_full_reload(env, store):
    """Seen live: after orch.py restore the baseline was still that of the failed run, so the next run
    stopped MANUAL/TargetChanged. The restore records the restored target as the baseline."""
    from lambdas.inspect_task.handler import handler

    restored = _put_fp(env, "runs/restore-x/jobs/restore/00-target-verify/target-fingerprint.json", _fingerprint(5))
    store.put_target_baseline(
        "ledger/core",
        run_id="r1",
        fingerprint_hash="sha256:x",
        fingerprint_uri=restored,
        source="restore",
        extra={"restoreRunId": "restore-x", "dumpUri": "s3://b/dumps/ledger/core/t-r1/"},
    )
    resume = {"action": "start", "startType": "resume-processing", "reset": False, "resetTables": []}
    now = _put_fp(env, "runs/r2/a.json", _fingerprint(5))
    event = {
        "mode": "preflight",
        "runId": "r2",
        "request": _req(),
        "ctx": _ctx(),
        "inspect": {
            "state": "interrupted",
            "decision": resume,
            "tables": [{"table": "app.a", "state": "Table completed", "fullLoadRows": 5}],
        },
        "verify": {"status": "ok", "summary": {"fingerprintUri": now}},
        "iteration": 0,
    }
    out = handler(event)
    # Not TargetChanged any more, and never resume: DMS still says app.a is loaded.
    assert (out["verdict"], out["error"]) == ("manual", "RestoredTarget"), out
    assert out["reason"] == "target restored from dump r1; a full reload is needed: re-run with --allow-reset"
    assert out["baseline"]["source"] == "restore" and out["baseline"]["restoreRunId"] == "restore-x"
    out = handler({**event, "request": _req(allowReset=True)})
    assert out["verdict"] == "reset" and out["decision"]["startType"] == "reload-target"


def test_preflight_fresh_task_with_table_history_is_manual(env, store):
    from lambdas.inspect_task.handler import handler

    store.put_table_states(
        "ledger/core", run_id="r0", tables=[{"SchemaName": "app", "TableName": "a", "TableState": "Table completed"}]
    )
    uri = _put_fp(env, "runs/r2/c.json", _fingerprint(0))
    start = {"action": "start", "startType": "start-replication", "reset": False, "resetTables": []}
    out = _preflight(
        handler, state="fresh", decision=start, verify={"status": "ok", "summary": {"fingerprintUri": uri}}
    )
    assert out["verdict"] == "manual" and "earlier runs" in out["reason"]


def test_preflight_without_a_fingerprint_is_manual_unless_allowed(env):
    from lambdas.inspect_task.handler import handler

    start = {"action": "start", "startType": "start-replication", "reset": False, "resetTables": []}
    out = _preflight(handler, state="fresh", decision=start, verify=None, request=_req(allowReset=True))
    assert (out["verdict"], out["error"]) == ("manual", "NoFingerprint") and out["fingerprint"] is None
    req = _req(allowReset=True, preflight={"allowNoFingerprint": True})
    out = _preflight(handler, state="fresh", decision=start, verify=None, request=req)
    assert out["verdict"] == "ready" and out["decision"]["reset"] is False and out["warnings"]


def test_preflight_reset_scope_follows_the_selection_rules(env):
    from lambdas.inspect_task.handler import handler

    fp = _fingerprint(5)
    fp["tables"]["app.tmp_x"] = fp["tables"]["app.a"]
    uri = _put_fp(env, "runs/r2/d.json", fp)
    rules = [
        {"schema": "app", "table": "%", "action": "include"},
        {"schema": "app", "table": "tmp_%", "action": "exclude"},
    ]
    start = {"action": "start", "startType": "start-replication", "reset": False, "resetTables": []}
    event = {
        "mode": "preflight",
        "runId": "r2",
        "request": _req(allowReset=True),
        "ctx": _ctx(),
        "inspect": {"state": "fresh", "decision": start, "tables": [{"table": "app.zz"}], "selectionRules": rules},
        "verify": {"status": "ok", "summary": {"fingerprintUri": uri}},
        "iteration": 0,
    }
    out = handler(event)
    assert out["verdict"] == "reset" and out["decision"]["resetTables"] == ["app.a"]
    assert out["decision"]["resetTablesCount"] == 1 and out["decision"]["resetTablesUri"] is None


def test_preflight_puts_a_long_reset_list_in_s3_and_its_count_in_the_payload(env):
    from lambdas.inspect_task.handler import MAX_TABLES_IN_PAYLOAD, handler

    fp = _fingerprint(5)
    one = fp["tables"].pop("app.a")
    fp["tables"] = {f"app.t{i:04d}": one for i in range(600)}
    fp["expected"] = sorted(fp["tables"])
    uri = _put_fp(env, "runs/r2/e.json", fp)
    start = {"action": "start", "startType": "start-replication", "reset": False, "resetTables": []}
    verify = {"status": "ok", "summary": {"fingerprintUri": uri}}
    out = _preflight(handler, state="fresh", decision=start, verify=verify, request=_req(allowReset=True))
    dec = out["decision"]
    assert out["verdict"] == "reset" and "truncate 600 table(s)" in out["reason"]
    assert dec["resetTables"] == [] and dec["resetTablesCount"] == 600 > MAX_TABLES_IN_PAYLOAD
    from shared.store import run_prefix

    key = f"{run_prefix(_ctx().get('project'), 'r2')}/inspect/reset-tables.json"
    assert dec["resetTablesUri"] == f"s3://{BUCKET}/{key}"
    stored = json.loads(env["s3"].get_object(Bucket=BUCKET, Key=key)["Body"].read())
    assert stored == {"resetTables": sorted(fp["tables"]), "count": 600}


def test_reset_tables_key_takes_the_run_prefix():
    from lambdas.inspect_task.handler import reset_tables_key

    assert reset_tables_key("runs/r2") == "runs/r2/inspect/reset-tables.json"
    assert reset_tables_key("runs/ledger/r2/") == "runs/ledger/r2/inspect/reset-tables.json"


def test_resolve_context_adds_the_target_check_when_the_target_has_the_data_api(env):
    from lambdas.resolve_context.handler import handler

    out = handler({"request": {"project": "ledger", "taskKey": "core"}, "execution": EXECUTION})
    assert out["ctx"]["jobs"]["preflight"] == []  # no Data API target: verdict from the DMS state alone

    target = {"engine": "postgresql", "secretArn": "arn:s", "dataApiClusterArn": "arn:aws:rds:eu-west-2:1:cluster:t"}
    reg = {**REGISTRATION, "connections": {**REGISTRATION["connections"], "target": target}}
    env["ssm"].put_parameter(Name="/dms-orch/test/ledger/core", Type="String", Value=json.dumps(reg), Overwrite=True)
    req = {"project": "ledger", "taskKey": "core", "preflight": {"exactCountMaxRows": 500}}
    out = handler({"request": req, "execution": {**EXECUTION, "name": "r-pf"}})
    (check,) = out["ctx"]["jobs"]["preflight"]
    assert check["handler"] == "builtin:checks/target_verify" and check["runtime"] == "lambda"
    assert check["with"] == {"connection": "target", "source": "source", "exactCountMaxRows": 500}
    assert check["idempotent"] is True and check["index"] == 0


def test_write_report_manual_is_notified_as_high_with_the_diff(env, store):
    from lambdas.write_report.handler import handler

    store.put_run("run-m", {"project": "ledger"})
    use(dms=FakeDms(dms_task("ready")))
    verdict = {
        "verdict": "manual",
        "reason": "the target changed since the last run (see diff)",
        "error": "TargetChanged",
        "diff": {"missing": [], "extra": ["app.junk"], "changed": [], "rowsChanged": ["app.tx"]},
        "warnings": ["1 object(s) outside the table mapping"],
    }
    out = handler(
        {
            "runId": "run-m",
            "status": "MANUAL",
            "request": {"project": "ledger", "taskKey": "core", "notify": {}},
            "ctx": {"taskArn": TASK_ARN},
            "preflight": verdict,
            "failure": {"Error": "TargetChanged", "Cause": verdict["reason"]},
            "started": False,
        }
    )
    assert out["notify"] is True and "MANUAL" in out["subject"]
    msg = json.loads(out["message"])
    assert msg["severity"] == "high" and msg["verdict"] == "manual" and msg["status"] == "MANUAL"
    report = json.loads(env["s3"].get_object(Bucket=BUCKET, Key="runs/ledger/run-m/report.json")["Body"].read())
    assert report["preflight"]["diff"]["extra"] == ["app.junk"]
    md = env["s3"].get_object(Bucket=BUCKET, Key="runs/ledger/run-m/report.md")["Body"].read().decode()
    assert "## Pre-flight" in md and "app.junk" in md and "app.tx" in md


def test_write_report_records_the_baseline_and_the_table_copy_at_the_end_of_post(env, store):
    from lambdas.write_report.handler import handler

    store.put_run("run-b", {"project": "ledger"})
    tables = [
        {"SchemaName": "app", "TableName": "a", "TableState": "Table completed", "FullLoadRows": 5},
        {"SchemaName": "app", "TableName": "b", "TableState": "Table error", "FullLoadRows": 0},
    ]
    use(dms=FakeDms(FINISHED, tables))
    baseline = {
        "name": "target-verify",
        "status": "ok",
        "summary": {
            "fingerprintUri": "s3://b/runs/run-b/fp.json",
            "fingerprintHash": "sha256:abc",
            "tables": 2,
            "rows": 5,
        },
    }
    event = {
        "runId": "run-b",
        "status": "FAILED",
        "request": {"project": "ledger", "taskKey": "core", "notify": {}},
        "ctx": {"taskArn": TASK_ARN, "project": "ledger", "taskKey": "core"},
        "baseline": baseline,
        "started": True,
    }
    out = handler(event)
    base = store.get_target_baseline("ledger/core")
    assert base["runId"] == "run-b" and base["fingerprintHash"] == "sha256:abc"
    assert base["summary"] == {"tables": 2, "rows": 5}
    assert {t["table"]: t["TableState"] for t in store.get_table_states("ledger/core")} == {
        "app.a": "Table completed",
        "app.b": "Table error",
    }
    report = json.loads(env["s3"].get_object(Bucket=BUCKET, Key="runs/ledger/run-b/report.json")["Body"].read())
    assert report["baseline"]["fingerprintHash"] == "sha256:abc" and json.loads(out["message"])["severity"] == "high"


def test_write_report_dry_run_records_no_baseline(env, store):
    from lambdas.write_report.handler import handler

    store.put_run("run-d", {"project": "ledger"})
    use(dms=FakeDms(FINISHED))
    baseline = {"status": "ok", "summary": {"fingerprintUri": "s3://b/x.json", "fingerprintHash": "sha256:x"}}
    handler(
        {
            "runId": "run-d",
            "status": "SUCCEEDED",
            "request": {"project": "ledger", "taskKey": "core", "dryRun": True, "notify": {}},
            "ctx": {"taskArn": TASK_ARN, "project": "ledger", "taskKey": "core"},
            "baseline": baseline,
            "started": True,
        }
    )
    assert store.get_target_baseline("ledger/core") is None and store.get_table_states("ledger/core") == []


def test_write_report_baseline_mode_records_without_writing_a_report(env, store):
    from lambdas.write_report.handler import handler

    tables = [{"SchemaName": "app", "TableName": "a", "TableState": "Table error", "FullLoadRows": 0}]
    use(dms=FakeDms(FINISHED, tables))
    check = {"status": "ok", "summary": {"fingerprintUri": "s3://b/fp.json", "fingerprintHash": "sha256:r"}}
    out = handler(
        {
            "mode": "baseline",
            "runId": "run-r",
            "status": "RESUMED",
            "request": {"project": "ledger", "taskKey": "core"},
            "ctx": {"taskArn": TASK_ARN, "project": "ledger", "taskKey": "core"},
            "baseline": check,
            "started": True,
        }
    )
    assert out["recorded"]["fingerprintHash"] == "sha256:r"
    assert store.get_target_baseline("ledger/core")["runId"] == "run-r"
    assert [t["table"] for t in store.get_table_states("ledger/core")] == ["app.a"]
    keys = env["s3"].list_objects_v2(Bucket=BUCKET, Prefix="runs/ledger/run-r/").get("KeyCount", 0)
    assert keys == 0


# --- SNS fan-out by message attributes --------------------------------


def test_notification_attributes_let_a_filtered_subscription_receive_only_what_it_asked_for(env, store):
    from lambdas.write_report.handler import handler

    sns = boto3.client("sns", region_name="eu-west-2")
    sqs = boto3.client("sqs", region_name="eu-west-2")
    topic = sns.create_topic(Name="platform-alerts")["TopicArn"]
    queue = sqs.create_queue(QueueName="oncall")["QueueUrl"]
    qarn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sns.subscribe(
        TopicArn=topic,
        Protocol="sqs",
        Endpoint=qarn,
        Attributes={
            "FilterPolicy": json.dumps({"severity": ["high"], "environment": ["test"]}),
            "RawMessageDelivery": "true",
        },
    )
    use(dms=FakeDms(FINISHED))
    for run_id, status in (("run-ok", "SUCCEEDED"), ("run-bad", "FAILED"), ("run-man", "MANUAL")):
        store.put_run(run_id, {"project": "ledger"})
        out = handler(
            {
                "runId": run_id,
                "status": status,
                "request": {"project": "ledger", "taskKey": "core", "notify": {}},
                "ctx": {"taskArn": TASK_ARN, "environment": "test", "project": "ledger", "taskKey": "core"},
                "started": False,
            }
        )
        attrs = out["attributes"]
        assert attrs["status"] == {"DataType": "String", "StringValue": status}
        assert set(attrs) == {"environment", "project", "taskKey", "status", "phase", "severity"}
        sns.publish(TopicArn=topic, Subject=out["subject"], Message=out["message"], MessageAttributes=attrs)
    got = sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=10).get("Messages", [])
    assert sorted(json.loads(m["Body"])["status"] for m in got) == ["FAILED", "MANUAL"]


def test_release_lock_notification_carries_attributes(store, table, monkeypatch):
    from lambdas.release_lock.handler import handler

    published: list[dict[str, Any]] = []

    class Sns:
        def publish(self, **kw: Any) -> dict[str, Any]:
            published.append(kw)
            return {}

    shared_aws.set_client("sns", Sns())
    use(dms=FakeDms(dms_task("ready")))
    monkeypatch.setenv("TOPIC_ARN", "arn:aws:sns:eu-west-2:1:t")
    monkeypatch.setenv("ENVIRONMENT", "test")
    table.put_item(
        Item={
            "pk": f"LOCK#{TASK_ARN}",
            "sk": "LOCK",
            "runId": "r1",
            "executionArn": EXEC_ARN,
            "project": "ledger",
            "taskKey": "core",
            "gsi2pk": "LOCK#ACTIVE",
            "gsi2sk": EXEC_ARN,
        }
    )
    handler({"detail": {"executionArn": EXEC_ARN, "status": "ABORTED", "name": "r1"}})
    attrs = published[0]["MessageAttributes"]
    assert attrs["severity"]["StringValue"] == "high" and attrs["status"]["StringValue"] == "ABORTED"
    assert attrs["project"]["StringValue"] == "ledger" and attrs["environment"]["StringValue"] == "test"


# --- dump before reset ----------------------------------------------


def test_resolve_context_adds_the_dump_job_when_asked_or_by_default(env):
    from lambdas.resolve_context.handler import handler

    out = handler({"request": {"project": "ledger", "taskKey": "core"}, "execution": EXECUTION})
    assert out["ctx"]["jobs"]["dump"] == []
    req = {"project": "ledger", "taskKey": "core", "dump": {"enabled": True}}
    (dump,) = handler({"request": req, "execution": {**EXECUTION, "name": "r-d1"}})["ctx"]["jobs"]["dump"]
    assert dump["command"][:3] == ["pg-dump-s3", "--connection", "target"] and dump["runtime"] == "fargate"
    reg = {**REGISTRATION, "defaults": {**REGISTRATION["defaults"], "dumpBeforeReset": True}}
    env["ssm"].put_parameter(Name="/dms-orch/test/ledger/core", Type="String", Value=json.dumps(reg), Overwrite=True)
    out = handler({"request": {"project": "ledger", "taskKey": "core"}, "execution": {**EXECUTION, "name": "r-d2"}})
    assert len(out["ctx"]["jobs"]["dump"]) == 1
    req = {"project": "ledger", "taskKey": "core", "dump": {"enabled": False}}
    assert handler({"request": req, "execution": {**EXECUTION, "name": "r-d3"}})["ctx"]["jobs"]["dump"] == []


def test_resolve_context_refuses_the_snapshot_method_unless_the_task_allows_it(env):
    from lambdas.resolve_context.handler import handler
    from shared.errors import InvalidRunRequest

    # The project job role gets the snapshot rights only for tasks with snapshot_dumps.
    req = {"project": "ledger", "taskKey": "core", "dump": {"enabled": True, "method": "snapshot"}}
    with pytest.raises(InvalidRunRequest, match="snapshot_dumps"):
        handler({"request": req, "execution": EXECUTION})


def test_resolve_context_snapshot_dump_job_and_retention_post_job(env):
    from lambdas.resolve_context.handler import handler

    reg = {
        **REGISTRATION,
        "defaults": {**REGISTRATION["defaults"], "snapshotDumps": True, "dumpRetentionDays": 7},
    }
    env["ssm"].put_parameter(Name="/dms-orch/test/ledger/core", Type="String", Value=json.dumps(reg), Overwrite=True)
    req = {"project": "ledger", "taskKey": "core", "dump": {"enabled": True, "method": "snapshot"}}
    ctx = handler({"request": req, "execution": EXECUTION})["ctx"]
    (dump,) = ctx["jobs"]["dump"]
    assert dump["runtime"] == "lambda" and dump["handler"] == "builtin:dump/cluster_snapshot"
    assert dump["with"]["connection"] == "target" and dump["idempotent"] is True
    # Polling up to 30 min: one Lambda waits at most waitSeconds, the retries pick the snapshot up again.
    assert dump["with"]["waitSeconds"] * dump["retry"]["maxAttempts"] >= 1800
    assert dump["timeoutSeconds"] > dump["with"]["waitSeconds"] and dump["timeoutSeconds"] <= 870
    retention = ctx["jobs"]["post"][-1]
    assert retention["handler"] == "builtin:cleanup/cluster_snapshots"
    assert retention["with"] == {"connection": "target", "retentionDays": 7}
    assert retention["onError"] == "warn" and retention["index"] == len(ctx["jobs"]["post"]) - 1
    # pg-dump stays the default method and adds no post job.
    plain = handler({"request": {**req, "dump": {"enabled": True}}, "execution": {**EXECUTION, "name": "r-pg"}})
    assert plain["ctx"]["jobs"]["dump"][0]["command"][0] == "pg-dump-s3"
    assert not [j for j in plain["ctx"]["jobs"]["post"] if j.get("handler") == "builtin:cleanup/cluster_snapshots"]


# --- task configuration checks before the start ----------------------


class EndpointDms(FakeDms):
    def __init__(self, task: dict[str, Any], endpoints: list[dict[str, Any]]):
        super().__init__(task)
        self.endpoints = endpoints

    def describe_endpoints(self, **kw: Any) -> dict[str, Any]:
        arns = kw["Filters"][0]["Values"]
        return {"Endpoints": [e for e in self.endpoints if e["EndpointArn"] in arns]}


def _task_with(settings: dict[str, Any] | None = None, mapping: dict[str, Any] | None = None, mtype: str = "full-load"):
    task = dms_task("ready", migration_type=mtype)
    task["ReplicationTaskSettings"] = json.dumps(
        settings or {"FullLoadSettings": {"TargetTablePrepMode": "DO_NOTHING"}}
    )
    if mapping is not None:
        task["TableMappings"] = json.dumps(mapping)
    task["TargetEndpointArn"] = "arn:tgt"
    return task


PG_TARGET_OK = {
    "EndpointArn": "arn:tgt",
    "EndpointType": "TARGET",
    "EngineName": "aurora-postgresql",
    "PostgreSQLSettings": {"MaxFileSize": 32768},
}


def test_config_checks_pass_for_a_task_like_the_upstream_module_makes(table):
    from lambdas.inspect_task.handler import handler

    use(dms=EndpointDms(_task_with(), [PG_TARGET_OK]))
    out = handler({"runId": "r1", "request": _req(), "ctx": _ctx(), "attempt": 1})
    assert out["configProblems"] == []


@pytest.mark.parametrize(
    "case,task,endpoints,needle",
    [
        ("no include rule", _task_with(mapping={"rules": []}), [PG_TARGET_OK], "selection rule"),
        (
            "prep mode",
            _task_with(settings={"FullLoadSettings": {"TargetTablePrepMode": "DROP_AND_CREATE"}}),
            [PG_TARGET_OK],
            "TargetTablePrepMode",
        ),
        (
            "cdc task that never stops",
            _task_with(settings={"FullLoadSettings": {"TargetTablePrepMode": "DO_NOTHING"}}, mtype="full-load-and-cdc"),
            [PG_TARGET_OK],
            "StopTaskCachedChanges",
        ),
        (
            "MaxFileSize 0",
            _task_with(),
            [{**PG_TARGET_OK, "PostgreSQLSettings": {"MaxFileSize": 0}}],
            "MaxFileSize",
        ),
    ],
)
def test_config_problems_fail_the_run_as_config_before_dms_starts(table, case, task, endpoints, needle):
    from lambdas.inspect_task.handler import handler
    from shared.errors import TaskConfigInvalid

    use(dms=EndpointDms(task, endpoints))
    with pytest.raises(TaskConfigInvalid) as err:
        handler({"runId": "r1", "request": _req(completion="task-stopped"), "ctx": _ctx(), "attempt": 1})
    assert needle in str(err.value), case


def test_config_problems_are_only_reported_by_plan(table):
    from lambdas.inspect_task.handler import handler

    use(dms=EndpointDms(_task_with(), [{**PG_TARGET_OK, "PostgreSQLSettings": {"MaxFileSize": 0}}]))
    out = handler({"runId": "r1", "request": _req(operation="plan"), "ctx": _ctx(), "attempt": 1})
    assert any("MaxFileSize" in p for p in out["configProblems"])


def test_classify_config_invalid_is_config_and_not_retried():
    out = _classify({"Error": "TaskConfigInvalid", "Cause": "MaxFileSize = 0"}, started=False)
    assert (out["category"], out["retryable"], out["autoResume"]) == ("CONFIG", False, False)


# --- premigration assessment settings ------------------------------

ASSESSMENT = {
    "roleArn": "arn:aws:iam::1:role/a",
    "bucket": "res",
    "folder": "pa/core",
    "kmsKeyArn": None,
    "enabled": False,
    "failOnWarning": True,
}


def test_resolve_context_carries_the_assessment_settings_and_the_request_override(env):
    from lambdas.resolve_context.handler import handler
    from shared.errors import InvalidRunRequest

    out = handler({"request": {"project": "ledger", "taskKey": "core"}, "execution": EXECUTION})
    assert out["ctx"]["assessment"] == {"enabled": False}
    with pytest.raises(InvalidRunRequest):
        req = {"project": "ledger", "taskKey": "core", "assessment": {"enabled": True}}
        handler({"request": req, "execution": {**EXECUTION, "name": "r-a0"}})

    reg = {**REGISTRATION, "assessment": ASSESSMENT}
    env["ssm"].put_parameter(Name="/dms-orch/test/ledger/core", Type="String", Value=json.dumps(reg), Overwrite=True)
    req = {"project": "ledger", "taskKey": "core", "assessment": {"enabled": True, "failOnWarning": False}}
    a = handler({"request": req, "execution": {**EXECUTION, "name": "r-a1"}})["ctx"]["assessment"]
    assert a["enabled"] is True and a["failOnWarning"] is False and a["bucket"] == "res" and a["folder"] == "pa/core"


def test_classify_assessment_failures():
    for error in ("AssessmentFailed", "AssessmentWarning", "AssessmentTimeout"):
        out = _classify({"Error": error, "Cause": "x"}, started=False)
        assert (out["category"], out["retryable"]) == ("ASSESSMENT", False)


# --- operator repeats post (--from-job) -------------------------------

REPEAT_EXEC = {
    "arn": "arn:aws:states:eu-west-2:111122223333:execution:dms-orch-test-post:run-1-post-r1",
    "name": "run-1-post-r1",
    "startTime": "2026-10-28T12:00:00Z",
}
POST_JOBS = [{"name": "indexes", "index": 0}, {"name": "row-counts", "index": 1}, {"name": "cleanup", "index": 2}]


def _save_repeat_context(env) -> None:
    saved = {
        **SAVED_CONTEXT,
        "ctx": {**SAVED_CONTEXT["ctx"], "taskKey": "core", "jobs": {"post": POST_JOBS}},
        "inspect": {"state": "fresh", "completionSpec": {"mode": "task-stopped"}},
        "startedAt": "2026-10-27T10:00:00Z",  # before FINISHED stopped
    }
    _pin_context(env, json.dumps(saved).encode())


def _job(table, name: str, index: int, status: str) -> None:
    table.put_item(
        Item={"pk": "RUN#run-1", "sk": f"JOB#post#{index:02d}#{name}", "phase": "post", "name": name, "status": status}
    )


def _repeat(handler, from_job: str | None = None):
    data = {"runId": "run-1", "operation": "post", "startedBy": "operator"}
    if from_job:
        data["fromJob"] = from_job
    return handler({"mode": "post", "input": data, "execution": REPEAT_EXEC})


def test_post_repeat_reads_the_dms_outcome_and_skips_jobs_already_ok(env, store, table):
    from lambdas.resolve_context.handler import handler

    _save_repeat_context(env)
    _job(table, "indexes", 0, "ok")
    _job(table, "row-counts", 1, "fail")
    use(dms=FakeDms(FINISHED), sfn=FakeSfn())
    out = _repeat(handler, from_job="row-counts")
    assert [j["name"] for j in out["ctx"]["jobs"]["post"]] == ["row-counts", "cleanup"]
    assert out["dmsOutcome"]["outcome"] == "COMPLETED" and out["failure"] is None
    assert "preflight" in out  # null for a context saved by an older version
    lock = store.get_lock(TASK_ARN)
    assert lock["runId"] == "run-1" and lock["executionArn"] == REPEAT_EXEC["arn"]


def test_post_repeat_runs_again_every_earlier_job_that_is_not_ok(env, store, table):
    from lambdas.resolve_context.handler import handler

    _save_repeat_context(env)
    _job(table, "indexes", 0, "fail")
    use(dms=FakeDms(FINISHED), sfn=FakeSfn())
    out = _repeat(handler, from_job="cleanup")
    # indexes failed and row-counts never ran: both run again; cleanup is where the operator starts
    assert [j["name"] for j in out["ctx"]["jobs"]["post"]] == ["indexes", "row-counts", "cleanup"]
    assert out["skippedJobs"] == []


def test_post_repeat_is_refused_while_another_run_or_the_first_post_holds_the_lock(env, store, table):
    from lambdas.resolve_context.handler import handler
    from shared.errors import InvalidRunRequest, RunAlreadyActive

    _save_repeat_context(env)
    use(dms=FakeDms(FINISHED), sfn=FakeSfn())
    table.put_item(Item={"pk": f"LOCK#{TASK_ARN}", "sk": "LOCK", "runId": "run-2", "executionArn": "arn:x"})
    with pytest.raises(RunAlreadyActive):
        _repeat(handler)
    table.put_item(Item={"pk": f"LOCK#{TASK_ARN}", "sk": "LOCK", "runId": "run-1", "executionArn": "arn:first-post"})
    with pytest.raises(RunAlreadyActive):  # FakeSfn: the first post is still RUNNING
        _repeat(handler)
    table.delete_item(Key={"pk": f"LOCK#{TASK_ARN}", "sk": "LOCK"})
    with pytest.raises(InvalidRunRequest):
        _repeat(handler, from_job="nope")


def test_post_repeat_with_a_running_dms_task_is_refused(env, table):
    from lambdas.resolve_context.handler import handler
    from shared.errors import InvalidRunRequest

    _save_repeat_context(env)
    use(dms=FakeDms(dms_task("running")), sfn=FakeSfn())
    with pytest.raises(InvalidRunRequest):
        _repeat(handler)


def test_post_repeat_can_take_the_post_jobs_of_a_fixed_job_set_version(env, store, table):
    from lambdas.resolve_context.handler import handler

    _save_repeat_context(env)
    fixed = {
        **JOBSET,
        "phases": {**JOBSET["phases"], "post": [{"name": "row-counts", "command": ["psql-run", "--sql", "SELECT 1"]}]},
    }
    v2 = put_bundle(env["s3"], "v2", {"jobset.json": json.dumps(fixed).encode()})
    register_jobs(env, bundles={**REGISTRATION["jobs"]["bundles"], "v2": v2})
    use(dms=FakeDms(FINISHED), sfn=FakeSfn())
    data = {
        "runId": "run-1",
        "operation": "post",
        "fromJob": "row-counts",
        "jobsVersion": "v2",
        "startedBy": "operator",
    }
    out = handler({"mode": "post", "input": data, "execution": REPEAT_EXEC})
    (job,) = out["ctx"]["jobs"]["post"]
    assert job["command"] == ["psql-run", "--sql", "SELECT 1"]
    assert out["ctx"]["jobsVersion"] == "v2" and out["ctx"]["jobsS3Prefix"] == f"s3://{BUCKET}/jobs/ledger/v2/"


# --- the report advises a snapshot dump for a big target -------------------


def _verify_job(store, run_id: str, size: int) -> None:
    store.table.put_item(
        Item={
            "pk": f"RUN#{run_id}",
            "sk": "JOB#preflight#00#target-verify",
            "phase": "preflight",
            "name": "target-verify",
            "status": "ok",
            "summary": json.dumps({"databaseBytes": size, "tables": 4}),
        }
    )


@pytest.mark.parametrize(
    ("dump", "size", "advised"),
    [
        ({"enabled": True}, 300 * 2**30, True),
        ({"enabled": True, "snapshotAboveBytes": 2**40}, 300 * 2**30, False),
        ({"enabled": True}, 2**30, False),
        ({"enabled": True, "method": "snapshot"}, 300 * 2**30, False),
        ({"enabled": False}, 300 * 2**30, False),
    ],
)
def test_write_report_advises_the_snapshot_method_above_the_threshold(env, store, dump, size, advised):
    from lambdas.write_report.handler import handler

    store.put_run("run-adv", {"project": "ledger", "status": "RUNNING", "startedAt": "2026-10-29T10:00:00Z"})
    _verify_job(store, "run-adv", size)
    use(dms=FakeDms(FINISHED, []))
    handler(
        {
            "runId": "run-adv",
            "status": "SUCCEEDED",
            "request": {"project": "ledger", "taskKey": "core", "dryRun": False, "dump": dump},
            "ctx": {"taskArn": TASK_ARN, "environment": "test"},
            "inspect": {"state": "fresh", "decision": {"action": "start"}},
            "started": False,
            "attempt": 1,
        }
    )
    report = json.loads(env["s3"].get_object(Bucket=BUCKET, Key="runs/ledger/run-adv/report.json")["Body"].read())
    md = env["s3"].get_object(Bucket=BUCKET, Key="runs/ledger/run-adv/report.md")["Body"].read().decode()
    found = [a for a in report["advice"] if "dump.method=snapshot" in a]
    assert bool(found) is advised
    assert ("dump.method=snapshot" in md) is advised


# --- preflight.verifyRuntime = lambda | fargate | auto -------------------

DATA_API_TARGET = {
    "engine": "postgresql",
    "secretArn": "arn:aws:secretsmanager:eu-west-2:111122223333:secret:tgt",
    "dataApiClusterArn": "arn:aws:rds:eu-west-2:111122223333:cluster:target",
    "host": "tgt.cluster.local",
    "database": "ledger",
}


def _register(env, target: dict[str, Any]) -> None:
    reg = {**REGISTRATION, "connections": {**REGISTRATION["connections"], "target": target}}
    env["ssm"].put_parameter(Name="/dms-orch/test/ledger/core", Type="String", Value=json.dumps(reg), Overwrite=True)


def _verify_jobs(name: str, preflight: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    from lambdas.resolve_context.handler import handler

    req = {"project": "ledger", "taskKey": "core", **({"preflight": preflight} if preflight else {})}
    return handler({"request": req, "execution": {**EXECUTION, "name": name}})["ctx"]["jobs"]["preflight"]


def test_verify_runtime_lambda_by_default_with_the_data_api(env, monkeypatch):
    monkeypatch.setenv("FARGATE_IMAGES", json.dumps(["sql-runner"]))
    _register(env, DATA_API_TARGET)
    (job,) = _verify_jobs("r-v1")
    assert job["runtime"] == "lambda" and job["handler"] == "builtin:checks/target_verify"


def test_verify_runtime_fargate_when_asked(env, monkeypatch):
    monkeypatch.setenv("FARGATE_IMAGES", json.dumps(["sql-runner"]))
    _register(env, DATA_API_TARGET)
    (job,) = _verify_jobs("r-v2", {"verifyRuntime": "fargate"})
    assert job["runtime"] == "fargate" and job["image"] == "sql-runner" and job["command"] == ["verify-target"]
    assert job["with"]["connection"] == "target" and job["with"]["exactCountMaxRows"] == 1_000_000
    assert job["name"] == "target-verify"  # same name: post reads the baseline check by it


def test_verify_runtime_fargate_without_an_image_is_refused(env, monkeypatch):
    from shared.errors import InvalidRunRequest

    monkeypatch.setenv("FARGATE_IMAGES", "[]")
    _register(env, DATA_API_TARGET)
    with pytest.raises(InvalidRunRequest, match="sql-runner"):
        _verify_jobs("r-v3", {"verifyRuntime": "fargate"})


def test_verify_runtime_auto_goes_to_fargate_for_a_big_baseline(env, store, monkeypatch):
    monkeypatch.setenv("FARGATE_IMAGES", json.dumps(["sql-runner"]))
    _register(env, DATA_API_TARGET)
    store.put_target_baseline(
        "ledger/core",
        run_id="r0",
        fingerprint_hash="sha256:x",
        fingerprint_uri="s3://b/k",
        summary={"tables": 900, "rows": 10, "databaseBytes": 10},
    )
    assert _verify_jobs("r-v4", {"verifyRuntime": "auto"})[0]["runtime"] == "fargate"
    assert _verify_jobs("r-v5", {"verifyRuntime": "auto", "fargateAboveTables": 1000})[0]["runtime"] == "lambda"
    store.put_target_baseline(
        "ledger/core",
        run_id="r1",
        fingerprint_hash="sha256:x",
        fingerprint_uri="s3://b/k",
        summary={"tables": 4, "rows": 10, "databaseBytes": 60 * 2**30},
    )
    assert _verify_jobs("r-v6", {"verifyRuntime": "auto"})[0]["runtime"] == "fargate"
    assert _verify_jobs("r-v7", {"verifyRuntime": "auto", "fargateAboveBytes": 2**40})[0]["runtime"] == "lambda"


def test_verify_runtime_with_a_host_runs_in_lambda_through_pg8000(env, monkeypatch):
    """No Data API, but a host: the Lambda reaches the target through pg8000 (no image needed)."""
    target = {k: v for k, v in DATA_API_TARGET.items() if k != "dataApiClusterArn"}
    _register(env, target)
    monkeypatch.setenv("FARGATE_IMAGES", "[]")
    (job,) = _verify_jobs("r-v8")
    assert job["runtime"] == "lambda" and job["handler"] == "builtin:checks/target_verify"
    monkeypatch.setenv("FARGATE_IMAGES", json.dumps(["sql-runner"]))
    assert _verify_jobs("r-v9")[0]["runtime"] == "lambda"
    assert _verify_jobs("r-v10", {"verifyRuntime": "lambda"})[0]["runtime"] == "lambda"


def test_verify_runtime_without_host_or_data_api_uses_fargate_or_nothing(env, monkeypatch):
    target = {k: v for k, v in DATA_API_TARGET.items() if k not in ("dataApiClusterArn", "host")}
    _register(env, target)
    monkeypatch.setenv("FARGATE_IMAGES", "[]")
    assert _verify_jobs("r-v11") == []  # the DMS state decides alone
    monkeypatch.setenv("FARGATE_IMAGES", json.dumps(["sql-runner"]))
    assert _verify_jobs("r-v12")[0]["runtime"] == "fargate"
    assert _verify_jobs("r-v13", {"verifyRuntime": "lambda"}) == []


def test_run_request_accepts_allow_no_fingerprint():
    from shared import validation

    validation.validate(
        "run_request", {"project": "ledger", "taskKey": "core", "preflight": {"allowNoFingerprint": True}}
    )


def test_write_report_counts_a_reset_list_kept_in_s3(env, store):
    from lambdas.write_report.handler import handler

    store.put_run("run-1", {"project": "ledger", "status": "RUNNING"})
    use(dms=FakeDms(FINISHED))
    decision = {"action": "start", "resetScope": "all", "resetTables": [], "resetTablesCount": 600}
    handler(
        {
            "runId": "run-1",
            "status": "SUCCEEDED",
            "request": {"project": "ledger", "taskKey": "core", "notify": {}},
            "ctx": {"taskArn": TASK_ARN},
            "inspect": {"state": "completed", "decision": decision},
            "started": True,
            "attempt": 1,
        }
    )
    md = env["s3"].get_object(Bucket=BUCKET, Key="runs/ledger/run-1/report.md")["Body"].read().decode()
    assert "- Target reset: all (600 tables)" in md


# --- review finding 7: failOnValidationErrors (registration default, request override) ---

BAD_VALIDATION = [
    {
        "SchemaName": "app",
        "TableName": "accounts",
        "TableState": "Table completed",
        "ValidationState": "Mismatched records",
        "ValidationFailedRecords": 2,
    }
]


def test_resolve_context_fail_on_validation_errors_defaults_on_registration_and_request(env, store):
    from lambdas.resolve_context.handler import handler

    out = handler({"request": {"project": "ledger", "taskKey": "core"}, "execution": EXECUTION})
    assert out["request"]["failOnValidationErrors"] is True
    reg = {**REGISTRATION, "defaults": {**REGISTRATION["defaults"], "failOnValidationErrors": False}}
    env["ssm"].put_parameter(Name="/dms-orch/test/ledger/core", Type="String", Value=json.dumps(reg), Overwrite=True)
    out = handler({"request": {"project": "ledger", "taskKey": "core"}, "execution": {**EXECUTION, "name": "v1"}})
    assert out["request"]["failOnValidationErrors"] is False
    req = {"project": "ledger", "taskKey": "core", "failOnValidationErrors": True}
    out = handler({"request": req, "execution": {**EXECUTION, "name": "v2"}})
    assert out["request"]["failOnValidationErrors"] is True


@pytest.mark.parametrize("flag,expected", [(None, True), (False, False), (True, True)])
def test_inspect_puts_fail_on_validation_errors_into_the_completion_spec(table, flag, expected):
    from lambdas.inspect_task.handler import handler

    use(dms=FakeDms(dms_task("ready")))
    req = _req() if flag is None else _req(failOnValidationErrors=flag)
    out = handler({"runId": "r1", "request": req, "ctx": _ctx(), "attempt": 1})
    assert out["completionSpec"]["failOnValidationErrors"] is expected


def test_classify_validation_errors_are_data_and_not_recovered_after_a_wait_timeout():
    from lambdas.classify_failure.handler import classify

    assert _classify({"Error": "DmsValidationErrors", "Cause": "1 table(s) not validated"})["category"] == "DATA"
    event = {
        "failure": {"Error": "WaitTimeout", "Cause": "heartbeat"},
        "ctx": {"taskArn": TASK_ARN},
        "request": {},
        "started": True,
        "attempt": 1,
        "completion": {"mode": "task-stopped", "failOnValidationErrors": True},
        "startedAt": "2026-10-27T10:00:00Z",
    }
    out = classify(event, FakeDms(FINISHED, BAD_VALIDATION), FakeLogs())
    assert out["recovered"] is False


# --- review finding 9: job sets pinned by the bundle hash in the registration ---


def register_jobs(env, **jobs: Any) -> None:
    reg = {**REGISTRATION, "jobs": {**REGISTRATION["jobs"], **jobs}}
    env["ssm"].put_parameter(Name="/dms-orch/test/ledger/core", Type="String", Value=json.dumps(reg), Overwrite=True)


def _resolve(version: str = "v1", name: str = "r-pin") -> dict[str, Any]:
    from lambdas.resolve_context.handler import handler

    req = {"project": "ledger", "taskKey": "core", "jobsVersion": version}
    return handler({"request": req, "execution": {**EXECUTION, "name": name}})


def test_pinned_job_set_puts_its_hashes_into_ctx_and_every_job(env):
    ctx = _resolve()["ctx"]
    pinned = REGISTRATION["jobs"]["bundles"]["v1"]
    assert ctx["jobsBundle"] == {
        "sha256": pinned,
        "files": bundle_files({"jobset.json": json.dumps(JOBSET).encode()}),
        "pinned": True,
    }
    assert {j["bundleSha256"] for p in ("pre", "reset") for j in ctx["jobs"][p]} == {pinned}
    assert "bundle" not in ctx["jobs"]


def test_unregistered_job_set_version_is_refused(env):
    from shared.errors import JobSetNotPinned

    put_bundle(env["s3"], "v3", {"jobset.json": json.dumps(JOBSET).encode()})
    with pytest.raises(JobSetNotPinned, match="v3"):
        _resolve("v3")


def test_allow_unpinned_takes_the_bundle_hash_it_finds(env):
    total = put_bundle(env["s3"], "v3", {"jobset.json": json.dumps(JOBSET).encode()})
    register_jobs(env, allowUnpinned=True)
    ctx = _resolve("v3")["ctx"]
    assert ctx["jobsBundle"]["sha256"] == total and ctx["jobsBundle"]["pinned"] is False
    assert ctx["jobs"]["pre"][0]["bundleSha256"] == total


def test_allow_unpinned_without_a_manifest_runs_unverified(env):
    env["s3"].put_object(Bucket=BUCKET, Key="jobs/ledger/v4/jobset.json", Body=json.dumps(JOBSET).encode())
    register_jobs(env, allowUnpinned=True)
    ctx = _resolve("v4")["ctx"]
    assert ctx["jobsBundle"] is None and ctx["jobs"]["pre"][0]["bundleSha256"] is None


def test_tampered_job_set_file_is_refused(env):
    from shared.errors import JobSetTampered

    evil = {**JOBSET, "phases": {**JOBSET["phases"], "pre": [{"name": "x", "command": ["sh", "-c", "id"]}]}}
    env["s3"].put_object(Bucket=BUCKET, Key="jobs/ledger/v1/jobset.json", Body=json.dumps(evil).encode())
    with pytest.raises(JobSetTampered, match="jobset.json"):
        _resolve()


def test_bundle_rewritten_with_a_fresh_manifest_does_not_match_the_pin(env):
    from shared.errors import JobSetTampered

    evil = {**JOBSET, "phases": {**JOBSET["phases"], "pre": [{"name": "x", "command": ["sh", "-c", "id"]}]}}
    put_bundle(env["s3"], "v1", {"jobset.json": json.dumps(evil).encode()})
    with pytest.raises(JobSetTampered, match="pinned"):
        _resolve()
    register_jobs(env, allowUnpinned=True)  # allow_unpinned never overrides a registered hash
    with pytest.raises(JobSetTampered, match="pinned"):
        _resolve(name="r-pin2")


def test_pinned_version_without_a_manifest_is_refused(env):
    from shared.errors import JobSetTampered

    env["s3"].delete_object(Bucket=BUCKET, Key="jobs/ledger/v1/bundle.sha256")
    with pytest.raises(JobSetTampered, match="bundle.sha256"):
        _resolve()


def test_post_repeat_with_an_unpinned_job_set_version_is_refused(env, table):
    from lambdas.resolve_context.handler import handler
    from shared.errors import JobSetNotPinned

    _save_repeat_context(env)
    put_bundle(env["s3"], "v5", {"jobset.json": json.dumps(JOBSET).encode()})
    use(dms=FakeDms(FINISHED), sfn=FakeSfn())
    data = {"runId": "run-1", "operation": "post", "jobsVersion": "v5", "startedBy": "operator"}
    with pytest.raises(JobSetNotPinned):
        handler({"mode": "post", "input": data, "execution": REPEAT_EXEC})


def test_job_set_errors_are_configuration_failures():
    for error in ("JobSetNotPinned", "JobSetTampered"):
        assert _classify({"Error": error, "Cause": "x"})["category"] == "CONFIG"

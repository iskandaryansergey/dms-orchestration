"""State machine definitions: static structure and path scenarios (local JSONata runner).

Two machines (ADR 0005): migrate ends after the DMS start, post is started by the DMS event.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from asl_runner import Interpreter, TaskError, compile_expr, iter_expressions, render, template_variables

MIGRATE = "migrate.asl.json"
POST = "post.asl.json"
JOB_RUNNER = "job-runner.asl.json"
ALL = [MIGRATE, POST, JOB_RUNNER]
TASK_ARN = "arn:aws:dms:eu-west-2:111122223333:task:ABCDEF"
TERMINAL = ("Succeed", "Fail")


# --- static checks ----------------------------------------------------------------


def _edges(state: dict[str, Any]) -> list[str]:
    out = [state[k] for k in ("Next", "Default") if k in state]
    out += [c["Next"] for c in state.get("Choices", [])]
    out += [c["Next"] for c in state.get("Catch", [])]
    return out


def _reachable(states: dict[str, Any], start: str, blocked: frozenset[str] = frozenset()) -> set[str]:
    seen: set[str] = set()
    todo = [start]
    while todo:
        name = todo.pop()
        if name in seen or name in blocked:
            continue
        seen.add(name)
        todo.extend(_edges(states[name]))
    return seen


def _success_edges(state: dict[str, Any]) -> list[str]:
    """Edges taken when the state itself succeeds (no Catch)."""
    out = [state[k] for k in ("Next", "Default") if k in state]
    return out + [c["Next"] for c in state.get("Choices", [])]


def _all_machines(defn: dict[str, Any]):
    yield defn
    for state in defn["States"].values():
        if state["Type"] == "Map":
            yield from _all_machines(state["ItemProcessor"])


@pytest.mark.parametrize("name", ALL)
def test_definition_renders_and_graph_is_closed(name):
    defn = render(name)
    assert defn["QueryLanguage"] == "JSONata"
    for machine in _all_machines(defn):
        states = machine["States"]
        for state_name, state in states.items():
            for target in _edges(state):
                assert target in states, f"{state_name} -> unknown state {target}"
            if state["Type"] not in TERMINAL and state["Type"] != "Choice" and not state.get("End"):
                assert "Next" in state, f"{state_name} has no Next"
        unreachable = set(states) - _reachable(states, machine["StartAt"])
        assert not unreachable, f"unreachable states: {unreachable}"


@pytest.mark.parametrize("name", ALL)
def test_every_jsonata_expression_compiles(name):
    defn = render(name)
    exprs = list(iter_expressions(defn))
    assert len(exprs) > 20
    for path, text in exprs:
        try:
            compile_expr(text)
        except Exception as err:  # noqa: BLE001
            pytest.fail(f"{path}: {text!r} does not compile: {err}")


@pytest.mark.parametrize("name", ALL)
def test_template_only_uses_known_placeholders(name):
    known = {
        "state_machine_timeout_seconds",
        "resolve_context_arn",
        "inspect_task_arn",
        "classify_failure_arn",
        "write_report_arn",
        "prepare_job_arn",
        "job_runner_arn",
        "post_arn",
        "migrate_arn",
        "migrate_executions_prefix",
        "table_name",
        "topic_arn",
        "ecs_cluster_arn",
        "fargate_platform_version",
        "subnets_json",
        "security_groups_json",
        "assign_public_ip",
        "artifacts_bucket",
    }
    assert template_variables(name) <= known


def test_no_machine_waits_for_dms():
    for name in ALL:
        for machine in _all_machines(render(name)):
            for state_name, state in machine["States"].items():
                resource = state.get("Resource", "")
                assert not resource.endswith(".waitForTaskToken"), f"{name}:{state_name} waits for a token"
                assert "HeartbeatSeconds" not in state, f"{name}:{state_name}"
                assert "describeReplicationTasks" not in resource, f"{name}:{state_name} polls DMS"
                # Only the job-runner retry backoff and the assessment poll (minutes) wait; never DMS.
                if state["Type"] == "Wait":
                    assert (name, state_name) in {(JOB_RUNNER, "WaitBeforeRetry"), (MIGRATE, "WaitAssessment")}


def test_migrate_releases_the_lock_on_every_path_that_ends_before_the_dms_start():
    states = render(MIGRATE)["States"]
    after_lock = states["AcquireLock"]["Next"]
    # Started is the only terminal allowed without ReleaseLock: DMS (or the dry-run post) holds it.
    without_release = _reachable(states, after_lock, blocked=frozenset({"ReleaseLock"}))
    terminals = {n for n in without_release if states[n]["Type"] in TERMINAL}
    assert terminals == {"Started"}, terminals
    # ...and the lock-contention path never releases someone else's lock
    assert "ReleaseLock" not in _reachable(states, "MarkRunEndedWithoutLock")


def test_migrate_after_arm_releases_the_lock_only_when_the_start_is_refused():
    states = render(MIGRATE)["States"]
    # A successful start (or a start DMS may have taken) never reaches ReleaseLock.
    for start in ("ReportStarted", "RecordStartError"):
        assert "ReleaseLock" not in _reachable(states, start), start
    assert _success_edges(states["StartReplicationTask"]) == ["ReportStarted"]
    catch = {tuple(c["ErrorEquals"]): c["Next"] for c in states["StartReplicationTask"]["Catch"]}
    assert catch[("DatabaseMigration.InvalidResourceStateException",)] == "RecordStartError"
    assert catch[("States.ALL",)] == "Disarm" and "ReleaseLock" in _reachable(states, "Disarm")


def test_start_replication_task_is_only_reachable_through_arm():
    states = render(MIGRATE)["States"]
    preds = [n for n, s in states.items() if "StartReplicationTask" in _edges(s)]
    assert preds == ["CheckWaitOnly"]
    assert [n for n, s in states.items() if "CheckWaitOnly" in _edges(s)] == ["Arm"]
    assert [n for n, s in states.items() if "Arm" in _edges(s)] == ["CheckDryRun"]
    assert states["Arm"]["Arguments"]["ConditionExpression"] == "attribute_not_exists(pk) OR #s = :posted"


DESTRUCTIVE = ("DumpJobs", "ResetJobs")


def test_nothing_touches_the_target_before_the_assessment_and_the_arm_guard_passed():
    """Seen live: the reset truncated the target before Arm and the assessment, so a refusal there left
    it empty. Every path to the dump or the reset now goes through the assessment choice and the
    read-only Arm guard; the guard is entered only when the assessment passed (or is off)."""
    states = render(MIGRATE)["States"]
    start = render(MIGRATE)["StartAt"]
    for gate in ("CheckAssessment", "ReadMigration", "ArmGuard"):
        reachable = _reachable(states, start, blocked=frozenset({gate}))
        assert not reachable & set(DESTRUCTIVE), (gate, reachable & set(DESTRUCTIVE))
    assert sorted(n for n, s in states.items() if "ReadMigration" in _edges(s)) == [
        "CheckAssessment",
        "CheckAssessmentStatus",
    ]
    into_guard = [c["Condition"] for c in states["CheckAssessmentStatus"]["Choices"] if c["Next"] == "ReadMigration"]
    assert len(into_guard) == 2 and all("'passed'" in c or "'warning'" in c for c in into_guard)
    assert states["CheckAssessmentStatus"]["Default"] == "WaitAssessment"
    # A refusing check never leads on to the dump or the reset.
    for refusing in ("ClassifyFailure", "WriteReport"):
        assert not _reachable(states, refusing) & set(DESTRUCTIVE), refusing
    assert states["ReadMigration"]["Resource"].endswith("dynamodb:getItem")
    assert states["ReadMigration"]["Arguments"]["ConsistentRead"] is True
    assert states["ArmGuard"]["Default"] == "CheckReset"


def test_every_terminal_path_of_post_passes_release_lock():
    states = render(POST)["States"]
    after_load = states["LoadRun"]["Next"]
    without_release = _reachable(states, after_load, blocked=frozenset({"ReleaseLock"}))
    terminals = {n for n in without_release if states[n]["Type"] in TERMINAL}
    assert terminals == set(), f"terminal states reachable without ReleaseLock: {terminals}"


def test_post_resumes_only_through_a_lock_hand_over():
    states = render(POST)["States"]
    assert [n for n, s in states.items() if "StartResume" in _edges(s)] == ["HandOverLock"]
    assert states["HandOverLock"]["Arguments"]["ConditionExpression"] == "runId = :o"
    assert states["StartResume"]["Resource"] == "arn:aws:states:::states:startExecution"  # no .sync
    assert "GiveLockBack" in _edges(states["StartResume"])


def test_lambda_tasks_retry_transient_lambda_errors():
    for name in ALL:
        for state_name, state in render(name)["States"].items():
            if state.get("Resource", "").startswith("arn:aws:states:::lambda:invoke"):
                errors = {e for r in state.get("Retry", []) for e in r["ErrorEquals"]}
                assert "Lambda.TooManyRequestsException" in errors, state_name
                assert "Catch" in state, state_name


def test_start_does_not_retry_invalid_state():
    start = render(MIGRATE)["States"]["StartReplicationTask"]
    assert start["Retry"][0] == {"ErrorEquals": ["DatabaseMigration.InvalidResourceStateException"], "MaxAttempts": 0}


DDB_RESERVED = {
    "status",
    "name",
    "data",
    "value",
    "timestamp",
    "ttl",
    "source",
    "result",
    "comment",
    "count",
    "size",
    "consumed",
    "phase",
    "summary",
    "message",
    "attempt",
    "runtime",
}


def test_dynamodb_expressions_alias_reserved_words():
    import re

    for name in ALL:
        for state_name, state in render(name)["States"].items():
            args = state.get("Arguments", {})
            for key in ("UpdateExpression", "ConditionExpression"):
                if key in args:
                    words = set(re.findall(r"(?<![#:])\b([A-Za-z_][A-Za-z0-9_]*)\b", args[key]))
                    words -= {"SET", "REMOVE", "AND", "OR", "NOT", "attribute_not_exists", "attribute_exists"}
                    assert not (words & DDB_RESERVED), f"{state_name}.{key} uses reserved {words & DDB_RESERVED}"


# --- shared fixtures for scenarios -------------------------------------------------

JOB_PRE = [{"name": "check-permissions", "index": 0}, {"name": "create-tables", "index": 1}]
JOB_POST = [{"name": "indexes", "index": 0}]


def ctx(**jobs: list[dict[str, Any]]) -> dict[str, Any]:
    all_jobs = {"pre": JOB_PRE, "reset": [{"name": "reset-target", "index": 0}], "post": JOB_POST, "on_failure": []}
    all_jobs.update(jobs)
    return {
        "project": "ledger",
        "taskKey": "core",
        "environment": "test",
        "taskArn": TASK_ARN,
        "connections": {"source": {"engine": "postgresql"}},
        "jobsS3Prefix": "s3://b/jobs/ledger/v1/",
        "jobsVersion": "v1",
        "jobs": all_jobs,
        "runTimeoutSeconds": 604800,
    }


def request(**kw: Any) -> dict[str, Any]:
    return {"project": "ledger", "taskKey": "core", "operation": "migrate", "dryRun": False, **kw}


def decision(action: str = "start", start_type: str | None = "start-replication", reset: bool = False, **kw: Any):
    return {
        "action": action,
        "startType": start_type,
        "reset": reset,
        "resetScope": "partial" if reset else "none",
        "resetTables": ["public.b"] if reset else [],
        "reason": "test",
        "error": None,
        **kw,
    }


def payload(value: Any) -> Callable[[Any], Any]:
    return lambda _args: {"Payload": value}


def sequence(*results: Any) -> Callable[[Any], Any]:
    """Each call returns the next result; exceptions are raised."""
    items = list(results)

    def call(_args: Any) -> Any:
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item

    return call


def raises(error: str, cause: Any = "boom") -> Callable[[Any], Any]:
    def call(_args: Any) -> Any:
        raise TaskError(error, cause)

    return call


def job_ok(args: Any) -> Any:
    job = args["Input"]["job"]
    return {"Output": {"name": job["name"], "phase": args["Input"]["phase"], "status": "ok", "summary": {}}}


def report(a: Any) -> Any:
    return {
        "Payload": {
            "reportS3Uri": "s3://b/runs/run-1/report.json",
            "notify": a["Payload"]["status"] != "STARTED",
            "subject": f"[dms-orch] {a['Payload']['status']}",
            "message": "{}",
        }
    }


# --- migrate scenarios ----------------------------------------------------------------

COMPLETION = {"mode": "task-stopped", "migrationType": "full-load", "failOnTableErrors": True, "expectStop": False}


def verdict(name: str = "ready", dec: dict[str, Any] | None = None, error: str | None = None, **kw: Any):
    return {
        "verdict": name,
        "reason": f"test {name}",
        "error": error,
        "diff": {},
        "decision": dec or decision(),
        "warnings": [],
        "fingerprint": None,
        "baseline": None,
        **kw,
    }


VERIFY_JOB = {"name": "target-verify", "index": 0, "runtime": "lambda", "handler": "builtin:checks/target_verify"}


def verify_ok(args: Any) -> Any:
    return {"Output": {"name": "target-verify", "phase": args["Input"]["phase"], "status": "ok", "summary": {}}}


def migrate_mocks(req: dict[str, Any] | None = None, dec: dict[str, Any] | None = None, **over: Any):
    req = req or request()
    mocks: dict[str, Any] = {
        "ResolveContext": payload({"runId": "run-1", "request": req, "ctx": ctx(), "attempt": req.get("attempt", 1)}),
        "AcquireLock": lambda _a: {},
        "MarkRunEndedWithoutLock": lambda _a: {},
        "InspectTask": payload(
            {
                "state": "fresh",
                "migrationType": "full-load",
                "decision": dec or decision(),
                "completionSpec": COMPLETION,
                "task": {"status": "ready"},
                "selectionRules": [{"schema": "app", "table": "%", "action": "include"}],
            }
        ),
        "RunPreflightJob": verify_ok,
        "Preflight": payload(verdict(dec=dec)),
        "RunPreJob": job_ok,
        "RunResetJob": job_ok,
        "RunOnFailureJob": job_ok,
        "SaveContext": lambda _a: {"VersionId": "ctx-v1", "ETag": '"e"'},
        "RecordContextVersion": lambda _a: {},
        "ReadMigration": lambda _a: {},
        "StartPostDryRun": lambda a: {"ExecutionArn": "arn:post:" + a["Name"]},
        "Arm": lambda _a: {},
        "StartReplicationTask": lambda _a: {"ReplicationTask": {"Status": "starting"}},
        "RecordStartError": lambda _a: {},
        "Disarm": lambda _a: {},
        "ReportStarted": report,
        "NotifyStarted": lambda _a: {},
        "ClassifyFailure": payload(
            {"category": "PERMISSIONS", "retryable": False, "autoResume": False, "reason": "permission denied"}
        ),
        "WriteReport": report,
        "Notify": lambda _a: {},
        "ReleaseLock": lambda _a: {},
    }
    mocks.update(over)
    return mocks


def run_migrate(mocks: dict[str, Any], input_: Any = None):
    return Interpreter(render(MIGRATE), mocks).execute(input_ or {"project": "ledger", "taskKey": "core"})


def test_migrate_records_the_version_of_the_saved_context_before_arming():
    """Review finding 1: post loads only the context.json version migrate recorded on RUN#."""
    run = run_migrate(migrate_mocks())
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    assert run.path.index("SaveContext") < run.path.index("RecordContextVersion") < run.path.index("Arm")
    rec = run.called("RecordContextVersion")[0]
    assert rec["Key"] == {"pk": {"S": "RUN#run-1"}, "sk": {"S": "META"}}
    assert rec["UpdateExpression"] == "SET contextVersionId = :v"
    assert rec["ExpressionAttributeValues"] == {":v": {"S": "ctx-v1"}}
    assert rec["ConditionExpression"] == "attribute_exists(pk)"


def test_migrate_without_a_context_version_stops_before_dms():
    """An unversioned bucket returns no VersionId: post could not verify the context, so DMS is not started."""
    run = run_migrate(migrate_mocks(SaveContext=lambda _a: {"ETag": '"e"'}))
    assert run.called("RecordContextVersion") == [] and run.called("Arm") == []
    assert run.called("StartReplicationTask") == []
    failure = run.called("ClassifyFailure")[0]["Payload"]["failure"]
    assert failure["Error"] == "ContextNotPinned" and "versioning" in failure["Cause"]


def test_migrate_arms_then_starts_then_ends_without_waiting_or_unlocking():
    run = run_migrate(migrate_mocks())
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    order = [n for n in run.path if n in ("PreJobs", "SaveContext", "Arm", "StartReplicationTask", "ReportStarted")]
    assert order == ["PreJobs", "SaveContext", "Arm", "StartReplicationTask", "ReportStarted"]
    assert run.path[-1] == "Started" and run.called("ReleaseLock") == []
    arm = run.called("Arm")[0]["Item"]
    assert arm["pk"] == {"S": f"MIGRATION#{TASK_ARN}"} and arm["status"] == {"S": "armed"}
    assert arm["gsi2pk"] == {"S": "MIGRATION#ACTIVE"} and arm["runId"] == {"S": "run-1"}
    assert arm["completionSpec"]["M"]["failOnTableErrors"] == {"Bool": True}
    assert arm["runTimeoutSeconds"] == {"N": "604800"} and arm["startedAt"]["S"]
    start = run.called("StartReplicationTask")[0]
    assert start == {"ReplicationTaskArn": TASK_ARN, "StartReplicationTaskType": "start-replication"}
    # An object, not $string(...): the S3 SDK integration JSON-encodes a string Body once more,
    # and post read a quoted string back.
    saved = run.called("SaveContext")[0]["Body"]
    assert isinstance(saved, dict)
    assert run.called("SaveContext")[0]["Key"] == "runs/ledger/run-1/context.json"
    assert saved["jobCtx"]["startType"] == "start-replication" and saved["startedAt"] == arm["startedAt"]["S"]
    assert "preflight" in saved  # post's final report carries the verdict (S4)
    assert run.called("ReportStarted")[0]["Payload"]["status"] == "STARTED"
    assert run.called("NotifyStarted") == []  # notify.onStart is off by default
    lock = run.called("AcquireLock")[0]
    assert lock["ConditionExpression"] == "attribute_not_exists(pk) OR runId = :me"
    assert run.output["status"] == "STARTED" and run.output["startType"] == "start-replication"


@pytest.mark.parametrize("left", ["posted", "armed", "claimed"])
def test_migrate_arm_replaces_only_a_migration_item_whose_post_already_started(table, left):
    """Seen live: a post that failed before deleting MIGRATION# left it 'posted', and every later Arm
    was refused (MigrationAlreadyArmed). Arm runs under the lock, and the lock is free only once
    that post no longer runs: a 'posted' item is garbage, 'armed'/'claimed' mean DMS may run."""
    import boto3

    arm = run_migrate(migrate_mocks()).called("Arm")[0]
    ddb = boto3.client("dynamodb", region_name="eu-west-2")
    ddb.put_item(
        TableName=table.name,
        Item={"pk": arm["Item"]["pk"], "sk": {"S": "ACTIVE"}, "status": {"S": left}, "runId": {"S": "old"}},
    )
    # Step Functions' SDK integration spells the boolean type "Bool" (Java SDK), boto3 "BOOL".
    args = json.loads(json.dumps({k: v for k, v in arm.items() if k != "TableName"}).replace('"Bool":', '"BOOL":'))
    if left == "posted":
        ddb.put_item(TableName=table.name, **args)
        got = ddb.get_item(TableName=table.name, Key={"pk": arm["Item"]["pk"], "sk": {"S": "ACTIVE"}})["Item"]
        assert got["runId"] == {"S": "run-1"} and got["status"] == {"S": "armed"}
    else:
        with pytest.raises(ddb.exceptions.ConditionalCheckFailedException):
            ddb.put_item(TableName=table.name, **args)


def test_migrate_start_rejected_disarms_and_unlocks():
    run = run_migrate(migrate_mocks(StartReplicationTask=raises("DatabaseMigration.AccessDeniedException")))
    assert run.status == "FAILED"
    assert [n for n in run.path if n in ("Arm", "Disarm", "ClassifyFailure", "ReleaseLock")] == [
        "Arm",
        "Disarm",
        "ClassifyFailure",
        "ReleaseLock",
    ]
    assert run.called("Disarm")[0]["ExpressionAttributeValues"] == {":r": {"S": "run-1"}}
    classify = run.called("ClassifyFailure")[0]["Payload"]
    assert classify["failure"]["Error"] == "DatabaseMigration.AccessDeniedException" and classify["started"] is False
    assert run.called("ReportStarted") == []


def test_migrate_invalid_state_on_start_is_left_to_the_handoff():
    mocks = migrate_mocks(
        StartReplicationTask=raises("DatabaseMigration.InvalidResourceStateException", "status=ready")
    )
    run = run_migrate(mocks)
    assert run.status == "SUCCEEDED" and run.called("ReleaseLock") == []
    err = run.called("RecordStartError")[0]["ExpressionAttributeValues"][":e"]["M"]
    assert err == {"Error": {"S": "DatabaseMigration.InvalidResourceStateException"}, "Cause": {"S": "status=ready"}}


def test_migrate_refuses_when_another_migration_is_armed():
    run = run_migrate(migrate_mocks(Arm=raises("DynamoDb.ConditionalCheckFailedException")))
    assert (run.status, run.error) == ("FAILED", "MigrationAlreadyArmed")
    assert run.called("StartReplicationTask") == [] and run.called("ReleaseLock")


def migration_item(status: str, run_id: str = "old") -> Callable[[Any], Any]:
    return lambda _a: {"Item": {"pk": {"S": f"MIGRATION#{TASK_ARN}"}, "status": {"S": status}, "runId": {"S": run_id}}}


RESET = decision(start_type="reload-target", reset=True, resetScope="all", resetTables=["app.a"])


def reset_mocks(**over: Any) -> dict[str, Any]:
    mocks = migrate_mocks(dec=RESET, Preflight=payload(verdict("reset", dec=RESET)), RunDumpJob=job_ok, **over)
    mocks["ResolveContext"] = payload(
        {"runId": "run-1", "request": request(), "ctx": ctx(dump=[DUMP_JOB]), "attempt": 1}
    )
    return mocks


@pytest.mark.parametrize("status", ["armed", "claimed"])
def test_already_armed_refuses_before_the_target_is_touched(status):
    """Seen live: MigrationAlreadyArmed came from Arm, after the reset had truncated the target."""
    run = run_migrate(reset_mocks(ReadMigration=migration_item(status)))
    assert (run.status, run.error) == ("FAILED", "MigrationAlreadyArmed"), (run.error, run.path)
    assert run.called("RunDumpJob") == [] and run.called("RunResetJob") == []
    assert run.called("Arm") == [] and run.called("StartReplicationTask") == []
    assert "old" in run.called("ClassifyFailure")[0]["Payload"]["failure"]["Cause"]
    assert run.called("ReleaseLock")
    read = run.called("ReadMigration")[0]
    assert read["Key"] == {"pk": {"S": f"MIGRATION#{TASK_ARN}"}, "sk": {"S": "ACTIVE"}}


def test_a_posted_migration_item_does_not_stop_the_guard():
    run = run_migrate(reset_mocks(ReadMigration=migration_item("posted")))
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    assert [n for n in run.path if n in ("ArmGuard", "DumpJobs", "ResetJobs", "Arm", "StartReplicationTask")] == [
        "ArmGuard",
        "DumpJobs",
        "ResetJobs",
        "Arm",
        "StartReplicationTask",
    ]


def test_migrate_dry_run_hands_over_to_post_without_dms():
    run = run_migrate(migrate_mocks(req=request(dryRun=True)))
    assert run.status == "SUCCEEDED", (run.error, run.cause)
    assert run.called("StartReplicationTask") == [] and run.called("Arm") == []
    assert all(a["Input"]["dryRun"] is True for a in run.called("RunPreJob"))
    post = run.called("StartPostDryRun")[0]
    assert post["Name"] == "run-1-post" and post["Input"]["outcome"]["outcome"] == "DRY_RUN"
    assert post["Input"]["startedBy"] == "migrate:dry-run"
    assert run.called("ReleaseLock") == []  # post releases it


def test_migrate_plan_only_inspects_reports_and_unlocks():
    run = run_migrate(migrate_mocks(req=request(operation="plan")))
    assert run.status == "SUCCEEDED" and run.output["status"] == "PLANNED"
    assert run.called("RunPreJob") == [] and run.called("Arm") == [] and run.called("StartReplicationTask") == []
    assert run.called("WriteReport")[0]["Payload"]["status"] == "PLANNED"
    assert run.called("ReleaseLock")


def test_second_run_is_rejected_by_lock_without_touching_dms_or_the_lock():
    run = run_migrate(migrate_mocks(AcquireLock=raises("DynamoDb.ConditionalCheckFailedException")))
    assert (run.status, run.error) == ("FAILED", "RunAlreadyActive")
    assert run.path == ["ResolveContext", "AcquireLock", "MarkRunEndedWithoutLock", "FailWithoutLock"]
    assert run.called("MarkRunEndedWithoutLock")[0]["ExpressionAttributeValues"][":s"] == {"S": "REJECTED"}


def test_invalid_request_fails_before_lock():
    run = run_migrate(migrate_mocks(ResolveContext=raises("InvalidRunRequest", "$.operation: bad")))
    assert (run.status, run.error, run.cause) == ("FAILED", "InvalidRunRequest", "$.operation: bad")
    assert run.called("AcquireLock") == []


def test_rejected_decision_reports_and_unlocks_without_failure_jobs():
    dec = decision(action="reject", start_type=None, error="TaskAlreadyRunning", reason="already running")
    run = run_migrate(migrate_mocks(dec=dec, RunOnFailureJob=raises("ShouldNotRun")))
    assert (run.status, run.error) == ("FAILED", "TaskAlreadyRunning")
    assert run.called("ClassifyFailure") == [] and run.called("RunPreJob") == []
    assert run.called("WriteReport")[0]["Payload"]["status"] == "REJECTED"
    assert run.called("ReleaseLock")


def test_reset_jobs_run_before_the_start_with_reset_tables():
    run = run_migrate(migrate_mocks(dec=decision(start_type="resume-processing", reset=True)))
    assert run.status == "SUCCEEDED"
    order = [n for n in run.path if n in ("PreJobs", "ResetJobs", "Arm", "StartReplicationTask")]
    assert order == ["PreJobs", "ResetJobs", "Arm", "StartReplicationTask"]
    assert run.called("RunResetJob")[0]["Input"]["ctx"]["resetScope"] == "partial"


def test_pre_job_failure_stops_before_dms_and_unlocks():
    run = run_migrate(migrate_mocks(RunPreJob=raises("JobFailed", '{"job": "check-permissions"}')))
    assert run.status == "FAILED"
    failure = run.called("ClassifyFailure")[0]["Payload"]["failure"]
    assert failure["Error"] == "JobFailed" and failure["phase"] == "pre"
    assert run.called("Arm") == [] and run.called("StartReplicationTask") == [] and run.called("ReleaseLock")


def test_failure_in_inspect_is_classified_and_unlocked():
    run = run_migrate(migrate_mocks(InspectTask=raises("DmsTaskNotFound", "gone")))
    assert run.status == "FAILED" and run.called("ReleaseLock")
    assert run.called("ClassifyFailure")[0]["Payload"]["failure"]["Error"] == "DmsTaskNotFound"


def test_chained_resume_skips_pre_jobs_and_passes_the_parent_start_time():
    req = request(operation="resume", attempt=2, parentRunId="run-0", parentStartedAt="2026-10-28T10:00:00Z")
    run = run_migrate(migrate_mocks(req=req, dec=decision(start_type="resume-processing", reset=True)))
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    assert run.called("RunPreJob") == [] and len(run.called("RunResetJob")) == 1
    assert run.called("InspectTask")[0]["Payload"]["startedAt"] == "2026-10-28T10:00:00Z"
    assert run.called("Arm")[0]["Item"]["attempt"] == {"N": "2"}


def test_chained_resume_arms_without_starting_a_task_dms_restarted_itself():
    req = request(operation="resume", attempt=2, parentRunId="run-0", parentStartedAt="2026-10-28T10:00:00Z")
    run = run_migrate(migrate_mocks(req=req, dec=decision(action="wait", start_type=None)))
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    assert run.called("StartReplicationTask") == [] and run.called("RunResetJob") == []
    assert run.called("Arm")[0]["Item"]["startedAt"] == {"S": "2026-10-28T10:00:00Z"}
    assert run.output["startType"] is None


def test_report_started_failure_still_ends_started():
    run = run_migrate(migrate_mocks(ReportStarted=raises("Lambda.Unknown", "boom")))
    assert run.status == "SUCCEEDED" and run.called("ReleaseLock") == []


# --- post scenarios -------------------------------------------------------------------

POST_INPUT = {
    "runId": "run-1",
    "taskArn": TASK_ARN,
    "outcome": {"kind": "SUCCESS", "outcome": "COMPLETED"},
    "startedBy": "event-handler",
}
JOB_CTX = {"taskArn": TASK_ARN, "migrationType": "full-load", "startType": "start-replication", "attempt": 1}


def loaded(failure: Any = None, attempt: int = 1, run_id: str = "run-1", **req_kw: Any) -> dict[str, Any]:
    ok = failure is None
    return {
        "runId": run_id,
        "request": request(
            autoResume={"maxAttempts": 1, "onlyCategories": ["TRANSIENT_SOURCE_CONN"]}, jobsVersion=None, **req_kw
        ),
        "ctx": ctx(),
        "inspect": {"state": "fresh", "completionSpec": COMPLETION},
        "jobCtx": {**JOB_CTX, "attempt": attempt},
        "attempt": attempt,
        "startedAt": "2026-10-28T10:00:00.000Z",
        "startedBy": "event-handler",
        "dmsOutcome": {"outcome": "COMPLETED", "cause": "full load finished", "dms": {}} if ok else None,
        "failure": failure,
    }


def post_mocks(load: dict[str, Any] | None = None, **over: Any):
    mocks: dict[str, Any] = {
        "LoadRun": payload(load or loaded()),
        "RunBaselineJob": verify_ok,
        "RunResumeBaselineJob": verify_ok,
        "RecordResumeBaseline": payload({"recorded": None}),
        "TakeOverLock": lambda _a: {},
        "DeleteMigration": lambda _a: {},
        "RunPostJob": job_ok,
        "RunOnFailureJob": job_ok,
        "ClassifyFailure": payload(
            {"category": "PERMISSIONS", "retryable": False, "autoResume": False, "reason": "permission denied"}
        ),
        "HandOverLock": lambda _a: {},
        "StartResume": lambda a: {"ExecutionArn": "arn:migrate:" + a["Name"]},
        "GiveLockBack": lambda _a: {},
        "WriteReport": report,
        "Notify": lambda _a: {},
        "ReleaseLock": lambda _a: {},
    }
    mocks.update(over)
    return mocks


def run_post(mocks: dict[str, Any], input_: Any = None):
    return Interpreter(render(POST), mocks).execute(input_ or dict(POST_INPUT))


def test_post_final_report_carries_the_preflight_verdict_migrate_saved():
    """Seen live (S4): the final report (written by post) had preflight null, so the verdict, diff and
    warnings of the run were only in migrate's STARTED report."""
    pf = {"verdict": "reset", "reason": "allowReset", "diff": {"extra": []}, "warnings": []}
    run = run_post(post_mocks(load={**loaded(), "preflight": pf}))
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    assert run.called("WriteReport")[0]["Payload"]["preflight"] == pf
    run = run_post(post_mocks(load={**loaded(), "preflight": None}))
    assert run.status == "SUCCEEDED" and run.called("WriteReport")[0]["Payload"]["preflight"] is None


def test_post_success_runs_post_jobs_reports_and_unlocks():
    run = run_post(post_mocks())
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    assert run.path[:4] == ["LoadRun", "TakeOverLock", "DeleteMigration", "CheckOutcome"]
    assert run.path[-4:] == ["Notify", "ReleaseLock", "Done", "Succeeded"]
    load = run.called("LoadRun")[0]["Payload"]
    assert load["mode"] == "post" and load["input"] == POST_INPUT
    take = run.called("TakeOverLock")[0]
    assert take["ConditionExpression"] == "runId = :r"
    assert take["ExpressionAttributeValues"][":e"]["S"].startswith("arn:aws:states:")
    assert run.called("DeleteMigration")[0]["Key"]["pk"] == {"S": f"MIGRATION#{TASK_ARN}"}
    post = run.called("RunPostJob")[0]["Input"]
    assert post["ctx"]["outcome"] == "COMPLETED" and post["ctx"]["startType"] == "start-replication"
    assert run.called("WriteReport")[0]["Payload"]["status"] == "SUCCEEDED"
    assert run.called("ReleaseLock")[0]["ExpressionAttributeValues"] == {":r": {"S": "run-1"}}
    assert run.output["outcome"] == "COMPLETED" and run.output["status"] == "SUCCEEDED"


def test_post_dms_failure_runs_the_failure_branch_and_unlocks():
    failure = {"Error": "DmsTaskFailed", "Cause": '{"message": "permission denied"}'}
    mocks = post_mocks({**loaded(failure=failure), "ctx": ctx(on_failure=[{"name": "diag", "index": 0}])})
    run = run_post(mocks)
    assert (run.status, run.error) == ("FAILED", "DmsTaskFailed")
    classify = run.called("ClassifyFailure")[0]["Payload"]
    assert classify["failure"]["Error"] == "DmsTaskFailed" and classify["started"] is True
    assert run.called("RunOnFailureJob")[0]["Input"]["failure"]["category"] == "PERMISSIONS"
    assert run.called("RunPostJob") == [] and run.called("HandOverLock") == []
    assert run.called("WriteReport")[0]["Payload"]["status"] == "FAILED"
    assert run.path[-3:] == ["ReleaseLock", "Done", "Failed"]


def test_post_failure_auto_resumes_as_a_new_migrate_with_the_lock_handed_over():
    failure = {"Error": "DmsTaskFailed", "Cause": "source connection lost"}
    mocks = post_mocks(
        loaded(failure=failure, requestedBy="ops@example.com"),
        ClassifyFailure=payload({"category": "TRANSIENT_SOURCE_CONN", "retryable": True, "autoResume": True}),
    )
    run = run_post(mocks)
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    assert run.output["status"] == "RESUMED" and run.output["nextRunId"] == "run-1-r2"
    hand = run.called("HandOverLock")[0]["ExpressionAttributeValues"]
    assert hand[":o"] == {"S": "run-1"} and hand[":n"] == {"S": "run-1-r2"}
    assert hand[":e"]["S"].endswith(":execution:dms-orch-test-migrate:run-1-r2")
    start = run.called("StartResume")[0]
    assert start["Name"] == "run-1-r2" and start["StateMachineArn"].endswith(":stateMachine:dms-orch-test-migrate")
    chained = start["Input"]
    assert chained["operation"] == "resume" and chained["attempt"] == 2 and chained["parentRunId"] == "run-1"
    assert chained["parentStartedAt"] == "2026-10-28T10:00:00.000Z" and chained["requestedBy"] == "ops@example.com"
    assert "jobsVersion" not in chained and "dryRun" not in chained  # nulls and operator-only fields are dropped
    assert run.called("RunOnFailureJob") == []
    assert run.called("WriteReport")[0]["Payload"]["status"] == "RESUMED"


def test_resume_of_a_resume_keeps_one_suffix():
    mocks = post_mocks(
        loaded(failure={"Error": "DmsTaskFailed", "Cause": "x"}, attempt=2, run_id="run-1-r2"),
        ClassifyFailure=payload({"category": "TRANSIENT_SOURCE_CONN", "retryable": True, "autoResume": True}),
    )
    run = run_post(mocks)
    assert run.called("StartResume")[0]["Name"] == "run-1-r3"


def test_post_takes_the_lock_back_when_the_new_migrate_does_not_start():
    mocks = post_mocks(
        loaded(failure={"Error": "DmsTaskFailed", "Cause": "x"}),
        ClassifyFailure=payload({"category": "TRANSIENT_SOURCE_CONN", "retryable": True, "autoResume": True}),
        StartResume=raises("StepFunctions.AccessDeniedException"),
    )
    run = run_post(mocks)
    assert run.status == "FAILED" and run.called("GiveLockBack") and run.called("ReleaseLock")
    assert run.called("WriteReport")[0]["Payload"]["status"] == "FAILED"


def test_post_job_failure_fails_the_run():
    run = run_post(post_mocks(RunPostJob=raises("JobFailed", "{}")))
    assert run.status == "FAILED"
    assert run.called("ClassifyFailure")[0]["Payload"]["failure"]["phase"] == "post"


def test_post_load_failure_fails_without_touching_anything():
    run = run_post(post_mocks(LoadRun=raises("InvalidRunRequest", "no saved context")))
    assert (run.status, run.error) == ("FAILED", "InvalidRunRequest")
    assert run.path == ["LoadRun", "FailLoad"]


def test_post_lock_bookkeeping_failures_do_not_stop_the_run():
    run = run_post(
        post_mocks(
            TakeOverLock=raises("DynamoDb.ConditionalCheckFailedException"),
            DeleteMigration=raises("DynamoDb.ConditionalCheckFailedException"),
            ReleaseLock=raises("DynamoDb.ConditionalCheckFailedException"),
        )
    )
    assert run.status == "SUCCEEDED"


def test_post_report_failure_still_notifies_and_unlocks():
    run = run_post(post_mocks(WriteReport=raises("Lambda.Unknown", "boom")))
    assert run.status == "SUCCEEDED"
    assert run.called("Notify")[0]["Subject"].startswith("[dms-orch] SUCCEEDED (report failed)")
    assert run.called("ReleaseLock")


def test_post_notification_can_be_disabled_and_failures_do_not_matter():
    quiet = post_mocks(WriteReport=payload({"notify": False, "reportS3Uri": "s3://x"}))
    assert run_post(quiet).called("Notify") == []
    noisy = run_post(post_mocks(Notify=raises("SNS.AuthorizationErrorException")))
    assert noisy.status == "SUCCEEDED" and noisy.called("ReleaseLock")


def test_post_classify_crash_still_reports_failed():
    run = run_post(
        post_mocks(loaded(failure={"Error": "DmsTaskFailed"}), ClassifyFailure=raises("Lambda.Unknown", "crash"))
    )
    assert run.status == "FAILED"
    assert run.called("WriteReport")[0]["Payload"]["classification"]["category"] == "UNKNOWN"


# --- job-runner scenarios -----------------------------------------------------------


def spec(**kw: Any) -> dict[str, Any]:
    base = {
        "skip": False,
        "runtime": "lambda",
        "functionArn": "arn:aws:lambda:eu-west-2:1:function:builtin_jobs",
        "taskDefinitionArn": "arn:aws:ecs:eu-west-2:1:task-definition/sql-runner:3",
        "command": ["psql-run", "-f", "x"],
        "timeoutSeconds": 600,
        "maxAttempts": 1,
        "backoffSeconds": 5,
        "idempotent": True,
        "onError": "fail",
        "specS3Uri": "s3://b/runs/r/jobs/pre/00-x/spec.json",
        "resultKey": "runs/r/jobs/pre/00-x/result.json",
        "recordSk": "JOB#pre#00#x",
    }
    return {**base, **kw}


JOB_INPUT = {
    "job": {"name": "x", "index": 0},
    "phase": "pre",
    "runId": "r",
    "dryRun": False,
    "ctx": {},
    "failure": None,
}


def job_mocks(job_spec: dict[str, Any], **over: Any) -> dict[str, Any]:
    mocks = {
        "PrepareJob": payload({"spec": job_spec}),
        "InvokeLambdaJob": payload({"status": "ok", "summary": {"checked": 3}}),
        "RunFargateJob": lambda _a: {
            "Containers": [{"Name": "init-volumes", "ExitCode": 0}, {"Name": "job", "ExitCode": 0}]
        },
        "FetchFargateResult": lambda _a: {"Body": json.dumps({"status": "ok", "summary": {"rows": 10}})},
        "RecordJob": lambda _a: {},
    }
    mocks.update(over)
    return mocks


def run_job(mocks: dict[str, Any], input_: dict[str, Any] | None = None):
    return Interpreter(render(JOB_RUNNER), mocks).execute(input_ or dict(JOB_INPUT))


def test_lambda_job_ok_is_recorded():
    run = run_job(job_mocks(spec()))
    assert run.status == "SUCCEEDED" and run.output["status"] == "ok" and run.output["summary"] == {"checked": 3}
    record = run.called("RecordJob")[0]["Item"]
    assert record["sk"] == {"S": "JOB#pre#00#x"} and record["status"] == {"S": "ok"}
    assert record["attempt"] == {"N": "1"} and json.loads(record["summary"]["S"]) == {"checked": 3}


def test_skipped_job_does_not_run():
    run = run_job(job_mocks(spec(skip=True, skipReason="when is false")))
    assert run.status == "SUCCEEDED" and run.output["status"] == "skipped"
    assert run.called("InvokeLambdaJob") == [] and run.called("RunFargateJob") == []


def test_fargate_job_passes_command_and_env_and_reads_result():
    run = run_job(job_mocks(spec(runtime="fargate")), {**JOB_INPUT, "dryRun": True})
    assert run.status == "SUCCEEDED" and run.output["summary"] == {"rows": 10}
    args = run.called("RunFargateJob")[0]
    override = args["Overrides"]["ContainerOverrides"][0]
    assert override["Command"] == ["psql-run", "-f", "x"]
    env = {e["Name"]: e["Value"] for e in override["Environment"]}
    assert env == {
        "ORCH_JOB_SPEC_S3": "s3://b/runs/r/jobs/pre/00-x/spec.json",
        "ORCH_RUN_ID": "r",
        "ORCH_DRY_RUN": "1",
        "ORCH_ATTEMPT": "1",
    }
    assert args["NetworkConfiguration"]["AwsvpcConfiguration"]["Subnets"] == ["subnet-a", "subnet-b"]
    assert run.called("FetchFargateResult")[0]["Key"] == "runs/r/jobs/pre/00-x/result.json"


def test_fargate_non_zero_exit_is_failure_even_if_result_says_ok():
    run = run_job(
        job_mocks(spec(runtime="fargate"), RunFargateJob=lambda _a: {"Containers": [{"Name": "job", "ExitCode": 2}]})
    )
    assert (run.status, run.error) == ("FAILED", "JobFailed")
    assert "exit code 2" in json.loads(run.cause)["message"]


def test_fargate_exit_code_is_taken_from_the_job_container_not_the_init_container():
    # DescribeTasks does not keep the container order of the task definition.
    for containers in (
        [{"Name": "init-volumes", "ExitCode": 0}, {"Name": "job", "ExitCode": 3}],
        [{"Name": "job", "ExitCode": 3}, {"Name": "init-volumes", "ExitCode": 0}],
    ):
        run = run_job(job_mocks(spec(runtime="fargate"), RunFargateJob=lambda _a, c=containers: {"Containers": c}))
        assert (run.status, run.error) == ("FAILED", "JobFailed")
        assert "exit code 3" in json.loads(run.cause)["message"]


def test_fargate_init_container_failed_job_never_ran():
    containers = [{"Name": "init-volumes", "ExitCode": 1}, {"Name": "job"}]
    run = run_job(job_mocks(spec(runtime="fargate"), RunFargateJob=lambda _a: {"Containers": containers}))
    assert (run.status, run.error) == ("FAILED", "JobFailed")
    assert "exit code -1" in json.loads(run.cause)["message"]


def test_fargate_without_result_file_fails():
    mocks = job_mocks(
        spec(runtime="fargate"),
        RunFargateJob=raises("States.TaskFailed", "Essential container exited"),
        FetchFargateResult=raises("S3.NoSuchKey"),
    )
    run = run_job(mocks)
    assert run.status == "FAILED" and "no result.json" in json.loads(run.cause)["message"]


def test_idempotent_job_is_retried_up_to_max_attempts():
    invoke = sequence({"Payload": {"status": "fail", "message": "first"}}, {"Payload": {"status": "ok"}})
    run = run_job(job_mocks(spec(maxAttempts=2), InvokeLambdaJob=invoke))
    assert run.status == "SUCCEEDED" and run.output["attempt"] == 2
    assert len(run.called("InvokeLambdaJob")) == 2 and "WaitBeforeRetry" in run.path
    assert [r["Item"]["attempt"]["N"] for r in run.called("RecordJob")] == ["1", "2"]


def test_non_idempotent_job_is_not_retried():
    run = run_job(job_mocks(spec(maxAttempts=3, idempotent=False), InvokeLambdaJob=payload({"status": "fail"})))
    assert run.status == "FAILED" and len(run.called("InvokeLambdaJob")) == 1


def test_on_error_continue_lets_the_run_go_on():
    run = run_job(job_mocks(spec(onError="continue"), InvokeLambdaJob=payload({"status": "fail", "message": "m"})))
    assert run.status == "SUCCEEDED" and run.output == {**run.output, "status": "fail", "continued": True}


def test_lambda_crash_is_a_job_failure():
    run = run_job(job_mocks(spec(), InvokeLambdaJob=raises("KeyError", "'connection'")))
    assert run.status == "FAILED" and "KeyError" in json.loads(run.cause)["message"]


def test_prepare_failure_fails_the_job():
    run = run_job(job_mocks(spec(), PrepareJob=raises("InvalidJobSet", "unknown image")))
    assert run.status == "FAILED" and "unknown image" in json.loads(run.cause)["message"]


# Found in the sandbox: SDK integrations use the AWS SDK names of AttributeValue
# members ("Bool", not the wire name "BOOL"); CreateStateMachine accepts the
# definition, every execution then fails with States.Runtime.
SDK_ATTRIBUTE_TYPES = {"S", "N", "B", "Ss", "Ns", "Bs", "M", "L", "Null", "Bool"}


def _attribute_values(node: Any):
    if isinstance(node, dict):
        if (
            len(node) == 1
            and next(iter(node)).isalpha()
            and next(iter(node))[0].isupper()
            and len(next(iter(node))) <= 4
        ):
            yield next(iter(node))
        for v in node.values():
            yield from _attribute_values(v)
    elif isinstance(node, list):
        for v in node:
            yield from _attribute_values(v)


@pytest.mark.parametrize("name", ALL)
def test_dynamodb_sdk_arguments_use_sdk_attribute_type_names(name):
    for state_name, state in render(name)["States"].items():
        if state.get("Resource", "").startswith("arn:aws:states:::aws-sdk:dynamodb:"):
            args = state["Arguments"]
            for key in ("Item", "Key", "ExpressionAttributeValues"):
                types = set(_attribute_values(args.get(key, {})))
                assert types <= SDK_ATTRIBUTE_TYPES, f"{state_name}.{key}: {types - SDK_ATTRIBUTE_TYPES}"


# --- pre-flight cycle and baseline (ADR 0006) ---------------------------------


def ctx_with_check(**jobs: Any) -> dict[str, Any]:
    c = ctx(**jobs)
    c["jobs"]["preflight"] = [VERIFY_JOB]
    return c


def test_preflight_runs_before_the_pre_jobs_and_passes_the_check_to_the_verdict():
    mocks = migrate_mocks()
    mocks["ResolveContext"] = payload({"runId": "run-1", "request": request(), "ctx": ctx_with_check(), "attempt": 1})
    run = run_migrate(mocks)
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    order = [n for n in run.path if n in ("InspectTask", "TargetVerify", "Preflight", "PreJobs", "Arm")]
    assert order == ["InspectTask", "TargetVerify", "Preflight", "PreJobs", "Arm"]
    check = run.called("RunPreflightJob")[0]["Input"]
    assert check["phase"] == "preflight" and check["job"]["handler"] == "builtin:checks/target_verify"
    assert check["ctx"]["selectionRules"] == [{"schema": "app", "table": "%", "action": "include"}]
    pf = run.called("Preflight")[0]["Payload"]
    assert pf["mode"] == "preflight" and pf["verify"]["status"] == "ok" and pf["iteration"] == 0
    assert len(run.called("RunPreJob")) == 2  # once


def test_preflight_without_a_target_check_still_decides():
    run = run_migrate(migrate_mocks())
    assert run.status == "SUCCEEDED"
    assert run.called("RunPreflightJob") == [] and run.called("Preflight")[0]["Payload"]["verify"] is None


def test_manual_verdict_stops_before_anything_runs_and_reports_the_diff():
    diff = {"extra": ["app.junk"], "rowsChanged": ["app.tx"]}
    mocks = migrate_mocks(Preflight=payload(verdict("manual", error="TargetChanged", diff=diff)))
    run = run_migrate(mocks)
    assert (run.status, run.error) == ("FAILED", "TargetChanged")
    assert run.called("RunPreJob") == [] and run.called("Arm") == [] and run.called("StartReplicationTask") == []
    report = run.called("WriteReport")[0]["Payload"]
    assert report["status"] == "MANUAL" and report["preflight"]["diff"] == diff
    assert run.called("ReleaseLock") and run.called("ClassifyFailure") == []


def test_reset_verdict_replaces_the_decision_and_feeds_the_reset_jobs():
    reset = decision(start_type="reload-target", reset=True, resetScope="all", resetTables=["app.a", "app.b"])
    run = run_migrate(migrate_mocks(Preflight=payload(verdict("reset", dec=reset))))
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    assert run.called("RunResetJob")[0]["Input"]["ctx"]["resetTables"] == ["app.a", "app.b"]
    assert run.called("StartReplicationTask")[0]["StartReplicationTaskType"] == "reload-target"


def test_prepare_runs_the_pre_jobs_then_checks_again_once():
    mocks = migrate_mocks(Preflight=sequence({"Payload": verdict("prepare")}, {"Payload": verdict("ready")}))
    mocks["ResolveContext"] = payload({"runId": "run-1", "request": request(), "ctx": ctx_with_check(), "attempt": 1})
    run = run_migrate(mocks)
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    assert [a["Payload"]["iteration"] for a in run.called("Preflight")] == [0, 1]
    assert len(run.called("RunPreflightJob")) == 2
    assert len(run.called("RunPreJob")) == 2  # the two pre jobs ran once, not again after the second check
    assert run.path.count("PreJobs") == 1 and "CheckAfterPre" in run.path


def test_plan_reports_the_verdict_and_runs_nothing():
    mocks = migrate_mocks(req=request(operation="plan"), Preflight=payload(verdict("manual", error="TargetChanged")))
    run = run_migrate(mocks)
    assert run.status == "SUCCEEDED" and run.output["status"] == "PLANNED"
    assert run.called("WriteReport")[0]["Payload"]["preflight"]["verdict"] == "manual"
    assert run.called("RunPreJob") == []


def test_already_completed_goes_to_preflight_instead_of_a_rejection():
    done = decision(action="reject", start_type=None, error="AlreadyCompleted", reason="done")
    reset = decision(start_type="reload-target", reset=True, resetScope="all", resetTables=["app.a"])
    run = run_migrate(migrate_mocks(dec=done, Preflight=payload(verdict("reset", dec=reset))))
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    assert run.called("StartReplicationTask")[0]["StartReplicationTaskType"] == "reload-target"


def test_verdict_reject_is_reported_as_rejected():
    rej = verdict("reject", error="TaskBusy")
    run = run_migrate(migrate_mocks(Preflight=payload(rej)))
    assert (run.status, run.error) == ("FAILED", "TaskBusy")
    assert run.called("WriteReport")[0]["Payload"]["status"] == "REJECTED"


def test_target_check_failure_is_classified_before_dms():
    mocks = migrate_mocks(RunPreflightJob=raises("JobFailed", "Data API unreachable"))
    mocks["ResolveContext"] = payload({"runId": "run-1", "request": request(), "ctx": ctx_with_check(), "attempt": 1})
    run = run_migrate(mocks)
    assert run.status == "FAILED" and run.called("Arm") == []
    assert run.called("ClassifyFailure")[0]["Payload"]["failure"]["phase"] == "preflight"


def test_post_records_the_baseline_on_success_and_on_failure():
    load = {**loaded(), "ctx": ctx_with_check()}
    run = run_post(post_mocks(load))
    assert run.status == "SUCCEEDED"
    assert [n for n in run.path if n in ("PostJobs", "BaselineVerify", "WriteReport")] == [
        "PostJobs",
        "BaselineVerify",
        "WriteReport",
    ]
    assert run.called("RunBaselineJob")[0]["Input"]["phase"] == "baseline"
    assert run.called("WriteReport")[0]["Payload"]["baseline"]["status"] == "ok"

    failed = {**loaded(failure={"Error": "DmsTaskFailed", "Cause": "x"}), "ctx": ctx_with_check()}
    run = run_post(post_mocks(failed))
    assert run.status == "FAILED" and run.called("RunBaselineJob")


def test_post_dry_run_records_no_baseline_and_a_failed_check_does_not_matter():
    dry = {**loaded(dryRun=True), "ctx": ctx_with_check()}
    dry["request"]["dryRun"] = True
    run = run_post(post_mocks(dry))
    assert run.status == "SUCCEEDED" and run.called("RunBaselineJob") == []
    load = {**loaded(), "ctx": ctx_with_check()}
    run = run_post(post_mocks(load, RunBaselineJob=raises("JobFailed", "x")))
    assert run.status == "SUCCEEDED" and run.called("WriteReport")[0]["Payload"]["baseline"] is None


def test_resumed_post_records_the_baseline_before_the_next_migrate_checks_it():
    load = {**loaded(failure={"Error": "DmsTaskFailed", "Cause": "x"}), "ctx": ctx_with_check()}
    mocks = post_mocks(
        load, ClassifyFailure=payload({"category": "TRANSIENT_SOURCE_CONN", "retryable": True, "autoResume": True})
    )
    run = run_post(mocks)
    assert run.output["status"] == "RESUMED"
    # Recorded before the chained migrate can run its pre-flight against it.
    order = [n for n in run.path if n in ("ResumeBaseline", "RecordResumeBaseline", "HandOverLock", "StartResume")]
    assert order == ["ResumeBaseline", "RecordResumeBaseline", "HandOverLock", "StartResume"]
    assert "BaselineVerify" not in run.path
    rec = run.called("RecordResumeBaseline")[0]["Payload"]
    assert rec["mode"] == "baseline" and rec["baseline"]["status"] == "ok"


def test_post_resume_path_records_the_baseline_before_the_hand_over():
    states = render(POST)["States"]
    before_resume = _reachable(states, "CheckAutoResume", blocked=frozenset({"RecordResumeBaseline"}))
    assert "StartResume" not in before_resume and "HandOverLock" not in before_resume


# --- notifications carry SNS message attributes ------------------------------


def test_notifications_carry_message_attributes_even_when_the_report_failed():
    attrs = {"severity": {"DataType": "String", "StringValue": "high"}}
    mocks = post_mocks(
        loaded(failure={"Error": "DmsTaskFailed", "Cause": "x"}),
        WriteReport=payload({"notify": True, "subject": "s", "message": "{}", "attributes": attrs}),
    )
    assert run_post(mocks).called("Notify")[0]["MessageAttributes"] == attrs
    run = run_post(post_mocks(loaded(failure={"Error": "DmsTaskFailed"}), WriteReport=raises("Lambda.Unknown")))
    sent = run.called("Notify")[0]["MessageAttributes"]
    assert sent["status"]["StringValue"] == "FAILED" and sent["severity"]["StringValue"] == "high"
    for name in (MIGRATE, POST):
        for state_name, state in render(name)["States"].items():
            if state.get("Resource") == "arn:aws:states:::sns:publish":
                assert "MessageAttributes" in state["Arguments"], (name, state_name)


# --- dump before reset --------------------------------------------------------

DUMP_JOB = {
    "name": "dump-target",
    "index": 0,
    "runtime": "fargate",
    "command": ["pg-dump-s3", "--connection", "target"],
}


def test_dump_runs_before_the_reset_and_only_when_something_is_reset():
    reset = decision(start_type="reload-target", reset=True, resetScope="all", resetTables=["app.a"])
    mocks = migrate_mocks(dec=reset, Preflight=payload(verdict("resume", dec=reset)), RunDumpJob=job_ok)
    mocks["ResolveContext"] = payload(
        {"runId": "run-1", "request": request(), "ctx": ctx(dump=[DUMP_JOB]), "attempt": 1}
    )
    run = run_migrate(mocks)
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    assert [n for n in run.path if n in ("DumpJobs", "ResetJobs", "StartReplicationTask")] == [
        "DumpJobs",
        "ResetJobs",
        "StartReplicationTask",
    ]
    assert run.called("RunDumpJob")[0]["Input"]["phase"] == "dump"

    no_reset = migrate_mocks(RunDumpJob=raises("ShouldNotRun"))
    no_reset["ResolveContext"] = payload(
        {"runId": "run-1", "request": request(), "ctx": ctx(dump=[DUMP_JOB]), "attempt": 1}
    )
    run = run_migrate(no_reset)
    assert run.status == "SUCCEEDED" and "DumpJobs" not in run.path


def test_failed_dump_stops_before_the_reset():
    reset = decision(start_type="reload-target", reset=True, resetScope="all", resetTables=["app.a"])
    mocks = migrate_mocks(
        dec=reset, Preflight=payload(verdict("resume", dec=reset)), RunDumpJob=raises("JobFailed", "x")
    )
    mocks["ResolveContext"] = payload(
        {"runId": "run-1", "request": request(), "ctx": ctx(dump=[DUMP_JOB]), "attempt": 1}
    )
    run = run_migrate(mocks)
    assert run.status == "FAILED" and run.called("RunResetJob") == [] and run.called("Arm") == []
    assert run.called("ClassifyFailure")[0]["Payload"]["failure"]["phase"] == "dump"


# --- premigration assessment ----------------------------------------------------

ASSESS_CFG = {
    "enabled": True,
    "roleArn": "arn:role",
    "bucket": "res",
    "folder": "pa/core",
    "kmsKeyArn": None,
    "failOnWarning": True,
}


def assess_mocks(statuses: list[str], **cfg: Any) -> dict[str, Any]:
    c = ctx()
    c["assessment"] = {**ASSESS_CFG, **cfg}
    runs = [{"ReplicationTaskAssessmentRuns": [{"Status": st, "LastFailureMessage": "m"}]} for st in statuses]
    return migrate_mocks(
        ResolveContext=payload({"runId": "run-1", "request": request(), "ctx": c, "attempt": 1}),
        StartAssessment=lambda _a: {"ReplicationTaskAssessmentRun": {"ReplicationTaskAssessmentRunArn": "arn:ar"}},
        DescribeAssessment=sequence(*runs),
    )


def test_assessment_runs_before_arm_and_passes():
    run = run_migrate(assess_mocks(["running", "passed"]))
    assert run.status == "SUCCEEDED", (run.error, run.cause, run.path)
    assert run.path.index("StartAssessment") < run.path.index("Arm") < run.path.index("StartReplicationTask")
    assert run.path.index("CheckAssessmentStatus") < run.path.index("ArmGuard") < run.path.index("SaveContext")
    start = run.called("StartAssessment")[0]
    assert start["ServiceAccessRoleArn"] == "arn:role" and start["ResultEncryptionMode"] == "SSE_S3"
    assert start["AssessmentRunName"] == "run-1" and "ResultKmsKeyArn" not in start
    assert len(run.called("DescribeAssessment")) == 2
    assert run.called("ReportStarted")[0]["Payload"]["assessment"]["Status"] == "passed"


def test_assessment_with_kms_results():
    run = run_migrate(assess_mocks(["passed"], kmsKeyArn="arn:kms"))
    start = run.called("StartAssessment")[0]
    assert start["ResultEncryptionMode"] == "SSE_KMS" and start["ResultKmsKeyArn"] == "arn:kms"


@pytest.mark.parametrize(
    "statuses,cfg,error",
    [
        (["warning"], {}, "AssessmentWarning"),
        (["failed"], {}, "AssessmentFailed"),
        (["error-executing"], {}, "AssessmentFailed"),
        (["running"], {}, "AssessmentTimeout"),
    ],
)
def test_assessment_failures_stop_before_dms(statuses, cfg, error):
    run = run_migrate(assess_mocks(statuses, **cfg))
    assert run.status == "FAILED" and run.called("Arm") == [] and run.called("StartReplicationTask") == []
    assert run.called("ClassifyFailure")[0]["Payload"]["failure"]["Error"] == error
    assert run.called("ReleaseLock")


@pytest.mark.parametrize("status", ["failed", "warning"])
def test_failed_assessment_leaves_the_target_untouched(status):
    """Seen live: the reset ran before the assessment; a failed assessment left the
    target truncated. The assessment now runs before the dump and the reset."""
    mocks = assess_mocks([status])
    c = mocks["ResolveContext"]({})["Payload"]["ctx"]
    c = {**c, "jobs": {**c["jobs"], "dump": [DUMP_JOB]}}
    mocks.update(
        ResolveContext=payload({"runId": "run-1", "request": request(), "ctx": c, "attempt": 1}),
        InspectTask=payload({**mocks["InspectTask"]({})["Payload"], "decision": RESET}),
        Preflight=payload(verdict("reset", dec=RESET)),
        RunDumpJob=job_ok,
    )
    run = run_migrate(mocks)
    assert run.status == "FAILED" and run.called("ClassifyFailure"), (run.error, run.path)
    assert run.called("RunDumpJob") == [] and run.called("RunResetJob") == [] and run.called("Arm") == []
    assert run.called("ReadMigration") == []


def test_assessment_warning_is_accepted_when_asked():
    run = run_migrate(assess_mocks(["warning"], failOnWarning=False))
    assert run.status == "SUCCEEDED" and run.called("StartReplicationTask")


def test_no_assessment_unless_enabled_and_not_in_a_dry_run():
    assert "StartAssessment" not in run_migrate(migrate_mocks()).path
    m = assess_mocks(["passed"])
    m["ResolveContext"] = payload(
        {"runId": "run-1", "request": request(dryRun=True), "ctx": {**ctx(), "assessment": ASSESS_CFG}, "attempt": 1}
    )
    assert "StartAssessment" not in run_migrate(m).path

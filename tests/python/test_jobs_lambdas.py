"""prepare_job and builtin_jobs."""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest
from conftest import TASK_ARN, FakeDms, dms_task

from shared import aws as shared_aws

BUCKET = "dms-orch-artifacts-test"
TD = "arn:aws:ecs:eu-west-2:111122223333:task-definition/dms-orch-test-sql-runner:3"
BUILTIN = "arn:aws:lambda:eu-west-2:111122223333:function:dms-orch-test-builtin_jobs"
PROJECT_FN = "arn:aws:lambda:eu-west-2:111122223333:function:ledger-custom-check"
CTX = {
    "project": "ledger",
    "taskKey": "core",
    "taskArn": TASK_ARN,
    "resetTables": ["public.a", "public.b"],
    "migrationType": "full-load",
    "connections": {
        "target": {
            "engine": "postgresql",
            "secretArn": "arn:aws:secretsmanager:eu-west-2:111122223333:secret:t",
            "dataApiClusterArn": "arn:aws:rds:eu-west-2:111122223333:cluster:target",
            "database": "ledger",
        }
    },
    "jobsS3Prefix": f"s3://{BUCKET}/jobs/ledger/v1/",
}


@pytest.fixture
def jobs_env(table, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_BUCKET", BUCKET)
    monkeypatch.setenv("BUILTIN_JOBS_ARN", BUILTIN)
    monkeypatch.setenv("TASK_DEFINITIONS", json.dumps({"sql-runner": TD}))
    monkeypatch.setenv("JOB_LAMBDA_ARNS", json.dumps([PROJECT_FN]))
    monkeypatch.setenv(
        "JOB_ROLE_ARNS", json.dumps({"ledger": "arn:aws:iam::111122223333:role/dms-orch-test-job-ledger"})
    )
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    return s3


def job(**kw: Any) -> dict[str, Any]:
    base = {
        "name": "reset-target",
        "index": 1,
        "runtime": "fargate",
        "image": "sql-runner",
        "timeoutSeconds": 600,
        "retry": {"maxAttempts": 2, "backoffSeconds": 30},
        "idempotent": True,
        "onError": "fail",
        "command": ["psql-run", "--connection", "target", "--truncate", "{{ ctx.resetTables }}"],
    }
    return {**base, **kw}


def event(j: dict[str, Any], phase: str = "reset", **kw: Any) -> dict[str, Any]:
    return {"job": j, "phase": phase, "runId": "run-1", "dryRun": False, "ctx": CTX, "failure": None, **kw}


# --- prepare_job ------------------------------------------------------------------


def test_prepare_fargate_job_renders_command_and_writes_spec(jobs_env):
    from lambdas.prepare_job.handler import handler

    spec = handler(event(job()))["spec"]
    assert spec["command"] == ["psql-run", "--connection", "target", "--truncate", "public.a,public.b"]
    assert spec["taskDefinitionArn"] == TD and spec["recordSk"] == "JOB#reset#01#reset-target"
    assert spec["resultKey"] == "runs/ledger/run-1/jobs/reset/01-reset-target/result.json"
    assert spec["maxAttempts"] == 2 and spec["backoffSeconds"] == 30 and spec["skip"] is False
    assert "connections" not in spec  # routing fields only; the full spec is in S3
    stored = json.loads(
        jobs_env.get_object(Bucket=BUCKET, Key="runs/ledger/run-1/jobs/reset/01-reset-target/spec.json")["Body"].read()
    )
    assert stored["connections"]["target"]["secretArn"].endswith(":secret:t")
    assert stored["jobsS3Prefix"] == CTX["jobsS3Prefix"]


LONG = [f"app.t{i:04d}" for i in range(600)]
LONG_KEY = "runs/run-1/inspect/reset-tables.json"


def _long_ctx(jobs_env, body: dict[str, Any] | None = None) -> dict[str, Any]:
    jobs_env.put_object(Bucket=BUCKET, Key=LONG_KEY, Body=json.dumps(body or {"resetTables": LONG, "count": 600}))
    return {**CTX, "resetTables": [], "resetTablesCount": 600, "resetTablesUri": f"s3://{BUCKET}/{LONG_KEY}"}


@pytest.mark.parametrize("runtime", ["fargate", "lambda"])
def test_prepare_expands_a_long_reset_list_into_spec_json_only(jobs_env, runtime):
    from lambdas.prepare_job.handler import handler

    cmd = ["psql-run", "--connection", "target", "--truncate-scope"]
    j = (
        job(runtime="lambda", handler="builtin:checks/connections", command=[])
        if runtime == "lambda"
        else job(command=cmd)
    )
    spec = handler(event(j, ctx=_long_ctx(jobs_env)))["spec"]
    stored = json.loads(
        jobs_env.get_object(Bucket=BUCKET, Key="runs/ledger/run-1/jobs/reset/01-reset-target/spec.json")["Body"].read()
    )
    assert stored["ctx"]["resetTables"] == LONG and stored["ctx"]["resetTablesCount"] == 600
    assert "app.t0599" not in json.dumps(spec)  # the payload stays small
    if runtime == "lambda":
        assert spec["ctx"]["resetTables"] == [] and spec["ctx"]["resetTablesCount"] == 600


def test_prepare_refuses_a_reset_list_template_when_the_list_is_in_s3(jobs_env):
    from lambdas.prepare_job.handler import handler
    from shared.errors import InvalidJobSet

    with pytest.raises(InvalidJobSet, match="--truncate-scope"):
        handler(event(job(), ctx=_long_ctx(jobs_env)))


def test_prepare_refuses_a_reset_list_that_does_not_match_its_count(jobs_env):
    from lambdas.prepare_job.handler import handler
    from shared.errors import JobFailed

    with pytest.raises(JobFailed, match="600"):
        handler(
            event(job(command=["psql-run", "--truncate-scope"]), ctx=_long_ctx(jobs_env, {"resetTables": LONG[:3]}))
        )
    other = {**CTX, "resetTables": [], "resetTablesCount": 600, "resetTablesUri": "s3://elsewhere/x.json"}
    with pytest.raises(JobFailed, match="artifacts bucket"):
        handler(event(job(command=["psql-run", "--truncate-scope"]), ctx=other))


def test_prepare_builtin_lambda_job(jobs_env):
    from lambdas.prepare_job.handler import handler

    j = job(
        name="grants",
        runtime="lambda",
        handler="builtin:checks/postgresql_source_grants",
        with_={"connection": "target"},
        command=[],
    )
    j["with"] = j.pop("with_")
    spec = handler(event(j, phase="pre"))["spec"]
    assert spec["functionArn"] == BUILTIN and spec["with"] == {"connection": "target"}
    assert spec["connections"] == CTX["connections"]


def test_prepare_project_lambda_must_be_allow_listed(jobs_env):
    from lambdas.prepare_job.handler import handler
    from shared.errors import InvalidJobSet

    ok = handler(event(job(runtime="lambda", function=PROJECT_FN, command=[])))["spec"]
    assert ok["functionArn"] == PROJECT_FN
    with pytest.raises(InvalidJobSet):
        handler(event(job(runtime="lambda", function="arn:aws:lambda:eu-west-2:1:function:evil", command=[])))


def test_prepare_unknown_image(jobs_env):
    from lambdas.prepare_job.handler import handler
    from shared.errors import InvalidJobSet

    with pytest.raises(InvalidJobSet):
        handler(event(job(image="oracle-runner")))


def test_when_uses_results_of_previous_jobs(jobs_env, store, table):
    from lambdas.prepare_job.handler import handler

    table.put_item(
        Item={
            "pk": "RUN#run-1",
            "sk": "JOB#pre#01#schema-diff",
            "phase": "pre",
            "name": "schema-diff",
            "status": "ok",
            "summary": json.dumps({"missingTables": 0}),
        }
    )
    j = job(name="create-tables", when="pre.schema-diff.summary.missingTables > 0")
    spec = handler(event(j, phase="pre"))["spec"]
    assert spec["skip"] is True and "when is false" in spec["skipReason"]
    table.put_item(
        Item={
            "pk": "RUN#run-1",
            "sk": "JOB#pre#01#schema-diff",
            "phase": "pre",
            "name": "schema-diff",
            "status": "ok",
            "summary": json.dumps({"missingTables": 2}),
        }
    )
    assert handler(event(j, phase="pre"))["spec"]["skip"] is False


def test_when_can_use_failure_category(jobs_env):
    from lambdas.prepare_job.handler import handler

    j = job(name="rollback", when="failure.category != 'TRANSIENT_SOURCE_CONN'")
    assert handler(event(j, phase="on_failure", failure={"category": "PERMISSIONS"}))["spec"]["skip"] is False
    skipped = handler(event(j, phase="on_failure", failure={"category": "TRANSIENT_SOURCE_CONN"}))["spec"]
    assert skipped["skip"] is True


def test_bad_when_is_invalid_job_set(jobs_env):
    from lambdas.prepare_job.handler import handler
    from shared.errors import InvalidJobSet

    with pytest.raises(InvalidJobSet):
        handler(event(job(when="pre.x >")))


# --- builtin_jobs -----------------------------------------------------------------


class FakeRdsData:
    """Answers by matching a substring of the SQL."""

    def __init__(self, answers: dict[str, list[dict[str, Any]]]):
        self.answers = answers
        self.calls: list[dict[str, Any]] = []

    def execute_statement(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        for needle, rows in self.answers.items():
            if needle in kw["sql"]:
                return {"formattedRecords": json.dumps(rows)}
        return {"formattedRecords": "[]"}


def builtin_spec(handler_name: str, **kw: Any) -> dict[str, Any]:
    return {
        "name": "x",
        "runId": "run-1",
        "handler": handler_name,
        "with": {},
        "connections": CTX["connections"],
        "ctx": {**CTX, "endpointArns": ["arn:e1", "arn:e2"], "replicationInstanceArn": "arn:ri"},
        "dryRun": False,
        "artifactsS3Prefix": f"s3://{BUCKET}/runs/run-1/jobs/on_failure/00-x/",
        "jobRoleArn": "arn:aws:iam::111122223333:role/dms-orch-test-job-ledger",
        **kw,
    }


def run_builtin(spec: dict[str, Any]) -> dict[str, Any]:
    from lambdas.builtin_jobs.handler import handler

    return handler({"spec": spec, "attempt": 1})


GRANTS_INFO = {
    "usr": "dms_user",
    "wal_level": "logical",
    "max_slots": 10,
    "used_slots": 2,
    "replication": False,
    "superuser": False,
    "rds_replication": True,
}


def test_grants_check_ok(aws):
    rds = FakeRdsData({"current_setting('wal_level')": [GRANTS_INFO], "has_table_privilege": []})
    shared_aws.set_client("rds-data", rds)
    spec = builtin_spec(
        "builtin:checks/postgresql_source_grants",
        **{"with": {"connection": "target", "require": ["SELECT", "REPLICATION"], "walLevel": "logical"}},
    )
    out = run_builtin(spec)
    assert out["status"] == "ok" and out["summary"]["freeSlots"] == 8
    assert rds.calls[0]["resourceArn"].endswith(":cluster:target") and rds.calls[0]["database"] == "ledger"
    assert rds.calls[1]["parameters"] == [{"name": "schemas", "value": {"stringValue": "public"}}]


def test_grants_check_reports_every_problem(aws):
    info = {**GRANTS_INFO, "wal_level": "replica", "rds_replication": False, "used_slots": 10}
    rds = FakeRdsData({"current_setting('wal_level')": [info], "has_table_privilege": [{"tbl": "public.secret"}]})
    shared_aws.set_client("rds-data", rds)
    spec = builtin_spec(
        "builtin:checks/postgresql_source_grants",
        **{"with": {"connection": "target", "require": ["SELECT", "REPLICATION"], "walLevel": "logical"}},
    )
    out = run_builtin(spec)
    assert out["status"] == "fail"
    for part in ("SELECT on 1 table", "rds_replication", "wal_level is replica", "free replication slot"):
        assert part in out["message"]


def test_grants_check_without_data_api_is_a_job_failure(aws):
    conns = {"target": {"engine": "postgresql", "secretArn": "arn:s"}}
    out = run_builtin(
        builtin_spec("builtin:checks/postgresql_source_grants", connections=conns, **{"with": {"connection": "target"}})
    )
    assert out["status"] == "fail" and "Data API" in out["message"]


def test_unknown_connection_and_unknown_job(aws):
    out = run_builtin(builtin_spec("builtin:checks/postgresql_source_grants", **{"with": {"connection": "nope"}}))
    assert out["status"] == "fail" and "not registered" in out["message"]
    assert run_builtin(builtin_spec("builtin:checks/teleport"))["status"] == "fail"


def test_connections_check(aws):
    dms = FakeDms()
    dms.connections = [
        {"EndpointArn": "arn:e1", "EndpointIdentifier": "src", "Status": "successful"},
        {"EndpointArn": "arn:e2", "EndpointIdentifier": "tgt", "Status": "failed", "LastFailureMessage": "timeout"},
    ]
    shared_aws.set_client("dms", dms)
    no_test = {"with": {"test": False}}
    out = run_builtin(builtin_spec("builtin:checks/connections", **no_test))
    assert out["status"] == "fail" and out["summary"]["connections"][1]["error"] == "timeout"
    dms.connections = dms.connections[:1]
    assert run_builtin(builtin_spec("builtin:checks/connections", **no_test))["status"] == "warn"  # e2 never tested
    assert not [c for c in dms.calls if c[0] == "test_connection"]


def test_connections_check_tests_untested_and_failed_endpoints(aws):
    # The orchestrator runs TestConnection itself, so a new endpoint
    # or one whose test was reset by ModifyEndpoint does not fail StartReplicationTask.
    dms = FakeDms()
    dms.connections = [
        {"EndpointArn": "arn:e1", "EndpointIdentifier": "src", "Status": "successful"},
    ]
    shared_aws.set_client("dms", dms)
    out = run_builtin(builtin_spec("builtin:checks/connections"))
    tested = [c[1] for c in dms.calls if c[0] == "test_connection"]
    assert tested == [{"ReplicationInstanceArn": "arn:ri", "EndpointArn": "arn:e2"}]
    assert out["status"] == "ok" and out["summary"]["tested"] == ["arn:e2"]


def test_connections_check_retest_still_failing_fails(aws):
    dms = FakeDms()
    dms.test_outcomes = {"arn:e2": "failed"}
    dms.connections = [
        {"EndpointArn": "arn:e1", "EndpointIdentifier": "src", "Status": "successful"},
        {"EndpointArn": "arn:e2", "EndpointIdentifier": "tgt", "Status": "failed", "LastFailureMessage": "timeout"},
    ]
    shared_aws.set_client("dms", dms)
    out = run_builtin(builtin_spec("builtin:checks/connections"))
    assert [c[1]["EndpointArn"] for c in dms.calls if c[0] == "test_connection"] == ["arn:e2"]
    assert out["status"] == "fail" and "still failing" in json.dumps(out["summary"])


def test_connections_check_test_already_running_is_awaited(aws):
    # Seen live: ModifyEndpoint starts a connection test by itself; TestConnection then
    # fails with InvalidResourceStateFault "Connection is already being tested".
    from botocore.exceptions import ClientError

    class Busy(FakeDms):
        def test_connection(self, **kw: Any) -> dict[str, Any]:
            super().test_connection(**kw)
            raise ClientError(
                {"Error": {"Code": "InvalidResourceStateFault", "Message": "Connection is already being tested"}},
                "TestConnection",
            )

    dms = Busy()
    dms.connections = [{"EndpointArn": "arn:e1", "EndpointIdentifier": "src", "Status": "successful"}]
    shared_aws.set_client("dms", dms)
    out = run_builtin(builtin_spec("builtin:checks/connections"))
    assert out["status"] == "ok"


def test_connections_check_dry_run_does_not_test(aws):
    dms = FakeDms()
    shared_aws.set_client("dms", dms)
    out = run_builtin(builtin_spec("builtin:checks/connections", dryRun=True))
    assert not [c for c in dms.calls if c[0] == "test_connection"]
    assert out["status"] == "warn" and out["summary"]["wouldTest"] == ["arn:e1", "arn:e2"]


def test_row_counts(aws):
    class Rds(FakeRdsData):
        def execute_statement(self, **kw: Any) -> dict[str, Any]:
            self.calls.append(kw)
            n = 10 if "target" in kw["resourceArn"] or '"b"' not in kw["sql"] else 9
            return {"formattedRecords": json.dumps([{"n": n}])}

    conns = {
        **CTX["connections"],
        "source": {**CTX["connections"]["target"], "dataApiClusterArn": "arn:aws:rds:eu-west-2:1:cluster:source"},
    }
    shared_aws.set_client("rds-data", Rds({}))
    out = run_builtin(
        builtin_spec("builtin:checks/row_counts", connections=conns, **{"with": {"tables": ["public.a", "public.b"]}})
    )
    assert out["status"] == "fail" and out["summary"]["mismatched"] == ["public.b"]
    # Dry run: DMS was not started, so a mismatch is expected and must not fail the run.
    dry = run_builtin(
        builtin_spec(
            "builtin:checks/row_counts",
            connections=conns,
            dryRun=True,
            **{"with": {"tables": ["public.a", "public.b"]}},
        )
    )
    assert dry["status"] == "ok" and dry["message"] == "dry run: 1 table(s) would differ"
    assert dry["summary"]["mismatched"] == ["public.b"]


def test_row_counts_rejects_unsafe_table_names(aws):
    shared_aws.set_client("rds-data", FakeRdsData({}))
    conns = {**CTX["connections"], "source": CTX["connections"]["target"]}
    out = run_builtin(
        builtin_spec(
            "builtin:checks/row_counts", connections=conns, **{"with": {"tables": ['public.a"; DROP TABLE x; --']}}
        )
    )
    assert out["status"] == "fail" and "unsafe identifier" in out["message"]


def test_drop_control_tables_dry_run_and_real(aws):
    rds = FakeRdsData(
        {"information_schema.tables": [{"table_name": "awsdms_status"}, {"table_name": "awsdms_history"}]}
    )
    shared_aws.set_client("rds-data", rds)
    dry = run_builtin(builtin_spec("builtin:cleanup/drop_control_tables", dryRun=True))
    assert dry["summary"]["wouldDrop"] == ["awsdms_status", "awsdms_history"]
    assert not any("DROP" in c["sql"] for c in rds.calls)
    assert "awsdms_validation_failures_v1" not in rds.calls[0]["parameters"][1]["value"]["stringValue"]
    real = run_builtin(builtin_spec("builtin:cleanup/drop_control_tables"))
    assert real["summary"]["dropped"] == ["awsdms_status", "awsdms_history"]
    assert 'DROP TABLE IF EXISTS "dms_control"."awsdms_status"' in [c["sql"] for c in rds.calls]


def test_dms_dump_writes_artifact(aws):
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})

    class Dms(FakeDms):
        def describe_events(self, **_kw: Any) -> dict[str, Any]:
            return {"Events": [{"Message": "Replication task has failed"}]}

    task = dms_task("failed", last_failure="boom")
    shared_aws.set_client("dms", Dms(task, [{"SchemaName": "public", "TableName": "a", "TableState": "Table error"}]))
    out = run_builtin(builtin_spec("builtin:diagnostics/dms_dump"))
    assert out["status"] == "ok" and out["summary"]["tablesNotCompleted"] == 1
    key = out["artifacts"][0].removeprefix(f"s3://{BUCKET}/")
    dump = json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    assert dump["task"]["lastFailureMessage"] == "boom" and len(dump["events"]) == 1


def test_builtin_jobs_write_artifacts_as_the_project_job_role(aws):
    """Review finding 1: the shared lambda-jobs role has no runs/* access; artifacts go through the job role."""
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    sts = FakeSts()
    shared_aws.set_client("sts", sts)

    class Dms(FakeDms):
        def describe_events(self, **_kw: Any) -> dict[str, Any]:
            return {"Events": []}

    shared_aws.set_client("dms", Dms(dms_task("failed", last_failure="boom"), []))
    assert run_builtin(builtin_spec("builtin:diagnostics/dms_dump"))["status"] == "ok"
    assert [c["RoleArn"] for c in sts.calls] == [LEDGER_ROLE]
    out = run_builtin(builtin_spec("builtin:diagnostics/dms_dump", jobRoleArn=None))
    assert out["status"] == "fail" and "job role" in out["message"]


# --- per-project job role (review finding 4) ----------------------------------------

LEDGER_ROLE = "arn:aws:iam::111122223333:role/dms-orch-test-job-ledger"


def test_prepare_writes_the_spec_under_the_project_of_the_run(jobs_env):
    """Review finding 1: runs/<project>/<runId>/jobs/..., the only run prefix a project job role can write."""
    from lambdas.prepare_job.handler import handler

    spec = handler(event(job()))["spec"]
    assert spec["specS3Uri"] == f"s3://{BUCKET}/runs/ledger/run-1/jobs/reset/01-reset-target/spec.json"
    assert spec["artifactsS3Prefix"] == f"s3://{BUCKET}/runs/ledger/run-1/jobs/reset/01-reset-target/"


def test_prepare_puts_the_project_job_role_into_the_spec(jobs_env, monkeypatch):
    from lambdas.prepare_job.handler import handler

    monkeypatch.setenv("JOB_ROLE_ARNS", json.dumps({"ledger": LEDGER_ROLE, "cards": "arn:other"}))
    assert handler(event(job()))["spec"]["jobRoleArn"] == LEDGER_ROLE
    j = job(name="grants", runtime="lambda", handler="builtin:checks/connections", command=[])
    assert handler(event(j, phase="pre"))["spec"]["jobRoleArn"] == LEDGER_ROLE


def test_prepare_refuses_a_project_without_a_job_role(jobs_env, monkeypatch):
    from lambdas.prepare_job.handler import handler
    from shared.errors import InvalidJobSet

    monkeypatch.setenv("JOB_ROLE_ARNS", json.dumps({"cards": "arn:other"}))
    with pytest.raises(InvalidJobSet, match="job role"):
        handler(event(job()))


class FakeSts:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def assume_role(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        return {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "ST"}}


def test_builtin_data_api_jobs_act_as_the_project_job_role(aws):
    sts = FakeSts()
    shared_aws.set_client("sts", sts)
    rds = FakeRdsData({"current_setting('wal_level')": [GRANTS_INFO], "has_table_privilege": []})
    shared_aws.set_client("rds-data", rds)
    spec = builtin_spec(
        "builtin:checks/postgresql_source_grants", jobRoleArn=LEDGER_ROLE, **{"with": {"connection": "target"}}
    )
    assert run_builtin(spec)["status"] == "ok"
    assert [c["RoleArn"] for c in sts.calls] == [LEDGER_ROLE]
    assert sts.calls[0]["RoleSessionName"].startswith("run-1")


def test_builtin_data_api_job_without_a_job_role_fails(aws):
    shared_aws.set_client("rds-data", FakeRdsData({}))
    spec = builtin_spec(
        "builtin:checks/postgresql_source_grants", jobRoleArn=None, **{"with": {"connection": "target"}}
    )
    out = run_builtin(spec)
    assert out["status"] == "fail" and "job role" in out["message"]


# --- Aurora Serverless v2 auto-pause: the first Data API call wakes the cluster -----


class ResumingRdsData(FakeRdsData):
    def __init__(self, answers: dict[str, list[dict[str, Any]]], resuming: int):
        super().__init__(answers)
        self.resuming = resuming

    def execute_statement(self, **kw: Any) -> dict[str, Any]:
        if self.resuming:
            self.resuming -= 1
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "DatabaseResumingException", "Message": "resuming"}}, "ExecuteStatement"
            )
        return super().execute_statement(**kw)


def test_data_api_waits_for_a_paused_cluster_to_resume(aws, monkeypatch):
    from lambdas.builtin_jobs import handler as builtin

    monkeypatch.setattr(builtin.time, "sleep", lambda _s: None)
    rds = ResumingRdsData({"current_setting('wal_level')": [GRANTS_INFO], "has_table_privilege": []}, resuming=3)
    shared_aws.set_client("rds-data", rds)
    out = run_builtin(builtin_spec("builtin:checks/postgresql_source_grants", **{"with": {"connection": "target"}}))
    assert out["status"] == "ok" and rds.resuming == 0


# --- checks/target_verify (pre-flight fingerprint, ADR 0006) ---------

SOURCE_CONN = {
    "engine": "postgresql",
    "secretArn": "arn:aws:secretsmanager:eu-west-2:111122223333:secret:s",
    "dataApiClusterArn": "arn:aws:rds:eu-west-2:111122223333:cluster:source",
    "database": "ledger",
}
RULES = [
    {"schema": "app", "table": "%", "action": "include"},
    {"schema": "app", "table": "tmp_%", "action": "exclude"},
]


class ByClusterRdsData:
    """Answers per cluster (source / target) by matching a substring of the SQL."""

    def __init__(self, answers: dict[str, dict[str, list[dict[str, Any]]]]):
        self.answers = answers
        self.calls: list[dict[str, Any]] = []

    def execute_statement(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        cluster = kw["resourceArn"].rsplit(":", 1)[-1]
        for needle, rows in self.answers.get(cluster, {}).items():
            if needle in kw["sql"]:
                return {"formattedRecords": json.dumps(rows)}
        return {"formattedRecords": "[]"}


TARGET_ANSWERS = {
    "pg_stat_user_tables": [
        {"s": "app", "t": "accounts", "est": 10, "ins": 10, "upd": 0, "del": 0},
        {"s": "app", "t": "tmp_load", "est": 5, "ins": 5, "upd": 0, "del": 0},
        {"s": "app", "t": "tx", "est": 5000000, "ins": 5000000, "upd": 2, "del": 0},
    ],
    "information_schema.columns": [
        {"s": "app", "t": "accounts", "c": "id", "ty": "bigint", "n": False},
        {"s": "app", "t": "accounts", "c": "name", "ty": "text", "n": True},
        {"s": "app", "t": "tx", "c": "id", "ty": "bigint", "n": False},
        {"s": "app", "t": "tmp_load", "c": "x", "ty": "text", "n": True},
    ],
    "pg_index": [
        {"s": "app", "t": "accounts", "p": True, "cols": "id"},
        {"s": "app", "t": "accounts", "p": False, "cols": "name"},
        {"s": "app", "t": "tx", "p": True, "cols": "id"},
    ],
    "contype = 'f'": [{"src": "app.tx", "def": "FOREIGN KEY (account_id) REFERENCES app.accounts(id)"}],
    "pg_sequences": [{"s": "app", "q": "accounts_id_seq", "v": 10}],
    "LIKE 'awsdms": [{"t": "dms_control.awsdms_status"}],
    'count(*)::bigint AS n FROM "app"."accounts"': [{"n": 10}],
}
SOURCE_ANSWERS = {
    "pg_stat_user_tables": [
        {"s": "app", "t": "accounts", "est": 10, "ins": 0, "upd": 0, "del": 0},
        {"s": "app", "t": "refs", "est": 3, "ins": 0, "upd": 0, "del": 0},
        {"s": "app", "t": "tmp_x", "est": 1, "ins": 0, "upd": 0, "del": 0},
        {"s": "app", "t": "tx", "est": 5000000, "ins": 0, "upd": 0, "del": 0},
    ]
}


def _verify_spec(**with_: Any) -> dict[str, Any]:
    conns = {**CTX["connections"], "source": SOURCE_CONN}
    return builtin_spec(
        "builtin:checks/target_verify",
        connections=conns,
        ctx={**CTX, "connections": conns, "selectionRules": RULES},
        artifactsS3Prefix=f"s3://{BUCKET}/runs/run-1/jobs/preflight/00-target-verify/",
        **{"with": with_},
    )


def test_target_verify_builds_the_fingerprint_and_writes_it_to_s3(aws):
    from shared import preflight

    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    shared_aws.set_client("sts", FakeSts())
    rds = ByClusterRdsData({"target": TARGET_ANSWERS, "source": SOURCE_ANSWERS})
    shared_aws.set_client("rds-data", rds)
    out = run_builtin(_verify_spec())
    assert out["status"] == "ok", out
    key = "runs/run-1/jobs/preflight/00-target-verify/target-fingerprint.json"
    assert out["summary"]["fingerprintUri"] == f"s3://{BUCKET}/{key}"
    fp = json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    assert set(fp["tables"]) == {"app.accounts", "app.tx"}  # tmp_load is excluded by the rules
    assert fp["outOfScope"] == ["app.tmp_load"]
    acc = fp["tables"]["app.accounts"]
    assert acc["columns"] == [["id", "bigint", False], ["name", "text", True]]
    assert acc["primaryKey"] == ["id"] and acc["uniques"] == [["name"]]
    assert acc["rows"] == 10 and acc["exact"] is True  # small table: count(*)
    assert fp["tables"]["app.tx"]["rows"] == 5000000 and fp["tables"]["app.tx"]["exact"] is False
    assert fp["tables"]["app.tx"]["upd"] == 2
    assert fp["foreignKeys"] == ["app.tx FOREIGN KEY (account_id) REFERENCES app.accounts(id)"]
    assert fp["sequences"] == {"app.accounts_id_seq": 10} and fp["controlTables"] == ["dms_control.awsdms_status"]
    assert fp["expected"] == ["app.accounts", "app.refs", "app.tx"]  # source, filtered by the rules
    assert out["summary"]["fingerprintHash"] == preflight.fingerprint_hash(fp)
    assert out["summary"]["tables"] == 2 and out["summary"]["expected"] == 3
    # schemas from the rules only, no counting of the big table
    sqls = [c["sql"] for c in rds.calls]
    assert not any('FROM "app"."tx"' in s for s in sqls)
    assert any(
        c["parameters"] and {"name": "schemas", "value": {"stringValue": "app"}} in c["parameters"] for c in rds.calls
    )


def test_target_verify_without_a_source_has_no_expectation(aws):
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    shared_aws.set_client("sts", FakeSts())
    shared_aws.set_client("rds-data", ByClusterRdsData({"target": TARGET_ANSWERS}))
    spec = _verify_spec(source=None)
    out = run_builtin(spec)
    assert out["status"] == "ok" and out["summary"]["expected"] is None


def test_target_verify_needs_selection_rules(aws):
    shared_aws.set_client("rds-data", ByClusterRdsData({}))
    spec = _verify_spec()
    spec["ctx"] = {**spec["ctx"], "selectionRules": []}
    out = run_builtin(spec)
    assert out["status"] == "fail" and "selection rules" in out["message"]


# --- a restore records the baseline (source restore) ------------------------------


def _restored_target(table: Any) -> Any:
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    shared_aws.set_client("sts", FakeSts())
    shared_aws.set_client("rds-data", ByClusterRdsData({"target": TARGET_ANSWERS}))
    return table


DUMP = "s3://b/dumps/ledger/core/20261028T103000Z-r1/"


def test_target_verify_records_a_restore_baseline_with_the_dump_run(table):
    from shared.store import Store

    _restored_target(table)
    spec = _verify_spec(source=None, recordBaseline={"runId": "r1", "dumpUri": DUMP})
    spec.update(phase="restore", runId="restore-ledger-core-20261028T120000Z")
    out = run_builtin(spec)
    assert out["status"] == "ok", out
    base = Store(table).get_target_baseline("ledger/core")
    assert base["source"] == "restore" and base["runId"] == "r1"
    assert base["restoreRunId"] == "restore-ledger-core-20261028T120000Z" and base["dumpUri"] == DUMP
    assert base["fingerprintHash"] == out["summary"]["fingerprintHash"]
    assert base["fingerprintUri"] == out["summary"]["fingerprintUri"]
    assert out["summary"]["baseline"] == {"source": "restore", "runId": "r1"}


@pytest.mark.parametrize("phase", ["pre", "post", "preflight", "baseline", None])
def test_only_the_restore_phase_may_record_a_baseline(table, phase):
    """A job set cannot declare a restore phase: only orch.py restore records this baseline."""
    from shared.store import Store

    _restored_target(table)
    spec = _verify_spec(source=None, recordBaseline={"runId": "r1", "dumpUri": DUMP})
    spec["phase"] = phase
    out = run_builtin(spec)
    assert out["status"] == "fail" and "restore" in out["message"]
    assert Store(table).get_target_baseline("ledger/core") is None


def test_restore_baseline_needs_the_dump_run(table):
    _restored_target(table)
    spec = _verify_spec(source=None, recordBaseline={"dumpUri": DUMP})
    spec["phase"] = "restore"
    out = run_builtin(spec)
    assert out["status"] == "fail" and "runId" in out["message"]


# --- dump by cluster snapshot (dump.method = snapshot) ---------------------

SNAP_RUN = "ledger-core-20261029T100000Z-ab12"


class FakeRds:
    """Cluster snapshots of one account: create, describe (with tags), delete; status per poll."""

    def __init__(self, statuses: list[str] | None = None, existing: list[dict[str, Any]] | None = None):
        self.statuses = list(statuses or ["creating", "available"])
        self.snapshots: list[dict[str, Any]] = list(existing or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create_db_cluster_snapshot(self, **kw: Any) -> dict[str, Any]:
        from datetime import UTC, datetime

        self.calls.append(("create", kw))
        name = kw["DBClusterSnapshotIdentifier"]
        snap = {
            "DBClusterSnapshotIdentifier": name,
            "DBClusterSnapshotArn": f"arn:aws:rds:eu-west-2:111122223333:cluster-snapshot:{name}",
            "DBClusterIdentifier": kw["DBClusterIdentifier"],
            "SnapshotType": "manual",
            "Status": "creating",
            "Engine": "aurora-postgresql",
            "EngineVersion": "16.8",
            "AllocatedStorage": 1,
            "SnapshotCreateTime": datetime.now(UTC),
            "TagList": kw.get("Tags") or [],
        }
        self.snapshots.append(snap)
        return {"DBClusterSnapshot": snap}

    def describe_db_cluster_snapshots(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("describe", kw))
        for s in self.snapshots:
            if s["Status"] == "creating" and self.statuses:
                s["Status"] = self.statuses.pop(0)
        out = [s for s in self.snapshots if s["DBClusterIdentifier"] == kw.get("DBClusterIdentifier")]
        return {"DBClusterSnapshots": out}

    def delete_db_cluster_snapshot(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("delete", kw))
        name = kw["DBClusterSnapshotIdentifier"]
        self.snapshots = [s for s in self.snapshots if s["DBClusterSnapshotIdentifier"] != name]
        return {}

    def created(self) -> list[dict[str, Any]]:
        return [kw for name, kw in self.calls if name == "create"]


def _snapshot_spec(**kw: Any) -> dict[str, Any]:
    return builtin_spec(
        "builtin:dump/cluster_snapshot",
        runId=SNAP_RUN,
        phase="dump",
        artifactsS3Prefix=f"s3://{BUCKET}/runs/ledger/{SNAP_RUN}/jobs/dump/00-snapshot-target/",
        **{"with": {"connection": "target", "waitSeconds": 60, **kw}},
    )


def _bucket() -> Any:
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    return s3


def test_cluster_snapshot_is_created_tagged_polled_and_recorded(aws, monkeypatch):
    import re

    from lambdas.builtin_jobs import handler as builtin

    monkeypatch.setattr(builtin.time, "sleep", lambda _s: None)
    s3, sts, rds = _bucket(), FakeSts(), FakeRds(["creating", "creating", "available"])
    shared_aws.set_client("sts", sts)
    shared_aws.set_client("rds", rds)
    out = run_builtin(_snapshot_spec())
    assert out["status"] == "ok", out
    (create,) = rds.created()
    name = create["DBClusterSnapshotIdentifier"]
    # <project>-<taskKey>-<ts>: no host, user or secret in the name.
    assert re.fullmatch(r"ledger-core-\d{8}-\d{6}", name), name
    assert create["DBClusterIdentifier"] == "target"
    tags = {t["Key"]: t["Value"] for t in create["Tags"]}
    assert tags["dms-orch:project"] == "ledger" and tags["dms-orch:task"] == "core"
    assert tags["dms-orch:run"] == SNAP_RUN
    assert {c["RoleArn"] for c in sts.calls} == {LEDGER_ROLE}  # acts as the project job role (rds and s3)
    # Under the job's own prefix: the project job role writes only runs/<project>/*/jobs/*.
    manifest_key = f"runs/ledger/{SNAP_RUN}/jobs/dump/00-snapshot-target/dump.json"
    manifest = json.loads(s3.get_object(Bucket=BUCKET, Key=manifest_key)["Body"].read())
    assert manifest["method"] == "snapshot" and manifest["snapshotIdentifier"] == name
    assert manifest["clusterIdentifier"] == "target" and manifest["status"] == "available"
    assert out["summary"]["snapshotIdentifier"] == name
    assert out["summary"]["manifestUri"] == f"s3://{BUCKET}/{manifest_key}"
    text = json.dumps(manifest).lower()
    assert "rds.amazonaws.com" not in text and "password" not in text and "secret" not in text


def test_cluster_snapshot_retry_picks_up_the_snapshot_of_the_run(aws, monkeypatch):
    from lambdas.builtin_jobs import handler as builtin

    monkeypatch.setattr(builtin.time, "sleep", lambda _s: None)
    clock = iter(range(0, 100_000, 30))
    monkeypatch.setattr(builtin.time, "monotonic", lambda: next(clock))
    _bucket()
    shared_aws.set_client("sts", FakeSts())
    rds = FakeRds(["creating"] * 10)
    shared_aws.set_client("rds", rds)
    first = run_builtin(_snapshot_spec())
    # Still creating when this Lambda's wait ends: a failure the job-runner retries (idempotent).
    assert first["status"] == "fail" and "still creating" in first["message"]
    rds.statuses = ["available"]
    second = run_builtin(_snapshot_spec())
    assert second["status"] == "ok", second
    assert len(rds.created()) == 1  # the retry found the run's snapshot by its tag


def test_cluster_snapshot_failed_status_fails_the_job(aws, monkeypatch):
    from lambdas.builtin_jobs import handler as builtin

    monkeypatch.setattr(builtin.time, "sleep", lambda _s: None)
    _bucket()
    shared_aws.set_client("sts", FakeSts())
    shared_aws.set_client("rds", FakeRds(["failed"]))
    out = run_builtin(_snapshot_spec())
    assert out["status"] == "fail" and "failed" in out["message"]


def test_cluster_snapshot_dry_run_creates_nothing(aws):
    _bucket()
    shared_aws.set_client("sts", FakeSts())
    rds = FakeRds()
    shared_aws.set_client("rds", rds)
    out = run_builtin({**_snapshot_spec(), "dryRun": True})
    assert out["status"] == "ok" and out["summary"]["dryRun"] is True and rds.created() == []


def test_cluster_snapshot_needs_the_cluster_of_the_connection(aws):
    shared_aws.set_client("sts", FakeSts())
    shared_aws.set_client("rds", FakeRds())
    conns = {"target": {**CTX["connections"]["target"], "dataApiClusterArn": None}}
    out = run_builtin({**_snapshot_spec(), "connections": conns})
    assert out["status"] == "fail" and "clusterArn" in out["message"]


def _snap(name: str, days: int, project: str | None = None, task: str | None = None) -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta

    tags = []
    if project:
        tags = [{"Key": "dms-orch:project", "Value": project}, {"Key": "dms-orch:task", "Value": task}]
    return {
        "DBClusterSnapshotIdentifier": name,
        "DBClusterIdentifier": "target",
        "SnapshotType": "manual",
        "Status": "available",
        "SnapshotCreateTime": datetime.now(UTC) - timedelta(days=days),
        "TagList": tags,
    }


def test_snapshot_retention_deletes_only_old_snapshots_of_this_task(aws):
    rds = FakeRds(
        existing=[
            _snap("ledger-core-old", 20, "ledger", "core"),
            _snap("ledger-core-new", 2, "ledger", "core"),
            _snap("ledger-other-old", 20, "ledger", "other"),
            _snap("cards-core-old", 20, "cards", "core"),
            _snap("dba-manual-old", 20),
        ]
    )
    shared_aws.set_client("sts", FakeSts())
    shared_aws.set_client("rds", rds)
    spec = builtin_spec(
        "builtin:cleanup/cluster_snapshots", phase="post", **{"with": {"connection": "target", "retentionDays": 14}}
    )
    dry = run_builtin({**spec, "dryRun": True})
    assert dry["status"] == "ok" and dry["summary"]["wouldDelete"] == ["ledger-core-old"]
    assert not [c for c in rds.calls if c[0] == "delete"]
    out = run_builtin(spec)
    assert out["status"] == "ok" and out["summary"]["deleted"] == ["ledger-core-old"]
    left = sorted(s["DBClusterSnapshotIdentifier"] for s in rds.snapshots)
    assert left == ["cards-core-old", "dba-manual-old", "ledger-core-new", "ledger-other-old"]


def test_target_verify_reports_the_database_size(aws):
    _bucket()
    shared_aws.set_client("sts", FakeSts())
    answers = {**TARGET_ANSWERS, "pg_database_size": [{"b": 123456789}]}
    shared_aws.set_client("rds-data", ByClusterRdsData({"target": answers, "source": SOURCE_ANSWERS}))
    out = run_builtin(_verify_spec())
    assert out["status"] == "ok" and out["summary"]["databaseBytes"] == 123456789


# --- target check through psql on Fargate (verify-target) ----------------


class FakePsql:
    """psql as subprocess.run: answers the SQL on stdin by substring, JSON like json_agg."""

    def __init__(self, answers: dict[str, list[dict[str, Any]]]):
        self.answers = answers
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kw: Any) -> Any:
        from types import SimpleNamespace

        self.calls.append({"argv": argv, **kw})
        for needle, rows in self.answers.items():
            if needle in kw["input"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps(rows) + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="[]\n", stderr="")


PSQL_TARGET = {
    "engine": "postgresql",
    "host": "t.cluster.local",
    "port": 5432,
    "database": "ledger",
    "username": "orch",
    "password": "pw-not-in-argv",
    "sslMode": "verify-full",
}


def test_psql_and_data_api_give_the_same_fingerprint(aws):
    """One fixture catalogue, two transports: the Fargate check must be equivalent to the Lambda one."""
    import orch_verify

    from shared import fingerprint

    target = {**TARGET_ANSWERS, "pg_database_size": [{"b": 4242}]}
    data_api = ByClusterRdsData({"target": target, "source": SOURCE_ANSWERS})
    shared_aws.set_client("rds-data", data_api)
    db_t = lambda_handler_dataapi("target")
    db_s = lambda_handler_dataapi("source")
    fp_api, size_api = fingerprint.build(db_t.query, RULES, max_exact=1_000_000, source_query=db_s.query)

    psql_t, psql_s = FakePsql(target), FakePsql(SOURCE_ANSWERS)
    q_t = orch_verify.psql_query(PSQL_TARGET, runner=psql_t)
    q_s = orch_verify.psql_query({**PSQL_TARGET, "host": "s.cluster.local"}, runner=psql_s)
    fp_psql, size_psql = fingerprint.build(q_t, RULES, max_exact=1_000_000, source_query=q_s)

    assert fp_psql == fp_api and size_psql == size_api == 4242
    from shared import preflight

    assert preflight.fingerprint_hash(fp_psql) == preflight.fingerprint_hash(fp_api)
    # Parameters as psql variables, quoted by psql (:'name'), the password only in the environment.
    first = psql_t.calls[0]
    assert "-v" in first["argv"] and "schemas=app" in first["argv"] and "all=false" in first["argv"]
    assert ":'schemas'" in first["input"] and ":schemas," not in first["input"]
    assert all("pw-not-in-argv" not in " ".join(c["argv"]) for c in psql_t.calls)
    assert first["env"]["PGPASSWORD"] == "pw-not-in-argv"


def test_psql_wrapper_aggregates_whole_rows_not_a_column_of_the_same_name(aws):
    """PostgreSQL resolves a bare name to a column before a table alias: wrapping a query with a
    column ``q`` (the sequences query: ``sequencename AS q``) in ``json_agg(q) ... ) q``
    aggregates that column, so every row arrives as a string. The wrapper must aggregate a
    qualified whole-row reference under an alias no fingerprint query uses."""
    import re

    import orch_verify

    from shared import fingerprint

    psql = FakePsql({**TARGET_ANSWERS, "pg_database_size": [{"b": 1}]})
    fingerprint.build(orch_verify.psql_query(PSQL_TARGET, runner=psql), RULES, max_exact=1_000_000)
    wrapper = re.compile(r"^SELECT coalesce\(json_agg\((\w+)\.\*\), '\[\]'::json\) FROM \((.*)\) (\w+);\n$", re.S)
    assert any("pg_sequences" in c["input"] for c in psql.calls)
    for call in psql.calls:
        m = wrapper.match(call["input"])
        assert m, call["input"]
        ref, body, alias = m.groups()
        assert ref == alias
        assert alias not in set(re.findall(r"\bAS\s+(\w+)", body, re.I)), body


def lambda_handler_dataapi(name: str) -> Any:
    from lambdas.builtin_jobs.handler import DataApi

    conn = {"target": CTX["connections"]["target"], "source": SOURCE_CONN}[name]
    db = DataApi(conn)
    db.client = shared_aws.client("rds-data")
    return db


def test_verify_target_writes_the_fingerprint_and_the_lambda_summary(aws, tmp_path, monkeypatch):
    import orch_verify

    from shared import preflight

    s3 = _bucket()
    spec = {
        "name": "target-verify",
        "phase": "preflight",
        "runId": "run-1",
        "artifactsS3Prefix": f"s3://{BUCKET}/runs/run-1/jobs/preflight/00-target-verify/",
        "ctx": {"selectionRules": RULES},
        "with": {"connection": "target", "source": "source", "exactCountMaxRows": 1_000_000},
    }
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    (tmp_path / "conns.json").write_text(json.dumps({"target": PSQL_TARGET}))
    monkeypatch.setenv("ORCH_JOB_SPEC_FILE", str(tmp_path / "spec.json"))
    monkeypatch.setenv("ORCH_CONNECTIONS_FILE", str(tmp_path / "conns.json"))
    monkeypatch.setenv("ORCH_OUT_DIR", str(tmp_path))
    runner = FakePsql({**TARGET_ANSWERS, "pg_database_size": [{"b": 99}]})
    assert orch_verify.verify_main([], runner=runner, s3=s3) == 0
    result = json.loads((tmp_path / "result.json").read_text())
    key = "runs/run-1/jobs/preflight/00-target-verify/target-fingerprint.json"
    fp = json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    assert result["status"] == "ok" and result["artifacts"] == [f"s3://{BUCKET}/{key}"]
    summary = result["summary"]
    assert summary["fingerprintUri"] == f"s3://{BUCKET}/{key}"
    assert summary["fingerprintHash"] == preflight.fingerprint_hash(fp)
    assert summary["tables"] == 2 and summary["databaseBytes"] == 99
    assert summary["expected"] is None  # no source connection in this job: no expectation
    assert set(summary) == {
        "fingerprintUri",
        "fingerprintHash",
        "tables",
        "rows",
        "expected",
        "outOfScope",
        "databaseBytes",
    }


def test_verify_target_fails_the_job_on_a_psql_error(aws, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import orch_verify

    spec = {"runId": "r", "artifactsS3Prefix": f"s3://{BUCKET}/runs/r/", "ctx": {"selectionRules": RULES}, "with": {}}
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    (tmp_path / "conns.json").write_text(json.dumps({"target": PSQL_TARGET}))
    monkeypatch.setenv("ORCH_JOB_SPEC_FILE", str(tmp_path / "spec.json"))
    monkeypatch.setenv("ORCH_CONNECTIONS_FILE", str(tmp_path / "conns.json"))
    monkeypatch.setenv("ORCH_OUT_DIR", str(tmp_path))

    def broken(argv: list[str], **kw: Any) -> Any:
        return SimpleNamespace(returncode=2, stdout="", stderr="psql: error: connection refused")

    assert orch_verify.verify_main([], runner=broken, s3=_bucket()) != 0
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "fail" and "connection refused" in result["message"]


# --- S1p - the checks survive a cluster waking up from auto-pause -----------


class WakingRdsData(FakeRdsData):
    """A paused Serverless v2 cluster: first resuming, then briefly unavailable, then answers."""

    def __init__(self, answers: dict[str, list[dict[str, Any]]], codes: list[str]):
        super().__init__(answers)
        self.codes = list(codes)

    def execute_statement(self, **kw: Any) -> dict[str, Any]:
        if self.codes:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": self.codes.pop(0), "Message": "waking"}}, "ExecuteStatement")
        return super().execute_statement(**kw)


def test_data_api_waits_while_the_writer_is_still_unavailable(aws, monkeypatch):
    from lambdas.builtin_jobs import handler as builtin

    monkeypatch.setattr(builtin.time, "sleep", lambda _s: None)
    codes = ["DatabaseResumingException", "DatabaseResumingException", "DatabaseUnavailableException"]
    rds = WakingRdsData({"current_setting('wal_level')": [GRANTS_INFO], "has_table_privilege": []}, codes)
    shared_aws.set_client("rds-data", rds)
    out = run_builtin(builtin_spec("builtin:checks/postgresql_source_grants", **{"with": {"connection": "target"}}))
    assert out["status"] == "ok" and rds.codes == []


def test_other_data_api_errors_are_not_retried(aws, monkeypatch):
    from botocore.exceptions import ClientError

    from lambdas.builtin_jobs import handler as builtin

    monkeypatch.setattr(builtin.time, "sleep", lambda _s: None)
    rds = WakingRdsData({}, ["BadRequestException", "DatabaseResumingException"])
    shared_aws.set_client("rds-data", rds)
    db = builtin.DataApi(CTX["connections"]["target"], rds)
    with pytest.raises(ClientError):
        db.query("SELECT 1")
    assert rds.codes == ["DatabaseResumingException"]


def test_wake_budget_fits_the_pre_flight_job_and_the_lambda(aws):
    """A live run measured ~2.5 min more for S1p; Aurora resumes in seconds to about a minute. The
    target check may wake the target and then the source: twice the budget plus the queries must
    fit the job timeout, and the job timeout the Lambda's 900 s."""
    from lambdas.builtin_jobs import handler as builtin
    from lambdas.resolve_context.handler import preflight_jobs

    budget = builtin.resume_budget_seconds()
    assert budget >= 180
    (job,) = preflight_jobs("lambda", {})
    assert job["timeoutSeconds"] >= 2 * budget + 120
    assert job["timeoutSeconds"] + 30 <= 900


# --- review finding 9: the pinned bundle hash reaches the runtimes ---------------


def test_prepare_passes_the_pinned_bundle_hash_to_the_runtime(jobs_env):
    from lambdas.prepare_job.handler import handler

    spec = handler(event(job(bundleSha256="b" * 64)))["spec"]
    assert spec["bundleSha256"] == "b" * 64  # routing fields keep it for the runtime too
    key = "runs/ledger/run-1/jobs/reset/01-reset-target/spec.json"
    stored = json.loads(jobs_env.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    assert stored["bundleSha256"] == "b" * 64
    assert handler(event(job(), runId="run-2"))["spec"]["bundleSha256"] is None


def _bundle_spec(s3: Any, files: dict[str, bytes], pin: bool = True) -> dict[str, Any]:
    from shared import bundle

    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    hashes = {rel: bundle.digest(data) for rel, data in files.items()}
    for rel, data in files.items():
        s3.put_object(Bucket=BUCKET, Key=f"jobs/ledger/v1/{rel}", Body=data)
    s3.put_object(Bucket=BUCKET, Key="jobs/ledger/v1/bundle.sha256", Body=bundle.render(hashes).encode())
    return builtin_spec(
        "builtin:x", jobsS3Prefix=CTX["jobsS3Prefix"], bundleSha256=bundle.total(hashes) if pin else None
    )


def test_builtin_jobs_read_bundle_files_only_when_they_match_the_pin(aws):
    from lambdas.builtin_jobs import handler as builtin
    from shared.errors import JobSetTampered

    s3 = boto3.client("s3", region_name="eu-west-2")
    shared_aws.set_client("s3", s3)
    shared_aws.set_client("sts", FakeSts())
    spec = _bundle_spec(s3, {"sql/check.sql": b"SELECT 1;", "jobset.json": b"{}"})
    assert builtin.bundle_file(spec, "sql/check.sql") == b"SELECT 1;"
    s3.put_object(Bucket=BUCKET, Key="jobs/ledger/v1/sql/check.sql", Body=b"DROP TABLE t;")
    with pytest.raises(JobSetTampered, match="hash"):
        builtin.bundle_file(spec, "sql/check.sql")
    s3.put_object(Bucket=BUCKET, Key="jobs/ledger/v1/sql/extra.sql", Body=b"SELECT 2;")
    with pytest.raises(JobSetTampered, match="not in"):
        builtin.bundle_file(spec, "sql/extra.sql")
    with pytest.raises(JobSetTampered, match="pinned"):
        builtin.bundle_file({**spec, "bundleSha256": "c" * 64}, "jobset.json")

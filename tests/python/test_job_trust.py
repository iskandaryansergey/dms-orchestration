"""prepare_job does not trust the job a job-runner execution was started with (review item N1).

The operator may start the job-runner (orch.py restore). A job is accepted only when it is a
job of the run's pinned job set (looked up by name, hash from the registration), one of the
module's own jobs with its fixed code, or, in the restore phase, the restore jobs orch.py builds
with arguments limited to the task's own dumps. Connections, project and task always come from the
registration and the RUN# item, never from the input.
"""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest
from conftest import TASK_ARN, FakeDms, dms_task

from shared import aws as shared_aws
from shared import bundle as jobs_bundle

BUCKET = "dms-orch-artifacts-test"
PREFIX = "/dms-orch/test"
LEDGER_ROLE = "arn:aws:iam::111122223333:role/dms-orch-test-job-ledger"
CARDS_ROLE = "arn:aws:iam::111122223333:role/dms-orch-test-job-cards"
TD = "arn:aws:ecs:eu-west-2:111122223333:task-definition/dms-orch-test-sql-runner:3"
BUILTIN = "arn:aws:lambda:eu-west-2:111122223333:function:dms-orch-test-builtin_jobs"
SQL_JOBS = "arn:aws:lambda:eu-west-2:111122223333:function:dms-orch-test-sql_jobs"
TARGET = {
    "engine": "postgresql",
    "secretArn": "arn:aws:secretsmanager:eu-west-2:111122223333:secret:ledger-target",
    "host": "target.cluster.local",
    "port": 5432,
    "database": "ledger",
    "sslMode": "verify-full",
}
JOBSET = {
    "schemaVersion": 1,
    "defaults": {"runtime": "ssm", "timeoutSeconds": 1800},
    "phases": {
        "pre": [
            {"name": "create-tables", "command": ["psql-run", "--connection", "target", "--file", "ddl/010.sql"]},
            {"name": "count", "sql": {"inline": "select count(*) from app.t"}, "runtime": "lambda"},
        ],
        "reset": [{"name": "reset-target", "command": ["psql-run", "--connection", "target", "--truncate-scope"]}],
        "post": [],
    },
}
JOBSET_BODY = json.dumps(JOBSET).encode()
PIN = jobs_bundle.total({"jobset.json": jobs_bundle.digest(JOBSET_BODY)})
RULES = [{"schema": "public", "table": "%"}]
DECISION = {
    "startType": "reload-target",
    "resetScope": "tables",
    "resetTables": ["public.a"],
    "resetTablesCount": 1,
    "resetTablesUri": None,
}


def registration(project: str = "ledger", task: str = "core", **kw: Any) -> dict[str, Any]:
    return {
        "schemaVersion": "1",
        "environment": "test",
        "project": project,
        "taskKey": task,
        "taskArn": TASK_ARN,
        "connections": {"target": TARGET},
        "jobs": {"s3Prefix": f"jobs/{project}", "defaultVersion": "v1", "bundles": {"v1": PIN}},
        "defaults": {},
        **kw,
    }


@pytest.fixture
def env(table, store, monkeypatch):
    monkeypatch.setenv("SSM_PREFIX", PREFIX)
    monkeypatch.setenv("ARTIFACTS_BUCKET", BUCKET)
    monkeypatch.setenv("BUILTIN_JOBS_ARN", BUILTIN)
    monkeypatch.setenv("SQL_JOBS_ARN", SQL_JOBS)
    monkeypatch.setenv("SSM_JOBS", "1")
    monkeypatch.setenv("TASK_DEFINITIONS", json.dumps({"sql-runner": TD}))
    monkeypatch.setenv("JOB_LAMBDA_ARNS", "[]")
    monkeypatch.setenv("JOB_ROLE_ARNS", json.dumps({"ledger": LEDGER_ROLE, "cards": CARDS_ROLE}))
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    for project in ("ledger", "cards"):
        s3.put_object(Bucket=BUCKET, Key=f"jobs/{project}/v1/jobset.json", Body=JOBSET_BODY)
        manifest = jobs_bundle.render({"jobset.json": jobs_bundle.digest(JOBSET_BODY)}).encode()
        s3.put_object(Bucket=BUCKET, Key=f"jobs/{project}/v1/{jobs_bundle.MANIFEST}", Body=manifest)
    ssm = boto3.client("ssm", region_name="eu-west-2")
    ssm.put_parameter(Name=f"{PREFIX}/ledger/core", Type="String", Value=json.dumps(registration()))
    ssm.put_parameter(Name=f"{PREFIX}/cards/extra", Type="String", Value=json.dumps(registration("cards", "extra")))
    store.put_run(
        "run-1",
        {
            "project": "ledger",
            "taskKey": "core",
            "status": "RUNNING",
            "jobsVersion": "v1",
            "attempt": 1,
            # What inspect_task decided for this run (InspectTask, then the pre-flight).
            "jobInspect": {"migrationType": "full-load", "selectionRules": RULES},
            "jobDecision": DECISION,
        },
    )
    shared_aws.set_client("dms", FakeDms(dms_task("stopped")))
    return {"s3": s3, "ssm": ssm, "store": store, "table": table}


def ctx(**kw: Any) -> dict[str, Any]:
    base = {
        "project": "ledger",
        "taskKey": "core",
        "taskArn": TASK_ARN,
        "connections": {"target": TARGET},
        "jobsS3Prefix": f"s3://{BUCKET}/jobs/ledger/v1/",
        "jobsVersion": "v1",
        "resetTables": [],
    }
    return {**base, **kw}


def pinned(phase: str, name: str) -> dict[str, Any]:
    """The job as resolve_context hands it to the job-runner."""
    from shared import jobset

    for j in jobset.normalize(json.loads(JOBSET_BODY))[phase]:
        if j["name"] == name:
            return {**j, "bundleSha256": PIN, "jobSet": "default"}
    raise AssertionError(name)


def event(job: dict[str, Any], phase: str = "pre", **kw: Any) -> dict[str, Any]:
    return {"job": job, "phase": phase, "runId": "run-1", "dryRun": False, "ctx": ctx(), "failure": None, **kw}


def spec_json(s3: Any, key: str) -> dict[str, Any]:
    return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())


def prepare(ev: dict[str, Any]) -> dict[str, Any]:
    from lambdas.prepare_job.handler import handler

    return handler(ev)["spec"]


# --- jobs of the pinned job set -----------------------------------------------------------


def test_a_job_of_the_pinned_job_set_runs_with_the_hash_from_the_registration(env):
    spec = prepare(event({**pinned("pre", "create-tables"), "bundleSha256": None}))
    assert spec["jobRoleArn"] == LEDGER_ROLE
    assert spec["bundleSha256"] == PIN  # from the registration, never from the input
    stored = spec_json(env["s3"], "runs/ledger/run-1/jobs/pre/00-create-tables/spec.json")
    assert stored["command"] == ["psql-run", "--connection", "target", "--file", "ddl/010.sql"]


def test_the_jobs_resolve_context_builds_are_accepted_as_they_are(env, monkeypatch):
    """migrate passes ctx.jobs.<phase>[i] and jobCtx unchanged: that path keeps working."""
    from lambdas.resolve_context.handler import build

    out = build({"project": "ledger", "taskKey": "core"}, {"name": "run-1", "arn": "x", "startTime": "t"})
    job_ctx = {
        k: out["ctx"][k] for k in ("project", "taskKey", "taskArn", "connections", "jobsS3Prefix", "jobsVersion")
    }
    for phase, items in (("pre", out["ctx"]["jobs"]["pre"]), ("reset", out["ctx"]["jobs"]["reset"])):
        for j in items:
            assert prepare(event(j, phase=phase, ctx=job_ctx))["name"] == j["name"]
    (verify,) = out["ctx"]["jobs"]["preflight"]
    assert prepare(event(verify, phase="preflight", ctx=job_ctx))["functionArn"] == BUILTIN


@pytest.mark.parametrize(
    "job",
    [
        {"name": "evil", "runtime": "ssm", "command": ["bash", "-c", "env"]},
        {"name": "evil", "runtime": "lambda", "sql": {"inline": "select * from secrets"}},
        {"name": "evil", "runtime": "lambda", "handler": "builtin:checks/connections"},
    ],
    ids=["command", "sql", "handler"],
)
def test_an_inline_job_is_refused(env, job):
    from shared.errors import JobNotInJobSet

    with pytest.raises(JobNotInJobSet, match="evil"):
        prepare(event({"index": 0, **job}))
    assert "Contents" not in env["s3"].list_objects_v2(Bucket=BUCKET, Prefix="runs/")


def test_a_job_set_name_with_another_body_is_refused(env):
    from shared.errors import JobNotInJobSet

    j = {**pinned("pre", "create-tables"), "command": ["bash", "-c", "cat /proc/self/environ"]}
    with pytest.raises(JobNotInJobSet, match="pinned job set"):
        prepare(event(j))
    sql = {**pinned("pre", "count"), "sql": {"inline": "select * from pg_shadow"}}
    with pytest.raises(JobNotInJobSet, match="pinned job set"):
        prepare(event(sql))


def test_a_job_set_with_another_hash_than_the_registration_is_refused(env):
    from shared.errors import JobSetTampered

    env["s3"].put_object(Bucket=BUCKET, Key="jobs/ledger/v1/jobset.json", Body=JOBSET_BODY + b" ")
    with pytest.raises(JobSetTampered):
        prepare(event(pinned("pre", "create-tables")))


def test_an_unpinned_version_is_refused(env):
    from shared.errors import JobSetNotPinned

    env["store"].update_run("run-1", {"jobsVersion": "v9"})
    with pytest.raises(JobSetNotPinned):
        prepare(event(pinned("pre", "create-tables"), ctx=ctx(jobsVersion="v9")))


def test_the_job_set_version_is_the_one_recorded_on_the_run(env):
    """Another registered version than the run's is refused, even when it is pinned."""
    from shared.errors import JobNotInJobSet

    with pytest.raises(JobNotInJobSet, match="v2"):
        prepare(event(pinned("pre", "create-tables"), ctx=ctx(jobsVersion="v2")))


def test_a_job_of_another_phase_is_refused(env):
    from shared.errors import JobNotInJobSet

    with pytest.raises(JobNotInJobSet):
        prepare(event(pinned("pre", "create-tables"), phase="reset"))


# --- the run is the module's, not the input's ----------------------------------------------


def test_a_run_that_does_not_exist_is_refused(env):
    from shared.errors import JobNotInJobSet

    with pytest.raises(JobNotInJobSet, match="run-x"):
        prepare(event(pinned("pre", "create-tables"), runId="run-x"))


def test_a_run_that_has_ended_is_refused(env):
    from shared.errors import JobNotInJobSet

    env["store"].update_run("run-1", {"status": "SUCCEEDED"})
    with pytest.raises(JobNotInJobSet, match="not running"):
        prepare(event(pinned("pre", "create-tables")))


def test_the_project_of_the_input_must_be_the_project_of_the_run(env):
    """A run of ledger cannot be used to get the job role of cards."""
    from shared.errors import JobNotInJobSet

    with pytest.raises(JobNotInJobSet, match="cards"):
        prepare(event(pinned("pre", "create-tables"), ctx=ctx(project="cards", taskKey="extra")))


def test_connections_come_from_the_registration_not_the_input(env):
    forged = {"target": {**TARGET, "host": "attacker.example"}}
    prepare(event(pinned("pre", "create-tables"), ctx=ctx(connections=forged)))
    stored = spec_json(env["s3"], "runs/ledger/run-1/jobs/pre/00-create-tables/spec.json")
    assert stored["connections"]["target"]["host"] == "target.cluster.local"
    assert stored["ctx"]["connections"]["target"]["host"] == "target.cluster.local"


def test_the_context_is_the_run_decision_not_the_input(env):
    """A pinned reset job truncates the list inspect_task decided, not one the caller passes."""
    forged = ctx(
        resetTables=["public.accounts"],
        resetTablesCount=1,
        resetScope="all",
        selectionRules=[{"schema": "x", "table": "y"}],
        endpointArns=["arn:forged"],
        anything="else",
    )
    prepare(event(pinned("reset", "reset-target"), phase="reset", ctx=forged))
    stored = spec_json(env["s3"], "runs/ledger/run-1/jobs/reset/00-reset-target/spec.json")["ctx"]
    assert stored["resetTables"] == ["public.a"] and stored["resetScope"] == "tables"
    assert stored["selectionRules"] == RULES and stored["migrationType"] == "full-load"
    assert stored["endpointArns"] == [] and "anything" not in stored
    assert stored["startType"] == "reload-target" and stored["attempt"] == 1


def test_a_reset_list_in_s3_is_read_only_from_the_inspect_prefix_of_the_run(env):
    from shared.errors import JobFailed

    key = "runs/ledger/run-1/inspect/reset-tables.json"
    env["s3"].put_object(Bucket=BUCKET, Key=key, Body=json.dumps({"resetTables": ["public.big"]}))
    uri = f"s3://{BUCKET}/{key}"
    env["store"].update_run("run-1", {"jobDecision": {**DECISION, "resetTables": [], "resetTablesUri": uri}})
    prepare(event(pinned("reset", "reset-target"), phase="reset"))
    stored = spec_json(env["s3"], "runs/ledger/run-1/jobs/reset/00-reset-target/spec.json")["ctx"]
    assert stored["resetTables"] == ["public.big"]
    # A list elsewhere in the bucket (another run, another project) is never read.
    other = f"s3://{BUCKET}/runs/cards/run-9/inspect/reset-tables.json"
    env["store"].update_run("run-1", {"jobDecision": {**DECISION, "resetTables": [], "resetTablesUri": other}})
    with pytest.raises(JobFailed, match="inspect"):
        prepare(event(pinned("reset", "reset-target"), phase="reset"))


def test_the_dry_run_flag_is_the_one_recorded_on_the_run(env):
    env["store"].update_run("run-1", {"dryRun": True})
    ev = event(pinned("pre", "create-tables"))
    del ev["dryRun"]
    assert prepare(ev)["dryRun"] is True
    env["store"].update_run("run-1", {"dryRun": False})
    assert prepare(ev)["dryRun"] is False


@pytest.mark.parametrize("recorded, given", [(True, False), (False, True), (False, "true"), (None, True)])
def test_a_job_is_not_rerun_with_another_dry_run_flag_than_its_run(env, recorded, given):
    """Run 11 finding 2: a dry re-run of a pinned job of a real run overwrote the run's JOB# record
    with ok, and a post retry skips ok jobs. The flag of the input must match RUN#, or nothing runs."""
    from shared.errors import JobNotInJobSet

    env["store"].update_run("run-1", {"dryRun": recorded})
    with pytest.raises(JobNotInJobSet, match="dry"):
        prepare(event(pinned("pre", "create-tables"), dryRun=given))
    listed = env["s3"].list_objects_v2(Bucket=BUCKET, Prefix="runs/").get("Contents") or []
    assert listed == []  # no spec.json
    assert env["store"].job_results("run-1") == []


# --- the module's own jobs ---------------------------------------------------------------


def test_module_jobs_with_their_fixed_code_are_accepted(env):
    from shared import module_jobs

    (dump,) = module_jobs.dump_jobs({"enabled": True}, {})
    assert prepare(event(dump, phase="dump"))["runtime"] == "ssm"
    (snap,) = module_jobs.dump_jobs({"enabled": True, "method": "snapshot"}, {"snapshotDumps": True})
    assert prepare(event(snap, phase="dump"))["functionArn"] == BUILTIN
    retention = module_jobs.snapshot_retention_job(0, 14)
    assert prepare(event(retention, phase="post"))["functionArn"] == BUILTIN
    (verify,) = module_jobs.preflight_jobs("lambda", {"exactCountMaxRows": 5})
    assert prepare(event(verify, phase="baseline"))["functionArn"] == BUILTIN


def test_snapshot_retention_keeps_at_least_the_registered_days(env):
    """The operator cannot delete the task's snapshots early with a short retentionDays."""
    from shared import module_jobs
    from shared.errors import JobNotInJobSet

    with pytest.raises(JobNotInJobSet):
        prepare(event(module_jobs.snapshot_retention_job(0, 1), phase="post"))
    reg = registration(defaults={"dumpRetentionDays": 30})
    env["ssm"].put_parameter(Name=f"{PREFIX}/ledger/core", Type="String", Value=json.dumps(reg), Overwrite=True)
    with pytest.raises(JobNotInJobSet):
        prepare(event(module_jobs.snapshot_retention_job(0, 14), phase="post"))
    assert prepare(event(module_jobs.snapshot_retention_job(0, 30), phase="post"))["with"]["retentionDays"] == 30


def test_snapshot_wait_is_the_module_value(env):
    from shared import module_jobs
    from shared.errors import JobNotInJobSet

    (snap,) = module_jobs.dump_jobs({"enabled": True, "method": "snapshot"}, {"snapshotDumps": True})
    with pytest.raises(JobNotInJobSet):
        prepare(event({**snap, "with": {**snap["with"], "waitSeconds": 1}}, phase="dump"))


@pytest.mark.parametrize(
    ("phase", "change"),
    [
        ("dump", {"command": ["pg-dump-s3", "--connection", "target", "--extra", "x"]}),
        ("dump", {"command": ["bash", "-c", "env"]}),
        ("preflight", {"with": {"connection": "source", "source": "source", "exactCountMaxRows": 1}}),
        ("preflight", {"handler": "builtin:checks/connections"}),
    ],
)
def test_a_module_job_name_with_other_code_is_refused(env, phase, change):
    from shared import module_jobs
    from shared.errors import JobNotInJobSet

    base = (
        module_jobs.dump_jobs({"enabled": True}, {})[0]
        if phase == "dump"
        else module_jobs.preflight_jobs("lambda", {})[0]
    )
    with pytest.raises(JobNotInJobSet):
        prepare(event({**base, **change}, phase=phase))


# --- restore (orch.py restore) -----------------------------------------------------------

DUMP = f"s3://{BUCKET}/dumps/ledger/core/20261030T100000Z-run-0/"


def restore_job(dump: str = DUMP, **kw: Any) -> dict[str, Any]:
    base = {
        "name": "restore-target",
        "index": 0,
        "runtime": "ssm",
        "command": ["pg-restore-s3", "--connection", "target", "--from", dump, "--confirm"],
        "timeoutSeconds": 21600,
        "retry": {"maxAttempts": 1, "backoffSeconds": 60},
        "idempotent": False,
        "onError": "fail",
    }
    return {**base, **kw}


def verify_job(dump: str = DUMP, **with_: Any) -> dict[str, Any]:
    return {
        "name": "target-verify",
        "index": 0,
        "runtime": "lambda",
        "handler": "builtin:checks/target_verify",
        "with": {
            "connection": "target",
            "source": None,
            "recordBaseline": {"runId": "run-0", "dumpUri": dump},
            **with_,
        },
        "timeoutSeconds": 600,
        "retry": {"maxAttempts": 2, "backoffSeconds": 30},
        "idempotent": True,
        "onError": "fail",
    }


RESTORE_ID = "restore-ledger-core-20261031T100000Z"
RUNNER = "arn:aws:states:eu-west-2:111122223333:execution:dms-orch-test-job-runner"
RESTORE_EXEC = f"{RUNNER}:{RESTORE_ID}"


def restore_event(job: dict[str, Any], **kw: Any) -> dict[str, Any]:
    c = {"project": "ledger", "taskKey": "core", "taskArn": TASK_ARN, "environment": "test", "connections": {}}
    return {
        "job": job,
        "phase": "restore",
        "runId": RESTORE_ID,
        "executionArn": RESTORE_EXEC,
        "dryRun": False,
        "ctx": c,
        "failure": None,
        **kw,
    }


def hold_lock(table: Any, run_id: str = RESTORE_ID, execution_arn: str = RESTORE_EXEC) -> None:
    """As orch.py restore holds it (TaskLock): this restore's id, its current job-runner execution."""
    table.put_item(
        Item={"pk": f"LOCK#{TASK_ARN}", "sk": "LOCK", "entity": "LOCK", "runId": run_id, "executionArn": execution_arn}
    )


@pytest.fixture
def locked(env):
    hold_lock(env["table"])
    return env


def test_restore_runs_the_restore_job_of_the_module_with_the_registered_connections(env, locked):
    spec = prepare(restore_event(restore_job()))
    assert spec["jobRoleArn"] == LEDGER_ROLE and spec["runtime"] == "ssm"
    stored = spec_json(env["s3"], f"runs/ledger/{RESTORE_ID}/jobs/restore/00-restore-target/spec.json")
    assert stored["command"] == ["pg-restore-s3", "--connection", "target", "--from", DUMP, "--confirm"]
    assert stored["connections"] == {"target": TARGET}  # the input had none
    assert stored["idempotent"] is False and stored["maxAttempts"] == 1


def test_restore_records_the_baseline_with_the_selection_rules_of_the_dms_task(env):
    """The scope of the baseline is the task's table mapping, not rules the caller passes."""
    from shared import preflight

    baseline_exec = f"{RESTORE_EXEC}-baseline"
    hold_lock(env["table"], execution_arn=baseline_exec)  # orch.py moves the lock to this execution
    c = {"project": "ledger", "taskKey": "core", "selectionRules": [{"schema": "x", "table": "y"}], "connections": {}}
    spec = prepare(restore_event(verify_job(), ctx=c, executionArn=baseline_exec))
    assert spec["functionArn"] == BUILTIN
    assert spec["with"]["recordBaseline"] == {"runId": "run-0", "dumpUri": DUMP}
    rules = preflight.selection_rules(dms_task("stopped")["TableMappings"])
    assert spec["ctx"]["selectionRules"] == rules and spec["ctx"]["connections"] == {"target": TARGET}


@pytest.mark.parametrize(
    ("lock_run", "lock_exec"),
    [(None, None), ("ledger-core-20261031T090000Z-ab12", RESTORE_EXEC), (RESTORE_ID, f"{RUNNER}:other")],
    ids=["no-lock", "a-run-holds-it", "another-execution"],
)
def test_restore_needs_the_task_lock_held_by_this_restore_execution(env, lock_run, lock_exec):
    """A direct StartExecution cannot restore under a running migration or record a baseline."""
    from shared.errors import JobNotInJobSet

    if lock_run:
        hold_lock(env["table"], lock_run, lock_exec)
    for job in (restore_job(), verify_job()):
        with pytest.raises(JobNotInJobSet, match="lock"):
            prepare(restore_event(job))


@pytest.mark.parametrize(
    "job",
    [
        restore_job(command=["bash", "-c", "env"]),
        restore_job(command=["pg-restore-s3", "--connection", "source", "--from", DUMP, "--confirm"]),
        restore_job(dump=f"s3://{BUCKET}/dumps/cards/extra/20261030T100000Z-run-0/"),
        restore_job(dump=f"s3://{BUCKET}/dumps/ledger/core/../../cards/extra/x/"),
        restore_job(dump="s3://elsewhere/dumps/ledger/core/x/"),
        {**restore_job(), "name": "anything-else"},
        verify_job(connection="source"),
        verify_job(dump=f"s3://{BUCKET}/dumps/cards/extra/x/"),
        {"name": "evil", "index": 0, "runtime": "lambda", "sql": {"inline": "select 1"}},
    ],
    ids=[
        "command",
        "source-connection",
        "other-task-dump",
        "dotdot",
        "other-bucket",
        "name",
        "verify-source",
        "verify-other-dump",
        "inline-sql",
    ],
)
def test_restore_accepts_nothing_else(locked, job):
    from shared.errors import JobNotInJobSet

    with pytest.raises(JobNotInJobSet):
        prepare(restore_event(job))


@pytest.mark.parametrize("given", [None, True, False])
def test_restore_takes_the_dry_run_flag_of_the_operator(locked, given):
    """A restore has its own restore-… id under its own lock, no RUN# of a run: the flag is the operator's."""
    assert prepare(restore_event(restore_job(), dryRun=given))["dryRun"] is bool(given)


@pytest.mark.parametrize("given", ["false", "true", 0, 1, "", [], {}])
def test_restore_refuses_a_dry_run_flag_that_is_not_a_boolean(locked, given):
    """prepare_job does bool(dryRun): the string "false" would become a dry run."""
    from shared.errors import JobNotInJobSet

    with pytest.raises(JobNotInJobSet, match="dryRun"):
        prepare(restore_event(restore_job(), dryRun=given))
    listed = locked["s3"].list_objects_v2(Bucket=BUCKET, Prefix="runs/").get("Contents") or []
    assert listed == []  # no spec.json


def test_restore_needs_a_registered_task_and_a_restore_run_id(locked):
    from shared.errors import JobNotInJobSet, UnknownTask

    with pytest.raises(UnknownTask):
        prepare(restore_event(restore_job(), ctx={"project": "ledger", "taskKey": "nope"}))
    with pytest.raises(JobNotInJobSet, match="restore-"):
        prepare(restore_event(restore_job(), runId="run-1"))


def test_an_unknown_phase_is_refused(env):
    from shared.errors import JobNotInJobSet

    with pytest.raises(JobNotInJobSet, match="phase"):
        prepare(event(pinned("pre", "create-tables"), phase="anything"))

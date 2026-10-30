#!/usr/bin/env python3
"""End-to-end scenarios against the sandbox.

    e2e.py s1   # full load from scratch: SUCCEEDED, counts match, Data API jobs ok
    e2e.py s1p  # S1 after both Aurora clusters auto-paused (min 0 ACU): pre-flight and the Data API jobs wake them
    e2e.py s1f  # S1 with the reset and post jobs in the Fargate sql-runner image (sql_runner.tf)
    e2e.py s1iam # S1 with a Fargate post job logged in by IAM token as orch_iam (iam_auth)
    e2e.py s7   # dry run: nothing started, nothing written
    e2e.py s2   # interrupt a load from outside, then resume without duplicate rows
    e2e.py s3   # revoke SELECT from the DMS user: DMS fails, classified PERMISSIONS
    e2e.py s5   # two runs at once: the second is rejected by the lock
    e2e.py s6   # DMS events rule disabled: the watchdog starts post
    e2e.py idem # StartExecution idempotency by name (same/other input), for docs/dms-observations.md
    e2e.py idem-sfn # post's StartResume meets an execution of the same name: ExecutionAlreadyExists, no failure
    e2e.py s8   # spoiled target (extra table, edited rows) -> MANUAL with the diff, gate refuses
    e2e.py s8fargate # the target check through psql on Fargate: same hash as the Data API one, then S8
    e2e.py s4   # the same target with allowReset -> reset, reload, counts match, gate passes
    e2e.py s9   # S2 through two machines: the resume verdict sees the baseline of the interrupted run
    e2e.py s13  # a post job fails; orch.py post --from-job with the fixed job set skips the jobs already ok
    e2e.py s15  # DMS task replaced by terraform between runs -> MANUAL, then allowReset recovers
    e2e.py s10  # dump before a reload that fails half-way, restore: counts as before, restore baseline (Fargate)
    e2e.py s10snap # dump.method = snapshot before a reload: tagged cluster snapshot, dump.json, retention job
    e2e.py s14  # premigration assessment with fail_on_warning: stops before DMS (variable assessment)
    e2e.py guard   # an armed MIGRATION# of another run refuses before the reset: target untouched
    e2e.py postdie # a post that dies in LoadRun releases its run's lock in seconds (release_lock)
    e2e.py s16  # SQS fan-out: FAILED and MANUAL reach the filtered queue, SUCCEEDED does not (sqs_fanout)

A run is migrate, then post started by the DMS event (ADR 0005); wait() follows the chain.
Needs the terraform outputs of examples/sandbox (sandbox.outputs()), AWS_PROFILE, pg8000.
Prints a JSON verdict per scenario; exit code 0 only if every check passed.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox  # noqa: E402

OUT = sandbox.outputs()
SFN = boto3.client("stepfunctions", region_name=OUT["region"])
DMS = boto3.client("dms", region_name=OUT["region"])
EVENTS = boto3.client("events", region_name=OUT["region"])
TABLE = boto3.resource("dynamodb", region_name=OUT["region"]).Table(OUT["table_name"])


def start(**req: Any) -> dict[str, str]:
    body = {"project": "ledger", "taskKey": "core", "requestedBy": "e2e", **req}
    name = f"ledger-core-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"
    arn = SFN.start_execution(stateMachineArn=OUT["state_machine_arn"], name=name, input=json.dumps(body))[
        "executionArn"
    ]
    print(f"started {name} {json.dumps(req)}")
    return {"runId": name, "arn": arn}


def _execution(machine_arn: str, name: str) -> str:
    return machine_arn.replace(":stateMachine:", ":execution:") + ":" + name


def _describe(arn: str) -> dict[str, Any] | None:
    try:
        return SFN.describe_execution(executionArn=arn)
    except SFN.exceptions.ExecutionDoesNotExist:
        return None


def wait(run: dict[str, str], timeout: int = 900) -> dict[str, Any]:
    """Follow the run to its end (ADR 0005): migrate, then post started by the DMS event, and on
    RESUMED the next migrate. Returns the last execution; ``chain`` lists every execution and
    ``startDate`` is the start of the first one."""
    deadline = time.monotonic() + timeout
    run_id, arn, chain, first_start = run["runId"], run["arn"], [], None
    while time.monotonic() < deadline:
        d = _describe(arn)
        if d is None or d["status"] == "RUNNING":
            time.sleep(5)
            continue
        first_start = first_start or d["startDate"]
        chain.append({"executionArn": arn, "status": d["status"]})
        out = json.loads(d.get("output") or "{}")
        if d["status"] == "SUCCEEDED" and out.get("status") == "STARTED":
            arn = _execution(OUT["post_state_machine_arn"], f"{run_id}-post")
            continue
        if d["status"] == "SUCCEEDED" and out.get("status") == "RESUMED" and out.get("nextRunId"):
            run_id = out["nextRunId"]
            arn = _execution(OUT["state_machine_arn"], run_id)
            continue
        return {**d, "startDate": first_start, "chain": chain, "runId": run_id}
    raise TimeoutError(run["runId"])


def task() -> dict[str, Any]:
    return DMS.describe_replication_tasks(
        Filters=[{"Name": "replication-task-arn", "Values": [OUT["task_arn"]]}], WithoutSettings=True
    )["ReplicationTasks"][0]


def load_type() -> str:
    """start-replication for a task that never ran (the orchestrator rejects anything else
    then, src/shared/dms_outcome.py), reload-target afterwards."""
    stats = task().get("ReplicationTaskStats") or {}
    return "reload-target" if stats.get("StartDate") or stats.get("FreshStartDate") else "start-replication"


def counts() -> dict[str, Any]:
    return {t: (r["source"], r["target"]) for t, r in sandbox.cmd_counts(OUT).items()}


def cause(d: dict[str, Any]) -> Any:
    try:
        return json.loads(d.get("cause") or "{}")
    except ValueError:
        return d.get("cause")


def timeline(run_id: str) -> list[dict[str, Any]]:
    items = TABLE.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq(f"RUN#{run_id}"))["Items"]
    return sorted(items, key=lambda i: i["sk"])


def migration() -> dict[str, Any]:
    return TABLE.get_item(Key={"pk": f"MIGRATION#{OUT['task_arn']}", "sk": "ACTIVE"}).get("Item") or {}


def started_by(d: dict[str, Any]) -> str | None:
    """Who started the post execution: event-handler, watchdog, watchdog:repeat, migrate:dry-run."""
    try:
        return json.loads(d.get("input") or "{}").get("startedBy")
    except ValueError:
        return None


def lock() -> dict[str, Any] | None:
    return TABLE.get_item(Key={"pk": f"LOCK#{OUT['task_arn']}", "sk": "LOCK"}).get("Item")


def verdict(name: str, checks: dict[str, bool], **info: Any) -> bool:
    ok = all(checks.values())
    print(json.dumps({"scenario": name, "ok": ok, "checks": checks, **info}, indent=2, default=str))
    return ok


# --- scenarios ---------------------------------------------------------------------


def jobs(run_id: str) -> dict[str, dict[str, Any]]:
    out = {}
    for i in timeline(run_id):
        if str(i.get("sk", "")).startswith("JOB#"):
            out[i["sk"].split("#", 3)[-1]] = {k: i.get(k) for k in ("status", "message", "startedAt", "finishedAt")}
    return out


def capacity(cluster: str) -> float | None:
    """Last ServerlessDatabaseCapacity of the cluster (0 = auto-paused)."""
    cw = boto3.client("cloudwatch", region_name=OUT["region"])
    now = datetime.now(UTC)
    pts = cw.get_metric_statistics(
        Namespace="AWS/RDS",
        MetricName="ServerlessDatabaseCapacity",
        Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster}],
        StartTime=now - timedelta(minutes=5),
        EndTime=now,
        Period=60,
        Statistics=["Maximum"],
    )["Datapoints"]
    return max(pts, key=lambda p: p["Timestamp"])["Maximum"] if pts else None


def paused(cluster_arn: str) -> bool:
    cluster = cluster_arn.rsplit(":", 1)[-1]
    return capacity(cluster) == 0


def s1f() -> bool:
    kind = load_type()
    run = start(operation="migrate", startType=kind, targetReset={"policy": "always"}, jobsVersion="e2e-fargate")
    d = wait(run, timeout=2400)
    j = jobs(run["runId"])
    c = counts()
    # A task that never ran starts with start-replication and no reset: the Fargate
    # reset job runs from the second run on (reload-target).
    reset_ok = (j.get("truncate-tables") or {}).get("status") == "ok" if kind == "reload-target" else True
    return verdict(
        "S1-fargate",
        {
            "run succeeded": d["status"] == "SUCCEEDED",
            "counts match": all(s == t for s, t in c.values()),
            "truncate-tables (Fargate) ok, if reload-target": reset_ok,
            "tls-target (Fargate) ok": (j.get("tls-target") or {}).get("status") == "ok",
            "row-counts-data-api ok": (j.get("row-counts-data-api") or {}).get("status") == "ok",
            "lock released": lock() is None,
        },
        output=json.loads(d.get("output") or "{}"),
        cause=cause(d) if d["status"] != "SUCCEEDED" else None,
        jobs=j,
        counts=c,
        seconds=(d["stopDate"] - d["startDate"]).total_seconds(),
    )


def s1(after_pause: bool = False) -> bool:
    if after_pause:
        deadline = time.monotonic() + 1800
        while not all(paused(a) for a in OUT["cluster_arns"].values()):
            if time.monotonic() > deadline:
                raise TimeoutError("clusters did not auto-pause in 30 min")
            time.sleep(30)
        print("both clusters auto-paused at", datetime.now(UTC).isoformat())
    run = start(operation="migrate", startType=load_type(), targetReset={"policy": "always"})
    d = wait(run, timeout=2400)
    out = json.loads(d.get("output") or "{}")
    j = jobs(run["runId"])
    c = counts()
    return verdict(
        "S1-after-pause" if after_pause else "S1",
        {
            "run succeeded": d["status"] == "SUCCEEDED",
            "counts match": all(s == t for s, t in c.values()),
            "source-grants (Data API) ok": (j.get("source-grants") or {}).get("status") == "ok",
            "row-counts-data-api ok": (j.get("row-counts-data-api") or {}).get("status") == "ok",
            "lock released": lock() is None,
        },
        output=out,
        jobs=j,
        counts=c,
        seconds=(d["stopDate"] - d["startDate"]).total_seconds(),
        task={k: task().get(k) for k in ("Status", "StopReason", "ReplicationTaskStats")},
    )


def s1iam() -> bool:
    """IAM database authentication: S1 with the job set e2e-iam; its post
    job iam-target logs in to the target as orch_iam with an IAM token over TLS (connection
    target_iam, no secret) and fails unless current_user is that user. Neither the registration
    nor the job spec holds a secret or a token for that connection. Needs iam_auth = true, the
    Fargate image and sbx sync-jobs-iam."""
    run = start(operation="migrate", startType=load_type(), targetReset={"policy": "always"}, jobsVersion="e2e-iam")
    d = wait(run, timeout=2400)
    j = jobs(run["runId"])
    ssm = boto3.client("ssm", region_name=OUT["region"])
    reg = json.loads(ssm.get_parameter(Name=f"{OUT['ssm_prefix']}/ledger/core")["Parameter"]["Value"])
    iam_conn = (reg.get("connections") or {}).get("target_iam") or {}
    s3 = boto3.client("s3", region_name=OUT["region"])
    listed = s3.list_objects_v2(Bucket=OUT["artifacts_bucket"], Prefix=f"runs/ledger/{run['runId']}/jobs/post/")
    spec_key = next((o["Key"] for o in listed.get("Contents") or [] if o["Key"].endswith("iam-target/spec.json")), "")
    spec_text = s3.get_object(Bucket=OUT["artifacts_bucket"], Key=spec_key)["Body"].read().decode() if spec_key else ""
    c = counts()
    return verdict(
        "S1-iam",
        {
            "run succeeded": d["status"] == "SUCCEEDED",
            "iam-target (token login as orch_iam, TLS) ok": (j.get("iam-target") or {}).get("status") == "ok",
            "registration: auth iam, no secret": iam_conn.get("auth") == "iam" and not iam_conn.get("secretArn"),
            "job spec without a password or token": bool(spec_text)
            and "password" not in spec_text.lower()
            and "X-Amz-Signature" not in spec_text,
            "counts match": all(s == t for s, t in c.values()),
            "lock released": lock() is None,
        },
        jobs=j,
        connection={k: iam_conn.get(k) for k in ("auth", "iamUser", "host")},
    )


def s1p(pause_timeout: int = 1800) -> bool:
    """S1 after auto-pause: wait until both clusters report 0 ACU, then a
    full S1. The first Data API calls of pre-flight (target-verify) and of the pre-jobs wake the
    clusters (DatabaseResumingException / DatabaseUnavailableException retried); the timeouts of
    the jobs must cover it. Records how long the wake-up took against a warm S1."""
    t0 = time.monotonic()
    while not all(paused(a) for a in OUT["cluster_arns"].values()):
        if time.monotonic() - t0 > pause_timeout:
            return verdict("S1-after-pause", {"clusters auto-paused": False}, waitedSeconds=pause_timeout)
        time.sleep(30)
    waited = round(time.monotonic() - t0)
    print("both clusters auto-paused at", datetime.now(UTC).isoformat(), f"after {waited}s")
    run = start(operation="migrate", startType=load_type(), targetReset={"policy": "always"})
    d = wait(run, timeout=2400)
    j = jobs(run["runId"])
    r = report(d["runId"])
    verify = j.get("target-verify") or {}
    woke = None
    if verify.get("finishedAt"):
        woke = (datetime.fromisoformat(str(verify["finishedAt"])) - d["startDate"]).total_seconds()
    c = counts()
    return verdict(
        "S1-after-pause",
        {
            "clusters auto-paused": True,
            "run succeeded": d["status"] == "SUCCEEDED",
            "pre-flight target-verify ok after the wake-up": verify.get("status") == "ok",
            "pre-flight verdict reported": bool((r.get("preflight") or {}).get("verdict")),
            "source-grants (Data API) ok": (j.get("source-grants") or {}).get("status") == "ok",
            "row-counts-data-api ok": (j.get("row-counts-data-api") or {}).get("status") == "ok",
            "counts match": all(src == tgt for src, tgt in c.values()),
            "lock released": lock() is None,
        },
        waitedForPauseSeconds=waited,
        secondsToFirstCheck=woke,
        seconds=(d["stopDate"] - d["startDate"]).total_seconds(),
        jobs=j,
    )


def s7() -> bool:
    before = task()
    run = start(operation="migrate", startType=load_type(), targetReset={"policy": "always"}, dryRun=True)
    d = wait(run)
    after = task()
    out = json.loads(d.get("output") or "{}")
    return verdict(
        "S7",
        {
            "run succeeded": d["status"] == "SUCCEEDED",
            "DMS task not started": after["Status"] == before["Status"]
            and after.get("ReplicationTaskStartDate") == before.get("ReplicationTaskStartDate"),
            "lock released": lock() is None,
        },
        output=out,
        jobs=jobs(run["runId"]),
        task={"before": before["Status"], "after": after["Status"]},
    )


def s2() -> bool:
    run = start(operation="migrate", startType=load_type(), targetReset={"policy": "always"})
    # Interrupt the load from outside. On Aurora the 235k rows load in ~12 s, faster than
    # DMS starts accepting StopReplicationTask (seen live: the stop landed after the load
    # finished). So once DMS starts, hold a SHARE lock on the biggest target table (its
    # inserts wait), stop the task, and keep the lock until DMS gives the task up: that
    # table is then certainly not loaded. StopReplicationTask itself is the probe of
    # "really running" (Describe lags, docs/dms-observations.md).
    while task()["Status"] not in ("starting", "running"):
        time.sleep(2)
    hold = sandbox.connect(OUT, "target")
    try:
        hold.run("BEGIN")
        hold.run("LOCK TABLE app.transactions IN SHARE MODE")
        print("locked app.transactions on the target at", datetime.now(UTC).isoformat())
        while True:
            try:
                DMS.stop_replication_task(ReplicationTaskArn=OUT["task_arn"])
                break
            except DMS.exceptions.InvalidResourceStateFault:
                time.sleep(2)
        print("stopped the DMS task from outside at", datetime.now(UTC).isoformat())
        deadline = time.monotonic() + 900
        while task()["Status"] not in ("stopped", "failed") and time.monotonic() < deadline:
            time.sleep(10)
    finally:
        hold.run("ROLLBACK")
        hold.close()
    print("released the lock at", datetime.now(UTC).isoformat())
    first = wait(run)
    t1 = task()
    tables_mid = DMS.describe_table_statistics(ReplicationTaskArn=OUT["task_arn"])["TableStatistics"]
    second_run = start(operation="resume", targetReset={"policy": "if-interrupted"})
    second = wait(second_run)
    out2 = json.loads(second.get("output") or "{}")
    inspect = [
        i for i in timeline(second_run["runId"]) if '"inspect"' in str(i.get("phase")) or i.get("phase") == "inspect"
    ]
    c = counts()
    return verdict(
        "S2",
        {
            "interrupted run failed": first["status"] == "FAILED",
            "classified as external stop": cause(first).get("category") == "EXTERNAL_STOP",
            "resume succeeded": second["status"] == "SUCCEEDED",
            "no duplicate rows": all(s == t for s, t in c.values()),
            "lock released": lock() is None,
        },
        firstCause=cause(first),
        taskAfterStop={
            "status": t1["Status"],
            "stopReason": t1.get("StopReason"),
            "stats": t1.get("ReplicationTaskStats"),
        },
        tablesAfterStop=[(x["TableName"], x["TableState"], x.get("FullLoadRows")) for x in tables_mid],
        resumeOutput=out2,
        resumeDecision=[i.get("detail") for i in inspect],
        counts=c,
    )


def s3() -> bool:
    """Revoke SELECT from the DMS user. (a) The default job set's source-grants pre-check
    stops the run before DMS starts. (b) Without pre-checks DMS itself fails, classified
    PERMISSIONS."""
    sandbox.data_api(OUT, "source", "REVOKE SELECT ON app.transactions FROM dms_source")
    try:
        before = task().get("ReplicationTaskStartDate")
        pre = start(operation="migrate", startType=load_type(), targetReset={"policy": "always"})
        dp = wait(pre)
        started_a = task().get("ReplicationTaskStartDate") != before
        run = start(
            operation="migrate", startType=load_type(), targetReset={"policy": "always"}, jobsVersion="e2e-dms-only"
        )
        d = wait(run)
    finally:
        sandbox.data_api(OUT, "source", "GRANT SELECT ON app.transactions TO dms_source")
    t = task()
    c = cause(d)
    items = timeline(run["runId"])
    on_failure = [i for i in items if str(i.get("sk", "")).startswith("JOB#on_failure")]
    pre_jobs = jobs(pre["runId"])
    return verdict(
        "S3",
        {
            "pre-check run failed": dp["status"] == "FAILED",
            "pre-check: source-grants failed": (pre_jobs.get("source-grants") or {}).get("status") == "fail",
            "pre-check: DMS not started": not started_a,
            "run failed": d["status"] == "FAILED",
            "category PERMISSIONS": c.get("category") == "PERMISSIONS",
            "on_failure job ran": bool(on_failure),
            "lock released": lock() is None,
        },
        preCheckCause=cause(dp),
        cause=c,
        task={
            "status": t["Status"],
            "stopReason": t.get("StopReason"),
            "lastFailureMessage": t.get("LastFailureMessage"),
        },
        tables=[
            (x["TableName"], x["TableState"])
            for x in DMS.describe_table_statistics(ReplicationTaskArn=OUT["task_arn"])["TableStatistics"]
        ],
    )


def s5() -> bool:
    first = start(operation="migrate", startType=load_type(), targetReset={"policy": "always"})
    time.sleep(3)
    t0 = time.monotonic()
    second = start(operation="migrate", startType=load_type(), targetReset={"policy": "always"})
    d2 = wait(second, timeout=120)
    took = time.monotonic() - t0
    d1 = wait(first)
    return verdict(
        "S5",
        {
            "second rejected RunAlreadyActive": d2["status"] == "FAILED" and d2.get("error") == "RunAlreadyActive",
            "second rejected within 30 s": took < 30,
            "first succeeded": d1["status"] == "SUCCEEDED",
            "lock released": lock() is None,
        },
        secondSeconds=round(took, 1),
        firstOutput=json.loads(d1.get("output") or "{}"),
    )


def s6() -> bool:
    rule = OUT["dms_event_rule"]
    EVENTS.disable_rule(Name=rule)
    try:
        run = start(operation="migrate", startType=load_type(), targetReset={"policy": "always"})
        d = wait(run, timeout=1500)
    finally:
        EVENTS.enable_rule(Name=rule)
    return verdict(
        "S6",
        {
            "run succeeded": d["status"] == "SUCCEEDED",
            "post started by the watchdog": started_by(d) == "watchdog",
            "no duplicate rows": all(s == t for s, t in counts().values()),
            "migration item removed by post": not migration(),
            "lock released": lock() is None,
        },
        chain=d["chain"],
        seconds=(d["stopDate"] - d["startDate"]).total_seconds(),
    )


def report(run_id: str) -> dict[str, Any]:
    body = boto3.client("s3", region_name=OUT["region"]).get_object(
        Bucket=OUT["artifacts_bucket"], Key=f"runs/ledger/{run_id}/report.json"
    )["Body"]
    return json.loads(body.read())


def gate(run_id: str) -> int:
    script = Path(__file__).resolve().parents[3] / "scripts" / "orch.py"
    env = {**os.environ, "ORCH_TABLE": OUT["table_name"]}
    return subprocess.run([sys.executable, str(script), "gate", run_id], env=env, check=False).returncode


def s8() -> bool:
    """Spoiled target (ADR 0006): an extra table and an UPDATE in a loaded table since the last
    run -> MANUAL with the diff, severity high, lock released, gate refuses. Run after S1."""
    sandbox.data_api(OUT, "target", "CREATE TABLE IF NOT EXISTS app.junk (id int)")
    sandbox.data_api(
        OUT,
        "target",
        "UPDATE app.accounts SET balance = balance WHERE account_id IN (SELECT min(account_id) FROM app.accounts)",
    )
    try:
        run = start(operation="migrate", startType="reload-target", targetReset={"policy": "always"})
        d = wait(run)
        r = report(d["runId"])
    finally:
        sandbox.data_api(OUT, "target", "DROP TABLE IF EXISTS app.junk")
    diff = (r.get("preflight") or {}).get("diff") or {}
    return verdict(
        "S8",
        {
            "run stopped as MANUAL": r.get("status") == "MANUAL" and d["status"] == "FAILED",
            "verdict manual TargetChanged": (r.get("preflight") or {}).get("error") == "TargetChanged",
            "extra table in the diff": "app.junk" in (diff.get("extra") or []),
            "changed rows in the diff": "app.accounts" in (diff.get("rowsChanged") or []),
            "DMS not started": d.get("chain", [{}])[-1].get("executionArn", "").endswith(run["runId"]),
            "gate refuses": gate(d["runId"]) != 0,
            "lock released": lock() is None,
        },
        preflight=r.get("preflight"),
        chain=d["chain"],
    )


def verify_job(run_id: str) -> dict[str, Any]:
    """The pre-flight target check of a run, from its report."""
    return next(
        (
            j
            for j in report(run_id).get("jobs") or []
            if j.get("phase") == "preflight" and j.get("name") == "target-verify"
        ),
        {},
    )


def s8fargate() -> bool:
    """Target check through psql on Fargate. Two plans on the same target, one
    per runtime, give the same fingerprint hash; then S8 with verifyRuntime = fargate stops as
    MANUAL with the same diff. Needs the Fargate image (sql_runner.tf). Run after S1."""
    via_lambda = wait(start(operation="plan", preflight={"verifyRuntime": "lambda"}))
    via_fargate = wait(start(operation="plan", preflight={"verifyRuntime": "fargate"}), timeout=1800)
    jl, jf = verify_job(via_lambda["runId"]), verify_job(via_fargate["runId"])
    hl = (jl.get("summary") or {}).get("fingerprintHash")
    hf = (jf.get("summary") or {}).get("fingerprintHash")
    sandbox.data_api(OUT, "target", "CREATE TABLE IF NOT EXISTS app.junk (id int)")
    sandbox.data_api(
        OUT,
        "target",
        "UPDATE app.accounts SET balance = balance WHERE account_id IN (SELECT min(account_id) FROM app.accounts)",
    )
    try:
        run = start(
            operation="migrate",
            startType="reload-target",
            targetReset={"policy": "always"},
            preflight={"verifyRuntime": "fargate"},
        )
        d = wait(run, timeout=1800)
        r = report(d["runId"])
    finally:
        sandbox.data_api(OUT, "target", "DROP TABLE IF EXISTS app.junk")
    diff = (r.get("preflight") or {}).get("diff") or {}
    js = verify_job(d["runId"])
    return verdict(
        "S8-fargate",
        {
            "lambda check ok": jl.get("status") == "ok" and jl.get("runtime") == "lambda",
            "fargate check ok": jf.get("status") == "ok" and jf.get("runtime") == "fargate",
            "same fingerprint hash": bool(hl) and hl == hf,
            "spoiled run checked on Fargate": js.get("runtime") == "fargate",
            "run stopped as MANUAL TargetChanged": r.get("status") == "MANUAL"
            and (r.get("preflight") or {}).get("error") == "TargetChanged",
            "extra table in the diff": "app.junk" in (diff.get("extra") or []),
            "changed rows in the diff": "app.accounts" in (diff.get("rowsChanged") or []),
            "lock released": lock() is None,
        },
        hashes={"lambda": hl, "fargate": hf},
        databaseBytes=(jf.get("summary") or {}).get("databaseBytes"),
        preflight=r.get("preflight"),
    )


def s4() -> bool:
    """Explicit reset (ADR 0006): the spoiled target of S8 is reset only with allowReset; the run
    reloads, counts match and the new baseline makes the gate pass."""
    sandbox.data_api(
        OUT,
        "target",
        "UPDATE app.accounts SET balance = balance WHERE account_id IN (SELECT min(account_id) FROM app.accounts)",
    )
    run = start(operation="migrate", startType="reload-target", targetReset={"policy": "always"}, allowReset=True)
    d = wait(run, timeout=2400)
    r = report(d["runId"])
    c = counts()
    return verdict(
        "S4",
        {
            "verdict reset": (r.get("preflight") or {}).get("verdict") in ("reset", "resume"),
            "run succeeded": d["status"] == "SUCCEEDED",
            "counts = source": all(s == t for s, t in c.values()),
            "baseline recorded by this run": (r.get("baseline") or {}).get("fingerprintHash") is not None,
            "gate passes": gate(d["runId"]) == 0,
            "lock released": lock() is None,
        },
        preflight=r.get("preflight"),
        counts=c,
    )


def s9() -> bool:
    """Continue from the checkpoint through two machines (S2 with pre-flight): the interrupted
    run's post records the baseline, the resume finds the target unchanged -> verdict resume."""
    ok = s2()
    last = sorted(
        (
            i
            for i in TABLE.query(
                IndexName="gsi1",
                KeyConditionExpression=boto3.dynamodb.conditions.Key("gsi1pk").eq("PROJECT#ledger"),
                ScanIndexForward=False,
                Limit=1,
            )["Items"]
        ),
        key=lambda i: i.get("gsi1sk", ""),
    )
    run_id = last[-1]["pk"].removeprefix("RUN#") if last else ""
    r = report(run_id) if run_id else {}
    return (
        verdict(
            "S9",
            {
                "S2 checks passed": ok,
                "resume verdict": (r.get("preflight") or {}).get("verdict") == "resume",
            },
            resumeRun=run_id,
            preflight=r.get("preflight"),
        )
        and ok
    )


def _dms_task_address() -> str:
    state = subprocess.run(
        ["terraform", "state", "list"], cwd=Path(sandbox.HERE), check=True, capture_output=True, text=True
    ).stdout.split()
    tasks = [a for a in state if "aws_dms_replication_task" in a]
    if len(tasks) != 1:
        raise RuntimeError(f"expected one DMS task in the state, found {tasks}")
    return tasks[0]


def s15() -> bool:
    """A recreated DMS task (ADR 0006): the DMS task is destroyed and created again between runs. The new
    task is fresh, but our TABLE# copy and the target rows survive -> MANUAL; allowReset recovers."""
    address = _dms_task_address()
    subprocess.run(
        ["terraform", "apply", "-auto-approve", "-input=false", f"-replace={address}"],
        cwd=Path(sandbox.HERE),
        check=True,
    )
    global OUT
    OUT = sandbox.outputs()  # the task ARN changed
    sandbox.cmd_test_connections(OUT)
    first = wait(start(operation="migrate"))
    r1 = report(first["runId"])
    second = wait(start(operation="migrate", allowReset=True), timeout=2400)
    r2 = report(second["runId"])
    c = counts()
    return verdict(
        "S15",
        {
            "new task, history kept -> MANUAL": r1.get("status") == "MANUAL"
            and (r1.get("preflight") or {}).get("error") == "TargetNotEmpty",
            "allowReset -> reset and start-replication": (r2.get("preflight") or {}).get("verdict") == "reset",
            "second run succeeded": second["status"] == "SUCCEEDED",
            "counts = source": all(s == t for s, t in c.values()),
            "lock released": lock() is None,
        },
        first=r1.get("preflight"),
        second=r2.get("preflight"),
    )


def s10() -> bool:
    """Dump and restore: a reload-target with a dump fails half-way (SELECT
    revoked on the source), then the dump is restored: target counts are back to before the run.
    Needs the Fargate image (sql_runner.tf) and the jobsVersion e2e-fargate."""
    before = {t: tgt for t, (_src, tgt) in counts().items()}
    sandbox.data_api(OUT, "source", "REVOKE SELECT ON app.transactions FROM dms_source")
    try:
        run = start(
            operation="migrate",
            startType="reload-target",
            targetReset={"policy": "always"},
            jobsVersion="e2e-fargate",
            dump={"enabled": True},
            autoResume={"maxAttempts": 0},
        )
        d = wait(run, timeout=3600)
    finally:
        sandbox.data_api(OUT, "source", "GRANT SELECT ON app.transactions TO dms_source")
    dumps = jobs(run["runId"]).get("dump-target") or {}
    prefix = "dumps/ledger/core/"
    listed = boto3.client("s3", region_name=OUT["region"]).list_objects_v2(
        Bucket=OUT["artifacts_bucket"], Prefix=prefix, Delimiter="/"
    )
    latest = sorted(p["Prefix"] for p in listed.get("CommonPrefixes") or [] if run["runId"] in p["Prefix"])
    uri = f"s3://{OUT['artifacts_bucket']}/{latest[-1]}" if latest else ""
    script = Path(__file__).resolve().parents[3] / "scripts" / "orch.py"
    env = {
        **os.environ,
        "ORCH_STATE_MACHINE_ARN": OUT["state_machine_arn"],
        "ORCH_TABLE": OUT["table_name"],
        "ORCH_SSM_PREFIX": OUT.get("ssm_prefix", "/dms-orch/sandbox"),
    }
    restored = subprocess.run(
        [
            sys.executable,
            str(script),
            "restore",
            "--project",
            "ledger",
            "--task",
            "core",
            "--dump",
            uri,
            "--confirm",
            "--wait",
        ],
        env=env,
        check=False,
    ).returncode
    after = {t: tgt for t, (_src, tgt) in counts().items()}
    # Live fix: the restored target is the new baseline (source restore, runId of the dump run);
    # a plan right after it is MANUAL/RestoredTarget (never resume, never TargetChanged).
    base = TABLE.get_item(Key={"pk": "TARGET#ledger/core", "sk": "BASELINE"}).get("Item") or {}
    plan = wait(start(operation="plan"), timeout=900)
    pf = report(plan["runId"]).get("preflight") or {}
    return verdict(
        "S10",
        {
            "dump ok before the reset": dumps.get("status") == "ok",
            "dump key holds no host": bool(uri) and "rds.amazonaws.com" not in uri,
            "run failed half-way": d["status"] == "FAILED",
            "restore succeeded": restored == 0,
            "counts = before the run": after == before,
            "baseline from the restore": base.get("source") == "restore" and base.get("runId") == run["runId"],
            "next plan is manual RestoredTarget": pf.get("verdict") == "manual" and pf.get("error") == "RestoredTarget",
            "lock released": lock() is None,
        },
        dump=uri,
        before=before,
        after=after,
        baseline={k: base.get(k) for k in ("source", "runId", "restoreRunId", "dumpUri")},
        planVerdict={k: pf.get(k) for k in ("verdict", "error", "reason")},
    )


def s10snap() -> bool:
    """Dump by cluster snapshot: a reload-target with reset and
    dump.method = snapshot. The snapshot of the target cluster is available before the reset,
    named <project>-<taskKey>-<ts> (no host), tagged with project, task and run, recorded in
    runs/<project>/<runId>/jobs/dump/00-snapshot-target/dump.json; the post job
    snapshot-retention keeps it (younger than the retention).
    Needs snapshot_dumps = true. The snapshot is deleted at the end: it would outlive destroy."""
    rds = boto3.client("rds", region_name=OUT["region"])
    s3 = boto3.client("s3", region_name=OUT["region"])
    cluster = OUT["cluster_arns"]["target"].rsplit(":", 1)[-1]
    run = start(
        operation="migrate",
        startType=load_type(),
        targetReset={"policy": "always"},
        dump={"enabled": True, "method": "snapshot"},
    )
    d = wait(run, timeout=3600)
    j = jobs(run["runId"])
    try:
        manifest = json.loads(
            s3.get_object(
                Bucket=OUT["artifacts_bucket"], Key=f"runs/ledger/{run['runId']}/jobs/dump/00-snapshot-target/dump.json"
            )["Body"].read()
        )
    except s3.exceptions.NoSuchKey:
        manifest = {}
    name = manifest.get("snapshotIdentifier") or ""
    snaps = rds.describe_db_cluster_snapshots(DBClusterIdentifier=cluster, SnapshotType="manual")["DBClusterSnapshots"]
    snap = next((x for x in snaps if x["DBClusterSnapshotIdentifier"] == name), {})
    tags = {t["Key"]: t["Value"] for t in snap.get("TagList") or []}
    took = None
    if snap.get("SnapshotCreateTime"):
        jt = j.get("snapshot-target") or {}
        if jt.get("finishedAt"):
            took = (datetime.fromisoformat(str(jt["finishedAt"])) - snap["SnapshotCreateTime"]).total_seconds()
    deleted = False
    if name:
        rds.delete_db_cluster_snapshot(DBClusterSnapshotIdentifier=name)
        deleted = True
    c = counts()
    return verdict(
        "S10-snapshot",
        {
            "run succeeded": d["status"] == "SUCCEEDED",
            "snapshot-target ok": (j.get("snapshot-target") or {}).get("status") == "ok",
            "snapshot available": snap.get("Status") == "available",
            "name <project>-<taskKey>-<ts>, no host": name.startswith("ledger-core-") and "rds" not in name,
            "tags project, task, run": tags.get("dms-orch:project") == "ledger"
            and tags.get("dms-orch:task") == "core"
            and tags.get("dms-orch:run") == run["runId"],
            "dump.json recorded": manifest.get("method") == "snapshot" and manifest.get("clusterIdentifier") == cluster,
            "snapshot-retention ran": (j.get("snapshot-retention") or {}).get("status") in ("ok", "warn"),
            "counts match": all(src == tgt for src, tgt in c.values()),
            "lock released": lock() is None,
            "snapshot deleted after the check": deleted,
        },
        snapshot=name,
        manifest=manifest,
        secondsSnapshotToJobEnd=took,
        jobs=j,
    )


def s14() -> bool:
    """Premigration assessment with fail_on_warning: the run stops before the
    DMS start and the report lands in the assessment bucket. Needs the module variable
    assessment (from the upstream premigration_assessment) applied in the sandbox. Records the API facts
    (required task state, service-linked role) for docs/dms-observations.md."""
    before = task().get("ReplicationTaskStats", {}).get("StartDate")
    rows_before = {t: tgt for t, (_src, tgt) in counts().items()}
    run = start(
        operation="migrate",
        startType=load_type(),
        targetReset={"policy": "always"},
        assessment={"enabled": True, "failOnWarning": True},
    )
    d = wait(run, timeout=2400)
    rows_after = {t: tgt for t, (_src, tgt) in counts().items()}
    r = report(d["runId"])
    a = r.get("assessment") or {}
    runs = DMS.describe_replication_task_assessment_runs(
        Filters=[{"Name": "replication-task-arn", "Values": [OUT["task_arn"]]}]
    )["ReplicationTaskAssessmentRuns"]
    iam = boto3.client("iam")
    slr = [x["RoleName"] for x in iam.list_roles(PathPrefix="/aws-service-role/")["Roles"] if "DMS" in x["RoleName"]]
    return verdict(
        "S14",
        {
            "assessment ran": bool(a.get("Status")),
            "warning or failure stops before DMS": d["status"] != "FAILED"
            or task().get("ReplicationTaskStats", {}).get("StartDate") == before,
            # seen live: the reset used to truncate the target before the assessment refused
            "refusal leaves the target untouched": d["status"] != "FAILED" or rows_after == rows_before,
            "lock released": lock() is None,
        },
        assessment=a,
        status=d["status"],
        error=d.get("error"),
        runs=[
            {
                k: x.get(k)
                for k in (
                    "AssessmentRunName",
                    "Status",
                    "LastFailureMessage",
                    "ResultLocationBucket",
                    "ResultLocationFolder",
                )
            }
            for x in runs[:3]
        ],
        serviceLinkedRoles=slr,
        rowsBefore=rows_before,
        rowsAfter=rows_after,
        taskStatusAtStart=task().get("Status"),
    )


def guard() -> bool:
    """Live fix: an armed MIGRATION# of another run (planted, not in the active index, so the
    watchdog leaves it alone) refuses a reload with reset before anything touches the target."""
    before = {t: tgt for t, (_src, tgt) in counts().items()}
    loads_before = task().get("ReplicationTaskStats", {}).get("StartDate")
    fake = f"guard-{secrets.token_hex(3)}"
    key = {"pk": f"MIGRATION#{OUT['task_arn']}", "sk": "ACTIVE"}
    TABLE.put_item(
        Item={**key, "entity": "MIGRATION", "taskArn": OUT["task_arn"], "runId": fake, "status": "armed"},
        ConditionExpression="attribute_not_exists(pk) OR #s = :posted",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":posted": "posted"},
    )
    try:
        run = start(operation="migrate", startType="reload-target", targetReset={"policy": "always"})
        d = wait(run)
    finally:
        TABLE.delete_item(Key=key, ConditionExpression="runId = :r", ExpressionAttributeValues={":r": fake})
    after = {t: tgt for t, (_src, tgt) in counts().items()}
    resets = [i["sk"] for i in timeline(run["runId"]) if str(i.get("sk", "")).startswith(("JOB#reset#", "JOB#dump#"))]
    return verdict(
        "GUARD",
        {
            "refused MigrationAlreadyArmed": d["status"] == "FAILED" and d.get("error") == "MigrationAlreadyArmed",
            "no dump or reset job ran": resets == [],
            "target untouched": after == before,
            "DMS not started": task().get("ReplicationTaskStats", {}).get("StartDate") == loads_before,
            "lock released": lock() is None,
        },
        resets=resets,
        before=before,
        after=after,
    )


def postdie() -> bool:
    """Live fix: a post that dies in LoadRun (no saved context, before TakeOverLock) releases
    its run's lock through release_lock within seconds, not after the watchdog's 300 s grace; a
    lock already handed over to the next run (auto-resume) stays. Planted locks, no DMS."""
    if lock() is not None:
        return verdict("POSTDIE", {"task lock free before the scenario": False})
    rid = f"postdie-{secrets.token_hex(3)}"
    key = {"pk": f"LOCK#{OUT['task_arn']}", "sk": "LOCK"}

    def plant(run_id: str) -> None:
        holder = _execution(OUT["state_machine_arn"], run_id)
        TABLE.put_item(
            Item={
                **key,
                "entity": "LOCK",
                "runId": run_id,
                "executionArn": holder,
                "project": "ledger",
                "taskKey": "core",
                "acquiredAt": datetime.now(UTC).isoformat(),
                "gsi2pk": "LOCK#ACTIVE",
                "gsi2sk": holder,
            },
            ConditionExpression="attribute_not_exists(pk)",
        )

    def die(name: str) -> str:
        body = {"runId": rid, "taskArn": OUT["task_arn"], "outcome": {}, "startedBy": "e2e"}
        arn = SFN.start_execution(stateMachineArn=OUT["post_state_machine_arn"], name=name, input=json.dumps(body))
        d = wait({"runId": name, "arn": arn["executionArn"]}, timeout=300)
        return d["status"]

    def released_within(seconds: int) -> float | None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            if lock() is None:
                return round(time.monotonic() - t0, 1)
            time.sleep(5)
        return None

    plant(f"{rid}-r2")
    try:
        first = die(f"{rid}-post")
        kept = released_within(60) is None and (lock() or {}).get("runId") == f"{rid}-r2"
    finally:
        TABLE.delete_item(Key=key, ConditionExpression="runId = :r", ExpressionAttributeValues={":r": f"{rid}-r2"})
    plant(rid)
    second = die(f"{rid}-post-{datetime.now(UTC):%Y%m%dT%H%M%S}")
    took = released_within(240)
    if took is None:
        TABLE.delete_item(Key=key, ConditionExpression="runId = :r", ExpressionAttributeValues={":r": rid})
    return verdict(
        "POSTDIE",
        {
            "posts failed in LoadRun": first == "FAILED" and second == "FAILED",
            "handed-over lock kept": kept,
            "own lock released by release_lock (< 240 s)": took is not None,
        },
        runId=rid,
        releasedAfterSeconds=took,
    )


def s13() -> bool:
    """Repeat post: a post job fails (job set e2e-postfail), the operator repeats
    post with the fixed job set (e2e) from the next job; jobs already ok are skipped, no DMS rerun."""
    run = start(
        operation="migrate", startType=load_type(), targetReset={"policy": "always"}, jobsVersion="e2e-postfail"
    )
    first = wait(run, timeout=2400)
    loads_before = task().get("ReplicationTaskStats", {}).get("StartDate")
    script = Path(__file__).resolve().parents[3] / "scripts" / "orch.py"
    env = {
        **os.environ,
        "ORCH_STATE_MACHINE_ARN": OUT["state_machine_arn"],
        "ORCH_POST_STATE_MACHINE_ARN": OUT["post_state_machine_arn"],
    }
    argv = [
        sys.executable,
        str(script),
        "post",
        first["runId"],
        "--from-job",
        "drop-control-tables",
        "--jobs-version",
        "e2e",
        "--wait",
    ]
    repeated = subprocess.run(argv, env=env, check=False).returncode
    j = jobs(first["runId"])
    return verdict(
        "S13",
        {
            "first post failed on broken-check": first["status"] == "FAILED"
            and (j.get("broken-check") or {}).get("status") == "fail",
            "repeat succeeded": repeated == 0,
            "drop-control-tables ran": (j.get("drop-control-tables") or {}).get("status") in ("ok", "warn"),
            "DMS not started again": task().get("ReplicationTaskStats", {}).get("StartDate") == loads_before,
            "lock released": lock() is None,
        },
        jobs=j,
    )


def idem() -> bool:
    """StartExecution with a fixed name (the second guard of the handoff, ADR 0005). Uses the
    post machine with a run id that has no saved context: post fails in LoadRun and touches
    nothing. Records what AWS answers for the same name with the same and a different input."""
    name = f"idem-{secrets.token_hex(3)}"
    body_a = json.dumps({"runId": f"{name}-none", "taskArn": OUT["task_arn"], "outcome": {}, "startedBy": "e2e"})
    body_b = json.dumps({"runId": f"{name}-none", "taskArn": OUT["task_arn"], "outcome": {}, "startedBy": "e2e-2"})
    sm = OUT["post_state_machine_arn"]
    first = SFN.start_execution(stateMachineArn=sm, name=name, input=body_a)
    again = SFN.start_execution(stateMachineArn=sm, name=name, input=body_a)
    try:
        SFN.start_execution(stateMachineArn=sm, name=name, input=body_b)
        other = "accepted"
    except SFN.exceptions.ExecutionAlreadyExists as err:
        other = f"ExecutionAlreadyExists: {err}"
    time.sleep(10)
    ended = SFN.describe_execution(executionArn=first["executionArn"])
    try:
        after_end = SFN.start_execution(stateMachineArn=sm, name=name, input=body_a)["executionArn"]
    except SFN.exceptions.ExecutionAlreadyExists as err:
        after_end = f"ExecutionAlreadyExists: {err}"
    return verdict(
        "IDEM",
        {
            "same name and input -> same execution": again["executionArn"] == first["executionArn"],
            "same name, other input -> ExecutionAlreadyExists": other.startswith("ExecutionAlreadyExists"),
            "post without saved context fails in LoadRun": ended["status"] == "FAILED",
        },
        executionArn=first["executionArn"],
        otherInput=other,
        sameInputAfterEnd=after_end,
        endedWith={"status": ended["status"], "error": ended.get("error")},
    )


def until_done(arn: str, timeout: int = 900) -> dict[str, Any]:
    """One execution to its end, without following the chain (unlike wait())."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = _describe(arn)
        if d is not None and d["status"] != "RUNNING":
            return d
        time.sleep(5)
    raise TimeoutError(arn)


def idem_sfn() -> bool:
    """A real repeated states:startExecution from post with the same name and another input
    (otherwise only a TestState mock). No DMS run:

    1. a dry run R saves its context and ends (post released the lock);
    2. a decoy migrate execution named R-r2 (the name post's auto-resume uses) with an invalid
       input fails at once in ResolveContext and touches nothing;
    3. a lock for R is planted and post is started for R with a simulated transient DMS failure
       (source connection timed out): ClassifyFailure allows the auto-resume, HandOverLock gives
       the lock to R-r2, StartResume collides with the decoy.
    Expected: StepFunctions.ExecutionAlreadyExistsException, caught as "already started"
    (WriteReport, status RESUMED), no GiveLockBack, post SUCCEEDED, the decoy untouched. The lock
    handed to R-r2 (whose execution is dead) is removed at the end."""
    if lock() is not None:
        return verdict("IDEM-SFN", {"task lock free before the scenario": False})
    dry = wait(start(operation="migrate", startType=load_type(), targetReset={"policy": "always"}, dryRun=True))
    rid = dry["runId"]
    decoy_name = f"{rid}-r2"
    decoy_input = json.dumps({"e2e": "idem-sfn decoy, not a run request"})
    decoy = SFN.start_execution(stateMachineArn=OUT["state_machine_arn"], name=decoy_name, input=decoy_input)
    decoy_end = wait({"runId": decoy_name, "arn": decoy["executionArn"]}, timeout=300)
    key = {"pk": f"LOCK#{OUT['task_arn']}", "sk": "LOCK"}
    holder = _execution(OUT["state_machine_arn"], rid)
    TABLE.put_item(
        Item={
            **key,
            "entity": "LOCK",
            "runId": rid,
            "executionArn": holder,
            "project": "ledger",
            "taskKey": "core",
            "acquiredAt": datetime.now(UTC).isoformat(),
            "gsi2pk": "LOCK#ACTIVE",
            "gsi2sk": holder,
        },
        ConditionExpression="attribute_not_exists(pk)",
    )
    body = {
        "runId": rid,
        "taskArn": OUT["task_arn"],
        "outcome": {
            "kind": "FAILURE",
            "error": "DmsTaskFailed",
            "cause": "e2e idem-sfn: source endpoint connection timed out (simulated, DMS did not run)",
            "task": {},
        },
        "startedBy": "e2e-idem-sfn",
    }
    name = f"{rid[:60]}-post-idem"
    post = SFN.start_execution(stateMachineArn=OUT["post_state_machine_arn"], name=name, input=json.dumps(body))
    try:
        end = until_done(post["executionArn"], timeout=900)
    finally:
        for run_id in (decoy_name, rid):
            with contextlib.suppress(TABLE.meta.client.exceptions.ConditionalCheckFailedException):
                TABLE.delete_item(Key=key, ConditionExpression="runId = :r", ExpressionAttributeValues={":r": run_id})
    events = SFN.get_execution_history(executionArn=post["executionArn"], maxResults=1000)["events"]
    out = json.loads(end.get("output") or "{}")
    decoy_now = SFN.describe_execution(executionArn=decoy["executionArn"])
    classified = [i.get("detail") for i in timeline(rid) if i.get("phase") == "classify"]
    checks = sandbox.resume_collision_checks(events)
    checks.update(
        {
            "post SUCCEEDED": end["status"] == "SUCCEEDED",
            "post reported RESUMED": out.get("status") == "RESUMED",
            "decoy ended before the collision": decoy_end["status"] == "FAILED",
            "decoy input untouched": decoy_now.get("input") == decoy_input,
            "lock removed": lock() is None,
        }
    )
    failed = [e["taskFailedEventDetails"] for e in events if "taskFailedEventDetails" in e]
    return verdict("IDEM-SFN", checks, runId=rid, post=post["executionArn"], taskFailed=failed, classify=classified)


def s16() -> bool:
    """Fan-out: an SQS queue subscribed to the run topic with the filter policy
    severity = high (examples/sandbox/fanout.tf, variable sqs_fanout) gets the FAILED and the
    MANUAL notification and not the SUCCEEDED one. Three runs: a full load (SUCCEEDED), a post
    job that fails (job set e2e-postfail, FAILED), an extra table in the target (MANUAL)."""
    url = OUT.get("fanout_queue_url")
    if not url:
        return verdict("S16", {"sqs_fanout = true applied": False})
    sqs = boto3.client("sqs", region_name=OUT["region"])
    stale = sandbox.drain_queue(sqs, url, wait_seconds=1, empty_polls=1)
    runs: dict[str, str] = {}
    ok = wait(start(operation="migrate", startType=load_type(), targetReset={"policy": "always"}), timeout=2400)
    runs["SUCCEEDED"] = ok["runId"]
    bad = start(
        operation="migrate", startType=load_type(), targetReset={"policy": "always"}, jobsVersion="e2e-postfail"
    )
    runs["FAILED"] = wait(bad, timeout=2400)["runId"]
    sandbox.data_api(OUT, "target", "CREATE TABLE IF NOT EXISTS app.junk (id int)")
    try:
        stop = wait(start(operation="migrate", startType="reload-target", targetReset={"policy": "always"}))
    finally:
        sandbox.data_api(OUT, "target", "DROP TABLE IF EXISTS app.junk")
    runs["MANUAL"] = stop["runId"]
    statuses = {k: report(v).get("status") for k, v in runs.items()}
    got = sandbox.drain_queue(sqs, url, wait_seconds=20, empty_polls=3)
    checks = {f"run ended {k}": statuses[k] == k for k in runs}
    checks.update(sandbox.fanout_checks(runs, got))
    checks["lock released"] = lock() is None
    return verdict("S16", checks, runs=runs, statuses=statuses, received=got, staleDrained=len(stale))


def main(argv: list[str]) -> int:
    scenarios = {
        "s1": s1,
        "s1p": s1p,
        "s7": s7,
        "s2": s2,
        "s3": s3,
        "s5": s5,
        "s6": s6,
        "idem": idem,
        "s8": s8,
        "s4": s4,
        "s9": s9,
        "s13": s13,
    }
    names = argv or list(scenarios)
    scenarios["s1f"] = s1f  # only by name: needs the Fargate image (sql_runner.tf)
    scenarios["s1iam"] = s1iam  # only by name: needs iam_auth = true, the image, sync-jobs-iam
    scenarios["s15"] = s15  # only by name: replaces the DMS task with terraform apply
    scenarios["s10"] = s10  # only by name: needs the Fargate image (dump and restore)
    scenarios["s10snap"] = s10snap  # only by name: needs snapshot_dumps = true
    scenarios["s8fargate"] = s8fargate  # only by name: needs the Fargate image (verify-target)
    scenarios["s14"] = s14  # only by name: needs the module variable assessment
    scenarios["guard"] = guard  # only by name: plants a MIGRATION# item
    scenarios["postdie"] = postdie  # only by name: plants task locks
    scenarios["idem-sfn"] = idem_sfn  # only by name: plants a lock and a decoy execution
    scenarios["s16"] = s16  # only by name: needs sqs_fanout = true (examples/sandbox/fanout.tf)
    ok = True
    for name in names:
        ok = scenarios[name]() and ok
    return 0 if ok else 1


if __name__ == "__main__":
    os.environ.setdefault("AWS_REGION", OUT["region"])
    _ = subprocess  # keep the import list identical to sandbox.py for packaging
    sys.exit(main(sys.argv[1:]))

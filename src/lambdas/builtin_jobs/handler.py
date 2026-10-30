"""Built-in Lambda jobs, referenced in job sets as ``handler: builtin:<group>/<name>``.

=================================  ==========================================================
checks/connections                 DMS endpoint connections successful; tests the ones that are not
checks/postgresql_source_grants    PostgreSQL source user can be used by DMS (Data API)
checks/row_counts                  row counts match between two connections (Data API)
diagnostics/dms_dump               DMS task, table statistics and recent events -> S3
dump/cluster_snapshot              Aurora cluster snapshot of the target before a reset (dump.method=snapshot)
cleanup/drop_control_tables        drop DMS control tables on the target (Data API)
cleanup/cluster_snapshots          delete this task's snapshots older than the dump retention
=================================  ==========================================================

Input: ``{"spec": {...from prepare_job...}, "attempt": n}``.
Output (job result contract): ``{"status": "ok|warn|fail", "summary": {...}, "message": "...", "artifacts": [...]}``.

SQL jobs use the RDS Data API (no drivers in Lambda); databases without Data API
use the Fargate ``sql-runner`` image instead. Dry run: checks still run (they are
read-only), row counts report a mismatch without failing (DMS did not run), cleanup only
lists what it would drop.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from shared import aws, bundle, dms_outcome, log

LOGGER = log.get_logger("builtin_jobs")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
DMS_CONTROL_TABLES = [
    "awsdms_apply_exceptions",
    "awsdms_status",
    "awsdms_suspended_tables",
    "awsdms_history",
    "awsdms_validation_failures_v1",
]

RESUME_ATTEMPTS = 12  # ~3.5 minutes: Aurora Serverless v2 resumes from 0 ACU in seconds to a minute
# A paused cluster first answers DatabaseResumingException; while its writer comes back the Data API
# can also answer DatabaseUnavailableException (S1p). Both mean "try again".
WAKING_ERRORS = {"DatabaseResumingException", "DatabaseUnavailableException"}


def resume_delay(attempt: int) -> int:
    return min(5 * (attempt + 1), 20)


def resume_budget_seconds() -> int:
    """How long one Data API call waits for a waking cluster before it gives up."""
    return sum(resume_delay(a) for a in range(RESUME_ATTEMPTS))


Job = Callable[[dict[str, Any]], dict[str, Any]]
REGISTRY: dict[str, Job] = {}


def job(name: str) -> Callable[[Job], Job]:
    def register(fn: Job) -> Job:
        REGISTRY[name] = fn
        return fn

    return register


def result(status: str, message: str, **summary: Any) -> dict[str, Any]:
    return {"status": status, "message": message, "summary": summary}


# --- helpers ------------------------------------------------------------------


def connection(spec: dict[str, Any], name: str) -> dict[str, Any]:
    conns = spec.get("connections") or {}
    if name not in conns:
        raise ValueError(f"connection {name!r} is not registered for this task (have: {sorted(conns)})")
    return conns[name]


def quote_ident(name: str) -> str:
    if not IDENTIFIER.match(name):
        raise ValueError(f"unsafe identifier {name!r}")
    return '"' + name + '"'


def quote_table(qualified: str) -> str:
    schema, _, table = qualified.partition(".")
    return f"{quote_ident(schema)}.{quote_ident(table)}" if table else quote_ident(schema)


def artifacts_s3(spec: dict[str, Any]) -> Any:
    """S3 for the job's artifacts, acting as the project's job role.

    The shared lambda-jobs role has no access to ``runs/``; the job role writes only
    ``runs/<its project>/*/jobs/*`` (review finding 1).
    """
    session = f"{spec.get('runId', 'run')}-{spec.get('name', 'job')}"
    return aws.job_client("s3", spec.get("jobRoleArn"), session_name=session)


def bundle_file(spec: dict[str, Any], rel: str) -> bytes:
    """A file of the job set bundle, read as the project job role, checked against the pin.

    The only way a built-in job reads job set files: ``bundle.sha256`` must carry the total the
    run pinned (spec ``bundleSha256``) and list the file with its hash (JobSetTampered).
    """
    prefix_uri = spec.get("jobsS3Prefix")
    if not prefix_uri:
        raise ValueError("the job has no job set bundle (jobsS3Prefix)")
    bucket, _, prefix = str(prefix_uri).removeprefix("s3://").partition("/")
    s3 = artifacts_s3(spec)
    base = prefix.rstrip("/")

    def get(key: str) -> bytes | None:
        try:
            return s3.get_object(Bucket=bucket, Key=f"{base}/{key}")["Body"].read()
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise

    files = bundle.check_manifest(get(bundle.MANIFEST), spec.get("bundleSha256"))
    data = get(bundle.check_path(rel))
    if data is None:
        raise ValueError(f"{rel} is not in the job set bundle")
    return data if files is None else bundle.verify(rel, data, files)


class DataApi:
    """Thin wrapper over rds-data ExecuteStatement returning rows as dicts."""

    def __init__(self, conn: dict[str, Any], client: Any = None):
        if not conn.get("dataApiClusterArn"):
            raise ValueError("this built-in job needs a connection with dataApiClusterArn (Aurora Data API)")
        self.conn = conn
        self.client = client

    @classmethod
    def for_job(cls, spec: dict[str, Any], name: str) -> DataApi:
        """Data API on a registered connection, acting as the project's job role."""
        db = cls(connection(spec, name))
        session = f"{spec.get('runId', 'run')}-{spec.get('name', 'job')}"
        db.client = aws.job_client("rds-data", spec.get("jobRoleArn"), session_name=session)
        return db

    def query(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        parameters = []
        for key, value in params.items():
            if isinstance(value, bool):
                field = {"booleanValue": value}
            elif isinstance(value, int):
                field = {"longValue": value}
            else:
                field = {"stringValue": str(value)}
            parameters.append({"name": key, "value": field})
        for attempt in range(RESUME_ATTEMPTS):
            try:
                resp = self.client.execute_statement(
                    resourceArn=self.conn["dataApiClusterArn"],
                    secretArn=self.conn["secretArn"],
                    database=self.conn.get("database") or "postgres",
                    sql=sql,
                    parameters=parameters,
                    formatRecordsAs="JSON",
                )
                return json.loads(resp.get("formattedRecords") or "[]")
            except ClientError as err:
                # Aurora Serverless v2 with auto-pause: the first call wakes the cluster.
                if err.response.get("Error", {}).get("Code") not in WAKING_ERRORS:
                    raise
                time.sleep(resume_delay(attempt))
        raise RuntimeError(f"cluster {self.conn['dataApiClusterArn']} did not resume from auto-pause")


def task_tables(spec: dict[str, Any]) -> list[str]:
    dms = aws.client("dms")
    tables: list[str] = []
    for page in dms.get_paginator("describe_table_statistics").paginate(ReplicationTaskArn=spec["ctx"]["taskArn"]):
        tables.extend(dms_outcome.table_name(t) for t in page.get("TableStatistics") or [])
    return sorted(set(tables))


# --- jobs -------------------------------------------------------------------------


CONNECTION_POLL_SECONDS = 10


def _connection_rows(dms: Any, arns: list[str], ri: str | None) -> dict[str, dict[str, Any]]:
    filters = [{"Name": "endpoint-arn", "Values": arns}]
    if ri:
        filters.append({"Name": "replication-instance-arn", "Values": [ri]})
    conns = dms.describe_connections(Filters=filters).get("Connections") or []
    return {
        c.get("EndpointArn"): {
            "endpoint": c.get("EndpointIdentifier"),
            "status": c.get("Status"),
            "error": c.get("LastFailureMessage"),
        }
        for c in conns
    }


@job("checks/connections")
def check_connections(spec: dict[str, Any]) -> dict[str, Any]:
    """DMS starts a task only after every endpoint passed TestConnection on its instance
    (docs/dms-observations.md 3); ModifyEndpoint resets that state. Endpoints that are
    untested or not ``successful`` are tested here (``with.test``, default true; not in a
    dry run) and polled until the tests finish (``with.timeoutSeconds``, default 300)."""
    ctx = spec.get("ctx") or {}
    w = spec.get("with") or {}
    arns = ctx.get("endpointArns") or []
    if not arns:
        return result("warn", "no endpoint ARNs registered for this task; nothing to check")
    ri = ctx.get("replicationInstanceArn")
    dms = aws.client("dms")
    rows = _connection_rows(dms, arns, ri)
    to_test = [a for a in arns if (rows.get(a) or {}).get("status") != "successful"]
    tested: list[str] = []
    if to_test and ri and w.get("test", True) and not spec.get("dryRun"):
        for arn in to_test:
            if (rows.get(arn) or {}).get("status") == "testing":
                continue  # a test is already running; wait for it
            try:
                dms.test_connection(ReplicationInstanceArn=ri, EndpointArn=arn)
            except ClientError as err:
                # ModifyEndpoint starts a test by itself: "Connection is already being tested".
                if err.response.get("Error", {}).get("Code") != "InvalidResourceStateFault":
                    raise
            tested.append(arn)
        deadline = time.monotonic() + int(w.get("timeoutSeconds", 300))
        while True:
            rows = _connection_rows(dms, arns, ri)
            if all((rows.get(a) or {}).get("status") != "testing" for a in to_test) or time.monotonic() > deadline:
                break
            time.sleep(CONNECTION_POLL_SECONDS)
    connections = [rows[a] for a in arns if a in rows]
    untested = [a for a in arns if a not in rows]
    bad = [r for r in connections if r["status"] != "successful"]
    extra: dict[str, Any] = {"tested": tested} if tested else {}
    if spec.get("dryRun") and to_test:
        extra["wouldTest"] = to_test
    if bad:
        return result(
            "fail",
            f"{len(bad)} endpoint connection(s) not successful",
            connections=connections,
            untested=untested,
            **extra,
        )
    if untested:
        return result(
            "warn", f"{len(untested)} endpoint(s) never tested", connections=connections, untested=untested, **extra
        )
    return result("ok", "all endpoint connections successful", connections=connections, **extra)


@job("checks/postgresql_source_grants")
def check_postgresql_source_grants(spec: dict[str, Any]) -> dict[str, Any]:
    w = spec.get("with") or {}
    db = DataApi.for_job(spec, w.get("connection", "source"))
    schemas = w.get("schemas") or ["public"]
    for s in schemas:
        quote_ident(s)
    require = {r.upper() for r in w.get("require") or ["SELECT"]}
    info = db.query(
        "SELECT current_user AS usr, current_setting('wal_level') AS wal_level, "
        "current_setting('max_replication_slots')::int AS max_slots, "
        "(SELECT count(*) FROM pg_replication_slots)::int AS used_slots, "
        "r.rolreplication AS replication, r.rolsuper AS superuser, "
        "CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_replication') "
        "THEN pg_has_role(current_user, 'rds_replication', 'member') ELSE false END AS rds_replication "
        "FROM pg_roles r WHERE r.rolname = current_user"
    )[0]
    missing_select = [
        row["tbl"]
        for row in db.query(
            "SELECT n.nspname || '.' || c.relname AS tbl FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY(string_to_array(:schemas, ',')) "
            "AND NOT has_table_privilege(c.oid, 'SELECT') ORDER BY 1",
            schemas=",".join(schemas),
        )
    ]
    problems: list[str] = []
    if "SELECT" in require and missing_select:
        problems.append(f"no SELECT on {len(missing_select)} table(s)")
    if "REPLICATION" in require and not (info["replication"] or info["superuser"] or info["rds_replication"]):
        problems.append(f"user {info['usr']} has neither REPLICATION nor rds_replication")
    wal = w.get("walLevel")
    if wal and info["wal_level"] != wal:
        problems.append(f"wal_level is {info['wal_level']}, expected {wal}")
    free_slots = info["max_slots"] - info["used_slots"]
    if "REPLICATION" in require and free_slots < int(w.get("minFreeSlots", 1)):
        problems.append(f"only {free_slots} free replication slot(s)")
    summary = {**info, "freeSlots": free_slots, "tablesWithoutSelect": missing_select[:200], "schemas": schemas}
    if problems:
        return result("fail", "; ".join(problems), **summary)
    return result("ok", f"user {info['usr']} has the required privileges", **summary)


@job("checks/row_counts")
def check_row_counts(spec: dict[str, Any]) -> dict[str, Any]:
    w = spec.get("with") or {}
    src = DataApi.for_job(spec, w.get("source", "source"))
    tgt = DataApi.for_job(spec, w.get("target", "target"))
    tables = w.get("tables") or task_tables(spec)
    if isinstance(tables, str):
        tables = [t for t in tables.split(",") if t]
    tolerance = int(w.get("tolerance", 0))
    rows = []
    for table in tables:
        sql = f"SELECT count(*)::bigint AS n FROM {quote_table(table)}"  # noqa: S608 - identifiers are validated
        s = src.query(sql)[0]["n"]
        t = tgt.query(sql)[0]["n"]
        rows.append({"table": table, "source": s, "target": t, "diff": t - s})
    mismatched = [r for r in rows if abs(r["diff"]) > tolerance]
    if spec.get("dryRun"):
        # DMS was not started: a mismatch is the expected state, report it without failing.
        msg = f"dry run: {len(mismatched)} table(s) would differ"
        return result("ok", msg, tables=rows[:500], mismatched=[r["table"] for r in mismatched])
    status = "fail" if mismatched else "ok"
    msg = f"{len(mismatched)} of {len(rows)} table(s) differ" if mismatched else f"{len(rows)} table(s) match"
    return result(status, msg, tables=rows[:500], mismatched=[r["table"] for r in mismatched])


@job("diagnostics/dms_dump")
def dms_dump(spec: dict[str, Any]) -> dict[str, Any]:
    ctx = spec.get("ctx") or {}
    dms = aws.client("dms")
    task = dms.describe_replication_tasks(
        Filters=[{"Name": "replication-task-arn", "Values": [ctx["taskArn"]]}], WithoutSettings=True
    )["ReplicationTasks"][0]
    tables = []
    for page in dms.get_paginator("describe_table_statistics").paginate(ReplicationTaskArn=ctx["taskArn"]):
        tables.extend(page.get("TableStatistics") or [])
    events = (
        dms.describe_events(
            SourceIdentifier=task.get("ReplicationTaskIdentifier"), SourceType="replication-task", Duration=1440
        ).get("Events")
        or []
    )
    dump = {
        "collectedAt": datetime.now(UTC).isoformat(),
        "task": dms_outcome.task_summary(task),
        "tablesNotCompleted": [t for t in tables if t.get("TableState") != dms_outcome.COMPLETED_TABLE_STATE],
        "events": events,
    }
    bucket, _, prefix = spec["artifactsS3Prefix"].removeprefix("s3://").partition("/")
    key = f"{prefix}dms-dump.json"
    artifacts_s3(spec).put_object(Bucket=bucket, Key=key, Body=json.dumps(dump, default=str, indent=2).encode())
    return {
        "status": "ok",
        "message": "DMS diagnostics collected",
        "summary": {
            "status": task.get("Status"),
            "stopReason": task.get("StopReason"),
            "lastFailureMessage": task.get("LastFailureMessage"),
            "tablesNotCompleted": len(dump["tablesNotCompleted"]),
            "events": len(events),
        },
        "artifacts": [f"s3://{bucket}/{key}"],
    }


@job("checks/target_verify")
def target_verify(spec: dict[str, Any]) -> dict[str, Any]:
    """Fingerprint of the target in the scope of the task's selection rules (ADR 0006).

    Tables, columns and types, primary key and unique indexes, foreign keys, sequences,
    row estimates and counters (``n_tup_ins/upd/del``), exact ``count(*)`` up to
    ``with.exactCountMaxRows`` (default 1 000 000), DMS control tables. With a source
    connection (``with.source``, default ``source`` when it has the Data API) the source
    catalogue filtered by the rules is the expectation. Read-only, so it runs in dry runs.
    The full JSON goes next to the job result; the summary carries its URI and hash.

    ``with.recordBaseline: {runId, dumpUri}`` (orch.py restore only, phase ``restore``): the
    fingerprint of the restored target becomes ``TARGET#BASELINE`` with ``source=restore`` and
    the ``runId`` of the run that took the dump (seen live: the next run no longer stops as
    ``TargetChanged``; ``orch.py gate`` never promotes on such a baseline).
    """
    from shared import fingerprint

    w = spec.get("with") or {}
    record = w.get("recordBaseline")
    if record is not None:
        # A job set has no restore phase: only orch.py restore starts this phase.
        if spec.get("phase") != "restore":
            return result("fail", "recordBaseline is only accepted in the restore phase (orch.py restore)")
        if not isinstance(record, dict) or not record.get("runId"):
            return result("fail", "recordBaseline needs the runId of the run that took the dump")
    rules = (spec.get("ctx") or {}).get("selectionRules") or w.get("rules") or []
    if not rules:
        return result("fail", "no selection rules for this task: the table mapping cannot scope the check")
    tgt = DataApi.for_job(spec, w.get("connection", "target"))
    max_exact = int(w.get("exactCountMaxRows", 1_000_000))
    source_name = w.get("source", "source")
    source = (spec.get("connections") or {}).get(source_name) if source_name else None
    source_query = None
    if source and source.get("dataApiClusterArn"):
        source_query = DataApi.for_job(spec, source_name).query
    fp, database_bytes = fingerprint.build(tgt.query, rules, max_exact=max_exact, source_query=source_query)
    bucket, _, prefix = spec["artifactsS3Prefix"].removeprefix("s3://").partition("/")
    key = f"{prefix}target-fingerprint.json"
    artifacts_s3(spec).put_object(
        Bucket=bucket, Key=key, Body=json.dumps(fp, indent=2).encode(), ContentType="application/json"
    )
    uri = f"s3://{bucket}/{key}"
    summary: dict[str, Any] = fingerprint.summary(fp, uri, database_bytes)
    if record is not None:
        from shared.store import Store, registration_key

        ctx = spec.get("ctx") or {}
        Store(aws.table()).put_target_baseline(
            registration_key(ctx["project"], ctx["taskKey"]),
            run_id=str(record["runId"]),
            fingerprint_hash=summary["fingerprintHash"],
            fingerprint_uri=uri,
            summary={k: summary[k] for k in ("tables", "rows", "databaseBytes")},
            source="restore",
            extra={"restoreRunId": spec.get("runId"), "dumpUri": record.get("dumpUri")},
        )
        summary["baseline"] = {"source": "restore", "runId": str(record["runId"])}
    return {
        "status": "ok",
        "message": f"{summary['tables']} table(s) in scope, {summary['rows']} row(s)",
        "summary": summary,
        "artifacts": [uri],
    }


SNAPSHOT_POLL_SECONDS = 20
SNAPSHOT_FAILED = {"failed", "incompatible-restore", "incompatible-parameters", "deleted"}
TAG_PROJECT, TAG_TASK, TAG_RUN = "dms-orch:project", "dms-orch:task", "dms-orch:run"


def snapshot_name(project: str, task_key: str, now: datetime) -> str:
    """``<project>-<taskKey>-<YYYYMMDD-HHMMSS>``: what the run is about, never a host, user or secret.
    RDS identifiers: a letter first, then letters, digits and single hyphens."""
    raw = f"{project}-{task_key}-{now:%Y%m%d-%H%M%S}".lower().replace("_", "-")
    name = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]", "-", raw))
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,200}[a-z0-9]", name):
        raise ValueError(f"cannot build a snapshot identifier from project {project!r} and task {task_key!r}")
    return name


def _target_cluster(spec: dict[str, Any], name: str) -> str:
    conn = connection(spec, name)
    arn = conn.get("clusterArn") or conn.get("dataApiClusterArn")
    if not arn:
        raise ValueError(f"connection {name!r} has no clusterArn (or dataApiClusterArn): no cluster to snapshot")
    return str(arn)


def _tags(snapshot: dict[str, Any]) -> dict[str, str]:
    return {t.get("Key"): t.get("Value") for t in snapshot.get("TagList") or []}


def _cluster_snapshots(rds: Any, cluster_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"DBClusterIdentifier": cluster_id, "SnapshotType": "manual"}
    while True:
        page = rds.describe_db_cluster_snapshots(**kwargs)
        out.extend(page.get("DBClusterSnapshots") or [])
        if not page.get("Marker"):
            return out
        kwargs["Marker"] = page["Marker"]


@job("dump/cluster_snapshot")
def cluster_snapshot(spec: dict[str, Any]) -> dict[str, Any]:
    """Aurora cluster snapshot of the target before a reset (``dump.method = snapshot``).

    Faster than pg_dump on big databases; a restore is a new cluster, the database team's
    zone (RUNBOOK). The snapshot carries the project, task and run tags; the job role may
    create, describe and delete only snapshots with the project tag. Idempotent: a retry
    finds the run's snapshot by its tag and goes on polling. One Lambda polls at most
    ``with.waitSeconds``; still ``creating`` then is a failure the job-runner retries.
    When available, ``dump.json`` next to the job result records it (no host, no secret).
    """
    w = spec.get("with") or {}
    ctx = spec.get("ctx") or {}
    run_id = str(spec.get("runId") or "")
    project, task_key = str(ctx.get("project") or ""), str(ctx.get("taskKey") or "")
    cluster_id = _target_cluster(spec, w.get("connection", "target")).rsplit(":", 1)[-1]
    if spec.get("dryRun"):
        name = snapshot_name(project, task_key, datetime.now(UTC))
        return result("ok", f"dry run: would snapshot cluster {cluster_id}", dryRun=True, snapshotIdentifier=name)
    rds = aws.job_client("rds", spec.get("jobRoleArn"), session_name=f"{run_id}-snapshot")
    mine = [s for s in _cluster_snapshots(rds, cluster_id) if _tags(s).get(TAG_RUN) == run_id]
    if mine:
        name = mine[0]["DBClusterSnapshotIdentifier"]
    else:
        name = snapshot_name(project, task_key, datetime.now(UTC))
        rds.create_db_cluster_snapshot(
            DBClusterSnapshotIdentifier=name,
            DBClusterIdentifier=cluster_id,
            Tags=[
                {"Key": TAG_PROJECT, "Value": project},
                {"Key": TAG_TASK, "Value": task_key},
                {"Key": TAG_RUN, "Value": run_id},
                {"Key": "dms-orch:module", "Value": "dms-orchestration"},
            ],
        )
    deadline = time.monotonic() + int(w.get("waitSeconds", 600))
    while True:
        snaps = _cluster_snapshots(rds, cluster_id)
        snap = next((s for s in snaps if s.get("DBClusterSnapshotIdentifier") == name), None)
        status = (snap or {}).get("Status", "missing")
        if snap is not None and status == "available":
            break
        if snap is None or status in SNAPSHOT_FAILED:
            return result("fail", f"snapshot {name} of cluster {cluster_id} is {status}", snapshotIdentifier=name)
        if time.monotonic() >= deadline:
            progress = snap.get("PercentProgress", 0)
            msg = f"snapshot {name} still creating ({progress}%); the job-runner retries"
            return result("fail", msg, snapshotIdentifier=name)
        time.sleep(SNAPSHOT_POLL_SECONDS)

    manifest = {
        "method": "snapshot",
        "status": status,
        "snapshotIdentifier": name,
        "snapshotArn": snap.get("DBClusterSnapshotArn"),
        "clusterIdentifier": cluster_id,
        "engine": snap.get("Engine"),
        "engineVersion": snap.get("EngineVersion"),
        "allocatedStorageGb": snap.get("AllocatedStorage"),
        "createdAt": snap.get("SnapshotCreateTime"),
        "runId": run_id,
        "project": project,
        "taskKey": task_key,
        "restore": "a new cluster from this snapshot, by the database team (RUNBOOK)",
    }
    bucket, _, prefix = spec["artifactsS3Prefix"].removeprefix("s3://").partition("/")
    key = f"{prefix}dump.json"
    artifacts_s3(spec).put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(manifest, indent=2, default=str).encode(),
        ContentType="application/json",
    )
    uri = f"s3://{bucket}/{key}"
    return {
        "status": "ok",
        "message": f"cluster {cluster_id} snapshot {name} available",
        "summary": {"snapshotIdentifier": name, "snapshotArn": manifest["snapshotArn"], "manifestUri": uri},
        "artifacts": [uri],
    }


@job("cleanup/cluster_snapshots")
def cleanup_cluster_snapshots(spec: dict[str, Any]) -> dict[str, Any]:
    """Delete this task's snapshots (project and task tags) older than ``with.retentionDays``
    (``dump_retention_days``). Snapshots without both tags are never touched. Dry run: list only."""
    w = spec.get("with") or {}
    ctx = spec.get("ctx") or {}
    cluster_id = _target_cluster(spec, w.get("connection", "target")).rsplit(":", 1)[-1]
    days = int(w.get("retentionDays", 14))
    cutoff = datetime.now(UTC).timestamp() - days * 86400
    rds = aws.job_client("rds", spec.get("jobRoleArn"), session_name=f"{spec.get('runId', 'run')}-snapshots")

    def expired(s: dict[str, Any]) -> bool:
        tags, created = _tags(s), dms_outcome.as_dt(s.get("SnapshotCreateTime"))
        ours = tags.get(TAG_PROJECT) == ctx.get("project") and tags.get(TAG_TASK) == ctx.get("taskKey")
        return ours and s.get("Status") == "available" and created is not None and created.timestamp() < cutoff

    old = sorted(s["DBClusterSnapshotIdentifier"] for s in _cluster_snapshots(rds, cluster_id) if expired(s))
    if spec.get("dryRun"):
        return result("ok", f"dry run: would delete {len(old)} snapshot(s)", wouldDelete=old, retentionDays=days)
    for name in old:
        rds.delete_db_cluster_snapshot(DBClusterSnapshotIdentifier=name)
    return result("ok", f"deleted {len(old)} snapshot(s) older than {days} day(s)", deleted=old, retentionDays=days)


@job("cleanup/drop_control_tables")
def drop_control_tables(spec: dict[str, Any]) -> dict[str, Any]:
    w = spec.get("with") or {}
    db = DataApi.for_job(spec, w.get("connection", "target"))
    schema = w.get("schema", "dms_control")
    quote_ident(schema)
    wanted = [t for t in DMS_CONTROL_TABLES if not (w.get("keepValidationFailures", True) and "validation" in t)]
    existing = [
        r["table_name"]
        for r in db.query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = :schema "
            "AND table_name = ANY(string_to_array(:tables, ',')) ORDER BY 1",
            schema=schema,
            tables=",".join(wanted),
        )
    ]
    if spec.get("dryRun"):
        return result("ok", f"dry run: would drop {len(existing)} control table(s)", wouldDrop=existing, schema=schema)
    for table in existing:
        db.query(f"DROP TABLE IF EXISTS {quote_ident(schema)}.{quote_ident(table)}")
    return result("ok", f"dropped {len(existing)} control table(s)", dropped=existing, schema=schema)


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    spec = event["spec"]
    name = (spec.get("handler") or "").removeprefix("builtin:")
    fn = REGISTRY.get(name)
    if fn is None:
        return result("fail", f"unknown built-in job {spec.get('handler')!r}; known: {sorted(REGISTRY)}")
    try:
        out = fn(spec)
    except ValueError as err:  # configuration problems are job failures, not Lambda crashes
        out = result("fail", str(err))
    log.info(LOGGER, "built-in job finished", runId=spec.get("runId"), job=spec.get("name"), status=out["status"])
    return out

"""InspectTask: read the DMS task and decide what this run should do.

Input: ``{"runId", "request", "ctx", "attempt", "startedAt"}`` (``startedAt`` is set
after this run called StartReplicationTask; on auto-resume it tells our own restarted
task from someone else's run).

Output::

    {"state": "fresh|interrupted|completed|running|busy|needs-attention",
     "task": {...summary...}, "migrationType": "full-load",
     "targetTablePrepMode": "DO_NOTHING",
     "decision": {"action": "start|wait|reject", "startType", "reset", "resetTables", "reason", "error"},
     "completionSpec": {...}, "connections": [...], "selectionRules": [...], "warnings": [...]}

Pre-flight mode (ADR 0006), after the built-in job ``checks/target_verify``::

    {"mode": "preflight", "runId", "request", "ctx", "inspect", "verify": <job output> | null,
     "previous": <verdict of the previous check in this run> | null,
     "iteration": n}
    -> {"verdict": "ready|prepare|resume|reset|manual|reject", "reason", "error", "diff",
        "decision", "warnings", "fingerprint": {"uri", "hash"} | null, "baseline": {...} | null}

It never changes anything in DMS; ``plan`` and ``dryRun`` rely on that.

``decision`` always carries ``resetTablesCount`` and ``resetTablesUri``. Above
``MAX_TABLES_IN_PAYLOAD`` tables the list goes to ``<run prefix>/inspect/reset-tables.json``
(``resetTablesUri``) and ``resetTables`` is empty in the payload; ``prepare_job`` expands it
into the job's ``spec.json`` only.
"""

from __future__ import annotations

import json
from typing import Any

from shared import aws, dms_outcome, log, preflight
from shared.errors import DmsTaskNotFound, TaskConfigInvalid
from shared.store import Store, registration_key

LOGGER = log.get_logger("inspect_task")
MAX_TABLES_IN_PAYLOAD = 500
RESET_TABLES_FILE = "inspect/reset-tables.json"


def run_prefix(ctx: dict[str, Any], run_id: str) -> str:
    """S3 prefix of the run's artifacts (``ctx`` for a per-project layout)."""
    return f"runs/{run_id}"


def reset_tables_key(prefix: str) -> str:
    """Key of the full reset list under a run prefix (``runs/<runId>`` or ``runs/<project>/<runId>``)."""
    return f"{prefix.rstrip('/')}/{RESET_TABLES_FILE}"


def offload_reset_tables(decision: dict[str, Any], ctx: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Add ``resetTablesCount``/``resetTablesUri``; a list above the payload cap goes to S3."""
    out = dict(decision)
    if out.get("resetTablesUri"):
        return out  # already in S3 (the decision of InspectTask passed through the pre-flight)
    tables = list(out.get("resetTables") or [])
    out["resetTablesCount"] = len(tables)
    out["resetTablesUri"] = None
    if len(tables) <= MAX_TABLES_IN_PAYLOAD:
        return out
    bucket = aws.env("ARTIFACTS_BUCKET")
    key = reset_tables_key(run_prefix(ctx, run_id))
    aws.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps({"resetTables": tables, "count": len(tables)}).encode(),
        ContentType="application/json",
    )
    out["resetTables"] = []
    out["resetTablesUri"] = f"s3://{bucket}/{key}"
    return out


def describe_with_settings(dms: Any, task_arn: str) -> dict[str, Any]:
    resp = dms.describe_replication_tasks(
        Filters=[{"Name": "replication-task-arn", "Values": [task_arn]}], WithoutSettings=False
    )
    tasks = resp.get("ReplicationTasks") or []
    if not tasks:
        raise DmsTaskNotFound(f"replication task {task_arn} not found")
    return tasks[0]


def table_statistics(dms: Any, task_arn: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in dms.get_paginator("describe_table_statistics").paginate(ReplicationTaskArn=task_arn):
        out.extend(page.get("TableStatistics") or [])
    return out


def connection_status(dms: Any, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """Last known endpoint connection tests (read-only; TestConnection is not called)."""
    arns = ctx.get("endpointArns") or []
    if not arns or not ctx.get("replicationInstanceArn"):
        return []
    resp = dms.describe_connections(
        Filters=[
            {"Name": "endpoint-arn", "Values": arns},
            {"Name": "replication-instance-arn", "Values": [ctx["replicationInstanceArn"]]},
        ]
    )
    return [
        {
            "endpointArn": c.get("EndpointArn"),
            "endpointIdentifier": c.get("EndpointIdentifier"),
            "status": c.get("Status"),
            "lastFailureMessage": c.get("LastFailureMessage"),
        }
        for c in resp.get("Connections") or []
    ]


def endpoint_problems(dms: Any, task: dict[str, Any]) -> list[str]:
    """Live defect: MaxFileSize = 0 on a PostgreSQL target makes DMS load 0 rows
    and never finish. The upstream dms module sends it; until the module is fixed, ModifyEndpoint."""
    arn = task.get("TargetEndpointArn")
    if not arn:
        return []
    out = []
    for e in dms.describe_endpoints(Filters=[{"Name": "endpoint-arn", "Values": [arn]}]).get("Endpoints") or []:
        engine = str(e.get("EngineName") or "").lower()
        pg = e.get("PostgreSQLSettings") or {}
        if "postgres" in engine and "MaxFileSize" in pg and int(pg["MaxFileSize"] or 0) == 0:
            out.append(
                f"target endpoint {e.get('EndpointIdentifier') or arn}: PostgreSQLSettings.MaxFileSize = 0 "
                "(DMS loads 0 rows and never finishes); fix the dms module or set MaxFileSize with ModifyEndpoint"
            )
    return out


def inspect(event: dict[str, Any], dms: Any) -> dict[str, Any]:
    req, ctx = event["request"], event["ctx"]
    task = describe_with_settings(dms, ctx["taskArn"])
    settings = json.loads(task.get("ReplicationTaskSettings") or "{}")
    migration_type = task.get("MigrationType") or "full-load"
    state = dms_outcome.classify_task_state(task)
    stats = table_statistics(dms, ctx["taskArn"]) if state in ("interrupted", "completed") else []

    decision = dms_outcome.decide(
        state=state,
        migration_type=migration_type,
        operation=req.get("operation", "migrate"),
        start_type=req.get("startType", "auto"),
        reset_policy=(req.get("targetReset") or {}).get("policy", "if-interrupted"),
        table_stats=stats,
        reset_configured=bool((ctx.get("jobs") or {}).get("reset")),
        attempt=int(event.get("attempt") or 1),
        started_at=event.get("startedAt"),
        task_start_date=(task.get("ReplicationTaskStats") or {}).get("StartDate"),
    )
    spec = dms_outcome.completion_spec(
        migration_type=migration_type,
        settings=settings,
        completion=req.get("completion", "auto"),
        fail_on_table_errors=bool(req.get("failOnTableErrors")),
        operation=req.get("operation", "migrate"),
    )
    prep_mode = (settings.get("FullLoadSettings") or {}).get("TargetTablePrepMode")

    warnings: list[str] = []
    problems: list[str] = []
    if prep_mode and prep_mode != "DO_NOTHING":
        problems.append(
            f"TargetTablePrepMode={prep_mode}: DMS itself drops or truncates target tables; "
            "the orchestrator resets the target (DO_NOTHING expected)"
        )
    full_load = settings.get("FullLoadSettings") or {}
    stops = full_load.get("StopTaskCachedChangesApplied") or full_load.get("StopTaskCachedChangesNotApplied")
    if migration_type != "full-load" and spec.get("mode") == "task-stopped" and not stops:
        problems.append(
            f"{migration_type} task without StopTaskCachedChangesApplied/NotApplied never stops after the full "
            "load, but completion=task-stopped waits for the stop"
        )
    try:
        problems += endpoint_problems(dms, task)
    except Exception as err:  # noqa: BLE001 - diagnostics only
        warnings.append(f"could not read the target endpoint: {err}")
    try:
        connections = connection_status(dms, ctx)
    except Exception as err:  # noqa: BLE001 - diagnostics only
        connections = []
        warnings.append(f"could not read endpoint connections: {err}")
    for conn in connections:
        if conn.get("status") not in (None, "successful"):
            warnings.append(f"endpoint {conn.get('endpointIdentifier')} connection status is {conn.get('status')}")

    try:
        rules = preflight.selection_rules(task.get("TableMappings") or "{}")
    except (ValueError, TypeError) as err:
        rules = []
        problems.append(f"table mapping: {err} (at least one include selection rule)")
    if problems and req.get("operation") != "plan":
        raise TaskConfigInvalid("; ".join(problems))

    decision_dict = offload_reset_tables(decision.to_dict(), ctx, event.get("runId") or "")
    if decision_dict["resetTablesUri"]:
        warnings.append(
            f"{decision_dict['resetTablesCount']} tables to reset; list at {decision_dict['resetTablesUri']}"
        )
    return {
        "configProblems": problems,
        "selectionRules": rules,
        "state": state,
        "task": dms_outcome.task_summary(task),
        "migrationType": migration_type,
        "targetTablePrepMode": prep_mode,
        "decision": decision_dict,
        "completionSpec": spec,
        "connections": connections,
        "tables": [
            {"table": dms_outcome.table_name(t), "state": t.get("TableState"), "fullLoadRows": t.get("FullLoadRows")}
            for t in stats[:MAX_TABLES_IN_PAYLOAD]
        ],
        "warnings": warnings,
    }


def _read_json(uri: str | None) -> dict[str, Any] | None:
    if not uri or not uri.startswith("s3://"):
        return None
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    try:
        return json.loads(aws.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:  # noqa: BLE001 - a missing fingerprint is reported, not fatal
        return None


def _missing_count(previous: dict[str, Any] | None) -> int | None:
    """Missing tables at the previous check of this run (pre-jobs ran in between)."""
    if not previous or not isinstance(previous.get("diff"), dict):
        return None
    return len(previous["diff"].get("missing") or [])


def run_preflight(event: dict[str, Any], store: Store) -> dict[str, Any]:
    """Verdict of ADR 0006 from the target fingerprint, the baseline and our table copy."""
    req, ctx = event["request"], event["ctx"]
    inspected = event.get("inspect") or {}
    verify = event.get("verify") or {}
    task_id = registration_key(ctx["project"], ctx["taskKey"])
    jobs = ctx.get("jobs") or {}
    summary = verify.get("summary") or {}

    current = _read_json(summary.get("fingerprintUri")) if verify.get("status") == "ok" else None
    base_item = store.get_target_baseline(task_id)
    baseline = _read_json(base_item.get("fingerprintUri")) if base_item else None
    v = preflight.verdict(
        state=inspected.get("state", "fresh"),
        decision=inspected.get("decision") or {},
        current=current,
        baseline=baseline,
        table_copy=store.get_table_states(task_id),
        allow_reset=bool(req.get("allowReset")),
        pre_jobs=bool(jobs.get("pre")),
        reset_configured=bool(jobs.get("reset")),
        iteration=int(event.get("iteration") or 0),
        max_iterations=int((req.get("preflight") or {}).get("maxIterations") or 3),
        selection_rules=inspected.get("selectionRules") or None,
        allow_no_fingerprint=bool((req.get("preflight") or {}).get("allowNoFingerprint")),
        previous_missing=_missing_count(event.get("previous")),
        restored_from=base_item.get("runId") if base_item and base_item.get("source") == "restore" else None,
        table_stats=inspected.get("tables") or [],
    )
    out = v.to_dict()
    out["decision"] = offload_reset_tables(out["decision"] or {}, ctx, event.get("runId") or "")
    if verify and verify.get("status") not in (None, "ok"):
        out["warnings"].append(f"target check {verify.get('status')}: {verify.get('message')}")
    if base_item and baseline is None:
        out["warnings"].append(
            f"baseline of run {base_item.get('runId')} not readable at {base_item.get('fingerprintUri')}"
        )
    if base_item and base_item.get("source") == "restore":
        out["warnings"].append(
            f"the baseline is the target restored from a dump of run {base_item.get('runId')} "
            f"({base_item.get('restoreRunId')})"
        )
    out["fingerprint"] = (
        {"uri": summary.get("fingerprintUri"), "hash": preflight.fingerprint_hash(current)} if current else None
    )
    out["baseline"] = (
        {
            k: base_item.get(k)
            for k in ("runId", "source", "restoreRunId", "fingerprintHash", "fingerprintUri", "recordedAt")
        }
        if base_item
        else None
    )
    return out


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    if event.get("mode") == "preflight":
        store = Store(aws.table())
        out = run_preflight(event, store)
        store.append_event(
            event.get("runId"),
            "sfn",
            "preflight",
            {k: out[k] for k in ("verdict", "reason", "error", "diff", "warnings")},
        )
        log.info(LOGGER, "pre-flight verdict", runId=event.get("runId"), verdict=out["verdict"], error=out["error"])
        return out
    result = inspect(event, aws.client("dms"))
    run_id = event.get("runId")
    Store(aws.table()).append_event(
        run_id,
        "sfn",
        "inspect",
        {"attempt": event.get("attempt"), "state": result["state"], "decision": result["decision"]},
    )
    log.info(LOGGER, "task inspected", runId=run_id, state=result["state"], decision=result["decision"])
    return result

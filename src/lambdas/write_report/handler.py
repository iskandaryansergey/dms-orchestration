"""WriteReport: run report to S3 (JSON + Markdown), run status to DynamoDB.

Input (built by the state machine)::

    {"runId", "status": "STARTED|SUCCEEDED|RESUMED|FAILED|PLANNED|REJECTED", "request", "ctx",
     "inspect", "dmsOutcome", "jobs": {"pre": [...], ...}, "failure", "classification",
     "attempt", "started"}

Output::

    {"reportS3Uri", "subject", "message", "status", "notify": bool}

``message`` is a short JSON string for SNS; the full report is in S3.

STARTED is written by migrate after the DMS start (notified only with
``notify.onStart``); RESUMED by post when it chained a new migrate (not notified:
the chained run reports the end).
"""

from __future__ import annotations

import json
from typing import Any

from shared import aws, dms_outcome, log
from shared.store import Store, now_iso, registration_key, run_prefix

LOGGER = log.get_logger("write_report")
TEXT_LIMIT = 300


def _short(value: Any, limit: int = TEXT_LIMIT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


SEVERITY = {"MANUAL": "high", "FAILED": "high", "ABORTED": "high", "TIMED_OUT": "high", "REJECTED": "medium"}


def message_attributes(**values: Any) -> dict[str, dict[str, str]]:
    """SNS MessageAttributes, so subscriptions of the platform topic filter without parsing
    (fan-out). Empty values are left out: SNS rejects them."""
    return {k: {"DataType": "String", "StringValue": str(v)} for k, v in values.items() if v not in (None, "")}


def raw_table_stats(dms: Any, task_arn: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in dms.get_paginator("describe_table_statistics").paginate(ReplicationTaskArn=task_arn):
        out.extend(page.get("TableStatistics") or [])
    return out


def table_row(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "table": dms_outcome.table_name(t),
        "state": t.get("TableState"),
        "fullLoadRows": t.get("FullLoadRows"),
        "fullLoadErrorRows": t.get("FullLoadErrorRows"),
        "inserts": t.get("Inserts"),
        "updates": t.get("Updates"),
        "deletes": t.get("Deletes"),
        "validationState": t.get("ValidationState"),
        "validationFailedRecords": t.get("ValidationFailedRecords"),
    }


def record_baseline(store: Store, report: dict[str, Any], event: dict[str, Any], raw: list[dict[str, Any]]) -> Any:
    """End of post (ADR 0006): the target fingerprint becomes the baseline of the next run, and
    our copy of the table states outlives the DMS task. Not for dry runs: DMS did not run."""
    ctx = event.get("ctx") or {}
    if not ctx.get("project") or not ctx.get("taskKey"):
        return None
    task_id = registration_key(ctx["project"], ctx["taskKey"])
    if (event.get("request") or {}).get("dryRun") or report["status"] == "STARTED":
        return None
    if event.get("started") and raw:
        store.put_table_states(task_id, run_id=report["runId"], tables=raw)
    check = event.get("baseline") or {}
    summary = check.get("summary") or {}
    if check.get("status") != "ok" or not summary.get("fingerprintUri"):
        return None
    store.put_target_baseline(
        task_id,
        run_id=report["runId"],
        fingerprint_hash=summary.get("fingerprintHash") or "",
        fingerprint_uri=summary["fingerprintUri"],
        summary={k: summary[k] for k in ("tables", "rows", "databaseBytes") if summary.get(k) is not None},
    )
    return {"fingerprintHash": summary.get("fingerprintHash"), "fingerprintUri": summary["fingerprintUri"]}


SNAPSHOT_ABOVE_BYTES = 100 * 2**30


def advice(req: dict[str, Any], jobs: list[dict[str, Any]]) -> list[str]:
    """Hints for the next run. A pg_dump of a big target runs for hours on Fargate: above
    ``dump.snapshotAboveBytes`` (pg_database_size from the pre-flight check) suggest a snapshot."""
    out: list[str] = []
    dump = req.get("dump") or {}
    dumped = dump.get("enabled") is True or any(j.get("phase") == "dump" for j in jobs)
    if dump.get("method") == "snapshot" or dump.get("enabled") is False or not dumped:
        return out
    sizes = [
        (j.get("summary") or {}).get("databaseBytes")
        for j in jobs
        if j.get("phase") == "preflight" and isinstance((j.get("summary") or {}).get("databaseBytes"), (int, float))
    ]
    limit = int(dump.get("snapshotAboveBytes", SNAPSHOT_ABOVE_BYTES))
    if sizes and max(sizes) > limit:
        out.append(
            f"target database is {int(max(sizes))} bytes (> {limit}): consider dump.method=snapshot "
            "(a cluster snapshot; a restore is a new cluster, RUNBOOK)"
        )
    return out


def build_report(event: dict[str, Any], store: Store, dms: Any) -> dict[str, Any]:
    run_id = event["runId"]
    req = event.get("request") or {}
    ctx = event.get("ctx") or {}
    inspect = event.get("inspect") or {}
    outcome = event.get("dmsOutcome") or {}
    meta = store.get_run(run_id) or {}
    notes: list[str] = []

    raw: list[dict[str, Any]] = []
    if event.get("started") and ctx.get("taskArn"):
        try:
            raw = raw_table_stats(dms, ctx["taskArn"])
        except Exception as err:  # noqa: BLE001 - a report must still be written
            notes.append(f"table statistics unavailable: {err}")
    tables = [table_row(t) for t in raw]

    events = [
        {"at": e["sk"].split("#")[1], "source": e.get("source"), "phase": e.get("phase"), "detail": e.get("detail")}
        for e in store.run_items(run_id, "EVENT#")
    ]
    jobs = store.job_results(run_id)
    report = {
        "schemaVersion": "1",
        "runId": run_id,
        "status": event.get("status"),
        "outcome": outcome.get("outcome"),
        "dryRun": bool(req.get("dryRun")),
        "operation": req.get("operation"),
        "project": req.get("project"),
        "taskKey": req.get("taskKey"),
        "environment": ctx.get("environment"),
        "taskArn": ctx.get("taskArn"),
        "executionArn": ctx.get("executionArn") or meta.get("executionArn"),
        "executions": {
            "migrate": meta.get("executionArn") or ctx.get("executionArn"),
            "post": meta.get("postExecutionArn") or ctx.get("postExecutionArn"),
        },
        "requestedBy": req.get("requestedBy"),
        "changeRef": req.get("changeRef"),
        "jobsVersion": ctx.get("jobsVersion"),
        "startedAt": meta.get("startedAt"),
        "finishedAt": None if event.get("status") == "STARTED" else now_iso(),
        "attempts": event.get("attempt"),
        "request": req,
        "decision": inspect.get("decision"),
        "taskStateBefore": inspect.get("state"),
        "completionSpec": inspect.get("completionSpec"),
        "connections": inspect.get("connections"),
        "warnings": inspect.get("warnings") or [],
        "dms": outcome.get("dms") or inspect.get("task"),
        "dmsCompletedBy": outcome.get("completedBy"),
        "tables": tables,
        "jobs": [
            {
                "phase": j.get("phase"),
                "name": j.get("name"),
                "status": j.get("status"),
                "attempt": j.get("attempt"),
                "runtime": j.get("runtime"),
                "finishedAt": j.get("finishedAt"),
                "summary": j.get("summary"),
            }
            for j in jobs
        ],
        "failure": event.get("failure"),
        "classification": event.get("classification"),
        "preflight": event.get("preflight"),
        "assessment": event.get("assessment"),
        "baseline": None,
        "advice": advice(req, jobs),
        "_tableStats": raw,
        "timeline": events,
        "notes": notes,
    }
    return report


def _reset_count(decision: dict[str, Any]) -> int:
    """Full count: above the payload cap the list is in S3 and only its count is in the decision."""
    count = decision.get("resetTablesCount")
    return int(count) if count is not None else len(decision.get("resetTables") or [])


def to_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# DMS orchestration run `{report['runId']}`",
        "",
        "| Field | Value |",
        "|---|---|",
    ]
    for label, key in [
        ("Status", "status"),
        ("Outcome", "outcome"),
        ("Operation", "operation"),
        ("Dry run", "dryRun"),
        ("Project / task", None),
        ("Environment", "environment"),
        ("DMS task", "taskArn"),
        ("Started", "startedAt"),
        ("Finished", "finishedAt"),
        ("Attempts", "attempts"),
        ("Requested by", "requestedBy"),
        ("Change ref", "changeRef"),
        ("Jobs version", "jobsVersion"),
    ]:
        value = f"{report.get('project')}/{report.get('taskKey')}" if key is None else report.get(key)
        lines.append(f"| {label} | {'' if value is None else value} |")
    decision = report.get("decision") or {}
    lines += [
        "",
        "## Decision",
        "",
        f"- Task state before run: `{report.get('taskStateBefore')}`",
        f"- Action: `{decision.get('action')}`, start type: `{decision.get('startType')}`",
        f"- Target reset: {decision.get('resetScope', 'none')} ({_reset_count(decision)} tables)",
        f"- Reason: {decision.get('reason', '')}",
    ]
    if report.get("warnings"):
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in report["warnings"]]
    pf = report.get("preflight") or {}
    if pf:
        diff = pf.get("diff") or {}
        code = f" ({pf['error']})" if pf.get("error") else ""
        lines += [
            "",
            "## Pre-flight",
            "",
            f"- Verdict: `{pf.get('verdict')}`{code}",
            f"- Reason: {pf.get('reason', '')}",
        ]
        for key in ("missing", "extra", "changed", "rowsChanged"):
            if diff.get(key):
                lines.append(f"- {key}: {', '.join(diff[key][:50])}")
        lines += [f"- warning: {w}" for w in pf.get("warnings") or []]
    if report.get("advice"):
        lines += ["", "## Advice", ""] + [f"- {a}" for a in report["advice"]]
    if report.get("baseline"):
        lines += ["", f"Baseline recorded: `{report['baseline'].get('fingerprintHash')}`"]
    if report.get("jobs"):
        lines += ["", "## Jobs", "", "| Phase | Job | Status | Attempt |", "|---|---|---|---|"]
        lines += [f"| {j['phase']} | {j['name']} | {j['status']} | {j.get('attempt') or ''} |" for j in report["jobs"]]
    if report.get("tables"):
        lines += ["", "## Tables", "", "| Table | State | Rows | Error rows |", "|---|---|---|---|"]
        lines += [
            f"| {t['table']} | {t['state']} | {t.get('fullLoadRows') or 0} | {t.get('fullLoadErrorRows') or 0} |"
            for t in report["tables"]
        ]
    cls = report.get("classification") or {}
    if report.get("failure") or cls:
        lines += [
            "",
            "## Failure",
            "",
            f"- Category: `{cls.get('category', 'n/a')}` (retryable: {cls.get('retryable', 'n/a')})",
            f"- Reason: {_short(cls.get('reason') or (report.get('failure') or {}).get('Cause', ''), 1000)}",
        ]
        if cls.get("erroredTables"):
            lines.append(f"- Tables in error: {', '.join(cls['erroredTables'])}")
        if cls.get("logTail"):
            lines += ["", "```", *cls["logTail"], "```"]
    lines += ["", "## Timeline", ""]
    lines += [
        f"- {e['at']} `{e['source']}` {e['phase']}: {_short(e.get('detail') or '', 200)}"
        for e in report.get("timeline", [])
    ]
    return "\n".join(lines) + "\n"


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    store = Store(aws.table())
    if event.get("mode") == "baseline":
        # post, before it chains a resume: record the baseline now, the report comes later.
        task_arn = (event.get("ctx") or {}).get("taskArn")
        raw = raw_table_stats(aws.client("dms"), task_arn) if task_arn and event.get("started") else []
        recorded = record_baseline(store, {"runId": event["runId"], "status": event.get("status")}, event, raw)
        store.append_event(event["runId"], "sfn", "baseline", {"recorded": recorded})
        return {"recorded": recorded}
    report = build_report(event, store, aws.client("dms"))
    raw = report.pop("_tableStats")
    try:
        report["baseline"] = record_baseline(store, report, event, raw)
    except Exception as err:  # noqa: BLE001 - the run still reports and unlocks
        report["notes"].append(f"baseline not recorded: {err}")
    bucket = aws.env("ARTIFACTS_BUCKET")
    prefix = run_prefix(report.get("project"), report["runId"])
    s3 = aws.client("s3")
    s3.put_object(
        Bucket=bucket,
        Key=f"{prefix}/report.json",
        Body=json.dumps(report, indent=2, default=str).encode(),
        ContentType="application/json",
    )
    s3.put_object(
        Bucket=bucket, Key=f"{prefix}/report.md", Body=to_markdown(report).encode(), ContentType="text/markdown"
    )
    uri = f"s3://{bucket}/{prefix}/report.json"
    store.update_run(
        report["runId"],
        {
            "status": report["status"],
            "outcome": report.get("outcome"),
            "finishedAt": report["finishedAt"],
            "reportS3Uri": uri,
            "phase": "dms" if report["status"] == "STARTED" else "done",
        },
    )
    store.append_event(report["runId"], "sfn", "report", {"status": report["status"], "reportS3Uri": uri})

    req = event.get("request") or {}
    notify_cfg = req.get("notify") or {}
    failed = report["status"] in ("FAILED", "REJECTED", "MANUAL")
    notify = bool(notify_cfg.get("onFailure", True) if failed else notify_cfg.get("onSuccess", True))
    if report["status"] == "STARTED":
        notify = bool(notify_cfg.get("onStart", False))
    if report["status"] in ("PLANNED", "RESUMED"):
        notify = False
    cls = report.get("classification") or {}
    subject = (
        f"[dms-orch] {report['status']}{' (dry run)' if report['dryRun'] else ''}: "
        f"{report.get('project')}/{report.get('taskKey')} {report['runId']}"
    )
    message = {
        "runId": report["runId"],
        "status": report["status"],
        "outcome": report.get("outcome"),
        "project": report.get("project"),
        "taskKey": report.get("taskKey"),
        "environment": report.get("environment"),
        "failureCategory": cls.get("category"),
        "reason": _short(cls.get("reason") or "", 500) or None,
        "reportS3Uri": uri,
        "executionArn": report.get("executionArn"),
        "severity": SEVERITY.get(report["status"], "low"),
        "verdict": (report.get("preflight") or {}).get("verdict"),
    }
    log.info(LOGGER, "report written", runId=report["runId"], status=report["status"], uri=uri)
    return {
        "reportS3Uri": uri,
        "status": report["status"],
        "subject": subject[:100],
        "message": json.dumps(message),
        "notify": notify,
        "attributes": message_attributes(
            environment=report.get("environment"),
            project=report.get("project"),
            taskKey=report.get("taskKey"),
            status=report["status"],
            phase="dms" if report["status"] == "STARTED" else "done",
            severity=SEVERITY.get(report["status"], "low"),
        ),
    }

"""ClassifyFailure: turn an error into a category and decide on auto-resume.

Input::

    {"runId", "request", "ctx", "attempt", "started": bool,
     "failure": {"Error": "...", "Cause": "..."},
     "completion": {...completionSpec...}, "startedAt": "..."}

Output::

    {"category", "retryable", "autoResume", "recovered", "dmsOutcome", "reason", "error",
     "cause", "lastFailureMessage", "erroredTables": [...], "logTail": [...]}

``WaitTimeout`` (no callback within the heartbeat) is checked against DMS once
more: if the task in fact finished, ``recovered`` is true and ``dmsOutcome`` is
the payload the callback would have carried; the run continues with post-jobs.

Sources, best effort: the Step Functions error, ``LastFailureMessage`` and
``StopReason`` from DMS, tables in error state, and the tail of the task's
CloudWatch log (log group from the upstream DMS module).
"""

from __future__ import annotations

import json
import re
from typing import Any

from shared import aws, dms_outcome, log
from shared.handoff import describe_task, table_statistics
from shared.store import Store

LOGGER = log.get_logger("classify_failure")

RETRYABLE = {"TRANSIENT_SOURCE_CONN", "TRANSIENT_TARGET_CONN", "DMS_RESTART", "THROTTLED"}

# Order matters: first match wins.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Observed in the sandbox: StartReplicationTask on endpoints that never passed
    # TestConnection. A setup step is missing; retrying does not help.
    ("CONFIG", re.compile(r"test connection for replication instance .* should be successful", re.I)),
    (
        "PERMISSIONS",
        re.compile(
            r"permission denied|must be (?:superuser|owner)|access denied|accessdenied|not authori[sz]ed|"
            r"insufficient privilege|password authentication failed|authentication failed|"
            r"no pg_hba\.conf entry|role .* does not exist|replication slot.*permission",
            re.I,
        ),
    ),
    ("RESOURCE", re.compile(r"LOW_MEMORY|LOW_DISK|out of memory|no space left|disk full|storage full", re.I)),
    (
        "TRANSIENT_SOURCE_CONN",
        re.compile(
            r"source.{0,80}(?:connect|connection|timeout|timed out|network|unreachable|reset)|"
            r"(?:connect|connection|timeout|network).{0,80}source",
            re.I,
        ),
    ),
    (
        "TRANSIENT_TARGET_CONN",
        re.compile(
            r"target.{0,80}(?:connect|connection|timeout|timed out|network|unreachable|reset)|"
            r"(?:connect|connection|timeout|network).{0,80}target",
            re.I,
        ),
    ),
    (
        "DATA",
        re.compile(
            r"duplicate key|violates|invalid input syntax|value too long|out of range|"
            r"null value in column|invalid byte sequence|data truncation|conversion failed",
            re.I,
        ),
    ),
    ("THROTTLED", re.compile(r"throttl|rate exceeded|TooManyRequests", re.I)),
    ("CONFIG", re.compile(r"InvalidParameter|InvalidResourceState|ResourceNotFound|ValidationException", re.I)),
]

_ERROR_CATEGORIES = {
    "WaitTimeout": "WAIT_TIMEOUT",
    "States.Timeout": "WAIT_TIMEOUT",
    "States.HeartbeatTimeout": "WAIT_TIMEOUT",
    "JobFailed": "JOB_FAILED",
    "DmsTaskDeleted": "TASK_DELETED",
    "DmsTaskNotFound": "TASK_DELETED",
    "DmsTaskNotStarted": "DMS_START",
    "DmsStartRejected": "DMS_START",
    "InvalidJobSet": "CONFIG",
    "TaskConfigInvalid": "CONFIG",
    "AssessmentFailed": "ASSESSMENT",
    "AssessmentWarning": "ASSESSMENT",
    "AssessmentTimeout": "ASSESSMENT",
    "JobSetNotFound": "CONFIG",
    "JobSetNotPinned": "CONFIG",
    "JobSetTampered": "CONFIG",
    "DmsValidationErrors": "DATA",
}


def _cause_text(failure: dict[str, Any]) -> str:
    cause = failure.get("Cause") or failure.get("cause") or ""
    if isinstance(cause, (dict, list)):
        return json.dumps(cause)
    return str(cause)


def categorize(error: str, text: str, stop_reason: str, errored_tables: int) -> str:
    if error in _ERROR_CATEGORIES:
        return _ERROR_CATEGORIES[error]
    reason = dms_outcome.stop_reason_name(stop_reason)
    if error == "DmsTaskStoppedUnexpectedly":
        if reason in dms_outcome.RESTART_STOP_REASONS:
            return "DMS_RESTART"
        if reason == dms_outcome.NORMAL_STOP_REASON or not reason:
            return "EXTERNAL_STOP"
    for category, pattern in _PATTERNS:
        if pattern.search(text) or pattern.search(reason):
            return category
    # Table errors: DMS keeps the task "finished"; the cause is only in the task log
    # (docs/dms-observations.md). No known pattern there -> data problem.
    if errored_tables or error == "DmsTableErrors":
        return "DATA"
    return "UNKNOWN"


def errored_table_names(dms: Any, task_arn: str) -> list[str]:
    out: list[str] = []
    for page in dms.get_paginator("describe_table_statistics").paginate(
        ReplicationTaskArn=task_arn, Filters=[{"Name": "table-state", "Values": ["Table error"]}]
    ):
        out.extend(dms_outcome.table_name(t) for t in page.get("TableStatistics") or [])
    return out


def log_tail(
    logs: Any, group: str | None, resource_id: str | None, since: Any = None, limit: int = 20, pages: int = 20
) -> list[str]:
    """Error lines of the task log since the task start.

    FilterLogEvents may answer with empty pages and a nextToken (sandbox), so pages
    are followed; without startTime it scans the group from its first event.
    """
    if not group:
        return []
    kwargs: dict[str, Any] = {"logGroupName": group, "limit": 200, "filterPattern": '?"]E:" ?ERROR ?Error ?FATAL'}
    if resource_id:
        kwargs["logStreamNamePrefix"] = f"dms-task-{resource_id}"
    start = dms_outcome.as_dt(since)
    if start:
        kwargs["startTime"] = int(start.timestamp() * 1000) - 60_000
    events: list[dict[str, Any]] = []
    for _ in range(pages):
        page = logs.filter_log_events(**kwargs)
        events.extend(page.get("events") or [])
        if not page.get("nextToken"):
            break
        kwargs["nextToken"] = page["nextToken"]
    return [e.get("message", "")[:500] for e in events[-limit:]]


def classify(event: dict[str, Any], dms: Any, logs: Any) -> dict[str, Any]:
    failure = event.get("failure") or {}
    ctx = event.get("ctx") or {}
    req = event.get("request") or {}
    error = str(failure.get("Error") or failure.get("error") or "Unknown")
    cause = _cause_text(failure)
    started = bool(event.get("started"))
    attempt = int(event.get("attempt") or 1)
    notes: list[str] = []

    task: dict[str, Any] | None = None
    tables: list[str] = []
    tail: list[str] = []
    if ctx.get("taskArn") and started:
        try:
            task = describe_task(dms, ctx["taskArn"])
            if task and int((task.get("ReplicationTaskStats") or {}).get("TablesErrored") or 0):
                tables = errored_table_names(dms, ctx["taskArn"])
        except Exception as err:  # noqa: BLE001 - diagnostics are best effort
            notes.append(f"describe failed: {err}")
        try:
            since = ((task or {}).get("ReplicationTaskStats") or {}).get("StartDate")
            tail = log_tail(logs, ctx.get("logGroupName"), ctx.get("taskResourceId"), since=since)
        except Exception as err:  # noqa: BLE001
            notes.append(f"log tail failed: {err}")

    recovered: dict[str, Any] | None = None
    if error == "WaitTimeout" and task is not None:
        outcome = dms_outcome.evaluate_outcome(
            task,
            event.get("completion") or {},
            started_at=event.get("startedAt"),
            table_stats=lambda: table_statistics(dms, ctx["taskArn"]),
        )
        if outcome.kind == "SUCCESS":
            recovered = {**outcome.payload(), "completedBy": "classify-failure"}
            notes.append(f"wait timed out but DMS reports {outcome.outcome}; continuing")

    last_failure = (task or {}).get("LastFailureMessage") or ""
    stop_reason = (task or {}).get("StopReason") or ""
    text = "\n".join([cause, last_failure, *tail])
    category = categorize(error, text, stop_reason, len(tables))
    retryable = category in RETRYABLE

    auto = req.get("autoResume") or {}
    max_attempts = int(auto.get("maxAttempts") or 0)
    allowed = set(auto.get("onlyCategories") or [])
    auto_resume = bool(started and retryable and category in allowed and attempt - 1 < max_attempts)
    if started and retryable and not auto_resume:
        if category not in allowed:
            notes.append(f"category {category} is not in autoResume.onlyCategories")
        else:
            notes.append(f"auto-resume limit reached ({max_attempts})")

    return {
        "category": category,
        "retryable": retryable,
        "autoResume": auto_resume and recovered is None,
        "recovered": recovered is not None,
        "dmsOutcome": recovered,
        "error": error,
        "cause": cause[:4000],
        "reason": f"{error}: {(last_failure or cause)[:300]}",
        "lastFailureMessage": last_failure,
        "stopReason": stop_reason,
        "erroredTables": tables[:100],
        "logTail": tail,
        "attempt": attempt,
        "notes": notes,
    }


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    result = classify(event, aws.client("dms"), aws.client("logs"))
    Store(aws.table()).append_event(
        event.get("runId"),
        "sfn",
        "classify",
        {k: result[k] for k in ("category", "retryable", "autoResume", "recovered", "reason", "attempt")},
    )
    log.info(
        LOGGER,
        "failure classified",
        runId=event.get("runId"),
        category=result["category"],
        autoResume=result["autoResume"],
    )
    return result

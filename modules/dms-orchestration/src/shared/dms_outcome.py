"""Pure decision logic for DMS replication tasks.

Nothing in this module talks to AWS. It turns the output of
``DescribeReplicationTasks`` / ``DescribeTableStatistics`` into decisions:

* :func:`classify_task_state` — what state is the task in before a run
  (fresh / interrupted / completed / running / busy / needs-attention);
* :func:`decide` — what the run should do (start type, target reset, or reject);
* :func:`evaluate_outcome` — while waiting: success, failure or still pending.

The same functions are used by ``inspect_task``, ``dms_event_handler`` and ``watchdog``, so the
behaviour is identical whoever looks at the task.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

# --- DMS vocabulary ---------------------------------------------------------

RUNNING_STATUSES = {"starting", "running", "stopping"}
BUSY_STATUSES = {"creating", "modifying", "moving", "testing", "deleting"}
NEEDS_ATTENTION_STATUSES = {"failed-move"}

# StopReason values are reported as "Stop Reason <NAME>"; match by name.
COMPLETED_STOP_REASONS = {"FULL_LOAD_ONLY_FINISHED"}
CHECKPOINT_STOP_REASONS = {
    "STOPPED_AFTER_FULL_LOAD",
    "STOPPED_AFTER_CACHED_EVENTS",
    "STOPPED_AFTER_DDL_APPLY",
    "STOPPED_AT_SERVER_TIME",
    "STOPPED_AT_COMMIT_TIME",
}
ERROR_STOP_REASONS = {
    "RECOVERABLE_ERROR",
    "FATAL_ERROR",
    "STOPPED_DUE_TO_LOW_MEMORY",
    "STOPPED_DUE_TO_LOW_DISK",
    "EXPRESS_LICENSE_LIMITS_REACHED",
}
RESTART_STOP_REASONS = {"RECONFIGURATION_RESTART", "RECYCLE_TASK"}
NORMAL_STOP_REASON = "NORMAL"

# TableState values from DescribeTableStatistics that mean "rows may be partial".
PARTIAL_TABLE_STATES = {
    "Before load",
    "Full load",
    "Table cancelled",
    "Table error",
    "Table is being reloaded",
}
COMPLETED_TABLE_STATE = "Table completed"

START_TYPES = ("start-replication", "resume-processing", "reload-target")

# ValidationState values from DescribeTableStatistics. "Not enabled": the task (or the table)
# does not validate, nothing to judge. While CDC keeps running, records wait for validation
# all the time: the in-progress states are not a problem in full-load-complete mode.
VALIDATED_STATE = "Validated"
VALIDATION_OFF_STATES = {"Not enabled", ""}
VALIDATION_IN_PROGRESS_STATES = {"Pending records", "Pending validation", "Pending revalidation", "Preparing table"}
# DMS cannot validate a table without a primary key or unique index and reports "No primary key"
# (seen as "No primary Key" in the sandbox; compared case-insensitively). That is not a mismatch:
# the run does not fail on it, the table is listed as not validated (validationSkippedTables).
# "Table error" and the other states stay problems.
VALIDATION_SKIPPED_STATES = {"no primary key"}
VALIDATION_LISTED_TABLES = 10


_STOP_REASON = re.compile(r"stop reason\s+([A-Z_]+)", re.I)


def stop_reason_name(stop_reason: str | None) -> str:
    """Reason name from the StopReason text.

    Seen in the sandbox (docs/dms-observations.md): "Stop Reason FULL_LOAD_ONLY_FINISHED"
    and "Last Error The task stopped abnormally Stop Reason FULL_LOAD_ONLY_FINISHED Error Level NO".
    """
    if not stop_reason:
        return ""
    m = _STOP_REASON.search(stop_reason)
    return (m.group(1) if m else stop_reason.strip()).upper()


def _stats(task: dict[str, Any]) -> dict[str, Any]:
    return task.get("ReplicationTaskStats") or {}


def as_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(value: Any) -> str | None:
    dt = as_dt(value)
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def task_summary(task: dict[str, Any]) -> dict[str, Any]:
    """Small, JSON-safe view of a replication task for payloads and reports."""
    stats = _stats(task)
    return {
        "status": task.get("Status"),
        "stopReason": task.get("StopReason"),
        "migrationType": task.get("MigrationType"),
        "lastFailureMessage": task.get("LastFailureMessage"),
        "recoveryCheckpoint": task.get("RecoveryCheckpoint"),
        "stats": {
            "fullLoadProgressPercent": stats.get("FullLoadProgressPercent"),
            "tablesLoaded": stats.get("TablesLoaded"),
            "tablesLoading": stats.get("TablesLoading"),
            "tablesQueued": stats.get("TablesQueued"),
            "tablesErrored": stats.get("TablesErrored"),
            "freshStartDate": iso(stats.get("FreshStartDate")),
            "startDate": iso(stats.get("StartDate")),
            "stopDate": iso(stats.get("StopDate")),
            "fullLoadStartDate": iso(stats.get("FullLoadStartDate")),
            "fullLoadFinishDate": iso(stats.get("FullLoadFinishDate")),
        },
    }


# --- Pre-run inspection -----------------------------------------------------


def classify_task_state(task: dict[str, Any]) -> str:
    """Return one of: fresh, running, busy, needs-attention, completed, interrupted."""
    status = (task.get("Status") or "").lower()
    stats = _stats(task)
    reason = stop_reason_name(task.get("StopReason"))
    ever_started = bool(stats.get("StartDate") or stats.get("FreshStartDate"))

    if status in RUNNING_STATUSES:
        return "running"
    if status in BUSY_STATUSES:
        return "busy"
    if status in NEEDS_ATTENTION_STATUSES:
        return "needs-attention"
    if status == "ready":
        return "interrupted" if ever_started else "fresh"
    if status == "stopped":
        errored = int(stats.get("TablesErrored") or 0)
        unfinished = int(stats.get("TablesLoading") or 0) + int(stats.get("TablesQueued") or 0)
        finished_reason = reason in COMPLETED_STOP_REASONS or reason in CHECKPOINT_STOP_REASONS
        if finished_reason and not errored and not unfinished:
            return "completed"
        return "interrupted"
    if status == "failed":
        return "interrupted"
    return "busy"


def table_name(stat: dict[str, Any]) -> str:
    return f"{stat.get('SchemaName', '')}.{stat.get('TableName', '')}"


def partial_tables(table_stats: list[dict[str, Any]] | None) -> list[str]:
    """Tables whose rows may be incomplete after an interrupted load."""
    return sorted(table_name(t) for t in (table_stats or []) if t.get("TableState") in PARTIAL_TABLE_STATES)


def all_tables(table_stats: list[dict[str, Any]] | None) -> list[str]:
    return sorted(table_name(t) for t in (table_stats or []) if t.get("TableState") != "Table does not exist")


@dataclass
class Decision:
    action: str  # "start" | "wait" | "reject"
    start_type: str | None = None
    reset: bool = False
    reset_scope: str = "none"  # none | partial | all
    reset_tables: list[str] = field(default_factory=list)
    reason: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "startType": self.start_type,
            "reset": self.reset,
            "resetScope": self.reset_scope,
            "resetTables": self.reset_tables,
            "reason": self.reason,
            "error": self.error,
        }


def _reject(error: str, reason: str) -> Decision:
    return Decision(action="reject", error=error, reason=reason)


def decide(
    *,
    state: str,
    migration_type: str,
    operation: str = "migrate",
    start_type: str = "auto",
    reset_policy: str = "if-interrupted",
    table_stats: list[dict[str, Any]] | None = None,
    reset_configured: bool = True,
    attempt: int = 1,
    started_at: Any = None,
    task_start_date: Any = None,
) -> Decision:
    """Pick the start type and whether the target must be reset first.

    Duplicate-row safety under ``TargetTablePrepMode = DO_NOTHING`` (the only mode
    the upstream DMS module allows by default):

    * ``resume-processing`` keeps completed tables and reloads unfinished ones, so
      only *partial* tables are truncated first;
    * ``reload-target`` reloads *every* table, so *all* tables are truncated first.

    On an auto-resume attempt (``attempt > 1``) a running task whose ``StartDate``
    is not older than this run's ``started_at`` is our own run that DMS restarted
    (or that never really stopped): the decision is ``wait``, not a new start.
    """
    if state == "running" and attempt > 1:
        ours_since, task_since = as_dt(started_at), as_dt(task_start_date)
        if ours_since and task_since and task_since >= ours_since:
            return Decision(action="wait", reason="the task runs again since this run started it; waiting again")
    if state == "running":
        return _reject("TaskAlreadyRunning", "the DMS task is already running outside this run")
    if state == "busy":
        return _reject("TaskBusy", "the DMS task is being created, modified, moved, tested or deleted")
    if state == "needs-attention":
        return _reject("TaskNeedsAttention", "the DMS task is in failed-move; fix it manually")

    loads_data = migration_type != "cdc"

    if start_type == "auto":
        if state == "fresh":
            chosen = "start-replication"
        elif state == "completed":
            if operation == "resume" and migration_type in ("full-load-and-cdc", "cdc"):
                chosen = "resume-processing"
            else:
                return _reject(
                    "AlreadyCompleted",
                    "the task already completed its load; use startType=reload-target (with targetReset) to load again",
                )
        else:  # interrupted
            # resume-processing leaves "Table error" tables in error (sandbox,
            # docs/dms-observations.md): they are only loaded again by reload-target.
            errored = any(t.get("TableState") == "Table error" for t in table_stats or [])
            reload = reset_policy == "always" or errored
            chosen = "reload-target" if reload and loads_data else "resume-processing"
    else:
        chosen = start_type
        if chosen not in START_TYPES:
            return _reject("InvalidStartType", f"unknown startType {chosen!r}")
        if state == "fresh" and chosen != "start-replication":
            return _reject("InvalidStartType", f"a task that never ran can only use start-replication, not {chosen}")
        if state != "fresh" and chosen == "start-replication":
            return _reject(
                "InvalidStartType",
                "start-replication is only valid for the first run; use resume-processing or reload-target",
            )
        if state == "completed" and chosen == "resume-processing" and migration_type == "full-load":
            return _reject("AlreadyCompleted", "the full load already completed; nothing to resume")

    scope = "none"
    tables: list[str] = []
    if loads_data and state != "fresh":
        if chosen == "reload-target":
            if reset_policy == "never":
                return _reject(
                    "ResetRequired",
                    "reload-target reloads every table; with TargetTablePrepMode=DO_NOTHING the target must be "
                    "reset first, but targetReset.policy=never forbids it",
                )
            scope, tables = "all", all_tables(table_stats)
        elif chosen == "resume-processing" and state == "interrupted" and reset_policy != "never":
            tables = partial_tables(table_stats)
            scope = "partial" if tables else "none"
    reset = bool(tables)
    if reset and not reset_configured:
        return _reject(
            "ResetNotConfigured",
            f"{len(tables)} table(s) must be reset before {chosen} but the job set has no 'reset' phase",
        )

    reasons = {
        "fresh": "first run of the task",
        "completed": "task completed earlier",
        "interrupted": "previous run was interrupted",
    }
    reason = f"{reasons.get(state, state)}; {chosen}"
    if reset:
        reason += f" after resetting {len(tables)} {scope} table(s)"
    return Decision(
        action="start", start_type=chosen, reset=reset, reset_scope=scope, reset_tables=tables, reason=reason
    )


def completion_spec(
    *,
    migration_type: str,
    settings: dict[str, Any] | None,
    completion: str = "auto",
    fail_on_table_errors: bool = False,
    fail_on_validation_errors: bool = True,
    operation: str = "migrate",
) -> dict[str, Any]:
    """What counts as 'done' for the wait step.

    ``auto``: full-load, or full-load-and-cdc with StopTaskCachedChanges* ->
    the task must stop (``task-stopped``); otherwise the full load must finish
    while CDC keeps running (``full-load-complete``).
    """
    settings = settings or {}
    full_load = settings.get("FullLoadSettings") or {}
    stops_after_full_load = bool(
        full_load.get("StopTaskCachedChangesApplied") or full_load.get("StopTaskCachedChangesNotApplied")
    )
    mode = completion
    if completion == "auto" and operation == "resume" and migration_type != "full-load":
        # Resuming CDC after a checkpoint stop: done when the task runs again with the full load finished.
        mode = "full-load-complete"
    elif completion == "auto":
        mode = "task-stopped" if migration_type == "full-load" or stops_after_full_load else "full-load-complete"
    return {
        "mode": mode,
        "migrationType": migration_type,
        "failOnTableErrors": bool(fail_on_table_errors),
        "failOnValidationErrors": bool(fail_on_validation_errors),
        "expectStop": False,
    }


# --- Waiting: outcome ---------------------------------------------------------


def validation_problems(table_stats: list[dict[str, Any]] | None, *, cdc_running: bool = False) -> list[str]:
    """``<schema>.<table>: <what>`` for every table DMS did not load or validate cleanly.

    A problem is a ``ValidationState`` other than ``Validated`` (tables that do not validate,
    ``Not enabled``, are skipped), ``ValidationFailedRecords > 0`` or ``FullLoadErrorRows > 0``.
    """
    out: list[str] = []
    for t in table_stats or []:
        if t.get("TableState") == "Table does not exist":
            continue
        what: list[str] = []
        state = str(t.get("ValidationState") or "")
        tolerated = (cdc_running and state in VALIDATION_IN_PROGRESS_STATES) or _skipped(t)
        if state != VALIDATED_STATE and state not in VALIDATION_OFF_STATES and not tolerated:
            what.append(f"validation state {state}")
        failed = int(t.get("ValidationFailedRecords") or 0)
        if failed:
            what.append(f"{failed} record(s) failed validation")
        error_rows = int(t.get("FullLoadErrorRows") or 0)
        if error_rows:
            what.append(f"{error_rows} full-load error row(s)")
        if what:
            out.append(f"{table_name(t)}: {', '.join(what)}")
    return sorted(out)


def _skipped(stat: dict[str, Any]) -> bool:
    return str(stat.get("ValidationState") or "").strip().lower() in VALIDATION_SKIPPED_STATES


def validation_skipped(table_stats: list[dict[str, Any]] | None) -> list[str]:
    """Tables DMS loaded but cannot validate (no primary key): reported, never a failure."""
    return sorted(
        table_name(t) for t in table_stats or [] if t.get("TableState") != "Table does not exist" and _skipped(t)
    )


def _listed(names: list[str]) -> str:
    more = len(names) - VALIDATION_LISTED_TABLES
    return "; ".join(names[:VALIDATION_LISTED_TABLES]) + (f"; and {more} more" if more > 0 else "")


def skipped_text(skipped: list[str]) -> str:
    return f"{len(skipped)} table(s) not validated by DMS (no primary key): {_listed(skipped)}"


def _problem_text(problems: list[str]) -> str:
    shown = "; ".join(problems[:VALIDATION_LISTED_TABLES])
    more = len(problems) - VALIDATION_LISTED_TABLES
    return f"{len(problems)} table(s) not validated: {shown}" + (f"; and {more} more" if more > 0 else "")


@dataclass
class Outcome:
    kind: str  # SUCCESS | FAILURE | PENDING
    outcome: str | None = None  # COMPLETED, COMPLETED_WITH_TABLE_ERRORS, COMPLETED_WITH_VALIDATION_ERRORS, ...
    error: str | None = None  # DmsTaskFailed, DmsTaskStoppedUnexpectedly, ...
    cause: str = ""
    task: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "cause": self.cause, "dms": self.task}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


NOT_STARTED_GRACE_SECONDS = 900


def evaluate_outcome(
    task: dict[str, Any] | None,
    spec: dict[str, Any],
    *,
    started_at: Any = None,
    now: Any = None,
    start_error: Any = None,
    table_stats: list[dict[str, Any]] | Callable[[], list[dict[str, Any]]] | None = None,
) -> Outcome:
    """Decide whether the wait is over.

    ``started_at`` is the time just before ``StartReplicationTask``. A terminal
    status whose ``StopDate`` is older than that belongs to the previous run and is
    ignored: the task has not picked up the new start request yet.

    ``start_error`` is set when ``StartReplicationTask`` answered
    ``InvalidResourceStateException``. That is expected after a retried start whose
    first call went through (the task is starting or running); if instead the task
    has not moved since ``started_at``, the start was really refused and the wait
    fails at once with the DMS text instead of after the not-started grace period.

    ``table_stats`` (``DescribeTableStatistics`` rows, or a function returning them, called
    only when the load finished) gates the outcome on DMS validation: with
    ``failOnValidationErrors`` (absent = on) a table not validated, with failed validation
    records or full-load error rows fails ``DmsValidationErrors``; off, the run succeeds as
    ``COMPLETED_WITH_VALIDATION_ERRORS``. A table DMS cannot validate (``No primary key``) is no
    problem either way: the cause lists it as not validated. ``None``: validation is not looked at.
    """
    if task is None:
        return Outcome("FAILURE", error="DmsTaskDeleted", cause="replication task not found")

    summary = task_summary(task)
    status = (task.get("Status") or "").lower()
    reason = stop_reason_name(task.get("StopReason"))
    stats = _stats(task)
    errored = int(stats.get("TablesErrored") or 0)
    mode = spec.get("mode", "task-stopped")
    started = as_dt(started_at)
    now_dt = as_dt(now) or datetime.now(UTC)

    def success(outcome: str, cause: str) -> Outcome:
        if errored and spec.get("failOnTableErrors"):
            return Outcome(
                "FAILURE", error="DmsTableErrors", cause=f"{errored} table(s) in error state; {cause}", task=summary
            )
        rows = table_stats() if callable(table_stats) else table_stats
        problems = validation_problems(rows, cdc_running=status == "running") if rows is not None else []
        skipped = validation_skipped(rows)
        if skipped:
            cause = f"{cause}; {skipped_text(skipped)}"
        if problems and spec.get("failOnValidationErrors", True):
            return Outcome("FAILURE", error="DmsValidationErrors", cause=_problem_text(problems), task=summary)
        if errored:
            note = f"; {_problem_text(problems)}" if problems else ""
            return Outcome(
                "SUCCESS", "COMPLETED_WITH_TABLE_ERRORS", cause=f"{errored} table(s) errored{note}", task=summary
            )
        if problems:
            return Outcome("SUCCESS", "COMPLETED_WITH_VALIDATION_ERRORS", cause=_problem_text(problems), task=summary)
        return Outcome("SUCCESS", outcome, cause=cause, task=summary)

    if status == "deleting":
        return Outcome("FAILURE", error="DmsTaskDeleted", cause="replication task is being deleted", task=summary)

    stop_date = as_dt(stats.get("StopDate"))
    if start_error and status not in RUNNING_STATUSES and not (started and stop_date and stop_date >= started):
        text = start_error.get("Cause") or start_error.get("Error") if isinstance(start_error, dict) else start_error
        return Outcome(
            "FAILURE",
            error="DmsStartRejected",
            cause=f"StartReplicationTask was refused and the task is {status or 'unknown'}: {text}",
            task=summary,
        )

    if status in ("stopped", "failed", "failed-move") and started and stop_date and stop_date < started:
        # A StartDate after the start request means the task did pick it up: the StopDate is the
        # previous run's because Describe has not caught up with this stop yet (seen live with the
        # STOPPED event of a 22-minute load). Wait for it instead of failing DmsTaskNotStarted.
        start_date = as_dt(stats.get("StartDate") or stats.get("FreshStartDate"))
        if start_date and start_date >= started:
            return Outcome("PENDING", cause=f"task is {status}; DMS has not updated StopDate yet", task=summary)
        return _pending_or_not_started(summary, started, now_dt)

    if status in ("failed", "failed-move"):
        msg = task.get("LastFailureMessage") or task.get("StopReason") or status
        return Outcome("FAILURE", error="DmsTaskFailed", cause=str(msg), task=summary)

    if status == "stopped":
        if reason in COMPLETED_STOP_REASONS:
            return success("COMPLETED", "full load finished")
        if reason in CHECKPOINT_STOP_REASONS:
            return success("STOPPED_AT_CHECKPOINT", f"task stopped at checkpoint ({reason})")
        if reason == NORMAL_STOP_REASON:
            if spec.get("expectStop"):
                return Outcome("SUCCESS", "STOPPED", cause="stopped as requested", task=summary)
            return Outcome(
                "FAILURE",
                error="DmsTaskStoppedUnexpectedly",
                cause="task was stopped outside the orchestrator (Stop Reason NORMAL)",
                task=summary,
            )
        if reason in ERROR_STOP_REASONS:
            msg = task.get("LastFailureMessage") or reason
            return Outcome("FAILURE", error="DmsTaskFailed", cause=f"{reason}: {msg}", task=summary)
        if reason in RESTART_STOP_REASONS:
            return Outcome(
                "FAILURE", error="DmsTaskStoppedUnexpectedly", cause=f"task stopped by DMS ({reason})", task=summary
            )
        return Outcome(
            "FAILURE",
            error="DmsTaskStoppedUnexpectedly",
            cause=f"task stopped with unexpected reason {task.get('StopReason')!r}",
            task=summary,
        )

    if status == "running" and mode == "full-load-complete":
        unfinished = int(stats.get("TablesLoading") or 0) + int(stats.get("TablesQueued") or 0)
        progress = int(stats.get("FullLoadProgressPercent") or 0)
        if stats.get("FullLoadFinishDate") and unfinished == 0 and progress >= 100:
            return success("FULL_LOAD_COMPLETE_CDC_RUNNING", "full load finished; CDC keeps running")

    if status == "ready" and started:
        return _pending_or_not_started(summary, started, now_dt)

    return Outcome("PENDING", cause=f"task is {status}", task=summary)


def _pending_or_not_started(summary: dict[str, Any], started: datetime, now: datetime) -> Outcome:
    if (now - started).total_seconds() > NOT_STARTED_GRACE_SECONDS:
        return Outcome(
            "FAILURE",
            error="DmsTaskNotStarted",
            cause=f"task did not start within {NOT_STARTED_GRACE_SECONDS}s after StartReplicationTask",
            task=summary,
        )
    return Outcome("PENDING", cause="waiting for the task to pick up the start request", task=summary)

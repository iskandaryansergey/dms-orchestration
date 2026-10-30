"""Decision tables for DMS task state, start type, reset and wait outcome."""

from __future__ import annotations

import pytest
from conftest import dms_task

from shared import dms_outcome as d

STARTED = "2026-10-27T10:00:00Z"
AFTER = "2026-10-27T11:00:00Z"
BEFORE = "2026-10-27T09:00:00Z"
FULL_LOAD = {"mode": "task-stopped", "migrationType": "full-load", "failOnTableErrors": False}


# --- stop reason parsing ------------------------------------------------------


@pytest.mark.parametrize(
    "raw,name",
    [
        ("Stop Reason FULL_LOAD_ONLY_FINISHED", "FULL_LOAD_ONLY_FINISHED"),
        ("stop reason normal", "NORMAL"),
        ("RECOVERABLE_ERROR", "RECOVERABLE_ERROR"),
        (None, ""),
    ],
)
def test_stop_reason_name(raw, name):
    assert d.stop_reason_name(raw) == name


# --- classify_task_state ------------------------------------------------------


@pytest.mark.parametrize(
    "task,state",
    [
        (dms_task("ready"), "fresh"),
        (dms_task("ready", StartDate=BEFORE), "interrupted"),
        (dms_task("starting"), "running"),
        (dms_task("running"), "running"),
        (dms_task("stopping"), "running"),
        (dms_task("modifying"), "busy"),
        (dms_task("testing"), "busy"),
        (dms_task("creating"), "busy"),
        (dms_task("failed-move"), "needs-attention"),
        (dms_task("stopped", "Stop Reason FULL_LOAD_ONLY_FINISHED", StartDate=BEFORE), "completed"),
        (dms_task("stopped", "Stop Reason STOPPED_AFTER_FULL_LOAD", StartDate=BEFORE), "completed"),
        (dms_task("stopped", "Stop Reason FULL_LOAD_ONLY_FINISHED", TablesErrored=1), "interrupted"),
        (dms_task("stopped", "Stop Reason NORMAL", StartDate=BEFORE), "interrupted"),
        (dms_task("stopped", "Stop Reason RECOVERABLE_ERROR"), "interrupted"),
        (dms_task("stopped", "Stop Reason STOPPED_AFTER_FULL_LOAD", TablesQueued=2), "interrupted"),
        (dms_task("failed", "Stop Reason FATAL_ERROR"), "interrupted"),
        (dms_task("something-new"), "busy"),
    ],
)
def test_classify_task_state(task, state):
    assert d.classify_task_state(task) == state


# --- decide -------------------------------------------------------------------

TABLES = [
    {"SchemaName": "public", "TableName": "a", "TableState": "Table completed"},
    {"SchemaName": "public", "TableName": "b", "TableState": "Full load"},
    {"SchemaName": "public", "TableName": "c", "TableState": "Table error"},
    {"SchemaName": "public", "TableName": "d", "TableState": "Table does not exist"},
]
# Interrupted mid-load without errored tables (errored tables force reload-target).
PARTIAL = [t for t in TABLES if t["TableState"] != "Table error"]


@pytest.mark.parametrize(
    "state,error",
    [("running", "TaskAlreadyRunning"), ("busy", "TaskBusy"), ("needs-attention", "TaskNeedsAttention")],
)
def test_decide_rejects_unavailable_task(state, error):
    dec = d.decide(state=state, migration_type="full-load")
    assert dec.action == "reject" and dec.error == error


def test_decide_fresh_auto_is_start_replication_without_reset():
    dec = d.decide(state="fresh", migration_type="full-load", table_stats=TABLES)
    assert (dec.action, dec.start_type, dec.reset) == ("start", "start-replication", False)


def test_decide_completed_migrate_is_rejected():
    dec = d.decide(state="completed", migration_type="full-load")
    assert dec.error == "AlreadyCompleted"


def test_decide_completed_resume_cdc_is_resume_processing():
    dec = d.decide(state="completed", migration_type="full-load-and-cdc", operation="resume")
    assert dec.start_type == "resume-processing" and not dec.reset


def test_decide_completed_resume_full_load_is_rejected():
    assert d.decide(state="completed", migration_type="full-load", operation="resume").error == "AlreadyCompleted"


def test_decide_interrupted_if_interrupted_truncates_only_partial_tables():
    dec = d.decide(state="interrupted", migration_type="full-load", reset_policy="if-interrupted", table_stats=PARTIAL)
    assert dec.start_type == "resume-processing"
    assert dec.reset and dec.reset_scope == "partial"
    assert dec.reset_tables == ["public.b"]


def test_decide_interrupted_without_partial_tables_needs_no_reset():
    tables = [{"SchemaName": "public", "TableName": "a", "TableState": "Table completed"}]
    dec = d.decide(state="interrupted", migration_type="full-load-and-cdc", table_stats=tables)
    assert dec.start_type == "resume-processing" and not dec.reset


def test_decide_interrupted_never_resumes_without_reset():
    dec = d.decide(state="interrupted", migration_type="full-load", reset_policy="never", table_stats=PARTIAL)
    assert dec.start_type == "resume-processing" and not dec.reset


def test_decide_interrupted_always_reloads_after_truncating_all_tables():
    dec = d.decide(state="interrupted", migration_type="full-load", reset_policy="always", table_stats=TABLES)
    assert dec.start_type == "reload-target"
    assert dec.reset_scope == "all"
    assert dec.reset_tables == ["public.a", "public.b", "public.c"]


def test_decide_explicit_reload_target_resets_all_tables_even_when_completed():
    dec = d.decide(state="completed", migration_type="full-load", start_type="reload-target", table_stats=TABLES)
    assert dec.action == "start" and dec.start_type == "reload-target" and dec.reset_scope == "all"


def test_decide_reload_target_with_policy_never_is_rejected():
    dec = d.decide(state="completed", migration_type="full-load", start_type="reload-target", reset_policy="never")
    assert dec.error == "ResetRequired"


def test_decide_cdc_only_never_resets():
    dec = d.decide(state="interrupted", migration_type="cdc", reset_policy="always", table_stats=TABLES)
    assert dec.start_type == "resume-processing" and not dec.reset


@pytest.mark.parametrize(
    "state,start_type",
    [("fresh", "resume-processing"), ("fresh", "reload-target"), ("interrupted", "start-replication"), ("x", "bogus")],
)
def test_decide_invalid_explicit_start_type(state, start_type):
    assert d.decide(state=state, migration_type="full-load", start_type=start_type).error == "InvalidStartType"


def test_decide_reset_needed_but_not_configured_is_rejected():
    dec = d.decide(state="interrupted", migration_type="full-load", table_stats=TABLES, reset_configured=False)
    assert dec.error == "ResetNotConfigured"


# --- completion_spec ------------------------------------------------------------


@pytest.mark.parametrize(
    "mtype,settings,mode",
    [
        ("full-load", {}, "task-stopped"),
        ("full-load-and-cdc", {"FullLoadSettings": {"StopTaskCachedChangesApplied": True}}, "task-stopped"),
        ("full-load-and-cdc", {"FullLoadSettings": {"StopTaskCachedChangesNotApplied": True}}, "task-stopped"),
        ("full-load-and-cdc", {"FullLoadSettings": {}}, "full-load-complete"),
        ("cdc", None, "full-load-complete"),
    ],
)
def test_completion_spec_auto(mtype, settings, mode):
    assert d.completion_spec(migration_type=mtype, settings=settings)["mode"] == mode


def test_completion_spec_explicit_mode_wins():
    assert d.completion_spec(migration_type="full-load", settings={}, completion="full-load-complete")["mode"] == (
        "full-load-complete"
    )


# --- evaluate_outcome -----------------------------------------------------------


@pytest.mark.parametrize(
    "task,kind,outcome,error",
    [
        (dms_task("running"), "PENDING", None, None),
        (dms_task("starting"), "PENDING", None, None),
        (dms_task("stopped", "Stop Reason FULL_LOAD_ONLY_FINISHED", StopDate=AFTER), "SUCCESS", "COMPLETED", None),
        (
            dms_task("stopped", "Stop Reason STOPPED_AFTER_FULL_LOAD", StopDate=AFTER),
            "SUCCESS",
            "STOPPED_AT_CHECKPOINT",
            None,
        ),
        (
            dms_task("stopped", "Stop Reason STOPPED_AFTER_CACHED_EVENTS", StopDate=AFTER),
            "SUCCESS",
            "STOPPED_AT_CHECKPOINT",
            None,
        ),
        (
            dms_task("stopped", "Stop Reason STOPPED_AT_SERVER_TIME", StopDate=AFTER),
            "SUCCESS",
            "STOPPED_AT_CHECKPOINT",
            None,
        ),
        (
            dms_task("stopped", "Stop Reason NORMAL", StopDate=AFTER),
            "FAILURE",
            None,
            "DmsTaskStoppedUnexpectedly",
        ),
        (dms_task("stopped", "Stop Reason RECOVERABLE_ERROR", StopDate=AFTER), "FAILURE", None, "DmsTaskFailed"),
        (dms_task("stopped", "Stop Reason FATAL_ERROR", StopDate=AFTER), "FAILURE", None, "DmsTaskFailed"),
        (
            dms_task("stopped", "Stop Reason STOPPED_DUE_TO_LOW_MEMORY", StopDate=AFTER),
            "FAILURE",
            None,
            "DmsTaskFailed",
        ),
        (dms_task("stopped", "Stop Reason STOPPED_DUE_TO_LOW_DISK", StopDate=AFTER), "FAILURE", None, "DmsTaskFailed"),
        (
            dms_task("stopped", "Stop Reason EXPRESS_LICENSE_LIMITS_REACHED", StopDate=AFTER),
            "FAILURE",
            None,
            "DmsTaskFailed",
        ),
        (
            dms_task("stopped", "Stop Reason RECONFIGURATION_RESTART", StopDate=AFTER),
            "FAILURE",
            None,
            "DmsTaskStoppedUnexpectedly",
        ),
        (
            dms_task("stopped", "Stop Reason RECYCLE_TASK", StopDate=AFTER),
            "FAILURE",
            None,
            "DmsTaskStoppedUnexpectedly",
        ),
        (
            dms_task("stopped", "Stop Reason SOMETHING_NEW", StopDate=AFTER),
            "FAILURE",
            None,
            "DmsTaskStoppedUnexpectedly",
        ),
        (dms_task("failed", last_failure="boom", StopDate=AFTER), "FAILURE", None, "DmsTaskFailed"),
        (dms_task("deleting"), "FAILURE", None, "DmsTaskDeleted"),
        (None, "FAILURE", None, "DmsTaskDeleted"),
    ],
)
def test_evaluate_outcome_table(task, kind, outcome, error):
    res = d.evaluate_outcome(task, FULL_LOAD, started_at=STARTED, now=AFTER)
    assert (res.kind, res.outcome, res.error) == (kind, outcome, error)


def test_failure_cause_carries_last_failure_message():
    task = dms_task("failed", last_failure="password authentication failed", StopDate=AFTER)
    res = d.evaluate_outcome(task, FULL_LOAD, started_at=STARTED, now=AFTER)
    assert "password authentication failed" in res.cause


def test_stale_stopped_state_from_previous_run_is_pending():
    task = dms_task("stopped", "Stop Reason NORMAL", StopDate=BEFORE)
    res = d.evaluate_outcome(task, FULL_LOAD, started_at=STARTED, now="2026-10-27T10:01:00Z")
    assert res.kind == "PENDING"


def test_task_that_never_picks_up_start_fails_after_grace():
    task = dms_task("ready")
    res = d.evaluate_outcome(task, FULL_LOAD, started_at=STARTED, now="2026-10-27T10:20:00Z")
    assert (res.kind, res.error) == ("FAILURE", "DmsTaskNotStarted")


def test_table_errors_succeed_with_warning_by_default():
    task = dms_task("stopped", "Stop Reason FULL_LOAD_ONLY_FINISHED", StopDate=AFTER, TablesErrored=2)
    res = d.evaluate_outcome(task, FULL_LOAD, started_at=STARTED, now=AFTER)
    assert (res.kind, res.outcome) == ("SUCCESS", "COMPLETED_WITH_TABLE_ERRORS")


def test_table_errors_fail_when_policy_says_so():
    task = dms_task("stopped", "Stop Reason FULL_LOAD_ONLY_FINISHED", StopDate=AFTER, TablesErrored=2)
    res = d.evaluate_outcome(task, {**FULL_LOAD, "failOnTableErrors": True}, started_at=STARTED, now=AFTER)
    assert (res.kind, res.error) == ("FAILURE", "DmsTableErrors")


def test_expected_stop_is_success():
    task = dms_task("stopped", "Stop Reason NORMAL", StopDate=AFTER)
    res = d.evaluate_outcome(task, {**FULL_LOAD, "expectStop": True}, started_at=STARTED, now=AFTER)
    assert (res.kind, res.outcome) == ("SUCCESS", "STOPPED")


def test_full_load_complete_mode_succeeds_while_cdc_runs():
    spec = {"mode": "full-load-complete", "migrationType": "full-load-and-cdc"}
    running = dms_task(
        "running", FullLoadProgressPercent=100, TablesLoading=0, TablesQueued=0, FullLoadFinishDate=AFTER
    )
    res = d.evaluate_outcome(running, spec, started_at=STARTED, now=AFTER)
    assert (res.kind, res.outcome) == ("SUCCESS", "FULL_LOAD_COMPLETE_CDC_RUNNING")


def test_full_load_complete_mode_waits_while_tables_load():
    spec = {"mode": "full-load-complete", "migrationType": "full-load-and-cdc"}
    running = dms_task("running", FullLoadProgressPercent=60, TablesLoading=2)
    assert d.evaluate_outcome(running, spec, started_at=STARTED, now=AFTER).kind == "PENDING"


def test_task_summary_is_json_safe():
    import json
    from datetime import UTC, datetime

    task = dms_task("running", StartDate=datetime(2026, 10, 27, tzinfo=UTC))
    assert json.loads(json.dumps(d.task_summary(task)))["stats"]["startDate"] == "2026-10-27T00:00:00Z"


def test_completion_spec_resume_of_cdc_waits_for_cdc_running_not_for_a_stop():
    settings = {"FullLoadSettings": {"StopTaskCachedChangesApplied": True}}
    spec = d.completion_spec(migration_type="full-load-and-cdc", settings=settings, operation="resume")
    assert spec["mode"] == "full-load-complete"
    assert d.completion_spec(migration_type="full-load", settings={}, operation="resume")["mode"] == "task-stopped"


# --- auto-resume finds the task running again (review finding 1) ------------------


def test_decide_auto_resume_waits_when_dms_restarted_the_task_after_our_start():
    dec = d.decide(state="running", migration_type="full-load", attempt=2, started_at=STARTED, task_start_date=AFTER)
    assert dec.action == "wait" and dec.start_type is None and dec.reset is False


def test_decide_auto_resume_rejects_a_run_started_before_ours():
    dec = d.decide(state="running", migration_type="full-load", attempt=2, started_at=STARTED, task_start_date=BEFORE)
    assert (dec.action, dec.error) == ("reject", "TaskAlreadyRunning")


def test_decide_first_attempt_still_rejects_a_running_task():
    dec = d.decide(state="running", migration_type="full-load", attempt=1, started_at=STARTED, task_start_date=AFTER)
    assert (dec.action, dec.error) == ("reject", "TaskAlreadyRunning")


# --- start rejected by DMS (review finding 3) --------------------------------------

START_FAULT = "Replication task is not in a valid state to be started: status=ready, type=resume-processing"


def test_rejected_start_fails_at_once_with_the_dms_text_when_the_task_did_not_move():
    task = dms_task("ready", StopDate=BEFORE)
    out = d.evaluate_outcome(task, FULL_LOAD, started_at=STARTED, now=STARTED, start_error=START_FAULT)
    assert (out.kind, out.error) == ("FAILURE", "DmsStartRejected")
    assert START_FAULT in out.cause


def test_rejected_start_keeps_waiting_when_the_task_runs_after_all():
    for task in (dms_task("starting"), dms_task("running"), dms_task("stopping")):
        out = d.evaluate_outcome(task, FULL_LOAD, started_at=STARTED, now=STARTED, start_error=START_FAULT)
        assert out.kind == "PENDING"


def test_rejected_start_judges_a_run_that_already_finished_after_our_start_normally():
    task = dms_task("stopped", "Stop Reason FULL_LOAD_ONLY_FINISHED", StopDate=AFTER)
    out = d.evaluate_outcome(task, FULL_LOAD, started_at=STARTED, now=AFTER, start_error=START_FAULT)
    assert (out.kind, out.outcome) == ("SUCCESS", "COMPLETED")


# --- sandbox finding: StopReason may carry a "Last Error ..." prefix and suffix ----------

OBSERVED_LONG = "Last Error The task stopped abnormally Stop Reason FULL_LOAD_ONLY_FINISHED Error Level NO"


def test_stop_reason_name_finds_the_reason_inside_the_long_form():
    assert d.stop_reason_name(OBSERVED_LONG) == "FULL_LOAD_ONLY_FINISHED"
    assert d.stop_reason_name("Stop Reason RECOVERABLE_ERROR Error Level RECOVERABLE") == "RECOVERABLE_ERROR"


def test_full_load_finished_in_long_form_with_all_tables_loaded_is_completed():
    task = dms_task("stopped", OBSERVED_LONG, StopDate=AFTER, TablesLoaded=4)
    out = d.evaluate_outcome(task, FULL_LOAD, started_at=STARTED, now=AFTER)
    assert (out.kind, out.outcome) == ("SUCCESS", "COMPLETED")
    assert d.classify_task_state(dms_task("stopped", OBSERVED_LONG, StartDate=BEFORE, TablesLoaded=4)) == "completed"


# --- sandbox finding: resume-processing leaves "Table error" tables in error --------

ERRORED = [
    {"SchemaName": "app", "TableName": "customers", "TableState": "Table completed"},
    {"SchemaName": "app", "TableName": "transactions", "TableState": "Table error"},
]


def test_errored_tables_need_reload_target_with_full_reset():
    dec = d.decide(state="interrupted", migration_type="full-load", table_stats=ERRORED)
    assert (dec.action, dec.start_type, dec.reset_scope) == ("start", "reload-target", "all")
    assert dec.reset_tables == ["app.customers", "app.transactions"]


def test_errored_tables_with_reset_policy_never_are_rejected():
    dec = d.decide(state="interrupted", migration_type="full-load", table_stats=ERRORED, reset_policy="never")
    assert (dec.action, dec.error) == ("reject", "ResetRequired")


# --- review finding 7: DMS validation gates the run ----------------------------------

FINISHED = dms_task("stopped", "Stop Reason FULL_LOAD_ONLY_FINISHED", StopDate=AFTER, TablesLoaded=2)


def vstat(table: str, state: str = "Validated", failed: int = 0, error_rows: int = 0) -> dict:
    return {
        "SchemaName": "app",
        "TableName": table,
        "TableState": "Table completed",
        "ValidationState": state,
        "ValidationFailedRecords": failed,
        "FullLoadErrorRows": error_rows,
    }


@pytest.mark.parametrize(
    "stats,needle",
    [
        ([vstat("a"), vstat("b", state="Mismatched records")], "app.b: validation state Mismatched records"),
        ([vstat("a"), vstat("b", state="Pending records")], "app.b: validation state Pending records"),
        ([vstat("a"), vstat("b", failed=3)], "app.b: 3 record(s) failed validation"),
        ([vstat("a"), vstat("b", error_rows=2)], "app.b: 2 full-load error row(s)"),
    ],
)
def test_validation_problems_fail_the_run_when_on(stats, needle):
    spec = {**FULL_LOAD, "failOnValidationErrors": True}
    res = d.evaluate_outcome(FINISHED, spec, started_at=STARTED, now=AFTER, table_stats=stats)
    assert (res.kind, res.error) == ("FAILURE", "DmsValidationErrors")
    assert needle in res.cause and "1 table(s)" in res.cause


def test_validation_problems_succeed_with_warning_when_off():
    spec = {**FULL_LOAD, "failOnValidationErrors": False}
    stats = [vstat("a"), vstat("b", failed=3)]
    res = d.evaluate_outcome(FINISHED, spec, started_at=STARTED, now=AFTER, table_stats=stats)
    assert (res.kind, res.outcome) == ("SUCCESS", "COMPLETED_WITH_VALIDATION_ERRORS")
    assert "app.b" in res.cause


def test_validation_is_on_when_the_spec_does_not_say():
    res = d.evaluate_outcome(FINISHED, FULL_LOAD, started_at=STARTED, now=AFTER, table_stats=[vstat("a", failed=1)])
    assert (res.kind, res.error) == ("FAILURE", "DmsValidationErrors")


def test_validated_or_not_enabled_tables_complete():
    stats = [
        vstat("a"),
        vstat("b", state="Not enabled"),
        {"SchemaName": "app", "TableName": "gone", "TableState": "Table does not exist", "ValidationState": "Error"},
    ]
    res = d.evaluate_outcome(FINISHED, FULL_LOAD, started_at=STARTED, now=AFTER, table_stats=stats)
    assert (res.kind, res.outcome) == ("SUCCESS", "COMPLETED")


def test_table_stats_are_read_only_for_a_finished_load():
    calls: list[int] = []

    def stats() -> list[dict]:
        calls.append(1)
        return [vstat("a", failed=1)]

    running = dms_task("running")
    assert d.evaluate_outcome(running, FULL_LOAD, started_at=STARTED, now=AFTER, table_stats=stats).kind == "PENDING"
    assert calls == []
    res = d.evaluate_outcome(FINISHED, FULL_LOAD, started_at=STARTED, now=AFTER, table_stats=stats)
    assert res.error == "DmsValidationErrors" and calls == [1]


def test_cdc_running_tolerates_pending_validation_but_not_failures():
    spec = {"mode": "full-load-complete", "migrationType": "full-load-and-cdc", "failOnValidationErrors": True}
    running = dms_task(
        "running", FullLoadProgressPercent=100, TablesLoading=0, TablesQueued=0, FullLoadFinishDate=AFTER
    )
    pending = [vstat("a", state="Pending records"), vstat("b", state="Pending validation")]
    res = d.evaluate_outcome(running, spec, started_at=STARTED, now=AFTER, table_stats=pending)
    assert (res.kind, res.outcome) == ("SUCCESS", "FULL_LOAD_COMPLETE_CDC_RUNNING")
    res = d.evaluate_outcome(running, spec, started_at=STARTED, now=AFTER, table_stats=[vstat("a", failed=4)])
    assert (res.kind, res.error) == ("FAILURE", "DmsValidationErrors")


def test_table_errors_still_fail_first():
    task = dms_task("stopped", "Stop Reason FULL_LOAD_ONLY_FINISHED", StopDate=AFTER, TablesErrored=1)
    spec = {**FULL_LOAD, "failOnTableErrors": True}
    res = d.evaluate_outcome(task, spec, started_at=STARTED, now=AFTER, table_stats=[vstat("a", failed=1)])
    assert (res.kind, res.error) == ("FAILURE", "DmsTableErrors")


def test_completion_spec_carries_fail_on_validation_errors_default_true():
    assert d.completion_spec(migration_type="full-load", settings={})["failOnValidationErrors"] is True
    spec = d.completion_spec(migration_type="full-load", settings={}, fail_on_validation_errors=False)
    assert spec["failOnValidationErrors"] is False


def test_armed_migration_keeps_every_field_of_the_completion_spec():
    """The Arm putItem lists the spec fields one by one; the event path reads only those."""
    from asl_runner import compile_expr, iter_expressions, render

    spec_keys = set(d.completion_spec(migration_type="full-load", settings={}))
    found: dict[str, str] = {}
    for path, text in iter_expressions(render("migrate.asl.json")):
        if ".Item.completionSpec.M." in path:
            found[path.split(".Item.completionSpec.M.")[1].split(".")[0]] = text
    assert set(found) == spec_keys
    expr = compile_expr(found["failOnValidationErrors"])
    assert expr.evaluate(None, {"inspect": {"completionSpec": {}}}) is True  # absent: on
    assert expr.evaluate(None, {"inspect": {"completionSpec": {"failOnValidationErrors": False}}}) is False


def test_resumed_run_keeps_the_request_flag():
    from pathlib import Path

    post = (Path(__file__).resolve().parents[2] / "modules/dms-orchestration/asl/post.asl.json").read_text("utf-8")
    assert "'failOnTableErrors', 'failOnValidationErrors'," in post


# --- tables DMS cannot validate (no primary key) are reported, not failed ----------------


@pytest.mark.parametrize("state", ["No primary key", "No primary Key"])
def test_a_table_without_primary_key_does_not_fail_the_run(state):
    stats = [vstat("a"), vstat("notes", state=state)]
    res = d.evaluate_outcome(FINISHED, FULL_LOAD, started_at=STARTED, now=AFTER, table_stats=stats)
    assert (res.kind, res.outcome) == ("SUCCESS", "COMPLETED")
    assert "app.notes" in res.cause and "no primary key" in res.cause.lower()


def test_validation_skipped_lists_pk_less_tables_only():
    stats = [vstat("a"), vstat("n1", state="No primary key"), vstat("e", state="Table error")]
    assert d.validation_skipped(stats) == ["app.n1"]
    assert d.validation_problems(stats) == ["app.e: validation state Table error"]


def test_a_pk_less_table_with_failed_records_or_error_rows_still_fails():
    stats = [vstat("notes", state="No primary key", error_rows=2)]
    res = d.evaluate_outcome(FINISHED, FULL_LOAD, started_at=STARTED, now=AFTER, table_stats=stats)
    assert (res.kind, res.error) == ("FAILURE", "DmsValidationErrors")


def test_table_error_validation_state_still_fails():
    stats = [vstat("a", state="Table error")]
    res = d.evaluate_outcome(FINISHED, FULL_LOAD, started_at=STARTED, now=AFTER, table_stats=stats)
    assert (res.kind, res.error) == ("FAILURE", "DmsValidationErrors")


def test_the_skipped_note_is_capped():
    stats = [vstat(f"t{i:02d}", state="No primary key") for i in range(d.VALIDATION_LISTED_TABLES + 3)]
    res = d.evaluate_outcome(FINISHED, FULL_LOAD, started_at=STARTED, now=AFTER, table_stats=stats)
    assert res.kind == "SUCCESS" and "and 3 more" in res.cause

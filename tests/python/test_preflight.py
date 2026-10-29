"""Pre-flight verdicts (ADR 0006): one test per row of the table, plus the diff and the scope rules."""

from __future__ import annotations

import copy
import json

import pytest

from shared import preflight

MAPPING = json.dumps(
    {
        "rules": [
            {
                "rule-type": "selection",
                "rule-id": "1",
                "rule-name": "app",
                "object-locator": {"schema-name": "app", "table-name": "%"},
                "rule-action": "include",
            },
            {
                "rule-type": "selection",
                "rule-id": "2",
                "rule-name": "no-tmp",
                "object-locator": {"schema-name": "app", "table-name": "tmp_%"},
                "rule-action": "exclude",
            },
            {
                "rule-type": "transformation",
                "rule-id": "3",
                "rule-name": "lower",
                "rule-target": "schema",
                "object-locator": {"schema-name": "%"},
                "rule-action": "convert-lowercase",
            },
        ]
    }
)


def table(rows: int = 0, ins: int | None = None, upd: int = 0, dele: int = 0, cols=None, pk=("id",)) -> dict:
    return {
        "columns": cols or [["id", "bigint", False], ["name", "text", True]],
        "primaryKey": list(pk),
        "uniques": [],
        "rows": rows,
        "exact": True,
        "ins": rows if ins is None else ins,
        "upd": upd,
        "del": dele,
    }


def fp(tables: dict, expected=None, fks=None, out_of_scope=None) -> dict:
    return {
        "schemaVersion": 1,
        "schemas": ["app"],
        "tables": tables,
        "foreignKeys": fks or [],
        "sequences": {},
        "controlTables": [],
        "outOfScope": out_of_scope or [],
        "expected": expected,
    }


LOADED = fp({"app.accounts": table(10), "app.tx": table(100)}, expected=["app.accounts", "app.tx"])
EMPTY = fp({"app.accounts": table(0), "app.tx": table(0)}, expected=["app.accounts", "app.tx"])
START = {"action": "start", "startType": "start-replication", "reset": False, "resetScope": "none", "resetTables": []}
RESUME = {
    "action": "start",
    "startType": "resume-processing",
    "reset": True,
    "resetScope": "partial",
    "resetTables": ["app.tx"],
}
ALREADY = {"action": "reject", "error": "AlreadyCompleted", "reason": "done", "startType": None}


def judge(state="fresh", decision=None, current=EMPTY, baseline=None, **kw):
    args = {
        "state": state,
        "decision": decision or START,
        "current": current,
        "baseline": baseline,
        "table_copy": [],
        "allow_reset": False,
        "pre_jobs": True,
        "reset_configured": True,
        "iteration": 0,
        "max_iterations": 3,
    }
    args.update(kw)
    return preflight.verdict(**args)


# --- scope rules --------------------------------------------------------------------


def test_selection_rules_include_exclude_and_wildcards():
    rules = preflight.selection_rules(MAPPING)
    assert [r["action"] for r in rules] == ["include", "exclude"]
    assert preflight.in_scope(rules, "app", "accounts") is True
    assert preflight.in_scope(rules, "app", "tmp_load") is False
    assert preflight.in_scope(rules, "other", "accounts") is False
    assert preflight.scope_schemas(rules) == ["app"]


def test_scope_with_a_schema_wildcard_scans_every_user_schema():
    rules = preflight.selection_rules(
        json.dumps(
            {
                "rules": [
                    {
                        "rule-type": "selection",
                        "object-locator": {"schema-name": "sales%", "table-name": "orders"},
                        "rule-action": "include",
                    }
                ]
            }
        )
    )
    assert preflight.scope_schemas(rules) is None
    assert preflight.in_scope(rules, "sales_eu", "orders") and not preflight.in_scope(rules, "sales_eu", "x")


def test_no_selection_rule_is_a_configuration_error():
    with pytest.raises(ValueError):
        preflight.selection_rules(json.dumps({"rules": []}))


# --- fingerprint and diff -------------------------------------------------------------


def test_hash_is_stable_and_ignores_reporting_fields():
    a = copy.deepcopy(LOADED)
    b = copy.deepcopy(LOADED)
    b["outOfScope"] = ["app.tmp_x"]
    b["expected"] = None
    assert preflight.fingerprint_hash(a) == preflight.fingerprint_hash(b)
    b["tables"]["app.tx"]["upd"] = 1
    assert preflight.fingerprint_hash(a) != preflight.fingerprint_hash(b)


def test_diff_against_baseline_finds_missing_extra_changed_and_rows():
    now = copy.deepcopy(LOADED)
    now["tables"]["app.audit"] = table(5)  # extra
    now["tables"]["app.accounts"] = table(10, cols=[["id", "integer", False], ["name", "text", True]])  # changed
    now["tables"]["app.tx"] = table(100, upd=3)  # rows changed in a completed table
    now["expected"] = ["app.accounts", "app.tx", "app.refs"]  # refs missing
    d = preflight.diff(now, LOADED)
    assert d == {
        "missing": ["app.refs"],
        "extra": ["app.audit"],
        "changed": ["app.accounts"],
        "rowsChanged": ["app.tx"],
        "foreignKeys": {"missing": [], "extra": []},
        "outOfScope": [],
    }
    assert preflight.is_dirty(d)


def test_diff_without_baseline_uses_the_source_expectation():
    now = fp({"app.accounts": table(0), "app.audit": table(0)}, expected=["app.accounts", "app.tx"])
    d = preflight.diff(now, None)
    assert d["missing"] == ["app.tx"] and d["extra"] == ["app.audit"] and d["rowsChanged"] == []


def test_out_of_scope_objects_only_warn():
    now = fp({"app.accounts": table(0)}, expected=["app.accounts"], out_of_scope=["app.tmp_load", "other.x"])
    d = preflight.diff(now, None)
    assert not preflight.is_dirty(d) and d["outOfScope"] == ["app.tmp_load", "other.x"]


# --- verdicts: one test per row of ADR 0006 ------------------------------------------


def test_row1_missing_tables_with_pre_jobs_prepare_while_they_make_progress():
    partial = fp({"app.accounts": table(0)}, expected=["app.accounts", "app.tx", "app.refs"])
    v = judge(current=partial)
    assert v.verdict == "prepare" and v.diff["missing"] == ["app.refs", "app.tx"]
    # the pre-jobs created one table: check again
    v = judge(current=partial, iteration=1, previous_missing=3)
    assert v.verdict == "prepare"
    # still progress, but the limit is reached
    v = judge(current=partial, iteration=3, previous_missing=3)
    assert v.verdict == "manual" and v.error == "PreflightTablesMissing"


def test_row1_pre_jobs_that_do_not_create_the_tables_leave_them_to_dms():
    # Sandbox and target platform: DMS creates missing tables (TargetTablePrepMode DO_NOTHING).
    partial = fp({}, expected=["app.accounts", "app.tx"])
    v = judge(current=partial, iteration=1, previous_missing=2)
    assert v.verdict == "ready" and any("DMS creates them" in w for w in v.warnings)


def test_row1_missing_tables_without_pre_jobs_only_warn():
    partial = fp({"app.accounts": table(0)}, expected=["app.accounts", "app.tx"])
    v = judge(current=partial, pre_jobs=False)
    assert v.verdict == "ready" and any("app.tx" in w for w in v.warnings)


def test_row2_fresh_task_empty_scope_no_baseline_is_ready():
    v = judge()
    assert v.verdict == "ready" and v.decision == START


@pytest.mark.parametrize(
    "case,kw",
    [
        ("rows in scope", {"current": LOADED}),
        ("DMS recreated", {"table_copy": [{"table": "app.tx", "TableState": "Table completed"}]}),
    ],
)
def test_row3_fresh_task_but_rows_or_history_is_manual_or_reset(case, kw):
    v = judge(**kw)
    assert v.verdict == "manual" and v.error == "TargetNotEmpty", case
    v = judge(allow_reset=True, **kw)
    assert v.verdict == "reset"
    assert v.decision["startType"] == "start-replication" and v.decision["resetScope"] == "all"
    assert v.decision["resetTables"] == ["app.accounts", "app.tx"] and v.decision["reset"] is True


def test_row4_interrupted_and_unchanged_resumes_with_the_dms_decision():
    v = judge(state="interrupted", decision=RESUME, current=LOADED, baseline=copy.deepcopy(LOADED))
    assert v.verdict == "resume" and v.decision == RESUME


def test_row4_without_a_baseline_resumes_with_a_warning():
    v = judge(state="interrupted", decision=RESUME, current=LOADED, baseline=None)
    assert v.verdict == "resume" and any("baseline" in w for w in v.warnings)


def test_row5_interrupted_and_changed_is_manual_or_reload():
    spoiled = copy.deepcopy(LOADED)
    spoiled["tables"]["app.tx"]["upd"] = 7
    spoiled["tables"]["app.junk"] = table(1)
    v = judge(state="interrupted", decision=RESUME, current=spoiled, baseline=LOADED)
    assert v.verdict == "manual" and v.error == "TargetChanged"
    assert v.diff["extra"] == ["app.junk"] and v.diff["rowsChanged"] == ["app.tx"]
    v = judge(state="interrupted", decision=RESUME, current=spoiled, baseline=LOADED, allow_reset=True)
    assert v.verdict == "reset" and v.decision["startType"] == "reload-target"
    assert v.decision["resetTables"] == ["app.accounts", "app.junk", "app.tx"]


def test_row6_completed_migrate_is_manual_or_reload():
    v = judge(state="completed", decision=ALREADY, current=LOADED, baseline=LOADED)
    assert v.verdict == "manual" and v.error == "AlreadyCompleted"
    v = judge(state="completed", decision=ALREADY, current=LOADED, baseline=LOADED, allow_reset=True)
    assert v.verdict == "reset" and v.decision["startType"] == "reload-target"


@pytest.mark.parametrize("error", ["TaskAlreadyRunning", "TaskBusy", "TaskNeedsAttention"])
def test_row7_running_busy_or_needs_attention_is_rejected_as_before(error):
    v = judge(state="running", decision={"action": "reject", "error": error, "reason": "x"})
    assert v.verdict == "reject" and v.error == error


def test_reset_without_a_reset_phase_is_manual():
    v = judge(current=LOADED, allow_reset=True, reset_configured=False)
    assert v.verdict == "manual" and v.error == "ResetNotConfigured"


def test_without_a_fingerprint_the_dms_state_decides_and_warns():
    v = judge(state="interrupted", decision=RESUME, current=None)
    assert v.verdict == "resume" and any("fingerprint" in w for w in v.warnings)
    v = judge(state="completed", decision=ALREADY, current=None, allow_reset=True, scope_tables=["app.a"])
    assert v.verdict == "reset" and v.decision["resetTables"] == ["app.a"]


def test_verdict_report_is_json_ready():
    v = judge(current=LOADED)
    data = v.to_dict()
    json.dumps(data)
    assert set(data) == {"verdict", "reason", "error", "diff", "decision", "warnings"}


# --- after orch.py restore: never resume -------------------------------------------

RESTORED_MSG = "target restored from dump r1; a full reload is needed: re-run with --allow-reset"
STATS = [{"table": "app.accounts", "state": "Table completed"}, {"table": "app.tx", "state": "Table error"}]


@pytest.mark.parametrize("state,dec", [("interrupted", RESUME), ("completed", ALREADY)])
def test_restore_with_dms_progress_is_manual_restored_target(state, dec):
    """The dump is the target before the run, but DMS still reports tables as loaded: a resume
    would skip them and leave the migrated rows out, silently."""
    same = copy.deepcopy(LOADED)
    v = judge(state=state, decision=dec, current=LOADED, baseline=same, restored_from="r1", table_stats=STATS)
    assert (v.verdict, v.error, v.reason) == ("manual", "RestoredTarget", RESTORED_MSG)
    partial_only = [{"table": "app.tx", "state": "Before load"}]
    v = judge(state=state, decision=dec, current=LOADED, baseline=same, restored_from="r1", table_stats=partial_only)
    assert v.verdict == "manual" and v.error == "RestoredTarget"


def test_restore_with_allow_reset_is_a_full_reload():
    v = judge(
        state="interrupted",
        decision=RESUME,
        current=LOADED,
        baseline=copy.deepcopy(LOADED),
        restored_from="r1",
        table_stats=STATS,
        allow_reset=True,
    )
    assert v.verdict == "reset" and v.error is None
    assert v.decision["startType"] == "reload-target" and v.decision["resetScope"] == "all"
    assert v.decision["resetTables"] == ["app.accounts", "app.tx"] and v.decision["reset"] is True


def test_restore_on_a_fresh_task_follows_the_usual_rules():
    v = judge(state="fresh", current=EMPTY, baseline=copy.deepcopy(EMPTY), restored_from="r1", table_stats=[])
    assert v.verdict == "ready" and v.decision == START


def test_a_post_baseline_still_resumes():
    v = judge(state="interrupted", decision=RESUME, current=LOADED, baseline=copy.deepcopy(LOADED), table_stats=STATS)
    assert v.verdict == "resume"

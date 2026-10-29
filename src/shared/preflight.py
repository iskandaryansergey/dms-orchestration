"""Pre-flight: target fingerprint, diff and verdict (ADR 0006). Pure functions, no AWS.

The fingerprint is built by the built-in job ``checks/target_verify`` (Data API) over the
schemas of the task's selection rules::

    {"schemaVersion": 1, "schemas": ["app"],
     "tables": {"app.accounts": {"columns": [[name, type, nullable], ...], "primaryKey": [...],
                                 "uniques": [[...]], "rows": n, "exact": bool,
                                 "ins": n, "upd": n, "del": n}},
     "foreignKeys": ["app.tx(account_id)->app.accounts(id)"], "sequences": {"app.s": n},
     "controlTables": [...], "outOfScope": [...], "expected": [...] | null}

``expected`` is the source catalogue filtered by the selection rules (null when the source
has no Data API). ``outOfScope`` lists objects in the scanned schemas that the rules do not
select: many-to-one targets have neighbours, so they only warn.

Verdicts (``verdict``): ``ready | prepare | resume | reset | manual | reject``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

VERDICTS = ("ready", "prepare", "resume", "reset", "manual", "reject")
HASHED_KEYS = ("tables", "foreignKeys", "sequences")


# --- scope (selection rules of the table mapping) ----------------------------------------


def selection_rules(table_mappings: str | dict[str, Any] | None) -> list[dict[str, str]]:
    """Selection rules of a DMS table mapping, as ``[{"schema", "table", "action"}]``."""
    data = json.loads(table_mappings) if isinstance(table_mappings, str) else (table_mappings or {})
    rules = []
    for rule in data.get("rules") or []:
        if rule.get("rule-type") != "selection":
            continue
        loc = rule.get("object-locator") or {}
        action = rule.get("rule-action", "include")
        rules.append({"schema": loc.get("schema-name", "%"), "table": loc.get("table-name", "%"), "action": action})
    if not any(r["action"] in ("include", "explicit") for r in rules):
        raise ValueError("the table mapping has no include selection rule")
    return rules


def _like(pattern: str, value: str) -> bool:
    """DMS patterns use % (any) and _ is literal enough for our names."""
    return fnmatch.fnmatchcase(value.lower(), pattern.lower().replace("%", "*"))


def in_scope(rules: list[dict[str, str]], schema: str, table: str) -> bool:
    """Selected by an include rule and not by an exclude rule (exclude wins, as in DMS)."""
    selected = False
    for rule in rules:
        if _like(rule["schema"], schema) and _like(rule["table"], table):
            if rule["action"] == "exclude":
                return False
            selected = True
    return selected


def scope_schemas(rules: list[dict[str, str]]) -> list[str] | None:
    """Schemas to scan; None when a schema pattern has a wildcard (scan every user schema)."""
    names = {r["schema"] for r in rules if r["action"] != "exclude"}
    if any("%" in n for n in names):
        return None
    return sorted(names)


# --- fingerprint and diff ------------------------------------------------------------------


def fingerprint_hash(fp: dict[str, Any]) -> str:
    """sha256 of what a run is responsible for: tables (structure, rows, row counters), FKs, sequences."""
    body = json.dumps({k: fp.get(k) for k in HASHED_KEYS}, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _structure(t: dict[str, Any]) -> Any:
    return [t.get("columns"), t.get("primaryKey"), sorted(map(list, t.get("uniques") or []))]


def _rows(t: dict[str, Any]) -> Any:
    return [t.get("rows"), t.get("ins"), t.get("upd"), t.get("del")]


def diff(current: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
    """What is missing, extra or changed in the target.

    With a baseline (the fingerprint the last post recorded) it answers "who changed the
    target since"; without one, "extra" is measured against the source expectation.
    """
    now = current.get("tables") or {}
    expected = current.get("expected")
    if expected is None and baseline:
        expected = list((baseline.get("tables") or {}).keys())
    missing = sorted(set(expected or []) - set(now))
    if baseline:
        before = baseline.get("tables") or {}
        extra = sorted(set(now) - set(before))
        both = sorted(set(now) & set(before))
        changed = [t for t in both if _structure(now[t]) != _structure(before[t])]
        rows_changed = [t for t in both if t not in changed and _rows(now[t]) != _rows(before[t])]
        fk_now, fk_before = set(current.get("foreignKeys") or []), set(baseline.get("foreignKeys") or [])
        fks = {"missing": sorted(fk_before - fk_now), "extra": sorted(fk_now - fk_before)}
    else:
        extra = sorted(set(now) - set(expected)) if expected is not None else []
        changed, rows_changed, fks = [], [], {"missing": [], "extra": []}
    return {
        "missing": missing,
        "extra": extra,
        "changed": changed,
        "rowsChanged": rows_changed,
        "foreignKeys": fks,
        "outOfScope": sorted(current.get("outOfScope") or []),
    }


def is_dirty(d: dict[str, Any]) -> bool:
    """Extra objects, changed structure or rows, or changed FKs: someone touched the target."""
    fks = d.get("foreignKeys") or {}
    return bool(d.get("extra") or d.get("changed") or d.get("rowsChanged") or fks.get("missing") or fks.get("extra"))


def rows_in_scope(fp: dict[str, Any]) -> int:
    return sum(int(t.get("rows") or 0) for t in (fp.get("tables") or {}).values())


# --- verdict ---------------------------------------------------------------------------------


@dataclass
class Verdict:
    verdict: str
    reason: str
    decision: dict[str, Any]
    error: str | None = None
    diff: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _reset_decision(start_type: str, tables: list[str], reason: str) -> dict[str, Any]:
    return {
        "action": "start",
        "startType": start_type,
        "reset": bool(tables),
        "resetScope": "all" if tables else "none",
        "resetTables": sorted(tables),
        "reason": reason,
        "error": None,
    }


def verdict(
    *,
    state: str,
    decision: dict[str, Any],
    current: dict[str, Any] | None,
    baseline: dict[str, Any] | None,
    table_copy: list[dict[str, Any]] | None,
    allow_reset: bool,
    pre_jobs: bool,
    reset_configured: bool,
    iteration: int,
    max_iterations: int,
    scope_tables: list[str] | None = None,
    previous_missing: int | None = None,
    restored_from: str | None = None,
    table_stats: list[dict[str, Any]] | None = None,
) -> Verdict:
    """The pre-flight table of ADR 0006 on top of the DMS decision (``dms_outcome.decide``).

    ``restored_from``: the baseline came from ``orch.py restore`` of a dump taken by that run.
    ``table_stats``: DMS table statistics of the task (``[{"table", "state"}]``)."""
    warnings: list[str] = []
    d = diff(current, baseline) if current is not None else {}
    if d.get("outOfScope"):
        warnings.append(
            f"{len(d['outOfScope'])} object(s) outside the table mapping (left alone): {d['outOfScope'][:5]}"
        )
    tables = sorted((current or {}).get("tables") or scope_tables or [])

    def manual(error: str, reason: str) -> Verdict:
        if not allow_reset:
            return Verdict(
                "manual", f"{reason}; run again with allowReset to reset the target", decision, error, d, warnings
            )
        if not reset_configured:
            return Verdict(
                "manual",
                f"{reason}; allowReset needs a 'reset' phase in the job set",
                decision,
                "ResetNotConfigured",
                d,
                warnings,
            )
        start = "start-replication" if state == "fresh" else "reload-target"
        why = f"{reason}; allowReset: truncate {len(tables)} table(s) in scope, then {start}"
        return Verdict("reset", why, _reset_decision(start, tables, why), None, d, warnings)

    # Row 7: running / busy / needs attention stay rejections, as before.
    if decision.get("action") == "reject" and decision.get("error") != "AlreadyCompleted":
        return Verdict("reject", decision.get("reason") or "", decision, decision.get("error"), d, warnings)

    # After a restore: the target is the dump, from before the run, but DMS still reports
    # tables as loaded or partial. A resume would skip them and leave the migrated rows out,
    # silently: never resume, a full reload only with allowReset. A fresh task has no statistics
    # and follows the usual rows.
    progressed = [t for t in table_stats or [] if t.get("state") and t.get("state") != "Table does not exist"]
    if restored_from and state != "fresh" and progressed:
        why = f"target restored from dump {restored_from}; a full reload is needed: re-run with --allow-reset"
        if not allow_reset:
            return Verdict("manual", why, decision, "RestoredTarget", d, warnings)
        return manual("RestoredTarget", f"target restored from dump {restored_from}")

    if current is None:
        warnings.append("no target fingerprint (no Data API connection named 'target'): the DMS state decides alone")
    elif d["missing"]:
        # Row 1: tables the source has and the target lacks. Pre-jobs run while they make
        # progress; tables they do not create are created by DMS (TargetTablePrepMode DO_NOTHING).
        missing = len(d["missing"])
        progress = iteration == 0 or (previous_missing is not None and missing < previous_missing)
        if pre_jobs and progress and iteration < max_iterations:
            return Verdict("prepare", f"{missing} table(s) missing; running pre-jobs", decision, None, d, warnings)
        if pre_jobs and progress:
            return Verdict(
                "manual",
                f"{missing} table(s) still missing after {iteration} pre-job run(s)",
                decision,
                "PreflightTablesMissing",
                d,
                warnings,
            )
        warnings.append(f"{missing} table(s) missing; DMS creates them: {d['missing'][:5]}")

    if decision.get("error") == "AlreadyCompleted":
        # Row 6.
        return manual("AlreadyCompleted", decision.get("reason") or "the task already completed its load")

    if state == "fresh":
        rows = rows_in_scope(current) if current is not None else 0
        if table_copy:
            # Row 3: our copy of the table states survived a DMS task that was destroyed and created again.
            return manual(
                "TargetNotEmpty", f"the DMS task is new but {len(table_copy)} table state(s) of earlier runs exist"
            )
        if rows:
            return manual("TargetNotEmpty", f"the DMS task never ran but the target has {rows} row(s) in scope")
        return Verdict("ready", "first run, target empty in scope", decision, None, d, warnings)  # row 2

    if state == "interrupted" or (state == "completed" and decision.get("action") == "start"):
        if current is not None and baseline is None:
            warnings.append("no baseline from an earlier run: changes since then cannot be seen")
        elif current is not None and is_dirty(d):
            return manual("TargetChanged", "the target changed since the last run (see diff)")  # row 5
        return Verdict(
            "resume", decision.get("reason") or "continue from the DMS checkpoint", decision, None, d, warnings
        )

    return Verdict("ready", decision.get("reason") or "", decision, None, d, warnings)

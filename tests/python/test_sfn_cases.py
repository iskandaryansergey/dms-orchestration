"""The TestState cases in tests/sfn/cases, checked locally before they run against AWS."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from asl_runner import local_test_state, render

CASES = sorted((Path(__file__).resolve().parents[1] / "sfn" / "cases").glob("*.json"))


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_case_expectation_matches_definition(path):
    case = json.loads(path.read_text())
    got = local_test_state(
        render(case["definition"]),
        case["state"],
        input_=case.get("input"),
        variables=case.get("variables"),
        context=case.get("context"),
        mock=case.get("mock"),
        retrier_retry_count=case.get("retrierRetryCount", 0),
    )
    exp = case["expect"]
    assert got["status"] == exp["status"]
    if "nextState" in exp:
        assert got["nextState"] == exp["nextState"]
    for name, value in (exp.get("variables") or {}).items():
        assert got["variables"].get(name) == value, name


def test_there_are_cases_for_the_risky_states():
    states = {(json.loads(p.read_text())["definition"], json.loads(p.read_text())["state"]) for p in CASES}
    assert {
        ("migrate.asl.json", "AcquireLock"),
        ("migrate.asl.json", "Arm"),
        ("migrate.asl.json", "ArmGuard"),
        ("migrate.asl.json", "CheckAssessmentStatus"),
        ("migrate.asl.json", "StartReplicationTask"),
        ("migrate.asl.json", "CheckDryRun"),
        ("post.asl.json", "CheckOutcome"),
        ("post.asl.json", "CheckAutoResume"),
        ("post.asl.json", "StartResume"),
        ("job-runner.asl.json", "CheckStatus"),
    } <= states


@pytest.mark.parametrize("machine", ["migrate.asl.json", "post.asl.json"])
def test_cases_declare_every_variable_the_run_starts_with(machine):
    """AWS TestState fails on a variable the case does not declare ($startError, seen
    live); the local runner reads it as undefined. A real run always has every variable
    the first state assigns, so a case must declare all of them too."""
    defn = render(machine)
    declared = set(defn["States"][defn["StartAt"]]["Assign"])
    found = 0
    for path in CASES:
        case = json.loads(path.read_text())
        if case["definition"] == machine:
            found += 1
            assert declared <= set(case.get("variables") or {}), (path.stem, declared - set(case["variables"]))
    assert found


def test_generated_cases_are_up_to_date():
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_cases", CASES[0].parents[1] / "make_cases.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name, body in mod.CASES.items():
        path = CASES[0].parent / f"{name}.json"
        assert path.exists() and json.loads(path.read_text()) == body, f"run tests/sfn/make_cases.py ({name})"


def test_teststate_requests_send_context_only_with_a_mock():
    """Seen live: AWS TestState refuses a context when no mock is given ("TestState does not support
    context when a mock is not specified"); the Choice-state cases carry one."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_teststate", CASES[0].parent.parent / "run_teststate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for path in CASES:
        case = json.loads(path.read_text())
        req = mod.request(case)
        assert "context" not in req or "mock" in req, path.stem

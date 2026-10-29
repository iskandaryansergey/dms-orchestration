#!/usr/bin/env python3
"""Run the state machine cases in tests/sfn/cases/*.json against the AWS TestState API.

TestState executes one state of the *real* definition with mocked integrations: it
checks what the local runner cannot — the real JSONata engine, the real
Retry/Catch semantics and error names. It needs credentials with
``states:TestState`` only (no role, nothing is created: every case uses a mock).

    python tests/sfn/run_teststate.py [--region eu-west-2] [case-name ...]

Case file::

    {"definition": "migrate.asl.json", "state": "StartReplicationTask",
     "variables": {...}, "input": {...}, "context": {...},
     "mock": {"result": {...}} | {"errorOutput": {"error": "...", "cause": "..."}},
     "retrierRetryCount": 0,
     "expect": {"status": "SUCCEEDED|FAILED|CAUGHT_ERROR|RETRIABLE", "nextState": "...",
                "variables": {"name": value, ...}}}

Status: written before sandbox access; run it in CI (job sfn-teststate) once the
OIDC role exists and record real error names in the cases.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "python"))

from asl_runner import render  # noqa: E402  (same template values as the local tests)


def load_cases(names: list[str]) -> list[tuple[str, dict[str, Any]]]:
    files = sorted((HERE / "cases").glob("*.json"))
    cases = [(f.stem, json.loads(f.read_text())) for f in files]
    return [c for c in cases if not names or c[0] in names]


def request(case: dict[str, Any]) -> dict[str, Any]:
    definition = render(case["definition"])
    kwargs: dict[str, Any] = {
        "definition": json.dumps(definition),
        "stateName": case["state"],
        "input": json.dumps(case.get("input", {})),
        "inspectionLevel": "DEBUG",
    }
    if case.get("variables") is not None:
        kwargs["variables"] = json.dumps(case["variables"])
    context = case.get("context")
    state = definition["States"].get(case["state"]) or {}
    if str(state.get("Resource", "")).endswith(".waitForTaskToken"):
        # TestState does not invent the callback token (the local runner does): without it
        # the Arguments fail with States.QueryEvaluationError before the mock is used.
        context = {**(context or {}), "Task": {"Token": "token-1"}}
    if context is not None and "mock" in case:
        # AWS refuses a context without a mock; states without a mock (Choice) get none.
        kwargs["context"] = json.dumps(context)
    if "mock" in case:
        mock = case["mock"]
        kwargs["mock"] = (
            {"result": json.dumps(mock["result"])} if "result" in mock else {"errorOutput": mock["errorOutput"]}
        )
    if "retrierRetryCount" in case:
        kwargs["stateConfiguration"] = {"retrierRetryCount": case["retrierRetryCount"]}
    return kwargs


def check(case: dict[str, Any], resp: dict[str, Any]) -> list[str]:
    exp = case["expect"]
    problems = []
    if "status" in exp and resp.get("status") != exp["status"]:
        problems.append(f"status {resp.get('status')} != {exp['status']} ({resp.get('error')}: {resp.get('cause')})")
    if "nextState" in exp and resp.get("nextState") != exp["nextState"]:
        problems.append(f"nextState {resp.get('nextState')} != {exp['nextState']}")
    assigned = json.loads((resp.get("inspectionData") or {}).get("variables") or "{}")
    for name, value in (exp.get("variables") or {}).items():
        if assigned.get(name) != value:
            problems.append(f"variable {name} = {assigned.get(name)!r}, expected {value!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--region")
    p.add_argument("--dry", action="store_true", help="only build the requests (no AWS call)")
    p.add_argument("cases", nargs="*")
    args = p.parse_args(argv)

    cases = load_cases(args.cases)
    if args.dry:
        for name, case in cases:
            request(case)
            print(f"{name}: request ok")
        return 0

    import boto3

    sfn = boto3.client("stepfunctions", region_name=args.region)
    failed = 0
    for name, case in cases:
        resp = sfn.test_state(**request(case))
        problems = check(case, resp)
        print(f"{'FAIL' if problems else 'ok  '} {name}")
        for prob in problems:
            print(f"      {prob}")
        failed += bool(problems)
    print(f"{len(cases) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Generate the migrate/post TestState cases in tests/sfn/cases (ADR 0005).

Each case declares every variable its machine's first state assigns: AWS TestState
fails on an undeclared variable. Run after changing the machines:

    uv run python tests/sfn/make_cases.py
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TASK = "arn:aws:dms:eu-west-2:111122223333:task:CORE1"
CTX = {
    "project": "ledger",
    "taskKey": "core",
    "environment": "dev",
    "taskArn": TASK,
    "runTimeoutSeconds": 604800,
    "jobs": {"pre": [], "reset": [], "post": [], "on_failure": []},
}
COMPLETION = {"mode": "task-stopped", "migrationType": "full-load", "failOnTableErrors": True, "expectStop": False}
MIGRATE_VARS: dict[str, Any] = {
    "runId": "run-1",
    "req": {"project": "ledger", "taskKey": "core", "operation": "migrate", "dryRun": False},
    "ctx": CTX,
    "attempt": 1,
    "startedAt": "2026-10-28T10:00:00Z",
    "waitOnly": False,
    "inspect": {"state": "fresh", "completionSpec": COMPLETION},
    "decision": {"action": "start", "startType": "start-replication", "reset": False},
    "jobCtx": {},
    "verify": None,
    "preflight": {"verdict": "ready", "reason": "first run", "error": None, "diff": {}, "warnings": []},
    "iteration": 0,
    "preDone": False,
    "assessment": None,
    "assessmentArn": None,
    "assessmentPolls": 0,
    "migration": None,
    "failure": None,
    "classification": None,
    "report": None,
    "finalStatus": "FAILED",
}
POST_VARS: dict[str, Any] = {
    "runId": "run-1",
    "req": {"project": "ledger", "taskKey": "core", "operation": "migrate", "dryRun": False},
    "ctx": CTX,
    "inspect": {"state": "fresh", "completionSpec": COMPLETION},
    "jobCtx": {"taskArn": TASK, "startType": "start-replication", "attempt": 1},
    "attempt": 1,
    "startedAt": "2026-10-28T10:00:00Z",
    "preflight": None,
    "dmsOutcome": {"outcome": "COMPLETED", "cause": "full load finished", "dms": {}},
    "failure": None,
    "classification": None,
    "nextRunId": None,
    "baseline": None,
    "report": None,
    "finalStatus": "FAILED",
}


def context(machine: str, name: str) -> dict[str, Any]:
    return {
        "Execution": {
            "Id": f"arn:aws:states:eu-west-2:111122223333:execution:dms-orch-dev-{machine}:{name}",
            "Name": name,
            "StartTime": "2026-10-28T10:00:00Z",
        }
    }


def case(
    machine: str,
    state: str,
    expect: dict[str, Any],
    mock: Any = None,
    retries: int | None = None,
    **var_over: Any,
) -> dict[str, Any]:
    base = MIGRATE_VARS if machine == "migrate" else POST_VARS
    variables = copy.deepcopy(base)
    variables.update(var_over)
    out: dict[str, Any] = {
        "definition": f"{machine}.asl.json",
        "state": state,
        "variables": variables,
        "context": context(machine, "run-1" if machine == "migrate" else "run-1-post"),
    }
    if mock is not None:
        out["mock"] = mock
    if retries is not None:
        out["retrierRetryCount"] = retries
    out["expect"] = expect
    return out


def err(error: str, cause: str = "x") -> dict[str, Any]:
    return {"errorOutput": {"error": error, "cause": cause}}


CONDITIONAL = err("DynamoDb.ConditionalCheckFailedException", "The conditional request failed")
ARMED = {"status": "armed", "runId": "old"}
RESUME_REQ = {
    "project": "ledger",
    "taskKey": "core",
    "operation": "resume",
    "dryRun": False,
    "parentStartedAt": "2026-10-28T09:00:00Z",
}

CASES: dict[str, dict[str, Any]] = {
    "acquire-lock-contention": case(
        "migrate",
        "AcquireLock",
        {"status": "CAUGHT_ERROR", "nextState": "MarkRunEndedWithoutLock", "variables": {"finalStatus": "REJECTED"}},
        mock=CONDITIONAL,
    ),
    "verdict-plan-reports": case(
        "migrate",
        "CheckVerdict",
        {"status": "SUCCEEDED", "nextState": "WriteReport", "variables": {"finalStatus": "PLANNED"}},
        req={"project": "ledger", "taskKey": "core", "operation": "plan", "dryRun": False},
    ),
    "verdict-manual-stops": case(
        "migrate",
        "CheckVerdict",
        {"status": "SUCCEEDED", "nextState": "WriteReport", "variables": {"finalStatus": "MANUAL"}},
        preflight={
            "verdict": "manual",
            "reason": "target changed",
            "error": "TargetChanged",
            "diff": {},
            "warnings": [],
        },
    ),
    "verdict-prepare-runs-pre-jobs": case(
        "migrate",
        "CheckVerdict",
        {"status": "SUCCEEDED", "nextState": "PreJobs", "variables": {"iteration": 1, "preDone": True}},
        preflight={"verdict": "prepare", "reason": "missing", "error": None, "diff": {}, "warnings": []},
    ),
    "decision-already-completed-goes-to-preflight": case(
        "migrate",
        "CheckDecision",
        {"status": "SUCCEEDED", "nextState": "TargetVerify"},
        decision={"action": "reject", "error": "AlreadyCompleted", "reason": "done", "startType": None},
    ),
    "decision-reject": case(
        "migrate",
        "CheckDecision",
        {"status": "SUCCEEDED", "nextState": "WriteReport", "variables": {"finalStatus": "REJECTED"}},
        decision={"action": "reject", "error": "TaskAlreadyRunning", "reason": "already running"},
    ),
    "decision-wait-arms-without-start": case(
        "migrate",
        "CheckDecision",
        {"status": "SUCCEEDED", "nextState": "PrepareStart", "variables": {"waitOnly": True}},
        attempt=2,
        decision={"action": "wait", "startType": None, "reset": False},
        req=RESUME_REQ,
    ),
    "reset-dumps-first": case(
        "migrate",
        "CheckReset",
        {"status": "SUCCEEDED", "nextState": "DumpJobs"},
        decision={"action": "start", "startType": "reload-target", "reset": True},
    ),
    "dry-run-hands-over-to-post": case(
        "migrate",
        "CheckDryRun",
        {"status": "SUCCEEDED", "nextState": "StartPostDryRun"},
        req={"project": "ledger", "taskKey": "core", "operation": "migrate", "dryRun": True},
    ),
    "arm-then-start": case(
        "migrate", "Arm", {"status": "SUCCEEDED", "nextState": "CheckWaitOnly"}, mock={"result": {}}
    ),
    "arm-refused-when-armed": case(
        "migrate", "Arm", {"status": "CAUGHT_ERROR", "nextState": "ClassifyFailure"}, mock=CONDITIONAL
    ),
    "start-ok": case(
        "migrate",
        "StartReplicationTask",
        {"status": "SUCCEEDED", "nextState": "ReportStarted"},
        mock={"result": {"ReplicationTask": {"ReplicationTaskArn": TASK, "Status": "starting"}}},
    ),
    "start-invalid-state-records-the-error": case(
        "migrate",
        "StartReplicationTask",
        {"status": "CAUGHT_ERROR", "nextState": "RecordStartError"},
        mock=err("DatabaseMigration.InvalidResourceStateException", "Replication task is not in a valid state"),
    ),
    "start-transient-error-retries": case(
        "migrate",
        "StartReplicationTask",
        {"status": "RETRIABLE"},
        mock=err("DatabaseMigration.ResourceQuotaExceededFault", "slow down"),
        retries=0,
    ),
    "migrate-start-rejected-unlocks": case(
        "migrate",
        "StartReplicationTask",
        {"status": "CAUGHT_ERROR", "nextState": "Disarm"},
        mock=err("DatabaseMigration.AccessDeniedException", "not authorized"),
        retries=4,
    ),
    "assessment-warning-fails-when-asked": case(
        "migrate",
        "CheckAssessmentStatus",
        {"status": "SUCCEEDED", "nextState": "ClassifyFailure"},
        assessment={"Status": "warning", "LastFailureMessage": None},
        ctx={**CTX, "assessment": {"enabled": True, "failOnWarning": True, "bucket": "res", "folder": "pa/core"}},
    ),
    "assessment-passed-checks-the-arm-guard": case(
        "migrate",
        "CheckAssessmentStatus",
        {"status": "SUCCEEDED", "nextState": "ReadMigration"},
        assessment={"Status": "passed"},
    ),
    # Seen live: a refusal after the reset left the target truncated. Both refusals now come first.
    "assessment-failed-leaves-the-target-untouched": case(
        "migrate",
        "CheckAssessmentStatus",
        {"status": "SUCCEEDED", "nextState": "ClassifyFailure"},
        assessment={"Status": "failed", "LastFailureMessage": "3 checks failed"},
        decision={"action": "start", "startType": "reload-target", "reset": True},
        ctx={**CTX, "assessment": {"enabled": True, "failOnWarning": False, "bucket": "res", "folder": "pa/core"}},
    ),
    "read-migration-finds-an-armed-item": case(
        "migrate",
        "ReadMigration",
        {"status": "SUCCEEDED", "nextState": "ArmGuard", "variables": {"migration": ARMED}},
        mock={"result": {"Item": {"pk": {"S": f"MIGRATION#{TASK}"}, "status": {"S": "armed"}, "runId": {"S": "old"}}}},
    ),
    "already-armed-leaves-the-target-untouched": case(
        "migrate",
        "ArmGuard",
        {"status": "SUCCEEDED", "nextState": "ClassifyFailure"},
        migration=ARMED,
        decision={"action": "start", "startType": "reload-target", "reset": True},
    ),
    "arm-guard-passes-a-posted-item": case(
        "migrate",
        "ArmGuard",
        {"status": "SUCCEEDED", "nextState": "CheckReset"},
        migration={"status": "posted", "runId": "old"},
    ),
    "post-success": case("post", "CheckOutcome", {"status": "SUCCEEDED", "nextState": "PostJobs"}),
    "post-failure": case(
        "post",
        "CheckOutcome",
        {"status": "SUCCEEDED", "nextState": "ClassifyFailure"},
        dmsOutcome=None,
        failure={"Error": "DmsTaskFailed", "Cause": '{"message": "FATAL_ERROR"}'},
    ),
    "post-failure-autoresume": case(
        "post",
        "CheckAutoResume",
        {"status": "SUCCEEDED", "nextState": "ResumeBaseline", "variables": {"nextRunId": "run-1-r2"}},
        classification={"category": "TRANSIENT_SOURCE_CONN", "retryable": True, "autoResume": True},
    ),
    "post-resume-already-started": case(
        "post",
        "StartResume",
        {"status": "CAUGHT_ERROR", "nextState": "WriteReport", "variables": {"finalStatus": "RESUMED"}},
        mock=err("StepFunctions.ExecutionAlreadyExistsException", "Execution already exists"),
        nextRunId="run-1-r2",
    ),
    "post-hand-over-lost": case(
        "post",
        "HandOverLock",
        {"status": "CAUGHT_ERROR", "nextState": "OnFailureJobs"},
        mock=CONDITIONAL,
        nextRunId="run-1-r2",
    ),
}


def main() -> None:
    for name, body in CASES.items():
        path = HERE / "cases" / f"{name}.json"
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(CASES)} cases written")


if __name__ == "__main__":
    main()

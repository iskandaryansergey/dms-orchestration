"""Handoff from migrate to post (ADR 0005): MIGRATION# claim, StartExecution(post) once, safety nets."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import EXEC_ARN, TASK_ARN, FakeDms, FakeSfn, dms_task

from shared import aws as shared_aws
from shared.store import ACTIVE_LOCKS, ACTIVE_MIGRATIONS

POST_SM = "arn:aws:states:eu-west-2:111122223333:stateMachine:dms-orch-test-post"
POST_EXEC = "arn:aws:states:eu-west-2:111122223333:execution:dms-orch-test-post:run-1-post"
STARTED_AT = "2026-10-28T10:00:00.000Z"
FINISHED = dms_task("stopped", "Stop Reason FULL_LOAD_ONLY_FINISHED", StopDate="2026-10-28T10:30:00Z")
STOPPED_OUTSIDE = dms_task("stopped", "Stop Reason NORMAL", StopDate="2026-10-28T10:30:00Z")
STOP_EVENT = {"resources": [TASK_ARN], "detail": {"eventType": "REPLICATION_TASK_STOPPED"}, "time": "t"}


@pytest.fixture(autouse=True)
def post_arn(monkeypatch):
    monkeypatch.setenv("POST_STATE_MACHINE_ARN", POST_SM)


def use(dms: Any = None, sfn: Any = None) -> None:
    if dms is not None:
        shared_aws.set_client("dms", dms)
    if sfn is not None:
        shared_aws.set_client("stepfunctions", sfn)


def arm(store, run_id: str = "run-1", **kw: Any) -> None:
    store.arm_migration(
        task_arn=TASK_ARN,
        run_id=run_id,
        execution_arn=EXEC_ARN,
        completion={"mode": "task-stopped", "failOnTableErrors": True},
        attempt=1,
        started_at=STARTED_AT,
        **kw,
    )


def post_inputs(sfn: FakeSfn) -> list[dict[str, Any]]:
    return [json.loads(c[1]["input"]) for c in sfn.calls if c[0] == "start_execution"]


# --- dms_event_handler ----------------------------------------------------------------


def test_event_while_task_runs_starts_nothing(store):
    from lambdas.dms_event_handler.handler import handler

    sfn = FakeSfn()
    use(dms=FakeDms(dms_task("running")), sfn=sfn)
    arm(store)
    event = {"resources": [TASK_ARN], "detail": {"eventType": "REPLICATION_TASK_STARTED"}, "time": "t"}
    assert handler(event)["results"][TASK_ARN] == "pending"
    assert sfn.calls == [] and store.get_migration(TASK_ARN)["status"] == "armed"


def test_two_events_start_post_exactly_once(store):
    from lambdas.dms_event_handler.handler import handler

    sfn = FakeSfn()
    use(dms=FakeDms(FINISHED), sfn=sfn)
    arm(store)
    assert handler(STOP_EVENT)["results"][TASK_ARN] == "posted"
    assert handler(STOP_EVENT)["results"][TASK_ARN] == "no-active-migration"  # duplicate delivery
    (name, call), *rest = sfn.calls
    assert name == "start_execution" and rest == []
    assert call["stateMachineArn"] == POST_SM and call["name"] == "run-1-post"
    payload = json.loads(call["input"])
    assert payload["runId"] == "run-1" and payload["taskArn"] == TASK_ARN
    assert payload["startedBy"] == "event-handler"
    assert payload["outcome"]["kind"] == "SUCCESS" and payload["outcome"]["outcome"] == "COMPLETED"
    item = store.get_migration(TASK_ARN)
    assert item["status"] == "posted" and item["postExecutionArn"] == POST_EXEC
    assert item["claimedBy"] == "event-handler"
    assert store.active(ACTIVE_MIGRATIONS) == []
    sources = [e["source"] for e in store.run_items("run-1", "EVENT#")]
    assert "dms-event" in sources and "event-handler" in sources


def test_failure_outcome_is_handed_to_post(store):
    from lambdas.dms_event_handler.handler import handler

    sfn = FakeSfn()
    use(dms=FakeDms(STOPPED_OUTSIDE), sfn=sfn)
    arm(store)
    handler(STOP_EVENT)
    outcome = post_inputs(sfn)[0]["outcome"]
    assert outcome["kind"] == "FAILURE" and outcome["error"] == "DmsTaskStoppedUnexpectedly"


def test_events_of_unknown_tasks_are_ignored(store):
    from lambdas.dms_event_handler.handler import handler

    sfn = FakeSfn()
    use(dms=FakeDms(FINISHED), sfn=sfn)
    out = handler({"resources": ["arn:aws:dms:eu-west-2:1:task:OTHER", "arn:aws:dms:eu-west-2:1:rep:X"]})
    assert out["results"] == {"arn:aws:dms:eu-west-2:1:task:OTHER": "no-active-migration"}
    assert sfn.calls == []


class LaggingDms(FakeDms):
    """Describe answers 'running' a few more times after the task has stopped (sandbox fact)."""

    def __init__(self, lag: int) -> None:
        super().__init__(dms_task("running"))
        self.lag = lag

    def describe_replication_tasks(self, **kw: Any) -> dict[str, Any]:
        if self.lag:
            self.lag -= 1
        else:
            self.task = FINISHED
        return super().describe_replication_tasks(**kw)


def test_describe_lag_after_a_stop_event_is_rechecked(store, monkeypatch):
    from lambdas.dms_event_handler import handler as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    sfn = FakeSfn()
    use(dms=LaggingDms(lag=2), sfn=sfn)
    arm(store)
    assert mod.handler(STOP_EVENT)["results"][TASK_ARN] == "posted"
    assert len(post_inputs(sfn)) == 1


def test_describe_lag_of_a_minute_and_a_half_is_still_rechecked_within_the_lambda_timeout(store, monkeypatch):
    """Seen live: on reload-target Describe lagged the STOPPED event by more than 30 s twice, and
    the watchdog had to start post. The handler keeps describing for up to 120 s, and its
    Lambda timeout leaves room for that."""
    import re
    from pathlib import Path

    from lambdas.dms_event_handler import handler as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    sfn = FakeSfn()
    use(dms=LaggingDms(lag=90 // mod.RECHECK_SECONDS), sfn=sfn)
    arm(store)
    assert mod.handler(STOP_EVENT)["results"][TASK_ARN] == "posted"
    budget = mod.RECHECKS * mod.RECHECK_SECONDS
    assert budget >= 120
    tf = (Path(__file__).resolve().parents[2] / "modules/dms-orchestration/lambda.tf").read_text(encoding="utf-8")
    timeout = int(re.search(r"dms_event_handler = \{[^}]*?timeout\s*=\s*(\d+)", tf, re.S).group(1))
    assert timeout >= budget + 30


def test_a_start_event_is_not_rechecked(store, monkeypatch):
    from lambdas.dms_event_handler import handler as mod

    sleeps: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    use(dms=FakeDms(dms_task("running")), sfn=FakeSfn())
    arm(store)
    event = {"resources": [TASK_ARN], "detail": {"eventType": "REPLICATION_TASK_STARTED"}, "time": "t"}
    assert mod.handler(event)["results"][TASK_ARN] == "pending" and sleeps == []


def test_throttled_start_gives_the_claim_back_for_the_retry(store):
    from botocore.exceptions import ClientError

    from lambdas.dms_event_handler.handler import handler

    sfn = FakeSfn()
    sfn.fail_with = "ThrottlingException"
    use(dms=FakeDms(FINISHED), sfn=sfn)
    arm(store)
    with pytest.raises(ClientError):
        handler(STOP_EVENT)
    assert store.get_migration(TASK_ARN)["status"] == "armed"
    sfn.fail_with = None
    assert handler(STOP_EVENT)["results"][TASK_ARN] == "posted"  # Lambda async retry


def test_rejected_start_is_judged_by_dms_and_handed_to_post(store):
    from lambdas.dms_event_handler.handler import handler

    sfn = FakeSfn()
    use(dms=FakeDms(dms_task("stopped", "Stop Reason NORMAL", StopDate="2026-10-28T09:00:00Z")), sfn=sfn)
    arm(store)
    store.record_start_error(
        TASK_ARN, run_id="run-1", error={"Error": "DatabaseMigration.InvalidResourceStateException", "Cause": "bad"}
    )
    handler(STOP_EVENT)
    outcome = post_inputs(sfn)[0]["outcome"]
    assert outcome["error"] == "DmsStartRejected" and "bad" in outcome["cause"]


# --- watchdog -------------------------------------------------------------------------


def test_watchdog_starts_post_when_the_event_was_lost(store):
    from lambdas.watchdog.handler import run

    sfn = FakeSfn()
    dms = FakeDms(dms_task("running"))
    arm(store)
    out = run(store, dms, sfn, now=datetime(2026, 10, 28, 10, 5, tzinfo=UTC))
    assert out["migrations"][TASK_ARN] == "pending" and sfn.calls == []
    dms.task = FINISHED
    out = run(store, dms, sfn, now=datetime(2026, 10, 28, 10, 40, tzinfo=UTC))
    assert out["migrations"][TASK_ARN] == "posted"
    assert post_inputs(sfn)[0]["startedBy"] == "watchdog"


class CrashingSfn(FakeSfn):
    """StartExecution dies like a Lambda killed mid-call (no ClientError, no give-back)."""

    def __init__(self, after_start: bool = False) -> None:
        super().__init__()
        self.crash = True
        self.after_start = after_start

    def start_execution(self, **kw: Any) -> dict[str, Any]:
        if self.crash:
            if self.after_start:
                super().start_execution(**kw)  # the execution exists, the answer is lost
            raise RuntimeError("Lambda timed out")
        return super().start_execution(**kw)


@pytest.mark.parametrize("after_start", [False, True])
def test_watchdog_repeats_start_of_a_stale_claim_with_the_same_name(store, after_start):
    from lambdas.dms_event_handler.handler import handler
    from lambdas.watchdog.handler import run

    sfn = CrashingSfn(after_start=after_start)
    dms = FakeDms(FINISHED)
    use(dms=dms, sfn=sfn)
    arm(store)
    with pytest.raises(RuntimeError):
        handler(STOP_EVENT)
    assert store.get_migration(TASK_ARN)["status"] == "claimed"

    sfn.crash = False
    assert run(store, dms, sfn, now=datetime.now(UTC))["migrations"] == {}  # the sender may still be working
    later = datetime.now(UTC) + timedelta(minutes=10)
    assert run(store, dms, sfn, now=later)["migrations"][TASK_ARN] == "posted"
    assert list(sfn.started) == [POST_EXEC]  # one post execution whatever happened
    assert store.get_migration(TASK_ARN)["postExecutionArn"] == POST_EXEC
    assert run(store, dms, sfn, now=later)["migrations"] == {}


def test_watchdog_alerts_an_overdue_migration_once(store):
    from lambdas.watchdog.handler import run

    arm(store, run_timeout_seconds=3600)
    dms = FakeDms(dms_task("running"))
    late = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    assert run(store, dms, FakeSfn(), now=late)["overdue"] == [TASK_ARN]
    assert run(store, dms, FakeSfn(), now=late)["overdue"] == []
    item = store.get_migration(TASK_ARN)
    assert item["status"] == "armed" and item["overdueAlertedAt"]


def _lock(table: Any, acquired: datetime, execution_arn: str = EXEC_ARN, run_id: str = "run-1") -> None:
    table.put_item(
        Item={
            "pk": f"LOCK#{TASK_ARN}",
            "sk": "LOCK",
            "runId": run_id,
            "executionArn": execution_arn,
            "acquiredAt": acquired.isoformat(),
            "gsi2pk": ACTIVE_LOCKS,
            "gsi2sk": execution_arn,
        }
    )


def test_watchdog_keeps_the_lock_while_dms_runs_after_migrate_ended(store, table):
    from lambdas.watchdog.handler import run

    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    sfn = FakeSfn()
    sfn.executions[EXEC_ARN] = "SUCCEEDED"
    _lock(table, now - timedelta(hours=1))
    arm(store)
    out = run(store, FakeDms(dms_task("running")), sfn, now=now)
    assert out["locksReleased"] == [] and store.get_lock(TASK_ARN) is not None


def test_watchdog_keeps_the_lock_while_post_of_the_run_runs_before_it_took_the_lock_over(store, table):
    # Dry run: migrate starts post and ends; there is no MIGRATION#; post takes the lock over
    # in its second state. The watchdog must not free the lock in between.
    from lambdas.watchdog.handler import run

    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    sfn = FakeSfn()
    sfn.executions[EXEC_ARN] = "SUCCEEDED"
    sfn.executions[POST_EXEC] = "RUNNING"
    _lock(table, now - timedelta(hours=1))
    assert run(store, FakeDms(), sfn, now=now)["locksReleased"] == []
    sfn.executions[POST_EXEC] = "SUCCEEDED"
    assert run(store, FakeDms(), sfn, now=now)["locksReleased"] == [TASK_ARN]


def test_watchdog_releases_the_lock_of_a_dead_post(store, table):
    from lambdas.watchdog.handler import run

    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    sfn = FakeSfn()
    sfn.executions[POST_EXEC] = "ABORTED"
    store.put_run("run-1", {"status": "RUNNING"})
    _lock(table, now - timedelta(hours=1), execution_arn=POST_EXEC)
    assert run(store, FakeDms(), sfn, now=now)["locksReleased"] == [TASK_ARN]


# --- release_lock ---------------------------------------------------------------------


def aborted(execution_arn: str = EXEC_ARN, name: str = "run-1") -> dict[str, Any]:
    return {"detail": {"executionArn": execution_arn, "status": "ABORTED", "name": name}}


def test_release_lock_leaves_lock_and_migration_when_dms_already_runs(store, table):
    from lambdas.release_lock.handler import handler

    use(dms=FakeDms(dms_task("running", StartDate="2026-10-28T10:00:05Z")), sfn=FakeSfn())
    _lock(table, datetime(2026, 10, 28, 9, 0, tzinfo=UTC))
    arm(store)
    out = handler(aborted())
    assert out["released"] == [] and out["dmsRunning"] == [TASK_ARN]
    assert store.get_lock(TASK_ARN) is not None and store.get_migration(TASK_ARN)["status"] == "armed"


def test_release_lock_disarms_when_migrate_died_before_the_start(store, table):
    from lambdas.release_lock.handler import handler

    use(dms=FakeDms(dms_task("ready")), sfn=FakeSfn())
    _lock(table, datetime(2026, 10, 28, 9, 0, tzinfo=UTC))
    store.put_run("run-1", {"status": "RUNNING"})
    arm(store)
    out = handler(aborted())
    assert out["released"] == [TASK_ARN] and out["disarmed"] == [TASK_ARN]
    assert store.get_lock(TASK_ARN) is None and store.get_migration(TASK_ARN) is None
    assert store.get_run("run-1")["status"] == "ABORTED"


def test_release_lock_without_migration_frees_the_lock(store, table):
    from lambdas.release_lock.handler import handler

    use(dms=FakeDms(dms_task("ready")), sfn=FakeSfn())
    _lock(table, datetime(2026, 10, 28, 9, 0, tzinfo=UTC))
    assert handler(aborted())["released"] == [TASK_ARN]


def test_release_lock_frees_the_lock_of_a_failed_post(store, table):
    from lambdas.release_lock.handler import handler

    use(dms=FakeDms(FINISHED), sfn=FakeSfn())
    _lock(table, datetime(2026, 10, 28, 9, 0, tzinfo=UTC), execution_arn=POST_EXEC)
    out = handler({"detail": {"executionArn": POST_EXEC, "status": "FAILED", "name": "run-1-post"}})
    assert out["released"] == [TASK_ARN]


MIGRATE_EXEC = "arn:aws:states:eu-west-2:111122223333:execution:dms-orch-test-migrate:run-1"


def post_ended(status: str = "FAILED", name: str = "run-1-post", run_id: str | None = "run-1") -> dict[str, Any]:
    detail: dict[str, Any] = {
        "executionArn": POST_EXEC.rsplit(":", 1)[0] + ":" + name,
        "stateMachineArn": POST_SM,
        "status": status,
        "name": name,
    }
    if run_id:
        detail["input"] = json.dumps({"runId": run_id, "taskArn": TASK_ARN, "outcome": {}})
    return {"detail": detail}


@pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "ABORTED"])
def test_release_lock_frees_the_lock_of_a_post_that_died_before_taking_it_over(store, table, status):
    """Seen live: post failed in LoadRun, its first state; the lock still named the migrate execution
    and stayed until the watchdog's grace (300 s) ran out."""
    from lambdas.release_lock.handler import handler

    use(dms=FakeDms(FINISHED), sfn=FakeSfn())
    _lock(table, datetime(2026, 10, 28, 9, 0, tzinfo=UTC), execution_arn=MIGRATE_EXEC)
    store.put_run("run-1", {"status": "STARTED"})
    arm(store)
    store.claim_migration(TASK_ARN, run_id="run-1", by="event-handler")
    store.mark_migration_posted(TASK_ARN, run_id="run-1", post_execution_arn=POST_EXEC)
    out = handler(post_ended(status))
    assert out["released"] == [TASK_ARN], out
    assert store.get_lock(TASK_ARN) is None
    assert store.get_migration(TASK_ARN) is None  # 'posted' by this post: nothing waits for it any more
    assert store.get_run("run-1")["status"] == status


def test_release_lock_frees_the_lock_of_a_post_that_died_after_taking_it_over(store, table):
    """A post that died between TakeOverLock and DeleteMigration left MIGRATION# 'posted': DMS is
    done, the post was this one; the lock goes."""
    from lambdas.release_lock.handler import handler

    use(dms=FakeDms(FINISHED), sfn=FakeSfn())
    _lock(table, datetime(2026, 10, 28, 9, 0, tzinfo=UTC), execution_arn=POST_EXEC)
    arm(store)
    store.claim_migration(TASK_ARN, run_id="run-1", by="event-handler")
    store.mark_migration_posted(TASK_ARN, run_id="run-1", post_execution_arn=POST_EXEC)
    out = handler(post_ended("FAILED"))
    assert out["released"] == [TASK_ARN] and out["dmsRunning"] == []
    assert store.get_lock(TASK_ARN) is None


def test_release_lock_keeps_a_lock_an_auto_resume_took_over(store, table):
    """post handed the lock to the next migrate (HandOverLock: runId and executionArn of run-1-r2),
    then died: the lock is the new run's, whether that migrate has started or not."""
    from lambdas.release_lock.handler import handler

    use(dms=FakeDms(FINISHED), sfn=FakeSfn())
    resumed = MIGRATE_EXEC.rsplit(":", 1)[0] + ":run-1-r2"
    _lock(table, datetime(2026, 10, 28, 9, 0, tzinfo=UTC), execution_arn=resumed, run_id="run-1-r2")
    out = handler(post_ended("FAILED"))
    assert out["released"] == []
    assert store.get_lock(TASK_ARN)["runId"] == "run-1-r2"


def test_release_lock_keeps_the_lock_of_a_repeat_post_of_the_same_run(store, table):
    """The first post's event comes late (EventBridge retries) while the operator's repeat holds it."""
    from lambdas.release_lock.handler import handler

    use(dms=FakeDms(FINISHED), sfn=FakeSfn())
    repeat = POST_EXEC + "-20261028T120000"
    _lock(table, datetime(2026, 10, 28, 9, 0, tzinfo=UTC), execution_arn=repeat)
    out = handler(post_ended("FAILED"))
    assert out["released"] == [] and store.get_lock(TASK_ARN)["executionArn"] == repeat


def test_release_lock_finds_the_run_of_a_post_without_input_by_its_name(store, table):
    from lambdas.release_lock.handler import handler

    use(dms=FakeDms(FINISHED), sfn=FakeSfn())
    _lock(table, datetime(2026, 10, 28, 9, 0, tzinfo=UTC), execution_arn=MIGRATE_EXEC)
    assert handler(post_ended("ABORTED", run_id=None))["released"] == [TASK_ARN]


def test_release_lock_ignores_other_executions(store, table):
    from lambdas.release_lock.handler import handler

    use(dms=FakeDms(FINISHED), sfn=FakeSfn())
    _lock(table, datetime(2026, 10, 28, 9, 0, tzinfo=UTC))
    assert handler(aborted(execution_arn="arn:other", name="other"))["released"] == []
    assert store.get_lock(TASK_ARN) is not None


# --- names ----------------------------------------------------------------------------


def test_post_execution_name_fits_the_80_character_limit():
    from shared.handoff import post_execution_name

    assert post_execution_name("run-1") == "run-1-post"
    long = "x" * 80
    name = post_execution_name(long)
    assert len(name) == 80 and name.endswith("-post")
    assert post_execution_name(long) == name  # deterministic: the name is the idempotency key


# --- review finding 7: validation gates the outcome handed to post ----------------------

BAD_VALIDATION = [
    {
        "SchemaName": "app",
        "TableName": "accounts",
        "TableState": "Table completed",
        "ValidationState": "Mismatched records",
        "ValidationFailedRecords": 2,
    }
]


@pytest.mark.parametrize(
    "flag,kind,name",
    [(True, "FAILURE", "DmsValidationErrors"), (False, "SUCCESS", "COMPLETED_WITH_VALIDATION_ERRORS")],
)
def test_finished_load_with_validation_errors_reaches_post_by_the_flag(store, flag, kind, name):
    from lambdas.dms_event_handler.handler import handler

    sfn = FakeSfn()
    use(dms=FakeDms(FINISHED, BAD_VALIDATION), sfn=sfn)
    store.arm_migration(
        task_arn=TASK_ARN,
        run_id="run-1",
        execution_arn=EXEC_ARN,
        completion={"mode": "task-stopped", "failOnTableErrors": True, "failOnValidationErrors": flag},
        attempt=1,
        started_at=STARTED_AT,
    )
    assert handler(STOP_EVENT)["results"][TASK_ARN] == "posted"
    outcome = post_inputs(sfn)[0]["outcome"]
    assert outcome["kind"] == kind and name in (outcome["error"], outcome["outcome"])
    assert "app.accounts" in outcome["cause"]

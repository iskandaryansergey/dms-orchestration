"""Watchdog (EventBridge Scheduler, every few minutes).

1. Armed migrations (``MIGRATION#ACTIVE``): describe DMS; a finished task gets its
   ``post`` started (covers lost or late DMS events). A migration older than its
   ``runTimeoutSeconds`` raises ``MigrationOverdue`` once; the task keeps running.
2. Claims without ``postExecutionArn`` older than ``CLAIM_RETRY_SECONDS``: the
   sender died between the claim and ``StartExecution``; start ``post`` again under
   the same name (idempotent).
3. Active locks: if the owning execution is no longer RUNNING (and the lock is
   older than a grace period), remove the lock (``OrphanedLock``) — unless DMS runs
   for that run (armed migration) or its ``post`` is running. This backs up the
   EventBridge rule that triggers ``release_lock``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError

from shared import aws, dms_outcome, handoff, log
from shared.store import ACTIVE_LOCKS, ACTIVE_MIGRATIONS, Store

LOGGER = log.get_logger("watchdog")


def execution_status(sfn: Any, execution_arn: str | None) -> str:
    if not execution_arn:
        return "UNKNOWN"
    try:
        return sfn.describe_execution(executionArn=execution_arn)["status"]
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == "ExecutionDoesNotExist":
            return "MISSING"
        raise


def _overdue(item: dict[str, Any], now: datetime) -> bool:
    started = dms_outcome.as_dt(item.get("startedAt"))
    limit = int(item.get("runTimeoutSeconds") or 0)
    return bool(started and limit and now > started + timedelta(seconds=limit))


def _run_still_holds_lock(store: Store, sfn: Any, task_arn: str, run_id: str | None) -> bool:
    """migrate ended, but the run goes on: DMS runs (armed/claimed) or its post runs and has not
    taken the lock over yet (handoff in flight, or a dry run handed straight to post)."""
    if not run_id:
        return False
    item = store.get_migration(task_arn)
    if item and item.get("runId") == run_id and item.get("status") in ("armed", "claimed"):
        return True
    post = handoff.execution_arn(handoff.post_state_machine_arn(), handoff.post_execution_name(run_id))
    return execution_status(sfn, post) == "RUNNING"


def run(store: Store, dms: Any, sfn: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    grace = int(os.environ.get("LOCK_GRACE_SECONDS", "300"))
    claim_after = int(os.environ.get("CLAIM_RETRY_SECONDS", "120"))
    summary: dict[str, Any] = {"migrations": {}, "overdue": [], "locksReleased": [], "errors": []}

    for item in store.active(ACTIVE_MIGRATIONS):
        arn, run_id = item["taskArn"], item["runId"]
        try:
            if item.get("status") == "claimed":
                claimed = dms_outcome.as_dt(item.get("claimedAt"))
                if claimed and (now - claimed).total_seconds() < claim_after:
                    continue  # the sender may still be working
                summary["migrations"][arn] = handoff.repeat_post(
                    store=store, dms=dms, sfn=sfn, item=item, logger=LOGGER, now=now
                )
                log.metric(LOGGER, "PostStartRepeated")
                continue
            if _overdue(item, now) and store.mark_migration_overdue(arn, run_id=run_id):
                store.append_event(run_id, "watchdog", "dms", {"overdue": True, "startedAt": item.get("startedAt")})
                log.metric(LOGGER, "MigrationOverdue")
                summary["overdue"].append(arn)
            summary["migrations"][arn] = handoff.check_migration(
                store=store, dms=dms, sfn=sfn, item=item, by="watchdog", logger=LOGGER, now=now
            )
        except Exception as err:  # noqa: BLE001 - one bad migration must not stop the others
            summary["errors"].append(f"{arn}: {err}")
            log.warn(LOGGER, "migration check failed", taskArn=arn, error=str(err))

    for lock in store.active(ACTIVE_LOCKS):
        task_arn = lock["pk"].removeprefix("LOCK#")
        try:
            acquired = dms_outcome.as_dt(lock.get("acquiredAt"))
            if acquired and (now - acquired).total_seconds() < grace:
                continue
            status = execution_status(sfn, lock.get("executionArn"))
            if status in ("RUNNING", "UNKNOWN"):
                continue
            if _run_still_holds_lock(store, sfn, task_arn, lock.get("runId")):
                continue
            if store.release_lock_for_execution(task_arn, lock["executionArn"]):
                store.append_event(lock.get("runId"), "watchdog", "lock", {"released": True, "execution": status})
                store.update_run(lock["runId"], {"status": status, "finishedAt": now.isoformat()})
                log.metric(LOGGER, "OrphanedLock")
                summary["locksReleased"].append(task_arn)
        except Exception as err:  # noqa: BLE001
            summary["errors"].append(f"lock {task_arn}: {err}")
            log.warn(LOGGER, "lock check failed", taskArn=task_arn, error=str(err))

    log.info(LOGGER, "watchdog pass", **summary)
    return summary


def handler(_event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    return run(Store(aws.table()), aws.client("dms"), aws.client("stepfunctions"))

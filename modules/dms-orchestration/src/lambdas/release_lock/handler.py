"""Safety net: release the task lock when an execution dies outside its own Catch.

Triggered by EventBridge ``Step Functions Execution Status Change`` with status
FAILED, TIMED_OUT or ABORTED for the migrate and post state machines. Normal terminal
paths already release the lock inside the state machine; then this finds nothing.

After the DMS start the lock belongs to the migration, not to migrate (ADR 0005):
if migrate dies with an armed ``MIGRATION#`` and DMS picked up the start, the lock
and the item stay and ``post`` releases them. If DMS never started, the item is
removed together with the lock.

A dead ``post`` (any phase) releases the lock of its run (seen live: post failed in
``LoadRun``, before ``TakeOverLock``, and the lock waited for the watchdog's grace):
DMS is done once a post runs. The lock goes only while it still belongs to that run —
its ``runId``, and its ``executionArn`` is this post or the run's own migrate. A lock
handed over to an auto-resume (``runId`` of the next run) or held by a repeat of post
stays.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from shared import aws, handoff, log
from shared.store import ACTIVE_LOCKS, Store, now_iso

LOGGER = log.get_logger("release_lock")
_POST_NAME = re.compile(r"^(?P<run>.+)-post(-\d{8}T\d{6})?$")


def _is_post(detail: dict[str, Any]) -> bool:
    machine = detail.get("stateMachineArn") or ""
    expected = os.environ.get("POST_STATE_MACHINE_ARN")
    return machine == expected if expected else machine.endswith("-post")


def _post_run_id(detail: dict[str, Any]) -> str | None:
    """The run a post execution belongs to: its input, else its name (``<runId>-post[-<ts>]``)."""
    try:
        run_id = json.loads(detail.get("input") or "{}").get("runId")
    except (ValueError, AttributeError):
        run_id = None
    if run_id:
        return str(run_id)
    m = _POST_NAME.match(detail.get("name") or "")
    return m.group("run") if m else None


def _owned_by_run(lock: dict[str, Any], run_id: str, post_arn: str) -> bool:
    """The lock still belongs to the dead post's run: same runId, and held by this post or by
    the run's own migrate (execution name = runId) because the post died before taking it over."""
    holder = str(lock.get("executionArn") or "")
    return lock.get("runId") == run_id and (holder == post_arn or holder.rsplit(":", 1)[-1] == run_id)


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    detail = event.get("detail") or {}
    execution_arn = detail.get("executionArn")
    status = detail.get("status", "UNKNOWN")
    run_id = detail.get("name")
    if not execution_arn:
        return {"released": [], "reason": "no executionArn"}

    store = Store(aws.table())
    released: list[str] = []
    disarmed: list[str] = []
    dms_running: list[str] = []
    owner: dict[str, Any] = {}
    post_run = _post_run_id(detail) if _is_post(detail) else None
    for lock in store.active(ACTIVE_LOCKS):
        task_arn = lock["pk"].removeprefix("LOCK#")
        if post_run:
            if not _owned_by_run(lock, post_run, execution_arn):
                continue
            run_id = post_run
            item = store.get_migration(task_arn) or {}
            posted = item.get("runId") == post_run and item.get("status") == "posted"
            if posted and store.delete_migration(task_arn, run_id=post_run):
                disarmed.append(task_arn)
            if store.release_lock_for_run(task_arn, run_id=post_run, execution_arn=str(lock.get("executionArn"))):
                released.append(task_arn)
                owner = lock
            continue
        if lock.get("executionArn") != execution_arn:
            continue
        run_id = lock.get("runId") or run_id
        item = store.get_migration(task_arn)
        if item and item.get("runId") == lock.get("runId"):
            started = item.get("status") != "armed" or handoff.task_started_since(
                handoff.describe_task(aws.client("dms"), task_arn), item.get("startedAt")
            )
            if started:
                dms_running.append(task_arn)
                store.append_event(run_id, "release-lock", "dms", {"status": status, "dmsRunning": True})
                continue
            if store.delete_migration(task_arn, run_id=item["runId"]):
                disarmed.append(task_arn)
        if store.release_lock_for_execution(task_arn, execution_arn):
            released.append(task_arn)
            owner = lock

    if run_id and released:
        store.update_run(run_id, {"status": status, "finishedAt": now_iso()})
        store.append_event(run_id, "release-lock", "finalize", {"status": status, "locks": released})
        topic = os.environ.get("TOPIC_ARN")
        if topic:
            message = {
                "event": "RunAborted",
                "runId": run_id,
                "status": status,
                "executionArn": execution_arn,
                "releasedLocks": released,
            }
            aws.client("sns").publish(
                TopicArn=topic,
                Subject=f"[dms-orch] run {status}: {run_id}"[:100],
                Message=json.dumps(message),
                MessageAttributes={
                    k: {"DataType": "String", "StringValue": str(v)}
                    for k, v in {
                        "environment": os.environ.get("ENVIRONMENT"),
                        "project": owner.get("project"),
                        "taskKey": owner.get("taskKey"),
                        "status": status,
                        "phase": "aborted",
                        "severity": "high",
                    }.items()
                    if v
                },
            )
    log.info(
        LOGGER,
        "execution ended outside the state machine",
        executionArn=execution_arn,
        status=status,
        released=released,
        disarmed=disarmed,
        dmsRunning=dms_running,
    )
    return {"released": released, "disarmed": disarmed, "dmsRunning": dms_running}

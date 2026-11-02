"""Hand a finished DMS task over from migrate to post exactly once (ADR 0005).

Shared by ``dms_event_handler`` and ``watchdog``: both describe the task, judge it
with ``dms_outcome.evaluate_outcome`` and, on a terminal outcome, claim
``MIGRATION#<taskArn>`` (one winner) before ``StartExecution(post)``.

``StartExecution`` uses a fixed name (``<runId>-post``) as a second guard: Standard
workflows return the same execution for the same name and input and answer
``ExecutionAlreadyExists`` for a different input, which is treated as "already started".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from botocore.exceptions import ClientError

from shared import dms_outcome, log
from shared.store import Store

NAME_LIMIT = 80
POST_SUFFIX = "-post"


def describe_task(dms: Any, task_arn: str) -> dict[str, Any] | None:
    try:
        resp = dms.describe_replication_tasks(
            Filters=[{"Name": "replication-task-arn", "Values": [task_arn]}],
            WithoutSettings=True,
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == "ResourceNotFoundFault":
            return None
        raise
    tasks = resp.get("ReplicationTasks") or []
    return tasks[0] if tasks else None


def table_statistics(dms: Any, task_arn: str) -> list[dict[str, Any]]:
    """``DescribeTableStatistics`` of the task (validation state per table), all pages."""
    out: list[dict[str, Any]] = []
    for page in dms.get_paginator("describe_table_statistics").paginate(ReplicationTaskArn=task_arn):
        out.extend(page.get("TableStatistics") or [])
    return out


def post_execution_name(run_id: str) -> str:
    """``<runId>-post``; a run id too long for the 80-character limit is shortened with a hash."""
    name = f"{run_id}{POST_SUFFIX}"
    if len(name) <= NAME_LIMIT:
        return name
    digest = hashlib.sha1(run_id.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{run_id[: NAME_LIMIT - len(POST_SUFFIX) - 9]}-{digest}{POST_SUFFIX}"


def execution_arn(state_machine_arn: str, name: str) -> str:
    return f"{state_machine_arn.replace(':stateMachine:', ':execution:')}:{name}"


def post_state_machine_arn() -> str:
    return os.environ["POST_STATE_MACHINE_ARN"]


def task_started_since(task: dict[str, Any] | None, started_at: Any) -> bool:
    """Did DMS pick up a start request made at ``started_at``? (release_lock after migrate died)"""
    if not task:
        return False
    status = (task.get("Status") or "").lower()
    if status in dms_outcome.RUNNING_STATUSES:
        return True
    since = dms_outcome.as_dt(started_at)
    stats = task.get("ReplicationTaskStats") or {}
    for key in ("StartDate", "StopDate"):
        moved = dms_outcome.as_dt(stats.get(key))
        if since and moved and moved >= since:
            return True
    return False


def evaluate(dms: Any, item: dict[str, Any], now: Any = None) -> dms_outcome.Outcome:
    return dms_outcome.evaluate_outcome(
        describe_task(dms, item["taskArn"]),
        item.get("completionSpec") or {},
        started_at=item.get("startedAt"),
        now=now,
        start_error=item.get("startError"),
        table_stats=lambda: table_statistics(dms, item["taskArn"]),
    )


def check_migration(
    *, store: Store, dms: Any, sfn: Any, item: dict[str, Any], by: str, logger: logging.Logger, now: Any = None
) -> str:
    """Return what happened: 'pending', 'posted' or 'lost-race'."""
    outcome = evaluate(dms, item, now)
    if outcome.kind == "PENDING":
        return "pending"
    task_arn, run_id = item["taskArn"], item["runId"]
    if not store.claim_migration(task_arn, run_id=run_id, by=by):
        log.info(logger, "migration already claimed", runId=run_id, taskArn=task_arn, by=by)
        return "lost-race"
    store.append_event(
        run_id,
        source=by,
        phase="dms",
        detail={"outcome": outcome.outcome, "error": outcome.error, "cause": outcome.cause},
    )
    try:
        return start_post(store=store, sfn=sfn, item=item, outcome=outcome, by=by, logger=logger)
    except ClientError:
        # Transient: give the claim back so the Lambda retry or the watchdog repeats it.
        store.unclaim_migration(task_arn, run_id=run_id, by=by)
        raise


def start_post(
    *, store: Store, sfn: Any, item: dict[str, Any], outcome: dms_outcome.Outcome, by: str, logger: logging.Logger
) -> str:
    sm_arn = post_state_machine_arn()
    run_id, task_arn = item["runId"], item["taskArn"]
    name = post_execution_name(run_id)
    payload = {"runId": run_id, "taskArn": task_arn, "outcome": outcome.to_dict(), "startedBy": by}
    try:
        arn = sfn.start_execution(stateMachineArn=sm_arn, name=name, input=json.dumps(payload, default=str))[
            "executionArn"
        ]
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
            raise
        arn = execution_arn(sm_arn, name)
        log.info(logger, "post already started", runId=run_id, executionArn=arn)
    store.mark_migration_posted(task_arn, run_id=run_id, post_execution_arn=arn)
    log.info(
        logger, "post started", runId=run_id, taskArn=task_arn, by=by, outcome=outcome.outcome, error=outcome.error
    )
    return "posted"


def repeat_post(
    *, store: Store, dms: Any, sfn: Any, item: dict[str, Any], logger: logging.Logger, now: Any = None
) -> str:
    """A claim without ``postExecutionArn``: the sender died. Start post again under the same name."""
    outcome = evaluate(dms, item, now)
    if outcome.kind == "PENDING":
        store.unclaim_migration(item["taskArn"], run_id=item["runId"], by=item.get("claimedBy") or "")
        return "requeued"
    store.append_event(item["runId"], "watchdog", "dms", {"repeatPost": True, "outcome": outcome.outcome})
    return start_post(store=store, sfn=sfn, item=item, outcome=outcome, by="watchdog:repeat", logger=logger)

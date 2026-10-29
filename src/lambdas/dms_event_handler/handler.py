"""DMS event handler: EventBridge ``DMS Replication Task State Change`` -> start post once.

The event text is not trusted (exact ``eventType`` strings for stop/fail are not
documented). Any event for a task with an armed migration means "describe the task
again"; ``handoff.check_migration`` decides, claims ``MIGRATION#`` and starts ``post``
(ADR 0005).

Observed in the sandbox (docs/dms-observations.md): one second after
``REPLICATION_TASK_STOPPED`` DescribeReplicationTasks still reported the task as
running (later runs: ~30 s, and > 30 s twice on reload-target). For stop/fail
events the describe is therefore repeated for up to 120 s before the task is left to the
next event or the watchdog.
"""

from __future__ import annotations

import time
from typing import Any

from shared import aws, handoff, log
from shared.store import Store

LOGGER = log.get_logger("dms_event_handler")
TERMINAL_EVENTS = {"REPLICATION_TASK_STOPPED", "REPLICATION_TASK_FAILED"}
RECHECKS = 24  # 120 s: on reload-target Describe lagged the stop event by > 30 s
RECHECK_SECONDS = 5


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    store = Store(aws.table())
    detail = event.get("detail") or {}
    results: dict[str, str] = {}
    for arn in event.get("resources") or []:
        if ":task:" not in arn:
            continue
        item = store.get_migration(arn)
        if item:
            store.append_event(
                item.get("runId"),
                "dms-event",
                "dms",
                {
                    "eventType": detail.get("eventType"),
                    "category": detail.get("category"),
                    "message": detail.get("detailMessage"),
                    "time": event.get("time"),
                },
            )
        if not item or item.get("status") != "armed":
            results[arn] = "no-active-migration"
            continue
        kwargs = {
            "store": store,
            "dms": aws.client("dms"),
            "sfn": aws.client("stepfunctions"),
            "item": item,
            "by": "event-handler",
            "logger": LOGGER,
        }
        result = handoff.check_migration(**kwargs)
        rechecks = RECHECKS if detail.get("eventType") in TERMINAL_EVENTS else 0
        while result == "pending" and rechecks:
            rechecks -= 1
            time.sleep(RECHECK_SECONDS)
            result = handoff.check_migration(**kwargs)
        results[arn] = result
    log.info(LOGGER, "dms event processed", eventType=detail.get("eventType"), results=results)
    return {"results": results}

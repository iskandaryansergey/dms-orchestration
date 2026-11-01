"""Jobs the module adds to a run itself (not from a job set): the target check of pre-flight and
of the baseline, the dump before a reset and the snapshot retention after it.

resolve_context builds them; prepare_job accepts a job of one of these names only with exactly
this code (review item N1).
"""

from __future__ import annotations

from typing import Any

from shared import aws
from shared.errors import InvalidRunRequest


def preflight_jobs(runtime: str | None, options: dict[str, Any]) -> list[dict[str, Any]]:
    """The target check of pre-flight and of the baseline (ADR 0006), not part of the job set.

    Lambda: ``checks/target_verify`` through the Data API. Fargate: ``verify-target`` (psql),
    the same fingerprint. No runtime: the list is empty and the verdict comes from the DMS state
    alone (the report says so).
    """
    if runtime is None:
        return []
    common = {
        "name": "target-verify",
        "index": 0,
        "with": {
            "connection": "target",
            "source": "source",
            "exactCountMaxRows": int(options.get("exactCountMaxRows", 1_000_000)),
        },
        "retry": {"maxAttempts": 2, "backoffSeconds": 30},
        "idempotent": True,
        "onError": "fail",
    }
    if runtime == "fargate":
        return [
            {
                **common,
                "runtime": "fargate",
                "image": "sql-runner",
                "command": ["verify-target"],
                "timeoutSeconds": 7200,
            }
        ]
    return [{**common, "runtime": "lambda", "handler": "builtin:checks/target_verify", "timeoutSeconds": 600}]


SNAPSHOT_WAIT_SECONDS = 600  # one Lambda polls this long; 3 attempts cover the 30 min of a snapshot


def dump_jobs(options: dict[str, Any], defaults: dict[str, Any]) -> list[dict[str, Any]]:
    """Dump of the target before a reset.

    ``pg-dump`` (default): pg_dump of the scope streamed to S3, on an SSM runner instance when the
    module's SSM runtime is on (``SSM_JOBS``), in the Fargate sql-runner otherwise.
    ``snapshot``: an Aurora cluster snapshot (built-in Lambda job, polled until available);
    only for tasks registered with ``snapshot_dumps`` (the project job role has the rights).
    """
    if not options.get("enabled", bool(defaults.get("dumpBeforeReset", False))):
        return []
    method = options.get("method", "pg-dump")
    if method == "snapshot":
        if not defaults.get("snapshotDumps"):
            raise InvalidRunRequest(
                "dump.method=snapshot needs snapshot_dumps = true on the task (module variable projects)"
            )
        return [
            {
                "name": "snapshot-target",
                "index": 0,
                "runtime": "lambda",
                "handler": "builtin:dump/cluster_snapshot",
                "with": {"connection": "target", "waitSeconds": SNAPSHOT_WAIT_SECONDS},
                "timeoutSeconds": SNAPSHOT_WAIT_SECONDS + 240,
                "retry": {"maxAttempts": 3, "backoffSeconds": 10},
                "idempotent": True,
                "onError": "fail",
            }
        ]
    where = {"runtime": "ssm"} if aws.env("SSM_JOBS", "0") == "1" else {"runtime": "fargate", "image": "sql-runner"}
    return [
        {
            "name": "dump-target",
            "index": 0,
            **where,
            "command": ["pg-dump-s3", "--connection", "target"],
            "timeoutSeconds": 21600,
            "retry": {"maxAttempts": 1, "backoffSeconds": 60},
            "idempotent": True,
            "onError": "fail",
        }
    ]


def snapshot_retention_job(index: int, retention_days: int) -> dict[str, Any]:
    """Post job: delete this task's snapshots older than dump_retention_days (S3 dumps expire by lifecycle)."""
    return {
        "name": "snapshot-retention",
        "index": index,
        "runtime": "lambda",
        "handler": "builtin:cleanup/cluster_snapshots",
        "with": {"connection": "target", "retentionDays": retention_days},
        "timeoutSeconds": 300,
        "retry": {"maxAttempts": 2, "backoffSeconds": 30},
        "idempotent": True,
        "onError": "warn",
    }

"""DynamoDB single-table access for the orchestrator.

Item layout (pk / sk):

* ``LOCK#<taskArn>`` / ``LOCK`` — one active run per task (written by the state
  machine; ``gsi2pk = LOCK#ACTIVE`` lets the watchdog list locks). Held from migrate to the
  end of post; ``executionArn`` names the execution that owns it now.
* ``RUN#<runId>`` / ``META`` — run metadata (``gsi1pk = PROJECT#<project>``); ``contextVersionId``
  is the S3 version of ``context.json`` that migrate saved, the only one post loads.
  ``resumeChildRunId`` names the only migrate execution post handed the lock to (``HandOverLock``):
  the only one that may claim this run as its ``parentRunId``. ``chainDumped`` / ``dumpUri``: a
  run of this chain (this one or a parent) dumped the target before a reset (``RecordChainDump``).
* ``RUN#<runId>`` / ``EVENT#<ts>#<seq>`` — timeline.
* ``RUN#<runId>`` / ``JOB#<phase>#<nn>#<name>`` — job results.
* ``MIGRATION#<taskArn>`` / ``ACTIVE`` — DMS was started by ``migrate`` and ``post`` has not
  started yet: ``status`` ``armed`` → ``claimed`` (one winner) → ``posted``
  (``gsi2pk = MIGRATION#ACTIVE`` until posted). ``post`` deletes it once it owns the lock.
* ``TARGET#<project>/<taskKey>`` / ``BASELINE`` — fingerprint of the target after the last ``post``
  (hash and S3 URI; the full JSON lives under ``runs/<project>/<runId>/jobs/``).
* ``TABLE#<project>/<taskKey>`` / ``<schema>.<table>`` — our copy of ``DescribeTableStatistics``;
  outlives a DMS task that was destroyed and created again (keyed by the registration, not
  the task ARN: Terraform gives a recreated task a new ARN).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

GSI_ACTIVE = "gsi2"
ACTIVE_LOCKS = "LOCK#ACTIVE"
ACTIVE_MIGRATIONS = "MIGRATION#ACTIVE"
EVENT_TTL_DAYS = 90
EVENT_DETAIL_MAX = 4000
# Project keys as validated by the module (variables.tf); anything else never names a prefix.
PROJECT_KEY = re.compile(r"[a-z][a-z0-9-]{0,39}")
UNREGISTERED_PROJECT = "_unregistered"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def lock_key(task_arn: str) -> dict[str, str]:
    return {"pk": f"LOCK#{task_arn}", "sk": "LOCK"}


def migration_key(task_arn: str) -> dict[str, str]:
    return {"pk": f"MIGRATION#{task_arn}", "sk": "ACTIVE"}


def registration_key(project: str, task_key: str) -> str:
    """Stable id of a registered task: survives a DMS task recreated with a new ARN."""
    return f"{project}/{task_key}"


def target_baseline_key(task_id: str) -> dict[str, str]:
    return {"pk": f"TARGET#{task_id}", "sk": "BASELINE"}


def table_pk(task_id: str) -> str:
    return f"TABLE#{task_id}"


def run_prefix(project: Any, run_id: str) -> str:
    """S3 prefix of a run's artifacts: ``runs/<project>/<runId>`` (context, report, ``jobs/``).

    Job roles may write only ``runs/<their project>/*/jobs/*``. A project that is not a
    valid project key (a request rejected before it was resolved) gets a prefix no job
    role can write.
    """
    p = project if isinstance(project, str) and PROJECT_KEY.fullmatch(project) else UNREGISTERED_PROJECT
    return f"runs/{p}/{run_id}"


def run_key(run_id: str) -> dict[str, str]:
    return {"pk": f"RUN#{run_id}", "sk": "META"}


def job_sk(phase: str, index: int, name: str) -> str:
    return f"JOB#{phase}#{index:02d}#{name}"


def _is_conditional_failure(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def to_plain(value: Any) -> Any:
    """Convert DynamoDB Decimals back to int/float for JSON."""
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def to_ddb(value: Any) -> Any:
    """Floats are not accepted by boto3's serializer; round-trip through Decimal."""
    return json.loads(json.dumps(value, default=str), parse_float=Decimal)


class Store:
    def __init__(self, table: Any):
        self.table = table

    def active(self, kind: str) -> list[dict[str, Any]]:
        """Items in the sparse 'active' index: ACTIVE_LOCKS or ACTIVE_MIGRATIONS."""
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "IndexName": GSI_ACTIVE,
            "KeyConditionExpression": "gsi2pk = :p",
            "ExpressionAttributeValues": {":p": kind},
        }
        while True:
            page = self.table.query(**kwargs)
            items.extend(to_plain(i) for i in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    # --- migration (v4: DMS running, post not started yet) ----------------
    def arm_migration(
        self,
        *,
        task_arn: str,
        run_id: str,
        execution_arn: str,
        completion: dict[str, Any],
        attempt: int,
        started_at: str | None = None,
        run_timeout_seconds: int | None = None,
    ) -> None:
        """Arm a migration; fails if another run is armed.

        The state machine writes the same item itself (``Arm`` in migrate.asl.json);
        this method is the reference used by tests and tools.
        Writing before the start closes the "event before registration" race by order alone.
        """
        now = now_iso()
        item: dict[str, Any] = {
            **migration_key(task_arn),
            "entity": "MIGRATION",
            "taskArn": task_arn,
            "runId": run_id,
            "migrateExecutionArn": execution_arn,
            "completionSpec": to_ddb(completion),
            "attempt": int(attempt),
            "startedAt": started_at or now,
            "armedAt": now,
            "status": "armed",
            "gsi2pk": ACTIVE_MIGRATIONS,
            "gsi2sk": now,
        }
        if run_timeout_seconds:
            item["runTimeoutSeconds"] = int(run_timeout_seconds)
        self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")

    def record_start_error(self, task_arn: str, *, run_id: str, error: Any) -> None:
        """StartReplicationTask answered InvalidResourceState: DMS decides later whether it started."""
        self._update_migration(task_arn, run_id, "SET startError = :e", {":e": to_ddb(error)})

    def unclaim_migration(self, task_arn: str, *, run_id: str, by: str) -> None:
        """Give a claim back after StartExecution failed transiently."""
        try:
            self.table.update_item(
                Key=migration_key(task_arn),
                UpdateExpression="SET #s = :a REMOVE claimedBy, claimedAt",
                ConditionExpression="#s = :c AND runId = :r AND claimedBy = :by",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":a": "armed", ":c": "claimed", ":r": run_id, ":by": by},
            )
        except ClientError as err:
            if not _is_conditional_failure(err):
                raise

    def mark_migration_overdue(self, task_arn: str, *, run_id: str) -> bool:
        """True once: the first caller records the alert."""
        try:
            self.table.update_item(
                Key=migration_key(task_arn),
                UpdateExpression="SET overdueAlertedAt = :at",
                ConditionExpression="runId = :r AND attribute_not_exists(overdueAlertedAt)",
                ExpressionAttributeValues={":r": run_id, ":at": now_iso()},
            )
            return True
        except ClientError as err:
            if _is_conditional_failure(err):
                return False
            raise

    def _update_migration(self, task_arn: str, run_id: str, update: str, values: dict[str, Any]) -> None:
        try:
            self.table.update_item(
                Key=migration_key(task_arn),
                UpdateExpression=update,
                ConditionExpression="runId = :r",
                ExpressionAttributeValues={":r": run_id, **values},
            )
        except ClientError as err:
            if not _is_conditional_failure(err):
                raise

    def get_migration(self, task_arn: str) -> dict[str, Any] | None:
        item = self.table.get_item(Key=migration_key(task_arn), ConsistentRead=True).get("Item")
        return to_plain(item) if item else None

    def claim_migration(self, task_arn: str, *, run_id: str, by: str) -> bool:
        """armed → claimed. Only the winner calls StartExecution(post)."""
        try:
            self.table.update_item(
                Key=migration_key(task_arn),
                UpdateExpression="SET #s = :c, claimedBy = :by, claimedAt = :at",
                ConditionExpression="#s = :a AND runId = :r",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":c": "claimed", ":a": "armed", ":r": run_id, ":by": by, ":at": now_iso()},
            )
            return True
        except ClientError as err:
            if _is_conditional_failure(err):
                return False
            raise

    def mark_migration_posted(self, task_arn: str, *, run_id: str, post_execution_arn: str) -> None:
        """post started: leave the active index; post deletes the item when it ends."""
        try:
            self.table.update_item(
                Key=migration_key(task_arn),
                UpdateExpression="SET #s = :p, postExecutionArn = :e, postedAt = :at REMOVE gsi2pk, gsi2sk",
                ConditionExpression="runId = :r",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":p": "posted", ":e": post_execution_arn, ":r": run_id, ":at": now_iso()},
            )
        except ClientError as err:
            if not _is_conditional_failure(err):
                raise

    def delete_migration(self, task_arn: str, *, run_id: str) -> bool:
        try:
            self.table.delete_item(
                Key=migration_key(task_arn),
                ConditionExpression="runId = :r",
                ExpressionAttributeValues={":r": run_id},
            )
            return True
        except ClientError as err:
            if _is_conditional_failure(err):
                return False
            raise

    # --- target baseline and table states, keyed by registration_key() (outlive the DMS task)
    def put_target_baseline(
        self,
        task_id: str,
        *,
        run_id: str,
        fingerprint_hash: str,
        fingerprint_uri: str,
        summary: dict[str, Any] | None = None,
        source: str = "post",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """``source`` is ``post`` (end of a run, ``runId`` = that run) or ``restore`` (orch.py
        restore, ``runId`` = the run that took the dump; ``restoreRunId``, ``dumpUri`` in ``extra``)."""
        self.table.put_item(
            Item={
                **to_ddb({k: v for k, v in (extra or {}).items() if v is not None}),
                **target_baseline_key(task_id),
                "entity": "TARGET",
                "task": task_id,
                "runId": run_id,
                "source": source,
                "fingerprintHash": fingerprint_hash,
                "fingerprintUri": fingerprint_uri,
                "summary": to_ddb(summary or {}),
                "recordedAt": now_iso(),
            }
        )

    def get_target_baseline(self, task_id: str) -> dict[str, Any] | None:
        item = self.table.get_item(Key=target_baseline_key(task_id), ConsistentRead=True).get("Item")
        return to_plain(item) if item else None

    def put_table_states(self, task_id: str, *, run_id: str, tables: list[dict[str, Any]]) -> None:
        """Replace our copy of DescribeTableStatistics for the task; tables no longer reported are removed."""
        now = now_iso()
        previous = {t["sk"] for t in self.get_table_states(task_id)}
        keep: set[str] = set()
        with self.table.batch_writer() as batch:
            for row in tables:
                name = f"{row['SchemaName']}.{row['TableName']}"
                keep.add(name)
                batch.put_item(
                    Item={
                        **to_ddb(row),
                        "pk": table_pk(task_id),
                        "sk": name,
                        "entity": "TABLE",
                        "table": name,
                        "runId": run_id,
                        "recordedAt": now,
                    }
                )
            for old in previous - keep:
                batch.delete_item(Key={"pk": table_pk(task_id), "sk": old})

    def get_table_states(self, task_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :p",
            "ExpressionAttributeValues": {":p": table_pk(task_id)},
            "ConsistentRead": True,
        }
        while True:
            page = self.table.query(**kwargs)
            items.extend(to_plain(i) for i in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    # --- lock -------------------------------------------------------------
    def get_lock(self, task_arn: str) -> dict[str, Any] | None:
        item = self.table.get_item(Key=lock_key(task_arn), ConsistentRead=True).get("Item")
        return to_plain(item) if item else None

    def release_lock_for_execution(self, task_arn: str, execution_arn: str) -> bool:
        try:
            self.table.delete_item(
                Key=lock_key(task_arn),
                ConditionExpression="executionArn = :e",
                ExpressionAttributeValues={":e": execution_arn},
            )
            return True
        except ClientError as err:
            if _is_conditional_failure(err):
                return False
            raise

    def release_lock_for_run(self, task_arn: str, *, run_id: str, execution_arn: str) -> bool:
        """Delete the lock only if it still names this run and the holder that was read."""
        try:
            self.table.delete_item(
                Key=lock_key(task_arn),
                ConditionExpression="runId = :r AND executionArn = :e",
                ExpressionAttributeValues={":r": run_id, ":e": execution_arn},
            )
            return True
        except ClientError as err:
            if _is_conditional_failure(err):
                return False
            raise

    # --- runs -------------------------------------------------------------
    def put_run(self, run_id: str, attrs: dict[str, Any]) -> None:
        item = {**run_key(run_id), "entity": "RUN", **to_ddb(attrs)}
        project = attrs.get("project")
        if project:
            item["gsi1pk"] = f"PROJECT#{project}"
            item["gsi1sk"] = attrs.get("startedAt") or now_iso()
        self.table.put_item(Item=item)

    def update_run(self, run_id: str, attrs: dict[str, Any]) -> None:
        if not attrs:
            return
        names = {f"#a{i}": k for i, k in enumerate(attrs)}
        values = {f":v{i}": to_ddb(v) for i, v in enumerate(attrs.values())}
        expr = "SET " + ", ".join(f"#a{i} = :v{i}" for i in range(len(attrs)))
        self.table.update_item(
            Key=run_key(run_id),
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        item = self.table.get_item(Key=run_key(run_id)).get("Item")
        return to_plain(item) if item else None

    def append_event(self, run_id: str | None, source: str, phase: str, detail: Any) -> None:
        if not run_id:
            return
        text = detail if isinstance(detail, str) else json.dumps(detail, default=str)
        if len(text) > EVENT_DETAIL_MAX:
            text = text[: EVENT_DETAIL_MAX - 20] + "...<truncated>"
        self.table.put_item(
            Item={
                "pk": f"RUN#{run_id}",
                "sk": f"EVENT#{now_iso()}#{uuid.uuid4().hex[:6]}",
                "entity": "EVENT",
                "source": source,
                "phase": phase,
                "detail": text,
                "ttl": int(time.time()) + EVENT_TTL_DAYS * 86400,
            }
        )

    def run_items(self, run_id: str, prefix: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :p AND begins_with(sk, :s)",
            "ExpressionAttributeValues": {":p": f"RUN#{run_id}", ":s": prefix},
        }
        while True:
            page = self.table.query(**kwargs)
            items.extend(to_plain(i) for i in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def job_results(self, run_id: str) -> list[dict[str, Any]]:
        out = []
        for item in self.run_items(run_id, "JOB#"):
            summary = item.get("summary")
            if isinstance(summary, str):
                try:
                    summary = json.loads(summary)
                except ValueError:
                    summary = {"raw": summary}
            out.append({**item, "summary": summary})
        return out

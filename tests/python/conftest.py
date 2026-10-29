"""Shared fixtures: moto DynamoDB table shaped like the Terraform one, fake clients."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

TABLE = "dms-orch-test"
TASK_ARN = "arn:aws:dms:eu-west-2:111122223333:task:ABCDEF"
EXEC_ARN = "arn:aws:states:eu-west-2:111122223333:execution:dms-orch-test-task-runner:ledger-core-1"


def create_table(ddb: Any, name: str = TABLE) -> Any:
    return ddb.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": a, "AttributeType": "S"} for a in ("pk", "sk", "gsi1pk", "gsi1sk", "gsi2pk", "gsi2sk")
        ],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "gsi1",
                "KeySchema": [
                    {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "gsi2",
                "KeySchema": [
                    {"AttributeName": "gsi2pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi2sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )


@pytest.fixture
def aws() -> Iterator[None]:
    from shared import aws as shared_aws

    with mock_aws():
        shared_aws.reset_clients()
        yield
        shared_aws.reset_clients()


@pytest.fixture
def table(aws: None, monkeypatch: pytest.MonkeyPatch) -> Any:
    from shared import aws as shared_aws

    monkeypatch.setenv("TABLE_NAME", TABLE)
    ddb = boto3.resource("dynamodb", region_name="eu-west-2")
    tbl = create_table(ddb)
    shared_aws.set_client("table", tbl)
    return tbl


@pytest.fixture
def store(table: Any) -> Any:
    from shared.store import Store

    return Store(table)


class FakeSfn:
    """Records Step Functions callback calls; can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_with: str | None = None
        self.executions: dict[str, str] = {}

    def _record(self, name: str, **kwargs: Any) -> dict[str, Any]:
        if self.fail_with:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": self.fail_with, "Message": "x"}}, name)
        self.calls.append((name, kwargs))
        return {}

    def send_task_success(self, **kw: Any) -> dict[str, Any]:
        return self._record("send_task_success", **kw)

    def send_task_failure(self, **kw: Any) -> dict[str, Any]:
        return self._record("send_task_failure", **kw)

    def send_task_heartbeat(self, **kw: Any) -> dict[str, Any]:
        return self._record("send_task_heartbeat", **kw)

    def describe_execution(self, executionArn: str) -> dict[str, Any]:  # noqa: N803
        return {"executionArn": executionArn, "status": self.executions.get(executionArn, "RUNNING")}

    def start_execution(self, **kw: Any) -> dict[str, Any]:
        """Standard semantics: same name + same input -> same execution; other input -> ExecutionAlreadyExists."""
        from botocore.exceptions import ClientError

        if self.fail_with:
            raise ClientError({"Error": {"Code": self.fail_with, "Message": "x"}}, "StartExecution")
        arn = kw["stateMachineArn"].replace(":stateMachine:", ":execution:") + ":" + kw["name"]
        previous = self.started.get(arn)
        if previous is not None and previous != kw.get("input"):
            raise ClientError({"Error": {"Code": "ExecutionAlreadyExists", "Message": "x"}}, "StartExecution")
        self.started[arn] = kw.get("input")
        self.calls.append(("start_execution", kw))
        self.executions.setdefault(arn, "RUNNING")
        return {"executionArn": arn, "startDate": "2026-10-28T10:00:00Z"}

    @property
    def started(self) -> dict[str, Any]:
        if not hasattr(self, "_started"):
            self._started: dict[str, Any] = {}
        return self._started

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class FakeDms:
    """Returns canned DescribeReplicationTasks / DescribeTableStatistics answers."""

    def __init__(self, task: dict[str, Any] | None = None, tables: list[dict[str, Any]] | None = None) -> None:
        self.task = task
        self.tables = tables or []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.connections: list[dict[str, Any]] = []

    def describe_replication_tasks(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("describe_replication_tasks", kw))
        return {"ReplicationTasks": [self.task] if self.task else []}

    def describe_table_statistics(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("describe_table_statistics", kw))
        return {"TableStatistics": self.tables}

    def describe_connections(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("describe_connections", kw))
        return {"Connections": self.connections}

    # TestConnection outcome per endpoint ARN (default "successful"), applied at once.
    test_outcomes: dict[str, str] = {}

    def test_connection(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("test_connection", kw))
        arn = kw["EndpointArn"]
        status = self.test_outcomes.get(arn, "successful")
        row = {"EndpointArn": arn, "EndpointIdentifier": arn.rsplit(":", 1)[-1], "Status": status}
        if status == "failed":
            row["LastFailureMessage"] = "still failing"
        self.connections = [c for c in self.connections if c["EndpointArn"] != arn] + [row]
        return {"Connection": {**row, "Status": "testing"}}

    def get_paginator(self, name: str) -> Any:
        fake = self

        class _P:
            def paginate(self, **kw: Any) -> Any:
                yield getattr(fake, name)(**kw)

        return _P()


def dms_task(status: str, stop_reason: str | None = None, **stats: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "ReplicationTaskArn": TASK_ARN,
        "ReplicationTaskIdentifier": "ledger-core",
        "MigrationType": stats.pop("migration_type", "full-load"),
        "Status": status,
        "ReplicationTaskStats": stats,
        # Every DMS task has a table mapping; pre-flight scopes the target check by it.
        "TableMappings": '{"rules": [{"rule-type": "selection", "rule-id": "1", "object-locator": '
        '{"schema-name": "public", "table-name": "%"}, "rule-action": "include"}]}',
    }
    if stop_reason:
        task["StopReason"] = stop_reason
    if "last_failure" in stats:
        task["LastFailureMessage"] = stats.pop("last_failure")
    return task


@pytest.fixture
def sfn() -> FakeSfn:
    return FakeSfn()

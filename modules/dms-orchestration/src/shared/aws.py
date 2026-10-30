"""Lazily created boto3 clients, overridable in tests."""

from __future__ import annotations

import os
from functools import cache
from typing import Any

import boto3
from botocore.config import Config

_CONFIG = Config(retries={"mode": "standard", "max_attempts": 5}, connect_timeout=5, read_timeout=30)
_overrides: dict[str, Any] = {}


def set_client(name: str, client: Any) -> None:
    """Test hook: replace a client (or the 'table' resource)."""
    _overrides[name] = client


def reset_clients() -> None:
    _overrides.clear()
    _client.cache_clear()
    _table.cache_clear()


@cache
def _client(name: str) -> Any:
    return boto3.client(name, config=_CONFIG)


@cache
def _table(table_name: str) -> Any:
    return boto3.resource("dynamodb", config=_CONFIG).Table(table_name)


def client(name: str) -> Any:
    return _overrides.get(name) or _client(name)


def job_client(name: str, role_arn: str | None, session_name: str = "dms-orch-job") -> Any:
    """A client acting as the project's job role (sts:AssumeRole; trust model in docs/architecture.md).

    Built-in Lambda jobs use it for everything that touches project data (secrets,
    Data API); their own role cannot. Tests override the service client as usual;
    the AssumeRole call still goes through ``client("sts")``.
    """
    if not role_arn:
        raise ValueError("no project job role in the job spec (jobRoleArn); the job cannot reach project data")
    creds = client("sts").assume_role(RoleArn=role_arn, RoleSessionName=session_name[:64], DurationSeconds=900)[
        "Credentials"
    ]
    if name in _overrides:
        return _overrides[name]
    return boto3.client(
        name,
        config=_CONFIG,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def table() -> Any:
    if "table" in _overrides:
        return _overrides["table"]
    return _table(os.environ["TABLE_NAME"])


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise KeyError(f"environment variable {name} is not set")
    return value

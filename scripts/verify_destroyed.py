#!/usr/bin/env python3
"""After `terraform destroy`: list what is left in the account that belongs to the run.

    python scripts/verify_destroyed.py --match sbx --match dms-orch [--profile <profile>] [--region us-east-1]

Exit 0 when nothing matched, 1 otherwise (the leftovers are printed as JSON). A name matches
when it contains any of the ``--match`` strings. Covers what earlier sandbox runs left behind,
including the Container Insights log group of an ECS cluster and Aurora snapshots.
Read-only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any


def _safe(fn: Callable[[], list[str]]) -> list[str]:
    try:
        return fn()
    except Exception as err:  # noqa: BLE001 - one unavailable service must not hide the others
        return [f"<error: {type(err).__name__}: {err}>"]


def _pages(client: Any, op: str, key: str, **kw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in client.get_paginator(op).paginate(**kw):
        out.extend(page.get(key) or [])
    return out


def inventory(session: Any) -> dict[str, list[str]]:
    """Names of resources by kind (only the kinds this project creates)."""
    c = session.client
    return {
        "dms tasks": _safe(
            lambda: [x["ReplicationTaskIdentifier"] for x in c("dms").describe_replication_tasks()["ReplicationTasks"]]
        ),
        "dms instances": _safe(
            lambda: [
                x["ReplicationInstanceIdentifier"]
                for x in c("dms").describe_replication_instances()["ReplicationInstances"]
            ]
        ),
        "dms endpoints": _safe(lambda: [x["EndpointIdentifier"] for x in c("dms").describe_endpoints()["Endpoints"]]),
        "dms subnet groups": _safe(
            lambda: [
                x["ReplicationSubnetGroupIdentifier"]
                for x in c("dms").describe_replication_subnet_groups()["ReplicationSubnetGroups"]
            ]
        ),
        "rds clusters": _safe(
            lambda: [x["DBClusterIdentifier"] for x in c("rds").describe_db_clusters()["DBClusters"]]
        ),
        "rds cluster snapshots": _safe(
            lambda: [
                x["DBClusterSnapshotIdentifier"]
                for x in c("rds").describe_db_cluster_snapshots(SnapshotType="manual")["DBClusterSnapshots"]
            ]
        ),
        "lambda functions": _safe(
            lambda: [x["FunctionName"] for x in _pages(c("lambda"), "list_functions", "Functions")]
        ),
        "state machines": _safe(
            lambda: [x["name"] for x in _pages(c("stepfunctions"), "list_state_machines", "stateMachines")]
        ),
        "dynamodb tables": _safe(lambda: list(c("dynamodb").list_tables()["TableNames"])),
        "s3 buckets": _safe(lambda: [x["Name"] for x in c("s3").list_buckets()["Buckets"]]),
        "sns topics": _safe(lambda: [x["TopicArn"] for x in _pages(c("sns"), "list_topics", "Topics")]),
        "sqs queues": _safe(lambda: list(c("sqs").list_queues().get("QueueUrls") or [])),
        "log groups": _safe(lambda: [x["logGroupName"] for x in _pages(c("logs"), "describe_log_groups", "logGroups")]),
        "ecs clusters": _safe(lambda: list(c("ecs").list_clusters()["clusterArns"])),
        "secrets": _safe(lambda: [x["Name"] for x in _pages(c("secretsmanager"), "list_secrets", "SecretList")]),
        "ssm parameters": _safe(lambda: [x["Name"] for x in _pages(c("ssm"), "describe_parameters", "Parameters")]),
        "event rules": _safe(lambda: [x["Name"] for x in c("events").list_rules()["Rules"]]),
        "schedules": _safe(lambda: [x["Name"] for x in _pages(c("scheduler"), "list_schedules", "Schedules")]),
        "iam roles": _safe(lambda: [x["RoleName"] for x in _pages(c("iam"), "list_roles", "Roles")]),
        "vpcs (non-default)": _safe(
            lambda: [f"{v['VpcId']} {_tag(v, 'Name')}" for v in c("ec2").describe_vpcs()["Vpcs"] if not v["IsDefault"]]
        ),
        "ecr repositories": _safe(
            lambda: [x["repositoryName"] for x in c("ecr").describe_repositories()["repositories"]]
        ),
    }


def _tag(resource: dict[str, Any], key: str) -> str:
    return next((t["Value"] for t in resource.get("Tags") or [] if t["Key"] == key), "")


def leftovers(found: dict[str, list[str]], matches: list[str]) -> dict[str, list[str]]:
    wanted = [m.lower() for m in matches]
    out = {}
    for kind, names in found.items():
        hits = [n for n in names if n.startswith("<error") or any(m in n.lower() for m in wanted)]
        if hits:
            out[kind] = sorted(hits)
    return out


def main(argv: list[str] | None = None, session: Any = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--match", action="append", required=True, help="name fragment of the run (repeatable)")
    p.add_argument("--profile")
    p.add_argument("--region")
    args = p.parse_args(argv)
    if session is None:
        import boto3

        session = boto3.session.Session(profile_name=args.profile, region_name=args.region)
    left = leftovers(inventory(session), args.match)
    print(json.dumps(left, indent=2))
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Operator CLI for DMS orchestration runs.

    orch.py start  --project ledger --task core [--dry-run] [--operation plan] [--allow-reset] [--wait]
    orch.py status <runId | executionArn>
    orch.py stop   <runId | executionArn> [--stop-task] [--reason "..."]
    orch.py report <runId | executionArn> [--format md|json] [--project ledger]
    orch.py list   --project ledger [--limit 20]
    orch.py gate   <runId>        # exit 0 only if the run may be promoted (ADR 0006)
    orch.py post   <runId> [--from-job <name>]   # repeat post; jobs already ok before it are skipped
    orch.py dumps  --project ledger --task core
    orch.py restore --project ledger --task core --dump s3://.../dumps/ledger/core/<ts>-<runId>/ --confirm

A run is two executions (ADR 0005): migrate (<runId>) ends after the DMS start; the DMS
event starts post (<runId>-post). An auto-resume continues as migrate <runId>-r<n>.
``--wait`` follows that chain to the end.

Settings come from flags or environment variables (outputs of the Terraform module):
ORCH_STATE_MACHINE_ARN (state_machine_arns.migrate), ORCH_POST_STATE_MACHINE_ARN
(state_machine_arns.post; derived from the migrate ARN when not set), ORCH_TABLE
(table_name), ORCH_BUCKET (artifacts_bucket). Uses the standard AWS credential chain; the
operator role needs the policy from output operator_policy_json.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import UTC, datetime
from typing import Any

TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}


class Clients:
    """boto3 clients, created on first use (tests pass fakes)."""

    def __init__(self, region: str | None = None, profile: str | None = None, **fakes: Any):
        self._region = region
        self._profile = profile
        self._clients: dict[str, Any] = dict(fakes)

    def get(self, name: str) -> Any:
        if name not in self._clients:
            import boto3

            session = boto3.session.Session(profile_name=self._profile, region_name=self._region)
            self._clients[name] = session.client(name)
        return self._clients[name]


def execution_name(project: str, task: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    name = f"{project}-{task}-{stamp}-{secrets.token_hex(2)}"
    return re.sub(r"[^A-Za-z0-9_-]", "-", name)[:80]


def build_request(args: argparse.Namespace) -> dict[str, Any]:
    req: dict[str, Any] = {"project": args.project, "taskKey": args.task, "operation": args.operation}
    if args.start_type:
        req["startType"] = args.start_type
    if args.reset_policy:
        req["targetReset"] = {"policy": args.reset_policy}
    if args.jobs_version:
        req["jobsVersion"] = args.jobs_version
    if args.job_set:
        req["jobSet"] = args.job_set
    if args.dry_run:
        req["dryRun"] = True
    if args.max_resumes is not None:
        req["autoResume"] = {"maxAttempts": args.max_resumes}
    if args.allow_reset:
        req["allowReset"] = True
    if args.notify_on_start:
        req["notify"] = {"onStart": True}
    req["requestedBy"] = args.requested_by or os.environ.get("ORCH_REQUESTED_BY") or getpass.getuser()
    if args.change_ref:
        req["changeRef"] = args.change_ref
    return req


def execution_arn(state_machine_arn: str, ref: str) -> str:
    if ref.startswith("arn:"):
        return ref
    if not state_machine_arn:
        raise SystemExit("a run id needs --state-machine-arn (or ORCH_STATE_MACHINE_ARN)")
    return state_machine_arn.replace(":stateMachine:", ":execution:") + f":{ref}"


def run_id_of(ref: str) -> str:
    return ref.rsplit(":", 1)[-1] if ref.startswith("arn:") else ref


def post_execution_name(run_id: str) -> str:
    """Same rule as src/shared/handoff.py (the name is the idempotency key of the handoff)."""
    name = f"{run_id}-post"
    if len(name) <= 80:
        return name
    digest = hashlib.sha1(run_id.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{run_id[:66]}-{digest}-post"


def post_state_machine(migrate_arn: str, given: str = "") -> str:
    if given:
        return given
    if not migrate_arn.endswith("-migrate"):
        raise SystemExit("--post-state-machine-arn (or ORCH_POST_STATE_MACHINE_ARN) is required")
    return migrate_arn[: -len("-migrate")] + "-post"


def describe_or_none(sfn: Any, arn: str) -> dict[str, Any] | None:
    try:
        return sfn.describe_execution(executionArn=arn)
    except Exception as err:  # noqa: BLE001 - botocore is optional for the tests
        code = getattr(err, "response", {}).get("Error", {}).get("Code")
        if code == "ExecutionDoesNotExist":
            return None
        raise


def wait_for(sfn: Any, arn: str, poll: float, out: Any, missing: str = "") -> dict[str, Any]:
    """Poll until terminal. With ``missing``, an execution that does not exist yet is waited for."""
    last = None
    while True:
        desc = describe_or_none(sfn, arn) if missing else sfn.describe_execution(executionArn=arn)
        status = desc["status"] if desc else missing
        if status != last:
            print(f"{datetime.now(UTC):%H:%M:%S} {run_id_of(arn)} {status}", file=out)
            last = status
        if desc and status in TERMINAL:
            return desc
        time.sleep(poll)


def _output(desc: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(desc.get("output") or "{}")
    except ValueError:
        return {}


def follow(c: Clients, args: argparse.Namespace, run_id: str, out: Any) -> int:
    """migrate -> (DMS) -> post, and on RESUMED the next migrate, until the run ends."""
    sfn = c.get("stepfunctions")
    post_sm = post_state_machine(args.state_machine_arn, args.post_state_machine_arn)
    while True:
        desc = wait_for(sfn, execution_arn(args.state_machine_arn, run_id), args.poll, out)
        if desc["status"] != "SUCCEEDED" or _output(desc).get("status") != "STARTED":
            return _summarize(desc, out)
        post = execution_arn(post_sm, post_execution_name(run_id))
        desc = wait_for(sfn, post, args.poll, out, missing="DMS running (post not started yet)")
        result = _output(desc)
        if desc["status"] == "SUCCEEDED" and result.get("status") == "RESUMED" and result.get("nextRunId"):
            print(f"auto-resume continues as {result['nextRunId']}", file=out)
            run_id = result["nextRunId"]
            continue
        return _summarize(desc, out)


def _print(data: Any, out: Any) -> None:
    print(json.dumps(data, indent=2, default=str), file=out)


def cmd_start(args: argparse.Namespace, c: Clients, out: Any) -> int:
    if not args.state_machine_arn:
        raise SystemExit("--state-machine-arn (or ORCH_STATE_MACHINE_ARN) is required")
    req = build_request(args)
    name = execution_name(args.project, args.task)
    resp = c.get("stepfunctions").start_execution(
        stateMachineArn=args.state_machine_arn, name=name, input=json.dumps(req)
    )
    _print({"runId": name, "executionArn": resp["executionArn"], "request": req}, out)
    if not args.wait:
        return 0
    return follow(c, args, name, out)


def _summarize(desc: dict[str, Any], out: Any) -> int:
    summary: dict[str, Any] = {"status": desc["status"]}
    if desc.get("output"):
        summary["output"] = json.loads(desc["output"])
    if desc.get("error"):
        summary["error"] = desc["error"]
    if desc.get("cause"):
        try:
            summary["cause"] = json.loads(desc["cause"])
        except ValueError:
            summary["cause"] = desc["cause"]
    _print(summary, out)
    return 0 if desc["status"] == "SUCCEEDED" else 1


def cmd_status(args: argparse.Namespace, c: Clients, out: Any) -> int:
    arn = execution_arn(args.state_machine_arn, args.run)
    desc = c.get("stepfunctions").describe_execution(executionArn=arn)
    data: dict[str, Any] = {
        "runId": run_id_of(arn),
        "executionArn": arn,
        "status": desc["status"],
        "startDate": desc.get("startDate"),
        "stopDate": desc.get("stopDate"),
    }
    if desc.get("output"):
        data["output"] = json.loads(desc["output"])
    if desc.get("error"):
        data["error"], data["cause"] = desc.get("error"), desc.get("cause")
    post_arn = execution_arn(
        post_state_machine(args.state_machine_arn, args.post_state_machine_arn), post_execution_name(run_id_of(arn))
    )
    post = describe_or_none(c.get("stepfunctions"), post_arn)
    if post is None:
        data["post"] = {"status": "NOT_STARTED", "note": "DMS running or post not started yet"}
    else:
        data["post"] = {k: post.get(k) for k in ("executionArn", "status", "startDate", "stopDate")}
        data["post"]["executionArn"] = post_arn
        if post.get("output"):
            data["post"]["output"] = json.loads(post["output"])
        if post.get("error"):
            data["post"]["error"], data["post"]["cause"] = post.get("error"), post.get("cause")
    if args.table:
        item = (
            c.get("dynamodb")
            .get_item(TableName=args.table, Key={"pk": {"S": f"RUN#{run_id_of(arn)}"}, "sk": {"S": "META"}})
            .get("Item")
        )
        if item:
            data["run"] = {k: list(v.values())[0] for k, v in item.items() if k not in ("pk", "sk", "gsi1pk", "gsi1sk")}
    _print(data, out)
    return 0


def cmd_stop(args: argparse.Namespace, c: Clients, out: Any) -> int:
    arn = execution_arn(args.state_machine_arn, args.run)
    stopped_task = None
    if args.stop_task:
        if not args.table:
            raise SystemExit("--stop-task needs --table (or ORCH_TABLE) to find the DMS task of the run")
        item = (
            c.get("dynamodb")
            .get_item(TableName=args.table, Key={"pk": {"S": f"RUN#{run_id_of(arn)}"}, "sk": {"S": "META"}})
            .get("Item")
        )
        if not item or "taskArn" not in item:
            raise SystemExit(f"run {run_id_of(arn)} not found in {args.table}")
        stopped_task = item["taskArn"]["S"]
        c.get("dms").stop_replication_task(ReplicationTaskArn=stopped_task)
    sfn = c.get("stepfunctions")
    post_arn = execution_arn(
        post_state_machine(args.state_machine_arn, args.post_state_machine_arn), post_execution_name(run_id_of(arn))
    )
    stopped = []
    for target in (arn, post_arn):
        desc = describe_or_none(sfn, target)
        if desc and desc["status"] == "RUNNING":
            sfn.stop_execution(executionArn=target, error="StoppedByOperator", cause=args.reason[:32768])
            stopped.append(target)
    if stopped:
        note = "the task lock is released by the release_lock safety net within seconds"
    elif stopped_task:
        note = "DMS stop requested; its event starts post, which reports and releases the lock"
    else:
        note = "no execution is running: DMS keeps running and post will report; add --stop-task to stop DMS"
    _print({"executionArn": arn, "stopped": stopped, "stoppedDmsTask": stopped_task, "note": note}, out)
    return 0


def cmd_report(args: argparse.Namespace, c: Clients, out: Any) -> int:
    if not args.bucket:
        raise SystemExit("--bucket (or ORCH_BUCKET) is required")
    run_id = run_id_of(args.run)
    project = args.project
    if not project and args.table:
        item = (
            c.get("dynamodb")
            .get_item(TableName=args.table, Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "META"}})
            .get("Item")
        )
        project = _attr(item or {}, "project")
    if not project:
        raise SystemExit(
            "--project (or --table to read it from the run) is required: reports are under runs/<project>/"
        )
    key = f"runs/{project}/{run_id}/report.{'md' if args.format == 'md' else 'json'}"
    body = c.get("s3").get_object(Bucket=args.bucket, Key=key)["Body"].read().decode()
    print(body, file=out)
    return 0


def _attr(item: dict[str, Any], name: str) -> Any:
    value = item.get(name)
    return list(value.values())[0] if value else None


def cmd_gate(args: argparse.Namespace, c: Clients, out: Any) -> int:
    """Promotion guard (ADR 0006): the run SUCCEEDED and the target baseline it recorded is
    still the latest one, so nothing ran on the target since. Exit 2 otherwise."""
    if not args.table:
        raise SystemExit("--table (or ORCH_TABLE) is required")
    ddb = c.get("dynamodb")
    run_id = run_id_of(args.run)
    run = ddb.get_item(TableName=args.table, Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "META"}}).get("Item")
    reasons: list[str] = []
    baseline = None
    if not run:
        reasons.append(f"run {run_id} not found")
    else:
        status = _attr(run, "status")
        if status != "SUCCEEDED":
            hint = " (follow the chained run: orch.py status)" if status == "RESUMED" else ""
            reasons.append(f"status {status}{hint}")
        task = f"{_attr(run, 'project')}/{_attr(run, 'taskKey')}"
        item = ddb.get_item(TableName=args.table, Key={"pk": {"S": f"TARGET#{task}"}, "sk": {"S": "BASELINE"}}).get(
            "Item"
        )
        if not item:
            reasons.append("no baseline recorded for the task (no Data API target check?)")
        else:
            baseline = {
                "runId": _attr(item, "runId"),
                "source": _attr(item, "source") or "post",
                "fingerprintHash": _attr(item, "fingerprintHash"),
            }
            if baseline["source"] == "restore":
                # runId is the run that took the dump: the target is that run's starting point.
                reasons.append(f"target restored from a dump of run {baseline['runId']} since: run again first")
            elif baseline["runId"] != run_id:
                reasons.append(f"baseline belongs to run {baseline['runId']}: the target changed hands since")
    _print({"runId": run_id, "ok": not reasons, "reasons": reasons, "baseline": baseline}, out)
    return 0 if not reasons else 2


def cmd_post(args: argparse.Namespace, c: Clients, out: Any) -> int:
    """Repeat the post phase of a run: DMS is asked again; post jobs already ok
    before --from-job are skipped. Refused while the run's first post runs or another run holds
    the lock (resolve_context)."""
    run_id = run_id_of(args.run)
    post_sm = post_state_machine(args.state_machine_arn, args.post_state_machine_arn)
    body: dict[str, Any] = {"runId": run_id, "operation": "post"}
    if args.from_job:
        body["fromJob"] = args.from_job
    if args.jobs_version:
        body["jobsVersion"] = args.jobs_version
    body["startedBy"] = "operator"
    name = f"{run_id[:56]}-post-{datetime.now(UTC):%Y%m%dT%H%M%S}"
    resp = c.get("stepfunctions").start_execution(stateMachineArn=post_sm, name=name, input=json.dumps(body))
    _print({"runId": run_id, "executionArn": resp["executionArn"], "fromJob": args.from_job}, out)
    if not args.wait:
        return 0
    return _summarize(wait_for(c.get("stepfunctions"), resp["executionArn"], args.poll, out), out)


def cmd_dumps(args: argparse.Namespace, c: Clients, out: Any) -> int:
    """Target dumps of a task (pg-dump-s3 before a reset): location and manifest."""
    if not args.bucket:
        raise SystemExit("--bucket (or ORCH_BUCKET) is required")
    s3 = c.get("s3")
    prefix = f"dumps/{args.project}/{args.task}/"
    rows = []
    for common in s3.list_objects_v2(Bucket=args.bucket, Prefix=prefix, Delimiter="/").get("CommonPrefixes") or []:
        p = common["Prefix"]
        manifest = json.loads(s3.get_object(Bucket=args.bucket, Key=f"{p}manifest.json")["Body"].read())
        rows.append({"dump": f"s3://{args.bucket}/{p}", **{k: manifest.get(k) for k in ("bytes", "sha256", "runId")}})
    _print(sorted(rows, key=lambda r: r["dump"], reverse=True), out)
    return 0


def _selection_rules(table_mappings: str | None) -> list[dict[str, str]]:
    """Selection rules of a DMS table mapping, as checks/target_verify expects them (the same
    parsing as shared.preflight.selection_rules; this CLI ships alone)."""
    rules = []
    for rule in json.loads(table_mappings or "{}").get("rules") or []:
        if rule.get("rule-type") == "selection":
            loc = rule.get("object-locator") or {}
            action = rule.get("rule-action", "include")
            rules.append({"schema": loc.get("schema-name", "%"), "table": loc.get("table-name", "%"), "action": action})
    return rules


def _dump_run_id(c: Clients, dump: str) -> str | None:
    """runId of the run that took the dump, from its manifest."""
    bucket, _, prefix = dump.removeprefix("s3://").partition("/")
    key = prefix.rstrip("/") + "/manifest.json"
    return json.loads(c.get("s3").get_object(Bucket=bucket, Key=key)["Body"].read()).get("runId")


def _conditional_failure(err: Exception) -> bool:
    return getattr(err, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class TaskLock:
    """The task lock (LOCK#<taskArn>) held by orch.py restore like a run holds it (ADR 0004).

    ``executionArn`` is the job-runner execution working at the moment: the watchdog releases
    the lock once that execution ended and the grace period passed, so a lock left behind by a
    killed CLI does not block the task for good."""

    def __init__(self, ddb: Any, table: str, task_arn: str, run_id: str, project: str, task: str):
        self.ddb, self.table, self.run_id = ddb, table, run_id
        self.key = {"pk": {"S": f"LOCK#{task_arn}"}, "sk": {"S": "LOCK"}}
        self.attrs = {"entity": {"S": "LOCK"}, "project": {"S": project}, "taskKey": {"S": task}}

    def hold(self, execution_arn: str, *, first: bool) -> None:
        """First: only if no lock exists. Later: move it to the next execution (or take it back
        if the watchdog released it between two executions)."""
        item = {
            **self.key,
            **self.attrs,
            "runId": {"S": self.run_id},
            "executionArn": {"S": execution_arn},
            "acquiredAt": {"S": datetime.now(UTC).isoformat()},
            "gsi2pk": {"S": "LOCK#ACTIVE"},
            "gsi2sk": {"S": execution_arn},
        }
        cond: dict[str, Any] = {"ConditionExpression": "attribute_not_exists(pk)"}
        if not first:
            cond = {
                "ConditionExpression": "attribute_not_exists(pk) OR runId = :me",
                "ExpressionAttributeValues": {":me": {"S": self.run_id}},
            }
        try:
            self.ddb.put_item(TableName=self.table, Item=item, **cond)
        except Exception as err:
            if _conditional_failure(err):
                raise SystemExit("another run holds the task lock: restore after it ends") from None
            raise

    def release(self, out: Any) -> None:
        try:
            self.ddb.delete_item(
                TableName=self.table,
                Key=self.key,
                ConditionExpression="runId = :me",
                ExpressionAttributeValues={":me": {"S": self.run_id}},
            )
        except Exception as err:  # noqa: BLE001 - report, never mask the restore's own outcome
            note = "no longer held by this restore" if _conditional_failure(err) else f"not released: {err}"
            _print({"taskLock": note}, out)


def cmd_restore(args: argparse.Namespace, c: Clients, out: Any) -> int:
    """pg-restore-s3 as a job-runner execution with the project's job role, under the task lock.

    A successful restore puts the target in a known, intended state: the target is then
    fingerprinted (checks/target_verify, phase restore) and the fingerprint becomes the baseline
    with ``source=restore`` and the ``runId`` of the run that took the dump, so the next run does
    not stop as ``TargetChanged`` (ADR 0006). The command always waits for both and holds the
    task lock (``LOCK#<taskArn>``, only if none exists) from before the start to the end."""
    if not args.confirm:
        raise SystemExit("restore drops and recreates the objects of the dump: add --confirm")
    if not (args.table and args.ssm_prefix and args.state_machine_arn):
        raise SystemExit("restore needs --table, --ssm-prefix and --state-machine-arn (or the ORCH_* variables)")
    name = f"{args.ssm_prefix.rstrip('/')}/{args.project}/{args.task}"
    reg = json.loads(c.get("ssm").get_parameter(Name=name)["Parameter"]["Value"])
    lock = (
        c.get("dynamodb")
        .get_item(TableName=args.table, Key={"pk": {"S": f"LOCK#{reg['taskArn']}"}, "sk": {"S": "LOCK"}})
        .get("Item")
    )
    if lock:
        raise SystemExit(f"run {_attr(lock, 'runId')} holds the task lock: restore after it ends")
    if not args.state_machine_arn.endswith("-migrate"):
        raise SystemExit("cannot derive the job-runner ARN from --state-machine-arn")
    job_runner = args.state_machine_arn[: -len("-migrate")] + "-job-runner"
    connections = reg.get("connections") or {}
    record = bool((connections.get("target") or {}).get("dataApiClusterArn")) and not args.dry_run
    dump_run = _dump_run_id(c, args.dump) if record else None
    run_id = f"restore-{args.project}-{args.task}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"[:71]
    lock = TaskLock(c.get("dynamodb"), args.table, reg["taskArn"], run_id, args.project, args.task)
    lock.hold(execution_arn(job_runner, run_id), first=True)
    try:
        return _restore_locked(args, c, out, reg, job_runner, run_id, record, dump_run, lock)
    finally:
        lock.release(out)


def _restore_locked(
    args: argparse.Namespace,
    c: Clients,
    out: Any,
    reg: dict[str, Any],
    job_runner: str,
    run_id: str,
    record: bool,
    dump_run: str | None,
    lock: TaskLock,
) -> int:
    connections = reg.get("connections") or {}
    job = {
        "name": "restore-target",
        "index": 0,
        "runtime": "fargate",
        "image": "sql-runner",
        "command": ["pg-restore-s3", "--connection", "target", "--from", args.dump, "--confirm"],
        "timeoutSeconds": 21600,
        "retry": {"maxAttempts": 1, "backoffSeconds": 60},
        "idempotent": False,
        "onError": "fail",
    }
    ctx = {
        "project": args.project,
        "taskKey": args.task,
        "taskArn": reg["taskArn"],
        "environment": reg.get("environment"),
        "connections": connections,
    }
    sfn = c.get("stepfunctions")
    body = {"job": job, "phase": "restore", "runId": run_id, "dryRun": bool(args.dry_run), "ctx": ctx, "failure": None}
    resp = sfn.start_execution(stateMachineArn=job_runner, name=run_id, input=json.dumps(body))
    _print({"restoreRunId": run_id, "executionArn": resp["executionArn"], "from": args.dump}, out)
    desc = wait_for(sfn, resp["executionArn"], args.poll, out)
    if desc["status"] != "SUCCEEDED" or not record:
        code = _summarize(desc, out)
        if code == 0:
            why = "dry run" if args.dry_run else "the target has no Data API connection"
            _print({"baseline": None, "note": f"no baseline recorded ({why}): the next run may stop as MANUAL"}, out)
        return code

    task = c.get("dms").describe_replication_tasks(
        Filters=[{"Name": "replication-task-arn", "Values": [reg["taskArn"]]}]
    )["ReplicationTasks"][0]
    verify = {
        "name": "target-verify",
        "index": 0,
        "runtime": "lambda",
        "handler": "builtin:checks/target_verify",
        "with": {
            "connection": "target",
            "source": None,
            "recordBaseline": {"runId": dump_run, "dumpUri": args.dump},
        },
        "timeoutSeconds": 600,
        "retry": {"maxAttempts": 2, "backoffSeconds": 30},
        "idempotent": True,
        "onError": "fail",
    }
    vctx = {**ctx, "selectionRules": _selection_rules(task.get("TableMappings"))}
    body = {"job": verify, "phase": "restore", "runId": run_id, "dryRun": False, "ctx": vctx, "failure": None}
    lock.hold(execution_arn(job_runner, f"{run_id}-baseline"), first=False)
    resp = sfn.start_execution(stateMachineArn=job_runner, name=f"{run_id}-baseline", input=json.dumps(body))
    vdesc = wait_for(sfn, resp["executionArn"], args.poll, out)
    if vdesc["status"] != "SUCCEEDED":
        _summarize(vdesc, out)
        _print({"baseline": None, "note": "restore ok, baseline not recorded: the next run stops as MANUAL"}, out)
        return 1
    _summarize(desc, out)
    _print({"baseline": {"source": "restore", "dumpRunId": dump_run, "restoreRunId": run_id}}, out)
    return 0


def cmd_list(args: argparse.Namespace, c: Clients, out: Any) -> int:
    if not args.table:
        raise SystemExit("--table (or ORCH_TABLE) is required")
    resp = c.get("dynamodb").query(
        TableName=args.table,
        IndexName="gsi1",
        KeyConditionExpression="gsi1pk = :p",
        ExpressionAttributeValues={":p": {"S": f"PROJECT#{args.project}"}},
        ScanIndexForward=False,
        Limit=args.limit,
    )
    rows = []
    for item in resp.get("Items", []):
        rows.append(
            {
                k: list(item[k].values())[0]
                for k in ("pk", "taskKey", "operation", "status", "startedAt", "finishedAt", "dryRun")
                if k in item
            }
        )
        rows[-1]["runId"] = rows[-1].pop("pk").removeprefix("RUN#")
    _print(rows, out)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orch.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--state-machine-arn", default=os.environ.get("ORCH_STATE_MACHINE_ARN", ""), help="migrate")
    p.add_argument("--post-state-machine-arn", default=os.environ.get("ORCH_POST_STATE_MACHINE_ARN", ""))
    p.add_argument("--table", default=os.environ.get("ORCH_TABLE", ""))
    p.add_argument("--bucket", default=os.environ.get("ORCH_BUCKET", ""))
    p.add_argument("--ssm-prefix", default=os.environ.get("ORCH_SSM_PREFIX", ""), help="/<name>/<environment>")
    p.add_argument("--region", default=os.environ.get("AWS_REGION"))
    p.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("start", help="start a run")
    s.add_argument("--project", required=True)
    s.add_argument("--task", required=True, help="taskKey")
    s.add_argument("--operation", choices=["migrate", "resume", "plan"], default="migrate")
    s.add_argument("--start-type", choices=["auto", "start-replication", "resume-processing", "reload-target"])
    s.add_argument("--reset-policy", choices=["never", "if-interrupted", "always"])
    s.add_argument("--jobs-version")
    s.add_argument("--job-set")
    s.add_argument("--max-resumes", type=int)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--allow-reset", action="store_true", help="pre-flight may reset a dirty target (ADR 0006)")
    s.add_argument("--notify-on-start", action="store_true", help="also notify when DMS starts")
    s.add_argument("--requested-by")
    s.add_argument("--change-ref")
    s.add_argument("--wait", action="store_true", help="wait for the run and exit 0 on success, 1 otherwise")
    s.add_argument("--poll", type=float, default=30.0)
    s.set_defaults(func=cmd_start)

    for name, func, helptext in (("status", cmd_status, "show a run"), ("report", cmd_report, "print the report")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("run", help="runId or executionArn")
        if name == "report":
            sp.add_argument("--format", choices=["md", "json"], default="md")
            sp.add_argument("--project", help="project of the run (default: read from the run item with --table)")
        sp.set_defaults(func=func)

    st = sub.add_parser("stop", help="stop a run (and optionally the DMS task)")
    st.add_argument("run", help="runId or executionArn")
    st.add_argument("--stop-task", action="store_true", help="also call StopReplicationTask")
    st.add_argument("--reason", default="stopped by operator")
    st.set_defaults(func=cmd_stop)

    po = sub.add_parser("post", help="repeat the post phase of a run (skips post jobs already ok)")
    po.add_argument("run", help="runId")
    po.add_argument("--from-job", help="start at this post job")
    po.add_argument("--jobs-version", help="take the post jobs from this (fixed) job set version")
    po.add_argument("--wait", action="store_true")
    po.add_argument("--poll", type=float, default=30.0)
    po.set_defaults(func=cmd_post)

    d = sub.add_parser("dumps", help="target dumps of a task")
    d.add_argument("--project", required=True)
    d.add_argument("--task", required=True)
    d.set_defaults(func=cmd_dumps)

    r = sub.add_parser("restore", help="restore a target dump (pg-restore-s3 on Fargate)")
    r.add_argument("--project", required=True)
    r.add_argument("--task", required=True)
    r.add_argument("--dump", required=True, help="s3://<bucket>/dumps/<project>/<taskKey>/<ts>-<runId>/")
    r.add_argument("--confirm", action="store_true", help="required: pg_restore --clean drops objects first")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--wait", action="store_true", help="accepted for compatibility: restore always waits")
    r.add_argument("--poll", type=float, default=30.0)
    r.set_defaults(func=cmd_restore)

    g = sub.add_parser("gate", help="exit 0 only if the run may be promoted to the next environment")
    g.add_argument("run", help="runId (of the last run of a chain)")
    g.set_defaults(func=cmd_gate)

    ls = sub.add_parser("list", help="recent runs of a project")
    ls.add_argument("--project", required=True)
    ls.add_argument("--limit", type=int, default=20)
    ls.set_defaults(func=cmd_list)
    return p


def main(argv: list[str] | None = None, clients: Clients | None = None, out: Any = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args, clients or Clients(args.region, args.profile), out or sys.stdout)


if __name__ == "__main__":
    sys.exit(main())

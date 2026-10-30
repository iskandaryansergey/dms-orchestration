"""PrepareJob (first step of the job-runner state machine).

* evaluates the job's ``when`` condition against the run context, the failure
  (for on_failure jobs) and the results of earlier jobs of this run;
* renders ``{{ }}`` templates in ``command`` and ``with``;
* resolves where the job runs: built-in Lambda, a project Lambda from the
  allow-list, or a Fargate task definition for the job's image;
* adds the project's job role (``jobRoleArn``): the Fargate task role and the role
  built-in jobs assume, the only roles that can read the project's secrets;
* writes the full job spec to ``s3://<bucket>/runs/<project>/<runId>/jobs/<phase>/<nn>-<name>/spec.json``
  (Fargate jobs read it via ORCH_JOB_SPEC_S3; it never contains secret values);
* expands a reset list kept in S3 (``ctx.resetTablesUri``, above the payload cap) into
  ``spec.json`` only: the state payload keeps ``resetTables`` empty and ``resetTablesCount``.
  Such a job reads the list from its spec (``psql-run --truncate-scope``); a ``{{ ctx.resetTables }}``
  template is refused, it would carry the whole list through the payload.

Input: ``{"job", "phase", "runId", "dryRun", "ctx", "failure"}``. Output: ``{"spec": {...}}``.
"""

from __future__ import annotations

import json
from typing import Any

from shared import aws, expr, log
from shared.errors import InvalidJobSet, JobFailed
from shared.store import Store, run_prefix

LOGGER = log.get_logger("prepare_job")


def previous_results(store: Store, run_id: str) -> dict[str, dict[str, Any]]:
    """{phase: {jobName: {"status", "summary"}}} for jobs already recorded in this run."""
    out: dict[str, dict[str, Any]] = {"pre": {}, "reset": {}, "post": {}, "on_failure": {}}
    for item in store.job_results(run_id):
        out.setdefault(item.get("phase", ""), {})[item.get("name", "")] = {
            "status": item.get("status"),
            "summary": item.get("summary") or {},
            "attempt": item.get("attempt"),
        }
    return out


def expand_reset_tables(ctx: dict[str, Any]) -> dict[str, Any]:
    """ctx with the full reset list when inspect_task put it in S3 (``resetTablesUri``)."""
    uri = ctx.get("resetTablesUri")
    if not uri:
        return ctx
    bucket = aws.env("ARTIFACTS_BUCKET")
    if not str(uri).startswith(f"s3://{bucket}/"):
        raise JobFailed(f"reset list {uri} is not in the artifacts bucket")
    key = str(uri).removeprefix(f"s3://{bucket}/")
    tables = json.loads(aws.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()).get("resetTables")
    count = int(ctx.get("resetTablesCount") or 0)
    if not isinstance(tables, list) or len(tables) != count:
        raise JobFailed(f"reset list {uri}: {len(tables or [])} table(s), the decision says {count}")
    return {**ctx, "resetTables": tables}


def resolve_target(job: dict[str, Any]) -> dict[str, Any]:
    if job["runtime"] == "lambda":
        if job.get("handler"):
            return {"functionArn": aws.env("BUILTIN_JOBS_ARN")}
        allowed = json.loads(aws.env("JOB_LAMBDA_ARNS", "[]"))
        if job.get("function") not in allowed:
            raise InvalidJobSet(f"job {job['name']}: function {job.get('function')} is not in job_lambda_arns")
        return {"functionArn": job["function"]}
    task_definitions = json.loads(aws.env("TASK_DEFINITIONS", "{}"))
    image = job.get("image") or "sql-runner"
    if image not in task_definitions:
        raise InvalidJobSet(
            f"job {job['name']}: image {image!r} has no task definition (module variable fargate.images)"
        )
    return {"taskDefinitionArn": task_definitions[image]}


def prepare(event: dict[str, Any], store: Store) -> dict[str, Any]:
    job = event["job"]
    phase = event["phase"]
    run_id = event["runId"]
    payload_ctx = event.get("ctx") or {}
    ctx = expand_reset_tables(payload_ctx)
    dry_run = bool(event.get("dryRun"))
    index = int(job.get("index", 0))
    base = f"{run_prefix(ctx.get('project'), run_id)}/jobs/{phase}/{index:02d}-{job['name']}"
    bucket = aws.env("ARTIFACTS_BUCKET")

    scope = {
        **previous_results(store, run_id),
        "ctx": ctx,
        "failure": event.get("failure") or {},
        "run": {"runId": run_id, "dryRun": dry_run, "phase": phase},
    }
    retry = job.get("retry") or {}
    spec: dict[str, Any] = {
        "name": job["name"],
        "phase": phase,
        "index": index,
        "runtime": job["runtime"],
        "runId": run_id,
        "dryRun": dry_run,
        "timeoutSeconds": int(job.get("timeoutSeconds", 3600)),
        "maxAttempts": int(retry.get("maxAttempts", 1)),
        "backoffSeconds": int(retry.get("backoffSeconds", 60)),
        "idempotent": bool(job.get("idempotent", True)),
        "onError": job.get("onError", "fail"),
        "recordSk": f"JOB#{phase}#{index:02d}#{job['name']}",
        "artifactsS3Prefix": f"s3://{bucket}/{base}/",
        "resultKey": f"{base}/result.json",
        "specS3Uri": f"s3://{bucket}/{base}/spec.json",
        "skip": False,
    }

    try:
        run_it = expr.is_true(job.get("when"), scope)
    except expr.ExprError as err:
        raise InvalidJobSet(f"job {job['name']}: bad 'when' expression: {err}") from err
    if not run_it:
        spec.update(skip=True, skipReason=f"when is false: {job.get('when')}")
        return {"spec": spec}

    if ctx is not payload_ctx and "ctx.resetTables" in json.dumps([job.get("command"), job.get("with")]):
        raise InvalidJobSet(
            f"job {job['name']}: {ctx['resetTablesCount']} tables to reset, the list is in S3; "
            "use psql-run --truncate-scope instead of {{ ctx.resetTables }}"
        )
    try:
        spec["with"] = expr.render(job.get("with") or {}, scope)
        spec["command"] = expr.render_command(job.get("command") or [], scope)
    except expr.ExprError as err:
        raise InvalidJobSet(f"job {job['name']}: bad template: {err}") from err
    spec.update(resolve_target(job))
    if job["runtime"] == "fargate" or job.get("handler"):
        # Fargate task role / role assumed by built-in jobs: only this project's secrets.
        role = json.loads(aws.env("JOB_ROLE_ARNS", "{}")).get(ctx.get("project"))
        if not role:
            raise InvalidJobSet(f"job {job['name']}: no job role for project {ctx.get('project')!r}")
        spec["jobRoleArn"] = role
    spec["handler"] = job.get("handler")
    spec["image"] = job.get("image")
    spec["artifacts"] = job.get("artifacts") or {}
    spec["connections"] = ctx.get("connections") or {}
    spec["jobsS3Prefix"] = ctx.get("jobsS3Prefix")
    spec["ctx"] = ctx
    spec["failure"] = event.get("failure")
    spec["previous"] = {k: v for k, v in scope.items() if k in ("pre", "reset", "post", "on_failure")}

    aws.client("s3").put_object(
        Bucket=bucket,
        Key=f"{base}/spec.json",
        Body=json.dumps(spec, indent=2, default=str).encode(),
        ContentType="application/json",
    )
    spec["ctx"] = payload_ctx  # the full reset list stays in spec.json
    return {"spec": spec}


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    store = Store(aws.table())
    out = prepare(event, store)
    spec = out["spec"]
    store.append_event(
        event["runId"],
        "job-runner",
        event["phase"],
        {"job": spec["name"], "skip": spec["skip"], "runtime": spec["runtime"], "reason": spec.get("skipReason")},
    )
    log.info(LOGGER, "job prepared", runId=event["runId"], job=spec["name"], phase=event["phase"], skip=spec["skip"])
    # The state machine only needs the routing fields; the full spec stays in S3.
    if spec["runtime"] == "fargate" and not spec["skip"]:
        spec = {k: v for k, v in spec.items() if k not in ("ctx", "previous", "connections", "failure", "with")}
    return {"spec": spec}

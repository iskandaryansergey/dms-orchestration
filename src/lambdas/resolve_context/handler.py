"""ResolveContext: validate the run request and build the run context.

Input (from the state machine)::

    {"request": {...run request...},
     "execution": {"arn": "...", "name": "...", "startTime": "..."}}

Output::

    {"runId": "...", "request": {...with defaults...}, "ctx": {...}, "attempt": 1}

post mode (first state of the post machine, ADR 0005)::

    {"mode": "post", "input": {"runId", "taskArn", "outcome": {...}, "startedBy"}, "execution": {...}}
    -> {"runId", "request", "ctx", "inspect", "jobCtx", "attempt", "startedAt", "startedBy",
        "dmsOutcome": {...} | null, "failure": {"Error", "Cause"} | null}

The task registration comes from SSM (``<SSM_PREFIX>/<project>/<taskKey>``),
written by Terraform. It holds ARNs and connection metadata only, never secret
values. The job set comes from S3 (``jobs/<project>/<jobsVersion>/jobset.json``).
"""

from __future__ import annotations

import json
from typing import Any

from botocore.exceptions import ClientError

from shared import aws, jobset, log, validation
from shared.errors import ContextNotPinned, InvalidRunRequest, JobSetNotFound, RunAlreadyActive, UnknownTask
from shared.store import Store, now_iso, run_prefix

LOGGER = log.get_logger("resolve_context")
DEFAULT_AUTO_RESUME_CATEGORIES = ["TRANSIENT_SOURCE_CONN", "TRANSIENT_TARGET_CONN", "DMS_RESTART"]


def load_registration(project: str, task_key: str) -> dict[str, Any]:
    name = f"{aws.env('SSM_PREFIX').rstrip('/')}/{project}/{task_key}"
    try:
        value = aws.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == "ParameterNotFound":
            raise UnknownTask(f"no task registered as {project}/{task_key} (SSM parameter {name})") from err
        raise
    return json.loads(value)


def load_jobs(bucket: str, prefix: str, version: str, job_set: str) -> dict[str, list[dict[str, Any]]]:
    key = f"{prefix.strip('/')}/{version}/{jobset.jobset_file_name(job_set)}"
    try:
        body = aws.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "AccessDenied"):
            raise JobSetNotFound(f"job set not found at s3://{bucket}/{key} (run scripts/sync-jobs.sh)") from err
        raise
    return jobset.normalize(json.loads(body))


FARGATE_ABOVE_TABLES = 500
FARGATE_ABOVE_BYTES = 50 * 2**30


def verify_runtime(
    options: dict[str, Any], connections: dict[str, Any], baseline: dict[str, Any] | None, images: list[str]
) -> str | None:
    """Where the target check runs: ``lambda`` (Data API, default for a
    target that fits a 15-minute Lambda), ``fargate`` (psql in the sql-runner image), or None (no
    check: no Data API and no image). ``auto`` takes Fargate when the last baseline had more than
    ``fargateAboveTables`` tables or a database above ``fargateAboveBytes``, or the target has no
    Data API."""
    mode = options.get("verifyRuntime", "auto")
    data_api = bool((connections.get("target") or {}).get("dataApiClusterArn"))
    has_image = "sql-runner" in images
    if mode == "fargate":
        if not has_image:
            raise InvalidRunRequest("preflight.verifyRuntime=fargate needs the sql-runner image (fargate.images)")
        return "fargate"
    if mode == "lambda":
        return "lambda" if data_api else None
    summary = (baseline or {}).get("summary") or {}
    big = int(summary.get("tables") or 0) > int(options.get("fargateAboveTables", FARGATE_ABOVE_TABLES)) or int(
        summary.get("databaseBytes") or 0
    ) > int(options.get("fargateAboveBytes", FARGATE_ABOVE_BYTES))
    if has_image and (big or not data_api):
        return "fargate"
    return "lambda" if data_api else None


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

    ``pg-dump`` (default): pg_dump of the scope streamed to S3 (Fargate sql-runner).
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
    return [
        {
            "name": "dump-target",
            "index": 0,
            "runtime": "fargate",
            "image": "sql-runner",
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


def assessment_settings(registered: dict[str, Any] | None, asked: dict[str, Any]) -> dict[str, Any]:
    """DMS premigration assessment for this run: module settings + request override."""
    enabled = bool(asked.get("enabled", (registered or {}).get("enabled", False)))
    if not registered:
        if enabled:
            raise InvalidRunRequest(
                "assessment requested, but the module has no assessment settings (variable assessment)"
            )
        return {"enabled": False}
    fail_on_warning = bool(asked.get("failOnWarning", registered.get("failOnWarning", True)))
    return {**registered, "enabled": enabled, "failOnWarning": fail_on_warning}


def build(request: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    try:
        validation.validate("run_request", request)
    except validation.ValidationError as err:
        raise InvalidRunRequest(str(err)) from err
    req = validation.with_defaults("run_request", request)

    reg = load_registration(req["project"], req["taskKey"])
    defaults = reg.get("defaults") or {}

    req["completion"] = req.get("completion") or defaults.get("completion", "auto")
    req["failOnTableErrors"] = bool(req.get("failOnTableErrors", defaults.get("failOnTableErrors", True)))
    req["targetReset"] = {
        "policy": (req.get("targetReset") or {}).get("policy") or defaults.get("targetResetPolicy", "if-interrupted")
    }
    auto = req.get("autoResume") or {}
    req["autoResume"] = {
        "maxAttempts": int(auto.get("maxAttempts", defaults.get("autoResumeMaxAttempts", 1))),
        "onlyCategories": auto.get("onlyCategories")
        or defaults.get("autoResumeCategories")
        or DEFAULT_AUTO_RESUME_CATEGORIES,
    }

    bucket = aws.env("ARTIFACTS_BUCKET")
    jobs_cfg = reg.get("jobs") or {}
    jobs_prefix = jobs_cfg.get("s3Prefix") or f"jobs/{req['project']}"
    jobs_version = req.get("jobsVersion") or jobs_cfg.get("defaultVersion")
    jobs = load_jobs(bucket, jobs_prefix, jobs_version, req["jobSet"]) if jobs_version else jobset.empty_jobs()
    options = req.get("preflight") or {}
    baseline = Store(aws.table()).get_target_baseline(f"{req['project']}/{req['taskKey']}")
    images = json.loads(aws.env("FARGATE_IMAGES", "[]"))
    runtime = verify_runtime(options, reg.get("connections") or {}, baseline, images)
    jobs["preflight"] = preflight_jobs(runtime, options)
    jobs["dump"] = dump_jobs(req.get("dump") or {}, defaults)
    if any(j.get("handler") == "builtin:dump/cluster_snapshot" for j in jobs["dump"]):
        post = list(jobs.get("post") or [])
        jobs["post"] = [*post, snapshot_retention_job(len(post), int(defaults.get("dumpRetentionDays", 14)))]
    req["jobsVersion"] = jobs_version

    run_id = execution["name"]
    ctx = {
        "environment": reg.get("environment") or aws.env("ENVIRONMENT", ""),
        "project": req["project"],
        "taskKey": req["taskKey"],
        "taskArn": reg["taskArn"],
        "taskResourceId": reg.get("taskResourceId"),
        "replicationInstanceArn": reg.get("replicationInstanceArn"),
        "logGroupName": reg.get("logGroupName"),
        "endpointArns": reg.get("endpointArns") or [],
        "connections": reg.get("connections") or {},
        "artifactsBucket": bucket,
        "jobsVersion": jobs_version,
        "jobsS3Prefix": f"s3://{bucket}/{jobs_prefix.strip('/')}/{jobs_version}/" if jobs_version else None,
        "jobs": jobs,
        "runTimeoutSeconds": int(defaults.get("runTimeoutSeconds", 604800)),
        "executionArn": execution.get("arn"),
        "assessment": assessment_settings(reg.get("assessment"), req.get("assessment") or {}),
    }
    return {"runId": run_id, "request": req, "ctx": ctx, "attempt": int(req.get("attempt") or 1)}


def context_key(project: str, run_id: str) -> str:
    """Saved by migrate (``SaveContext``) for post: request, ctx, inspect, jobCtx, attempt, startedAt.

    migrate records the S3 version it wrote on ``RUN#`` (``RecordContextVersion``);
    post loads exactly that version (:func:`load_saved_context`).
    """
    return f"{run_prefix(project, run_id)}/context.json"


def load_saved_context(run_id: str) -> dict[str, Any]:
    """The context.json version migrate recorded, under the project recorded on ``RUN#``.

    Review finding 1: a later write to the key (by anything that can write the prefix)
    is never read; neither is the prefix of a project named in the post input.
    """
    store = Store(aws.table())
    run = store.get_run(run_id)
    if not run:
        raise InvalidRunRequest(f"no run {run_id}")
    version = run.get("contextVersionId")
    if not version or version == "null":
        raise ContextNotPinned(
            f"run {run_id} has no contextVersionId: migrate did not record the context it saved "
            "(the artifacts bucket must be versioned)"
        )
    bucket = aws.env("ARTIFACTS_BUCKET")
    key = context_key(run.get("project"), run_id)
    try:
        body = aws.client("s3").get_object(Bucket=bucket, Key=key, VersionId=str(version))["Body"].read()
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "NoSuchVersion", "404", "400", "InvalidArgument", "AccessDenied"):
            raise ContextNotPinned(
                f"no saved context for run {run_id} at s3://{bucket}/{key} version {version} ({code})"
            ) from err
        raise
    saved = json.loads(body)
    if isinstance(saved, str):
        # Written by an older migrate with a string Body, which the S3 integration encoded again.
        saved = json.loads(saved)
    if not isinstance(saved, dict) or saved.get("runId") != run_id:
        raise ContextNotPinned(f"the saved context at s3://{bucket}/{key} is not the context of run {run_id}")
    return saved


def _outcome_fields(outcome: dict[str, Any], by: str) -> tuple[Any, Any]:
    if outcome.get("kind") == "SUCCESS":
        return {
            "outcome": outcome.get("outcome"),
            "cause": outcome.get("cause"),
            "dms": outcome.get("task") or {},
            "completedBy": by,
        }, None
    return None, {
        "Error": outcome.get("error") or "DmsTaskFailed",
        "Cause": json.dumps(
            {"message": outcome.get("cause") or "no outcome", "dms": outcome.get("task") or {}, "completedBy": by},
            default=str,
        ),
    }


def repeat_post(data: dict[str, Any], saved: dict[str, Any], execution: dict[str, Any], by: str) -> dict[str, Any]:
    """Operator repeats post: DMS is asked again, the lock is taken for this
    execution, and post jobs already ``ok`` before ``fromJob`` are skipped."""
    from shared import dms_outcome, handoff

    run_id, ctx = saved["runId"], dict(saved["ctx"])
    store = Store(aws.table())
    lock = store.get_lock(ctx["taskArn"])
    if lock and lock.get("runId") != run_id:
        raise RunAlreadyActive(f"run {lock.get('runId')} holds the lock of {ctx['taskArn']}")
    if lock and lock.get("executionArn") != execution.get("arn"):
        status = aws.client("stepfunctions").describe_execution(executionArn=lock["executionArn"])["status"]
        if status == "RUNNING":
            raise RunAlreadyActive(f"post of run {run_id} is still running ({lock['executionArn']})")
    task = handoff.describe_task(aws.client("dms"), ctx["taskArn"])
    outcome = dms_outcome.evaluate_outcome(
        task, (saved.get("inspect") or {}).get("completionSpec") or {}, started_at=saved.get("startedAt")
    )
    if outcome.kind == "PENDING":
        raise InvalidRunRequest(f"the DMS task is still {(task or {}).get('Status')}: post comes with its event")

    if data.get("jobsVersion"):
        # A fixed job set version (S13: a post job failed, the job set was corrected).
        version = str(data["jobsVersion"])
        bucket = aws.env("ARTIFACTS_BUCKET")
        prefix = f"jobs/{ctx['project']}"
        if ctx.get("jobsS3Prefix"):
            prefix = ctx["jobsS3Prefix"].removeprefix(f"s3://{bucket}/").rstrip("/").rsplit("/", 1)[0]
        fresh = load_jobs(bucket, prefix, version, (saved.get("request") or {}).get("jobSet", "default"))
        ctx["jobs"] = {**(ctx.get("jobs") or {}), "post": fresh["post"], "on_failure": fresh["on_failure"]}
        ctx["jobsVersion"] = version
        ctx["jobsS3Prefix"] = f"s3://{bucket}/{prefix}/{version}/"
    jobs = (ctx.get("jobs") or {}).get("post") or []
    names = [j["name"] for j in jobs]
    from_job = data.get("fromJob")
    if from_job and from_job not in names:
        raise InvalidRunRequest(f"fromJob {from_job!r} is not a post job of run {run_id} (have: {names})")
    ok = {j.get("name") for j in store.job_results(run_id) if j.get("phase") == "post" and j.get("status") == "ok"}
    start = names.index(from_job) if from_job else 0
    selected = [j for i, j in enumerate(jobs) if i >= start or j["name"] not in ok]
    ctx["jobs"] = {**(ctx.get("jobs") or {}), "post": selected}
    ctx["postExecutionArn"] = execution.get("arn")

    store.table.put_item(
        Item={
            "pk": f"LOCK#{ctx['taskArn']}",
            "sk": "LOCK",
            "entity": "LOCK",
            "runId": run_id,
            "executionArn": execution.get("arn"),
            "project": ctx.get("project"),
            "taskKey": ctx.get("taskKey"),
            "acquiredAt": now_iso(),
            "gsi2pk": "LOCK#ACTIVE",
            "gsi2sk": execution.get("arn"),
        }
    )
    dms_out, failure = _outcome_fields(outcome.to_dict(), by)
    return {
        "runId": run_id,
        "request": saved["request"],
        "ctx": ctx,
        "inspect": saved.get("inspect"),
        "jobCtx": {
            **(saved.get("jobCtx") or {}),
            "jobsS3Prefix": ctx.get("jobsS3Prefix"),
            "jobsVersion": ctx.get("jobsVersion"),
        },
        "attempt": int(saved.get("attempt") or 1),
        "startedAt": saved.get("startedAt"),
        "preflight": saved.get("preflight"),
        "startedBy": by,
        "dmsOutcome": dms_out,
        "failure": failure,
        "skippedJobs": [n for n in names if n not in {j["name"] for j in selected}],
    }


def load_post(event: dict[str, Any]) -> dict[str, Any]:
    """post mode: the run migrate saved, plus the DMS outcome the handoff passed in (ADR 0005)."""
    data = event.get("input") or {}
    execution = event.get("execution") or {}
    run_id = str(data.get("runId") or "")
    if not run_id:
        raise InvalidRunRequest("post needs runId")
    saved = load_saved_context(run_id)
    by = data.get("startedBy") or "unknown"
    if data.get("operation") == "post":
        return repeat_post(data, saved, execution, by)
    outcome = data.get("outcome") or {}
    dms_outcome = failure = None
    if outcome.get("kind") == "SUCCESS":
        dms_outcome = {
            "outcome": outcome.get("outcome"),
            "cause": outcome.get("cause"),
            "dms": outcome.get("task") or {},
            "completedBy": by,
        }
    else:
        failure = {
            "Error": outcome.get("error") or "DmsTaskFailed",
            "Cause": json.dumps(
                {"message": outcome.get("cause") or "no outcome", "dms": outcome.get("task") or {}, "completedBy": by},
                default=str,
            ),
        }
    ctx = {**saved["ctx"], "postExecutionArn": execution.get("arn")}
    return {
        "runId": run_id,
        "request": saved["request"],
        "ctx": ctx,
        "inspect": saved.get("inspect"),
        "jobCtx": saved.get("jobCtx"),
        "attempt": int(saved.get("attempt") or 1),
        "startedAt": saved.get("startedAt"),
        "preflight": saved.get("preflight"),
        "startedBy": by,
        "dmsOutcome": dms_outcome,
        "failure": failure,
    }


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    execution = event.get("execution") or {}
    if event.get("mode") == "post":
        result = load_post(event)
        store = Store(aws.table())
        store.update_run(
            result["runId"], {"postExecutionArn": execution.get("arn"), "phase": "post", "status": "RUNNING"}
        )
        store.append_event(
            result["runId"],
            "sfn",
            "post",
            {"startedBy": result["startedBy"], "failure": (result["failure"] or {}).get("Error")},
        )
        log.info(LOGGER, "post context loaded", runId=result["runId"], startedBy=result["startedBy"])
        return result
    result = build(event.get("request") or {}, execution)
    req, ctx = result["request"], result["ctx"]
    store = Store(aws.table())
    store.put_run(
        result["runId"],
        {
            "project": req["project"],
            "taskKey": req["taskKey"],
            "taskArn": ctx["taskArn"],
            "operation": req["operation"],
            "dryRun": req["dryRun"],
            "status": "RUNNING",
            "startedAt": execution.get("startTime") or now_iso(),
            "executionArn": execution.get("arn"),
            "requestedBy": req.get("requestedBy"),
            "changeRef": req.get("changeRef"),
            "jobsVersion": req.get("jobsVersion"),
        },
    )
    store.append_event(result["runId"], "sfn", "resolve", {"request": req})
    log.info(LOGGER, "context resolved", runId=result["runId"], taskArn=ctx["taskArn"], operation=req["operation"])
    return result

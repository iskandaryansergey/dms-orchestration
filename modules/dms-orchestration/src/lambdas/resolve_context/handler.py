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
from shared.errors import ContextNotPinned, InvalidRunRequest, RunAlreadyActive
from shared.module_jobs import dump_jobs, preflight_jobs, snapshot_retention_job
from shared.registry import load_jobs, load_registration
from shared.store import Store, now_iso, run_prefix

LOGGER = log.get_logger("resolve_context")
DEFAULT_AUTO_RESUME_CATEGORIES = ["TRANSIENT_SOURCE_CONN", "TRANSIENT_TARGET_CONN", "DMS_RESTART"]

FARGATE_ABOVE_TABLES = 500
FARGATE_ABOVE_BYTES = 50 * 2**30


def verify_runtime(
    options: dict[str, Any], connections: dict[str, Any], baseline: dict[str, Any] | None, images: list[str]
) -> str | None:
    """Where the target check runs: ``lambda`` (``builtin:checks/target_verify``: pg8000 when the
    target connection has a host, the Data API otherwise; default for a target that fits a
    15-minute Lambda), ``fargate`` (psql in the sql-runner image), or None (no check: the Lambda
    cannot reach the target and there is no image). ``auto`` takes Fargate when the last baseline
    had more than ``fargateAboveTables`` tables or a database above ``fargateAboveBytes``, or the
    Lambda cannot reach the target."""
    mode = options.get("verifyRuntime", "auto")
    target = connections.get("target") or {}
    reachable = bool(target.get("host") or target.get("dataApiClusterArn"))
    has_image = "sql-runner" in images
    if mode == "fargate":
        if not has_image:
            raise InvalidRunRequest("preflight.verifyRuntime=fargate needs the sql-runner image (fargate.images)")
        return "fargate"
    if mode == "lambda":
        return "lambda" if reachable else None
    summary = (baseline or {}).get("summary") or {}
    big = int(summary.get("tables") or 0) > int(options.get("fargateAboveTables", FARGATE_ABOVE_TABLES)) or int(
        summary.get("databaseBytes") or 0
    ) > int(options.get("fargateAboveBytes", FARGATE_ABOVE_BYTES))
    if has_image and (big or not reachable):
        return "fargate"
    return "lambda" if reachable else None


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


CHAIN_FIELDS = ("attempt", "parentRunId", "parentStartedAt")


def resolve_chain(req: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    """The dump already taken in this chain of runs, for a chained resume only.

    ``attempt``, ``parentRunId`` and ``parentStartedAt`` skip the pre-jobs, the dump and maybe
    the start (review finding 5), so they are accepted only from the execution that post named on
    the parent run when it handed the lock over (``RUN#<parentRunId>.resumeChildRunId``, written
    by ``HandOverLock``); an operator who sets them gets InvalidRunRequest.
    """
    if not any(req.get(f) is not None for f in CHAIN_FIELDS):
        return {"dumped": False, "dumpUri": None}
    parent_id, name = req.get("parentRunId"), execution.get("name")
    if not parent_id or not req.get("attempt") or int(req["attempt"]) < 2:
        raise InvalidRunRequest("attempt, parentRunId and parentStartedAt are set by post for a chained resume only")
    parent = Store(aws.table()).get_run(str(parent_id)) or {}
    if (
        not name
        or parent.get("resumeChildRunId") != name
        or parent.get("project") != req["project"]
        or parent.get("taskKey") not in (None, req["taskKey"])
    ):
        raise InvalidRunRequest(
            f"run {parent_id} did not hand over to {name}: attempt, parentRunId and parentStartedAt "
            "are set by post for a chained resume only"
        )
    return {"dumped": bool(parent.get("chainDumped")), "dumpUri": parent.get("dumpUri")}


def build(request: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    try:
        validation.validate("run_request", request)
    except validation.ValidationError as err:
        raise InvalidRunRequest(str(err)) from err
    req = validation.with_defaults("run_request", request)
    chain = resolve_chain(req, execution)

    reg = load_registration(req["project"], req["taskKey"])
    defaults = reg.get("defaults") or {}

    req["completion"] = req.get("completion") or defaults.get("completion", "auto")
    req["failOnTableErrors"] = bool(req.get("failOnTableErrors", defaults.get("failOnTableErrors", True)))
    req["failOnValidationErrors"] = bool(
        req.get("failOnValidationErrors", defaults.get("failOnValidationErrors", True))
    )
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
    jobs, jobs_bundle = (
        load_jobs(bucket, jobs_prefix, jobs_version, req["jobSet"], jobs_cfg)
        if jobs_version
        else (jobset.empty_jobs(), None)
    )
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
        "jobsBundle": jobs_bundle,
        "runTimeoutSeconds": int(defaults.get("runTimeoutSeconds", 604800)),
        "executionArn": execution.get("arn"),
        "assessment": assessment_settings(reg.get("assessment"), req.get("assessment") or {}),
        "chain": chain,
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
        task,
        (saved.get("inspect") or {}).get("completionSpec") or {},
        started_at=saved.get("startedAt"),
        table_stats=lambda: handoff.table_statistics(aws.client("dms"), ctx["taskArn"]),
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
        jobs_cfg = load_registration(ctx["project"], ctx["taskKey"]).get("jobs") or {}
        fresh, fresh_bundle = load_jobs(
            bucket, prefix, version, (saved.get("request") or {}).get("jobSet", "default"), jobs_cfg
        )
        ctx["jobs"] = {**(ctx.get("jobs") or {}), "post": fresh["post"], "on_failure": fresh["on_failure"]}
        ctx["jobsBundle"] = fresh_bundle
        ctx["jobsVersion"] = version
        ctx["jobsS3Prefix"] = f"s3://{bucket}/{prefix}/{version}/"
    jobs = (ctx.get("jobs") or {}).get("post") or []
    names = [j["name"] for j in jobs]
    from_job = data.get("fromJob")
    if from_job and from_job not in names:
        raise InvalidRunRequest(f"fromJob {from_job!r} is not a post job of run {run_id} (have: {names})")
    # A job is done only if its ok was recorded with the run's own flag: a JOB# written before the
    # dryRun fix by a dry re-run of a real run's job (run 11, finding 2) is not the run's result.
    dry = bool((saved.get("request") or {}).get("dryRun", False))
    ok = {
        j.get("name")
        for j in store.job_results(run_id)
        if j.get("phase") == "post" and j.get("status") == "ok" and bool(j.get("dryRun", False)) == dry
    }
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
        # jobsVersion: a repeat of post may take a corrected version; prepare_job looks every job
        # up in the version recorded here, never in the one the job-runner input names.
        store.update_run(
            result["runId"],
            {
                "postExecutionArn": execution.get("arn"),
                "phase": "post",
                "status": "RUNNING",
                "jobsVersion": result["ctx"].get("jobsVersion"),
            },
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
            "parentRunId": req.get("parentRunId"),
            "chainDumped": ctx["chain"]["dumped"],
            "dumpUri": ctx["chain"]["dumpUri"],
        },
    )
    store.append_event(result["runId"], "sfn", "resolve", {"request": req})
    log.info(LOGGER, "context resolved", runId=result["runId"], taskArn=ctx["taskArn"], operation=req["operation"])
    return result

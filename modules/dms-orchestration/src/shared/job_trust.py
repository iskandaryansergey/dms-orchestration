"""Which job a job-runner execution may run (review item N1).

The job-runner's input (``job``, ``ctx``, ``runId``) is whatever its caller passed. migrate and post
pass jobs of the run's pinned job set and the module's own jobs; the operator role may start the
job-runner too (``orch.py restore``). So prepare_job trusts none of it:

* a run phase needs a ``RUN#`` item that is ``RUNNING`` (the operator cannot write ``RUN#``) of the
  project and task the input names; the job is either looked up again by name in the job set
  version ``ctx.jobsVersion`` (its bundle hash checked against the registration, never against the
  input) and must have the same code there, or is one of the module's own jobs
  (:mod:`shared.module_jobs`) with exactly its code;
* the restore phase accepts only the two jobs ``orch.py restore`` builds, ``pg-restore-s3`` of a
  dump of that very task and the target check that records the restore baseline;
* project, task, task ARN and connections come from the registration; ``dryRun`` is the one on
  ``RUN#``, an input that says otherwise is refused (a dry re-run cannot overwrite a real run's
  ``JOB#`` record, a real re-run cannot write in a dry run).

Anything else is ``JobNotInJobSet``; nothing has been written by then.
"""

from __future__ import annotations

import re
from typing import Any

from shared import aws, jobset, module_jobs, preflight
from shared.errors import JobNotInJobSet
from shared.registry import load_jobs, load_registration
from shared.store import Store

RUN_PHASES = ("preflight", "baseline", "pre", "dump", "reset", "post", "on_failure")
RUN_DECISION_FIELDS = ("startType", "resetScope", "resetTables", "resetTablesCount", "resetTablesUri")
CODE_FIELDS = ("runtime", "image", "handler", "function", "command", "sql", "with", "when")
# with: values of the module's jobs a run may choose (pre-flight request option). Everything else
# is fixed: waitSeconds is the module's, retentionDays at least the registration's
# dumpRetentionDays (a shorter one would delete the task's snapshots early).
FREE_INT_WITH = frozenset({"exactCountMaxRows"})
DEFAULT_RETENTION_DAYS = 14
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_URI_SAFE = re.compile(r"^[A-Za-z0-9._:/=+-]+$")

RESTORE_COMMAND = ("pg-restore-s3", "--connection", "target", "--from")
RESTORE_JOB: dict[str, Any] = {
    "name": "restore-target",
    "index": 0,
    "timeoutSeconds": 21600,
    "retry": {"maxAttempts": 1, "backoffSeconds": 60},
    "idempotent": False,
    "onError": "fail",
}
RESTORE_VERIFY_JOB: dict[str, Any] = {
    "name": "target-verify",
    "index": 0,
    "runtime": "lambda",
    "handler": "builtin:checks/target_verify",
    "timeoutSeconds": 600,
    "retry": {"maxAttempts": 2, "backoffSeconds": 30},
    "idempotent": True,
    "onError": "fail",
}


def _refuse(message: str) -> JobNotInJobSet:
    return JobNotInJobSet(message)


def _name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        raise _refuse(f"{what} {value!r} is not a valid name")
    return value


def _index(job: dict[str, Any]) -> int:
    index = job.get("index", 0)
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 1000:
        raise _refuse(f"job {job.get('name')!r}: bad index {index!r}")
    return index


def _code(job: dict[str, Any]) -> dict[str, Any]:
    return {k: job.get(k) for k in CODE_FIELDS}


def _positive_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _same_module_code(job: dict[str, Any], canon: dict[str, Any], min_retention: int) -> bool:
    if any(job.get(k) != canon.get(k) for k in CODE_FIELDS if k != "with"):
        return False
    given, fixed = job.get("with") or {}, canon.get("with") or {}
    if not isinstance(given, dict) or set(given) != set(fixed):
        return False
    for key, value in given.items():
        if key in FREE_INT_WITH:
            if not _positive_int(value):
                return False
        elif key == "retentionDays":
            if not _positive_int(value) or value < min_retention:
                return False
        elif value != fixed[key]:
            return False
    return True


def _module_candidates(phase: str) -> list[dict[str, Any]]:
    if phase in ("preflight", "baseline"):
        return module_jobs.preflight_jobs("lambda", {}) + module_jobs.preflight_jobs("fargate", {})
    if phase == "dump":
        return module_jobs.dump_jobs({"enabled": True}, {}) + module_jobs.dump_jobs(
            {"enabled": True, "method": "snapshot"}, {"snapshotDumps": True}
        )
    if phase == "post":
        return [module_jobs.snapshot_retention_job(0, 1)]
    return []


def module_job(phase: str, job: dict[str, Any], reg: dict[str, Any]) -> dict[str, Any] | None:
    """The module's own job of this name and phase when ``job`` has exactly its code, else None."""
    defaults = reg.get("defaults") or {}
    min_retention = int(defaults.get("dumpRetentionDays") or DEFAULT_RETENTION_DAYS)
    for canon in _module_candidates(phase):
        if canon["name"] == job.get("name") and _same_module_code(job, canon, min_retention):
            return {**canon, "index": _index(job), "with": dict(job.get("with") or {})}
    return None


def job_set_job(phase: str, job: dict[str, Any], project: str, version: Any, reg: dict[str, Any]) -> dict[str, Any]:
    """The job of this name in the pinned job set version, checked to have the same code."""
    name = _name(job.get("name"), "job")
    if phase not in jobset.PHASES:
        raise _refuse(f"job {name!r} is not a job of the module for phase {phase!r}")
    if not version:
        raise _refuse(f"job {name!r}: the run has no job set, and this is not a job of the module")
    version = _name(version, "job set version")
    job_set = _name(job.get("jobSet") or "default", "job set")
    cfg = reg.get("jobs") or {}
    prefix = cfg.get("s3Prefix") or f"jobs/{project}"
    jobs, _ = load_jobs(aws.env("ARTIFACTS_BUCKET"), prefix, version, job_set, cfg)
    pinned = next((j for j in jobs.get(phase) or [] if j.get("name") == name), None)
    if pinned is None:
        raise _refuse(f"job {name!r} is not a {phase} job of job set {job_set!r} version {version!r}")
    if _code(pinned) != _code(job):
        raise _refuse(f"job {name!r} differs from the pinned job set {job_set!r} version {version!r}")
    return pinned


def _jobs_prefix(reg: dict[str, Any], project: str, version: Any) -> str | None:
    if not version:
        return None
    prefix = (reg.get("jobs") or {}).get("s3Prefix") or f"jobs/{project}"
    return f"s3://{aws.env('ARTIFACTS_BUCKET')}/{prefix.strip('/')}/{_name(version, 'job set version')}/"


def _run_phase(event: dict[str, Any], store: Store) -> dict[str, Any]:
    run_id, phase = event.get("runId"), event["phase"]
    given = event.get("ctx") or {}
    run = store.get_run(str(run_id)) if run_id else None
    if not run:
        raise _refuse(f"no run {run_id!r}: the job-runner runs jobs of a migrate or post run only")
    if run.get("status") != "RUNNING":
        raise _refuse(f"run {run_id} is not running ({run.get('status')})")
    project, task = run.get("project"), run.get("taskKey")
    if given.get("project") != project or given.get("taskKey") != task:
        raise _refuse(
            f"run {run_id} is a run of {project}/{task}, not of {given.get('project')}/{given.get('taskKey')}"
        )
    # The run's own flag: a dry re-run of a job of a real run would overwrite the run's JOB#
    # record (a post retry skips ok jobs), a real re-run of a dry run's job would write.
    dry_run = run.get("dryRun") is True
    given_dry = event.get("dryRun")
    if given_dry is not None and given_dry is not dry_run:
        raise _refuse(f"dryRun {given_dry!r} is not the dryRun of run {run_id} ({dry_run})")
    reg = load_registration(str(project), str(task))
    job = event.get("job") or {}
    version = run.get("jobsVersion")
    if given.get("jobsVersion") not in (None, version):
        raise _refuse(f"job set version {given.get('jobsVersion')!r} is not the version of run {run_id} ({version!r})")
    trusted = module_job(phase, job, reg) or job_set_job(phase, job, str(project), version, reg)
    # Only these fields, each from the registration or what the module recorded on RUN#; nothing
    # of the input's ctx is kept (review item N1).
    inspected, decision = run.get("jobInspect") or {}, run.get("jobDecision") or {}
    ctx = {
        "project": project,
        "taskKey": task,
        "environment": reg.get("environment") or aws.env("ENVIRONMENT", ""),
        "taskArn": reg["taskArn"],
        "replicationInstanceArn": reg.get("replicationInstanceArn"),
        "endpointArns": reg.get("endpointArns") or [],
        "connections": reg.get("connections") or {},
        "jobsS3Prefix": _jobs_prefix(reg, str(project), version),
        "jobsVersion": version,
        "attempt": int(run.get("attempt") or 1),
        "migrationType": inspected.get("migrationType"),
        "selectionRules": inspected.get("selectionRules"),
        **{k: decision.get(k) for k in RUN_DECISION_FIELDS},
    }
    if not ctx["resetTables"]:
        ctx["resetTables"] = []
    return {**event, "job": trusted, "ctx": ctx, "dryRun": dry_run}


def _dump_uri(value: Any, project: str, task: str) -> str:
    dumps = f"s3://{aws.env('ARTIFACTS_BUCKET')}/dumps/{project}/{task}/"
    if (
        not isinstance(value, str)
        or not value.startswith(dumps)
        or len(value) == len(dumps)
        or not _URI_SAFE.match(value)
        or any(p in ("", ".", "..") for p in value[len(dumps) :].rstrip("/").split("/"))
    ):
        raise _refuse(f"restore: {value!r} is not a dump of {project}/{task} ({dumps}...)")
    return value


def _restore_job(job: dict[str, Any], project: str, task: str) -> dict[str, Any]:
    if job.get("name") == "restore-target":
        command = job.get("command")
        if (
            not isinstance(command, list)
            or len(command) != 6
            or tuple(command[:4]) != RESTORE_COMMAND
            or command[5] != "--confirm"
            or any(job.get(k) for k in ("handler", "function", "sql", "with", "when"))
        ):
            raise _refuse("restore: restore-target runs pg-restore-s3 --connection target --from <dump> --confirm only")
        uri = _dump_uri(command[4], project, task)
        if job.get("runtime") == "ssm":
            where: dict[str, Any] = {"runtime": "ssm"}
        elif job.get("runtime") == "fargate" and job.get("image") in (None, "sql-runner"):
            where = {"runtime": "fargate", "image": "sql-runner"}
        else:
            raise _refuse(f"restore: runtime {job.get('runtime')!r} (ssm or fargate)")
        return {**RESTORE_JOB, **where, "command": [*RESTORE_COMMAND, uri, "--confirm"]}
    if job.get("name") == "target-verify":
        with_ = job.get("with") or {}
        record = with_.get("recordBaseline") if isinstance(with_, dict) else None
        if (
            job.get("runtime") != "lambda"
            or job.get("handler") != RESTORE_VERIFY_JOB["handler"]
            or any(job.get(k) for k in ("function", "command", "sql", "when"))
            or set(with_) != {"connection", "source", "recordBaseline"}
            or with_.get("connection") != "target"
            or with_.get("source") is not None
            or not isinstance(record, dict)
            or set(record) != {"runId", "dumpUri"}
            or not (record["runId"] is None or isinstance(record["runId"], str))
        ):
            raise _refuse("restore: target-verify checks the target and records the restore baseline only")
        run_of_dump = record["runId"] if record["runId"] is None else _name(record["runId"], "dump run")
        baseline = {"runId": run_of_dump, "dumpUri": _dump_uri(record["dumpUri"], project, task)}
        return {**RESTORE_VERIFY_JOB, "with": {"connection": "target", "source": None, "recordBaseline": baseline}}
    raise _refuse(f"restore: job {job.get('name')!r} is not a restore job of the module")


def _task_selection_rules(task_arn: str) -> list[dict[str, str]]:
    """The scope of the restore baseline: the DMS task's table mapping, never the caller's rules."""
    tasks = (
        aws.client("dms")
        .describe_replication_tasks(Filters=[{"Name": "replication-task-arn", "Values": [task_arn]}])
        .get("ReplicationTasks")
        or []
    )
    if not tasks:
        raise _refuse(f"restore: DMS task {task_arn} not found")
    return preflight.selection_rules(tasks[0].get("TableMappings") or "{}")


def _hold_by_this_restore(store: Store, task_arn: str, run_id: str, execution_arn: Any) -> None:
    """orch.py restore holds LOCK#<taskArn> with its restore id and the job-runner execution it
    started (TaskLock): a restore started any other way, or while a run holds the lock, is refused."""
    lock = store.get_lock(task_arn) or {}
    if not execution_arn or lock.get("runId") != run_id or lock.get("executionArn") != execution_arn:
        raise _refuse(
            f"restore: the task lock of {task_arn} is not held by this restore execution "
            f"(held by {lock.get('runId')!r}); restores run through orch.py restore"
        )


def _restore(event: dict[str, Any], store: Store) -> dict[str, Any]:
    run_id = str(event.get("runId") or "")
    if not run_id.startswith("restore-") or not _NAME.match(run_id):
        raise _refuse(f"restore: run id {run_id!r} is not a restore-… id (orch.py restore)")
    # The operator's flag (no RUN# here), but only a real boolean: prepare_job does bool(dryRun),
    # so the string "false" would become a dry run. Identity, since 0 == False and 1 == True.
    dry = event.get("dryRun")
    if dry is not None and dry is not True and dry is not False:
        raise _refuse(f"restore: dryRun {dry!r} is not true, false or absent")
    given = event.get("ctx") or {}
    project = _name(given.get("project"), "project")
    task = _name(given.get("taskKey"), "task")
    reg = load_registration(project, task)
    job = _restore_job(event.get("job") or {}, project, task)
    _hold_by_this_restore(store, reg["taskArn"], run_id, event.get("executionArn"))
    ctx: dict[str, Any] = {
        "project": project,
        "taskKey": task,
        "taskArn": reg["taskArn"],
        "environment": reg.get("environment") or aws.env("ENVIRONMENT", ""),
        "connections": reg.get("connections") or {},
    }
    if job["name"] == RESTORE_VERIFY_JOB["name"]:
        ctx["selectionRules"] = _task_selection_rules(reg["taskArn"])
    return {**event, "job": job, "ctx": ctx, "failure": None}


def authorize(event: dict[str, Any], store: Store) -> dict[str, Any]:
    """The event with the job and context prepare_job may use, or JobNotInJobSet."""
    phase = event.get("phase")
    if phase == "restore":
        return _restore(event, store)
    if phase not in RUN_PHASES:
        raise _refuse(f"unknown phase {phase!r}")
    return _run_phase(event, store)

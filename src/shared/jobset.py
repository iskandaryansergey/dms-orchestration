"""Job set loading and normalisation (shared by resolve_context and the CLI tool)."""

from __future__ import annotations

import copy
from typing import Any

from shared import validation
from shared.errors import InvalidJobSet

PHASES = ("pre", "reset", "post", "on_failure")
LAMBDA_MAX_TIMEOUT = 840  # keep below the 15-minute Lambda limit
BASE_DEFAULTS: dict[str, Any] = {
    "image": "sql-runner",
    "timeoutSeconds": 3600,
    "retry": {"maxAttempts": 1, "backoffSeconds": 60},
    "idempotent": True,
    "onError": "fail",
}


def empty_jobs() -> dict[str, list[dict[str, Any]]]:
    return {phase: [] for phase in PHASES}


def normalize(jobset: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Validate a job set and return jobs per phase with defaults merged in."""
    try:
        validation.validate("jobset", jobset)
    except validation.ValidationError as err:
        raise InvalidJobSet(str(err)) from err

    defaults = {**copy.deepcopy(BASE_DEFAULTS), **copy.deepcopy(jobset.get("defaults") or {})}
    defaults["retry"] = {**BASE_DEFAULTS["retry"], **(defaults.get("retry") or {})}
    out = empty_jobs()
    errors: list[str] = []
    for phase in PHASES:
        seen: set[str] = set()
        for index, raw in enumerate(jobset.get("phases", {}).get(phase) or []):
            job = {**copy.deepcopy(defaults), **copy.deepcopy(raw)}
            job["retry"] = {**defaults["retry"], **(raw.get("retry") or {})}
            name = job["name"]
            where = f"phases.{phase}[{index}] ({name})"
            if name in seen:
                errors.append(f"{where}: duplicate job name in phase")
            seen.add(name)
            if "runtime" not in raw:
                job["runtime"] = (
                    "lambda" if (raw.get("handler") or raw.get("function")) else defaults.get("runtime", "fargate")
                )
            if job["runtime"] == "lambda":
                if bool(job.get("handler")) == bool(job.get("function")):
                    errors.append(f"{where}: a lambda job needs exactly one of 'handler' or 'function'")
                if job["timeoutSeconds"] > LAMBDA_MAX_TIMEOUT:
                    job["timeoutSeconds"] = LAMBDA_MAX_TIMEOUT
            else:
                if not job.get("command"):
                    errors.append(f"{where}: a fargate job needs 'command'")
                if job.get("handler") or job.get("function"):
                    errors.append(f"{where}: 'handler'/'function' are only valid for lambda jobs")
            if not job.get("idempotent") and job["retry"]["maxAttempts"] > 1:
                errors.append(f"{where}: non-idempotent jobs cannot be retried automatically")
            job["phase"] = phase
            job["index"] = index
            out[phase].append(job)
    if errors:
        raise InvalidJobSet("; ".join(errors))
    return out


def jobset_file_name(job_set: str) -> str:
    return "jobset.json" if job_set == "default" else f"jobset.{job_set}.json"

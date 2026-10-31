"""Task registrations (SSM) and pinned job sets (S3), shared by resolve_context and prepare_job."""

from __future__ import annotations

import json
from typing import Any

from botocore.exceptions import ClientError

from shared import aws, jobset
from shared import bundle as bundle_hash
from shared.errors import JobSetNotFound, JobSetNotPinned, JobSetTampered, UnknownTask


def load_registration(project: str, task_key: str) -> dict[str, Any]:
    name = f"{aws.env('SSM_PREFIX').rstrip('/')}/{project}/{task_key}"
    try:
        value = aws.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == "ParameterNotFound":
            raise UnknownTask(f"no task registered as {project}/{task_key} (SSM parameter {name})") from err
        raise
    return json.loads(value)


def _get(bucket: str, key: str) -> bytes | None:
    try:
        return aws.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "AccessDenied"):
            return None
        raise


def load_jobs(
    bucket: str, prefix: str, version: str, job_set: str, jobs_cfg: dict[str, Any] | None = None
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any] | None]:
    """The job set of one version, checked against the bundle hash Terraform registered.

    ``jobs_cfg`` is the registration's ``jobs`` block: ``bundles`` maps a version to the total of
    its ``bundle.sha256`` (``jobset_tool.py hash``); ``allowUnpinned`` lets a version run without
    one (sandbox). A registered hash is always enforced. Returns the jobs, each stamped with
    ``bundleSha256`` for the runtimes, and ``{"sha256", "files", "pinned"}`` (None: no manifest,
    only when unpinned runs are allowed).
    """
    cfg = jobs_cfg or {}
    pinned = (cfg.get("bundles") or {}).get(version)
    if not pinned and not cfg.get("allowUnpinned"):
        raise JobSetNotPinned(
            f"job set version {version!r} has no bundle hash in the task registration "
            "(projects.<p>.tasks.<t>.jobs.bundles; jobset_tool.py hash <bundle-dir>)"
        )
    base = f"{prefix.strip('/')}/{version}"
    key = f"{base}/{jobset.jobset_file_name(job_set)}"
    manifest = _get(bucket, f"{base}/{bundle_hash.MANIFEST}")
    body = _get(bucket, key)
    if body is None:
        raise JobSetNotFound(f"job set not found at s3://{bucket}/{key} (run scripts/sync-jobs.sh)")
    pin: dict[str, Any] | None = None
    if manifest is None:
        if pinned:
            raise JobSetTampered(
                f"s3://{bucket}/{base}/ has no {bundle_hash.MANIFEST}, but version {version} is pinned"
            )
    else:
        files, total = bundle_hash.parse(manifest.decode("utf-8", errors="replace"))
        if pinned and total != pinned:
            raise JobSetTampered(
                f"s3://{bucket}/{base}/ does not match the bundle hash pinned for version {version} "
                f"(registered {pinned[:12]}…, found {total[:12]}…)"
            )
        bundle_hash.verify(jobset.jobset_file_name(job_set), body, files)
        pin = {"sha256": total, "files": files, "pinned": bool(pinned)}
    jobs = jobset.normalize(json.loads(body))
    for items in jobs.values():
        for job in items:
            job["bundleSha256"] = pin["sha256"] if pin else None
            # Which file of the version: prepare_job looks the job up there again (review item N1).
            job["jobSet"] = job_set
    return jobs, pin

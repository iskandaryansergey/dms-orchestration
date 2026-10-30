#!/usr/bin/env python3
"""Entrypoint of the sql-runner image: run one orchestration job inside Fargate.

The job-runner state machine starts the container with the job's ``command`` as
arguments and these environment variables:

* ``ORCH_JOB_SPEC_S3`` — s3://<bucket>/runs/<runId>/jobs/<phase>/<nn>-<name>/spec.json
* ``ORCH_RUN_ID``, ``ORCH_DRY_RUN`` (1/0), ``ORCH_ATTEMPT``

Steps: download the spec and the job bundle (``jobsS3Prefix``) into the work dir,
resolve connection secrets into a 0600 file (never into the environment or
logs), run the command, then upload ``result.json`` (and declared artifacts).
The container exit code is 0 only when the job status is ok or warn.

The command's output goes to the container log only, never into the result: a failed job's
message names the exit code and the CloudWatch log stream. In a dry run the command gets
``PGOPTIONS=-c default_transaction_read_only=on`` (libpq clients open read-only sessions).
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from shared import bundle  # image layout: /opt/orch/shared/bundle.py
from shared.errors import JobSetTampered

OK_STATUSES = {"ok", "warn"}
VALID_STATUSES = {"ok", "warn", "fail"}
READ_ONLY_PGOPTIONS = "-c default_transaction_read_only=on"


def split_s3(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3 uri: {uri}")
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def dirs() -> tuple[Path, Path, Path]:
    out = Path(os.environ.get("ORCH_OUT_DIR", "/out"))
    work = Path(os.environ.get("ORCH_WORK_DIR", "/work"))
    tmp = Path(os.environ.get("ORCH_TMP_DIR", "/tmp/orch"))  # noqa: S108 - container scratch volume
    for d in (out, work, tmp):
        d.mkdir(parents=True, exist_ok=True)
    return out, work, tmp


def _manifest(s3: Any, bucket: str, prefix: str) -> bytes | None:
    try:
        return s3.get_object(Bucket=bucket, Key=f"{prefix.rstrip('/')}/{bundle.MANIFEST}")["Body"].read()
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def download_bundle(s3: Any, prefix_uri: str | None, dest: Path, expected: str | None = None) -> int:
    """Copy s3://bucket/jobs/<project>/<version>/ into dest. Returns the file count.

    ``expected`` is the bundle hash the run pinned (spec ``bundleSha256``). Every file must
    match ``bundle.sha256``, files it does not list are refused, files it lists must exist
    (JobSetTampered). Without a pin a manifest found is still checked.
    """
    if not prefix_uri:
        return 0
    bucket, prefix = split_s3(prefix_uri)
    files = bundle.check_manifest(_manifest(s3, bucket, prefix), expected)
    root = dest.resolve()
    seen: set[str] = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix) :].lstrip("/")
            if not rel or rel.endswith("/") or rel == bundle.MANIFEST:
                continue
            target = (root / rel).resolve()
            if root not in target.parents:
                raise ValueError(f"refusing to write outside the work dir: {obj['Key']}")
            if files is not None and rel not in files:
                raise JobSetTampered(f"{rel} is not in the job set bundle ({bundle.MANIFEST})")
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, obj["Key"], str(target))
            if files is not None:
                try:
                    bundle.verify(rel, target.read_bytes(), files)
                except JobSetTampered:
                    target.unlink(missing_ok=True)
                    raise
            seen.add(rel)
    missing = sorted(set(files or {}) - seen)
    if missing:
        raise JobSetTampered(f"files listed in {bundle.MANIFEST} are missing: {', '.join(missing[:5])}")
    return len(seen)


def _rds_client() -> Any:
    return boto3.client("rds", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))


def iam_auth_token(conn: dict[str, Any]) -> str:
    """IAM database authentication token for one client start (valid 15 min).

    A local signature with the task role's credentials (no network call). The caller puts it
    into the client's environment only: never into a file, an argument or a log line."""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    return _rds_client().generate_db_auth_token(
        DBHostname=conn["host"], Port=int(conn.get("port") or 5432), DBUsername=conn["username"], Region=region
    )


def resolve_connections(secrets: Any, connections: dict[str, Any], path: Path) -> list[str]:
    """Write {name: {engine, host, port, database, username, password, sslMode, auth}} with mode 0600.

    ``auth = iam`` connections read no secret and hold no password: psql-run asks for a token
    when it starts the client (``iam_auth_token``)."""
    resolved: dict[str, Any] = {}
    for name, conn in (connections or {}).items():
        if conn.get("auth") == "iam":
            resolved[name] = {
                "engine": conn.get("engine"),
                "host": conn.get("host"),
                "port": conn.get("port"),
                "database": conn.get("database"),
                "username": conn.get("iamUser"),
                "password": None,
                "sslMode": conn.get("sslMode") or "verify-full",
                "auth": "iam",
            }
            continue
        secret = json.loads(secrets.get_secret_value(SecretId=conn["secretArn"])["SecretString"])
        resolved[name] = {
            "engine": conn.get("engine") or secret.get("engine"),
            "host": conn.get("host") or secret.get("host"),
            "port": conn.get("port") or secret.get("port"),
            "database": conn.get("database") or secret.get("dbname"),
            "username": secret.get("username"),
            "password": secret.get("password"),
            "sslMode": conn.get("sslMode") or "verify-full",
        }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(resolved, fh)
    return sorted(resolved)


def _kill(proc: subprocess.Popen[str]) -> None:
    """Kill the command and everything it started (bash -> psql keeps the pipe open)."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - local Windows runs only
            proc.kill()
    except ProcessLookupError:
        pass


def run_command(command: list[str], env: dict[str, str], cwd: Path, timeout: int) -> tuple[int, list[str]]:
    """Run the job command, stream its output, enforce ``timeout`` even if it prints nothing."""
    tail: collections.deque[str] = collections.deque(maxlen=40)
    proc = subprocess.Popen(  # noqa: S603 - command comes from the reviewed job set
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    assert proc.stdout is not None
    stdout = proc.stdout

    def pump() -> None:
        for line in stdout:
            sys.stdout.write(line)
            tail.append(line.rstrip("\n"))

    reader = threading.Thread(target=pump, name="job-output", daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        proc.wait(timeout=30)
        reader.join(timeout=5)
        tail.append(f"orch: killed after {timeout}s")
        return 124, list(tail)
    reader.join(timeout=30)
    return code, list(tail)


def _container_metadata(uri: str) -> dict[str, Any]:
    """Task metadata endpoint v4 of this container (link-local, no credentials)."""
    if not uri.startswith("http://"):
        raise ValueError(f"unexpected metadata uri: {uri}")
    with urllib.request.urlopen(uri, timeout=2) as resp:  # noqa: S310 - ECS-provided http uri
        return json.loads(resp.read())


def log_stream_name() -> str:
    """CloudWatch log stream of this job: ``ORCH_LOG_STREAM`` or the ECS container metadata."""
    if os.environ.get("ORCH_LOG_STREAM"):
        return os.environ["ORCH_LOG_STREAM"]
    uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if uri:
        try:
            stream = (_container_metadata(uri).get("LogOptions") or {}).get("awslogs-stream")
        except (OSError, ValueError, AttributeError):
            stream = None
        if stream:
            return str(stream)
    return "unavailable"


def build_result(exit_code: int, out_dir: Path, log_stream: str) -> dict[str, Any]:
    """The job result. Never holds command output: it reaches the execution Cause and the report."""
    produced: dict[str, Any] = {}
    result_file = out_dir / "result.json"
    if result_file.exists():
        try:
            produced = json.loads(result_file.read_text())
        except ValueError:
            produced = {"message": "result.json is not valid JSON"}
    status = produced.get("status") if produced.get("status") in VALID_STATUSES else "ok"
    message = str(produced.get("message") or "")
    if exit_code != 0:
        status = "fail"
        failed = f"exit code {exit_code} (see CloudWatch log stream {log_stream})"
        message = f"{failed}: {message}" if message else failed
    return {
        "status": status,
        "message": message[:4000],
        "summary": produced.get("summary") if isinstance(produced.get("summary"), dict) else {},
        "artifacts": [a for a in produced.get("artifacts") or [] if str(a).startswith("s3://")],
    }


def upload_artifacts(s3: Any, spec: dict[str, Any], out_dir: Path) -> list[str]:
    uploaded = []
    bucket, prefix = split_s3(spec["artifactsS3Prefix"])
    root = out_dir.resolve()
    for declared in (spec.get("artifacts") or {}).get("upload") or []:
        local = (root / Path(declared).relative_to("/out")).resolve()
        if root not in local.parents or not local.is_file():
            continue
        key = f"{prefix}artifacts/{local.name}"
        s3.upload_file(str(local), bucket, key)
        uploaded.append(f"s3://{bucket}/{key}")
    return uploaded


def main(argv: list[str] | None = None, s3: Any = None, secrets: Any = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    started = time.monotonic()
    out_dir, work_dir, tmp_dir = dirs()
    s3 = s3 or boto3.client("s3")
    secrets = secrets or boto3.client("secretsmanager")

    spec_uri = os.environ["ORCH_JOB_SPEC_S3"]
    bucket, key = split_s3(spec_uri)
    spec = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    spec_file = tmp_dir / "spec.json"
    spec_file.write_text(json.dumps(spec))
    print(f"orch: job {spec['phase']}/{spec['name']} run {spec['runId']} attempt {os.environ.get('ORCH_ATTEMPT', '1')}")

    jobs_dir = work_dir / "jobs"
    shutil.rmtree(jobs_dir, ignore_errors=True)
    jobs_dir.mkdir(parents=True)
    try:
        files = download_bundle(s3, spec.get("jobsS3Prefix"), jobs_dir, spec.get("bundleSha256"))
    except JobSetTampered as err:
        # Nothing of a bundle that differs from the pinned one runs; no connection is resolved.
        shutil.rmtree(jobs_dir, ignore_errors=True)
        print(f"orch: JobSetTampered: {err}")
        result = {"status": "fail", "message": f"JobSetTampered: {err}"[:4000], "summary": {}, "artifacts": []}
        s3.put_object(
            Bucket=bucket, Key=spec["resultKey"], Body=json.dumps(result).encode(), ContentType="application/json"
        )
        return 1
    conn_file = tmp_dir / "connections.json"
    names = resolve_connections(secrets, spec.get("connections") or {}, conn_file)
    print(f"orch: {files} bundle file(s), connections: {', '.join(names) or 'none'}")

    env = {
        **os.environ,
        "ORCH_JOB_SPEC_FILE": str(spec_file),
        "ORCH_CONNECTIONS_FILE": str(conn_file),
        "ORCH_OUT_DIR": str(out_dir),
        "ORCH_WORK_DIR": str(jobs_dir),
        "ORCH_DRY_RUN": "1" if spec.get("dryRun") else os.environ.get("ORCH_DRY_RUN", "0"),
        "ORCH_PHASE": spec["phase"],
        "ORCH_JOB_NAME": spec["name"],
    }
    if env["ORCH_DRY_RUN"] == "1":
        env["PGOPTIONS"] = f"{env.get('PGOPTIONS', '')} {READ_ONLY_PGOPTIONS}".strip()
    (out_dir / "result.json").unlink(missing_ok=True)
    if not command:
        exit_code = 2
        print("orch: no command given")
    else:
        timeout = max(30, int(spec.get("timeoutSeconds", 3600)) - 30)
        try:
            exit_code, _ = run_command(command, env, jobs_dir, timeout)
        except FileNotFoundError as err:
            exit_code = 127
            print(f"orch: command not found: {err}")

    result = build_result(exit_code, out_dir, log_stream_name() if exit_code else "")
    result["artifacts"] += upload_artifacts(s3, spec, out_dir)
    result["durationSeconds"] = round(time.monotonic() - started, 1)
    s3.put_object(
        Bucket=bucket, Key=spec["resultKey"], Body=json.dumps(result).encode(), ContentType="application/json"
    )
    conn_file.unlink(missing_ok=True)
    print(f"orch: status {result['status']} in {result['durationSeconds']}s")
    if result["status"] in OK_STATUSES:
        return 0
    return exit_code or 1


if __name__ == "__main__":
    sys.exit(main())

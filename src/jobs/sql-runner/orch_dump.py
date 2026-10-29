#!/usr/bin/env python3
"""pg-dump-s3 / pg-restore-s3: dump the target scope to S3 before a reset, restore it on demand.

    pg-dump-s3    --connection target [--schemas app,ref]
    pg-restore-s3 --connection target --from s3://<bucket>/dumps/<project>/<taskKey>/<ts>-<runId>/ --confirm

The dump is ``pg_dump -Fc --no-owner --no-privileges -n <schema>...`` streamed into an S3
multipart upload (no local disk), encrypted with SSE-KMS (``ORCH_DUMP_KMS_KEY``, or the AWS
managed key), next to a ``manifest.json`` with the SHA-256 and size. Object names hold only
the project, task key, time and run id: no host, user or secret. Roles and passwords are never
dumped (no ``pg_dumpall``). The schemas default to the selection rules of the task.

The restore downloads the dump to the work volume, checks the SHA-256 against the manifest and
runs ``pg_restore --clean --if-exists`` on the same schemas, piped into ``psql`` in one
transaction. It refuses without ``--confirm``. With ``ORCH_DRY_RUN=1`` both only print what
they would do.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import orch_sql  # noqa: E402

PART_SIZE = 8 * 1024 * 1024  # S3 multipart: every part but the last >= 5 MiB
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def dump_prefix(project: str, task_key: str, run_id: str, now: datetime) -> str:
    for label, value, pattern in (("project", project, NAME), ("taskKey", task_key, NAME), ("runId", run_id, RUN_ID)):
        if not pattern.fullmatch(value or ""):
            raise ValueError(f"unsafe {label} for a dump key: {value!r}")
    return f"dumps/{project}/{task_key}/{now:%Y%m%dT%H%M%SZ}-{run_id}/"


def scope_schemas(spec: dict[str, Any], given: str | None) -> list[str]:
    if given:
        schemas = [s for s in given.split(",") if s]
    else:
        rules = (spec.get("ctx") or {}).get("selectionRules") or []
        names = {r["schema"] for r in rules if r.get("action") != "exclude"}
        if any("%" in n for n in names):
            raise ValueError("the selection rules use a schema wildcard: pass --schemas")
        schemas = sorted(names)
    if not schemas:
        raise ValueError("no schemas to dump (no selection rules and no --schemas)")
    for s in schemas:
        if not orch_sql.IDENT.match(s):
            raise ValueError(f"unsafe schema name {s!r}")
    return schemas


def _connection(name: str) -> dict[str, Any]:
    conns = orch_sql.load_json("ORCH_CONNECTIONS_FILE")
    if name not in conns:
        raise ValueError(f"connection {name!r} not available (have: {sorted(conns)})")
    return conns[name]


def _pg_env(conn: dict[str, Any]) -> dict[str, str]:
    return orch_sql.client_command(conn, "postgresql", [], "stop", False)[1]


def _s3() -> Any:
    import boto3

    return boto3.client("s3")


def _result(status: str, message: str, **summary: Any) -> None:
    orch_sql.write_result(status, message, summary)


def dump_main(argv: list[str] | None = None, popen: Any = subprocess.Popen, now: datetime | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pg-dump-s3", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--connection", default="target")
    p.add_argument("--schemas")
    args = p.parse_args(argv)
    try:
        spec = orch_sql.load_json("ORCH_JOB_SPEC_FILE")
        ctx = spec.get("ctx") or {}
        schemas = scope_schemas(spec, args.schemas)
        prefix = dump_prefix(
            ctx.get("project", ""), ctx.get("taskKey", ""), spec.get("runId", ""), now or datetime.now(UTC)
        )
        conn = _connection(args.connection)
        bucket = os.environ["ORCH_ARTIFACTS_BUCKET"]
    except (ValueError, KeyError, OSError) as err:
        print(f"pg-dump-s3: {err}", file=sys.stderr)
        _result("fail", str(err))
        return 2

    argv_ = ["pg_dump", "-Fc", "--no-owner", "--no-privileges"]
    for s in schemas:
        argv_ += ["-n", s]
    uri = f"s3://{bucket}/{prefix}target.dump"
    if os.environ.get("ORCH_DRY_RUN") == "1":
        print(f"pg-dump-s3: dry run: {' '.join(argv_)} -> {uri}")
        _result("ok", "dry run: nothing dumped", dryRun=True, dumpS3Uri=uri, schemas=schemas)
        return 0

    s3 = _s3()
    sse: dict[str, str] = {"ServerSideEncryption": "aws:kms"}
    if os.environ.get("ORCH_DUMP_KMS_KEY"):
        sse["SSEKMSKeyId"] = os.environ["ORCH_DUMP_KMS_KEY"]
    key = f"{prefix}target.dump"
    upload = s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType="application/octet-stream", **sse)
    parts: list[dict[str, Any]] = []
    digest, size = hashlib.sha256(), 0
    print(f"pg-dump-s3: {', '.join(schemas)} -> {uri}")
    proc = popen(argv_, env={**os.environ, **_pg_env(conn)}, stdout=subprocess.PIPE)
    try:
        buffer = b""
        while True:
            chunk = proc.stdout.read(1024 * 1024)
            if chunk:
                digest.update(chunk)
                size += len(chunk)
                buffer += chunk
            if buffer and (len(buffer) >= PART_SIZE or not chunk):
                n = len(parts) + 1
                etag = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload["UploadId"], PartNumber=n, Body=buffer)[
                    "ETag"
                ]
                parts.append({"PartNumber": n, "ETag": etag})
                buffer = b""
            if not chunk:
                break
        code = proc.wait()
        if code != 0 or not parts:
            raise RuntimeError(f"pg_dump exit code {code}")
        s3.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload["UploadId"], MultipartUpload={"Parts": parts}
        )
    except Exception as err:  # noqa: BLE001 - abort the upload whatever failed
        s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload["UploadId"])
        print(f"pg-dump-s3: {err}", file=sys.stderr)
        _result("fail", str(err), schemas=schemas)
        return 1

    manifest = {
        "sha256": digest.hexdigest(),
        "bytes": size,
        "format": "pg_dump custom",
        "schemas": schemas,
        "runId": spec.get("runId"),
        "project": ctx.get("project"),
        "taskKey": ctx.get("taskKey"),
        "createdAt": (now or datetime.now(UTC)).isoformat(),
    }
    s3.put_object(
        Bucket=bucket,
        Key=f"{prefix}manifest.json",
        Body=json.dumps(manifest, indent=2).encode(),
        ContentType="application/json",
        **sse,
    )
    _result(
        "ok",
        f"dumped {size} bytes of {len(schemas)} schema(s)",
        dumpS3Uri=uri,
        manifestS3Uri=f"s3://{bucket}/{prefix}manifest.json",
        bytes=size,
        sha256=manifest["sha256"],
        schemas=schemas,
    )
    return 0


# Session settings a newer pg_dump/pg_restore writes in the SQL header that an older server
# refuses ("unrecognized configuration parameter"): PostgreSQL 17 client, Aurora 16 target.
NEWER_HEADER_SETTINGS = (b"SET transaction_timeout = 0;",)


def _copy_without_newer_settings(src: Any, dst: Any) -> None:
    """Stream pg_restore's SQL to psql; drop NEWER_HEADER_SETTINGS from the header only (the
    comment, psql meta-command (backslash, e.g. restrict), SET and set_config lines before the first
    statement), never from the data."""
    header = True
    for line in iter(src.readline, b""):
        if header:
            text = line.strip()
            if text in NEWER_HEADER_SETTINGS:
                continue
            header = not text or text.startswith((b"SET ", b"SELECT pg_catalog.set_config", b"--", b"\\"))
        dst.write(line)


def restore_main(argv: list[str] | None = None, popen: Any = subprocess.Popen) -> int:
    p = argparse.ArgumentParser(
        prog="pg-restore-s3", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--connection", default="target")
    p.add_argument("--from", dest="source", required=True, help="s3://bucket/dumps/<project>/<taskKey>/<ts>-<runId>/")
    p.add_argument("--schemas", help="default: the schemas in the manifest")
    p.add_argument("--confirm", action="store_true", help="required: the restore drops and recreates objects")
    args = p.parse_args(argv)
    if not args.confirm:
        msg = "refusing to restore without --confirm (pg_restore --clean drops the objects of the dump first)"
        print(f"pg-restore-s3: {msg}", file=sys.stderr)
        _result("fail", msg)
        return 2
    try:
        bucket, _, prefix = args.source.removeprefix("s3://").partition("/")
        prefix = prefix if prefix.endswith("/") else prefix + "/"
        if not prefix.startswith("dumps/"):
            raise ValueError(f"not a dump location: {args.source}")
        conn = _connection(args.connection)
        s3 = _s3()
        manifest = json.loads(s3.get_object(Bucket=bucket, Key=f"{prefix}manifest.json")["Body"].read())
        schemas = [s for s in (args.schemas or ",".join(manifest.get("schemas") or [])).split(",") if s]
        for s in schemas:
            if not orch_sql.IDENT.match(s):
                raise ValueError(f"unsafe schema name {s!r}")
    except (ValueError, KeyError, OSError) as err:
        print(f"pg-restore-s3: {err}", file=sys.stderr)
        _result("fail", str(err))
        return 2

    work = Path(os.environ.get("ORCH_WORK_DIR", "/work"))
    path = work / "target.dump"
    digest = hashlib.sha256()
    body = s3.get_object(Bucket=bucket, Key=f"{prefix}target.dump")["Body"]
    with path.open("wb") as fh:
        for chunk in iter(lambda: body.read(1024 * 1024), b""):
            digest.update(chunk)
            fh.write(chunk)
    if digest.hexdigest() != manifest.get("sha256"):
        msg = "the dump does not match its manifest (sha256); nothing restored"
        print(f"pg-restore-s3: {msg}", file=sys.stderr)
        _result("fail", msg)
        return 1
    # pg_restore writes SQL, psql applies it in one transaction: the client (PostgreSQL 17 in the
    # image) may be newer than the server, and its header settings must be filtered.
    argv_ = ["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges"]
    for s in schemas:
        argv_ += ["-n", s]
    argv_ += ["-f", "-", str(path)]
    psql = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "--single-transaction", "-d", str(conn["database"])]
    if os.environ.get("ORCH_DRY_RUN") == "1":
        print(f"pg-restore-s3: dry run: {' '.join(argv_)} | {' '.join(psql)}")
        _result("ok", "dry run: nothing restored", dryRun=True, schemas=schemas)
        return 0
    print(f"pg-restore-s3: {args.source} -> {', '.join(schemas)}")
    env = {**os.environ, **_pg_env(conn)}
    restore = popen(argv_, env=env, stdout=subprocess.PIPE)
    apply = popen(psql, env=env, stdin=subprocess.PIPE)
    try:
        _copy_without_newer_settings(restore.stdout, apply.stdin)
        apply.stdin.close()
    except BrokenPipeError:
        pass  # psql stopped (ON_ERROR_STOP): its exit code and stderr tell why
    code_psql = apply.wait()
    if code_psql:
        restore.kill()
    code_restore = restore.wait()
    if code_restore or code_psql:
        _result("fail", f"pg_restore exit code {code_restore}, psql exit code {code_psql}", schemas=schemas)
        return code_restore or code_psql
    _result("ok", f"restored {len(schemas)} schema(s)", source=args.source, schemas=schemas)
    return 0


if __name__ == "__main__":
    sys.exit(restore_main() if Path(sys.argv[0]).name == "pg-restore-s3" else dump_main())

#!/usr/bin/env python3
"""pg-dump-s3 / pg-restore-s3: dump the target scope to S3 before a reset, restore it on demand.

    pg-dump-s3    --connection target [--schemas app,ref]
    pg-restore-s3 --connection target --from s3://<bucket>/dumps/<project>/<taskKey>/<ts>-<runId>/ --confirm

The dump takes the tables of the task's scope: the target's tables (ordinary and partitioned,
not partitions) that the selection rules select (``shared.preflight.in_scope``, the tables a
reset truncates), optionally only in ``--schemas``. It is ``pg_dump -Fc --no-owner
--no-privileges -t <table>...`` (with each table's indexes, constraints, defaults, triggers and
owned sequences) streamed into an S3 multipart upload (no local disk), encrypted with SSE-KMS
(``ORCH_DUMP_KMS_KEY``, or the AWS managed key), next to a ``manifest.json`` with the SHA-256,
size, project, task key and the table list. Object names hold only the project, task key, time
and run id: no host, user or secret. Roles and passwords are never dumped (no ``pg_dumpall``).
A partitioned table in scope is refused (``-t`` would take it without its partitions).

The restore checks the manifest first: its project and task key must be those of the job spec
and the location under ``dumps/<project>/<taskKey>/``; then it downloads the dump to the work
volume and checks the SHA-256. ``pg_restore -l`` lists the archive, the list keeps only the
objects of the manifest's tables (never a schema, extension or function), and
``pg_restore --clean --if-exists -L <list>`` streams SQL into ``psql``: the job sends ``BEGIN;``
first and ``COMMIT;`` only after pg_restore exited 0, ``ROLLBACK;`` otherwise (``psql -1``
does nothing for a script on stdin). It refuses without ``--confirm``. With ``ORCH_DRY_RUN=1``
both only print what they would do.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import orch_sql  # noqa: E402
import orch_verify  # noqa: E402

from shared import fingerprint, preflight  # noqa: E402

UTC = timezone.utc  # noqa: UP017 - the SSM runtime runs the AMI's python3 (3.9 has no UTC alias in datetime)
PART_SIZE = 8 * 1024 * 1024  # S3 multipart: every part but the last >= 5 MiB
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def dump_prefix(project: str, task_key: str, run_id: str, now: datetime) -> str:
    for label, value, pattern in (("project", project, NAME), ("taskKey", task_key, NAME), ("runId", run_id, RUN_ID)):
        if not pattern.fullmatch(value or ""):
            raise ValueError(f"unsafe {label} for a dump key: {value!r}")
    return f"dumps/{project}/{task_key}/{now:%Y%m%dT%H%M%SZ}-{run_id}/"


# Ordinary and partitioned tables (not partitions) of the user schemas, as the fingerprint sees them.
CATALOGUE_SQL = (
    "SELECT n.nspname AS s, c.relname AS t, c.relkind = 'p' AS partitioned "  # noqa: S608 - constant fragments
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    f"WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition AND {fingerprint.SYSTEM_SCHEMAS} ORDER BY 1, 2"
)


def scope_tables(
    catalogue: list[dict[str, Any]], rules: list[dict[str, str]], schemas: list[str] | None = None
) -> list[str]:
    """``schema.table`` of the catalogue the rules select, in ``schemas`` if given (all of
    ``schemas`` without rules; nothing without either)."""
    if not rules and schemas is None:
        return []
    return sorted(
        f"{row['s']}.{row['t']}"
        for row in catalogue
        if (schemas is None or row["s"] in schemas) and (not rules or preflight.in_scope(rules, row["s"], row["t"]))
    )


def _check_tables(tables: Any) -> list[str]:
    if not isinstance(tables, list) or not tables:
        raise ValueError("no tables in scope")
    for t in tables:
        parts = str(t).split(".")
        if len(parts) != 2 or not all(orch_sql.IDENT.match(x) for x in parts):
            raise ValueError(f"unsafe table name {t!r}")
    return [str(t) for t in tables]


def _table_pattern(table: str) -> str:
    """pg_dump -t pattern matching exactly one table (quoted: no case folding, no wildcards)."""
    return ".".join(f'"{p}"' for p in table.split("."))


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


def dump_main(
    argv: list[str] | None = None,
    popen: Any = subprocess.Popen,
    now: datetime | None = None,
    runner: Any = subprocess.run,
) -> int:
    p = argparse.ArgumentParser(
        prog="pg-dump-s3", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--connection", default="target")
    p.add_argument("--schemas")
    args = p.parse_args(argv)
    try:
        spec = orch_sql.load_json("ORCH_JOB_SPEC_FILE")
        ctx = spec.get("ctx") or {}
        prefix = dump_prefix(
            ctx.get("project", ""), ctx.get("taskKey", ""), spec.get("runId", ""), now or datetime.now(UTC)
        )
        given = [s for s in (args.schemas or "").split(",") if s] or None
        rules = ctx.get("selectionRules") or []
        if not rules and not given:
            raise ValueError("no selection rules in the job spec and no --schemas")
        conn = _connection(args.connection)
        bucket = os.environ["ORCH_ARTIFACTS_BUCKET"]
        catalogue = orch_verify.psql_query(conn, runner)(CATALOGUE_SQL)
        tables = _check_tables(scope_tables(catalogue, rules, given))
        partitioned = sorted(f"{r['s']}.{r['t']}" for r in catalogue if r.get("partitioned"))
        if blocked := [t for t in tables if t in partitioned]:
            raise ValueError(
                f"partitioned table(s) in scope: {', '.join(blocked)}; pg-dump-s3 dumps whole tables only "
                "(use dump.method = snapshot)"
            )
        schemas = sorted({t.split(".")[0] for t in tables})
    except (ValueError, KeyError, OSError, RuntimeError) as err:
        print(f"pg-dump-s3: {err}", file=sys.stderr)
        _result("fail", str(err))
        return 2

    argv_ = ["pg_dump", "-Fc", "--no-owner", "--no-privileges"]
    for t in tables:
        argv_ += ["-t", _table_pattern(t)]
    uri = f"s3://{bucket}/{prefix}target.dump"
    if os.environ.get("ORCH_DRY_RUN") == "1":
        print(f"pg-dump-s3: dry run: pg_dump of {len(tables)} table(s) -> {uri}")
        _result("ok", "dry run: nothing dumped", dryRun=True, dumpS3Uri=uri, schemas=schemas, tables=len(tables))
        return 0

    s3 = _s3()
    sse: dict[str, str] = {"ServerSideEncryption": "aws:kms"}
    if os.environ.get("ORCH_DUMP_KMS_KEY"):
        sse["SSEKMSKeyId"] = os.environ["ORCH_DUMP_KMS_KEY"]
    key = f"{prefix}target.dump"
    upload = s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType="application/octet-stream", **sse)
    parts: list[dict[str, Any]] = []
    digest, size = hashlib.sha256(), 0
    print(f"pg-dump-s3: {len(tables)} table(s) of {', '.join(schemas)} -> {uri}")
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
        "tables": tables,
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
        f"dumped {size} bytes of {len(tables)} table(s)",
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


# pg_restore -l entry types this restore applies. Table-bound ones carry the table as the first
# word of the tag; the others (indexes, owned sequences) are in the archive only because pg_dump
# -t took them with a table of the manifest. Anything else (schema, extension, function, type,
# view, ACL) is not part of the scope and is never dropped or recreated.
TABLE_BOUND = (
    "TABLE DATA",
    "FK CONSTRAINT",
    "ROW SECURITY",
    "CONSTRAINT",
    "DEFAULT",
    "TRIGGER",
    "POLICY",
    "RULE",
    "TABLE",
)
WITH_TABLE = ("SEQUENCE OWNED BY", "SEQUENCE SET", "INDEX ATTACH", "SEQUENCE", "INDEX")
TOC_ENTRY = re.compile(r"^\d+; \d+ \d+ (?P<rest>.+)$")


def _entry(line: str) -> tuple[str, str, list[str]] | None:
    """(type, schema, tag words) of a pg_restore -l line; None for comments and unknown types."""
    m = TOC_ENTRY.match(line.strip())
    if not m:
        return None
    rest = m.group("rest")
    for kind in (*TABLE_BOUND, *WITH_TABLE, "COMMENT"):
        if rest.startswith(kind + " "):
            words = rest[len(kind) + 1 :].split(" ")
            return (kind, words[0], words[1:]) if len(words) >= 2 else None
    return None


def toc_in_scope(listing: str, tables: list[str]) -> str:
    """The pg_restore -l lines of the objects of ``tables``: the list for pg_restore -L."""
    wanted = {tuple(t.split(".", 1)) for t in tables}
    schemas = {s for s, _ in wanted}
    keep = []
    for line in listing.splitlines():
        entry = _entry(line)
        if entry is None:
            continue
        kind, schema, tag = entry
        if kind in TABLE_BOUND:
            ok = (schema, tag[0]) in wanted
        elif kind == "COMMENT":
            target = tag[1].split(".")[0] if len(tag) > 1 and tag[0] in ("TABLE", "COLUMN") else None
            ok = target is not None and (schema, target) in wanted
        else:
            ok = schema in schemas
        if ok:
            keep.append(line.strip())
    return "\n".join(keep) + ("\n" if keep else "")


def _restore_in_transaction(restore: Any, apply: Any) -> tuple[int, int]:
    """BEGIN, pg_restore's SQL, then COMMIT only once pg_restore exited 0 (else ROLLBACK).

    psql runs with ON_ERROR_STOP: a failing statement ends it before COMMIT, the server rolls the
    open transaction back. Returns (pg_restore exit code, psql exit code)."""
    try:
        apply.stdin.write(b"BEGIN;\n")
        _copy_without_newer_settings(restore.stdout, apply.stdin)
    except BrokenPipeError:
        restore.kill()  # psql stopped (ON_ERROR_STOP): its exit code and stderr tell why
        code_restore = restore.wait()
        return code_restore or 1, apply.wait()
    code_restore = restore.wait()
    try:
        apply.stdin.write(b"COMMIT;\n" if code_restore == 0 else b"ROLLBACK;\n")
        apply.stdin.close()
    except BrokenPipeError:
        pass
    return code_restore, apply.wait()


def restore_main(argv: list[str] | None = None, popen: Any = subprocess.Popen, runner: Any = subprocess.run) -> int:
    p = argparse.ArgumentParser(
        prog="pg-restore-s3", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--connection", default="target")
    p.add_argument("--from", dest="source", required=True, help="s3://bucket/dumps/<project>/<taskKey>/<ts>-<runId>/")
    p.add_argument("--confirm", action="store_true", help="required: the restore drops and recreates objects")
    args = p.parse_args(argv)
    if not args.confirm:
        msg = "refusing to restore without --confirm (pg_restore --clean drops the objects of the dump first)"
        print(f"pg-restore-s3: {msg}", file=sys.stderr)
        _result("fail", msg)
        return 2
    try:
        ctx = orch_sql.load_json("ORCH_JOB_SPEC_FILE").get("ctx") or {}
        project, task_key = str(ctx.get("project") or ""), str(ctx.get("taskKey") or "")
        if not (NAME.fullmatch(project) and NAME.fullmatch(task_key)):
            raise ValueError("the job spec has no valid ctx.project / ctx.taskKey")
        bucket, _, prefix = args.source.removeprefix("s3://").partition("/")
        prefix = prefix if prefix.endswith("/") else prefix + "/"
        if not prefix.startswith(f"dumps/{project}/{task_key}/") or ".." in prefix:
            raise ValueError(f"not a dump of {project}/{task_key}: {args.source}")
        conn = _connection(args.connection)
        s3 = _s3()
        manifest = json.loads(s3.get_object(Bucket=bucket, Key=f"{prefix}manifest.json")["Body"].read())
        if (manifest.get("project"), manifest.get("taskKey")) != (project, task_key):
            raise ValueError(
                f"the manifest is of {manifest.get('project')}/{manifest.get('taskKey')}, "
                f"not {project}/{task_key}; nothing restored"
            )
        if "tables" not in manifest:
            raise ValueError("the manifest lists no tables (a dump without a table scope); nothing restored")
        tables = _check_tables(manifest["tables"])
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
    # pg_restore writes SQL, psql applies it: the client (PostgreSQL 17 in the image) may be newer
    # than the server, and its header settings must be filtered.
    toc = work / "restore.toc"
    argv_ = ["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges", "-L", str(toc)]
    argv_ += ["-f", "-", str(path)]
    psql = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", str(conn["database"])]
    if os.environ.get("ORCH_DRY_RUN") == "1":
        print(f"pg-restore-s3: dry run: {len(tables)} table(s): {' '.join(argv_)} | {' '.join(psql)}")
        _result("ok", "dry run: nothing restored", dryRun=True, tables=tables)
        return 0
    env = {**os.environ, **_pg_env(conn)}
    listed = runner(["pg_restore", "-l", str(path)], env=env, capture_output=True, text=True, check=False)
    if listed.returncode != 0:
        msg = f"pg_restore -l exit code {listed.returncode}; nothing restored"
        print(f"pg-restore-s3: {msg}: {(listed.stderr or '').strip()[-300:]}", file=sys.stderr)
        _result("fail", msg)
        return 1
    scoped = toc_in_scope(listed.stdout or "", tables)
    present = {(e[1], e[2][0]) for e in map(_entry, scoped.splitlines()) if e and e[0] == "TABLE"}
    if missing := [t for t in tables if tuple(t.split(".", 1)) not in present]:
        msg = f"the dump lacks table(s) of its manifest: {', '.join(missing)}; nothing restored"
        print(f"pg-restore-s3: {msg}", file=sys.stderr)
        _result("fail", msg)
        return 1
    toc.write_text(scoped)
    print(f"pg-restore-s3: {args.source} -> {len(tables)} table(s)")
    restore = popen(argv_, env=env, stdout=subprocess.PIPE)
    apply = popen(psql, env=env, stdin=subprocess.PIPE)
    code_restore, code_psql = _restore_in_transaction(restore, apply)
    if code_restore or code_psql:
        msg = f"pg_restore exit code {code_restore}, psql exit code {code_psql}; rolled back"
        _result("fail", msg, tables=tables)
        return code_psql or code_restore
    _result("ok", f"restored {len(tables)} table(s)", source=args.source, tables=tables)
    return 0


if __name__ == "__main__":
    sys.exit(restore_main() if Path(sys.argv[0]).name == "pg-restore-s3" else dump_main())

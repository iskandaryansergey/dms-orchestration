"""Sandbox project Lambda: SQL on the sandbox databases (pg8000, inside the VPC).

Stands in for the Fargate ``sql-runner`` image in the sandbox (no docker build there,
no Aurora Data API: the account only allows RDS instances). Job contract input
``{"spec": {...}, "attempt": n}``; ``spec.with.action``:

* ``truncate``   — truncate ``ctx.resetTables`` on the target (reset phase). The list comes from
  the job's ``spec.json`` (``specS3Uri``): above the payload cap the payload carries only
  ``resetTablesCount``; a list that does not match the count fails the job;
* ``row_counts`` — compare ``with.tables`` between source and target (post phase).

Output: ``{"status": "ok|fail", "message", "summary"}``. TLS without CA verification
(the Lambda has no RDS CA bundle) — sandbox only.
"""

from __future__ import annotations

import json
import os
import re
import ssl
from typing import Any

import boto3
import pg8000.native

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
secrets = boto3.client("secretsmanager")
s3 = boto3.client("s3")


def quote_table(name: str) -> str:
    parts = name.split(".")
    if not 1 <= len(parts) <= 2 or not all(IDENT.match(p) for p in parts):
        raise ValueError(f"unsafe table name {name!r}")
    return ".".join(f'"{p}"' for p in parts)


def connect(which: str) -> pg8000.native.Connection:
    s = json.loads(secrets.get_secret_value(SecretId=os.environ[f"{which.upper()}_SECRET_ARN"])["SecretString"])
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return pg8000.native.Connection(
        user=s["username"],
        password=s["password"],
        host=s["host"],
        port=int(s["port"]),
        database=s["dbname"],
        ssl_context=ctx,
        timeout=60,
    )


def reset_tables(spec: dict[str, Any]) -> list[str]:
    """The full ``ctx.resetTables`` of the job's spec.json, checked against the decided count."""
    bucket, _, key = str(spec["specS3Uri"]).removeprefix("s3://").partition("/")
    full = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    ctx = full.get("ctx") or {}
    tables = ctx.get("resetTables") or []
    count = ctx.get("resetTablesCount")
    if count is not None and int(count) != len(tables):
        raise ValueError(f"{len(tables)} table(s) in the spec, the decision says {count}")
    return tables


def truncate(spec: dict[str, Any]) -> dict[str, Any]:
    try:
        tables = reset_tables(spec)
    except ValueError as err:
        return {"status": "fail", "message": str(err), "summary": {}}
    # The summary keeps the count only: a list of hundreds of tables would not fit the payload.
    if not tables:
        return {"status": "ok", "message": "nothing to truncate", "summary": {"tables": 0}}
    sql = "TRUNCATE TABLE " + ", ".join(quote_table(t) for t in tables)
    if spec.get("dryRun"):
        return {
            "status": "ok",
            "message": f"dry run: would truncate {len(tables)} table(s)",
            "summary": {"tables": len(tables)},
        }
    conn = connect("target")
    try:
        conn.run(sql)
    finally:
        conn.close()
    return {"status": "ok", "message": f"truncated {len(tables)} table(s)", "summary": {"tables": len(tables)}}


def row_counts(spec: dict[str, Any]) -> dict[str, Any]:
    tables = (spec.get("with") or {}).get("tables") or []
    rows = []
    src, tgt = connect("source"), connect("target")
    try:
        for t in tables:
            sql = f"SELECT count(*) FROM {quote_table(t)}"  # noqa: S608 - identifiers validated
            s = src.run(sql)[0][0]
            try:
                n = tgt.run(sql)[0][0]
            except pg8000.native.DatabaseError:
                n = None
            rows.append({"table": t, "source": s, "target": n})
    finally:
        src.close()
        tgt.close()
    bad = [r["table"] for r in rows if r["source"] != r["target"]]
    if spec.get("dryRun"):
        return {"status": "ok", "message": f"dry run: {len(bad)} table(s) would differ", "summary": {"tables": rows}}
    status = "fail" if bad else "ok"
    msg = f"{len(bad)} of {len(rows)} table(s) differ" if bad else f"{len(rows)} table(s) match"
    return {"status": status, "message": msg, "summary": {"tables": rows, "mismatched": bad}}


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    spec = event["spec"]
    action = (spec.get("with") or {}).get("action")
    if action == "truncate":
        return truncate(spec)
    if action == "row_counts":
        return row_counts(spec)
    return {"status": "fail", "message": f"unknown action {action!r}", "summary": {}}

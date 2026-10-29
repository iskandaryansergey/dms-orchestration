#!/usr/bin/env python3
"""verify-target: the pre-flight target fingerprint through psql, for targets too big for a Lambda.

    verify-target            # with.connection (target), with.source, with.exactCountMaxRows from the spec

Same SQL and the same JSON as the built-in ``checks/target_verify`` (``shared/fingerprint.py``):
the fingerprint goes to ``<artifactsS3Prefix>target-fingerprint.json``, the summary into
``result.json``, so pre-flight and the baseline read either the same way. Each query runs as
``SELECT json_agg(...)`` through ``psql -X -A -t`` with the SQL on stdin; its parameters are psql
variables (``-v name=value``) referenced as ``:'name'``, which psql quotes as literals. The
password reaches psql only through its environment. Read-only, so it runs in dry runs too.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # image layout: /opt/orch/shared/

import orch_sql  # noqa: E402

from shared import fingerprint  # noqa: E402

PARAM = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
ROW = "orch_verify_row"


def _psql_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def to_psql(sql: str, names: set[str]) -> str:
    """``:name`` (Data API style) -> ``:'name'`` (psql quoted variable); casts ``::`` stay."""
    return PARAM.sub(lambda m: f":'{m.group(1)}'" if m.group(1) in names else m.group(0), sql)


def psql_query(conn: dict[str, Any], runner: Any = subprocess.run) -> fingerprint.Query:
    env = {**os.environ, **orch_sql.client_command(conn, "postgresql", [], "stop", False)[1]}

    def query(sql: str, **params: Any) -> list[dict[str, Any]]:
        argv = ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]
        for key, value in params.items():
            argv += ["-v", f"{key}={_psql_value(value)}"]
        body = to_psql(sql, set(params))
        # The body is one of the constant queries of shared.fingerprint; values travel as psql variables.
        # A bare name resolves to a column before a table alias (the sequences query has a column
        # q): aggregate the qualified whole row, under an alias no fingerprint query uses.
        script = f"SELECT coalesce(json_agg({ROW}.*), '[]'::json) FROM ({body}) {ROW};\n"  # noqa: S608
        proc = runner(argv, input=script, env=env, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            raise RuntimeError(f"psql exit code {proc.returncode}: {' '.join(tail)}")
        return json.loads((proc.stdout or "").strip() or "[]")

    return query


def verify_main(argv: list[str] | None = None, runner: Any = subprocess.run, s3: Any = None) -> int:
    del argv  # everything comes from the job spec
    try:
        spec = orch_sql.load_json("ORCH_JOB_SPEC_FILE")
        w = spec.get("with") or {}
        rules = (spec.get("ctx") or {}).get("selectionRules") or w.get("rules") or []
        if not rules:
            raise ValueError("no selection rules for this task: the table mapping cannot scope the check")
        conns = orch_sql.load_json("ORCH_CONNECTIONS_FILE")
        name = w.get("connection", "target")
        if name not in conns:
            raise ValueError(f"connection {name!r} not available (have: {sorted(conns)})")
        source_name = w.get("source")
        source_query = psql_query(conns[source_name], runner) if source_name and source_name in conns else None
        fp, size = fingerprint.build(
            psql_query(conns[name], runner),
            rules,
            max_exact=int(w.get("exactCountMaxRows", 1_000_000)),
            source_query=source_query,
        )
    except (ValueError, KeyError, OSError, RuntimeError) as err:
        print(f"verify-target: {err}", file=sys.stderr)
        orch_sql.write_result("fail", str(err), {})
        return 1

    bucket, _, prefix = spec["artifactsS3Prefix"].removeprefix("s3://").partition("/")
    key = f"{prefix}target-fingerprint.json"
    if s3 is None:
        import boto3

        s3 = boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(fp, indent=2).encode(), ContentType="application/json")
    uri = f"s3://{bucket}/{key}"
    summary = fingerprint.summary(fp, uri, size)
    out = Path(os.environ.get("ORCH_OUT_DIR", "/out")) / "result.json"
    message = f"{summary['tables']} table(s) in scope, {summary['rows']} row(s)"
    out.write_text(json.dumps({"status": "ok", "message": message, "summary": summary, "artifacts": [uri]}))
    print(f"verify-target: {message}, {summary['fingerprintHash']}")
    return 0


if __name__ == "__main__":
    sys.exit(verify_main(sys.argv[1:]))

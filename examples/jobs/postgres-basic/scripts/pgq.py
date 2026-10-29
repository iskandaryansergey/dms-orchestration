"""Tiny psql helper for job scripts in the sql-runner image (no Python DB drivers needed).

from pgq import query, write_result
rows = query("source", "SELECT table_name FROM information_schema.tables")
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

RDS_CA = "/etc/ssl/certs/rds-global-bundle.pem"


def connection(name: str) -> dict[str, Any]:
    conns = json.loads(Path(os.environ["ORCH_CONNECTIONS_FILE"]).read_text())
    if name not in conns:
        raise SystemExit(f"connection {name!r} not available (have: {sorted(conns)})")
    return conns[name]


def query(conn_name: str, sql: str) -> list[dict[str, str]]:
    """Run one SELECT through psql and return rows as dicts of strings."""
    conn = connection(conn_name)
    env = {
        **os.environ,
        "PGHOST": str(conn["host"]),
        "PGPORT": str(conn.get("port") or 5432),
        "PGDATABASE": str(conn["database"]),
        "PGUSER": str(conn["username"]),
        "PGPASSWORD": str(conn["password"]),
        "PGSSLMODE": conn.get("sslMode") or "verify-full",
        "PGSSLROOTCERT": os.environ.get("ORCH_CA_BUNDLE", RDS_CA),
        "PGAPPNAME": "dms-orchestration-job",
    }
    out = subprocess.run(  # noqa: S603
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "--csv", "-c", sql],  # noqa: S607
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return list(csv.DictReader(io.StringIO(out)))


def write_result(
    status: str, message: str, summary: dict[str, Any], artifact: str | None = None, data: Any = None
) -> None:
    out = Path(os.environ.get("ORCH_OUT_DIR", "/out"))
    if artifact:
        (out / artifact).write_text(json.dumps(data, indent=2, default=str))
    (out / "result.json").write_text(json.dumps({"status": status, "message": message, "summary": summary}))

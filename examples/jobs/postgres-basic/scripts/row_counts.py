#!/usr/bin/env python3
"""Exact row counts per table of one schema on two connections; fail on any difference."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pgq import query, write_result  # noqa: E402

IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def tables(conn: str, schema: str) -> list[str]:
    # schema is checked against IDENT in main()
    sql = f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{schema}' AND table_type = 'BASE TABLE'"  # noqa: S608, E501
    rows = query(conn, sql + " ORDER BY 1")
    return [r["table_name"] for r in rows if IDENT.match(r["table_name"])]


def count(conn: str, schema: str, table: str) -> int:
    return int(query(conn, f'SELECT count(*) AS n FROM "{schema}"."{table}"')[0]["n"])  # noqa: S608


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--schema", required=True)
    args = p.parse_args(argv)
    if not IDENT.match(args.schema):
        raise SystemExit(f"bad schema name {args.schema!r}")

    rows = []
    for table in tables(args.source, args.schema):
        s, t = count(args.source, args.schema, table), count(args.target, args.schema, table)
        rows.append({"table": f"{args.schema}.{table}", "source": s, "target": t, "diff": t - s})
    bad = [r for r in rows if r["diff"]]
    summary = {"tables": len(rows), "mismatched": [r["table"] for r in bad]}
    if bad:
        write_result("fail", f"{len(bad)} of {len(rows)} table(s) differ", summary, "row_counts.json", rows)
        return 1
    write_result("ok", f"{len(rows)} table(s) match", summary, "row_counts.json", rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Compare tables and columns of one schema between two connections.

Writes /out/diff.json and result.json with summary
``{missingTables, extraTables, columnMismatches, missing: [...]}``.
Missing tables on the target are expected before the first load (create-tables
runs when missingTables > 0); column mismatches on existing tables fail the job.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pgq import query, write_result  # noqa: E402

COLUMNS_SQL = (
    "SELECT c.table_name, c.column_name, c.data_type, c.is_nullable "
    "FROM information_schema.columns c JOIN information_schema.tables t "
    "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
    "WHERE c.table_schema = '{schema}' AND t.table_type = 'BASE TABLE' ORDER BY 1, ordinal_position"
)


def columns(conn: str, schema: str) -> dict[str, dict[str, tuple[str, str]]]:
    tables: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    for row in query(conn, COLUMNS_SQL.format(schema=schema)):
        tables[row["table_name"]][row["column_name"]] = (row["data_type"], row["is_nullable"])
    return dict(tables)


def diff(source: dict, target: dict) -> dict:
    missing = sorted(set(source) - set(target))
    extra = sorted(set(target) - set(source))
    mismatches = []
    for table in sorted(set(source) & set(target)):
        for col, (dtype, _nullable) in source[table].items():
            got = target[table].get(col)
            if got is None:
                mismatches.append({"table": table, "column": col, "problem": "missing on target"})
            elif got[0] != dtype:
                mismatches.append({"table": table, "column": col, "problem": f"type {got[0]} != {dtype}"})
    return {"missing": missing, "extra": extra, "columnMismatches": mismatches}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--schema", required=True)
    args = p.parse_args(argv)
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", args.schema):
        raise SystemExit(f"bad schema name {args.schema!r}")

    result = diff(columns(args.source, args.schema), columns(args.target, args.schema))
    summary = {
        "missingTables": len(result["missing"]),
        "extraTables": len(result["extra"]),
        "columnMismatches": len(result["columnMismatches"]),
        "missing": result["missing"][:100],
    }
    if result["columnMismatches"]:
        write_result("fail", f"{len(result['columnMismatches'])} column mismatch(es)", summary, "diff.json", result)
        return 1
    message = f"{summary['missingTables']} table(s) missing on target" if result["missing"] else "schemas match"
    write_result("ok", message, summary, "diff.json", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())

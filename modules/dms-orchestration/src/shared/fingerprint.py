"""Target fingerprint for pre-flight (ADR 0006), independent of the transport.

The same SQL builds the same JSON whether it runs through the RDS Data API (built-in Lambda
job ``checks/target_verify``) or through psql on Fargate (``verify-target`` in the sql-runner
image, for targets too big for a 15-minute Lambda). ``query(sql, **params)`` returns rows as
dicts; parameters are named ``:name`` and always bound by the transport, never spliced in.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from shared import preflight

Query = Callable[..., list[dict[str, Any]]]
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
SYSTEM_SCHEMAS = "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\\_%'"
SUMMARY_KEYS = ("fingerprintUri", "fingerprintHash", "tables", "rows", "expected", "outOfScope", "databaseBytes")


def quote_table(qualified: str) -> str:
    parts = qualified.split(".")
    if not 1 <= len(parts) <= 2 or not all(IDENT.match(p) for p in parts):
        raise ValueError(f"unsafe identifier {qualified!r}")
    return ".".join(f'"{p}"' for p in parts)


def tables(query: Query, schemas: list[str] | None) -> list[dict[str, Any]]:
    """Ordinary and partitioned tables (not partitions) with the planner estimate and row counters."""
    return query(
        "SELECT n.nspname AS s, c.relname AS t, GREATEST(c.reltuples, 0)::bigint AS est, "  # noqa: S608 - constant fragments, values are bound parameters
        "COALESCE(st.n_tup_ins, 0)::bigint AS ins, COALESCE(st.n_tup_upd, 0)::bigint AS upd, "
        "COALESCE(st.n_tup_del, 0)::bigint AS del "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_stat_user_tables st ON st.relid = c.oid "
        f"WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition AND {SYSTEM_SCHEMAS} "
        "AND (:all OR n.nspname = ANY(string_to_array(:schemas, ','))) ORDER BY 1, 2",
        all=schemas is None,
        schemas=",".join(schemas or []),
    )


def build(
    query: Query, rules: list[dict[str, str]], *, max_exact: int, source_query: Query | None = None
) -> tuple[dict[str, Any], int | None]:
    """(fingerprint, pg_database_size of the target). ``source_query``: the source catalogue
    filtered by the rules becomes ``expected``."""
    schemas = preflight.scope_schemas(rules)
    params = {"all": schemas is None, "schemas": ",".join(schemas or [])}
    scope = "(:all OR {col} = ANY(string_to_array(:schemas, ',')))"
    found: dict[str, dict[str, Any]] = {}
    out_of_scope: list[str] = []
    for row in tables(query, schemas):
        name = f"{row['s']}.{row['t']}"
        if not preflight.in_scope(rules, row["s"], row["t"]):
            out_of_scope.append(name)
            continue
        found[name] = {
            "columns": [],
            "primaryKey": [],
            "uniques": [],
            "rows": int(row["est"]),
            "exact": False,
            "ins": int(row["ins"]),
            "upd": int(row["upd"]),
            "del": int(row["del"]),
        }
    for row in query(
        "SELECT table_schema AS s, table_name AS t, column_name AS c, data_type AS ty, is_nullable = 'YES' AS n "  # noqa: S608 - constant fragments, values are bound parameters
        f"FROM information_schema.columns WHERE {scope.format(col='table_schema')} "
        "ORDER BY table_schema, table_name, ordinal_position",
        **params,
    ):
        name = f"{row['s']}.{row['t']}"
        if name in found:
            found[name]["columns"].append([row["c"], row["ty"], bool(row["n"])])
    for row in query(
        "SELECT n.nspname AS s, c.relname AS t, i.indisprimary AS p, "  # noqa: S608 - constant fragments, values are bound parameters
        "array_to_string(ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum, ord) "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum ORDER BY k.ord), ',') AS cols "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE (i.indisprimary OR i.indisunique) AND {scope.format(col='n.nspname')} ORDER BY 1, 2, 4",
        **params,
    ):
        name = f"{row['s']}.{row['t']}"
        if name in found:
            cols = [c for c in str(row["cols"]).split(",") if c]
            if row["p"]:
                found[name]["primaryKey"] = cols
            else:
                found[name]["uniques"].append(cols)
    fks = sorted(
        f"{r['src']} {r['def']}"
        for r in query(
            "SELECT n.nspname || '.' || c.relname AS src, pg_get_constraintdef(k.oid) AS def "  # noqa: S608 - constant fragments, values are bound parameters
            "FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE k.contype = 'f' AND {scope.format(col='n.nspname')}",
            **params,
        )
        if r["src"] in found
    )
    sequences = {
        f"{r['s']}.{r['q']}": int(r["v"] or 0)
        for r in query(
            "SELECT schemaname AS s, sequencename AS q, COALESCE(last_value, 0)::bigint AS v FROM pg_sequences "  # noqa: S608 - constant fragments, values are bound parameters
            f"WHERE {scope.format(col='schemaname')} ORDER BY 1, 2",
            **params,
        )
    }
    control = [
        r["t"]
        for r in query(
            "SELECT table_schema || '.' || table_name AS t FROM information_schema.tables "
            "WHERE table_name LIKE 'awsdms\\_%' ORDER BY 1"
        )
    ]
    size = query("SELECT pg_database_size(current_database())::bigint AS b")
    database_bytes = int(size[0]["b"]) if size and size[0].get("b") is not None else None
    for name, t in found.items():
        if t["rows"] <= max_exact:
            sql = f"SELECT count(*)::bigint AS n FROM {quote_table(name)}"  # noqa: S608 - identifiers validated
            t["rows"], t["exact"] = int(query(sql)[0]["n"]), True

    expected = None
    if source_query is not None:
        expected = sorted(
            f"{r['s']}.{r['t']}" for r in tables(source_query, schemas) if preflight.in_scope(rules, r["s"], r["t"])
        )
    fp = {
        "schemaVersion": 1,
        "schemas": schemas,
        "tables": dict(sorted(found.items())),
        "foreignKeys": fks,
        "sequences": sequences,
        "controlTables": control,
        "outOfScope": sorted(out_of_scope),
        "expected": expected,
    }
    return fp, database_bytes


def summary(fp: dict[str, Any], uri: str, database_bytes: int | None) -> dict[str, Any]:
    """The job summary both transports return (pre-flight and the baseline read it)."""
    expected = fp.get("expected")
    return {
        "fingerprintUri": uri,
        "fingerprintHash": preflight.fingerprint_hash(fp),
        "tables": len(fp.get("tables") or {}),
        "rows": preflight.rows_in_scope(fp),
        "expected": None if expected is None else len(expected),
        "outOfScope": len(fp.get("outOfScope") or []),
        "databaseBytes": database_bytes,
    }

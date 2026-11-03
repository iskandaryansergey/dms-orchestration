#!/usr/bin/env python3
"""psql-run / mysql-run: run SQL against a named connection of the job.

    psql-run --connection target --file ddl/010_tables.sql [--file ...] [--single-transaction]
    psql-run --connection target --template reset/custom.sql --var schema=public
    psql-run --connection target --truncate "{{ ctx.resetTables }}" [--cascade]
    psql-run --connection target --truncate-scope [--cascade]    # ctx.resetTables of the spec file;
                                                                 # PostgreSQL skips tables missing on the target
    psql-run --connection target --sql "ANALYZE"

Connections come from ``ORCH_CONNECTIONS_FILE`` (written by orch_job.py, mode
0600); the password reaches the client only through its own environment.
Templates use the job manifest expressions with the job spec as context, but
every value is quoted for SQL (no raw text can be spliced in):

* ``{{ expr }}`` -> a SQL literal: ``'text'`` (quotes escaped), numbers, ``TRUE``/
  ``FALSE``, ``NULL``; a list becomes ``'a', 'b'`` (for ``IN (...)``);
* ``{{ ident:expr }}`` -> a quoted identifier, ``schema.table`` allowed, a list
  becomes ``"a"."b", "c"``; anything that is not a plain identifier is refused.

With ``ORCH_DRY_RUN=1`` (or ``--dry-run``) the SQL is printed and not executed.
A summary goes to ``$ORCH_OUT_DIR/result.json`` unless the job already wrote one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # image layout: /opt/orch/shared/expr.py

from shared import expr  # noqa: E402

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
TEMPLATE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
RDS_CA = "/etc/ssl/certs/rds-global-bundle.pem"


class SqlRunError(RuntimeError):
    pass


def load_json(env_name: str) -> dict[str, Any]:
    path = os.environ.get(env_name)
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text())


def quote_table(name: str, engine: str) -> str:
    parts = name.split(".")
    if not 1 <= len(parts) <= 2 or not all(IDENT.match(p) for p in parts):
        raise SqlRunError(f"unsafe table name {name!r}")
    q = "`" if engine == "mysql" else '"'
    return ".".join(f"{q}{p}{q}" for p in parts)


def truncate_sql(tables: list[str], engine: str, cascade: bool = False) -> str:
    if not tables:
        return ""
    quoted = [quote_table(t, engine) for t in tables]
    if engine == "mysql":
        body = "\n".join(f"TRUNCATE TABLE {t};" for t in quoted)
        return f"SET FOREIGN_KEY_CHECKS = 0;\n{body}\nSET FOREIGN_KEY_CHECKS = 1;\n"
    return f"TRUNCATE TABLE {', '.join(quoted)}{' CASCADE' if cascade else ''};\n"


TRUNCATE_RESULT_SETTING = "orch.truncate_result"
MISSING_LISTED = 10


def truncate_existing_sql(tables: list[str], cascade: bool = False, result_path: Path | None = None) -> str:
    """PostgreSQL: one ``TRUNCATE`` of the listed tables that exist, decided on the server.

    The reset scope comes from DMS table statistics, which keep tables dropped since the run
    they describe; a missing table has no rows to reset. The check and the ``TRUNCATE`` are one
    statement; the skipped tables are named in a NOTICE (psql output), none present is no error.
    After the ``TRUNCATE`` the block records what it did (``truncated``: count, ``missing``: the
    skipped names) in a session setting; with ``result_path`` psql writes that record to the file
    (``\\o``), so the job result carries it, not only the log (``truncate_result``).
    Names are checked identifiers (``quote_table``), so the dynamic statement holds no free text.
    """
    if not tables:
        return ""
    listed = ", ".join(sql_literal(quote_table(t, "postgresql"), "postgresql") for t in tables)
    tail = " CASCADE" if cascade else ""
    body = (
        "DECLARE\n"  # noqa: S608 - names checked by quote_table, passed as literals
        f"  listed text[] := ARRAY[{listed}];\n"
        "  present text;\n"
        "  present_count int;\n"
        "  missing text;\n"
        "  missing_list text[];\n"
        "  missing_count int;\n"
        "BEGIN\n"
        "  SELECT string_agg(t, ', ' ORDER BY o) FILTER (WHERE to_regclass(t) IS NOT NULL),\n"
        "         count(*) FILTER (WHERE to_regclass(t) IS NOT NULL),\n"
        "         string_agg(t, ', ' ORDER BY o) FILTER (WHERE to_regclass(t) IS NULL),\n"
        "         array_agg(t ORDER BY o) FILTER (WHERE to_regclass(t) IS NULL),\n"
        "         count(*) FILTER (WHERE to_regclass(t) IS NULL)\n"
        "    INTO present, present_count, missing, missing_list, missing_count\n"
        "    FROM unnest(listed) WITH ORDINALITY AS u(t, o);\n"
        "  IF missing IS NOT NULL THEN\n"
        "    RAISE NOTICE 'psql-run: skipped % table(s) missing on the target: %', missing_count, missing;\n"
        "  END IF;\n"
        "  IF present IS NOT NULL THEN\n"
        f"    EXECUTE 'TRUNCATE TABLE ' || present || '{tail}';\n"
        "  END IF;\n"
        f"  PERFORM set_config('{TRUNCATE_RESULT_SETTING}',\n"
        "    json_build_object('truncated', present_count, 'missing', missing_list)::text, false);\n"
        "END"
    )
    # `$` is valid in an identifier: pick a dollar-quote tag the body does not hold.
    tag, n = "$orch$", 0
    while tag in body:
        n += 1
        tag = f"$orch_{n}$"
    sql = f"DO {tag}\n{body}\n{tag};\n"
    if result_path is None:
        return sql
    target = result_path.as_posix()
    if any(c in target for c in "'\\\n\r"):
        raise SqlRunError(f"unusable result path {target!r}")
    return (
        sql + "\\set QUIET on\n\\pset tuples_only on\n\\pset format unaligned\n"
        f"\\o '{target}'\n"
        f"SELECT current_setting('{TRUNCATE_RESULT_SETTING}', true);\n"
        "\\o\n\\pset format aligned\n\\pset tuples_only off\n\\set QUIET off\n"
    )


def truncate_result(path: Path, tables: list[str]) -> tuple[str, str, dict[str, Any]]:
    """(status, message, summary) from the record ``truncate_existing_sql`` had psql write.

    ``tables`` counts the tables truncated, ``skippedMissing``/``skippedTables`` (at most
    MISSING_LISTED names) the skipped ones. No record (the block did not finish, e.g. past an
    error with ``--on-error continue``) is a warning: what was truncated is unknown."""
    try:
        text = path.read_text().strip() if path.exists() else ""
        record = json.loads(text) if text else None
    except (OSError, ValueError):
        record = None
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("truncated"), int)
        or isinstance(record["truncated"], bool)
    ):
        return "warn", "no truncate result from the server (see the job log)", {}
    by_quoted = {quote_table(t, "postgresql"): t for t in tables}
    names = [by_quoted.get(str(q), str(q)) for q in record.get("missing") or []]
    count = record["truncated"]
    message = f"truncated {count} table(s)" if count else "nothing to truncate"
    summary: dict[str, Any] = {"tables": count}
    if names:
        more = len(names) - MISSING_LISTED
        listed = ", ".join(names[:MISSING_LISTED]) + (f" and {more} more" if more > 0 else "")
        message += f"; skipped {len(names)} table(s) missing on the target: {listed}"
        summary |= {"skippedMissing": len(names), "skippedTables": names[:MISSING_LISTED]}
    return "ok", message, summary


def sql_literal(value: Any, engine: str) -> str:
    """A value as a SQL literal. MySQL also treats backslash as an escape character."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return ", ".join(sql_literal(v, engine) for v in value)
    text = json.dumps(value, sort_keys=True) if isinstance(value, dict) else str(value)
    if "\x00" in text:
        raise SqlRunError("NUL character in a template value")
    if engine == "mysql":
        text = text.replace("\\", "\\\\")
    return "'" + text.replace("'", "''") + "'"


def sql_identifiers(value: Any, engine: str) -> str:
    names = value if isinstance(value, list) else [value]
    if not names or any(not isinstance(n, str) for n in names):
        raise SqlRunError(f"unsafe identifier {value!r}")
    return ", ".join(quote_table(n, engine) for n in names)


def render_sql(text: str, scope: dict[str, Any], engine: str) -> str:
    """Render a SQL template; see the module docstring for the placeholder forms."""

    def one(m: re.Match[str]) -> str:
        inner = m.group(1)
        if inner.startswith("ident:"):
            return sql_identifiers(expr.evaluate(inner.removeprefix("ident:"), scope), engine)
        return sql_literal(expr.evaluate(inner, scope), engine)

    return TEMPLATE.sub(one, text)


def parse_vars(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SqlRunError(f"--var needs key=value, got {pair!r}")
        out[key] = value
    return out


def client_command(conn: dict[str, Any], engine: str, files: list[Path], on_error: str, single_tx: bool):
    """Return (argv, extra_env) for psql or mysql. ``auth = iam``: PostgreSQL over TLS only, the
    password is a fresh IAM token (orch_job.iam_auth_token), in the environment only."""
    iam = conn.get("auth") == "iam"
    if iam and engine == "mysql":
        raise SqlRunError("IAM authentication is supported for PostgreSQL connections only")
    if iam and (conn.get("sslMode") or "verify-full") not in ("require", "verify-ca", "verify-full"):
        raise SqlRunError("IAM authentication needs TLS (sslMode require, verify-ca or verify-full)")
    if engine == "mysql":
        argv = [
            "mysql",
            f"--host={conn['host']}",
            f"--port={conn.get('port') or 3306}",
            f"--user={conn['username']}",
            f"--database={conn['database']}",
            "--ssl-mode=VERIFY_IDENTITY",
            f"--ssl-ca={os.environ.get('ORCH_CA_BUNDLE', RDS_CA)}",
        ]
        if on_error == "continue":
            argv.append("--force")
        argv += ["-e", "; ".join(f"source {f}" for f in files)]
        return argv, {"MYSQL_PWD": conn["password"]}
    argv = ["psql", "-X", "-v", f"ON_ERROR_STOP={'1' if on_error == 'stop' else '0'}"]
    if single_tx:
        argv.append("--single-transaction")
    for f in files:
        argv += ["-f", str(f)]
    password = conn.get("password")
    if iam:
        import orch_job

        password = orch_job.iam_auth_token(conn)
    env = {
        "PGHOST": str(conn["host"]),
        "PGPORT": str(conn.get("port") or 5432),
        "PGDATABASE": str(conn["database"]),
        "PGUSER": str(conn["username"]),
        "PGPASSWORD": str(password),
        "PGSSLMODE": conn.get("sslMode") or "verify-full",
        "PGSSLROOTCERT": os.environ.get("ORCH_CA_BUNDLE", RDS_CA),
        "PGAPPNAME": "dms-orchestration",
    }
    return argv, env


def scope_tables(spec: dict[str, Any]) -> list[str]:
    """``ctx.resetTables`` of the spec file: the full list, also above the payload cap."""
    ctx = spec.get("ctx") or {}
    tables = ctx.get("resetTables") or []
    if not isinstance(tables, list) or not all(isinstance(t, str) for t in tables):
        raise SqlRunError("ctx.resetTables of the job spec is not a list of table names")
    count = ctx.get("resetTablesCount")
    if count is not None and int(count) != len(tables):
        raise SqlRunError(f"{len(tables)} table(s) in the spec, the decision says {count}")
    return tables


def build_plan(
    args: argparse.Namespace,
    spec: dict[str, Any],
    engine: str,
    tmp: Path,
    scope_result: list[tuple[Path, list[str]]] | None = None,
) -> list[tuple[str, Path]]:
    """List of (label, sql file) to run, rendering templates and generated SQL into tmp.
    A PostgreSQL ``--truncate-scope`` step adds (result file, tables) to ``scope_result``."""
    scope_result = [] if scope_result is None else scope_result
    scope = {
        "ctx": spec.get("ctx") or {},
        "run": {"runId": spec.get("runId"), "phase": spec.get("phase"), "dryRun": spec.get("dryRun")},
        "failure": spec.get("failure") or {},
        "var": parse_vars(args.var),
        **(spec.get("previous") or {}),
    }
    plan: list[tuple[str, Path]] = []
    for f in args.file:
        path = Path(f)
        if not path.is_file():
            raise SqlRunError(f"SQL file not found: {f}")
        plan.append((f, path))
    for i, t in enumerate(args.template):
        path = Path(t)
        if not path.is_file():
            raise SqlRunError(f"template not found: {t}")
        rendered = tmp / f"template-{i}.sql"
        rendered.write_text(render_sql(path.read_text(), scope, engine))
        plan.append((f"{t} (rendered)", rendered))
    if args.sql:
        inline = tmp / "inline.sql"
        inline.write_text(args.sql.rstrip(";") + ";\n")
        plan.append(("--sql", inline))
    if args.truncate is not None or args.truncate_scope:
        tables = (
            scope_tables(spec) if args.truncate_scope else [t.strip() for t in args.truncate.split(",") if t.strip()]
        )
        if args.truncate_scope and engine == "postgresql":
            result_path = tmp / "truncate-result.json"
            result_path.unlink(missing_ok=True)
            sql = truncate_existing_sql(tables, args.cascade, result_path)
            if sql:
                scope_result.append((result_path, tables))
        else:
            sql = truncate_sql(tables, engine, args.cascade)
        if sql:
            gen = tmp / "truncate.sql"
            gen.write_text(sql)
            plan.append((f"truncate {len(tables)} table(s)", gen))
    return plan


def write_result(status: str, message: str, summary: dict[str, Any]) -> None:
    out = os.environ.get("ORCH_OUT_DIR")
    if not out:
        return
    path = Path(out) / "result.json"
    if not path.exists():
        path.write_text(json.dumps({"status": status, "message": message, "summary": summary}))


def main(argv: list[str] | None = None, runner: Any = subprocess.run) -> int:
    p = argparse.ArgumentParser(
        prog="psql-run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--connection", required=True)
    p.add_argument("--file", action="append", default=[])
    p.add_argument("--template", action="append", default=[])
    p.add_argument("--var", action="append", default=[])
    p.add_argument("--sql")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--truncate", help="comma-separated schema.table list (may be empty)")
    scope.add_argument(
        "--truncate-scope", action="store_true", help="truncate ctx.resetTables of the job spec file (full list)"
    )
    p.add_argument("--cascade", action="store_true", help="TRUNCATE ... CASCADE (PostgreSQL)")
    p.add_argument("--on-error", choices=["stop", "continue"], default="stop")
    p.add_argument("--single-transaction", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--engine", choices=["postgresql", "mysql"], help="override the connection engine")
    args = p.parse_args(argv)

    try:
        conns = load_json("ORCH_CONNECTIONS_FILE")
        if args.connection not in conns:
            raise SqlRunError(f"connection {args.connection!r} not available (have: {sorted(conns)})")
        conn = conns[args.connection]
        engine = args.engine or ("mysql" if "mysql" in str(conn.get("engine", "")).lower() else "postgresql")
        spec = load_json("ORCH_JOB_SPEC_FILE")
        tmp = Path(os.environ.get("ORCH_TMP_DIR", "/tmp/orch"))  # noqa: S108
        tmp.mkdir(parents=True, exist_ok=True)
        scope_result: list[tuple[Path, list[str]]] = []
        plan = build_plan(args, spec, engine, tmp, scope_result)
    except (SqlRunError, expr.ExprError, OSError, ValueError) as err:
        print(f"psql-run: {err}", file=sys.stderr)
        write_result("fail", str(err), {})
        return 2

    labels = [label for label, _ in plan]
    if not plan:
        print("psql-run: nothing to do")
        write_result("ok", "nothing to do", {"steps": []})
        return 0
    dry = args.dry_run or os.environ.get("ORCH_DRY_RUN") == "1"
    if dry:
        for label, path in plan:
            print(f"-- dry run: {label}\n{path.read_text()}")
        write_result("ok", f"dry run: {len(plan)} step(s) not executed", {"steps": labels, "dryRun": True})
        return 0

    try:
        argv_, extra_env = client_command(
            conn, engine, [path for _, path in plan], args.on_error, args.single_transaction
        )
    except SqlRunError as err:
        print(f"psql-run: {err}", file=sys.stderr)
        write_result("fail", str(err), {"steps": labels})
        return 2
    print(f"psql-run: {engine} {conn.get('host')}/{conn.get('database')}: {', '.join(labels)}")
    proc = runner(argv_, env={**os.environ, **extra_env}, check=False)
    if proc.returncode != 0:
        write_result("fail", f"{engine} client exit code {proc.returncode}", {"steps": labels})
        return proc.returncode
    status, message, summary = "ok", f"{len(plan)} step(s) executed", {"steps": labels}
    for result_path, tables in scope_result:
        status, detail, counts = truncate_result(result_path, tables)
        message, summary = f"{message}; {detail}", {**summary, **counts}
    write_result(status, message, summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())

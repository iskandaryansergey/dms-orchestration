"""Lambda SQL runtime: one ``sql:`` job of a job set on a registered PostgreSQL connection.

Input ``{"spec": {...routing spec from prepare_job...}, "attempt": n}``. The job reads its full
spec from ``spec.json`` (as the project job role; the payload keeps a reset list above the cap
in S3 only), then, with ``sql``:

* ``file``: bundle files read through ``builtin_jobs.bundle_file`` (checked against the pinned
  ``bundleSha256``, JobSetTampered otherwise), each sent as one message (one implicit
  transaction); ``singleTransaction`` wraps all of them in ``BEGIN`` / ``COMMIT``;
* ``inline``: the same, for one short statement list in the job set;
* ``truncateScope``: ``TRUNCATE`` of the full ``ctx.resetTables`` in one statement
  (``cascade``: ``CASCADE``); tables missing on the target are skipped and named in the
  message (``skippedMissing``); a dry run only counts.

``{{ expr }}`` in the SQL becomes a SQL literal and ``{{ ident:expr }}`` a quoted identifier,
from ``ctx``, ``run``, ``failure``, ``var`` (``sql.vars``) and earlier results; nothing is spliced.
Sessions are TLS ``verify-full`` with the pinned RDS CA bundle (``shared/sqlclient.py``) and
read-only in a dry run: a write fails the job with ``ReadOnlySqlTransaction``.

Output: the job result, also written to ``resultKey`` (``result.json``). The first row of the
last statement, if any, becomes ``summary.result`` (at most 50 columns, short values): return
counts and flags there, never row data.
"""

from __future__ import annotations

import contextlib
import datetime
import decimal
import json
import re
from typing import Any

from botocore.exceptions import ClientError

from lambdas.builtin_jobs.handler import artifacts_s3, bundle_file
from shared import expr, jobset, log, sqlclient
from shared.errors import JobSetTampered

LOGGER = log.get_logger("sql_jobs")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
TEMPLATE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
# In a dry run the session is read-only; SQL that switches that off is refused before it runs.
READ_ONLY_OFF = re.compile(r"transaction_read_only|\bread\s+write\b", re.I)
TIMEOUT_MARGIN_SECONDS = 30
SUMMARY_COLUMNS = 50
SUMMARY_TEXT = 256


class SqlJobError(ValueError):
    pass


def result(status: str, message: str, **summary: Any) -> dict[str, Any]:
    return {"status": status, "message": message[:4000], "summary": summary, "artifacts": []}


# --- SQL text ---------------------------------------------------------------------------------


def quote_table(name: str) -> str:
    parts = name.split(".") if isinstance(name, str) else []
    if not 1 <= len(parts) <= 2 or not all(IDENT.match(p) for p in parts):
        raise SqlJobError(f"unsafe identifier {name!r}")
    return ".".join(f'"{p}"' for p in parts)


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return ", ".join(sql_literal(v) for v in value)
    text = json.dumps(value, sort_keys=True) if isinstance(value, dict) else str(value)
    if "\x00" in text:
        raise SqlJobError("NUL character in a template value")
    # standard_conforming_strings is on for every session (sqlclient): only quotes need doubling.
    return "'" + text.replace("'", "''") + "'"


def render(text: str, scope: dict[str, Any]) -> str:
    def one(m: re.Match[str]) -> str:
        inner = m.group(1)
        if inner.startswith("ident:"):
            value = expr.evaluate(inner.removeprefix("ident:"), scope)
            names = value if isinstance(value, list) else [value]
            if not names:
                raise SqlJobError(f"empty identifier list for {inner!r}")
            return ", ".join(quote_table(n) for n in names)
        return sql_literal(expr.evaluate(inner, scope))

    return TEMPLATE.sub(one, text)


def json_safe(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in list(row)[:SUMMARY_COLUMNS]:
        value = row[key]
        if isinstance(value, decimal.Decimal | datetime.date | datetime.time | datetime.timedelta):
            value = value.isoformat() if hasattr(value, "isoformat") else str(value)
        elif not isinstance(value, str | int | float | bool | type(None)):
            value = str(value)
        out[str(key)] = value[:SUMMARY_TEXT] if isinstance(value, str) else value
    return out


# --- the job ----------------------------------------------------------------------------------


def load_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """The full spec prepare_job wrote, read as the project job role; it must be this job's."""
    uri = str(spec.get("specS3Uri") or "")
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    try:
        full = json.loads(artifacts_s3(spec).get_object(Bucket=bucket, Key=key)["Body"].read())
    except ClientError as err:
        raise SqlJobError(f"cannot read the job spec.json: {err.response.get('Error', {}).get('Code')}") from None
    for field in ("runId", "phase", "name", "recordSk"):
        if full.get(field) != spec.get(field):
            raise SqlJobError(f"spec.json is not the spec of this job ({field} differs)")
    return full


def scope_tables(spec: dict[str, Any]) -> list[str]:
    """``ctx.resetTables`` of the full spec: the whole list, also above the payload cap."""
    ctx = spec.get("ctx") or {}
    tables = ctx.get("resetTables") or []
    if not isinstance(tables, list) or not all(isinstance(t, str) for t in tables):
        raise SqlJobError("ctx.resetTables of the job spec is not a list of table names")
    count = ctx.get("resetTablesCount")
    if count is not None and int(count) != len(tables):
        raise SqlJobError(f"{len(tables)} table(s) in the spec, the decision says {count}")
    return tables


def steps(spec: dict[str, Any], sql: dict[str, Any]) -> list[tuple[str, str]]:
    scope = {
        "ctx": spec.get("ctx") or {},
        "run": {"runId": spec.get("runId"), "phase": spec.get("phase"), "dryRun": bool(spec.get("dryRun"))},
        "failure": spec.get("failure") or {},
        "var": sql.get("vars") or {},
        **(spec.get("previous") or {}),
    }
    if sql.get("inline"):
        return [("inline", render(str(sql["inline"]), scope))]
    out = []
    for rel in jobset.sql_files(sql):
        out.append((rel, render(bundle_file(spec, rel).decode("utf-8"), scope)))
    return out


def timeout_seconds(spec: dict[str, Any]) -> int:
    return max(10, int(spec.get("timeoutSeconds") or 840) - TIMEOUT_MARGIN_SECONDS)


MISSING_LISTED = 10
MISSING_TABLES_SQL = "SELECT t AS missing FROM unnest(CAST(:names AS text[])) AS t WHERE to_regclass(t) IS NULL"


def truncate(spec: dict[str, Any], sql: dict[str, Any], name: str, dry: bool) -> dict[str, Any]:
    tables = scope_tables(spec)
    base = {"connection": name, "dryRun": dry}
    if not tables:
        return result("ok", "nothing to truncate", **base, tables=0)
    quoted = {quote_table(t): t for t in tables}
    with sqlclient.connect(spec, name, read_only=dry, timeout_seconds=timeout_seconds(spec)) as db:
        if dry:
            return result("ok", f"dry run: would truncate {len(tables)} table(s)", **base, wouldTruncate=len(tables))
        # The reset list comes from DMS table statistics, which keep tables dropped since the
        # last run: a missing table has no rows to reset, so it is skipped, not a failure.
        gone = {row["missing"] for row in db.query(MISSING_TABLES_SQL, names=list(quoted))}
        present = [q for q in quoted if q not in gone]
        if present:
            cascade = " CASCADE" if sql.get("cascade") else ""
            db.execute("TRUNCATE TABLE " + ", ".join(present) + cascade)  # one statement: all tables or none
    message = f"truncated {len(present)} table(s)" if present else "nothing to truncate"
    if gone:
        names = sorted(quoted[q] for q in gone)
        more = len(names) - MISSING_LISTED
        listed = ", ".join(names[:MISSING_LISTED]) + (f" and {more} more" if more > 0 else "")
        message += f"; skipped {len(names)} table(s) missing on the target: {listed}"
    return result("ok", message, **base, tables=len(present), **({"skippedMissing": len(gone)} if gone else {}))


def run_sql(spec: dict[str, Any], sql: dict[str, Any], name: str, dry: bool) -> dict[str, Any]:
    plan = steps(spec, sql)
    if dry and any(READ_ONLY_OFF.search(text) for _, text in plan):
        raise SqlJobError("dry run: the SQL changes the read-only setting of the session; nothing was run")
    single = bool(sql.get("singleTransaction")) and len(plan) > 1
    last: list[dict[str, Any]] = []
    with sqlclient.connect(spec, name, read_only=dry, timeout_seconds=timeout_seconds(spec)) as db:
        if single:
            db.execute("BEGIN")
        for label, text in plan:
            try:
                last = db.execute(text)
            except sqlclient.SqlError as err:
                if single:
                    with contextlib.suppress(sqlclient.SqlError):
                        db.execute("ROLLBACK")
                raise sqlclient.SqlError(err.name, f"{err.message} ({label})", err.code) from None
        if single:
            db.execute("COMMIT")
    labels = [label for label, _ in plan]
    summary: dict[str, Any] = {"connection": name, "dryRun": dry, "steps": labels}
    if last:
        summary["result"] = json_safe(last[0])
    prefix = "dry run (read-only): " if dry else ""
    return result("ok", f"{prefix}{len(plan)} step(s) executed", **summary)


def run(spec: dict[str, Any]) -> dict[str, Any]:
    full = load_spec(spec)
    sql = full.get("sql") or {}
    name = str(sql.get("connection") or "target")
    dry = bool(full.get("dryRun"))
    if sql.get("truncateScope"):
        return truncate(full, sql, name, dry)
    return run_sql(full, sql, name, dry)


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    spec = event["spec"]
    try:
        out = run(spec)
    except JobSetTampered as err:
        out = result("fail", f"JobSetTampered: {err}")
    except (sqlclient.SqlError, SqlJobError, ValueError, expr.ExprError, UnicodeDecodeError) as err:
        out = result("fail", str(err))
    bucket = str(spec.get("artifactsS3Prefix") or "").removeprefix("s3://").partition("/")[0]
    if bucket and spec.get("resultKey"):
        artifacts_s3(spec).put_object(
            Bucket=bucket, Key=spec["resultKey"], Body=json.dumps(out).encode(), ContentType="application/json"
        )
    log.info(LOGGER, "sql job finished", runId=spec.get("runId"), job=spec.get("name"), status=out["status"])
    return out

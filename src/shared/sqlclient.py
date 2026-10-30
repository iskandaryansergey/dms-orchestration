"""PostgreSQL client of the Lambda SQL runtime: pg8000 (pure Python), vendored in the Lambda zip.

Nothing is downloaded at run time: the driver comes from ``lambdas/sql_jobs/vendor`` (hashed
requirements, ``make vendor-check``) and the CA bundle from ``rds-global-bundle.pem`` next to it,
checked against ``CA_BUNDLE_SHA256`` before every TLS context is built.

* **TLS** ``verify-full`` (the default) or ``verify-ca`` with the RDS CA bundle; any other
  ``sslMode`` is refused: the Lambda never talks to a database without a verified certificate.
* **Login** as the project job role: the secret (``secretArn``) through Secrets Manager, or an
  IAM token (``auth = iam``) signed with the job role's credentials. Values stay in memory.
* **Parameters** are ``:name`` placeholders bound by the driver (the Data API form), never spliced.
* **Session settings** in the startup packet: ``statement_timeout`` from the job timeout,
  ``standard_conforming_strings = on`` and, for read-only sessions (dry runs, checks),
  ``default_transaction_read_only = on``: a write fails with ``ReadOnlySqlTransaction``.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import sys
from pathlib import Path
from typing import Any

from shared import aws

SQL_JOBS_DIR = Path(__file__).resolve().parent.parent / "lambdas" / "sql_jobs"
VENDOR_DIR = SQL_JOBS_DIR / "vendor"
# Amazon RDS global CA bundle, shipped in the zip and pinned (make vendor-check).
CA_BUNDLE = SQL_JOBS_DIR / "rds-global-bundle.pem"
CA_BUNDLE_SHA256 = "90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b"

TLS_MODES = ("verify-full", "verify-ca")
APPLICATION_NAME = "dms-orchestration"
CONNECT_TIMEOUT_SECONDS = 30
# SQLSTATE -> the name a job result carries.
ERROR_NAMES = {
    "25006": "ReadOnlySqlTransaction",
    "57014": "QueryCanceled",
    "28000": "InvalidAuthorization",
    "28P01": "InvalidPassword",
    "42501": "InsufficientPrivilege",
    "40P01": "DeadlockDetected",
    "55P03": "LockNotAvailable",
}

_driver: Any = None


class SqlError(Exception):
    """A failed connection or statement. The text never holds secrets, hosts' passwords or row data."""

    def __init__(self, name: str, message: str, code: str | None = None):
        super().__init__(f"{name}: {message}")
        self.name, self.message, self.code = name, message, code


def set_driver(module: Any) -> None:
    """Test hook: replace ``pg8000.native`` (None: the vendored one again)."""
    global _driver
    _driver = module


def driver() -> Any:
    """``pg8000.native`` from the vendored tree (imported on first use)."""
    global _driver
    if _driver is None:
        vendor = str(VENDOR_DIR)
        if vendor not in sys.path:
            sys.path.insert(0, vendor)
        import pg8000.native

        _driver = pg8000.native
    return _driver


def verified_ca(path: Path = CA_BUNDLE, expected: str = CA_BUNDLE_SHA256) -> Path:
    data = path.read_bytes() if path.is_file() else b""
    if hashlib.sha256(data).hexdigest() != expected:
        raise SqlError("CaBundleMismatch", f"{path.name} does not match its pinned sha256")
    return path


def ssl_context(mode: str | None, cafile: Path | None = None) -> ssl.SSLContext:
    mode = mode or "verify-full"
    if mode not in TLS_MODES:
        raise SqlError("TlsRequired", f"sslMode {mode!r} is not allowed; use verify-full or verify-ca")
    ctx = ssl.create_default_context(cafile=str(cafile or verified_ca()))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = mode == "verify-full"
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def login(spec: dict[str, Any], name: str, conn: dict[str, Any]) -> dict[str, Any]:
    """{host, port, database, user, password} of a registered connection, as the project job role."""
    role = spec.get("jobRoleArn")
    session = f"{spec.get('runId', 'run')}-{spec.get('name', 'job')}"
    if conn.get("auth") == "iam":
        if not (conn.get("host") and conn.get("iamUser")):
            raise ValueError(f"connection {name!r}: auth = iam needs host and iamUser")
        port = int(conn.get("port") or 5432)
        token = aws.job_client("rds", role, session_name=session).generate_db_auth_token(
            DBHostname=conn["host"],
            Port=port,
            DBUsername=conn["iamUser"],
            Region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        )
        return {
            "host": conn["host"],
            "port": port,
            "database": conn.get("database"),
            "user": conn["iamUser"],
            "password": token,
        }
    if not conn.get("secretArn"):
        raise ValueError(f"connection {name!r} has no secretArn")
    raw = aws.job_client("secretsmanager", role, session_name=session).get_secret_value(SecretId=conn["secretArn"])
    secret = json.loads(raw["SecretString"])
    host = conn.get("host") or secret.get("host")
    if not host:
        raise ValueError(f"connection {name!r} has no host (connection or secret)")
    return {
        "host": host,
        "port": int(conn.get("port") or secret.get("port") or 5432),
        "database": conn.get("database") or secret.get("dbname"),
        "user": secret.get("username"),
        "password": secret.get("password"),
    }


def session_params(read_only: bool, timeout_seconds: int) -> dict[str, str]:
    params = {
        "statement_timeout": str(max(1, int(timeout_seconds)) * 1000),
        "standard_conforming_strings": "on",
    }
    if read_only:
        params["default_transaction_read_only"] = "on"
    return params


def _error(err: BaseException) -> SqlError:
    fields = err.args[0] if err.args and isinstance(err.args[0], dict) else {}
    code = fields.get("C")
    if code:
        name = ERROR_NAMES.get(code, f"DatabaseError {code}")
        return SqlError(name, str(fields.get("M") or "statement failed")[:500], code)
    return SqlError("DriverError", type(err).__name__)


class Session:
    """One open connection. ``query`` binds ``:name`` parameters; ``execute`` runs a script."""

    def __init__(self, raw: Any, name: str):
        self.raw, self.name = raw, name

    def _rows(self, rows: Any) -> list[dict[str, Any]]:
        cols = [c["name"] for c in (self.raw.columns or [])]
        return [dict(zip(cols, row, strict=False)) for row in rows or []]

    def query(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        try:
            return self._rows(self.raw.run(sql, **params))
        except driver().Error as err:
            raise _error(err) from None

    def execute(self, script: str) -> list[dict[str, Any]]:
        """A script of one or more statements in one simple-protocol message (one implicit
        transaction); the rows of its last statement, if any."""
        try:
            return self._rows(self.raw.run(script))
        except driver().Error as err:
            raise _error(err) from None

    def close(self) -> None:
        try:
            self.raw.close()
        except (driver().Error, OSError):
            pass

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def connect(spec: dict[str, Any], name: str, *, read_only: bool, timeout_seconds: int) -> Session:
    """A TLS session on the registered connection ``name`` of the job spec."""
    conn = (spec.get("connections") or {}).get(name)
    if conn is None:
        raise ValueError(
            f"connection {name!r} is not registered for this task (have: {sorted(spec.get('connections') or {})})"
        )
    if "postgres" not in str(conn.get("engine") or "postgresql").lower():
        raise ValueError(f"connection {name!r}: the Lambda SQL runtime supports PostgreSQL only")
    tls = ssl_context(conn.get("sslMode"))
    creds = login(spec, name, conn)
    try:
        raw = driver().Connection(
            user=creds["user"],
            password=creds["password"],
            host=creds["host"],
            port=creds["port"],
            database=creds["database"],
            ssl_context=tls,
            timeout=CONNECT_TIMEOUT_SECONDS + int(timeout_seconds),
            application_name=APPLICATION_NAME,
            startup_params=session_params(read_only, timeout_seconds),
        )
    except driver().Error as err:
        raise _error(err) from None
    except OSError as err:
        raise SqlError("ConnectionFailed", f"connection {name!r}: {type(err).__name__}") from None
    return Session(raw, name)

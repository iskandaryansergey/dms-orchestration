"""Lambda SQL runtime: vendored driver, shared/sqlclient.py, the sql_jobs handler (fake pg8000)."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import ssl
from pathlib import Path
from typing import Any

import pytest
import vendor_sql_jobs

ROOT = Path(__file__).resolve().parents[2]


# --- vendored driver and CA bundle (make vendor-check) --------------------------------


def test_the_committed_ca_bundle_matches_its_pin():
    from shared import sqlclient

    assert vendor_sql_jobs.check_ca(sqlclient.CA_BUNDLE, sqlclient.CA_BUNDLE_SHA256) == []
    assert sqlclient.CA_BUNDLE.read_bytes().count(b"BEGIN CERTIFICATE") > 10


def test_a_changed_ca_bundle_is_refused(tmp_path):
    pem = tmp_path / "ca.pem"
    pem.write_bytes(b"-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    problems = vendor_sql_jobs.check_ca(pem, "0" * 64)
    assert problems and "sha256" in problems[0]
    assert vendor_sql_jobs.check_ca(tmp_path / "missing.pem", "0" * 64)


def test_the_committed_vendor_tree_is_pure_python_and_pinned():
    vendor = vendor_sql_jobs.VENDOR
    names = {p.name.split("-")[0].lower() for p in vendor.glob("*.dist-info")}
    assert {"pg8000", "scramp", "asn1crypto"} <= names
    assert vendor_sql_jobs.impure(vendor) == []
    pinned = vendor_sql_jobs.REQUIREMENTS.read_text(encoding="utf-8")
    for dist in vendor.glob("*.dist-info"):
        name, version = dist.name.removesuffix(".dist-info").rsplit("-", 1)
        assert f"{name.lower().replace('_', '-')}=={version}" in pinned.lower(), dist.name
    assert "--hash=sha256:" in pinned
    assert not (vendor / ".lock").exists()


def test_impure_wheels_are_reported(tmp_path):
    dist = tmp_path / "native-1.0.dist-info"
    dist.mkdir()
    (dist / "WHEEL").write_text("Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp313-cp313-manylinux_2_17_x86_64\n")
    assert vendor_sql_jobs.impure(tmp_path) == ["native-1.0.dist-info"]


def _fake_install(source: Path, extra: dict[str, bytes] | None = None) -> Any:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kw: Any) -> Any:
        from types import SimpleNamespace

        calls.append(argv)
        target = Path(argv[argv.index("--target") + 1])
        shutil.copytree(source, target, dirs_exist_ok=True)
        (target / ".lock").write_text("")
        for rel, data in (extra or {}).items():
            (target / rel).write_bytes(data)
        return SimpleNamespace(returncode=0)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_vendor_check_rebuilds_with_hashes_and_compares_every_file(tmp_path, capsys):
    vendor = tmp_path / "vendor"
    (vendor / "pkg").mkdir(parents=True)
    (vendor / "pkg" / "__init__.py").write_bytes(b"x = 1\n")
    runner = _fake_install(vendor)
    assert vendor_sql_jobs.check(vendor=vendor, runner=runner, ca_ok=True) == 0
    argv = runner.calls[0]
    assert "--require-hashes" in argv and "--no-deps" in argv and "--only-binary" in argv
    assert str(vendor_sql_jobs.REQUIREMENTS) in argv

    changed = _fake_install(vendor, {"pkg/__init__.py": b"x = 2\n", "pkg/extra.py": b""})
    assert vendor_sql_jobs.check(vendor=vendor, runner=changed, ca_ok=True) == 1
    err = capsys.readouterr().err
    assert "pkg/__init__.py" in err and "pkg/extra.py" in err


def test_vendor_build_replaces_the_tree_without_lock_files(tmp_path):
    source = tmp_path / "src"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "a.py").write_bytes(b"")
    vendor = tmp_path / "vendor"
    (vendor / "stale").mkdir(parents=True)
    assert vendor_sql_jobs.build(vendor=vendor, runner=_fake_install(source)) == 0
    assert sorted(vendor_sql_jobs.tree(vendor)) == ["pkg/a.py"]


def test_sha256_helper_matches_hashlib(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"abc")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "f.cpython-313.pyc").write_bytes(b"local import")
    assert vendor_sql_jobs.tree(tmp_path) == {"f": hashlib.sha256(b"abc").hexdigest()}


def test_vendor_dir_is_excluded_from_lint_and_coverage():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "src/lambdas/sql_jobs/vendor" in text
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "src/lambdas/sql_jobs/vendor/** -text" in attrs


@pytest.mark.parametrize("target", ["vendor-check", "vendor"])
def test_make_targets_exist(target):
    assert f"\n{target}:" in (ROOT / "Makefile").read_text(encoding="utf-8")


# --- a fake pg8000.native ----------------------------------------------------------------

WRITES = re.compile(r"^\s*(insert|update|delete|truncate|create|drop|alter|grant)\b", re.I | re.M)


class FakeError(Exception):
    pass


class FakeDatabaseError(FakeError):
    pass


class FakeConnection:
    """pg8000.native.Connection: answers by SQL substring; a read-only session refuses writes."""

    def __init__(self, server: FakeServer, **kw: Any):
        self.server, self.kw = server, kw
        self.columns: list[dict[str, Any]] | None = None
        self.closed = False
        server.connections.append(self)

    @property
    def read_only(self) -> bool:
        return (self.kw.get("startup_params") or {}).get("default_transaction_read_only") == "on"

    def run(self, sql: str, **params: Any) -> list[list[Any]] | None:
        self.server.calls.append({"sql": sql, "params": params, "readOnly": self.read_only})
        if self.read_only and WRITES.search(sql):
            word = WRITES.search(sql).group(1).upper()
            raise FakeDatabaseError(
                {"S": "ERROR", "C": "25006", "M": f"cannot execute {word} in a read-only transaction"}
            )
        for needle, err in self.server.errors.items():
            if needle in sql:
                raise FakeDatabaseError(err)
        for needle, rows in self.server.answers.items():
            if needle in sql:
                cols = list(rows[0]) if rows else []
                self.columns = [{"name": c} for c in cols]
                return [[r[c] for c in cols] for r in rows]
        self.columns = None
        return None

    def close(self) -> None:
        self.closed = True


class FakeServer:
    """The module object handed to sqlclient.set_driver (Connection, Error, DatabaseError)."""

    Error = FakeError
    DatabaseError = FakeDatabaseError
    InterfaceError = FakeError

    def __init__(self, answers: dict[str, list[dict[str, Any]]] | None = None, refuse: Exception | None = None):
        self.answers = answers or {}
        self.errors: dict[str, dict[str, str]] = {}
        self.refuse = refuse
        self.calls: list[dict[str, Any]] = []
        self.connections: list[FakeConnection] = []

    def Connection(self, **kw: Any) -> FakeConnection:  # noqa: N802 - the driver's class name
        if self.refuse is not None:
            raise self.refuse
        return FakeConnection(self, **kw)


class FakeSts:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def assume_role(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        return {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "ST"}}


class FakeSecrets:
    def __init__(self, values: dict[str, dict[str, Any]]):
        self.values = values
        self.calls: list[str] = []

    def get_secret_value(self, SecretId: str) -> dict[str, Any]:  # noqa: N803 - boto3 argument
        self.calls.append(SecretId)
        return {"SecretString": json.dumps(self.values[SecretId])}


class FakeRdsToken:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate_db_auth_token(self, **kw: Any) -> str:
        self.calls.append(kw)
        return "iam-token-value"


ROLE = "arn:aws:iam::111122223333:role/dms-orch-test-job-ledger"
TARGET_SECRET = "arn:aws:secretsmanager:eu-west-2:111122223333:secret:t"
TARGET = {
    "engine": "postgresql",
    "secretArn": TARGET_SECRET,
    "host": "target.cluster-xyz.eu-west-2.rds.amazonaws.com",
    "port": 5432,
    "database": "ledger",
    "sslMode": "verify-full",
}
SECRET_VALUES = {TARGET_SECRET: {"username": "runner", "password": "pw-secret-value", "dbname": "other"}}


@pytest.fixture
def pg(aws):
    from shared import aws as shared_aws
    from shared import sqlclient

    server = FakeServer()
    sqlclient.set_driver(server)
    shared_aws.set_client("sts", FakeSts())
    shared_aws.set_client("secretsmanager", FakeSecrets(SECRET_VALUES))
    yield server
    sqlclient.set_driver(None)


def conn_spec(**conns: dict[str, Any]) -> dict[str, Any]:
    return {"runId": "run-1", "name": "job", "jobRoleArn": ROLE, "connections": conns or {"target": TARGET}}


# --- shared/sqlclient.py --------------------------------------------------------------------


def test_connect_verifies_tls_logs_in_as_the_job_role_and_sets_the_session(pg):
    from shared import aws as shared_aws
    from shared import sqlclient

    with sqlclient.connect(conn_spec(), "target", read_only=True, timeout_seconds=120) as db:
        assert db.name == "target"
    (conn,) = pg.connections
    kw = conn.kw
    assert kw["host"] == TARGET["host"] and kw["port"] == 5432 and kw["database"] == "ledger"
    assert kw["user"] == "runner" and kw["password"] == "pw-secret-value"
    assert kw["application_name"] == "dms-orchestration"
    tls = kw["ssl_context"]
    assert tls.check_hostname is True and tls.verify_mode == ssl.CERT_REQUIRED
    assert kw["startup_params"] == {
        "statement_timeout": "120000",
        "standard_conforming_strings": "on",
        "default_transaction_read_only": "on",
    }
    assert conn.closed
    assert shared_aws.client("sts").calls[0]["RoleArn"] == ROLE
    assert shared_aws.client("secretsmanager").calls == [TARGET_SECRET]


def test_a_writable_session_has_no_read_only_setting_and_verify_ca_skips_the_hostname(pg):
    from shared import sqlclient

    spec = conn_spec(target={**TARGET, "sslMode": "verify-ca"})
    sqlclient.connect(spec, "target", read_only=False, timeout_seconds=10).close()
    kw = pg.connections[0].kw
    assert "default_transaction_read_only" not in kw["startup_params"]
    assert kw["ssl_context"].check_hostname is False and kw["ssl_context"].verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("mode", ["require", "prefer", "disable", "allow"])
def test_tls_without_certificate_verification_is_refused(pg, mode):
    from shared import sqlclient

    with pytest.raises(sqlclient.SqlError, match="TlsRequired"):
        sqlclient.connect(conn_spec(target={**TARGET, "sslMode": mode}), "target", read_only=True, timeout_seconds=10)
    assert pg.connections == []


def test_query_binds_named_parameters_and_returns_dicts(pg):
    from shared import sqlclient

    pg.answers = {"FROM pg_class": [{"s": "app", "t": "accounts"}]}
    with sqlclient.connect(conn_spec(), "target", read_only=True, timeout_seconds=10) as db:
        rows = db.query("SELECT ... FROM pg_class WHERE n = ANY(string_to_array(:schemas, ','))", schemas="app")
        assert db.query("SELECT nothing") == []
    assert rows == [{"s": "app", "t": "accounts"}]
    assert pg.calls[0]["params"] == {"schemas": "app"}


def test_a_write_in_a_read_only_session_fails_with_read_only_sql_transaction(pg):
    from shared import sqlclient

    db = sqlclient.connect(conn_spec(), "target", read_only=True, timeout_seconds=10)
    with db, pytest.raises(sqlclient.SqlError) as err:
        db.execute("TRUNCATE TABLE app.accounts;")
    assert err.value.name == "ReadOnlySqlTransaction" and err.value.code == "25006"
    assert str(err.value).startswith("ReadOnlySqlTransaction: cannot execute TRUNCATE")


def test_other_server_errors_keep_their_sqlstate_and_never_the_password(pg):
    from shared import sqlclient

    pg.errors = {"slow": {"C": "57014", "M": "canceling statement due to statement timeout"}, "x": {"C": "XX000"}}
    with sqlclient.connect(conn_spec(), "target", read_only=False, timeout_seconds=10) as db:
        with pytest.raises(sqlclient.SqlError, match="QueryCanceled"):
            db.execute("SELECT slow()")
        with pytest.raises(sqlclient.SqlError, match="DatabaseError XX000: statement failed") as err:
            db.query("SELECT x")
    assert "pw-secret-value" not in str(err.value)


def test_connection_failures_name_the_connection_only(aws):
    from shared import aws as shared_aws
    from shared import sqlclient

    shared_aws.set_client("sts", FakeSts())
    shared_aws.set_client("secretsmanager", FakeSecrets(SECRET_VALUES))
    for refuse, name in (
        (ConnectionRefusedError(111, "refused"), "ConnectionFailed"),
        (FakeError("auth"), "DriverError"),
    ):
        sqlclient.set_driver(FakeServer(refuse=refuse))
        try:
            with pytest.raises(sqlclient.SqlError, match=name) as err:
                sqlclient.connect(conn_spec(), "target", read_only=True, timeout_seconds=10)
            assert "pw-secret-value" not in str(err.value)
        finally:
            sqlclient.set_driver(None)


def test_iam_connections_log_in_with_a_token_of_the_job_role(pg):
    from shared import aws as shared_aws
    from shared import sqlclient

    rds = FakeRdsToken()
    shared_aws.set_client("rds", rds)
    iam = {
        "engine": "postgresql",
        "host": "t.example",
        "port": 5432,
        "database": "ledger",
        "auth": "iam",
        "iamUser": "runner_iam",
    }
    sqlclient.connect(conn_spec(target_iam=iam), "target_iam", read_only=True, timeout_seconds=10).close()
    kw = pg.connections[0].kw
    assert kw["user"] == "runner_iam" and kw["password"] == "iam-token-value"
    assert rds.calls[0]["DBHostname"] == "t.example" and rds.calls[0]["DBUsername"] == "runner_iam"
    with pytest.raises(ValueError, match="iamUser"):
        sqlclient.connect(conn_spec(x={**iam, "iamUser": None}), "x", read_only=True, timeout_seconds=10)


def test_connections_must_be_registered_postgresql_with_a_secret_and_a_host(pg):
    from shared import aws as shared_aws
    from shared import sqlclient

    with pytest.raises(ValueError, match="not registered"):
        sqlclient.connect(conn_spec(), "source", read_only=True, timeout_seconds=10)
    with pytest.raises(ValueError, match="PostgreSQL only"):
        sqlclient.connect(conn_spec(m={**TARGET, "engine": "mysql"}), "m", read_only=True, timeout_seconds=10)
    with pytest.raises(ValueError, match="secretArn"):
        sqlclient.connect(conn_spec(n={**TARGET, "secretArn": None}), "n", read_only=True, timeout_seconds=10)
    shared_aws.set_client("secretsmanager", FakeSecrets({TARGET_SECRET: {"username": "u", "password": "p"}}))
    with pytest.raises(ValueError, match="no host"):
        sqlclient.connect(conn_spec(h={**TARGET, "host": None}), "h", read_only=True, timeout_seconds=10)


def test_a_changed_ca_bundle_stops_every_connection(tmp_path):
    from shared import sqlclient

    pem = tmp_path / "ca.pem"
    pem.write_bytes(sqlclient.CA_BUNDLE.read_bytes() + b"\n")
    with pytest.raises(sqlclient.SqlError, match="CaBundleMismatch"):
        sqlclient.verified_ca(pem)
    assert sqlclient.verified_ca() == sqlclient.CA_BUNDLE


def test_the_vendored_driver_imports_without_the_network():
    from shared import sqlclient

    sqlclient.set_driver(None)
    try:
        mod = sqlclient.driver()
        assert mod.__name__ == "pg8000.native" and str(sqlclient.VENDOR_DIR) in mod.__file__
        assert {"Connection", "DatabaseError", "Error"} <= set(dir(mod))
    finally:
        sqlclient.set_driver(None)

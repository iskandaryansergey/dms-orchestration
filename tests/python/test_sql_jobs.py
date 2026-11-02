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
    assert "modules/dms-orchestration/src/lambdas/sql_jobs/vendor" in text
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "modules/dms-orchestration/src/lambdas/sql_jobs/vendor/** -text" in attrs


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
        answers = self.server.by_host.get(self.kw.get("host"), self.server.answers)
        for needle, rows in answers.items():
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
        self.by_host: dict[str, dict[str, list[dict[str, Any]]]] = {}
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


# --- built-in jobs on a connection with a host: pg8000 instead of the Data API --------------

BUCKET = "dms-orch-artifacts-test"
SOURCE_SECRET = "arn:aws:secretsmanager:eu-west-2:111122223333:secret:s"
SOURCE = {**TARGET, "secretArn": SOURCE_SECRET, "host": "source.cluster-xyz.eu-west-2.rds.amazonaws.com"}
BOTH_SECRETS = {**SECRET_VALUES, SOURCE_SECRET: {"username": "reader", "password": "pw-source"}}


def _builtin(handler: str, conns: dict[str, Any], **kw: Any) -> dict[str, Any]:
    from lambdas.builtin_jobs.handler import handler as run

    spec = {
        "name": "target-verify",
        "runId": "run-1",
        "handler": handler,
        "connections": conns,
        "ctx": {"project": "ledger", "taskKey": "core", "connections": conns, "selectionRules": RULES},
        "dryRun": False,
        "timeoutSeconds": 600,
        "artifactsS3Prefix": f"s3://{BUCKET}/runs/ledger/run-1/jobs/preflight/00-target-verify/",
        "jobRoleArn": ROLE,
        **kw,
    }
    return run({"spec": spec, "attempt": 1})


def _s3() -> Any:
    import boto3

    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    return s3


from test_jobs_lambdas import RULES, SOURCE_ANSWERS, TARGET_ANSWERS, ByClusterRdsData  # noqa: E402


def test_target_verify_through_pg8000_gives_the_data_api_fingerprint(pg):
    """One fixture catalogue, two transports: the pg8000 check is equivalent to the Data API one."""
    from shared import aws as shared_aws

    s3 = _s3()
    shared_aws.set_client("secretsmanager", FakeSecrets(BOTH_SECRETS))
    target = {**TARGET_ANSWERS, "pg_database_size": [{"b": 4242}]}
    data_api_conns = {
        "target": {**TARGET, "host": None, "dataApiClusterArn": "arn:aws:rds:eu-west-2:111122223333:cluster:target"},
        "source": {**SOURCE, "host": None, "dataApiClusterArn": "arn:aws:rds:eu-west-2:111122223333:cluster:source"},
    }
    shared_aws.set_client("rds-data", ByClusterRdsData({"target": target, "source": SOURCE_ANSWERS}))
    via_api = _builtin("builtin:checks/target_verify", data_api_conns)
    key = "runs/ledger/run-1/jobs/preflight/00-target-verify/target-fingerprint.json"
    fp_api = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()

    pg.by_host = {TARGET["host"]: target, SOURCE["host"]: SOURCE_ANSWERS}
    via_pg = _builtin("builtin:checks/target_verify", {"target": TARGET, "source": SOURCE})
    fp_pg = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()

    assert via_pg["status"] == via_api["status"] == "ok", via_pg
    assert via_pg["summary"] == via_api["summary"]  # fingerprintHash, tables, rows, expected, databaseBytes
    assert fp_pg == fp_api
    assert {c.kw["host"] for c in pg.connections} == {TARGET["host"], SOURCE["host"]}
    assert all(c.read_only and c.closed for c in pg.connections)  # a check never writes
    assert any(c["params"].get("schemas") == "app" for c in pg.calls)


def test_row_counts_and_grants_run_through_pg8000_when_the_connection_has_a_host(pg):
    from shared import aws as shared_aws

    shared_aws.set_client("secretsmanager", FakeSecrets(BOTH_SECRETS))
    pg.by_host = {
        TARGET["host"]: {'"app"."a"': [{"n": 10}], '"app"."b"': [{"n": 4}]},
        SOURCE["host"]: {'"app"."a"': [{"n": 10}], '"app"."b"': [{"n": 5}]},
    }
    conns = {"target": TARGET, "source": SOURCE}
    out = _builtin("builtin:checks/row_counts", conns, **{"with": {"tables": ["app.a", "app.b"]}})
    assert out["status"] == "fail" and out["summary"]["mismatched"] == ["app.b"]
    assert all(c.read_only for c in pg.connections)

    info = {"usr": "dms", "wal_level": "logical", "max_slots": 4, "used_slots": 1}
    info |= {"replication": True, "superuser": False, "rds_replication": False}
    pg.by_host = {SOURCE["host"]: {"current_setting('wal_level')": [info], "has_table_privilege": []}}
    grants = _builtin("builtin:checks/postgresql_source_grants", conns, **{"with": {"connection": "source"}})
    assert grants["status"] == "ok", grants


def test_drop_control_tables_through_pg8000_writes_only_outside_dry_runs(pg):
    pg.answers = {"information_schema.tables": [{"table_name": "awsdms_status"}]}
    dry = _builtin("builtin:cleanup/drop_control_tables", {"target": TARGET}, dryRun=True)
    assert dry["status"] == "ok" and dry["summary"]["wouldDrop"] == ["awsdms_status"]
    assert pg.connections[-1].read_only
    out = _builtin("builtin:cleanup/drop_control_tables", {"target": TARGET})
    assert out["status"] == "ok" and out["summary"]["dropped"] == ["awsdms_status"]
    assert not pg.connections[-1].read_only
    assert any('DROP TABLE IF EXISTS "dms_control"."awsdms_status"' in c["sql"] for c in pg.calls)


def test_a_database_error_in_a_built_in_job_is_a_job_failure(pg):
    pg.errors = {"pg_stat_user_tables": {"C": "42501", "M": "permission denied for table pg_class"}}
    out = _builtin("builtin:checks/target_verify", {"target": TARGET}, **{"with": {"source": None}})
    assert out["status"] == "fail" and out["message"].startswith("InsufficientPrivilege: permission denied")
    assert "pw-secret-value" not in json.dumps(out)


def test_a_connection_without_host_or_data_api_fails_the_job(pg):
    from shared import aws as shared_aws

    shared_aws.set_client("secretsmanager", FakeSecrets(BOTH_SECRETS))
    conns = {"target": {**TARGET, "host": None}, "source": SOURCE}
    out = _builtin("builtin:checks/row_counts", conns, **{"with": {"tables": ["app.a"]}})
    assert out["status"] == "fail" and "host" in out["message"] and "Data API" in out["message"]
    assert [c.closed for c in pg.connections] == [True]  # the source session opened first is closed


# --- job sets: sql jobs and the runtime enum ------------------------------------------------


def _normalize(*jobs: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    from shared import jobset

    body: dict[str, Any] = {"schemaVersion": 1, "phases": {"post": list(jobs)}}
    if defaults:
        body["defaults"] = defaults
    return jobset.normalize(body)


def test_a_sql_job_runs_in_lambda_by_default_and_is_capped_at_the_lambda_timeout():
    (job,) = _normalize(
        {"name": "ddl", "sql": {"connection": "target", "file": "ddl/010.sql"}, "timeoutSeconds": 3600},
        defaults={"runtime": "fargate"},
    )["post"]
    assert job["runtime"] == "lambda" and job["timeoutSeconds"] == 840


@pytest.mark.parametrize(
    "job,fragment",
    [
        ({"sql": {"file": "a.sql"}, "command": ["x"]}, "exactly one"),
        ({"sql": {"file": "a.sql"}, "handler": "builtin:a/b"}, "exactly one"),
        ({"sql": {"file": "a.sql"}, "runtime": "fargate"}, "Lambda SQL runtime"),
        ({"sql": {"file": "a.sql"}, "runtime": "ssm"}, "Lambda SQL runtime"),
        ({"sql": {"file": "a.sql", "inline": "SELECT 1"}}, "one of file, inline or truncateScope"),
        ({"sql": {"connection": "target"}}, "one of file, inline or truncateScope"),
        ({"sql": {"truncateScope": False}}, "one of file, inline or truncateScope"),
        ({"sql": {"inline": "SELECT 1", "cascade": True}}, "cascade"),
        ({"sql": {"file": "ddl/../../x.sql"}}, "bad path"),
        ({"sql": {"file": "/etc/x.sql"}}, "does not match"),
        ({"sql": {"file": "a.sql", "mode": "x"}}, "unexpected property 'mode'"),
        ({"sql": {"file": []}}, "fewer than 1"),
        ({"runtime": "ssm"}, "needs 'command'"),
        ({"runtime": "kubernetes", "command": ["x"]}, "kubernetes"),
    ],
)
def test_bad_sql_jobs_are_rejected(job, fragment):
    from shared.errors import InvalidJobSet

    with pytest.raises(InvalidJobSet, match=re.escape(fragment)):
        _normalize({"name": "j", **job})


def test_runtime_enum_is_lambda_ssm_fargate():
    schema_file = ROOT / "modules/dms-orchestration/src/shared/schemas/jobset.schema.json"
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    assert schema["$defs"]["runtime"]["enum"] == ["lambda", "ssm", "fargate"]
    for where in (schema["$defs"]["job"], schema["$defs"]["jobDefaults"]):
        assert where["properties"]["runtime"] == {"$ref": "#/$defs/runtime"}
    (job,) = _normalize({"name": "dump", "runtime": "ssm", "command": ["pg-dump-s3"]})["post"]
    assert job["runtime"] == "ssm"


def test_a_sql_file_list_and_truncate_scope_are_valid():
    jobs = _normalize(
        {"name": "ddl", "sql": {"file": ["a.sql", "b.sql"], "singleTransaction": True, "vars": {"s": "app"}}},
        {"name": "reset", "sql": {"truncateScope": True, "cascade": True}},
    )["post"]
    assert [j["runtime"] for j in jobs] == ["lambda", "lambda"]


# --- prepare_job: sql jobs go to the module's sql_jobs Lambda -------------------------------

SQL_JOBS_ARN = "arn:aws:lambda:eu-west-2:111122223333:function:dms-orch-test-sql_jobs"
LONG = [f"app.t{i:04d}" for i in range(600)]


@pytest.fixture
def env(table, monkeypatch):
    import boto3

    from shared import aws as shared_aws

    monkeypatch.setenv("ARTIFACTS_BUCKET", BUCKET)
    monkeypatch.setenv("BUILTIN_JOBS_ARN", "arn:aws:lambda:eu-west-2:111122223333:function:dms-orch-test-builtin_jobs")
    monkeypatch.setenv("SQL_JOBS_ARN", SQL_JOBS_ARN)
    monkeypatch.setenv("TASK_DEFINITIONS", "{}")
    monkeypatch.setenv("JOB_LAMBDA_ARNS", "[]")
    monkeypatch.setenv("JOB_ROLE_ARNS", json.dumps({"ledger": ROLE}))
    # Routing and rendering of sql jobs; which job may run at all is test_job_trust.py.
    from shared import job_trust

    monkeypatch.setattr(job_trust, "authorize", lambda ev, _store: ev)
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    shared_aws.set_client("s3", s3)
    shared_aws.set_client("sts", FakeSts())
    shared_aws.set_client("secretsmanager", FakeSecrets(BOTH_SECRETS))
    return s3


def _ctx(**kw: Any) -> dict[str, Any]:
    return {
        "project": "ledger",
        "taskKey": "core",
        "migrationType": "full-load",
        "resetTables": ["app.a", "app.b"],
        "connections": {"target": TARGET, "source": SOURCE},
        "jobsS3Prefix": f"s3://{BUCKET}/jobs/ledger/v1/",
        **kw,
    }


def _sql_job(sql: dict[str, Any], **kw: Any) -> dict[str, Any]:
    from shared import jobset

    pin = kw.pop("bundleSha256", None)
    raw = {"name": kw.pop("name", "ddl"), "sql": sql, **kw}
    (job,) = jobset.normalize({"schemaVersion": 1, "phases": {"post": [raw]}})["post"]
    return {**job, "bundleSha256": pin}


def _prepare(job: dict[str, Any], phase: str = "post", **kw: Any) -> dict[str, Any]:
    from lambdas.prepare_job.handler import handler

    event = {"job": job, "phase": phase, "runId": "run-1", "dryRun": False, "ctx": _ctx(), "failure": None, **kw}
    return handler(event)["spec"]


def test_prepare_routes_sql_jobs_to_the_sql_jobs_lambda_with_the_project_role(env):
    job = _sql_job({"file": "ddl/010.sql", "vars": {"schema": "{{ ctx.taskKey }}", "n": 3}})
    spec = _prepare(job)
    assert spec["runtime"] == "lambda" and spec["functionArn"] == SQL_JOBS_ARN
    assert spec["jobRoleArn"] == ROLE
    # vars are rendered like with:, the SQL text is left to the runtime (SQL quoting)
    assert spec["sql"] == {"file": "ddl/010.sql", "vars": {"schema": "core", "n": 3}}
    stored = json.loads(
        env.get_object(Bucket=BUCKET, Key="runs/ledger/run-1/jobs/post/00-ddl/spec.json")["Body"].read()
    )
    assert stored["sql"] == spec["sql"]


def test_prepare_refuses_sql_jobs_without_the_sql_jobs_lambda(env, monkeypatch):
    from shared.errors import InvalidJobSet

    monkeypatch.setenv("SQL_JOBS_ARN", "")
    with pytest.raises(InvalidJobSet, match="sql_jobs_enabled"):
        _prepare(_sql_job({"inline": "SELECT 1"}))


def test_prepare_refuses_runtime_ssm_until_it_is_configured(env):
    from shared import jobset
    from shared.errors import InvalidJobSet

    (job,) = jobset.normalize(
        {"schemaVersion": 1, "phases": {"post": [{"name": "d", "runtime": "ssm", "command": ["x"]}]}}
    )["post"]
    with pytest.raises(InvalidJobSet, match="runtime ssm"):
        _prepare(job)


def test_prepare_refuses_a_reset_list_template_in_sql_vars_when_the_list_is_in_s3(env):
    from shared.errors import InvalidJobSet

    key = "runs/ledger/run-1/inspect/reset-tables.json"
    env.put_object(Bucket=BUCKET, Key=key, Body=json.dumps({"resetTables": LONG}))
    ctx = _ctx(resetTables=[], resetTablesCount=600, resetTablesUri=f"s3://{BUCKET}/{key}")
    with pytest.raises(InvalidJobSet, match="truncateScope"):
        _prepare(_sql_job({"inline": "SELECT 1", "vars": {"t": "{{ ctx.resetTables }}"}}), ctx=ctx, phase="reset")


# --- the sql_jobs handler --------------------------------------------------------------------


def _bundle(s3: Any, files: dict[str, bytes]) -> str:
    from shared import bundle

    hashes = {rel: bundle.digest(data) for rel, data in files.items()}
    for rel, data in files.items():
        s3.put_object(Bucket=BUCKET, Key=f"jobs/ledger/v1/{rel}", Body=data)
    s3.put_object(Bucket=BUCKET, Key="jobs/ledger/v1/bundle.sha256", Body=bundle.render(hashes).encode())
    return bundle.total(hashes)


def _run(spec: dict[str, Any]) -> dict[str, Any]:
    from lambdas.sql_jobs.handler import handler

    return handler({"spec": spec, "attempt": 1})


def _result_json(s3: Any, spec: dict[str, Any]) -> dict[str, Any]:
    return json.loads(s3.get_object(Bucket=BUCKET, Key=spec["resultKey"])["Body"].read())


DDL = (
    b"CREATE TABLE IF NOT EXISTS {{ ident:var.schema }}.t (id bigint);\n"
    b"COMMENT ON TABLE {{ ident:var.schema }}.t IS {{ run.runId }};\n"
)


def test_a_sql_file_job_runs_the_verified_bundle_file_and_writes_result_json(pg, env):
    total = _bundle(env, {"ddl/010.sql": DDL, "jobset.json": b"{}"})
    pg.answers = {"COMMENT ON": [{"applied": 1}]}
    spec = _prepare(_sql_job({"file": "ddl/010.sql", "vars": {"schema": "app"}}, bundleSha256=total))
    out = _run(spec)
    assert out["status"] == "ok", out
    (conn,) = pg.connections
    assert not conn.read_only and conn.closed and conn.kw["host"] == TARGET["host"]
    sql = pg.calls[0]["sql"]
    assert 'CREATE TABLE IF NOT EXISTS "app".t' in sql and "IS 'run-1'" in sql and "{{" not in sql
    assert out["summary"]["steps"] == ["ddl/010.sql"] and out["summary"]["result"] == {"applied": 1}
    assert _result_json(env, spec) == out
    # statement timeout from the job timeout, less the margin
    assert conn.kw["startup_params"]["statement_timeout"] == str((840 - 30) * 1000)


def test_a_tampered_bundle_file_never_reaches_the_database(pg, env):
    total = _bundle(env, {"ddl/010.sql": DDL})
    env.put_object(Bucket=BUCKET, Key="jobs/ledger/v1/ddl/010.sql", Body=b"DROP SCHEMA app CASCADE;")
    spec = _prepare(_sql_job({"file": "ddl/010.sql", "vars": {"schema": "app"}}, bundleSha256=total))
    out = _run(spec)
    assert out["status"] == "fail" and out["message"].startswith("JobSetTampered")
    assert pg.connections == []
    assert _result_json(env, spec)["status"] == "fail"


def test_a_dry_run_write_fails_with_read_only_sql_transaction(pg, env):
    total = _bundle(env, {"ddl/010.sql": DDL})
    spec = _prepare(_sql_job({"file": "ddl/010.sql", "vars": {"schema": "app"}}, bundleSha256=total), dryRun=True)
    out = _run(spec)
    assert out["status"] == "fail"
    assert (
        out["message"].startswith("ReadOnlySqlTransaction: cannot execute CREATE") and "ddl/010.sql" in out["message"]
    )
    assert pg.connections[0].read_only and pg.connections[0].closed


def test_a_dry_run_reads_and_reports_the_first_row(pg, env):
    pg.answers = {"count(*)": [{"missing_tables": 2}]}
    sql = "SELECT count(*) AS missing_tables FROM pg_tables WHERE schemaname = {{ var.s }}"
    spec = _prepare(_sql_job({"inline": sql, "connection": "source", "vars": {"s": "app"}}), dryRun=True)
    out = _run(spec)
    assert out["status"] == "ok" and out["summary"]["result"] == {"missing_tables": 2}
    assert out["summary"]["dryRun"] is True and pg.connections[0].kw["host"] == SOURCE["host"]
    assert "schemaname = 'app'" in pg.calls[0]["sql"]


def test_a_dry_run_refuses_sql_that_turns_read_only_off(pg, env):
    for text in ("SET default_transaction_read_only = off; DELETE FROM t", "BEGIN READ WRITE; DELETE FROM t"):
        out = _run(_prepare(_sql_job({"inline": text}), dryRun=True))
        assert out["status"] == "fail" and "read-only" in out["message"]
    assert pg.connections == []


def test_truncate_scope_reads_the_full_list_and_truncates_in_one_statement(pg, env):
    key = "runs/ledger/run-1/inspect/reset-tables.json"
    env.put_object(Bucket=BUCKET, Key=key, Body=json.dumps({"resetTables": LONG}))
    ctx = _ctx(resetTables=[], resetTablesCount=600, resetTablesUri=f"s3://{BUCKET}/{key}")
    spec = _prepare(_sql_job({"truncateScope": True, "cascade": True}, name="truncate"), phase="reset", ctx=ctx)
    assert "app.t0599" not in json.dumps(spec)  # the payload stays small
    out = _run(spec)
    assert out["status"] == "ok" and out["summary"]["tables"] == 600, out
    check, call = pg.calls  # which tables exist, then one TRUNCATE
    assert "to_regclass" in check["sql"] and len(check["params"]["names"]) == 600
    assert call["sql"].startswith('TRUNCATE TABLE "app"."t0000", ') and call["sql"].endswith('"app"."t0599" CASCADE')
    assert call["sql"].count('"app"."t') == 600


def test_truncate_scope_in_a_dry_run_only_counts(pg, env):
    spec = _prepare(_sql_job({"truncateScope": True}, name="truncate"), phase="reset", dryRun=True)
    out = _run(spec)
    assert out["status"] == "ok" and out["summary"] == {"connection": "target", "dryRun": True, "wouldTruncate": 2}
    assert pg.calls == [] and pg.connections[0].read_only  # the connection is still checked


def test_truncate_scope_with_nothing_to_reset_opens_no_session(pg, env):
    spec = _prepare(_sql_job({"truncateScope": True}, name="truncate"), phase="reset", ctx=_ctx(resetTables=[]))
    out = _run(spec)
    assert out["status"] == "ok" and out["summary"]["tables"] == 0 and pg.connections == []


def test_truncate_scope_refuses_a_list_that_does_not_match_its_count_or_names(pg, env):
    spec = _prepare(_sql_job({"truncateScope": True}, name="truncate"), phase="reset", ctx=_ctx(resetTablesCount=3))
    assert "the decision says 3" in _run(spec)["message"]
    spec = _prepare(_sql_job({"truncateScope": True}, name="truncate"), phase="reset", ctx=_ctx(resetTables=["a;b"]))
    assert "unsafe" in _run(spec)["message"]
    assert pg.calls == []


def test_truncate_scope_skips_tables_missing_on_the_target(pg, env):
    # Tables dropped after an interrupted run stay in the DMS statistics the reset list comes
    # from; TRUNCATE of a missing table would fail the whole reset (live run, s4big).
    pg.answers = {"to_regclass": [{"missing": '"app"."b"'}]}
    ctx = _ctx(resetTables=["app.a", "app.b", "app.c"])
    spec = _prepare(_sql_job({"truncateScope": True}, name="truncate"), phase="reset", ctx=ctx)
    out = _run(spec)
    assert out["status"] == "ok", out
    assert out["summary"]["tables"] == 2 and out["summary"]["skippedMissing"] == 1
    assert "app.b" in out["message"] and "missing on the target" in out["message"]
    check, trunc = pg.calls
    assert check["params"]["names"] == ['"app"."a"', '"app"."b"', '"app"."c"']
    assert trunc["sql"] == 'TRUNCATE TABLE "app"."a", "app"."c"'


def test_truncate_scope_with_every_table_missing_truncates_nothing(pg, env):
    pg.answers = {"to_regclass": [{"missing": '"app"."a"'}, {"missing": '"app"."b"'}]}
    spec = _prepare(_sql_job({"truncateScope": True}, name="truncate"), phase="reset")
    out = _run(spec)
    assert out["status"] == "ok" and out["summary"] == {
        "connection": "target",
        "dryRun": False,
        "tables": 0,
        "skippedMissing": 2,
    }
    assert len(pg.calls) == 1  # the check only, no TRUNCATE


def test_single_transaction_wraps_every_file_and_rolls_back_on_error(pg, env):
    total = _bundle(env, {"a.sql": b"CREATE TABLE a();", "b.sql": b"CREATE TABLE b();"})
    spec = _prepare(_sql_job({"file": ["a.sql", "b.sql"], "singleTransaction": True}, bundleSha256=total))
    assert _run(spec)["status"] == "ok"
    assert [c["sql"] for c in pg.calls] == ["BEGIN", "CREATE TABLE a();", "CREATE TABLE b();", "COMMIT"]
    pg.calls.clear()
    pg.errors = {"TABLE b": {"C": "42P07", "M": 'relation "b" already exists'}}
    out = _run(spec)
    assert out["status"] == "fail" and 'relation "b" already exists (b.sql)' in out["message"]
    assert [c["sql"] for c in pg.calls] == ["BEGIN", "CREATE TABLE a();", "CREATE TABLE b();", "ROLLBACK"]


def test_without_single_transaction_each_file_is_its_own_message(pg, env):
    total = _bundle(env, {"a.sql": b"CREATE TABLE a();", "b.sql": b"CREATE TABLE b();"})
    spec = _prepare(_sql_job({"file": ["a.sql", "b.sql"]}, bundleSha256=total))
    assert _run(spec)["status"] == "ok"
    assert [c["sql"] for c in pg.calls] == ["CREATE TABLE a();", "CREATE TABLE b();"]


def test_template_values_are_quoted_never_spliced(pg, env):
    text = "SELECT {{ var.v }}, {{ var.n }}, {{ var.b }}, {{ var.none }}, {{ var.l }}, {{ ident:var.t }}"
    spec = _prepare(
        _sql_job(
            {"inline": text, "vars": {"v": "x'; DROP TABLE t; --", "n": 2, "b": True, "l": ["a", "b"], "t": "app.t"}}
        )
    )
    assert _run(spec)["status"] == "ok"
    assert pg.calls[0]["sql"] == "SELECT 'x''; DROP TABLE t; --', 2, TRUE, NULL, 'a', 'b', \"app\".\"t\""
    bad = _prepare(_sql_job({"inline": "SELECT {{ ident:var.t }}", "vars": {"t": "x; DROP"}}), runId="run-2")
    assert "unsafe identifier" in _run(bad)["message"]


def test_the_spec_in_s3_must_be_the_job_that_was_invoked(pg, env):
    spec = _prepare(_sql_job({"inline": "SELECT 1"}))
    out = _run({**spec, "name": "other"})
    assert out["status"] == "fail" and "spec.json" in out["message"] and pg.connections == []


def test_a_summary_row_is_json_safe_and_small(pg, env):
    import datetime
    import decimal

    row = {"d": decimal.Decimal("1.5"), "t": datetime.date(2020, 1, 2), "s": "x" * 1000}
    row |= {f"c{i}": i for i in range(80)}
    pg.answers = {"SELECT": [row]}
    out = _run(_prepare(_sql_job({"inline": "SELECT 1"})))
    result = out["summary"]["result"]
    assert result["d"] == "1.5" and result["t"] == "2020-01-02" and len(result["s"]) == 256
    assert len(result) == 50
    json.dumps(out)


def test_unknown_connection_and_an_unreadable_spec_are_job_failures(pg, env):
    out = _run(_prepare(_sql_job({"inline": "SELECT 1", "connection": "nope"})))
    assert out["status"] == "fail" and "not registered" in out["message"]
    gone = _prepare(_sql_job({"inline": "SELECT 1"}), runId="run-3")
    env.delete_object(Bucket=BUCKET, Key=gone["specS3Uri"].removeprefix(f"s3://{BUCKET}/"))
    assert _run(gone)["status"] == "fail"


# --- sandbox project Lambda: truncate reads the full reset list (above the payload cap) -------


@pytest.fixture
def sandbox_sql(env, monkeypatch):
    import importlib
    import sys

    from shared import sqlclient

    monkeypatch.syspath_prepend(str(sqlclient.VENDOR_DIR))
    monkeypatch.syspath_prepend(str(ROOT / "examples" / "sandbox" / "lambda"))
    sys.modules.pop("sandbox_sql", None)
    mod = importlib.import_module("sandbox_sql")
    sent: list[str] = []

    class Conn:
        def run(self, sql: str) -> None:
            sent.append(sql)

        def close(self) -> None:
            pass

    monkeypatch.setattr(mod, "connect", lambda _which: Conn())
    monkeypatch.setattr(mod, "s3", env)
    mod.sent = sent
    return mod


def test_sandbox_truncate_reads_the_full_list_from_the_spec_above_the_cap(sandbox_sql, env):
    key = "runs/ledger/run-1/inspect/reset-tables.json"
    env.put_object(Bucket=BUCKET, Key=key, Body=json.dumps({"resetTables": LONG}))
    ctx = _ctx(resetTables=[], resetTablesCount=600, resetTablesUri=f"s3://{BUCKET}/{key}")
    spec = _prepare(_sql_job({"truncateScope": True}, name="truncate"), phase="reset", ctx=ctx)
    spec = {**spec, "with": {"action": "truncate"}}
    assert spec["ctx"]["resetTables"] == []  # the payload holds the count only
    out = sandbox_sql.handler({"spec": spec, "attempt": 1})
    assert out["status"] == "ok" and out["summary"]["tables"] == 600, out
    (sql,) = sandbox_sql.sent
    assert sql.count('"app"."t') == 600


def test_sandbox_truncate_refuses_a_list_that_does_not_match_its_count(sandbox_sql, env):
    spec = _prepare(_sql_job({"truncateScope": True}, name="truncate"), phase="reset", ctx=_ctx(resetTablesCount=5))
    out = sandbox_sql.handler({"spec": {**spec, "with": {"action": "truncate"}}, "attempt": 1})
    assert out["status"] == "fail" and "5" in out["message"] and sandbox_sql.sent == []

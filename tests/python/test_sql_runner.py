"""sql-runner image code: orch_job.py (entrypoint) and orch_sql.py (psql-run)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import orch_job
import orch_sql
import pytest

BUCKET = "dms-orch-artifacts-test"
BASE = "runs/run-1/jobs/post/00-indexes"


# --- orch_sql (psql-run) -------------------------------------------------------------


@pytest.mark.parametrize(
    "tables,engine,cascade,expected",
    [
        (["public.a", "b"], "postgresql", False, 'TRUNCATE TABLE "public"."a", "b";\n'),
        (["public.a"], "postgresql", True, 'TRUNCATE TABLE "public"."a" CASCADE;\n'),
        ([], "postgresql", False, ""),
    ],
)
def test_truncate_sql(tables, engine, cascade, expected):
    assert orch_sql.truncate_sql(tables, engine, cascade) == expected


def test_truncate_sql_mysql_disables_fk_checks():
    sql = orch_sql.truncate_sql(["shop.orders"], "mysql")
    assert "TRUNCATE TABLE `shop`.`orders`;" in sql and sql.startswith("SET FOREIGN_KEY_CHECKS = 0;")


@pytest.mark.parametrize("bad", ['public.a"; DROP TABLE x; --', "a.b.c", "1table", "public.", "x y"])
def test_truncate_rejects_unsafe_names(bad):
    with pytest.raises(orch_sql.SqlRunError):
        orch_sql.truncate_sql([bad], "postgresql")


@pytest.fixture
def sql_env(tmp_path, monkeypatch):
    conns = {
        "target": {
            "engine": "postgresql",
            "host": "tgt.local",
            "port": 5432,
            "database": "ledger",
            "username": "runner",
            "password": "s3cr3t",
            "sslMode": "verify-full",
        },
        "shop": {
            "engine": "aurora-mysql",
            "host": "shop.local",
            "port": 3306,
            "database": "shop",
            "username": "runner",
            "password": "pw",
        },
    }
    spec = {
        "runId": "run-1",
        "phase": "reset",
        "ctx": {"resetTables": ["public.a"], "schema": "public"},
        "previous": {"pre": {"diff": {"summary": {"missing": 2}}}},
    }
    (tmp_path / "conns.json").write_text(json.dumps(conns))
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("ORCH_CONNECTIONS_FILE", str(tmp_path / "conns.json"))
    monkeypatch.setenv("ORCH_JOB_SPEC_FILE", str(tmp_path / "spec.json"))
    monkeypatch.setenv("ORCH_TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("ORCH_OUT_DIR", str(out))
    monkeypatch.delenv("ORCH_DRY_RUN", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class Runner:
    def __init__(self, code: int = 0):
        self.code = code
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: list[str], env: dict[str, str], check: bool) -> Any:
        self.calls.append((argv, env))
        return SimpleNamespace(returncode=self.code)


def test_psql_run_files_and_truncate(sql_env):
    (sql_env / "ddl.sql").write_text("CREATE TABLE x();")
    runner = Runner()
    code = orch_sql.main(
        ["--connection", "target", "--file", "ddl.sql", "--truncate", "public.a,public.b", "--single-transaction"],
        runner=runner,
    )
    assert code == 0
    argv, env = runner.calls[0]
    assert argv[:4] == ["psql", "-X", "-v", "ON_ERROR_STOP=1"] and "--single-transaction" in argv
    assert argv.count("-f") == 2
    assert env["PGHOST"] == "tgt.local" and env["PGPASSWORD"] == "s3cr3t" and env["PGSSLMODE"] == "verify-full"
    assert "s3cr3t" not in " ".join(argv)
    truncate = Path(argv[argv.index("-f", argv.index("-f") + 1) + 1]).read_text()
    assert truncate == 'TRUNCATE TABLE "public"."a", "public"."b";\n'
    result = json.loads((sql_env / "out" / "result.json").read_text())
    assert result["status"] == "ok" and len(result["summary"]["steps"]) == 2


def test_psql_run_template_uses_spec_and_vars(sql_env):
    (sql_env / "t.sql").write_text(
        "SELECT {{ var.label }}, {{ pre.diff.summary.missing }} FROM {{ ident:ctx.schema }}.t;"
    )
    runner = Runner()
    assert orch_sql.main(["--connection", "target", "--template", "t.sql", "--var", "label=x"], runner=runner) == 0
    rendered = Path(runner.calls[0][0][-1]).read_text()
    assert rendered == """SELECT 'x', 2 FROM "public".t;"""


# --- review finding 6: template values are SQL literals or checked identifiers -------


@pytest.mark.parametrize(
    "value,engine,expected",
    [
        ("it's", "postgresql", "'it''s'"),
        ("a\\'; DROP TABLE x; --", "mysql", "'a\\\\''; DROP TABLE x; --'"),
        (None, "postgresql", "NULL"),
        (True, "postgresql", "TRUE"),
        (7, "postgresql", "7"),
        (["public.a", "o'b"], "postgresql", "'public.a', 'o''b'"),
    ],
)
def test_sql_literal(value, engine, expected):
    assert orch_sql.sql_literal(value, engine) == expected


def test_template_quotes_a_hostile_value_instead_of_running_it(sql_env):
    spec = json.loads((sql_env / "spec.json").read_text())
    spec["failure"] = {"reason": "x'); DROP TABLE accounts; --"}
    (sql_env / "spec.json").write_text(json.dumps(spec))
    (sql_env / "t.sql").write_text("INSERT INTO audit(reason) VALUES ({{ failure.reason }});")
    runner = Runner()
    assert orch_sql.main(["--connection", "target", "--template", "t.sql"], runner=runner) == 0
    rendered = Path(runner.calls[0][0][-1]).read_text()
    assert rendered == "INSERT INTO audit(reason) VALUES ('x''); DROP TABLE accounts; --');"


def test_template_identifiers_are_validated_and_quoted(sql_env):
    (sql_env / "t.sql").write_text("TRUNCATE {{ ident:ctx.resetTables }};")
    runner = Runner()
    assert orch_sql.main(["--connection", "target", "--template", "t.sql"], runner=runner) == 0
    assert Path(runner.calls[0][0][-1]).read_text() == 'TRUNCATE "public"."a";'

    spec = json.loads((sql_env / "spec.json").read_text())
    spec["ctx"]["resetTables"] = ['public.a"; DROP TABLE x; --']
    (sql_env / "spec.json").write_text(json.dumps(spec))
    (sql_env / "out" / "result.json").unlink()
    assert orch_sql.main(["--connection", "target", "--template", "t.sql"], runner=Runner()) == 2
    result = json.loads((sql_env / "out" / "result.json").read_text())
    assert result["status"] == "fail" and "unsafe" in result["message"]


def test_psql_run_dry_run_executes_nothing(sql_env, monkeypatch, capsys):
    monkeypatch.setenv("ORCH_DRY_RUN", "1")
    runner = Runner()
    assert orch_sql.main(["--connection", "target", "--truncate", "public.a"], runner=runner) == 0
    assert runner.calls == []
    assert 'TRUNCATE TABLE "public"."a";' in capsys.readouterr().out
    assert json.loads((sql_env / "out" / "result.json").read_text())["summary"]["dryRun"] is True


def test_psql_run_empty_truncate_is_a_no_op(sql_env):
    runner = Runner()
    assert orch_sql.main(["--connection", "target", "--truncate", ""], runner=runner) == 0
    assert runner.calls == []


def test_psql_run_mysql_client(sql_env):
    (sql_env / "a.sql").write_text("SELECT 1;")
    runner = Runner()
    assert orch_sql.main(["--connection", "shop", "--file", "a.sql", "--on-error", "continue"], runner=runner) == 0
    argv, env = runner.calls[0]
    assert argv[0] == "mysql" and "--force" in argv and "--ssl-mode=VERIFY_IDENTITY" in argv
    assert env["MYSQL_PWD"] == "pw" and "pw" not in " ".join(argv[:-2])


def test_psql_run_failures(sql_env):
    assert orch_sql.main(["--connection", "nope", "--sql", "SELECT 1"], runner=Runner()) == 2
    (sql_env / "out" / "result.json").unlink()
    assert orch_sql.main(["--connection", "target", "--file", "missing.sql"], runner=Runner()) == 2
    (sql_env / "out" / "result.json").unlink()
    assert orch_sql.main(["--connection", "target", "--sql", "SELECT 1"], runner=Runner(code=3)) == 3
    assert json.loads((sql_env / "out" / "result.json").read_text())["status"] == "fail"


# --- orch_job (entrypoint) -----------------------------------------------------------


@pytest.fixture
def job_env(aws, tmp_path, monkeypatch):
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    sm = boto3.client("secretsmanager", region_name="eu-west-2")
    arn = sm.create_secret(
        Name="dms/ledger/target/runner", SecretString=json.dumps({"username": "runner", "password": "pw", "host": "h"})
    )["ARN"]
    s3.put_object(Bucket=BUCKET, Key="jobs/ledger/v1/ddl/020_indexes.sql", Body=b"CREATE INDEX i ON t(x);")
    s3.put_object(Bucket=BUCKET, Key="jobs/ledger/v1/jobset.json", Body=b"{}")
    for name in ("out", "work", "tmp"):
        monkeypatch.setenv(f"ORCH_{name.upper()}_DIR", str(tmp_path / name))
    monkeypatch.setenv("ORCH_JOB_SPEC_S3", f"s3://{BUCKET}/{BASE}/spec.json")
    monkeypatch.setenv("ORCH_ATTEMPT", "1")
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
    monkeypatch.delenv("ORCH_LOG_STREAM", raising=False)

    def put_spec(**kw: Any) -> dict[str, Any]:
        spec = {
            "name": "indexes",
            "phase": "post",
            "runId": "run-1",
            "dryRun": False,
            "timeoutSeconds": 120,
            "jobsS3Prefix": f"s3://{BUCKET}/jobs/ledger/v1/",
            "resultKey": f"{BASE}/result.json",
            "artifactsS3Prefix": f"s3://{BUCKET}/{BASE}/",
            "connections": {"target": {"engine": "postgresql", "secretArn": arn, "database": "ledger"}},
            **kw,
        }
        s3.put_object(Bucket=BUCKET, Key=f"{BASE}/spec.json", Body=json.dumps(spec).encode())
        return spec

    return SimpleNamespace(s3=s3, sm=sm, put_spec=put_spec, tmp=tmp_path)


def result_of(env: SimpleNamespace) -> dict[str, Any]:
    return json.loads(env.s3.get_object(Bucket=BUCKET, Key=f"{BASE}/result.json")["Body"].read())


SCRIPT = """
import json, os, pathlib
conns = json.load(open(os.environ['ORCH_CONNECTIONS_FILE']))
assert conns['target']['password'] == 'pw' and conns['target']['host'] == 'h'
assert pathlib.Path('ddl/020_indexes.sql').exists()
out = pathlib.Path(os.environ['ORCH_OUT_DIR'])
(out / 'diff.json').write_text('{"tables": 3}')
(out / 'result.json').write_text(json.dumps({'status': 'warn', 'message': 'two indexes exist', 'summary': {'n': 2}}))
print('job done')
"""


def test_orch_job_runs_command_with_bundle_and_connections(job_env):
    job_env.put_spec(artifacts={"upload": ["/out/diff.json", "/out/missing.json"]})
    code = orch_job.main([sys.executable, "-c", SCRIPT], s3=job_env.s3, secrets=job_env.sm)
    assert code == 0
    result = result_of(job_env)
    assert result["status"] == "warn" and result["summary"] == {"n": 2}
    assert result["artifacts"] == [f"s3://{BUCKET}/{BASE}/artifacts/diff.json"]
    assert not (job_env.tmp / "tmp" / "connections.json").exists()  # credentials file removed


def test_orch_job_non_zero_exit_is_failure(job_env):
    job_env.put_spec()
    code = orch_job.main(
        [sys.executable, "-c", "print('boom'); raise SystemExit(3)"], s3=job_env.s3, secrets=job_env.sm
    )
    assert code == 3
    result = result_of(job_env)
    assert result["status"] == "fail"
    # No output lines in the result: it reaches the execution Cause, readable by the operator.
    assert result["message"] == "exit code 3 (see CloudWatch log stream unavailable)"
    assert "boom" not in json.dumps(result)


# --- review finding 2: no job output in results; the log stream instead -------------------


def test_failed_job_points_at_the_log_stream_from_the_container_metadata(job_env, monkeypatch):
    job_env.put_spec()
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
    seen: list[str] = []

    def metadata(uri: str) -> dict[str, Any]:
        seen.append(uri)
        return {"LogDriver": "awslogs", "LogOptions": {"awslogs-stream": "core/job/0123abcd"}}

    monkeypatch.setattr(orch_job, "_container_metadata", metadata)
    script = (
        "import json, os, pathlib; print('password=pw');"
        "pathlib.Path(os.environ['ORCH_OUT_DIR'], 'result.json')"
        ".write_text(json.dumps({'status': 'fail', 'message': 'counts differ'})); raise SystemExit(4)"
    )
    assert orch_job.main([sys.executable, "-c", script], s3=job_env.s3, secrets=job_env.sm) == 4
    result = result_of(job_env)
    assert seen == ["http://169.254.170.2/v4/abc"]
    assert result["message"] == "exit code 4 (see CloudWatch log stream core/job/0123abcd): counts differ"
    assert "password" not in json.dumps(result)


def test_log_stream_name_falls_back_when_the_metadata_is_unreadable(monkeypatch):
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")

    def broken(uri: str) -> dict[str, Any]:
        raise OSError("no route")

    monkeypatch.setattr(orch_job, "_container_metadata", broken)
    assert orch_job.log_stream_name() == "unavailable"
    monkeypatch.setenv("ORCH_LOG_STREAM", "runner/job-1")
    assert orch_job.log_stream_name() == "runner/job-1"


def test_timed_out_job_result_has_no_output_lines(tmp_path):
    result = orch_job.build_result(124, tmp_path, "core/job/1")
    assert result == {
        "status": "fail",
        "message": "exit code 124 (see CloudWatch log stream core/job/1)",
        "summary": {},
        "artifacts": [],
    }


# --- review finding 11: a dry run opens read-only sessions for libpq clients ---------------

ENV_SCRIPT = (
    "import json, os, pathlib; pathlib.Path(os.environ['ORCH_OUT_DIR'], 'result.json')"
    ".write_text(json.dumps({'status': 'ok', 'summary': {'pgoptions': os.environ.get('PGOPTIONS')}}))"
)


def test_dry_run_job_gets_read_only_pgoptions(job_env, monkeypatch):
    monkeypatch.delenv("PGOPTIONS", raising=False)
    job_env.put_spec(dryRun=True)
    assert orch_job.main([sys.executable, "-c", ENV_SCRIPT], s3=job_env.s3, secrets=job_env.sm) == 0
    assert result_of(job_env)["summary"]["pgoptions"] == "-c default_transaction_read_only=on"


def test_dry_run_keeps_existing_pgoptions(job_env, monkeypatch):
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=60000")
    monkeypatch.setenv("ORCH_DRY_RUN", "1")
    job_env.put_spec()
    assert orch_job.main([sys.executable, "-c", ENV_SCRIPT], s3=job_env.s3, secrets=job_env.sm) == 0
    assert (
        result_of(job_env)["summary"]["pgoptions"] == "-c statement_timeout=60000 -c default_transaction_read_only=on"
    )


def test_real_run_does_not_set_read_only_pgoptions(job_env, monkeypatch):
    monkeypatch.delenv("PGOPTIONS", raising=False)
    monkeypatch.delenv("ORCH_DRY_RUN", raising=False)
    job_env.put_spec()
    assert orch_job.main([sys.executable, "-c", ENV_SCRIPT], s3=job_env.s3, secrets=job_env.sm) == 0
    assert result_of(job_env)["summary"]["pgoptions"] is None


def test_orch_job_result_fail_with_zero_exit_returns_one(job_env):
    job_env.put_spec()
    script = (
        "import json, os, pathlib; pathlib.Path(os.environ['ORCH_OUT_DIR'], 'result.json')"
        ".write_text(json.dumps({'status': 'fail', 'message': 'counts differ'}))"
    )
    assert orch_job.main([sys.executable, "-c", script], s3=job_env.s3, secrets=job_env.sm) == 1
    assert result_of(job_env)["message"] == "counts differ"


def test_orch_job_missing_command_binary(job_env):
    job_env.put_spec()
    assert orch_job.main(["definitely-not-a-binary-xyz"], s3=job_env.s3, secrets=job_env.sm) == 127
    assert result_of(job_env)["status"] == "fail"


def test_orch_job_refuses_bundle_path_traversal(job_env):
    job_env.s3.put_object(Bucket=BUCKET, Key="jobs/ledger/v1/../../evil.sh", Body=b"x")
    dest = job_env.tmp / "dest"
    dest.mkdir()
    # S3 keys are opaque strings; a key that resolves outside the work dir must be refused.
    with pytest.raises(ValueError):
        orch_job.download_bundle(job_env.s3, f"s3://{BUCKET}/jobs/ledger/v1/", dest)


# --- review finding 7: the job timeout also stops a command that prints nothing -----


def test_run_command_kills_a_silent_command_at_the_timeout(tmp_path):
    import time

    started = time.monotonic()
    code, tail = orch_job.run_command(
        [sys.executable, "-c", "print('working', flush=True); import time; time.sleep(60)"],
        env=dict(__import__("os").environ),
        cwd=tmp_path,
        timeout=2,
    )
    assert time.monotonic() - started < 20
    assert code == 124 and tail[0] == "working" and tail[-1] == "orch: killed after 2s"


def test_run_command_returns_the_exit_code_of_a_quick_command(tmp_path):
    code, tail = orch_job.run_command(
        [sys.executable, "-c", "print('a'); print('b'); raise SystemExit(3)"],
        env=dict(__import__("os").environ),
        cwd=tmp_path,
        timeout=30,
    )
    assert (code, tail) == (3, ["a", "b"])


# --- Dockerfile (trivy image findings; CI builds and scans the image) -----------------

DOCKERFILE = Path(__file__).resolve().parents[2] / "src" / "jobs" / "sql-runner" / "Dockerfile"


def test_dockerfile_upgrades_openssl_from_security_repo() -> None:
    # libssl3/openssl in the base image lag bookworm-security (CVE-2026-84782).
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "libssl3 openssl" in text


def test_dockerfile_removes_pip_from_final_image() -> None:
    # pip's vendored certifi/msgpack/setuptools(pkg_resources)/urllib3 are listed in its
    # SBOM and scanned by trivy; nothing in the image installs packages at runtime.
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "pip uninstall -y pip" in text


# --- the sandbox CodeBuild context (examples/sandbox/sql_runner.tf) -------------------


def test_sandbox_build_context_holds_every_file_the_dockerfile_copies() -> None:
    """Seen live: orch_dump.py was copied by the Dockerfile but missing from the zip CodeBuild
    builds from, so the image build failed. Every COPY source must be in sql_runner_files."""
    repo = Path(__file__).resolve().parents[2]
    dockerfile = (repo / "src/jobs/sql-runner/Dockerfile").read_text(encoding="utf-8")
    tf = (repo / "examples/sandbox/sql_runner.tf").read_text(encoding="utf-8")
    listed = set(re.findall(r'"(src/[^"]+)"', tf))
    copied = [
        src
        for line in dockerfile.splitlines()
        if line.startswith("COPY ")
        for src in line.split()[1:-1]
        if not src.startswith("--")
    ]
    assert copied
    missing = []
    for src in copied:
        if src.endswith("/"):
            # a directory: sql_runner.tf adds it with fileset()
            if f'fileset("${{local.repo_root}}/{src.rstrip("/")}"' not in tf:
                missing.append(src)
        elif src not in listed:
            missing.append(src)
    assert missing == []


# --- IAM database authentication for Fargate jobs --------------------------

IAM_CONN = {
    "engine": "postgresql",
    "auth": "iam",
    "iamUser": "orch_iam",
    "host": "tgt.cluster-abc.eu-west-2.rds.amazonaws.com",
    "port": 5432,
    "database": "ledger",
    "sslMode": "verify-full",
}


class FakeRdsToken:
    """generate_db_auth_token is a local signature; every call gives a new token."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate_db_auth_token(self, **kw: Any) -> str:
        self.calls.append(kw)
        return f"iam-token-{len(self.calls)}-SIGNATURE"


class NoSecrets:
    def get_secret_value(self, **kw: Any) -> Any:
        raise AssertionError(f"an IAM connection must not read a secret: {kw}")


def test_iam_connection_file_holds_no_password_and_reads_no_secret(tmp_path):
    path = tmp_path / "conns.json"
    names = orch_job.resolve_connections(NoSecrets(), {"target_iam": IAM_CONN}, path)
    assert names == ["target_iam"]
    conn = json.loads(path.read_text())["target_iam"]
    assert conn["auth"] == "iam" and conn["username"] == "orch_iam" and conn["password"] is None
    assert "token" not in path.read_text().lower()


def test_iam_token_reaches_psql_only_through_its_environment(sql_env, monkeypatch, capsys):
    conns = json.loads((sql_env / "conns.json").read_text())
    conns["target_iam"] = {**IAM_CONN, "username": "orch_iam", "password": None}
    (sql_env / "conns.json").write_text(json.dumps(conns))
    rds = FakeRdsToken()
    monkeypatch.setattr(orch_job, "_rds_client", lambda: rds)
    monkeypatch.setenv("AWS_REGION", "eu-west-2")
    runner = Runner()
    assert orch_sql.main(["--connection", "target_iam", "--sql", "SELECT 1"], runner=runner) == 0
    assert orch_sql.main(["--connection", "target_iam", "--sql", "SELECT 2"], runner=runner) == 0
    (argv1, env1), (_argv2, env2) = runner.calls
    assert env1["PGPASSWORD"] == "iam-token-1-SIGNATURE" and env1["PGUSER"] == "orch_iam"
    assert env2["PGPASSWORD"] == "iam-token-2-SIGNATURE"  # a fresh token per client: tokens live 15 min
    assert env1["PGSSLMODE"] == "verify-full"
    assert rds.calls[0] == {
        "DBHostname": IAM_CONN["host"],
        "Port": 5432,
        "DBUsername": "orch_iam",
        "Region": "eu-west-2",
    }
    out = capsys.readouterr()
    assert "SIGNATURE" not in out.out + out.err and "SIGNATURE" not in " ".join(argv1)
    assert "SIGNATURE" not in (sql_env / "conns.json").read_text()
    assert "SIGNATURE" not in (sql_env / "out" / "result.json").read_text()


def test_iam_connection_needs_tls(sql_env, monkeypatch):
    conns = json.loads((sql_env / "conns.json").read_text())
    conns["target_iam"] = {**IAM_CONN, "username": "orch_iam", "password": None, "sslMode": "disable"}
    (sql_env / "conns.json").write_text(json.dumps(conns))
    monkeypatch.setattr(orch_job, "_rds_client", FakeRdsToken)
    runner = Runner()
    assert orch_sql.main(["--connection", "target_iam", "--sql", "SELECT 1"], runner=runner) != 0
    assert runner.calls == []


def test_iam_connection_is_postgresql_only(sql_env, monkeypatch):
    conns = json.loads((sql_env / "conns.json").read_text())
    conns["shop_iam"] = {**IAM_CONN, "engine": "aurora-mysql", "username": "orch_iam", "password": None}
    (sql_env / "conns.json").write_text(json.dumps(conns))
    monkeypatch.setattr(orch_job, "_rds_client", FakeRdsToken)
    runner = Runner()
    assert orch_sql.main(["--connection", "shop_iam", "--sql", "SELECT 1"], runner=runner) != 0
    assert runner.calls == []

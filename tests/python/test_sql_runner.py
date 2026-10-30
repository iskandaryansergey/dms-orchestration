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


def _scope_spec(sql_env, tables: list[str], count: int | None) -> None:
    spec = json.loads((sql_env / "spec.json").read_text())
    spec["ctx"] = {"resetTables": tables, **({} if count is None else {"resetTablesCount": count})}
    (sql_env / "spec.json").write_text(json.dumps(spec))


def test_psql_run_truncate_scope_reads_the_full_list_from_the_spec(sql_env):
    """Above the payload cap only spec.json has the list; the step label carries the full count."""
    tables = [f"app.t{i:04d}" for i in range(600)]
    _scope_spec(sql_env, tables, 600)
    runner = Runner()
    assert orch_sql.main(["--connection", "target", "--truncate-scope", "--cascade"], runner=runner) == 0
    argv, _ = runner.calls[0]
    sql = Path(argv[-1]).read_text()
    assert sql.startswith('TRUNCATE TABLE "app"."t0000", ') and sql.endswith('"app"."t0599" CASCADE;\n')
    result = json.loads((sql_env / "out" / "result.json").read_text())
    assert result["summary"]["steps"] == ["truncate 600 table(s)"]


def test_psql_run_truncate_scope_small_list_and_empty_scope(sql_env):
    _scope_spec(sql_env, ["public.a"], None)
    runner = Runner()
    assert orch_sql.main(["--connection", "target", "--truncate-scope"], runner=runner) == 0
    assert Path(runner.calls[0][0][-1]).read_text() == 'TRUNCATE TABLE "public"."a";\n'
    _scope_spec(sql_env, [], 0)
    runner = Runner()
    assert orch_sql.main(["--connection", "target", "--truncate-scope"], runner=runner) == 0
    assert runner.calls == []


@pytest.mark.parametrize(
    "tables,count,message",
    [
        (["public.a"], 600, "1 table(s) in the spec, the decision says 600"),
        (['public.a"; DROP TABLE x; --'], 1, "unsafe table name"),
    ],
)
def test_psql_run_truncate_scope_refuses_a_short_or_unsafe_list(sql_env, tables, count, message):
    _scope_spec(sql_env, tables, count)
    runner = Runner()
    assert orch_sql.main(["--connection", "target", "--truncate-scope"], runner=runner) == 2
    assert runner.calls == []
    assert message in json.loads((sql_env / "out" / "result.json").read_text())["message"]


def test_psql_run_truncate_scope_and_truncate_are_exclusive(sql_env):
    with pytest.raises(SystemExit):
        orch_sql.main(["--connection", "target", "--truncate-scope", "--truncate", "public.a"], runner=Runner())


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

DOCKERFILE = Path(__file__).resolve().parents[2] / "modules/dms-orchestration/src/jobs/sql-runner/Dockerfile"


def test_dockerfile_upgrades_openssl_from_security_repo() -> None:
    # libssl3/openssl in the base image lag bookworm-security (CVE-2026-84782).
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "libssl3 openssl" in text


def test_dockerfile_removes_pip_from_final_image() -> None:
    # pip's vendored certifi/msgpack/setuptools(pkg_resources)/urllib3 are listed in its
    # SBOM and scanned by trivy; nothing in the image installs packages at runtime.
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "pip uninstall -y pip" in text


# --- Dockerfile supply chain (review finding 12): whoever builds the optional image ------

REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_args(text: str) -> dict[str, str]:
    return dict(re.findall(r"^ARG ([A-Z0-9_]+)=(\S*)$", text, re.M))


def test_dockerfile_base_image_is_a_build_arg_pinned_by_digest() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    base = _build_args(text).get("BASE_IMAGE", "")
    assert re.search(r"@sha256:[0-9a-f]{64}$", base), base
    froms = [line for line in text.splitlines() if line.startswith("FROM ")]
    assert froms == ["FROM ${BASE_IMAGE}"]
    assert text.index("ARG BASE_IMAGE=") < text.index("FROM ")


def test_dockerfile_takes_every_source_as_a_build_arg_for_internal_mirrors() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    args = _build_args(text)
    for arg in ("PGDG_APT_URL", "PGDG_KEY_URL", "PIP_INDEX_URL", "RDS_CA_URL", "POWERSHELL_DEB_URL"):
        assert args.get(arg, "").startswith("https://"), arg
        assert "${" + arg + "}" in text, f"{arg} is declared but not used"
    # No source is hard-coded outside its build arg.
    code = [line for line in text.splitlines() if not line.startswith(("#", "ARG "))]
    assert [line for line in code if "https://" in line or "http://" in line] == []


def test_dockerfile_checks_every_download() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    args = _build_args(text)
    assert re.fullmatch(r"[0-9a-f]{64}", args.get("RDS_CA_SHA256", ""))
    assert re.fullmatch(r"[0-9a-f]{64}", args.get("POWERSHELL_DEB_SHA256", ""))
    assert re.fullmatch(r"[0-9A-F]{40}", args.get("PGDG_KEY_FINGERPRINT", ""))
    # Three downloads (PGDG key, RDS CA bundle, PowerShell .deb), each checked before use.
    assert len(re.findall(r"\bcurl\b[^\n]*-o ", text)) == 3
    assert "${RDS_CA_SHA256}" in text and "${POWERSHELL_DEB_SHA256}" in text
    assert text.count("sha256sum -c") == 2
    assert '"${PGDG_KEY_FINGERPRINT}"' in text
    # One RDS CA bundle across runtimes: the one the Lambda SQL runtime pins.
    sqlclient = (REPO_ROOT / "modules/dms-orchestration/src/shared/sqlclient.py").read_text(encoding="utf-8")
    assert f'CA_BUNDLE_SHA256 = "{args["RDS_CA_SHA256"]}"' in sqlclient


def test_dockerfile_installs_python_packages_by_hash_only() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "--require-hashes" in text and '--index-url "${PIP_INDEX_URL}"' in text
    reqs = (REPO_ROOT / "modules/dms-orchestration/src/jobs/sql-runner/requirements.txt").read_text(encoding="utf-8")
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==\S+ \\$", reqs, re.M)
    assert {"boto3", "botocore", "urllib3"} <= set(pins)
    for block in re.split(r"\n(?=[A-Za-z])", reqs.strip()):
        if block.startswith("#"):
            continue
        assert "--hash=sha256:" in block, block.splitlines()[0]


def test_dockerfile_powershell_is_off_by_default() -> None:
    assert _build_args(DOCKERFILE.read_text(encoding="utf-8"))["INSTALL_POWERSHELL"] == "false"


def test_ci_builds_the_image_only_when_asked() -> None:
    import yaml

    ci = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8"))
    image = ci["jobs"]["image"]
    assert image["if"] == "vars.BUILD_SQL_RUNNER_IMAGE == 'true'"
    steps = " ".join(str(s.get("run", "")) for s in image["steps"])
    assert "hadolint" in steps
    static = " ".join(str(s.get("run", "")) for s in ci["jobs"]["static"]["steps"])
    assert "hadolint" not in static


# --- the sandbox CodeBuild context (examples/sandbox/sql_runner.tf) -------------------


def test_sandbox_build_context_holds_every_file_the_dockerfile_copies() -> None:
    """Seen live: orch_dump.py was copied by the Dockerfile but missing from the zip CodeBuild
    builds from, so the image build failed. Every COPY source must be in sql_runner_files."""
    repo = Path(__file__).resolve().parents[2]
    dockerfile = (repo / "modules/dms-orchestration/src/jobs/sql-runner/Dockerfile").read_text(encoding="utf-8")
    tf = (repo / "examples/sandbox/sql_runner.tf").read_text(encoding="utf-8")
    listed = set(re.findall(r'"(modules/dms-orchestration/src/[^"]+)"', tf))
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


def test_sandbox_build_takes_the_pinned_base_through_the_build_arg() -> None:
    """The sandbox CodeBuild pulls the digest-pinned default of BASE_IMAGE from the ECR Public
    mirror (Docker Hub rate-limits CodeBuild) and passes it as the build arg."""
    tf = (REPO_ROOT / "examples/sandbox/sql_runner.tf").read_text(encoding="utf-8")
    assert "s/^ARG BASE_IMAGE=//p" in tf
    assert '--build-arg BASE_IMAGE="public.ecr.aws/docker/library/$base"' in tf
    assert "s/^FROM //p" not in tf


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


# --- review finding 9: the bundle is checked file by file against the pinned hash ---

V1 = "jobs/ledger/v1/"
PIN_FILES = {"ddl/020_indexes.sql": b"CREATE INDEX i ON t(x);", "jobset.json": b"{}"}


def _manifest(files: dict[str, bytes]) -> tuple[bytes, str]:
    from shared import bundle

    hashes = {rel: bundle.digest(data) for rel, data in files.items()}
    return bundle.render(hashes).encode(), bundle.total(hashes)


def _pin(job_env) -> str:
    manifest, total = _manifest(PIN_FILES)
    job_env.s3.put_object(Bucket=BUCKET, Key="jobs/ledger/v1/bundle.sha256", Body=manifest)
    return total


def _download(job_env, expected: str | None) -> list[str]:
    dest = job_env.tmp / "dest"
    dest.mkdir(exist_ok=True)
    orch_job.download_bundle(job_env.s3, f"s3://{BUCKET}/jobs/ledger/v1/", dest, expected)
    return sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())


def test_pinned_bundle_downloads_when_every_file_matches(job_env):
    assert _download(job_env, _pin(job_env)) == sorted(PIN_FILES)


@pytest.mark.parametrize(
    "change,fragment",
    [
        (lambda s3: s3.put_object(Bucket=BUCKET, Key=f"{V1}ddl/020_indexes.sql", Body=b"DROP TABLE t;"), "hash"),
        (lambda s3: s3.put_object(Bucket=BUCKET, Key=f"{V1}ddl/zz_extra.sql", Body=b"SELECT 1;"), "not in"),
        (lambda s3: s3.delete_object(Bucket=BUCKET, Key=f"{V1}ddl/020_indexes.sql"), "missing"),
    ],
)
def test_pinned_bundle_refuses_a_changed_extra_or_missing_file(job_env, change, fragment):
    from shared.errors import JobSetTampered

    total = _pin(job_env)
    change(job_env.s3)
    with pytest.raises(JobSetTampered, match=fragment):
        _download(job_env, total)


def test_rewritten_manifest_does_not_match_the_pin_of_the_run(job_env):
    from shared.errors import JobSetTampered

    total = _pin(job_env)
    evil = {**PIN_FILES, "ddl/020_indexes.sql": b"DROP TABLE t;"}
    for rel, data in evil.items():
        job_env.s3.put_object(Bucket=BUCKET, Key=f"jobs/ledger/v1/{rel}", Body=data)
    job_env.s3.put_object(Bucket=BUCKET, Key="jobs/ledger/v1/bundle.sha256", Body=_manifest(evil)[0])
    with pytest.raises(JobSetTampered, match="pinned"):
        _download(job_env, total)


def test_pinned_run_without_a_manifest_is_refused(job_env):
    from shared.errors import JobSetTampered

    with pytest.raises(JobSetTampered, match="bundle.sha256"):
        _download(job_env, "a" * 64)


def test_unpinned_run_still_checks_a_manifest_it_finds(job_env):
    from shared.errors import JobSetTampered

    _pin(job_env)
    job_env.s3.put_object(Bucket=BUCKET, Key="jobs/ledger/v1/jobset.json", Body=b'{"x": 1}')
    with pytest.raises(JobSetTampered):
        _download(job_env, None)


def test_tampered_bundle_fails_the_job_without_running_the_command(job_env):
    total = _pin(job_env)
    job_env.s3.put_object(Bucket=BUCKET, Key="jobs/ledger/v1/ddl/020_indexes.sql", Body=b"DROP TABLE t;")
    job_env.put_spec(bundleSha256=total)
    marker = job_env.tmp / "ran"
    code = orch_job.main([sys.executable, "-c", f"open({str(marker)!r}, 'w')"], s3=job_env.s3, secrets=job_env.sm)
    assert code == 1 and not marker.exists()
    result = result_of(job_env)
    assert result["status"] == "fail" and result["message"].startswith("JobSetTampered:")


# --- SSM runtime: orch-job on a runner instance with the project's job role ----------

JOB_ROLE = "arn:aws:iam::111122223333:role/dms-orch-test-job-ledger"
SSM_SPEC_URI = f"s3://{BUCKET}/runs/ledger/run-1/jobs/post/00-indexes/spec.json"


class FakeSts:
    def __init__(self, arn: str):
        self.arn = arn

    def get_caller_identity(self) -> dict[str, str]:
        return {"Arn": self.arn}


class FakeSessions:
    """boto3.Session stand-in: records the profile and the config file it was created with."""

    def __init__(self, clients: dict[str, Any], identity: str):
        self.clients = {**clients, "sts": FakeSts(identity)}
        self.created: list[dict[str, Any]] = []

    def __call__(self, profile_name: str | None = None) -> FakeSessions:
        import os

        config = Path(os.environ["AWS_CONFIG_FILE"])
        self.created.append(
            {
                "profile": profile_name,
                "env_profile": os.environ.get("AWS_PROFILE"),
                "config": config.read_text(),
                "mode": config.stat().st_mode & 0o777,
                "static_keys": "AWS_ACCESS_KEY_ID" in os.environ,
            }
        )
        return self

    def client(self, name: str, **_kw: Any) -> Any:
        return self.clients[name]


@pytest.fixture
def ssm_env(job_env, monkeypatch):
    # assume_role_session changes the process environment: let monkeypatch restore it.
    for name in ("AWS_CONFIG_FILE", "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "unset")
        monkeypatch.delenv(name)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    return job_env


def test_assume_role_session_writes_a_private_profile_and_checks_the_identity(ssm_env):
    sessions = FakeSessions({}, "arn:aws:sts::111122223333:assumed-role/dms-orch-test-job-ledger/orch-ledger-run-1")
    tmp = ssm_env.tmp / "t"
    tmp.mkdir()
    orch_job.assume_role_session(JOB_ROLE, "orch-ledger-run-1", tmp, sessions)
    made = sessions.created[0]
    assert made["profile"] == made["env_profile"] == "orch-job" and not made["static_keys"]
    assert f"role_arn = {JOB_ROLE}" in made["config"]
    assert "credential_source = Ec2InstanceMetadata" in made["config"]
    assert "role_session_name = orch-ledger-run-1" in made["config"]
    if sys.platform != "win32":
        assert made["mode"] == 0o600


def test_assume_role_session_refuses_another_identity(ssm_env):
    sessions = FakeSessions({}, "arn:aws:sts::111122223333:assumed-role/runner-instance/i-0aaa")
    tmp = ssm_env.tmp / "t"
    tmp.mkdir()
    with pytest.raises(ValueError, match="not the job role"):
        orch_job.assume_role_session(JOB_ROLE, "orch-ledger-run-1", tmp, sessions)


@pytest.mark.parametrize(
    "uri,name",
    [
        (SSM_SPEC_URI, "orch-ledger-run-1"),
        (f"s3://{BUCKET}/runs/ledger/{'r' * 80}/jobs/post/00-x/spec.json", ("orch-ledger-" + "r" * 80)[:64]),
    ],
)
def test_session_name_comes_from_the_spec_uri(uri, name):
    assert orch_job.session_name(uri) == name


SSM_SCRIPT = """
import json, os, pathlib
conns = pathlib.Path(os.environ['ORCH_CONNECTIONS_FILE'])
assert json.loads(conns.read_text())['target']['password'] == 'pw'
out = pathlib.Path(os.environ['ORCH_OUT_DIR'])
summary = {k: os.environ.get(k) for k in ('AWS_PROFILE', 'AWS_ACCESS_KEY_ID')}
summary['config'] = pathlib.Path(os.environ['AWS_CONFIG_FILE']).exists()
(out / 'result.json').write_text(json.dumps({'status': 'ok', 'summary': summary}))
"""


def test_orch_job_on_ssm_runs_the_spec_command_with_the_job_role(ssm_env):
    ssm_env.put_spec(command=[sys.executable, "-c", SSM_SCRIPT])
    identity = "arn:aws:sts::111122223333:assumed-role/dms-orch-test-job-ledger/orch-ledger-run-1"
    sessions = FakeSessions({"s3": ssm_env.s3, "secretsmanager": ssm_env.sm}, identity)
    code = orch_job.main(["--assume-role", JOB_ROLE, "--command-from-spec"], session_factory=sessions)
    assert code == 0
    result = result_of(ssm_env)
    # The command saw the job role profile, never static keys of the instance.
    assert result["summary"] == {"AWS_PROFILE": "orch-job", "AWS_ACCESS_KEY_ID": None, "config": True}
    tmp = ssm_env.tmp / "tmp"
    assert not (tmp / "connections.json").exists() and not (tmp / "aws-config").exists()


def test_orch_job_on_ssm_refuses_a_role_that_is_not_an_iam_role(ssm_env):
    ssm_env.put_spec(command=[sys.executable, "-c", "raise SystemExit(0)"])
    sessions = FakeSessions({}, "x")
    code = orch_job.main(
        ["--assume-role", "arn:aws:iam::111122223333:user/me", "--command-from-spec"], session_factory=sessions
    )
    assert code == 2 and sessions.created == []


def test_orch_job_on_ssm_without_the_job_role_runs_nothing(ssm_env, capsys):
    ssm_env.put_spec(command=[sys.executable, "-c", "raise SystemExit(0)"])
    sessions = FakeSessions({"s3": ssm_env.s3}, "arn:aws:sts::111122223333:assumed-role/other/x")
    code = orch_job.main(["--assume-role", JOB_ROLE, "--command-from-spec"], session_factory=sessions)
    assert code == 2 and "cannot use the job role" in capsys.readouterr().out
    assert not (ssm_env.tmp / "tmp" / "aws-config").exists()


@pytest.mark.parametrize("command", [None, "psql-run -f x", [1, 2], []])
def test_command_from_spec_must_be_a_list_of_strings(ssm_env, command):
    ssm_env.put_spec(command=command)
    identity = "arn:aws:sts::111122223333:assumed-role/dms-orch-test-job-ledger/orch-ledger-run-1"
    sessions = FakeSessions({"s3": ssm_env.s3, "secretsmanager": ssm_env.sm}, identity)
    assert orch_job.main(["--assume-role", JOB_ROLE, "--command-from-spec"], session_factory=sessions) == 2
    assert result_of(ssm_env)["status"] == "fail"


def test_connections_file_is_removed_when_the_job_wrapper_fails(job_env, monkeypatch):
    job_env.put_spec()

    def boom(*_a: Any, **_k: Any) -> list[str]:
        raise RuntimeError("upload failed")

    monkeypatch.setattr(orch_job, "upload_artifacts", boom)
    with pytest.raises(RuntimeError):
        orch_job.main([sys.executable, "-c", "pass"], s3=job_env.s3, secrets=job_env.sm)
    assert not (job_env.tmp / "tmp" / "connections.json").exists()


RUNTIME_SHARED = Path(__file__).resolve().parents[2] / "modules" / "dms-orchestration" / "src" / "shared"
RUNTIME_DIR = Path(__file__).resolve().parents[2] / "modules" / "dms-orchestration" / "src" / "jobs" / "sql-runner"
SSM_TF = Path(__file__).resolve().parents[2] / "modules" / "dms-orchestration" / "ssm.tf"


def _ssm_shared_files() -> list[str]:
    match = re.search(r"ssm_runtime_shared\s*=\s*\[([^\]]*)\]", SSM_TF.read_text(encoding="utf-8"))
    assert match, "ssm.tf lists the shared modules of the runtime zip in ssm_runtime_shared"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_ssm_runtime_zip_holds_the_shared_modules_the_image_copies():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    copy_lines = "\n".join(line for line in dockerfile.splitlines() if line.startswith("COPY "))
    copied = sorted(Path(src).name for src in re.findall(r"src/shared/[a-z_]+\.py", copy_lines))
    assert sorted(_ssm_shared_files()) == copied


def test_ssm_runtime_zip_carries_the_pinned_rds_ca_and_the_clients_use_it():
    """Live sandbox run: psql on the runner instance failed, root certificate file
    /etc/ssl/certs/rds-global-bundle.pem does not exist (the image installs it, the AMI does not).
    The zip carries the bundle the Lambda SQL runtime pins and orch-job points the clients at it."""
    ssm_tf = SSM_TF.read_text(encoding="utf-8")
    bundle = r'"rds-global-bundle\.pem"\s*=\s*"\$\{path\.module\}/src/lambdas/sql_jobs/rds-global-bundle\.pem"'
    assert re.search(bundle, ssm_tf)
    script = (SSM_TF.parent / "ssm" / "orch-job.sh.tftpl").read_text(encoding="utf-8")
    assert 'ORCH_CA_BUNDLE="$base/rt/rds-global-bundle.pem"' in script


def test_mysql_uses_the_ca_bundle_of_the_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_CA_BUNDLE", "/opt/rt/rds-global-bundle.pem")
    conn = {"host": "h", "username": "u", "password": "p", "database": "d"}
    argv, _env = orch_sql.client_command(conn, "mysql", [tmp_path / "a.sql"], on_error="stop", single_tx=False)
    assert "--ssl-ca=/opt/rt/rds-global-bundle.pem" in argv


def test_ssm_runtime_code_runs_on_the_python3_of_the_ami():
    """The runner instance runs the AMI's python3 (3.9 on Amazon Linux 2023), not the image's 3.13."""
    import ast

    files = sorted(RUNTIME_DIR.glob("*.py")) + [RUNTIME_SHARED / f for f in _ssm_shared_files()]
    for path in files:
        text = path.read_text(encoding="utf-8")
        ast.parse(text, filename=str(path), feature_version=(3, 9))
        assert not re.search(r"from datetime import [^\n]*\bUTC\b|datetime\.UTC", text), path.name
        if re.search(r"\w+ \| None|list\[|dict\[", text):
            assert "from __future__ import annotations" in text, path.name

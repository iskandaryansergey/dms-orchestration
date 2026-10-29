"""sql-runner pg-dump-s3 / pg-restore-s3: streamed dump to S3 and restore."""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import UTC, datetime
from typing import Any

import boto3
import orch_dump
import pytest

BUCKET = "dms-orch-artifacts-test"
SECRET_HOST = "tgt.cluster-abc.eu-west-2.rds.amazonaws.com"
CONNS = {
    "target": {
        "engine": "postgresql",
        "host": SECRET_HOST,
        "port": 5432,
        "database": "ledger",
        "username": "runner",
        "password": "s3cr3t",
        "sslMode": "verify-full",
    }
}
SPEC = {
    "runId": "ledger-core-20261028T100000Z-ab12",
    "ctx": {
        "project": "ledger",
        "taskKey": "core",
        "selectionRules": [{"schema": "app", "table": "%", "action": "include"}],
    },
}


class FakeProc:
    def __init__(self, data: bytes = b"", code: int = 0):
        self.stdout = io.BytesIO(data)
        self.stdin = io.BytesIO()
        self._code = code
        self.argv: list[str] = []
        self.env: dict[str, str] = {}

    def wait(self, timeout: Any = None) -> int:
        return self._code

    def kill(self) -> None:
        pass


@pytest.fixture
def dump_env(aws, tmp_path, monkeypatch):
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    (tmp_path / "conns.json").write_text(json.dumps(CONNS))
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("ORCH_CONNECTIONS_FILE", str(tmp_path / "conns.json"))
    monkeypatch.setenv("ORCH_JOB_SPEC_FILE", str(tmp_path / "spec.json"))
    monkeypatch.setenv("ORCH_OUT_DIR", str(out))
    monkeypatch.setenv("ORCH_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_ARTIFACTS_BUCKET", BUCKET)
    monkeypatch.delenv("ORCH_DRY_RUN", raising=False)
    return {"s3": s3, "out": out, "tmp": tmp_path}


def popen_returning(proc: FakeProc):
    def popen(argv: list[str], env: dict[str, str], **_kw: Any) -> FakeProc:
        proc.argv, proc.env = argv, env
        return proc

    return popen


NOW = datetime(2026, 10, 28, 10, 30, 0, tzinfo=UTC)


def test_dump_key_holds_no_host_user_or_secret():
    key = orch_dump.dump_prefix("ledger", "core", SPEC["runId"], NOW)
    assert key == "dumps/ledger/core/20261028T103000Z-ledger-core-20261028T100000Z-ab12/"
    assert re.fullmatch(r"dumps/[a-z0-9-]+/[a-z0-9_-]+/\d{8}T\d{6}Z-[A-Za-z0-9_-]+/", key)
    for bad in ("../x", "a/b", "Ledger"):
        with pytest.raises(ValueError):
            orch_dump.dump_prefix(bad, "core", "r", NOW)


def test_dump_streams_pg_dump_into_s3_with_kms_and_a_manifest(dump_env, monkeypatch):
    monkeypatch.setenv("ORCH_DUMP_KMS_KEY", "arn:aws:kms:eu-west-2:1:key/k")
    data = b"PGDMP" + b"x" * 1000
    proc = FakeProc(data)
    code = orch_dump.dump_main(["--connection", "target"], popen=popen_returning(proc), now=NOW)
    assert code == 0
    assert proc.argv[:4] == ["pg_dump", "-Fc", "--no-owner", "--no-privileges"] and proc.argv[-2:] == ["-n", "app"]
    assert proc.env["PGPASSWORD"] == "s3cr3t" and "s3cr3t" not in " ".join(proc.argv)
    prefix = "dumps/ledger/core/20261028T103000Z-ledger-core-20261028T100000Z-ab12/"
    s3 = dump_env["s3"]
    body = s3.get_object(Bucket=BUCKET, Key=prefix + "target.dump")
    assert body["Body"].read() == data and body["ServerSideEncryption"] == "aws:kms"
    manifest = json.loads(s3.get_object(Bucket=BUCKET, Key=prefix + "manifest.json")["Body"].read())
    assert manifest["sha256"] == hashlib.sha256(data).hexdigest() and manifest["bytes"] == len(data)
    assert manifest["schemas"] == ["app"] and manifest["runId"] == SPEC["runId"]
    assert SECRET_HOST not in json.dumps(manifest) and "runner" not in json.dumps(manifest)
    result = json.loads((dump_env["out"] / "result.json").read_text())
    assert result["status"] == "ok" and result["summary"]["dumpS3Uri"] == f"s3://{BUCKET}/{prefix}target.dump"


def test_failed_pg_dump_leaves_no_object(dump_env):
    proc = FakeProc(b"partial", code=1)
    code = orch_dump.dump_main(["--connection", "target"], popen=popen_returning(proc), now=NOW)
    assert code == 1
    assert dump_env["s3"].list_objects_v2(Bucket=BUCKET, Prefix="dumps/").get("KeyCount", 0) == 0
    assert dump_env["s3"].list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) == []
    assert json.loads((dump_env["out"] / "result.json").read_text())["status"] == "fail"


def test_dump_dry_run_runs_nothing(dump_env, monkeypatch):
    monkeypatch.setenv("ORCH_DRY_RUN", "1")
    called: list[Any] = []
    assert orch_dump.dump_main(["--connection", "target"], popen=lambda *a, **k: called.append(a), now=NOW) == 0
    assert called == [] and json.loads((dump_env["out"] / "result.json").read_text())["summary"]["dryRun"] is True


def _put_dump(s3: Any, data: bytes) -> str:
    prefix = "dumps/ledger/core/20261028T103000Z-r0/"
    s3.put_object(Bucket=BUCKET, Key=prefix + "target.dump", Body=data)
    manifest = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "schemas": ["app"]}
    s3.put_object(Bucket=BUCKET, Key=prefix + "manifest.json", Body=json.dumps(manifest).encode())
    return f"s3://{BUCKET}/{prefix}"


def test_restore_refuses_without_confirm(dump_env):
    uri = _put_dump(dump_env["s3"], b"PGDMP")
    assert orch_dump.restore_main(["--connection", "target", "--from", uri], popen=None) == 2


SQL_FROM_PG17 = (
    b"--\n-- PostgreSQL database dump\n--\n\n\\restrict Abc123\n\n"
    b"SET statement_timeout = 0;\nSET transaction_timeout = 0;\nSET lock_timeout = 0;\n"
    b"SELECT pg_catalog.set_config('search_path', '', false);\n"
    b"COPY app.notes (id, body) FROM stdin;\n1\tSET transaction_timeout = 0;\n\\.\n"
)


class Recorder:
    """popen for the restore pipeline: pg_restore writes SQL, psql reads it."""

    def __init__(self, sql: bytes = SQL_FROM_PG17, codes: tuple[int, int] = (0, 0)) -> None:
        self.procs: list[FakeProc] = []
        self.sql, self.codes = sql, codes

    def __call__(self, argv: list[str], env: dict[str, str], **_kw: Any) -> FakeProc:
        first = not self.procs
        proc = FakeProc(self.sql if first else b"", self.codes[0 if first else 1])
        proc.argv, proc.env = argv, env
        proc.stdin.close = lambda: None  # keep what was written readable
        self.procs.append(proc)
        return proc


def test_restore_checks_the_hash_then_pipes_pg_restore_into_psql_on_the_scope(dump_env):
    data = b"PGDMP-data"
    uri = _put_dump(dump_env["s3"], data)
    rec = Recorder()
    code = orch_dump.restore_main(["--connection", "target", "--from", uri, "--confirm"], popen=rec)
    assert code == 0
    restore, psql = (p.argv for p in rec.procs)
    assert restore[0] == "pg_restore" and "--clean" in restore and "--if-exists" in restore
    assert restore[-5:-1] == ["-n", "app", "-f", "-"] and restore[-1].endswith("target.dump")
    assert psql[0] == "psql" and "--single-transaction" in psql and "ON_ERROR_STOP=1" in psql
    assert rec.procs[1].env["PGDATABASE"] == "ledger"


def test_restore_drops_the_pg17_transaction_timeout_setting_from_the_header_only(dump_env):
    """Seen live (S10): the image's PostgreSQL 17 client writes SET transaction_timeout, which the
    Aurora 16 target refuses ("unrecognized configuration parameter"); nothing was restored.
    The header line goes, the same text inside the data stays."""
    uri = _put_dump(dump_env["s3"], b"PGDMP-data")
    rec = Recorder()
    assert orch_dump.restore_main(["--connection", "target", "--from", uri, "--confirm"], popen=rec) == 0
    fed = rec.procs[1].stdin.getvalue()
    assert b"SET statement_timeout = 0;\nSET lock_timeout = 0;\n" in fed
    assert fed.count(b"SET transaction_timeout") == 1 and b"1\tSET transaction_timeout = 0;\n" in fed


@pytest.mark.parametrize("codes", [(1, 0), (0, 3)])
def test_restore_fails_when_either_side_of_the_pipe_fails(dump_env, codes):
    uri = _put_dump(dump_env["s3"], b"PGDMP-data")
    rec = Recorder(codes=codes)
    assert orch_dump.restore_main(["--connection", "target", "--from", uri, "--confirm"], popen=rec) != 0


def test_restore_refuses_a_dump_whose_hash_does_not_match(dump_env):
    uri = _put_dump(dump_env["s3"], b"PGDMP")
    dump_env["s3"].put_object(Bucket=BUCKET, Key=uri.split(BUCKET + "/")[1] + "target.dump", Body=b"tampered")
    called: list[Any] = []
    code = orch_dump.restore_main(
        ["--connection", "target", "--from", uri, "--confirm"], popen=lambda *a, **k: called.append(a)
    )
    assert code == 1 and called == []


class BrokenPipe(io.BytesIO):
    def write(self, _b: Any) -> int:
        raise BrokenPipeError(32, "Broken pipe")


def test_restore_reports_psql_failing_early_instead_of_a_broken_pipe(dump_env):
    """Seen live: psql stopped at the first error (ON_ERROR_STOP) and the copy died with
    BrokenPipeError; the job must report psql's exit code."""
    uri = _put_dump(dump_env["s3"], b"PGDMP-data")
    rec = Recorder(codes=(0, 3))
    real = rec.__call__

    def popen(argv: list[str], env: dict[str, str], **kw: Any) -> FakeProc:
        proc = real(argv, env, **kw)
        if argv[0] == "psql":
            proc.stdin = BrokenPipe()
        return proc

    assert orch_dump.restore_main(["--connection", "target", "--from", uri, "--confirm"], popen=popen) == 3

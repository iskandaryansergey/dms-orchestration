"""sql-runner pg-dump-s3 / pg-restore-s3: streamed dump to S3 and restore."""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
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
        "selectionRules": [
            {"schema": "app", "table": "%", "action": "include"},
            {"schema": "app", "table": "tmp_%", "action": "exclude"},
        ],
    },
}
# Tables of the target as the catalogue query returns them: app.tmp_x is excluded by a rule,
# ops.jobs is in no include rule.
CATALOGUE = [
    {"s": "app", "t": "accounts", "partitioned": False},
    {"s": "app", "t": "ledger_lines", "partitioned": False},
    {"s": "app", "t": "tmp_x", "partitioned": False},
    {"s": "ops", "t": "jobs", "partitioned": False},
]


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


class Done:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class Runner:
    """subprocess.run for the catalogue query (psql) and the TOC listing (pg_restore -l)."""

    def __init__(self, catalogue: list[dict[str, Any]] | None = None, toc: str = "") -> None:
        self.catalogue = CATALOGUE if catalogue is None else catalogue
        self.toc = toc
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kw: Any) -> Done:
        self.calls.append(argv)
        if argv[0] == "psql":
            return Done(json.dumps(self.catalogue))
        assert argv[:2] == ["pg_restore", "-l"]
        return Done(self.toc)


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
    code = orch_dump.dump_main(["--connection", "target"], popen=popen_returning(proc), runner=Runner(), now=NOW)
    assert code == 0
    assert proc.argv[:4] == ["pg_dump", "-Fc", "--no-owner", "--no-privileges"]
    assert proc.argv[4:] == ["-t", '"app"."accounts"', "-t", '"app"."ledger_lines"'] and "-n" not in proc.argv
    assert proc.env["PGPASSWORD"] == "s3cr3t" and "s3cr3t" not in " ".join(proc.argv)
    prefix = "dumps/ledger/core/20261028T103000Z-ledger-core-20261028T100000Z-ab12/"
    s3 = dump_env["s3"]
    body = s3.get_object(Bucket=BUCKET, Key=prefix + "target.dump")
    assert body["Body"].read() == data and body["ServerSideEncryption"] == "aws:kms"
    manifest = json.loads(s3.get_object(Bucket=BUCKET, Key=prefix + "manifest.json")["Body"].read())
    assert manifest["sha256"] == hashlib.sha256(data).hexdigest() and manifest["bytes"] == len(data)
    assert manifest["schemas"] == ["app"] and manifest["runId"] == SPEC["runId"]
    assert manifest["tables"] == ["app.accounts", "app.ledger_lines"]
    assert manifest["project"] == "ledger" and manifest["taskKey"] == "core"
    assert SECRET_HOST not in json.dumps(manifest) and "runner" not in json.dumps(manifest)
    result = json.loads((dump_env["out"] / "result.json").read_text())
    assert result["status"] == "ok" and result["summary"]["dumpS3Uri"] == f"s3://{BUCKET}/{prefix}target.dump"


def test_failed_pg_dump_leaves_no_object(dump_env):
    proc = FakeProc(b"partial", code=1)
    code = orch_dump.dump_main(["--connection", "target"], popen=popen_returning(proc), runner=Runner(), now=NOW)
    assert code == 1
    assert dump_env["s3"].list_objects_v2(Bucket=BUCKET, Prefix="dumps/").get("KeyCount", 0) == 0
    assert dump_env["s3"].list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) == []
    assert json.loads((dump_env["out"] / "result.json").read_text())["status"] == "fail"


def test_dump_dry_run_runs_nothing(dump_env, monkeypatch):
    monkeypatch.setenv("ORCH_DRY_RUN", "1")
    called: list[Any] = []
    dry = orch_dump.dump_main(
        ["--connection", "target"], popen=lambda *a, **k: called.append(a), runner=Runner(), now=NOW
    )
    assert dry == 0
    assert called == [] and json.loads((dump_env["out"] / "result.json").read_text())["summary"]["dryRun"] is True


def test_dump_takes_only_the_tables_the_selection_rules_select():
    rules = SPEC["ctx"]["selectionRules"]
    assert orch_dump.scope_tables(CATALOGUE, rules) == ["app.accounts", "app.ledger_lines"]
    assert orch_dump.scope_tables(CATALOGUE, rules, schemas=["ops"]) == []
    assert orch_dump.scope_tables(CATALOGUE, [], schemas=["ops"]) == ["ops.jobs"]


def test_dump_refuses_a_partitioned_table_in_scope(dump_env):
    """pg_dump -t takes a partitioned table without its partitions: the restore would drop them."""
    catalogue = [*CATALOGUE, {"s": "app", "t": "events", "partitioned": True}]
    called: list[Any] = []
    code = orch_dump.dump_main(
        ["--connection", "target"], popen=lambda *a, **k: called.append(a), runner=Runner(catalogue), now=NOW
    )
    assert code == 2 and called == []
    assert "app.events" in json.loads((dump_env["out"] / "result.json").read_text())["message"]


def test_dump_refuses_an_empty_scope(dump_env):
    code = orch_dump.dump_main(["--connection", "target"], popen=None, runner=Runner([]), now=NOW)
    assert code == 2


MANIFEST_TABLES = ["app.accounts", "app.ledger_lines"]


def _put_dump(s3: Any, data: bytes, **manifest_over: Any) -> str:
    prefix = "dumps/ledger/core/20261028T103000Z-r0/"
    s3.put_object(Bucket=BUCKET, Key=prefix + "target.dump", Body=data)
    manifest = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "schemas": ["app"],
        "tables": MANIFEST_TABLES,
        "project": "ledger",
        "taskKey": "core",
        **manifest_over,
    }
    manifest = {k: v for k, v in manifest.items() if v is not None}
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

# pg_restore -l of a dump: header comments, a schema entry, in-scope and out-of-scope objects.
TOC = """;
; Archive created at <time>
;     dbname: ledger
;
; Selected TOC Entries:
;
6; 2615 16385 SCHEMA - app runner
5000; 0 0 COMMENT - SCHEMA app runner
215; 1259 16387 TABLE app accounts runner
216; 1259 16390 SEQUENCE app accounts_id_seq runner
217; 0 0 SEQUENCE OWNED BY app accounts_id_seq runner
218; 1259 16395 TABLE app ledger_lines runner
219; 1259 16399 TABLE app other runner
3200; 2604 16391 DEFAULT app accounts id runner
5001; 0 0 COMMENT app TABLE accounts runner
5002; 0 0 COMMENT app COLUMN other.id runner
3400; 0 16387 TABLE DATA app accounts runner
3401; 0 16395 TABLE DATA app ledger_lines runner
3402; 0 16399 TABLE DATA app other runner
3403; 0 0 SEQUENCE SET app accounts_id_seq runner
3250; 2606 16400 CONSTRAINT app accounts accounts_pkey runner
3251; 2606 16401 CONSTRAINT app other other_pkey runner
3260; 1259 16402 INDEX app ledger_lines_account_idx runner
3270; 2620 16403 TRIGGER app other other_audit runner
3280; 2606 16404 FK CONSTRAINT app ledger_lines ledger_lines_account_fkey runner
3281; 2606 16405 FK CONSTRAINT app other other_account_fkey runner
3290; 1255 16406 FUNCTION app audit() runner
"""
KEPT = [
    "215; 1259 16387 TABLE app accounts runner",
    "216; 1259 16390 SEQUENCE app accounts_id_seq runner",
    "217; 0 0 SEQUENCE OWNED BY app accounts_id_seq runner",
    "218; 1259 16395 TABLE app ledger_lines runner",
    "3200; 2604 16391 DEFAULT app accounts id runner",
    "5001; 0 0 COMMENT app TABLE accounts runner",
    "3400; 0 16387 TABLE DATA app accounts runner",
    "3401; 0 16395 TABLE DATA app ledger_lines runner",
    "3403; 0 0 SEQUENCE SET app accounts_id_seq runner",
    "3250; 2606 16400 CONSTRAINT app accounts accounts_pkey runner",
    "3260; 1259 16402 INDEX app ledger_lines_account_idx runner",
    "3280; 2606 16404 FK CONSTRAINT app ledger_lines ledger_lines_account_fkey runner",
]


def test_restore_toc_keeps_only_the_manifest_tables_and_never_a_schema():
    """--clean on whole schemas dropped objects outside the run's scope: the TOC is the scope."""
    assert orch_dump.toc_in_scope(TOC, MANIFEST_TABLES).splitlines() == KEPT


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


def _run_restore(uri: str, popen: Any, runner: Any = None) -> int:
    return orch_dump.restore_main(
        ["--connection", "target", "--from", uri, "--confirm"], popen=popen, runner=runner or Runner(toc=TOC)
    )


def test_restore_checks_the_hash_then_pipes_the_scoped_toc_into_one_psql_transaction(dump_env):
    uri = _put_dump(dump_env["s3"], b"PGDMP-data")
    rec = Recorder()
    assert _run_restore(uri, rec) == 0
    restore, psql = (p.argv for p in rec.procs)
    assert restore[0] == "pg_restore" and "--clean" in restore and "--if-exists" in restore
    assert "-n" not in restore and "--schema" not in " ".join(restore)
    toc = restore[restore.index("-L") + 1]
    assert Path(toc).read_text().splitlines() == KEPT
    assert restore[-3:-1] == ["-f", "-"] and restore[-1].endswith("target.dump")
    # psql -1 / --single-transaction does nothing for a script on stdin: the job sends BEGIN itself.
    assert psql[0] == "psql" and "ON_ERROR_STOP=1" in psql
    assert "--single-transaction" not in psql and "-1" not in psql
    fed = rec.procs[1].stdin.getvalue()
    assert fed.startswith(b"BEGIN;\n") and fed.endswith(b"COMMIT;\n") and b"ROLLBACK" not in fed
    assert rec.procs[1].env["PGDATABASE"] == "ledger"
    result = json.loads((dump_env["out"] / "result.json").read_text())
    assert result["status"] == "ok" and result["summary"]["tables"] == MANIFEST_TABLES


def test_restore_rolls_back_when_pg_restore_fails_even_if_psql_applied_everything(dump_env):
    """Exit codes in order: pg_restore ends first, then COMMIT only on 0, else ROLLBACK."""
    uri = _put_dump(dump_env["s3"], b"PGDMP-data")
    rec = Recorder(codes=(1, 0))
    assert _run_restore(uri, rec) == 1
    fed = rec.procs[1].stdin.getvalue()
    assert fed.startswith(b"BEGIN;\n") and fed.endswith(b"ROLLBACK;\n") and b"COMMIT" not in fed
    assert json.loads((dump_env["out"] / "result.json").read_text())["status"] == "fail"


class SpyS3:
    def __init__(self, s3: Any) -> None:
        self.s3, self.keys = s3, []

    def get_object(self, **kw: Any) -> Any:
        self.keys.append(kw["Key"])
        return self.s3.get_object(**kw)


@pytest.mark.parametrize(
    "over",
    [{"project": "other"}, {"taskKey": "edge"}, {"project": None}, {"tables": None}, {"tables": ["app.x;drop"]}],
)
def test_restore_refuses_a_manifest_of_another_scope_before_the_download(dump_env, monkeypatch, over):
    uri = _put_dump(dump_env["s3"], b"PGDMP-data", **over)
    spy = SpyS3(dump_env["s3"])
    monkeypatch.setattr(orch_dump, "_s3", lambda: spy)
    called: list[Any] = []
    code = orch_dump.restore_main(
        ["--connection", "target", "--from", uri, "--confirm"], popen=lambda *a, **k: called.append(a)
    )
    assert code == 2 and called == []
    assert not any(k.endswith("target.dump") for k in spy.keys)


def test_restore_refuses_a_location_outside_the_dumps_of_its_task(dump_env):
    uri = _put_dump(dump_env["s3"], b"PGDMP-data").replace("/core/", "/edge/")
    assert orch_dump.restore_main(["--connection", "target", "--from", uri, "--confirm"], popen=None) == 2


def test_restore_refuses_a_dump_without_a_manifest_table(dump_env):
    uri = _put_dump(dump_env["s3"], b"PGDMP-data", tables=["app.accounts", "app.missing"])
    called: list[Any] = []
    code = orch_dump.restore_main(
        ["--connection", "target", "--from", uri, "--confirm"],
        popen=lambda *a, **k: called.append(a),
        runner=Runner(toc=TOC),
    )
    assert code == 1 and called == []


def test_restore_drops_the_pg17_transaction_timeout_setting_from_the_header_only(dump_env):
    """Seen live (S10): the image's PostgreSQL 17 client writes SET transaction_timeout, which the
    Aurora 16 target refuses ("unrecognized configuration parameter"); nothing was restored.
    The header line goes, the same text inside the data stays."""
    uri = _put_dump(dump_env["s3"], b"PGDMP-data")
    rec = Recorder()
    assert _run_restore(uri, rec) == 0
    fed = rec.procs[1].stdin.getvalue()
    assert b"SET statement_timeout = 0;\nSET lock_timeout = 0;\n" in fed
    assert fed.count(b"SET transaction_timeout") == 1 and b"1\tSET transaction_timeout = 0;\n" in fed


@pytest.mark.parametrize("codes", [(1, 0), (0, 3)])
def test_restore_fails_when_either_side_of_the_pipe_fails(dump_env, codes):
    uri = _put_dump(dump_env["s3"], b"PGDMP-data")
    assert _run_restore(uri, Recorder(codes=codes)) != 0


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
    BrokenPipeError; the job must report psql's exit code and roll back (no COMMIT reached)."""
    uri = _put_dump(dump_env["s3"], b"PGDMP-data")
    rec = Recorder(codes=(0, 3))
    real = rec.__call__

    def popen(argv: list[str], env: dict[str, str], **kw: Any) -> FakeProc:
        proc = real(argv, env, **kw)
        if argv[0] == "psql":
            proc.stdin = BrokenPipe()
        return proc

    assert _run_restore(uri, popen) == 3

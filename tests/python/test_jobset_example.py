"""jobset_tool.py and the postgres-basic example bundle."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import jobset_tool
import pytest
import yaml

from shared import jobset

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "jobs" / "postgres-basic"
sys.path.insert(0, str(EXAMPLE / "scripts"))

import diff_schema  # noqa: E402
import row_counts  # noqa: E402


def test_example_job_set_is_valid():
    assert jobset_tool.validate(EXAMPLE) == 0
    jobs = jobset.normalize(yaml.safe_load((EXAMPLE / "jobset.yaml").read_text()))
    names = [j["name"] for j in jobs["pre"]]
    assert names == ["check-connections", "check-source-grants", "schema-diff", "create-tables"]
    assert jobs["pre"][0]["runtime"] == "lambda" and jobs["pre"][2]["runtime"] == "fargate"
    assert jobs["reset"][0]["retry"] == {"maxAttempts": 2, "backoffSeconds": 30}
    assert jobs["post"][0]["timeoutSeconds"] == 14400


def test_build_writes_json_next_to_bundle(tmp_path):
    out = tmp_path / "bundle"
    assert jobset_tool.build(EXAMPLE, out) == 0
    built = json.loads((out / "jobset.json").read_text())
    assert built["schemaVersion"] == 1 and (out / "ddl" / "020_indexes.sql").is_file()


def write_bundle(tmp_path: Path, jobset_text: str) -> Path:
    (tmp_path / "jobset.yaml").write_text(jobset_text)
    return tmp_path


BAD = [
    ("pre:\n    - name: a\n      command: [psql-run, --file, nope.sql]\n", "not found"),
    ("pre:\n    - name: a\n      handler: builtin:x/y\n      function: arn\n", "phases"),
    ("pre:\n    - name: a\n      command: [x]\n      when: 'a >'\n", "bad 'when'"),
    ("pre:\n    - {name: a, command: [x]}\n    - {name: a, command: [y]}\n", "duplicate"),
    ("post:\n    - {name: a, command: [x], idempotent: false, retry: {maxAttempts: 2}}\n", "non-idempotent"),
    ("pre:\n    - {name: a, runtime: lambda}\n", "exactly one"),
]


@pytest.mark.parametrize("phases,fragment", BAD)
def test_bad_job_sets_are_rejected(tmp_path, capsys, phases, fragment):
    bundle = write_bundle(tmp_path, "schemaVersion: 1\nphases:\n  " + phases)
    assert jobset_tool.validate(bundle) == 1
    assert fragment in capsys.readouterr().err


def test_template_paths_are_not_checked(tmp_path):
    text = "schemaVersion: 1\nphases:\n  reset:\n    - name: t\n      command: [psql-run, --template, '{{ ctx.f }}']\n"
    assert jobset_tool.validate(write_bundle(tmp_path, text)) == 0


def test_named_job_sets(tmp_path):
    bundle = write_bundle(tmp_path, "schemaVersion: 1\nphases: {}\n")
    (bundle / "jobset.cutover.yaml").write_text("schemaVersion: 1\nphases: {}\n")
    out = tmp_path / "out"
    assert jobset_tool.build(bundle, out) == 0
    assert (out / "jobset.json").exists() and (out / "jobset.cutover.json").exists()


# --- example scripts (psql replaced by a fake) ------------------------------------


def fake_query(data):
    def query(conn, sql):
        for needle, rows in data[conn].items():
            if needle in sql:
                return rows
        return []

    return query


def col(table, name, dtype):
    return {"table_name": table, "column_name": name, "data_type": dtype, "is_nullable": "NO"}


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_OUT_DIR", str(tmp_path))
    return tmp_path


def test_diff_schema_missing_tables_is_ok(out_dir, monkeypatch):
    source = [col("customers", "customer_id", "bigint"), col("accounts", "account_id", "bigint")]
    target = [col("customers", "customer_id", "bigint")]
    monkeypatch.setattr(diff_schema, "query", fake_query({"s": {"columns": source}, "t": {"columns": target}}))
    assert diff_schema.main(["--source", "s", "--target", "t", "--schema", "app"]) == 0
    result = json.loads((out_dir / "result.json").read_text())
    assert result["summary"]["missingTables"] == 1 and result["summary"]["missing"] == ["accounts"]
    assert json.loads((out_dir / "diff.json").read_text())["missing"] == ["accounts"]


def test_diff_schema_type_mismatch_fails(out_dir, monkeypatch):
    source = [col("customers", "customer_id", "bigint")]
    target = [col("customers", "customer_id", "integer")]
    monkeypatch.setattr(diff_schema, "query", fake_query({"s": {"columns": source}, "t": {"columns": target}}))
    assert diff_schema.main(["--source", "s", "--target", "t", "--schema", "app"]) == 1
    assert json.loads((out_dir / "result.json").read_text())["status"] == "fail"


def test_row_counts(out_dir, monkeypatch):
    tables = [{"table_name": "a"}, {"table_name": "b"}]
    data = {
        "s": {"information_schema": tables, '"a"': [{"n": "10"}], '"b"': [{"n": "5"}]},
        "t": {"information_schema": tables, '"a"': [{"n": "10"}], '"b"': [{"n": "4"}]},
    }
    monkeypatch.setattr(row_counts, "query", fake_query(data))
    assert row_counts.main(["--source", "s", "--target", "t", "--schema", "app"]) == 1
    result = json.loads((out_dir / "result.json").read_text())
    assert result["summary"] == {"tables": 2, "mismatched": ["app.b"]}


def test_scripts_reject_bad_schema_names():
    with pytest.raises(SystemExit):
        row_counts.main(["--source", "s", "--target", "t", "--schema", "app; drop"])
    with pytest.raises(SystemExit):
        diff_schema.main(["--source", "s", "--target", "t", "--schema", "x'--"])


# --- sandbox job sets: every template is a valid job set (s1iam adds jobset-iam) -------------


@pytest.mark.parametrize("template", sorted((ROOT / "examples" / "sandbox" / "jobs").glob("*.yaml.tmpl")), ids=str)
def test_sandbox_job_set_templates_are_valid(template, tmp_path):
    fn = "arn:aws:lambda:us-east-1:111122223333:function:sbx-sql"
    (tmp_path / "jobset.yaml").write_text(template.read_text().replace("__SQL_LAMBDA_ARN__", fn))
    assert jobset_tool.validate(tmp_path) == 0


def test_iam_job_set_logs_in_through_the_iam_connection():
    text = (ROOT / "examples" / "sandbox" / "jobs" / "jobset-iam.yaml.tmpl").read_text()
    post = jobset.normalize(yaml.safe_load(text))["post"]
    (iam,) = [j for j in post if j["name"] == "iam-target"]
    assert iam["runtime"] == "fargate" and iam["command"][:3] == ["psql-run", "--connection", "target_iam"]


# --- examples/jobs/minimal: the first job set of docs/job-contract.md ------------------------

MINIMAL = ROOT / "examples" / "jobs" / "minimal"


def test_minimal_example_is_valid_and_covers_every_phase():
    assert jobset_tool.validate(MINIMAL) == 0
    jobs = jobset.normalize(yaml.safe_load((MINIMAL / "jobset.yaml").read_text()))
    assert [j["name"] for j in jobs["pre"]] == ["check-connections"]
    assert jobs["pre"][0]["runtime"] == "lambda"
    assert jobs["reset"][0]["command"][:2] == ["psql-run", "--connection"]
    assert [j["runtime"] for j in jobs["post"]] == ["fargate", "lambda"]
    assert jobs["on_failure"][0]["onError"] == "continue"


def test_job_contract_tutorial_shows_the_minimal_example_verbatim():
    doc = (ROOT / "docs" / "job-contract.md").read_text(encoding="utf-8")
    section = doc.split("## Authoring a first job set", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"^```yaml\n(.*?)^```", section, re.S | re.M)
    assert (MINIMAL / "jobset.yaml").read_text(encoding="utf-8") in blocks
    for command in ("jobset_tool.py validate examples/jobs/minimal", "sync-jobs.sh", "--dry-run"):
        assert command in section, command

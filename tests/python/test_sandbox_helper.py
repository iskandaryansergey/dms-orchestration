"""examples/sandbox/scripts/sandbox.py: sandbox-only helpers."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "sandbox" / "scripts" / "sandbox.py"
spec = importlib.util.spec_from_file_location("sandbox_helper", SCRIPT)
sandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sandbox)


class FakeDms:
    """Only TestConnection and DescribeConnections: any other DMS call fails the test."""

    def __init__(self):
        self.tested = []

    def test_connection(self, ReplicationInstanceArn, EndpointArn):  # noqa: N803
        self.tested.append((ReplicationInstanceArn, EndpointArn))
        return {}

    def describe_connections(self, Filters):  # noqa: N803
        return {"Connections": [{"EndpointIdentifier": a, "Status": "successful"} for _, a in self.tested]}


def test_the_max_file_size_workaround_is_gone():
    # Fixed in the upstream dms module from the minimum commit in upstream.env.example (contract 2.6).
    assert not hasattr(sandbox, "fix_target_max_file_size")
    assert "MaxFileSize" not in SCRIPT.read_text(encoding="utf-8")


def test_test_connections_only_tests_the_connections(monkeypatch, capsys):
    dms = FakeDms()

    class Session:
        def client(self, name):
            assert name == "dms"
            return dms

    monkeypatch.setattr(sandbox, "session", lambda out: Session())
    monkeypatch.setattr(sandbox.time, "sleep", lambda s: pytest.fail("no wait when nothing is testing"))
    out = {"replication_instance_arn": "ri", "endpoint_arns": {"source": "src", "target": "tgt"}}
    sandbox.cmd_test_connections(out)
    assert dms.tested == [("ri", "src"), ("ri", "tgt")]
    assert "successful" in capsys.readouterr().out


def test_connection_test_already_running_is_not_an_error():
    # Seen live: ModifyEndpoint starts a connection test by itself, so one can already be running.
    from botocore.exceptions import ClientError

    class Busy:
        def test_connection(self, **kw):
            raise ClientError(
                {"Error": {"Code": "InvalidResourceStateFault", "Message": "Connection is already being tested"}},
                "TestConnection",
            )

    assert sandbox.start_connection_test(Busy(), "ri", "ep") is False


def test_image_scan_blocks_on_high_and_critical_only():
    assert sandbox.scan_blocks({"MEDIUM": 3, "LOW": 9, "INFORMATIONAL": 1}) == []
    assert sandbox.scan_blocks({"HIGH": 1, "MEDIUM": 3}) == ["HIGH"]
    assert sandbox.scan_blocks({"CRITICAL": 2, "HIGH": 1}) == ["CRITICAL", "HIGH"]


def _zip(files: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_build_sources_older_than_the_tree_are_listed(tmp_path):
    # Seen live: CodeBuild builds from the zip that the last terraform apply uploaded; a build
    # right after a commit pushed the old code under the new, immutable tag.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "same.py").write_bytes(b"same\n")
    (tmp_path / "src" / "changed.py").write_bytes(b"new\n")
    archive = _zip(
        {
            "src/same.py": b"same\n",
            "src/changed.py": b"old\n",
            "src/gone.py": b"x\n",
            "buildspec.yml": b"version: 0.2\n",
        }
    )
    assert sandbox.stale_build_sources(archive, tmp_path) == ["src/changed.py", "src/gone.py"]
    assert sandbox.stale_build_sources(_zip({"src/same.py": b"same\n"}), tmp_path) == []


def test_build_image_refuses_when_the_sandbox_build_is_off(monkeypatch, capsys):
    """CodeBuild is a sandbox convenience behind var.sql_runner (off by default): without it
    the outputs carry no project, and build-image stops before any AWS call."""

    def no_session(out):
        raise AssertionError("must not call AWS")

    monkeypatch.setattr(sandbox, "session", no_session)
    out = {"artifacts_bucket": "bkt", "sql_runner_codebuild_project": None, "sql_runner_repository": None}
    assert sandbox.cmd_build_image(out, "abc1234") == 2
    assert "sql_runner=true" in capsys.readouterr().out


def test_sandbox_build_is_off_by_default_and_sandbox_only():
    tf = (SCRIPT.parents[1] / "variables.tf").read_text(encoding="utf-8")
    block = tf[tf.index('variable "sql_runner" {') :]
    block = block[: block.index("\n}\n")]
    assert "default     = false" in block and "Sandbox only" in block
    header = (SCRIPT.parents[1] / "sql_runner.tf").read_text(encoding="utf-8").split("\n\n")[0]
    assert "never part of the module" in header


def test_build_image_refuses_stale_sources_before_starting_a_build(tmp_path, monkeypatch, capsys):
    import io

    started = []

    class S3:
        def get_object(self, Bucket, Key):
            assert (Bucket, Key) == ("bkt", sandbox.BUILD_SOURCE_KEY)
            return {"Body": io.BytesIO(_zip({"src/changed.py": b"old\n"}))}

    class CodeBuild:
        def start_build(self, **kw):
            started.append(kw)
            raise AssertionError("must not build")

    class Session:
        def client(self, name):
            return {"s3": S3(), "codebuild": CodeBuild(), "ecr": object()}[name]

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "changed.py").write_bytes(b"new\n")
    monkeypatch.setattr(sandbox, "session", lambda out: Session())
    monkeypatch.setattr(sandbox, "REPO", tmp_path)
    out = {"artifacts_bucket": "bkt", "sql_runner_codebuild_project": "p", "sql_runner_repository": "r"}
    assert sandbox.cmd_build_image(out, "abc1234") != 0
    assert started == []
    assert "terraform apply" in capsys.readouterr().out


# --- S16: SQS fan-out with a filter policy -------------------------------

SANDBOX = SCRIPT.parents[1]
E2E = SANDBOX / "scripts" / "e2e.py"


def _attributes(status: str) -> dict[str, dict[str, str]]:
    from lambdas.write_report.handler import SEVERITY, message_attributes

    return message_attributes(
        environment="sandbox",
        project="ledger",
        taskKey="core",
        status=status,
        phase="done",
        severity=SEVERITY.get(status, "low"),
    )


@pytest.mark.parametrize(
    "status,delivered",
    [("FAILED", True), ("MANUAL", True), ("ABORTED", True), ("SUCCEEDED", False), ("REJECTED", False)],
)
def test_the_fanout_filter_policy_lets_through_only_high_severity(status, delivered):
    policy = json.loads((SANDBOX / "fanout-filter-policy.json").read_text())
    assert policy == {"severity": ["high"]}
    assert sandbox.filter_matches(policy, _attributes(status)) is delivered


def test_filter_matches_needs_every_key_of_the_policy():
    assert sandbox.filter_matches({"severity": ["high"], "project": ["x"]}, _attributes("FAILED")) is False
    assert sandbox.filter_matches({"severity": ["high"]}, {}) is False


def _envelope(run_id: str, status: str, severity: str) -> dict[str, str]:
    body = {
        "Type": "Notification",
        "Message": json.dumps({"runId": run_id, "status": status, "severity": severity}),
        "MessageAttributes": {"severity": {"Type": "String", "Value": severity}},
    }
    return {"Body": json.dumps(body), "ReceiptHandle": f"rh-{run_id}"}


class FakeSqs:
    def __init__(self, batches):
        self.batches = list(batches)
        self.deleted = []
        self.calls = []

    def receive_message(self, **kw):
        self.calls.append(kw)
        return {"Messages": self.batches.pop(0)} if self.batches else {}

    def delete_message(self, QueueUrl, ReceiptHandle):  # noqa: N803
        self.deleted.append(ReceiptHandle)


def test_drain_reads_sns_envelopes_and_deletes_them():
    sqs = FakeSqs([[_envelope("r1", "FAILED", "high")], [_envelope("r2", "MANUAL", "high")]])
    got = sandbox.drain_queue(sqs, "https://q", wait_seconds=1, empty_polls=1)
    assert got == [
        {"runId": "r1", "status": "FAILED", "severity": "high", "attributes": {"severity": "high"}},
        {"runId": "r2", "status": "MANUAL", "severity": "high", "attributes": {"severity": "high"}},
    ]
    assert sqs.deleted == ["rh-r1", "rh-r2"]
    assert sqs.calls[0]["QueueUrl"] == "https://q" and sqs.calls[0]["WaitTimeSeconds"] == 1


def test_fanout_verdict_wants_failed_and_manual_but_not_succeeded():
    runs = {"SUCCEEDED": "ok-run", "FAILED": "bad-run", "MANUAL": "stop-run"}
    got = [
        {"runId": "bad-run", "status": "FAILED", "severity": "high", "attributes": {"severity": "high"}},
        {"runId": "stop-run", "status": "MANUAL", "severity": "high", "attributes": {"severity": "high"}},
    ]
    checks = sandbox.fanout_checks(runs, got)
    assert all(checks.values()), checks
    leaked = [*got, {"runId": "ok-run", "status": "SUCCEEDED", "severity": "low", "attributes": {}}]
    assert sandbox.fanout_checks(runs, leaked)["SUCCEEDED not delivered"] is False
    assert sandbox.fanout_checks(runs, got[:1])["MANUAL delivered"] is False


def test_e2e_has_s16_by_name_only():
    text = E2E.read_text(encoding="utf-8")
    assert "e2e.py s16" in text and 'scenarios["s16"] = s16' in text
    assert '"s16": s16' not in text  # not in the default list: needs sqs_fanout = true


def test_e2e_has_the_live_checks_of_the_run6_fixes_by_name_only():
    text = E2E.read_text(encoding="utf-8")
    for name in ("guard", "postdie"):
        assert f"e2e.py {name}" in text and f'scenarios["{name}"] = {name}' in text
        assert f'"{name}": {name},' not in text  # they plant DynamoDB items: never in the default list
    assert '"refusal leaves the target untouched"' in text  # S14
    assert '"baseline from the restore"' in text  # S10
    assert 'pf.get("error") == "RestoredTarget"' in text  # S10: a plan after the restore never resumes


def test_e2e_has_s1ssm_by_name_only_and_s10_restores_on_the_runtime_of_the_sandbox():
    text = E2E.read_text(encoding="utf-8")
    assert "e2e.py s1ssm" in text and 'scenarios["s1ssm"] = s1ssm' in text
    assert '"s1ssm": s1ssm' not in text  # needs ssm_runner = true
    assert '"--runtime"' in text and '"e2e-ssm" if on_ssm else "e2e-fargate"' in text  # S10 on SSM


def test_the_sandbox_runner_instance_is_a_stand_in_for_the_team_ami():
    tf = re.sub(r"[ ]+", " ", (SANDBOX / "ssm_runner.tf").read_text(encoding="utf-8"))
    assert tf.count("count = var.ssm_runner ? 1 : 0") >= 4  # role, profile, instance, policy
    assert "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64" in tf
    assert 'var.runner_ami_id != "" ? var.runner_ami_id' in tf
    assert 'http_tokens = "required"' in tf and "associate_public_ip_address = false" in tf
    assert "encrypted = true" in tf
    assert "module.dms_orchestration.ssm_runner_policy_json" in tf
    assert "AmazonSSMManagedInstanceCore" in tf
    variables = (SANDBOX / "variables.tf").read_text(encoding="utf-8")
    for name in ("ssm_runner", "runner_ami_id"):
        assert f'variable "{name}"' in variables
    assert "default = false" in re.sub(
        r"[ ]+", " ", variables[variables.index('variable "ssm_runner"') :].split("}")[0]
    )
    main = re.sub(r"\s+", " ", (SANDBOX / "main.tf").read_text(encoding="utf-8"))
    assert "enabled = var.ssm_runner" in main and "instance_ids = aws_instance.ssm_runner[*].id" in main
    assert '"ec2messages", "ssmmessages"' in main  # agent endpoints from the network contract


def test_the_ssm_job_set_runs_its_command_jobs_on_ssm():
    text = (SANDBOX / "jobs" / "jobset-ssm.yaml.tmpl").read_text(encoding="utf-8")
    assert "runtime: ssm" in text and "image:" not in text
    helper = (SANDBOX / "scripts" / "sandbox.py").read_text(encoding="utf-8")
    assert 'cmd_sync_jobs(out, "jobset-ssm.yaml.tmpl", "e2e-ssm")' in helper


def test_the_sandbox_wires_the_queue_only_behind_the_variable():
    tf = re.sub(r"[ ]+", " ", (SANDBOX / "fanout.tf").read_text(encoding="utf-8"))
    assert 'file("${path.module}/fanout-filter-policy.json")' in tf
    assert tf.count("count = var.sqs_fanout ? 1 : 0") == 3
    assert 'filter_policy_scope = "MessageAttributes"' in tf and "sqs_managed_sse_enabled = true" in tf
    assert 'protocol = "sqs"' in tf and '"aws:SourceArn" = module.dms_orchestration.sns_topic_arn' in tf
    variables = (SANDBOX / "variables.tf").read_text(encoding="utf-8")
    block = variables[variables.index('variable "sqs_fanout"') :]
    assert "default     = false" in block.split("}")[0]


# --- idem-sfn reads the post history for the real ExecutionAlreadyExists ----


def _hist(*steps):
    """Step Functions history events: ("enter", state) | ("fail", error) | ("exec_failed",)."""
    out = []
    for i, step in enumerate(steps, 1):
        if step[0] == "enter":
            out.append({"id": i, "type": "TaskStateEntered", "stateEnteredEventDetails": {"name": step[1]}})
        elif step[0] == "fail":
            out.append({"id": i, "type": "TaskFailed", "taskFailedEventDetails": {"error": step[1], "cause": "x"}})
        else:
            out.append({"id": i, "type": "ExecutionFailed", "executionFailedEventDetails": {"error": "x"}})
    return out


ALREADY = "StepFunctions.ExecutionAlreadyExistsException"


def test_resume_collision_is_caught_as_already_started():
    events = _hist(("enter", "HandOverLock"), ("enter", "StartResume"), ("fail", ALREADY), ("enter", "WriteReport"))
    checks = sandbox.resume_collision_checks(events)
    assert checks and all(checks.values()), checks


@pytest.mark.parametrize(
    "events",
    [
        # AWS named the error differently: the Catch would not match and the post would give the lock back.
        _hist(("enter", "StartResume"), ("fail", "StepFunctions.ExecutionAlreadyExists"), ("enter", "GiveLockBack")),
        # StartResume was not reached (classification did not auto-resume).
        _hist(("enter", "ClassifyFailure"), ("enter", "OnFailureJobs"), ("enter", "WriteReport")),
        # The start went through: the decoy did not collide.
        _hist(("enter", "StartResume"), ("enter", "WriteReport")),
        _hist(("enter", "StartResume"), ("fail", ALREADY), ("enter", "WriteReport"), ("exec_failed",)),
    ],
)
def test_resume_collision_checks_fail_on_anything_else(events):
    assert not all(sandbox.resume_collision_checks(events).values())


def test_big_tables_are_one_statement_with_a_primary_key_and_one_row_on_the_source():
    source = sandbox.big_tables_sql(600, rows=True)
    target = sandbox.big_tables_sql(600, rows=False)
    drop = sandbox.big_tables_sql(600, drop=True)
    for sql in (source, target, drop):
        assert sql.startswith("DO $$") and sql.rstrip().endswith("$$") and "1..600" in sql
    assert "PRIMARY KEY" in source and "INSERT INTO" in source and "ON CONFLICT DO NOTHING" in source
    assert "PRIMARY KEY" in target and "INSERT INTO" not in target
    assert "DROP TABLE IF EXISTS" in drop and "CREATE" not in drop
    assert sandbox.big_table_name(7) == "app.e2e_big_0007"
    with pytest.raises(ValueError):
        sandbox.big_tables_sql(10000, rows=True)


def test_seed_tables_option_adds_only_the_big_tables(monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox, "outputs", lambda: {"region": "us-east-1"})
    monkeypatch.setattr(sandbox, "data_api", lambda out, db, sql, secret=None: calls.append((db, sql)) or [])
    monkeypatch.setattr(sandbox, "cmd_counts", lambda out: {})
    assert sandbox.main(["seed", "--tables", "600"]) == 0
    assert [db for db, _ in calls] == ["source", "target"]
    assert all("e2e_big_" in sql for _, sql in calls)


def test_sync_jobs_uploads_the_sql_and_recheck_job_sets():
    helper = (SANDBOX / "scripts" / "sandbox.py").read_text(encoding="utf-8")
    assert 'cmd_sync_jobs(out, "jobset-sql.yaml.tmpl", "e2e-sql")' in helper
    assert 'cmd_sync_jobs(out, "jobset-recheck.yaml.tmpl", "e2e-recheck")' in helper
    sql = (SANDBOX / "jobs" / "jobset-sql.yaml.tmpl").read_text(encoding="utf-8")
    assert 'when: "run.dryRun"' in sql and "truncateScope: true" in sql


PLANNED = ["dry-ro", "s4big", "s17-lock-lost", "s18-recheck", "s19-chain-dump", "s20-tampered-jobset", "s21-validation"]


@pytest.mark.parametrize("name", PLANNED)
def test_e2e_has_the_planned_scenarios_by_name_only(name):
    text = E2E.read_text(encoding="utf-8")
    assert f"e2e.py {name} " in text and f'scenarios["{name}"] = ' in text
    assert f'"{name}": ' not in text.split("names = argv or list(scenarios)")[0].split("def main")[-1]

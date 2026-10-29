"""scripts/orch.py with fake clients."""

from __future__ import annotations

import io
import json
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orch
import pytest

SM = "arn:aws:states:eu-west-2:111122223333:stateMachine:dms-orch-dev-migrate"
POST_SM = "arn:aws:states:eu-west-2:111122223333:stateMachine:dms-orch-dev-post"


def _missing() -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "ExecutionDoesNotExist", "Message": "x"}}, "DescribeExecution")


class FakeSfn:
    """describe_execution answers from one list, or per "<machine>:<name>" from a dict of lists."""

    def __init__(self, statuses: Any = None):
        self.started: list[dict[str, Any]] = []
        self.stopped: list[dict[str, Any]] = []
        self.statuses = statuses or [{"status": "RUNNING"}]

    def start_execution(self, **kw: Any) -> dict[str, Any]:
        self.started.append(kw)
        return {"executionArn": SM.replace(":stateMachine:", ":execution:") + ":" + kw["name"]}

    def describe_execution(self, executionArn: str) -> dict[str, Any]:  # noqa: N803
        seq = self.statuses
        if isinstance(seq, dict):
            machine, name = executionArn.split(":")[-2:]
            seq = seq.get(f"{machine.rsplit('-', 1)[-1]}:{name}")
            if seq is None:
                raise _missing()
        status = seq.pop(0) if len(seq) > 1 else seq[0]
        if status == "missing":
            raise _missing()
        return {"executionArn": executionArn, **status}

    def stop_execution(self, **kw: Any) -> dict[str, Any]:
        self.stopped.append(kw)
        return {}


class FakeDdb:
    def __init__(self, item: dict[str, Any] | None = None, items: list[dict[str, Any]] | None = None):
        self.item = item
        self.items = items or []
        self.queries: list[dict[str, Any]] = []

    def get_item(self, **_kw: Any) -> dict[str, Any]:
        return {"Item": self.item} if self.item else {}

    def query(self, **kw: Any) -> dict[str, Any]:
        self.queries.append(kw)
        return {"Items": self.items}


class FakeDms:
    def __init__(self) -> None:
        self.stopped: list[str] = []

    def stop_replication_task(self, ReplicationTaskArn: str) -> dict[str, Any]:  # noqa: N803
        self.stopped.append(ReplicationTaskArn)
        return {}


def run(argv: list[str], **fakes: Any) -> tuple[int, str]:
    out = io.StringIO()
    code = orch.main(argv, orch.Clients(**fakes), out)
    return code, out.getvalue()


def test_execution_name_is_valid_and_short():
    name = orch.execution_name("a" * 40, "b" * 40, datetime(2026, 10, 27, tzinfo=UTC))
    assert len(name) <= 80 and name.startswith("a")
    assert orch.execution_name("ledger", "core", datetime(2026, 10, 27, 10, tzinfo=UTC)).startswith(
        "ledger-core-20261027T100000Z-"
    )


def test_start_builds_request_and_starts_execution():
    sfn = FakeSfn()
    code, out = run(
        [
            "--state-machine-arn",
            SM,
            "start",
            "--project",
            "ledger",
            "--task",
            "core",
            "--dry-run",
            "--reset-policy",
            "never",
            "--jobs-version",
            "v2",
            "--requested-by",
            "op@example.com",
            "--max-resumes",
            "0",
        ],
        stepfunctions=sfn,
    )
    assert code == 0
    req = json.loads(sfn.started[0]["input"])
    assert req == {
        "project": "ledger",
        "taskKey": "core",
        "operation": "migrate",
        "targetReset": {"policy": "never"},
        "jobsVersion": "v2",
        "dryRun": True,
        "autoResume": {"maxAttempts": 0},
        "requestedBy": "op@example.com",
    }
    assert json.loads(out)["runId"] == sfn.started[0]["name"]


def test_start_request_validates_against_schema():
    from shared import validation

    args = orch.parser().parse_args(
        [
            "start",
            "--project",
            "ledger",
            "--task",
            "core",
            "--operation",
            "plan",
            "--start-type",
            "reload-target",
            "--change-ref",
            "CHG1",
        ]
    )
    validation.validate("run_request", orch.build_request(args))


def _done(status: str, **output: Any) -> dict[str, Any]:
    return {"status": "SUCCEEDED", "output": json.dumps({"status": status, **output})}


def test_start_wait_follows_migrate_then_post(monkeypatch):
    monkeypatch.setattr(orch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(orch, "execution_name", lambda *_a: "r1")
    sfn = FakeSfn(
        {
            "migrate:r1": [{"status": "RUNNING"}, _done("STARTED")],
            "post:r1-post": ["missing", {"status": "RUNNING"}, _done("SUCCEEDED", outcome="COMPLETED")],
        }
    )
    code, out = run(
        ["--state-machine-arn", SM, "start", "--project", "ledger", "--task", "core", "--wait"], stepfunctions=sfn
    )
    assert code == 0, out
    assert "DMS running" in out and '"outcome": "COMPLETED"' in out


def test_start_wait_follows_an_auto_resume_chain(monkeypatch):
    monkeypatch.setattr(orch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(orch, "execution_name", lambda *_a: "r1")
    sfn = FakeSfn(
        {
            "migrate:r1": [_done("STARTED")],
            "post:r1-post": [_done("RESUMED", nextRunId="r1-r2")],
            "migrate:r1-r2": [_done("STARTED")],
            "post:r1-r2-post": [_done("SUCCEEDED", outcome="COMPLETED")],
        }
    )
    code, out = run(
        ["--state-machine-arn", SM, "start", "--project", "ledger", "--task", "core", "--wait"], stepfunctions=sfn
    )
    assert code == 0 and "r1-r2" in out


def test_start_wait_ends_with_migrate_when_nothing_was_started(monkeypatch):
    monkeypatch.setattr(orch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(orch, "execution_name", lambda *_a: "r1")
    planned = FakeSfn({"migrate:r1": [_done("PLANNED")]})
    argv = ["--state-machine-arn", SM, "start", "--project", "ledger", "--task", "core", "--operation", "plan"]
    code, _ = run([*argv, "--wait"], stepfunctions=planned)
    assert code == 0


def test_start_wait_returns_exit_code(monkeypatch):
    monkeypatch.setattr(orch.time, "sleep", lambda _s: None)
    bad = FakeSfn([{"status": "FAILED", "error": "RunAlreadyActive", "cause": "locked"}])
    code, out = run(
        ["--state-machine-arn", SM, "start", "--project", "ledger", "--task", "core", "--wait"], stepfunctions=bad
    )
    assert code == 1 and "RunAlreadyActive" in out


def test_start_requires_state_machine():
    with pytest.raises(SystemExit):
        run(["start", "--project", "ledger", "--task", "core"], stepfunctions=FakeSfn())


def test_status_shows_both_executions_and_the_run_item():
    ddb = FakeDdb({"pk": {"S": "RUN#r1"}, "status": {"S": "RUNNING"}, "taskArn": {"S": "arn:task"}})
    sfn = FakeSfn({"migrate:r1": [_done("STARTED")], "post:r1-post": [{"status": "RUNNING"}]})
    code, out = run(["--state-machine-arn", SM, "--table", "t", "status", "r1"], stepfunctions=sfn, dynamodb=ddb)
    data = json.loads(out)
    assert code == 0 and data["executionArn"].endswith(":execution:dms-orch-dev-migrate:r1")
    assert data["status"] == "SUCCEEDED" and data["post"]["status"] == "RUNNING"
    assert data["post"]["executionArn"].endswith(":execution:dms-orch-dev-post:r1-post")
    assert data["run"] == {"status": "RUNNING", "taskArn": "arn:task"}


def test_status_before_post_says_dms_is_running():
    sfn = FakeSfn({"migrate:r1": [_done("STARTED")]})
    data = json.loads(run(["--state-machine-arn", SM, "status", "r1"], stepfunctions=sfn)[1])
    assert data["post"] == {"status": "NOT_STARTED", "note": "DMS running or post not started yet"}


def test_post_state_machine_is_derived_or_given():
    assert orch.post_state_machine(SM, "") == POST_SM
    assert orch.post_state_machine(SM, "arn:x") == "arn:x"


def test_post_execution_name_matches_the_handoff():
    from shared.handoff import post_execution_name

    for run_id in ("r1", "ledger-core-20261028T100000Z-ab12", "x" * 80):
        assert orch.post_execution_name(run_id) == post_execution_name(run_id)


def test_stop_stops_whichever_execution_of_the_run_is_running():
    sfn = FakeSfn({"migrate:r1": [_done("STARTED")], "post:r1-post": [{"status": "RUNNING"}]})
    code, _ = run(["--state-machine-arn", SM, "stop", "r1"], stepfunctions=sfn)
    assert code == 0 and [s["executionArn"].rsplit(":", 1)[-1] for s in sfn.stopped] == ["r1-post"]
    sfn = FakeSfn({"migrate:r1": [_done("STARTED")]})
    out = json.loads(run(["--state-machine-arn", SM, "stop", "r1"], stepfunctions=sfn)[1])
    assert out["stopped"] == [] and "DMS keeps running" in out["note"]


def test_stop_with_task():
    sfn, dms = FakeSfn(), FakeDms()
    ddb = FakeDdb({"taskArn": {"S": "arn:aws:dms:eu-west-2:1:task:X"}})
    code, out = run(
        ["--state-machine-arn", SM, "--table", "t", "stop", "r1", "--stop-task", "--reason", "window"],
        stepfunctions=sfn,
        dynamodb=ddb,
        dms=dms,
    )
    assert code == 0 and dms.stopped == ["arn:aws:dms:eu-west-2:1:task:X"]
    assert sfn.stopped[0]["error"] == "StoppedByOperator" and sfn.stopped[0]["cause"] == "window"


def test_report_reads_markdown():
    class S3:
        def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
            assert (Bucket, Key) == ("b", "runs/r1/report.md")
            return {"Body": io.BytesIO(b"# report")}

    code, out = run(["--bucket", "b", "report", "r1"], s3=S3())
    assert code == 0 and out.strip() == "# report"


def test_list_queries_project_history():
    ddb = FakeDdb(items=[{"pk": {"S": "RUN#r1"}, "status": {"S": "SUCCEEDED"}, "dryRun": {"BOOL": False}}])
    code, out = run(["--table", "t", "list", "--project", "ledger"], dynamodb=ddb)
    assert json.loads(out) == [{"status": "SUCCEEDED", "dryRun": False, "runId": "r1"}]
    assert ddb.queries[0]["ExpressionAttributeValues"] == {":p": {"S": "PROJECT#ledger"}}


# --- promotion gate and allowReset ---------------------------------------------


class KeyedDdb:
    def __init__(self, items: dict[str, dict[str, Any]]):
        self.items = items

    def get_item(self, **kw: Any) -> dict[str, Any]:
        item = self.items.get(kw["Key"]["pk"]["S"])
        return {"Item": item} if item else {}


TASK = "arn:aws:dms:eu-west-2:1:task:X"


def _gate_ddb(status: str, baseline_run: str | None) -> KeyedDdb:
    items = {
        "RUN#r1": {
            "status": {"S": status},
            "taskArn": {"S": TASK},
            "project": {"S": "ledger"},
            "taskKey": {"S": "core"},
        }
    }
    if baseline_run == "restore":
        # A restore of a dump taken in r1 itself: the target is r1's starting point, not its result.
        items["TARGET#ledger/core"] = {"runId": {"S": "r1"}, "source": {"S": "restore"}, "fingerprintHash": {"S": "x"}}
    elif baseline_run:
        items["TARGET#ledger/core"] = {"runId": {"S": baseline_run}, "fingerprintHash": {"S": "sha256:x"}}
    return KeyedDdb(items)


@pytest.mark.parametrize(
    "status,baseline_run,code,reason",
    [
        ("SUCCEEDED", "r1", 0, None),
        ("MANUAL", "r1", 2, "status MANUAL"),
        ("FAILED", "r1", 2, "status FAILED"),
        ("SUCCEEDED", "r2", 2, "baseline belongs to run r2"),
        ("SUCCEEDED", None, 2, "no baseline"),
        ("SUCCEEDED", "restore", 2, "restored from a dump"),
    ],
)
def test_gate_passes_only_a_succeeded_run_whose_baseline_is_still_the_latest(status, baseline_run, code, reason):
    got, out = run(["--table", "t", "gate", "r1"], dynamodb=_gate_ddb(status, baseline_run))
    data = json.loads(out)
    assert got == code and data["ok"] is (code == 0)
    if reason:
        assert any(reason in r for r in data["reasons"]), data


def test_gate_needs_the_table():
    with pytest.raises(SystemExit):
        run(["gate", "r1"], dynamodb=KeyedDdb({}))


def test_start_allow_reset_and_notify_on_start_reach_the_request():
    sfn = FakeSfn()
    run(
        [
            "--state-machine-arn",
            SM,
            "start",
            "--project",
            "ledger",
            "--task",
            "core",
            "--allow-reset",
            "--notify-on-start",
        ],
        stepfunctions=sfn,
    )
    req = json.loads(sfn.started[0]["input"])
    assert req["allowReset"] is True and req["notify"] == {"onStart": True}
    from shared import validation

    validation.validate("run_request", req)


# --- dumps and restore -------------------------------------------------------

DUMP_URI = "s3://b/dumps/ledger/core/20261028T103000Z-r1/"
REG = {
    "taskArn": TASK,
    "environment": "dev",
    "connections": {"target": {"engine": "postgresql", "secretArn": "arn:s"}},
    "jobs": {"s3Prefix": "jobs/ledger"},
}


class DumpS3:
    def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
        assert kw["Prefix"] == "dumps/ledger/core/"
        return {"CommonPrefixes": [{"Prefix": "dumps/ledger/core/20261028T103000Z-r1/"}]}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        assert Key == "dumps/ledger/core/20261028T103000Z-r1/manifest.json"
        return {"Body": io.BytesIO(json.dumps({"bytes": 10, "sha256": "ab", "runId": "r1"}).encode())}


class Ssm:
    def get_parameter(self, Name: str) -> dict[str, Any]:  # noqa: N803
        assert Name == "/dms-orch/dev/ledger/core"
        return {"Parameter": {"Value": json.dumps(REG)}}


def test_dumps_lists_the_manifests():
    code, out = run(["--bucket", "b", "dumps", "--project", "ledger", "--task", "core"], s3=DumpS3())
    rows = json.loads(out)
    assert code == 0 and rows == [{"dump": DUMP_URI, "bytes": 10, "sha256": "ab", "runId": "r1"}]


MAPPING = {
    "rules": [
        {
            "rule-type": "selection",
            "object-locator": {"schema-name": "app", "table-name": "%"},
            "rule-action": "include",
        },
        {"rule-type": "transformation", "rule-action": "rename"},
    ]
}


class MappingDms:
    def describe_replication_tasks(self, **kw: Any) -> dict[str, Any]:
        assert kw["Filters"] == [{"Name": "replication-task-arn", "Values": [TASK]}]
        assert not kw.get("WithoutSettings")  # TableMappings is a setting
        return {"ReplicationTasks": [{"ReplicationTaskArn": TASK, "TableMappings": json.dumps(MAPPING)}]}


class RegSsm:
    def __init__(self, reg: dict[str, Any]):
        self.reg = reg

    def get_parameter(self, Name: str) -> dict[str, Any]:  # noqa: N803
        assert Name == "/dms-orch/dev/ledger/core"
        return {"Parameter": {"Value": json.dumps(self.reg)}}


DATA_API_REG = {
    **REG,
    "connections": {
        "target": {"engine": "postgresql", "secretArn": "arn:s", "dataApiClusterArn": "arn:cluster:t"},
        "source": {"engine": "postgresql", "secretArn": "arn:src"},
    },
}


def _restore(
    *extra: str, lock: bool = False, sfn: FakeSfn | None = None, reg: dict[str, Any] | None = None
) -> tuple[int, str, FakeSfn]:
    sfn = sfn or FakeSfn([{"status": "SUCCEEDED"}])
    items = {f"LOCK#{TASK}": {"runId": {"S": "busy"}}} if lock else {}
    argv = ["--state-machine-arn", SM, "--table", "t", "--ssm-prefix", "/dms-orch/dev"]
    argv += ["restore", "--project", "ledger", "--task", "core", "--dump", DUMP_URI, "--poll", "0", *extra]
    fakes = {"stepfunctions": sfn, "ssm": RegSsm(reg or REG), "dynamodb": KeyedDdb(items)}
    code, out = run(argv, dms=MappingDms(), s3=DumpS3(), **fakes)
    return code, out, sfn


def test_restore_refuses_without_confirm_and_while_a_run_holds_the_lock():
    with pytest.raises(SystemExit):
        _restore()
    with pytest.raises(SystemExit):
        _restore("--confirm", lock=True)


def test_restore_runs_pg_restore_through_the_job_runner_as_the_project():
    code, out, sfn = _restore("--confirm")
    assert code == 0
    call = sfn.started[0]
    assert call["stateMachineArn"].endswith(":stateMachine:dms-orch-dev-job-runner")
    body = json.loads(call["input"])
    assert body["job"]["command"] == ["pg-restore-s3", "--connection", "target", "--from", DUMP_URI, "--confirm"]
    assert body["job"]["runtime"] == "fargate" and body["job"]["idempotent"] is False
    assert (
        body["phase"] == "restore"
        and body["ctx"]["project"] == "ledger"
        and body["ctx"]["connections"] == REG["connections"]
    )
    assert call["name"].startswith("restore-ledger-core-")


def test_restore_records_the_target_baseline_marked_as_restore_with_the_dump_run():
    """Seen live: after a restore the next run stopped MANUAL/TargetChanged. The restored target is
    known and intended: a successful restore fingerprints it and records the baseline."""
    code, out, sfn = _restore("--confirm", reg=DATA_API_REG)
    assert code == 0, out
    assert len(sfn.started) == 2
    restore, base = sfn.started
    assert base["stateMachineArn"] == restore["stateMachineArn"]
    assert base["name"] == restore["name"] + "-baseline"
    body = json.loads(base["input"])
    assert body["phase"] == "restore" and body["runId"] == restore["name"]
    assert body["job"]["handler"] == "builtin:checks/target_verify" and body["job"]["runtime"] == "lambda"
    assert body["job"]["with"]["recordBaseline"] == {"runId": "r1", "dumpUri": DUMP_URI}
    assert body["job"]["with"]["connection"] == "target"
    assert body["ctx"]["selectionRules"] == [{"schema": "app", "table": "%", "action": "include"}]
    assert body["ctx"]["connections"] == DATA_API_REG["connections"] and body["dryRun"] is False
    assert '"baseline": {' in out and '"source": "restore"' in out and '"dumpRunId": "r1"' in out


def test_failed_restore_records_no_baseline():
    sfn = FakeSfn([{"status": "FAILED", "error": "JobFailed", "cause": "psql exit 3"}])
    code, _out, sfn = _restore("--confirm", reg=DATA_API_REG, sfn=sfn)
    assert code == 1 and len(sfn.started) == 1


def test_restore_reports_a_baseline_that_could_not_be_recorded():
    sfn = FakeSfn([{"status": "SUCCEEDED"}, {"status": "FAILED", "error": "JobFailed", "cause": "x"}])
    code, out, sfn = _restore("--confirm", reg=DATA_API_REG, sfn=sfn)
    assert code == 1 and len(sfn.started) == 2
    assert "baseline not recorded" in out


def test_restore_without_a_data_api_target_says_no_baseline():
    code, out, sfn = _restore("--confirm")
    assert code == 0 and len(sfn.started) == 1
    assert "no baseline" in out


# --- operator repeats post ---------------------------------------------------


def test_post_starts_a_repeat_of_the_post_machine_from_a_job(monkeypatch):
    sfn = FakeSfn()
    code, out = run(["--state-machine-arn", SM, "post", "r1", "--from-job", "row-counts"], stepfunctions=sfn)
    assert code == 0
    call = sfn.started[0]
    assert call["stateMachineArn"] == POST_SM and call["name"].startswith("r1-post-") and len(call["name"]) <= 80
    assert json.loads(call["input"]) == {
        "runId": "r1",
        "operation": "post",
        "fromJob": "row-counts",
        "startedBy": "operator",
    }


# --- the commands the docs show exist (getting started, RUNBOOK, README) --------------------

DOC_FILES = ["README.md", "RUNBOOK.md", "docs/getting-started.md", "docs/preflight.md", "docs/job-contract.md"]
ROOT = Path(__file__).resolve().parents[2]


def _doc_orch_commands(doc: str) -> list[tuple[str, list[str]]]:
    """orch.py command lines of the fenced blocks (continuations joined) and inline spans."""
    text = (ROOT / doc).read_text(encoding="utf-8")
    out = []
    for block in re.findall(r"^```[a-z]*\n(.*?)^```", text, re.S | re.M):
        for line in re.sub(r"\\\n\s*", " ", block).splitlines():
            if not re.search(r"orch\.py [a-z-]", line):
                continue  # not a command line (a repository map, a comment)
            args = line.split("orch.py ", 1)[1].split(" #", 1)[0]
            args = re.sub(r"<[^>]*>", "x", re.sub(r"\[[^\]]*\]", "", args))
            out.append((line.strip(), shlex.split(args)))
    prose = re.sub(r"```.*?```", "", text, flags=re.S)
    for span in re.findall(r"`([^`\n]+)`", prose):
        if m := re.search(r"orch\.py ([a-z-]+)", span):
            out.append((f"inline {span}", [m.group(1)]))
    return out


@pytest.mark.parametrize("doc", DOC_FILES)
def test_orch_commands_in_the_docs_parse(doc):
    commands = _doc_orch_commands(doc)
    subcommands = set(orch.parser()._subparsers._group_actions[0].choices)
    for line, argv in commands:
        assert argv and argv[0] in subcommands, f"{doc}: unknown orch.py command in {line!r}"
        if line.startswith("inline"):
            continue
        try:
            orch.parser().parse_args(argv)
        except SystemExit as err:
            raise AssertionError(f"{doc}: {line!r} does not parse") from err


def test_docs_show_the_commands_of_a_first_run():
    commands = [argv[0] for _, argv in _doc_orch_commands("docs/getting-started.md")]
    assert {"start", "status", "report", "gate"} <= set(commands)


@pytest.mark.parametrize("doc", DOC_FILES)
def test_sync_jobs_calls_in_the_docs_have_project_version_and_bundle(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    for call in re.findall(r"scripts/sync-jobs\.sh ([^`\n#]*)", text):
        args = [a for a in shlex.split(re.sub(r"<[^>]*>", "x", call)) if not a.startswith("--")]
        assert len(args) >= 3, f"{doc}: sync-jobs.sh {call}"

"""expr (when/templates), validation (vs. jsonschema), store, log masking."""

from __future__ import annotations

import copy

import jsonschema
import pytest
from conftest import EXEC_ARN, TASK_ARN

from shared import expr, log, validation
from shared.store import ACTIVE_MIGRATIONS, Store

SCOPE = {
    "ctx": {"resetTables": ["public.a", "public.b"], "migrationType": "full-load", "dryRun": False},
    "pre": {"schema-diff": {"status": "ok", "summary": {"missingTables": 3}}},
    "failure": {"category": "PERMISSIONS"},
}

# --- expr -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("pre.schema-diff.summary.missingTables > 0", True),
        ("{{ pre.schema-diff.summary.missingTables > 0 }}", True),
        ("pre.schema-diff.summary.missingTables >= 4", False),
        ("failure.category != 'TRANSIENT_SOURCE_CONN'", True),
        ("failure.category in ['PERMISSIONS', 'DATA']", True),
        ("failure.category not in ['PERMISSIONS']", False),
        ('not ctx.dryRun and ctx.migrationType == "full-load"', True),
        ("(ctx.dryRun or false) or pre.missing.summary.x > 1", False),
        ("pre.missing.summary.x == null", True),
        ("ctx.resetTables[1] == 'public.b'", True),
        ("ctx.resetTables[5] == null", True),
        ("'public.a' in ctx.resetTables", True),
        ("1.5 < 2", True),
    ],
)
def test_expr_evaluate(text, expected):
    assert expr.evaluate(text, SCOPE) is expected


def test_expr_empty_when_is_true():
    assert expr.is_true(None, SCOPE) and expr.is_true("  ", SCOPE)


@pytest.mark.parametrize("bad", ["__import__('os')", "ctx.resetTables(", "a ==", "1 +", "ctx.x;"])
def test_expr_rejects_garbage(bad):
    with pytest.raises(expr.ExprError):
        expr.evaluate(bad, SCOPE)


def test_render_keeps_type_for_single_placeholder_and_joins_lists_inline():
    assert expr.render("{{ ctx.resetTables }}", SCOPE) == ["public.a", "public.b"]
    assert expr.render("tables={{ ctx.resetTables }}", SCOPE) == "tables=public.a,public.b"
    assert expr.render({"n": ["x{{ pre.schema-diff.summary.missingTables }}"]}, SCOPE) == {"n": ["x3"]}


def test_render_command_is_always_strings():
    cmd = expr.render_command(["psql-run", "--var", "{{ ctx.resetTables }}", "{{ ctx.dryRun }}"], SCOPE)
    assert cmd == ["psql-run", "--var", "public.a,public.b", "false"]


# --- validation -------------------------------------------------------------------

GOOD_REQUESTS = [
    {"project": "ledger", "taskKey": "core"},
    {
        "project": "ledger",
        "taskKey": "core",
        "operation": "plan",
        "startType": "reload-target",
        "targetReset": {"policy": "always"},
        "autoResume": {"maxAttempts": 2, "onlyCategories": ["TRANSIENT_SOURCE_CONN"]},
        "dryRun": True,
        "jobsVersion": "v1.2.3",
        "requestedBy": "user@example.com",
    },
    {
        "project": "ledger",
        "taskKey": "core",
        "operation": "migrate",
        "completion": "task-stopped",
        "allowReset": True,
        "dump": {"enabled": True, "method": "pg-dump"},
        "assessment": {"enabled": True, "failOnWarning": True},
        "preflight": {"maxIterations": 3, "exactCountMaxRows": 1000000},
        "notify": {"onStart": True},
    },
    {
        "project": "ledger",
        "taskKey": "core",
        "operation": "resume",
        "attempt": 2,
        "parentRunId": "ledger-core-20261028T100000Z-ab12cd",
    },
    {
        "project": "ledger",
        "taskKey": "core",
        "operation": "post",
        "runId": "ledger-core-20261028T100000Z-ab12cd",
        "fromJob": "row-counts",
    },
]
BAD_REQUESTS = [
    {},
    {"project": "Ledger", "taskKey": "core"},
    {"project": "ledger", "taskKey": "core", "operation": "drop-everything"},
    {"project": "ledger", "taskKey": "core", "unknown": 1},
    {"project": "ledger", "taskKey": "core", "dryRun": "yes"},
    {"project": "ledger", "taskKey": "core", "autoResume": {"maxAttempts": 9}},
    {"project": "ledger", "taskKey": "core", "autoResume": {"onlyCategories": ["A", "A"]}},
    {"project": "ledger", "taskKey": "core", "autoResume": {"onlyCategories": ["DMS_RESTART", "EXTERNAL_STOP"]}},
    {"project": "ledger", "taskKey": "core", "jobsVersion": "../../etc"},
    # v4: the first iteration is a full load; waiting for CDC is no longer part of the contract
    {"project": "ledger", "taskKey": "core", "completion": "full-load-complete"},
    {"project": "ledger", "taskKey": "core", "allowReset": "yes"},
    {"project": "ledger", "taskKey": "core", "dump": {"method": "pg_dumpall"}},
    {"project": "ledger", "taskKey": "core", "dump": {"enabled": True, "bucket": "elsewhere"}},
    {"project": "ledger", "taskKey": "core", "assessment": {"enabled": True, "roleArn": "arn:x"}},
    {"project": "ledger", "taskKey": "core", "preflight": {"maxIterations": 0}},
    {"project": "ledger", "taskKey": "core", "preflight": {"maxIterations": 9}},
    {"project": "ledger", "taskKey": "core", "attempt": 0},
    {"project": "ledger", "taskKey": "core", "operation": "post", "runId": "../x"},
    {"project": "ledger", "taskKey": "core", "operation": "post", "fromJob": "Bad Name"},
]


@pytest.mark.parametrize("req", GOOD_REQUESTS + BAD_REQUESTS)
def test_validator_agrees_with_jsonschema_on_run_requests(req):
    schema = validation.load_schema("run_request")
    reference_ok = jsonschema.Draft202012Validator(schema).is_valid(req)
    try:
        validation.validate("run_request", req)
        ours_ok = True
    except validation.ValidationError:
        ours_ok = False
    assert ours_ok == reference_ok
    assert ours_ok == (req in GOOD_REQUESTS)


def test_external_stop_is_refused_as_an_auto_resume_category_with_a_clear_message():
    """s19 (review item 5): someone stopped the task on purpose; it is never retryable, so a
    request that lists it would only look as if it resumed by itself."""
    req = {"project": "ledger", "taskKey": "core", "autoResume": {"onlyCategories": ["EXTERNAL_STOP"]}}
    with pytest.raises(validation.ValidationError) as err:
        validation.validate("run_request", req)
    assert err.value.errors == [
        "$.autoResume.onlyCategories[0]: 'EXTERNAL_STOP' is not allowed: "
        "EXTERNAL_STOP never resumes by itself (someone stopped the task on purpose)"
    ]


def test_with_defaults_fills_nested_defaults_without_mutating_input():
    req = {"project": "ledger", "taskKey": "core", "notify": {"onSuccess": False}}
    snapshot = copy.deepcopy(req)
    out = validation.with_defaults("run_request", req)
    assert req == snapshot
    assert out["operation"] == "migrate" and out["dryRun"] is False
    assert out["notify"] == {"onSuccess": False, "onFailure": True, "onStart": False}
    assert out["allowReset"] is False


def test_run_request_operations_of_two_machines():
    ops = validation.load_schema("run_request")["properties"]["operation"]["enum"]
    assert ops == ["migrate", "resume", "plan", "post"]
    completion = validation.load_schema("run_request")["properties"]["completion"]["enum"]
    assert "full-load-complete" not in completion


def test_schemas_are_valid_draft_2020_12():
    for name in ("run_request", "jobset", "job_result"):
        jsonschema.Draft202012Validator.check_schema(validation.load_schema(name))


# --- store --------------------------------------------------------------------------


def test_lock_release_requires_matching_execution(store, table):
    table.put_item(Item={"pk": f"LOCK#{TASK_ARN}", "sk": "LOCK", "executionArn": EXEC_ARN, "runId": "r"})
    assert store.release_lock_for_execution(TASK_ARN, "arn:other") is False
    assert store.release_lock_for_execution(TASK_ARN, EXEC_ARN) is True
    assert store.get_lock(TASK_ARN) is None


def test_run_meta_and_events(store):
    store.put_run("run-1", {"project": "ledger", "status": "RUNNING", "ratio": 0.5})
    store.update_run("run-1", {"status": "SUCCEEDED", "stats": {"rows": 10}})
    store.append_event("run-1", "sfn", "start", {"x": "y" * 10_000})
    run = store.get_run("run-1")
    assert run["status"] == "SUCCEEDED" and run["ratio"] == 0.5 and run["gsi1pk"] == "PROJECT#ledger"
    events = store.run_items("run-1", "EVENT#")
    assert len(events) == 1 and events[0]["detail"].endswith("<truncated>")


# --- v4 state: MIGRATION# / TARGET# / TABLE# ------------------------------------------


def _arm(store: Store, run_id: str = "run-1") -> None:
    store.arm_migration(
        task_arn=TASK_ARN,
        run_id=run_id,
        execution_arn=EXEC_ARN,
        completion={"mode": "task-stopped", "failOnTableErrors": True},
        attempt=1,
        started_at="2026-10-28T10:00:00Z",
    )


def test_migration_armed_once_per_task(store):
    from botocore.exceptions import ClientError

    _arm(store)
    with pytest.raises(ClientError):
        _arm(store, run_id="run-2")
    item = store.get_migration(TASK_ARN)
    assert item["status"] == "armed" and item["runId"] == "run-1" and item["migrateExecutionArn"] == EXEC_ARN
    assert [m["taskArn"] for m in store.active(ACTIVE_MIGRATIONS)] == [TASK_ARN]


def test_migration_claimed_exactly_once_then_posted(store):
    _arm(store)
    assert store.claim_migration(TASK_ARN, run_id="run-1", by="event-handler") is True
    assert store.claim_migration(TASK_ARN, run_id="run-1", by="watchdog") is False
    claimed = store.get_migration(TASK_ARN)
    assert claimed["status"] == "claimed" and claimed["claimedBy"] == "event-handler" and "claimedAt" in claimed
    # still listed: the watchdog repeats StartExecution for a claim without postExecutionArn
    assert len(store.active(ACTIVE_MIGRATIONS)) == 1

    store.mark_migration_posted(TASK_ARN, run_id="run-1", post_execution_arn="arn:post")
    posted = store.get_migration(TASK_ARN)
    assert posted["status"] == "posted" and posted["postExecutionArn"] == "arn:post"
    assert store.active(ACTIVE_MIGRATIONS) == []


def test_claim_of_another_run_fails(store):
    _arm(store)
    assert store.claim_migration(TASK_ARN, run_id="run-other", by="watchdog") is False
    assert store.claim_migration("arn:aws:dms:eu-west-2:111122223333:task:NONE", run_id="run-1", by="x") is False


def test_migration_delete_requires_matching_run(store):
    _arm(store)
    assert store.delete_migration(TASK_ARN, run_id="run-other") is False
    assert store.delete_migration(TASK_ARN, run_id="run-1") is True
    assert store.get_migration(TASK_ARN) is None
    _arm(store, run_id="run-2")  # the next run may arm again


def test_target_baseline_round_trip(store):
    assert store.get_target_baseline(TASK_ARN) is None
    store.put_target_baseline(
        TASK_ARN,
        run_id="run-1",
        fingerprint_hash="sha256:abc",
        fingerprint_uri="s3://b/runs/run-1/target-fingerprint.json",
        summary={"tables": 3, "rows": 1.5e6},
    )
    base = store.get_target_baseline(TASK_ARN)
    assert base["runId"] == "run-1" and base["fingerprintHash"] == "sha256:abc"
    assert base["summary"] == {"tables": 3, "rows": 1500000}
    store.put_target_baseline(TASK_ARN, run_id="run-2", fingerprint_hash="sha256:def", fingerprint_uri="s3://x")
    assert store.get_target_baseline(TASK_ARN)["runId"] == "run-2"


def test_table_states_replace_previous_copy(store):
    stats = [
        {"SchemaName": "public", "TableName": "a", "TableState": "Table completed", "FullLoadRows": 10},
        {"SchemaName": "public", "TableName": "b", "TableState": "Table error", "FullLoadRows": 0},
    ]
    store.put_table_states(TASK_ARN, run_id="run-1", tables=stats)
    got = {t["table"]: t for t in store.get_table_states(TASK_ARN)}
    assert set(got) == {"public.a", "public.b"}
    assert got["public.b"]["TableState"] == "Table error" and got["public.a"]["runId"] == "run-1"

    store.put_table_states(TASK_ARN, run_id="run-2", tables=stats[:1])
    got = store.get_table_states(TASK_ARN)
    assert [t["table"] for t in got] == ["public.a"] and got[0]["runId"] == "run-2"
    assert store.get_table_states("arn:aws:dms:eu-west-2:111122223333:task:NONE") == []


def test_log_masks_tokens():
    assert log.mask({"taskToken": "secret", "nested": [{"password": "p", "ok": 1}]}) == {
        "taskToken": "***",
        "nested": [{"password": "***", "ok": 1}],
    }

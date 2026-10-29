"""scripts/verify_destroyed.py with moto: what destroy left behind is found."""

from __future__ import annotations

import boto3
import verify_destroyed


def test_known_leftovers_are_found_and_others_ignored(aws, capsys):
    session = boto3.session.Session(region_name="eu-west-2")
    logs = session.client("logs")
    # Seen live: the Container Insights log group of the ECS cluster survived destroy.
    logs.create_log_group(logGroupName="/aws/ecs/containerinsights/dms-orch-sbx/performance")
    logs.create_log_group(logGroupName="/aws/lambda/someone-else")
    session.client("dynamodb").create_table(
        TableName="dms-orch-sbx",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
    )
    code = verify_destroyed.main(["--match", "dms-orch-sbx"], session=session)
    out = capsys.readouterr().out
    assert code == 1
    assert "/aws/ecs/containerinsights/dms-orch-sbx/performance" in out and "dms-orch-sbx" in out
    assert "someone-else" not in out


def test_clean_account_exits_zero(aws, capsys):
    session = boto3.session.Session(region_name="eu-west-2")
    assert verify_destroyed.main(["--match", "dms-orch-sbx"], session=session) == 0
    assert capsys.readouterr().out.strip() == "{}"

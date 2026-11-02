"""Functions and state machines are created after the policies of their roles.

CreateFunction in a VPC checks the role for ec2:CreateNetworkInterface, and CreateStateMachine
checks the role for the managed rule of a .sync state: both fail when terraform creates them
before the inline policy (live sandbox run, one apply from scratch). Only the role ARN is
referenced, so the order has to be stated with depends_on."""

from __future__ import annotations

import re
from pathlib import Path

MODULE = Path(__file__).resolve().parents[2] / "modules" / "dms-orchestration"


def block(path: Path, kind: str, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    found = re.search(rf'^resource "{kind}" "{name}" \{{(.*?)^\}}', text, flags=re.DOTALL | re.MULTILINE)
    assert found, f"{kind}.{name} not in {path.name}"
    return found.group(1)


def depends_on(body: str) -> str:
    found = re.search(r"depends_on\s*=\s*\[(.*?)\]", body, flags=re.DOTALL)
    return found.group(1) if found else ""


def test_lambda_functions_wait_for_every_lambda_role_policy():
    deps = depends_on(block(MODULE / "lambda.tf", "aws_lambda_function", "this"))
    for policy in ("lambda_core", "lambda_waiter", "lambda_jobs"):
        assert f"aws_iam_role_policy.{policy}" in deps, policy


def test_job_runner_waits_for_its_policy():
    deps = depends_on(block(MODULE / "state_machines.tf", "aws_sfn_state_machine", "job_runner"))
    assert "aws_iam_role_policy.job_runner" in deps


def test_run_machines_wait_for_their_policies():
    deps = depends_on(block(MODULE / "state_machines.tf", "aws_sfn_state_machine", "run"))
    for policy in ("migrate", "post"):
        assert f"aws_iam_role_policy.{policy}" in deps, policy

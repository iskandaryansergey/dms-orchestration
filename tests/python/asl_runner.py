"""A small local interpreter for our JSONata state machines (test-only).

Step Functions Local does not support JSONata, and TestState needs AWS
credentials. This runner walks the real ASL graph, evaluates the real JSONata
expressions with ``jsonata-python`` and replaces every Task with a mock, so the
tests can check paths: which states run, what each task receives, that every
terminal path releases the lock, that dry-run never calls StartReplicationTask.

Supported: Pass, Task, Choice, Wait, Map (inline), Succeed, Fail; Assign,
Output, Arguments, Catch (with Assign), Items/ItemSelector, workflow variables.
Retry is not simulated: a mocked error goes straight to Catch (Retry counts are
checked by static tests instead).
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonata
from jsonata.utils import Utils

ASL_DIR = Path(__file__).resolve().parents[2] / "modules" / "dms-orchestration" / "asl"
_EXPR = re.compile(r"^\{%(.*)%\}$", re.S)

TEMPLATE_VALUES: dict[str, Any] = {
    "state_machine_timeout_seconds": 864000,
    "resolve_context_arn": "arn:aws:lambda:eu-west-2:111122223333:function:resolve_context",
    "inspect_task_arn": "arn:aws:lambda:eu-west-2:111122223333:function:inspect_task",
    "classify_failure_arn": "arn:aws:lambda:eu-west-2:111122223333:function:classify_failure",
    "write_report_arn": "arn:aws:lambda:eu-west-2:111122223333:function:write_report",
    "prepare_job_arn": "arn:aws:lambda:eu-west-2:111122223333:function:prepare_job",
    "job_runner_arn": "arn:aws:states:eu-west-2:111122223333:stateMachine:job-runner",
    "migrate_arn": "arn:aws:states:eu-west-2:111122223333:stateMachine:dms-orch-test-migrate",
    "migrate_executions_prefix": "arn:aws:states:eu-west-2:111122223333:execution:dms-orch-test-migrate:",
    "post_arn": "arn:aws:states:eu-west-2:111122223333:stateMachine:dms-orch-test-post",
    "table_name": "dms-orch-test",
    "topic_arn": "arn:aws:sns:eu-west-2:111122223333:dms-orch-test",
    "ecs_cluster_arn": "arn:aws:ecs:eu-west-2:111122223333:cluster/dms-orch-test",
    "fargate_platform_version": "1.4.0",
    "subnets_json": ["subnet-a", "subnet-b"],
    "security_groups_json": ["sg-jobs"],
    "assign_public_ip": "DISABLED",
    "artifacts_bucket": "dms-orch-artifacts-test",
}


def render(name: str, values: dict[str, Any] | None = None) -> dict[str, Any]:
    """Render ``${var}`` placeholders like Terraform's templatefile()."""
    text = (ASL_DIR / name).read_text(encoding="utf-8")
    vals = {**TEMPLATE_VALUES, **(values or {})}

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in vals:
            raise KeyError(f"template variable {key} has no test value")
        value = vals[key]
        return json.dumps(value) if isinstance(value, list) else str(value)

    return json.loads(re.sub(r"\$\{([a-z_]+)\}", sub, text))


def template_variables(name: str) -> set[str]:
    return set(re.findall(r"\$\{([a-z_]+)\}", (ASL_DIR / name).read_text(encoding="utf-8")))


def _parse(value: str) -> Any:
    return json.loads(value)


def compile_expr(text: str) -> jsonata.Jsonata:
    expr = jsonata.Jsonata(text)
    expr.register_lambda("parse", _parse)
    return expr


def iter_expressions(node: Any, path: str = "$"):
    """Yield (path, expression) for every '{% %}' string in a definition."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from iter_expressions(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from iter_expressions(v, f"{path}[{i}]")
    elif isinstance(node, str):
        m = _EXPR.match(node.strip())
        if m:
            yield path, m.group(1).strip()


class TaskError(Exception):
    def __init__(self, error: str, cause: Any = ""):
        super().__init__(f"{error}: {cause}")
        self.error = error
        self.cause = cause if isinstance(cause, str) else json.dumps(cause)


@dataclass
class Run:
    status: str = "RUNNING"
    output: Any = None
    error: str | None = None
    cause: str | None = None
    path: list[str] = field(default_factory=list)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)

    def called(self, state: str) -> list[Any]:
        return [args for name, args in self.calls if name == state]


Mock = Callable[[Any], Any]


class Interpreter:
    def __init__(self, definition: dict[str, Any], mocks: dict[str, Mock], max_steps: int = 500):
        self.definition = definition
        self.mocks = mocks
        self.max_steps = max_steps
        self.run = Run()
        self._token = 0

    # --- expression evaluation ------------------------------------------------
    def evaluate(self, node: Any, variables: dict[str, Any], states: dict[str, Any]) -> Any:
        if isinstance(node, dict):
            return {k: self.evaluate(v, variables, states) for k, v in node.items()}
        if isinstance(node, list):
            return [self.evaluate(v, variables, states) for v in node]
        if isinstance(node, str):
            m = _EXPR.match(node.strip())
            if not m:
                return node
            bindings = {k: Utils.convert_nulls(copy.deepcopy(v)) for k, v in variables.items()}
            bindings["states"] = Utils.convert_nulls(copy.deepcopy(states))
            for k, v in list(bindings.items()):
                if v is None:
                    bindings[k] = Utils.NULL_VALUE
            result = compile_expr(m.group(1).strip()).evaluate(None, bindings)
            return _plain(result)
        return node

    # --- execution -------------------------------------------------------------
    def execute(self, input_: Any, *, context: dict[str, Any] | None = None) -> Run:
        ctx = context or {
            "Execution": {
                "Id": "arn:aws:states:eu-west-2:111122223333:execution:migrate:run-1",
                "Name": "run-1",
                "StartTime": "2026-10-27T10:00:00Z",
            }
        }
        status, output, error, cause = self._machine(self.definition, input_, {}, ctx)
        self.run.status, self.run.output, self.run.error, self.run.cause = status, output, error, cause
        return self.run

    def _machine(
        self, machine: dict[str, Any], input_: Any, variables: dict[str, Any], context: dict[str, Any]
    ) -> tuple[str, Any, str | None, str | None]:
        states = machine["States"]
        name = machine["StartAt"]
        current_input = input_
        for _ in range(self.max_steps):
            state = states[name]
            self.run.path.append(name)
            kind = state["Type"]
            st_ctx = {**context, "State": {"Name": name}}
            sv = {"input": current_input, "context": st_ctx}

            if kind == "Succeed":
                out = self.evaluate(state.get("Output", current_input), variables, sv)
                return "SUCCEEDED", out, None, None
            if kind == "Fail":
                return (
                    "FAILED",
                    None,
                    self.evaluate(state.get("Error"), variables, sv),
                    self.evaluate(state.get("Cause"), variables, sv),
                )
            if kind == "Choice":
                nxt = state.get("Default")
                for rule in state["Choices"]:
                    if self.evaluate(rule["Condition"], variables, sv) is True:
                        self._assign(rule.get("Assign"), variables, sv)
                        nxt = rule["Next"]
                        break
                if nxt is None:
                    raise AssertionError(f"Choice {name}: no rule matched and no Default")
                name = nxt
                continue
            if kind == "Wait":
                if "Seconds" in state:
                    self.evaluate(state["Seconds"], variables, sv)
                self._assign(state.get("Assign"), variables, sv)
                name = state["Next"]
                continue

            if kind in ("Pass", "Task", "Map"):
                try:
                    if kind == "Pass":
                        result = self.evaluate(state.get("Output", current_input), variables, sv)
                    elif kind == "Task":
                        result = self._task(name, state, variables, sv)
                    else:
                        result = self._map(name, state, variables, sv)
                except TaskError as err:
                    catcher = self._catcher(state, err.error)
                    if catcher is None:
                        return "FAILED", None, err.error, err.cause
                    sv_err = {**sv, "errorOutput": {"Error": err.error, "Cause": err.cause}}
                    self._assign(catcher.get("Assign"), variables, sv_err)
                    current_input = self.evaluate(catcher.get("Output", sv["input"]), variables, sv_err)
                    name = catcher["Next"]
                    continue
                sv_res = {**sv, "result": result}
                self._assign(state.get("Assign"), variables, sv_res)
                current_input = (
                    self.evaluate(state["Output"], variables, sv_res)
                    if "Output" in state and kind != "Pass"
                    else result
                )
                if state.get("End"):
                    return "SUCCEEDED", current_input, None, None
                name = state["Next"]
                continue
            raise AssertionError(f"unsupported state type {kind}")
        raise AssertionError("too many steps (loop?)")

    def _assign(self, assign: Any, variables: dict[str, Any], sv: dict[str, Any]) -> None:
        if not assign:
            return
        values = {k: self.evaluate(v, variables, sv) for k, v in assign.items()}
        variables.update(values)

    @staticmethod
    def _catcher(state: dict[str, Any], error: str) -> dict[str, Any] | None:
        for catcher in state.get("Catch", []):
            names = catcher["ErrorEquals"]
            if error in names or "States.ALL" in names:
                return catcher
            if "States.TaskFailed" in names and error != "States.Timeout":
                return catcher
        return None

    def _task(self, name: str, state: dict[str, Any], variables: dict[str, Any], sv: dict[str, Any]) -> Any:
        if state["Resource"].endswith(".waitForTaskToken"):
            self._token += 1
            sv = {**sv, "context": {**sv["context"], "Task": {"Token": f"token-{self._token}"}}}
        args = self.evaluate(state.get("Arguments", {}), variables, sv)
        for key in ("TimeoutSeconds", "HeartbeatSeconds"):
            if key in state:
                value = self.evaluate(state[key], variables, sv)
                assert isinstance(value, int) and value > 0, f"{name}.{key} must be a positive integer, got {value!r}"
        self.run.calls.append((name, args))
        mock = self.mocks.get(name)
        if mock is None:
            raise AssertionError(f"no mock for task state {name}")
        return mock(args)

    def _map(self, name: str, state: dict[str, Any], variables: dict[str, Any], sv: dict[str, Any]) -> Any:
        items = self.evaluate(state["Items"], variables, sv) or []
        assert isinstance(items, list), f"Map {name}: Items must be a list, got {items!r}"
        outputs = []
        for index, item in enumerate(items):
            map_ctx = {**sv["context"], "Map": {"Item": {"Index": index, "Value": item}}}
            item_input = self.evaluate(state.get("ItemSelector", item), variables, {**sv, "context": map_ctx})
            inner_vars = dict(variables)  # inner scope can read outer variables
            status, out, error, cause = self._machine(state["ItemProcessor"], item_input, inner_vars, sv["context"])
            if status != "SUCCEEDED":
                raise TaskError(error or "States.TaskFailed", cause or "")
            outputs.append(out)
        return outputs


def _plain(value: Any) -> Any:
    if value is Utils.NULL_VALUE:
        return None
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _matches(names: list[str], error: str) -> bool:
    if error in names or "States.ALL" in names:
        return True
    return "States.TaskFailed" in names and error not in ("States.Timeout", "States.HeartbeatTimeout")


def local_test_state(
    definition: dict[str, Any],
    state_name: str,
    *,
    input_: Any = None,
    variables: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    mock: dict[str, Any] | None = None,
    retrier_retry_count: int = 0,
) -> dict[str, Any]:
    """Local imitation of the TestState API for one state (same case format as tests/sfn)."""
    state = definition["States"][state_name]
    variables = copy.deepcopy(variables or {})
    ctx = {**(context or {}), "State": {"Name": state_name}}
    sv = {"input": input_ or {}, "context": ctx}
    interp = Interpreter(definition, {})
    kind = state["Type"]
    if kind == "Choice":
        for rule in state["Choices"]:
            if interp.evaluate(rule["Condition"], variables, sv) is True:
                interp._assign(rule.get("Assign"), variables, sv)
                return {"status": "SUCCEEDED", "nextState": rule["Next"], "variables": variables}
        return {"status": "SUCCEEDED", "nextState": state.get("Default"), "variables": variables}
    if kind == "Pass":
        interp._assign(state.get("Assign"), variables, sv)
        return {"status": "SUCCEEDED", "nextState": state.get("Next"), "variables": variables}
    if kind != "Task":
        raise AssertionError(f"local_test_state supports Task, Choice and Pass, not {kind}")
    if state["Resource"].endswith(".waitForTaskToken"):
        sv["context"] = {**sv["context"], "Task": {"Token": "token-1"}}
    interp.evaluate(state.get("Arguments", {}), variables, sv)  # expressions must evaluate
    mock = mock or {}
    if "errorOutput" in mock:
        error, cause = mock["errorOutput"]["error"], mock["errorOutput"].get("cause", "")
        for retrier in state.get("Retry", []):
            if _matches(retrier["ErrorEquals"], error):
                if retrier_retry_count < retrier.get("MaxAttempts", 3):
                    return {"status": "RETRIABLE", "nextState": None, "variables": variables}
                break
        for catcher in state.get("Catch", []):
            if _matches(catcher["ErrorEquals"], error):
                sv_err = {**sv, "errorOutput": {"Error": error, "Cause": cause}}
                interp._assign(catcher.get("Assign"), variables, sv_err)
                return {"status": "CAUGHT_ERROR", "nextState": catcher["Next"], "variables": variables}
        return {"status": "FAILED", "nextState": None, "variables": variables}
    sv_res = {**sv, "result": mock.get("result")}
    interp._assign(state.get("Assign"), variables, sv_res)
    return {"status": "SUCCEEDED", "nextState": state.get("Next"), "variables": variables}

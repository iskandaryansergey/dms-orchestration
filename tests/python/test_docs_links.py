"""The docs stand on their own: relative links resolve, named commands and code exist."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from test_docs_matrix import e2e_scenarios

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def all_docs() -> list[str]:
    """Every hand-written Markdown file of the repository (not the fetched upstream checkout)."""
    found = [*ROOT.glob("*.md"), *ROOT.glob("docs/**/*.md"), *ROOT.glob(".github/**/*.md")]
    found += [p for p in ROOT.glob("examples/**/*.md") if "upstream" not in p.relative_to(ROOT).parts]
    return sorted(p.relative_to(ROOT).as_posix() for p in found)


DOCS = all_docs()


def relative_links(text: str) -> list[str]:
    out = []
    for target in LINK.findall(text):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            continue  # URLs, mailto:, in-page anchors
        out.append(target.split("#", 1)[0])
    return out


def read(doc: str) -> str:
    return (ROOT / doc).read_text(encoding="utf-8")


EXPECTED_DOCS = (
    "README.md",
    "RUNBOOK.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "docs/getting-started.md",
    "docs/troubleshooting.md",
    "docs/testing.md",
    "docs/architecture.md",
    "docs/adr/0001-standard-workflow.md",
    "examples/sandbox/README.md",
)
PLAN_REFERENCES = (
    r"plan v\d",
    r"\bslice \d",
    r"\bdecision \d",
    r"\bruns? \d\b",
    r"plumber",
    r"plan step",
    r"plan final",
)


def test_all_docs_are_found():
    for doc in EXPECTED_DOCS:
        assert doc in DOCS, doc


@pytest.mark.parametrize("doc", DOCS)
def test_relative_links_resolve(doc):
    path = ROOT / doc
    links = relative_links(path.read_text(encoding="utf-8"))
    missing = [t for t in links if t and not (path.parent / t).exists()]
    assert not missing, f"{doc}: broken links {missing}"


@pytest.mark.parametrize("doc", DOCS)
def test_docs_carry_no_internal_plan_references(doc):
    text = read(doc)
    for pattern in PLAN_REFERENCES:
        assert not re.search(pattern, text, re.IGNORECASE), f"{doc}: {pattern}"


def test_agents_md_names_only_existing_make_targets_and_paths():
    agents = read("AGENTS.md")
    targets = set(re.findall(r"^([a-z][a-z0-9-]*):", read("Makefile"), re.MULTILINE))
    named = set(re.findall(r"`make ([a-z][a-z0-9 -]*)`", agents))
    assert named, "AGENTS.md lists no make commands"
    unknown = sorted({t for cmd in named for t in cmd.split()} - targets)
    assert not unknown, f"AGENTS.md names unknown make targets: {unknown}"
    for required in ("lint", "test", "teststate-dry", "denylist", "sandbox-validate", "docs-check"):
        assert required in {t for cmd in named for t in cmd.split()}, required
    for contract in (
        "modules/dms-orchestration/src/shared/schemas/run_request.schema.json",
        "modules/dms-orchestration/src/shared/schemas/jobset.schema.json",
        "modules/dms-orchestration/asl/",
    ):
        assert contract in agents and (ROOT / contract).exists(), contract


def test_ai_doc_covers_the_workflow_and_says_commits_carry_no_trailers():
    text = read("docs/ai-assisted-development.md")
    headings = ("Workflow", "Guardrails", "Briefing an agent", "Artifacts that help agents", "Attribution")
    for heading in headings:
        assert f"## {heading}" in text, heading
    assert "no tool attribution trailers" in text
    assert "AGENTS.md" in text


def test_contributing_links_the_agent_docs_and_explains_the_denylist_on_forks():
    text = read("CONTRIBUTING.md")
    assert "](AGENTS.md)" in text and "](docs/ai-assisted-development.md)" in text
    assert "uv sync" in text and "make lint test" in text and "make denylist" in text
    assert re.search(r"fork", text, re.IGNORECASE) and "DENYLIST_PATTERNS" in text
    assert "verify_destroyed" in text


# --- troubleshooting: every row points at the mechanism in the code -------------------------

REF = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|tf|json|hcl|md|patch))(?::([A-Za-z_][A-Za-z0-9_]*))?`")


def troubleshooting_rows() -> list[list[str]]:
    rows: list[list[str]] = []
    if not (ROOT / "docs/troubleshooting.md").is_file():
        return rows
    for line in read("docs/troubleshooting.md").splitlines():
        if line.startswith("| ") and not line.startswith("| Symptom") and not line.startswith("|---"):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


TROUBLESHOOTING_CASES = (
    "MaxFileSize",
    "TestConnection",
    "eventual consistency",
    "ExecutionAlreadyExists",
    "putObject",
    "transaction_timeout",
    "root",
    "containerinsights",
    "ENI",
    "Invalid for_each argument",
    "RunAlreadyActive",
    "RestoredTarget",
    "DatabaseResumingException",
    "dms.t3.small",
)


def test_troubleshooting_has_the_observed_cases():
    rows = troubleshooting_rows()
    assert len(rows) >= 17
    text = read("docs/troubleshooting.md")
    for needle in TROUBLESHOOTING_CASES:
        assert needle in text, needle
    assert "](dms-observations.md)" in text


@pytest.mark.parametrize("row", troubleshooting_rows(), ids=lambda r: r[0][:40])
def test_troubleshooting_row_names_existing_code(row):
    assert len(row) == 4, row
    refs = REF.findall(row[3])
    assert refs, f"no code reference in: {row[3]}"
    for path, name in refs:
        target = ROOT / path
        assert target.is_file(), path
        if name:
            assert re.search(rf"\b{re.escape(name)}\b", target.read_text(encoding="utf-8")), f"{name} not in {path}"


# --- getting started --------------------------------------------------------------------------


def test_getting_started_follows_the_runbook_order():
    text = read("docs/getting-started.md")
    steps = ["sync-jobs.sh", "--operation plan", "orch.py report", "--dry-run", "--change-ref"]
    positions = [text.index(s) for s in steps]
    assert positions == sorted(positions), steps
    for needle in ("1.9.8", "network_contract", "jobs_security_group_id", "dms = {", "upstream-modules-contract.md"):
        assert needle in text, needle


# --- README, architecture, RUNBOOK, sandbox walkthrough ---------------------------------------


def test_readme_is_the_entry_point_of_release_0_1_0():
    text = read("README.md")
    assert "## Status" not in text
    assert "## Release 0.1.0" in text and "](CHANGELOG.md)" in text
    for target in ("docs/getting-started.md", "docs/architecture.md", "RUNBOOK.md", "docs/troubleshooting.md"):
        assert f"]({target})" in text, target
    sources = re.findall(r'^\s*source\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert sources and all("<org>/dms-orchestration" in s for s in sources), sources


def test_architecture_is_one_page_with_existing_components():
    text = read("docs/architecture.md")
    assert len(text.splitlines()) <= 100
    assert not re.search(r"not built yet|to confirm", text, re.IGNORECASE)
    picture = text.split("## Picture", 1)[1].split("\n## ", 1)[0]
    assert "upstream aurora/dms modules" in picture and "platform SNS topic" in picture
    components = text.split("## Components", 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in components.splitlines() if ln.startswith("| ") and not ln.startswith("| Component")]
    assert len(rows) >= 8
    for row in rows:
        paths = re.findall(r"\]\(([^)]+)\)", row)
        assert paths, f"component without a file: {row}"
        for path in paths:
            assert (ROOT / "docs" / path).exists(), path
    failures = text.split("## Failures", 1)[1].split("\n## ", 1)[0]
    assert "| " not in failures, "the failure tables live in RUNBOOK.md; architecture links them"
    assert "RUNBOOK.md" in failures


RUNBOOK_SECTIONS = ("snapshot", "IAM", "verifyRuntime", "auto-pause", "Repeat post", "fan-out")


def test_runbook_covers_the_operator_topics():
    text = read("RUNBOOK.md")
    headings = [h.lower() for h in re.findall(r"^##+ (.+)$", text, re.MULTILINE)]
    for topic in RUNBOOK_SECTIONS:
        assert any(topic.lower() in h for h in headings), topic
    assert "--profile <profile>" in text
    assert "](docs/troubleshooting.md)" in text


def test_sandbox_walkthrough_names_every_scenario_variable_and_mask():
    text = read("examples/sandbox/README.md")
    scenarios = e2e_scenarios()
    missing = sorted(
        s for s in scenarios if not re.search(rf"^e2e [a-z0-9 -]*(?<![\w-]){re.escape(s)}(?![\w-])", text, re.M)
    )
    assert not missing, f"scenarios without a command in the sandbox README: {missing}"
    variables = re.findall(r'^variable "([a-z_]+)"', read("examples/sandbox/variables.tf"), re.MULTILINE)
    toggles = [
        v
        for v in variables
        if v in ("sql_runner", "ssm_runner", "assessment", "snapshot_dumps", "iam_auth", "sqs_fanout")
    ]
    assert len(toggles) == 6
    for v in [*toggles, "operator_cidr", "sql_runner_image_tag", "runner_ami_id"]:
        assert f"TF_VAR_{v}" in text, v
    name = re.search(r'variable "name" \{[^}]*default\s*=\s*"([^"]+)"', read("examples/sandbox/variables.tf"), re.S)
    assert f"verify_destroyed.py --match {name.group(1)}" in text
    for step in ("fetch-upstream", "build-image", "test-connections", "sync-jobs", "run_teststate.py", "destroy"):
        assert step in text, step


# --- state-machine.md, preflight.md, security.md ---------------------------------------------


def _asl_states() -> set[str]:
    states: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("States"), dict):
                states.update(node["States"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for path in (ROOT / "modules/dms-orchestration/asl").glob("*.asl.json"):
        walk(json.loads(re.sub(r"\$\{[^}]*\}", "0", path.read_text(encoding="utf-8"))))
    return states


def test_state_machine_doc_names_only_real_states():
    text = read("docs/state-machine.md")
    diagrams = "".join(re.findall(r"^```[a-z]*\n(.*?)^```", text, re.S | re.M))
    names = set(re.findall(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]*)+\b", diagrams))
    states = _asl_states()
    assert {"ResolveContext", "TargetVerify", "ArmGuard", "StartResume", "WriteReport"} <= names & states
    module = ROOT / "modules/dms-orchestration"
    sources = [*(module / "asl").glob("*.json"), *(module / "src").rglob("*.py")]
    code = "".join(p.read_text(encoding="utf-8") for p in sources)
    unknown = sorted(n for n in names - states if n not in code)
    assert not unknown, f"docs/state-machine.md names states that do not exist: {unknown}"


@pytest.mark.parametrize("doc", ["docs/state-machine.md", "docs/preflight.md"])
def test_state_machine_and_preflight_docs_are_current(doc):
    text = read(doc)
    assert not re.search(r"to confirm|not built yet|confirmed live \(", text, re.IGNORECASE)
    assert "verifyRuntime" in text


def test_preflight_doc_lists_every_manual_reason_and_verdict():
    text = read("docs/preflight.md")
    preflight = (ROOT / "modules/dms-orchestration/src/shared/preflight.py").read_text(encoding="utf-8")
    # manual("<Reason>", ...) calls, plus the two reasons Verdict("manual", ...) spells out.
    reasons = set(re.findall(r'manual\("([A-Za-z]+)"', preflight)) | {"PreflightTablesMissing", "ResetNotConfigured"}
    assert {"RestoredTarget", "TargetChanged", "TargetNotEmpty", "AlreadyCompleted"} <= reasons
    assert all(r in preflight for r in reasons)
    for verdict in re.findall(r'"([a-z]+)"', preflight.split("VERDICTS = ", 1)[1].split("\n", 1)[0]):
        assert f"| `{verdict}` |" in text, verdict
    missing = sorted(r for r in reasons if f"`{r}`" not in text)
    assert not missing, f"MANUAL reasons not in docs/preflight.md: {missing}"


SECURITY_REF = re.compile(r"`([a-z_]+\.tftest\.hcl|test_[a-z_]+\.py)::([a-z_0-9]+)`")


def security_rows() -> list[list[str]]:
    section = read("docs/security.md").split("## Enforced by tests", 1)[1].split("\n## ", 1)[0]
    return [
        [c.strip() for c in ln.strip().strip("|").split("|")]
        for ln in section.splitlines()
        if ln.startswith("| ") and not ln.startswith("| Control")
    ]


def test_security_doc_maps_every_control_family_to_tests():
    rows = security_rows()
    families = " ".join(r[0] for r in rows).lower()
    for family in ("wildcard", "startreplicationtask", "secrets", "passrole", "data api", "permissions boundary",
                   "snapshot", "rds-db:connect", "egress", "kms", "dumps", "restore baseline", "registration",
                   "container", "token"):  # fmt: skip
        assert family in families, family


@pytest.mark.parametrize(
    "row", security_rows() if (ROOT / "docs/security.md").is_file() else [], ids=lambda r: r[0][:40]
)
def test_security_row_names_existing_tests(row):
    refs = SECURITY_REF.findall(row[1])
    assert refs, f"no test named for: {row[0]}"
    for file, name in refs:
        if file.endswith(".hcl"):
            text = (ROOT / "modules/dms-orchestration/tests" / file).read_text(encoding="utf-8")
            assert f'run "{name}"' in text, f"{file}::{name}"
        else:
            text = (ROOT / "tests/python" / file).read_text(encoding="utf-8")
            assert f"def {name}(" in text, f"{file}::{name}"

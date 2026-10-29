"""docs/testing.md: the e2e matrix lists exactly the scenarios examples/sandbox/scripts/e2e.py runs."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
E2E = ROOT / "examples" / "sandbox" / "scripts" / "e2e.py"
TESTING = ROOT / "docs" / "testing.md"


def e2e_scenarios() -> set[str]:
    """Names main() of e2e.py accepts: the dict literal and the by-name additions."""
    text = E2E.read_text(encoding="utf-8")
    main = text[text.index("def main(") :]
    literal = main[main.index("scenarios = {") : main.index("}")]
    names = set(re.findall(r'"([a-z0-9-]+)":\s*\w+', literal))
    names |= set(re.findall(r'scenarios\["([a-z0-9-]+)"\]\s*=', main))
    return names


def matrix_scenarios() -> list[str]:
    """First cells of the table under '## End-to-end matrix' in docs/testing.md."""
    text = TESTING.read_text(encoding="utf-8")
    section = text.split("## End-to-end matrix", 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("| `")]
    return [re.match(r"\| `([^`]+)`", ln).group(1) for ln in rows]


def test_e2e_parser_finds_the_known_scenarios():
    names = e2e_scenarios()
    assert {"s1", "s1p", "idem-sfn", "s10snap", "s8fargate", "s1iam", "postdie", "s16"} <= names
    assert len(names) >= 20


def test_testing_matrix_matches_the_e2e_scenarios():
    listed = matrix_scenarios()
    assert len(listed) == len(set(listed)), "a scenario is listed twice"
    names = e2e_scenarios()
    assert set(listed) - names == set(), "docs/testing.md lists scenarios e2e.py does not have"
    assert names - set(listed) == set(), "e2e.py has scenarios docs/testing.md does not list"


def test_testing_doc_covers_every_level():
    text = TESTING.read_text(encoding="utf-8")
    for level in ("pytest", "moto", "asl_runner.py", "terraform test", "TestState", "e2e.py", "denylist", "trivy"):
        assert level in text, level
    assert "## Adding a scenario" in text and "## Adding a state machine case" in text

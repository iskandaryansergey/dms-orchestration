"""Release hygiene of 0.1.0: licence, security policy, pull request template, changelog."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOLDER = "dms-orchestration contributors"


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_license_is_mit_with_the_neutral_holder():
    text = read("LICENSE")
    assert text.startswith("MIT License\n")
    assert re.search(rf"^Copyright \(c\) \d{{4}} {re.escape(HOLDER)}$", text, re.MULTILINE)
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in text


def test_pyproject_declares_the_license():
    project = tomllib.loads(read("pyproject.toml"))["project"]
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["version"] == "0.1.0"


def test_security_policy_uses_private_vulnerability_reporting_without_contacts():
    text = read("SECURITY.md")
    assert "private vulnerability reporting" in text.lower()
    assert "/security/advisories/new" in text
    assert not re.search(r"[\w.+-]+@[\w-]+\.\w", text), "no e-mail addresses"
    assert "0.1" in text


def test_pull_request_template_has_the_checklist():
    text = read(".github/PULL_REQUEST_TEMPLATE.md")
    boxes = re.findall(r"^- \[ \] (.+)$", text, re.MULTILINE)
    joined = " ".join(boxes)
    for item in ("red", "make lint test", "make denylist", "docs", "live", "verify_destroyed"):
        assert item in joined, item
    assert "@" not in text and not (ROOT / ".github" / "CODEOWNERS").exists()


def test_changelog_has_release_0_1_0_and_no_unreleased_block():
    text = read("CHANGELOG.md")
    assert "Unreleased" not in text
    headings = re.findall(r"^## (.+)$", text, re.MULTILINE)
    assert headings[0].startswith("[0.1.0] — 20"), headings
    assert len([h for h in headings if h.startswith("[")]) == 1, "one release"
    release = text.split("\n## ", 1)[1]
    for topic in ("snapshot", "verifyRuntime", "IAM database authentication", "idem-sfn", "s1p",
                  "denylist", "fetch-upstream", "configuration.md", "AGENTS.md", "getting-started.md",
                  "troubleshooting.md", "testing.md", "LICENSE", "SECURITY.md"):  # fmt: skip
        assert topic in release, topic


def test_readme_and_contributing_link_the_license_and_the_security_policy():
    for doc in ("README.md", "CONTRIBUTING.md"):
        text = read(doc)
        assert "](SECURITY.md)" in text, doc
    assert "](LICENSE)" in read("README.md")

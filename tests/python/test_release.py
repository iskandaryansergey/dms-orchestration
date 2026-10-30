"""Release hygiene: licence, security policy, pull request template, changelog (0.2.0 on 0.1.0)."""

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
    assert project["version"] == "0.2.0"


def test_security_policy_uses_private_vulnerability_reporting_without_contacts():
    text = read("SECURITY.md")
    assert "private vulnerability reporting" in text.lower()
    assert "/security/advisories/new" in text
    assert not re.search(r"[\w.+-]+@[\w-]+\.\w", text), "no e-mail addresses"
    assert "| 0.2.x | yes |" in text


def test_pull_request_template_has_the_checklist():
    text = read(".github/PULL_REQUEST_TEMPLATE.md")
    boxes = re.findall(r"^- \[ \] (.+)$", text, re.MULTILINE)
    joined = " ".join(boxes)
    for item in ("red", "make lint test", "make denylist", "docs", "live", "verify_destroyed"):
        assert item in joined, item
    assert "@" not in text and not (ROOT / ".github" / "CODEOWNERS").exists()


def releases(text: str) -> dict[str, str]:
    """Release heading -> its body, in file order."""
    parts = re.split(r"^## (\[.+)$", text, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def test_changelog_lists_0_2_0_above_0_1_0_and_no_unreleased_block():
    text = read("CHANGELOG.md")
    assert "Unreleased" not in text
    headings = list(releases(text))
    assert len(headings) == 2, headings
    assert headings[0].startswith("[0.2.0] — 20") and headings[1].startswith("[0.1.0] — 20"), headings
    release = releases(text)[headings[1]]
    for topic in ("snapshot", "verifyRuntime", "IAM database authentication", "idem-sfn", "s1p",
                  "denylist", "fetch-upstream", "configuration.md", "AGENTS.md", "getting-started.md",
                  "troubleshooting.md", "testing.md", "LICENSE", "SECURITY.md"):  # fmt: skip
        assert topic in release, topic


BREAKING_0_2_0 = (
    "ContextNotPinned",  # runs saved by 0.1 are refused by post
    "versioning",  # the artifacts bucket must be versioned
    "manifest without `tables`",  # dumps made by 0.1 are refused by restore
    "partitioned",  # pg-dump refuses partitioned tables in scope
    "dump.method = snapshot",
    "jobs.bundles",
    "allow_unpinned",
    "--force",
    "jobset_publisher_policy_json",
    "failOnValidationErrors",
    "fargate.images",
    "nullable = false",
    "https_egress_vpc_cidr",
    "https_egress_gateway_endpoints",
    "ManagedBy",
    "kms_key_policy_statements",
    "modules/dms-orchestration/src",
)


def test_changelog_0_2_0_has_the_sections_and_every_breaking_change():
    text = read("CHANGELOG.md")
    body = next(b for h, b in releases(text).items() if h.startswith("[0.2.0]"))
    sections = re.findall(r"^### (.+)$", body, re.MULTILINE)
    assert sections == ["Security", "Changed", "Added", "Removed"], sections
    changed = body.split("### Changed", 1)[1].split("\n### ", 1)[0]
    assert changed.count("**Breaking:**") >= 10
    missing = [t for t in BREAKING_0_2_0 if t not in changed]
    assert not missing, f"breaking changes not in [0.2.0] Changed: {missing}"
    security = body.split("### Security", 1)[1].split("\n### ", 1)[0]
    assert "one runner per project" in security, "the SSM residual risk is stated"
    assert "notes/" not in body and "review" not in body.lower(), "no internal references"


def test_readme_and_contributing_link_the_license_and_the_security_policy():
    for doc in ("README.md", "CONTRIBUTING.md"):
        text = read(doc)
        assert "](SECURITY.md)" in text, doc
    assert "](LICENSE)" in read("README.md")

"""docs/configuration.md: the hand-written part covers every field of `projects` and every check.

The inputs/outputs tables between the terraform-docs markers are generated
(`make docs`, checked by `make docs-check` in CI); this test covers what the generator cannot
explain: the nested fields of the `projects` object and the advisory `check` blocks.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "modules" / "dms-orchestration"
DOC = ROOT / "docs" / "configuration.md"
BEGIN, END = "<!-- BEGIN_TF_DOCS -->", "<!-- END_TF_DOCS -->"


def variable_block(text: str, name: str) -> str:
    start = text.index(f'variable "{name}" {{')
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unterminated variable {name}")


def type_fields(block: str) -> set[str]:
    # Up to the nullable argument or the first validation block: field names may contain the
    # word (fail_on_validation_errors).
    end = re.search(r"^\s*(nullable\s*=|validation\s*\{)", block, re.M).start()
    type_expr = block[block.index("type = ") + len("type = ") : end]
    return set(re.findall(r"^\s*([a-z_][a-z0-9_]*)\s*=", type_expr, flags=re.MULTILINE))


def manual_text() -> str:
    doc = DOC.read_text(encoding="utf-8")
    assert doc.count(BEGIN) == 1 and doc.count(END) == 1, "terraform-docs markers missing"
    return doc[: doc.index(BEGIN)] + doc[doc.index(END) + len(END) :]


def test_every_field_of_projects_is_documented_by_hand():
    fields = type_fields(variable_block((MODULE / "variables.tf").read_text(encoding="utf-8"), "projects"))
    assert {"tasks", "connections", "defaults", "target_reset_policy", "snapshot_dumps"} <= fields
    text = manual_text()
    missing = sorted(f for f in fields if f"`{f}`" not in text)
    assert not missing, f"fields of projects not explained in docs/configuration.md: {missing}"


def test_every_check_block_is_listed_for_acknowledged_checks():
    checks = re.findall(r'^check "([a-z_]+)"', (MODULE / "checks.tf").read_text(encoding="utf-8"), re.MULTILINE)
    assert checks
    text = manual_text()
    missing = sorted(c for c in checks if f"`{c}`" not in text)
    assert not missing, f"check blocks not listed in docs/configuration.md: {missing}"


def test_the_generated_section_lists_every_variable_and_output():
    doc = DOC.read_text(encoding="utf-8")
    generated = doc[doc.index(BEGIN) : doc.index(END)]
    for tf, kind, anchor in (("variables.tf", "variable", "input"), ("outputs.tf", "output", "output")):
        names = re.findall(rf'^{kind} "([a-z_]+)"', (MODULE / tf).read_text(encoding="utf-8"), re.MULTILINE)
        assert names
        missing = [n for n in names if f'<a name="{anchor}_{n}">' not in generated]
        assert not missing, f"{kind}s missing from the generated tables (run make docs): {missing}"


def test_terraform_docs_config_injects_into_the_doc():
    cfg = (ROOT / ".terraform-docs.yml").read_text(encoding="utf-8")
    assert re.search(r"^formatter:\s*\"?markdown table\"?", cfg, re.MULTILINE)
    assert re.search(r"^\s+file:\s*\"?\.\./\.\./docs/configuration\.md\"?", cfg, re.MULTILINE)
    assert re.search(r"^\s+mode:\s*inject", cfg, re.MULTILINE)

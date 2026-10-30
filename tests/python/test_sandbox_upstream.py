"""examples/sandbox/scripts/sandbox.py fetch-upstream, against a local bare "upstream" repository."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "examples" / "sandbox"
SCRIPT = SANDBOX / "scripts" / "sandbox.py"
spec = importlib.util.spec_from_file_location("sandbox_upstream", SCRIPT)
sandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sandbox)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Neutral",
    "GIT_AUTHOR_EMAIL": "neutral@example.invalid",
    "GIT_COMMITTER_NAME": "Neutral",
    "GIT_COMMITTER_EMAIL": "neutral@example.invalid",
}


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def upstream(tmp_path: Path) -> dict[str, str]:
    """A bare repository with modules/aurora, modules/dms and an unrelated module; two commits."""
    work = tmp_path / "upstream-work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    write(work / "modules" / "aurora" / "main.tf", "# aurora v1\n")
    write(work / "modules" / "dms" / "main.tf", "# dms v1\n")
    write(work / "modules" / "unrelated" / "main.tf", "# not needed\n")
    write(work / "README.md", "upstream\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "v1")
    first = git(work, "rev-parse", "HEAD")
    write(work / "modules" / "dms" / "main.tf", "# dms v2\n")
    git(work, "commit", "-q", "-am", "v2")
    second = git(work, "rev-parse", "HEAD")
    bare = tmp_path / "upstream.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return {"url": bare.as_posix(), "first": first, "second": second}


def test_sparse_checkout_of_the_two_modules_at_the_pinned_commit(upstream, tmp_path):
    dest = tmp_path / "sbx" / "upstream"
    sha = sandbox.fetch_upstream(upstream["url"], upstream["first"], dest)
    assert sha == upstream["first"]
    assert (dest / "modules" / "aurora" / "main.tf").read_text(encoding="utf-8") == "# aurora v1\n"
    assert (dest / "modules" / "dms" / "main.tf").read_text(encoding="utf-8") == "# dms v1\n"
    assert not (dest / "modules" / "unrelated").exists()
    assert git(dest, "rev-parse", "HEAD") == upstream["first"]
    assert (dest / ".ref").read_text(encoding="utf-8").strip() == upstream["first"]


def test_refetch_moves_to_another_ref(upstream, tmp_path):
    dest = tmp_path / "upstream"
    sandbox.fetch_upstream(upstream["url"], upstream["first"], dest)
    sandbox.fetch_upstream(upstream["url"], upstream["second"], dest)
    assert (dest / "modules" / "dms" / "main.tf").read_text(encoding="utf-8") == "# dms v2\n"
    assert (dest / ".ref").read_text(encoding="utf-8").strip() == upstream["second"]


def test_patches_are_applied_in_order(upstream, tmp_path):
    patches = tmp_path / "upstream-patches"
    write(
        patches / "0001-dms.patch",
        "--- a/modules/dms/main.tf\n+++ b/modules/dms/main.tf\n@@ -1 +1 @@\n-# dms v1\n+# dms v1 patched\n",
    )
    dest = tmp_path / "upstream"
    sandbox.fetch_upstream(upstream["url"], upstream["first"], dest, patches_dir=patches)
    assert (dest / "modules" / "dms" / "main.tf").read_text(encoding="utf-8") == "# dms v1 patched\n"
    assert "0001-dms.patch" in (dest / ".ref").read_text(encoding="utf-8")


def test_a_patch_that_does_not_apply_fails(upstream, tmp_path):
    patches = tmp_path / "upstream-patches"
    write(patches / "0001.patch", "--- a/modules/dms/main.tf\n+++ b/modules/dms/main.tf\n@@ -1 +1 @@\n-nope\n+x\n")
    with pytest.raises(sandbox.UpstreamError, match="0001.patch"):
        sandbox.fetch_upstream(upstream["url"], upstream["first"], tmp_path / "upstream", patches_dir=patches)


def test_refuses_without_url(tmp_path):
    with pytest.raises(sandbox.UpstreamError, match="UPSTREAM_MODULES_GIT is not set"):
        sandbox.fetch_upstream("", "a" * 40, tmp_path / "upstream")


@pytest.mark.parametrize("ref", ["main", "v1.2.0", "abc1234", "A" * 40, "", "g" * 40])
def test_refuses_a_ref_that_is_not_a_full_commit_sha(ref, tmp_path):
    with pytest.raises(sandbox.UpstreamError, match="40-character commit sha"):
        sandbox.fetch_upstream("https://example.invalid/x.git", ref, tmp_path / "upstream")


def test_refuses_an_option_like_url(tmp_path):
    with pytest.raises(sandbox.UpstreamError, match="not a repository URL"):
        sandbox.fetch_upstream("--upload-pack=touch x", "a" * 40, tmp_path / "upstream")


def test_unknown_commit_fails_without_printing_the_url(upstream, tmp_path):
    with pytest.raises(sandbox.UpstreamError) as err:
        sandbox.fetch_upstream(upstream["url"], "0" * 40, tmp_path / "upstream")
    assert upstream["url"] not in str(err.value)


def test_config_from_env_wins_over_the_env_file(tmp_path):
    env_file = tmp_path / "upstream.env"
    env_file.write_text(
        "# comment\nUPSTREAM_MODULES_GIT=https://file.invalid/m.git\nUPSTREAM_MODULES_REF='" + "b" * 40 + "'\n",
        encoding="utf-8",
    )
    assert sandbox.upstream_config({}, env_file) == ("https://file.invalid/m.git", "b" * 40)
    env = {"UPSTREAM_MODULES_GIT": "https://env.invalid/m.git"}
    assert sandbox.upstream_config(env, env_file) == ("https://env.invalid/m.git", "b" * 40)
    assert sandbox.upstream_config({}, tmp_path / "missing.env") == ("", "")


def test_cli_fetch_upstream_needs_no_terraform_outputs(upstream, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "outputs", lambda: pytest.fail("fetch-upstream must not read terraform outputs"))
    monkeypatch.setattr(sandbox, "HERE", tmp_path)
    monkeypatch.setenv("UPSTREAM_MODULES_GIT", upstream["url"])
    monkeypatch.setenv("UPSTREAM_MODULES_REF", upstream["second"])
    assert sandbox.main(["fetch-upstream", "--ref", upstream["first"]]) == 0
    assert (tmp_path / "upstream" / ".ref").read_text(encoding="utf-8").startswith(upstream["first"])
    assert upstream["url"] not in capsys.readouterr().out
    monkeypatch.delenv("UPSTREAM_MODULES_GIT")
    assert sandbox.main(["fetch-upstream"]) == 2
    assert "UPSTREAM_MODULES_GIT is not set" in capsys.readouterr().err


def test_sandbox_sources_the_upstream_modules_from_the_fetched_directory():
    tf = (SANDBOX / "main.tf").read_text(encoding="utf-8")
    sources = re.findall(r'^\s*source\s*=\s*"([^"]+)"', tf, flags=re.MULTILINE)
    assert "./upstream/modules/aurora" in sources and "./upstream/modules/dms" in sources
    for path in SANDBOX.rglob("*.tf"):
        assert "git::" not in path.read_text(encoding="utf-8"), path


def test_endpoint_keys_do_not_read_the_orchestrator_module():
    # module.dms depends_on the endpoints, and the orchestrator reads module.dms outputs
    # (var.dms, and var.projects validates against it): keys taken from network_contract
    # close a cycle that terraform validate refuses. A check block compares them instead.
    tf = (SANDBOX / "main.tf").read_text(encoding="utf-8")
    block = re.search(r'resource "aws_vpc_endpoint" "interface" \{(.*?)\n\}', tf, flags=re.DOTALL)
    assert block, "aws_vpc_endpoint.interface not found"
    assert "module.dms_orchestration" not in block.group(1)
    check = re.search(r'check "endpoints_cover_the_network_contract" \{(.*?)\n\}', tf, flags=re.DOTALL)
    assert check and "network_contract.required_vpc_endpoints" in check.group(1)


def test_sandbox_task_does_not_fail_on_the_table_dms_cannot_validate():
    # app.audit_notes has no primary key (tests/e2e/seed): DMS reports "No primary Key" and, with
    # the module default fail_on_validation_errors = true, every load ends FAILED
    # DmsValidationErrors (live sandbox run). The scenarios opt out; the default stays the module's.
    seed = (ROOT / "tests" / "e2e" / "seed" / "010_source_schema.sql").read_text(encoding="utf-8")
    notes = re.search(r"CREATE TABLE IF NOT EXISTS app\.audit_notes \((.*?)\);", seed, flags=re.DOTALL)
    assert notes and "PRIMARY KEY" not in notes.group(1).upper()
    tf = (SANDBOX / "main.tf").read_text(encoding="utf-8")
    assert re.search(r"defaults\s*=\s*\{[^}]*fail_on_validation_errors\s*=\s*false", tf)


def test_sandbox_sql_lambda_reaches_s3_through_the_gateway_endpoint():
    # truncate reads spec.json from S3; the gateway endpoint's addresses are the S3 prefix list,
    # outside the VPC CIDR: without this egress the call hangs until the Lambda timeout (live run).
    tf = (SANDBOX / "main.tf").read_text(encoding="utf-8")
    rules = re.findall(r'resource "aws_vpc_security_group_egress_rule" "[^"]+" \{(.*?)\n\}', tf, flags=re.DOTALL)
    assert any(
        "aws_security_group.sql_lambda.id" in r and 'aws_vpc_endpoint.gateway["s3"].prefix_list_id' in r for r in rules
    )


def test_local_lint_does_not_reach_into_the_fetched_upstream_checkout():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'extend-exclude\s*=\s*\[[^\]]*"examples/sandbox/upstream"', pyproject)
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    fmt_lines = [ln.strip() for ln in makefile.splitlines() if "$(TF) fmt" in ln]
    assert fmt_lines, makefile
    for line in fmt_lines:  # -recursive from the root would descend into examples/sandbox/upstream
        assert "-recursive modules" in line or ("examples/sandbox" in line and "-recursive" not in line), line


def test_upstream_checkout_and_local_env_are_ignored_and_the_example_is_a_placeholder():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "examples/sandbox/upstream/" in ignored and "examples/sandbox/upstream.env" in ignored
    example = (SANDBOX / "upstream.env.example").read_text(encoding="utf-8")
    assert "UPSTREAM_MODULES_GIT=https://example.com/<org>/<modules-repo>.git" in example
    assert "UPSTREAM_MODULES_REF=<commit-sha>" in example


MIN_UPSTREAM = "08def61"


def test_the_example_states_the_minimum_upstream_commit():
    example = (SANDBOX / "upstream.env.example").read_text(encoding="utf-8")
    assert f"at or after {MIN_UPSTREAM}" in example


def test_the_contract_marks_2_3_and_2_6_fixed_upstream():
    text = (ROOT / "docs" / "upstream-modules-contract.md").read_text(encoding="utf-8")
    for section in ("### 2.3", "### 2.6"):
        body = re.split(r"\n#{2,3} ", text[text.index(section) :])[0]
        flat = re.sub(r"\s+", " ", body)
        assert f"Fixed upstream from {MIN_UPSTREAM}" in flat, section
        assert "the orchestrator no longer works around it" in flat, section
    assert "sbx test-connections` sets 32768" not in text


def test_troubleshooting_names_no_sandbox_workaround():
    text = (ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
    assert "fix_target_max_file_size" not in text
    assert MIN_UPSTREAM in text


def test_the_targeted_apply_and_destroy_are_marked_for_removal():
    readme = (SANDBOX / "README.md").read_text(encoding="utf-8")
    assert "MaxFileSize" not in readme
    lines = readme.splitlines()
    targeted = [i for i, ln in enumerate(lines) if re.search(r"terraform (apply|destroy).*-target", ln)]
    assert len(targeted) == 2, targeted
    for i in targeted:
        near = " ".join(lines[max(0, i - 6) : i + 1])
        assert "removed after the next sandbox run" in near, lines[i]


def test_upstream_patches_stay_local():
    # Patches carry fragments of third-party module code: they are never shipped.
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "examples/sandbox/upstream-patches/*.patch" in ignored

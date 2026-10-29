"""scripts/check_denylist.py: the denylist gate, on throwaway git repositories.

Only synthetic patterns here: the real list lives outside the repository (CI secret, local file).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_denylist.py"

# Synthetic terms that never occur in this repository's own files.
PATTERNS = ["zorblax", r"\bquux\b", "frobnic8"]


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    base = {
        "GIT_AUTHOR_NAME": "Neutral",
        "GIT_AUTHOR_EMAIL": "neutral@example.invalid",
        "GIT_COMMITTER_NAME": "Neutral",
        "GIT_COMMITTER_EMAIL": "neutral@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    full = {**os.environ, **base, **(env or {})}
    return subprocess.run(["git", *args], cwd=repo, env=full, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "README.md").write_text("clean text\n", encoding="utf-8")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "initial")
    return r


@pytest.fixture
def patterns_file(tmp_path: Path) -> Path:
    p = tmp_path / "outside" / "denylist"
    p.parent.mkdir()
    p.write_text("# synthetic\n" + "\n".join(PATTERNS) + "\n\n", encoding="utf-8")
    return p


def run(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full = {k: v for k, v in os.environ.items() if k != "DENYLIST_PATTERNS"}
    full["HOME"] = str(repo.parent / "home")  # no ~/.config fallback unless a test creates one
    full["USERPROFILE"] = full["HOME"]
    full["XDG_CONFIG_HOME"] = str(repo.parent / "home" / ".config")
    full.update(env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=repo, env=full, capture_output=True, text=True, check=False
    )


def commit_file(repo: Path, name: str, text: str, message: str = "add", env: dict[str, str] | None = None) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message, env=env)


def test_not_configured_is_exit_2(repo: Path):
    res = run(repo)
    assert res.returncode == 2
    assert "denylist not configured" in res.stderr


def test_clean_tree_is_exit_0(repo: Path, patterns_file: Path):
    res = run(repo, "--patterns-file", str(patterns_file))
    assert res.returncode == 0, res.stdout + res.stderr


def test_hit_in_content_reports_path_line_and_pattern_number(repo: Path, patterns_file: Path):
    commit_file(repo, "docs/a.md", "line one\nsee the Zorblax module\n")
    res = run(repo, "--patterns-file", str(patterns_file))
    assert res.returncode == 1
    assert "docs/a.md:2 pattern#1" in res.stdout


def test_match_is_case_insensitive_and_word_bounded(repo: Path, patterns_file: Path):
    commit_file(repo, "b.txt", "QUUX here\nquuxes are fine\n")
    res = run(repo, "--patterns-file", str(patterns_file))
    assert res.returncode == 1
    assert "b.txt:1 pattern#2" in res.stdout
    assert "b.txt:2" not in res.stdout


def test_hit_in_file_path_does_not_print_the_path(repo: Path, patterns_file: Path):
    commit_file(repo, "docs/zorblax-contract.md", "clean\n")
    res = run(repo, "--patterns-file", str(patterns_file))
    assert res.returncode == 1
    assert "pattern#1" in res.stdout
    assert "path" in res.stdout
    assert "zorblax" not in (res.stdout + res.stderr).lower()


def test_output_never_contains_the_matched_text(repo: Path, patterns_file: Path):
    commit_file(repo, "c.md", "a frobnic8 b\n", message="mention zorblax")
    res = run(repo, "--patterns-file", str(patterns_file), "--history")
    assert res.returncode == 1
    out = (res.stdout + res.stderr).lower()
    for term in ("frobnic8", "zorblax"):
        assert term not in out


def test_binary_files_are_skipped(repo: Path, patterns_file: Path):
    (repo / "blob.bin").write_bytes(b"\x00\x01zorblax\x00")
    (repo / ".gitattributes").write_text("*.dat binary\n", encoding="utf-8")
    (repo / "data.dat").write_bytes(b"zorblax but marked binary")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "binaries")
    res = run(repo, "--patterns-file", str(patterns_file))
    assert res.returncode == 0, res.stdout


def test_history_finds_commit_message(repo: Path, patterns_file: Path):
    commit_file(repo, "d.md", "clean\n", message="subject\n\nbody talks about Zorblax")
    assert run(repo, "--patterns-file", str(patterns_file)).returncode == 0
    res = run(repo, "--patterns-file", str(patterns_file), "--history")
    assert res.returncode == 1
    sha = git(repo, "rev-parse", "--short=12", "HEAD").strip()
    assert f"commit {sha} message:3 pattern#1" in res.stdout


def test_history_finds_author_email(repo: Path, patterns_file: Path):
    commit_file(repo, "e.md", "clean\n", env={"GIT_AUTHOR_EMAIL": "dev@frobnic8.example"})
    res = run(repo, "--patterns-file", str(patterns_file), "--history")
    assert res.returncode == 1
    assert "author-email pattern#3" in res.stdout


def test_history_all_covers_other_branches(repo: Path, patterns_file: Path):
    git(repo, "checkout", "-q", "-b", "side")
    commit_file(repo, "f.md", "clean\n", message="quux on a side branch")
    git(repo, "checkout", "-q", "main")
    assert run(repo, "--patterns-file", str(patterns_file), "--history").returncode == 0
    assert run(repo, "--patterns-file", str(patterns_file), "--history", "--all").returncode == 1


def test_session_trailer_is_always_denied(repo: Path, patterns_file: Path):
    commit_file(repo, "g.md", "clean\n", message="subject\n\nSomeTool-Session: https://example.invalid/s/1")
    res = run(repo, "--patterns-file", str(patterns_file), "--history")
    assert res.returncode == 1
    assert "message:3 builtin#1" in res.stdout


def test_co_author_trailer_is_always_denied(repo: Path, patterns_file: Path, tmp_path: Path):
    commit_file(repo, "g2.md", "clean\n", message="subject\n\nco-authored-by: Tool <tool@example.invalid>")
    res = run(repo, "--patterns-file", str(patterns_file), "--history")
    assert res.returncode == 1
    assert "message:3 builtin#2" in res.stdout
    msg = tmp_path / "MSG2"
    msg.write_text("subject\n\nCo-Authored-By: Tool <tool@example.invalid>\n", encoding="utf-8")
    assert "commit-msg:3 builtin#2" in run(repo, "--patterns-file", str(patterns_file), "--commit-msg", str(msg)).stdout


def test_trailer_patterns_apply_to_commit_messages_only(repo: Path, patterns_file: Path):
    commit_file(repo, "docs/commits.md", "Co-Authored-By: is not used here\nTool-Session: neither\n")
    res = run(repo, "--patterns-file", str(patterns_file))
    assert res.returncode == 0, res.stdout


def test_staged_mode_sees_the_index_not_the_worktree(repo: Path, patterns_file: Path):
    (repo / "h.md").write_text("zorblax\n", encoding="utf-8")
    git(repo, "add", "h.md")
    (repo / "h.md").write_text("clean\n", encoding="utf-8")
    (repo / "README.md").write_text("zorblax unstaged\n", encoding="utf-8")
    res = run(repo, "--patterns-file", str(patterns_file), "--staged")
    assert res.returncode == 1
    assert "h.md:1 pattern#1" in res.stdout
    assert "README.md" not in res.stdout


def test_commit_msg_mode(repo: Path, patterns_file: Path, tmp_path: Path):
    msg = tmp_path / "MSG"
    msg.write_text("fix\n\nQuux was here\n", encoding="utf-8")
    res = run(repo, "--patterns-file", str(patterns_file), "--commit-msg", str(msg))
    assert res.returncode == 1
    assert "commit-msg:3 pattern#2" in res.stdout


def test_patterns_from_env(repo: Path):
    commit_file(repo, "i.md", "frobnic8\n")
    res = run(repo, env={"DENYLIST_PATTERNS": "\n".join(PATTERNS)})
    assert res.returncode == 1
    assert "i.md:1 pattern#3" in res.stdout


def test_patterns_from_user_config_file(repo: Path):
    cfg = repo.parent / "home" / ".config" / "dms-orchestration" / "denylist"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("zorblax\n", encoding="utf-8")
    commit_file(repo, "j.md", "zorblax\n")
    res = run(repo)
    assert res.returncode == 1
    assert "j.md:1 pattern#1" in res.stdout


def test_patterns_file_inside_the_repository_is_refused(repo: Path):
    inside = repo / "denylist.txt"
    inside.write_text("zorblax\n", encoding="utf-8")
    res = run(repo, "--patterns-file", str(inside))
    assert res.returncode == 2
    assert "outside the repository" in res.stderr


def test_invalid_regex_is_exit_2_without_echoing_it(repo: Path, tmp_path: Path):
    bad = tmp_path / "bad"
    bad.write_text("zorblax\n(unclosed\n", encoding="utf-8")
    res = run(repo, "--patterns-file", str(bad))
    assert res.returncode == 2
    assert "pattern#2" in res.stderr
    assert "unclosed" not in res.stderr

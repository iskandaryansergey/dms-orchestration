#!/usr/bin/env python3
"""Denylist gate: fail when a denied term appears in files, paths, commit messages or authors.

    python scripts/check_denylist.py [--patterns-file PATH] [--staged | --commit-msg FILE | --history [--all]]

The list itself never lives in the repository. Patterns (one case-insensitive regex per line,
``#`` comments and blank lines ignored) come from, in order: ``--patterns-file`` (must be outside
the repository), the ``DENYLIST_PATTERNS`` environment variable (CI secret), or
``$XDG_CONFIG_HOME/dms-orchestration/denylist`` (default ``~/.config/...``).

Modes:
  (default)     tracked files: contents and paths (binary files are skipped);
  --staged      the index only, for the pre-commit hook;
  --commit-msg  one commit message file, for the commit-msg hook;
  --history     tracked files plus every commit reachable from HEAD (``--all``: from every ref):
                author and committer name and email, subject and body.

Built-in patterns always apply to commit messages (``--history``, ``--commit-msg``): commits
carry no tool attribution trailers, so a line starting with ``<Tool>-Session:`` (builtin#1,
an identifying session URL) or ``Co-Authored-By:`` (builtin#2) is a hit.

Output is ``<where> pattern#N`` only, never the matched text, so CI logs do not leak the list.
A path that itself matches is replaced by ``<path #i>`` (its index in ``git ls-files``).
Exit 0 clean, 1 hits, 2 not configured or invalid configuration.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

ENV = "DENYLIST_PATTERNS"
# Commit message trailers only; files may mention them in prose.
BUILTIN = [
    re.compile(r"^[A-Za-z][\w.]*-Session:", re.IGNORECASE),
    re.compile(r"^Co-Authored-By:", re.IGNORECASE),
]
SNIFF = 8192


class ConfigError(Exception):
    pass


def _git(*args: str, cwd: Path) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def repo_root(cwd: Path) -> Path:
    return Path(_git("rev-parse", "--show-toplevel", cwd=cwd).decode().strip()).resolve()


def _user_config() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "dms-orchestration" / "denylist"


def load_patterns(patterns_file: str | None, root: Path) -> list[re.Pattern[str]]:
    if patterns_file:
        path = Path(patterns_file).resolve()
        if path == root or root in path.parents:
            raise ConfigError("the patterns file must live outside the repository")
        text = path.read_text(encoding="utf-8")
    elif os.environ.get(ENV, "").strip():
        text = os.environ[ENV]
    elif _user_config().is_file():
        text = _user_config().read_text(encoding="utf-8")
    else:
        raise ConfigError(f"denylist not configured: set {ENV}, pass --patterns-file or create {_user_config()}")
    out: list[re.Pattern[str]] = []
    lines = [ln.strip() for ln in text.splitlines()]
    for n, line in enumerate((ln for ln in lines if ln and not ln.startswith("#")), start=1):
        try:
            out.append(re.compile(line, re.IGNORECASE))
        except re.error as err:
            raise ConfigError(f"pattern#{n} is not a valid regex ({err.msg})") from None
    if not out:
        raise ConfigError("denylist not configured: the pattern list is empty")
    return out


class Scanner:
    def __init__(self, patterns: list[re.Pattern[str]]):
        self.patterns = patterns
        self.hits: list[str] = []

    def _match(self, text: str, message: bool = False) -> Iterator[str]:
        for i, p in enumerate(self.patterns, start=1):
            if p.search(text):
                yield f"pattern#{i}"
        for i, p in enumerate(BUILTIN if message else [], start=1):
            if p.search(text):
                yield f"builtin#{i}"

    def text(self, where: str, text: str, message: bool = False) -> None:
        for n, line in enumerate(text.splitlines(), start=1):
            for tag in self._match(line, message):
                self.hits.append(f"{where}:{n} {tag}")

    def field(self, where: str, value: str) -> None:
        for tag in self._match(value):
            self.hits.append(f"{where} {tag}")

    def path_label(self, path: str, index: int) -> str:
        """Report a path, unless the path itself is a hit (then report it by index)."""
        tags = list(self._match(path))
        if not tags:
            return path
        label = f"<path #{index}>"
        self.hits.extend(f"{label}:path {tag}" for tag in tags)
        return label


def _binary_attr(paths: list[str], root: Path) -> set[str]:
    if not paths:
        return set()
    out = subprocess.run(
        ["git", "check-attr", "-z", "--stdin", "binary"],
        cwd=root,
        input="\0".join(paths).encode(),
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    # Records come as path, attribute, value triples.
    return {out[i].decode() for i in range(0, len(out) - 2, 3) if out[i + 2] == b"set"}


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:SNIFF]


def scan_blobs(scanner: Scanner, root: Path, paths: list[str], read: Callable[[str], bytes | None]) -> None:
    binary = _binary_attr(paths, root)
    for index, path in enumerate(paths, start=1):
        label = scanner.path_label(path, index)
        if path in binary:
            continue
        data = read(path)
        if data is None or _is_binary(data):
            continue
        scanner.text(label, data.decode("utf-8", errors="replace"))


def _split_z(raw: bytes) -> list[str]:
    return [p.decode() for p in raw.split(b"\0") if p]


def scan_tracked(scanner: Scanner, root: Path) -> None:
    paths = _split_z(_git("ls-files", "-z", cwd=root))

    def read(path: str) -> bytes | None:
        try:
            return (root / path).read_bytes()
        except OSError:  # deleted in the worktree, a submodule, a broken link
            return None

    scan_blobs(scanner, root, paths, read)


def scan_staged(scanner: Scanner, root: Path) -> None:
    paths = _split_z(_git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR", cwd=root))

    def read(path: str) -> bytes | None:
        res = subprocess.run(["git", "show", f":{path}"], cwd=root, capture_output=True, check=False)
        return res.stdout if res.returncode == 0 else None

    scan_blobs(scanner, root, paths, read)


FIELDS = ("author-name", "author-email", "committer-name", "committer-email")


def _commits(root: Path, all_refs: bool) -> Iterable[list[str]]:
    fmt = "%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x1e"
    args = ["log", f"--format={fmt}"] + (["--all"] if all_refs else [])
    raw = _git(*args, cwd=root).decode("utf-8", errors="replace")
    for record in raw.split("\x1e"):
        record = record.lstrip("\n")
        if record:
            yield record.split("\0", 5)


def scan_history(scanner: Scanner, root: Path, all_refs: bool) -> None:
    for sha, *values, message in _commits(root, all_refs):
        where = f"commit {sha[:12]}"
        for name, value in zip(FIELDS, values, strict=True):
            scanner.field(f"{where} {name}", value)
        scanner.text(f"{where} message", message, message=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--patterns-file", help="pattern list outside the repository")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="scan the index (pre-commit)")
    mode.add_argument("--commit-msg", metavar="FILE", help="scan one commit message file (commit-msg hook)")
    mode.add_argument("--history", action="store_true", help="tracked files plus commit metadata")
    ap.add_argument("--all", action="store_true", help="with --history: commits from every ref")
    args = ap.parse_args(argv)

    try:
        root = repo_root(Path.cwd())
        scanner = Scanner(load_patterns(args.patterns_file, root))
    except ConfigError as err:
        print(f"check_denylist: {err}", file=sys.stderr)
        return 2
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"check_denylist: {type(err).__name__}", file=sys.stderr)
        return 2

    if args.commit_msg:
        scanner.text("commit-msg", Path(args.commit_msg).read_text(encoding="utf-8", errors="replace"), message=True)
    elif args.staged:
        scan_staged(scanner, root)
    else:
        scan_tracked(scanner, root)
        if args.history:
            scan_history(scanner, root, args.all)

    for hit in scanner.hits:
        print(hit)
    if scanner.hits:
        print(f"check_denylist: {len(scanner.hits)} hit(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

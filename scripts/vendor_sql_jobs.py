#!/usr/bin/env python3
"""Vendor the PostgreSQL driver of the Lambda SQL runtime and check the vendored tree.

    vendor_sql_jobs.py build    # reinstall modules/dms-orchestration/src/lambdas/sql_jobs/vendor from requirements.txt
    vendor_sql_jobs.py check    # rebuild into a temporary directory and compare every file (CI)

The Lambda zip carries everything it runs: nothing is downloaded at run time. ``build`` installs
``requirements.txt`` with ``uv pip install --require-hashes --no-deps --only-binary :all:`` (every
wheel pinned by hash, no resolution, no sdist builds) into ``vendor/``. ``check`` does the same into
a temporary directory and fails on any added, missing or changed file, on a wheel that is not pure
Python (the zip runs on arm64 and x86_64), and when ``rds-global-bundle.pem`` (the CA bundle for
``verify-full``) does not match the hash pinned in ``shared/sqlclient.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "dms-orchestration" / "src"))

from shared import sqlclient  # noqa: E402

SQL_JOBS = ROOT / "modules" / "dms-orchestration" / "src" / "lambdas" / "sql_jobs"
VENDOR = SQL_JOBS / "vendor"
REQUIREMENTS = SQL_JOBS / "requirements.txt"
PYTHON_VERSION = "3.13"
DROP = {".lock"}

Runner = Callable[..., Any]


def install(target: Path, runner: Runner = subprocess.run) -> None:
    argv = [
        "uv",
        "pip",
        "install",
        "--quiet",
        "--require-hashes",
        "--no-deps",
        "--only-binary",
        ":all:",
        "--python-version",
        PYTHON_VERSION,
        "--python-platform",
        "aarch64-manylinux2014",
        "--target",
        str(target),
        "-r",
        str(REQUIREMENTS),
    ]
    proc = runner(argv, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"uv pip install failed with exit code {proc.returncode}")
    for name in DROP:
        (target / name).unlink(missing_ok=True)
    for cache in list(target.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)


def tree(root: Path) -> dict[str, str]:
    """{relative posix path: sha256} of every file under root; bytecode caches (written by local
    imports, gitignored, excluded from the zip) do not count."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }


def impure(root: Path) -> list[str]:
    """dist-info directories whose wheel is not pure Python (py3-none-any)."""
    bad = []
    for dist in sorted(root.glob("*.dist-info")):
        wheel = (dist / "WHEEL").read_text(encoding="utf-8") if (dist / "WHEEL").exists() else ""
        if "Root-Is-Purelib: true" not in wheel or "-none-any" not in wheel:
            bad.append(dist.name)
    return bad


def check_ca(path: Path, expected: str) -> list[str]:
    if not path.is_file():
        return [f"{path.name} is missing"]
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        return [f"{path.name}: sha256 {actual} is not the pinned {expected} (shared/sqlclient.py)"]
    return []


def build(vendor: Path = VENDOR, runner: Runner = subprocess.run) -> int:
    shutil.rmtree(vendor, ignore_errors=True)
    install(vendor, runner)
    print(f"{len(tree(vendor))} file(s) in {vendor}")
    return 0


def check(vendor: Path = VENDOR, runner: Runner = subprocess.run, ca_ok: bool | None = None) -> int:
    problems = [] if ca_ok else check_ca(sqlclient.CA_BUNDLE, sqlclient.CA_BUNDLE_SHA256)
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "vendor"
        install(fresh, runner)
        want, have = tree(fresh), tree(vendor)
        problems += [f"missing: {rel}" for rel in sorted(set(want) - set(have))]
        problems += [f"not from requirements.txt: {rel}" for rel in sorted(set(have) - set(want))]
        problems += [f"changed: {rel}" for rel in sorted(set(want) & set(have)) if want[rel] != have[rel]]
    problems += [f"not pure Python: {name}" for name in impure(vendor)]
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        print("vendor check failed: run `make vendor` and commit, or fix the pins", file=sys.stderr)
        return 1
    print(f"vendor ok: {len(have)} file(s) match requirements.txt; CA bundle matches its pin")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["build", "check"])
    args = p.parse_args(argv)
    return build() if args.command == "build" else check()


if __name__ == "__main__":
    sys.exit(main())

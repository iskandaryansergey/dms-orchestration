#!/usr/bin/env python3
"""Validate and build job set bundles.

    jobset_tool.py validate <bundle-dir>             # every jobset*.yaml against the schema
    jobset_tool.py build <bundle-dir> <out-dir>      # copy the bundle, write jobset*.json and bundle.sha256
    jobset_tool.py hash <bundle-dir>                 # print the bundle total (jobs.bundles in Terraform)

A bundle is a directory with ``jobset.yaml`` (job set "default") and optional
``jobset.<name>.yaml`` files plus the SQL/scripts they reference. The build
output is what ``sync-jobs.sh`` uploads to ``s3://<artifacts>/jobs/<project>/<version>/``.
``bundle.sha256`` lists the SHA-256 of every built file and a total over them; the
task registration pins the total per version, and runs refuse a bundle that differs.

Checks beyond the JSON Schema: duplicate job names, lambda/fargate fields,
non-idempotent retries (shared.jobset.normalize), 'when' expressions parse, and
every ``--file``/``--template`` path in a psql-run/mysql-run command exists in the bundle.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shared import bundle as bundle_hash  # noqa: E402
from shared import expr, jobset  # noqa: E402
from shared.errors import InvalidJobSet  # noqa: E402

SQL_RUNNERS = {"psql-run", "mysql-run"}
PATH_FLAGS = {"--file", "--template"}


def jobset_files(bundle: Path) -> list[Path]:
    return sorted(p for p in bundle.glob("jobset*.y*ml") if p.suffix in (".yaml", ".yml"))


def referenced_paths(job: dict[str, Any]) -> list[str]:
    cmd = job.get("command") or []
    if not cmd or Path(cmd[0]).name not in SQL_RUNNERS:
        return []
    return [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg in PATH_FLAGS and "{{" not in cmd[i + 1]]


def check(path: Path) -> tuple[dict[str, Any], list[str]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    try:
        jobs = jobset.normalize(raw)
    except InvalidJobSet as err:
        return raw, [f"{path.name}: {err}"]
    for phase, items in jobs.items():
        for job in items:
            where = f"{path.name}: {phase}/{job['name']}"
            if job.get("when"):
                try:
                    expr.evaluate(job["when"], {})
                except expr.ExprError as err:
                    problems.append(f"{where}: bad 'when': {err}")
            for ref in referenced_paths(job):
                if not (path.parent / ref).is_file():
                    problems.append(f"{where}: {ref} not found in the bundle")
    return raw, problems


def validate(bundle: Path) -> int:
    files = jobset_files(bundle)
    if not files:
        print(f"no jobset.yaml in {bundle}", file=sys.stderr)
        return 1
    failed = False
    for f in files:
        _, problems = check(f)
        for p in problems:
            print(p, file=sys.stderr)
        failed = failed or bool(problems)
        if not problems:
            print(f"{f.name}: ok")
    return 1 if failed else 0


def write_manifest(out: Path) -> str:
    """Write bundle.sha256 over every file of a built bundle; returns the total."""
    files = {
        p.relative_to(out).as_posix(): bundle_hash.digest(p.read_bytes())
        for p in sorted(out.rglob("*"))
        if p.is_file() and p.relative_to(out).as_posix() != bundle_hash.MANIFEST
    }
    text = bundle_hash.render(files)
    (out / bundle_hash.MANIFEST).write_bytes(text.encode())
    return bundle_hash.total(files)


def build(bundle: Path, out: Path) -> int:
    if validate(bundle):
        return 1
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(
        bundle, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", bundle_hash.MANIFEST)
    )
    for f in jobset_files(bundle):
        raw, _ = check(f)
        name = f.stem.removeprefix("jobset").lstrip(".") or "default"
        (out / jobset.jobset_file_name(name)).write_bytes(json.dumps(raw, indent=2).encode())
    total = write_manifest(out)
    print(f"built {out}")
    print(f"sha256 {total}")
    return 0


def hash_bundle(bundle: Path) -> int:
    """Print the total a build of ``bundle`` gets (a source or an already built bundle)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "bundle"
        with contextlib.redirect_stdout(io.StringIO()):
            code = build(bundle, out)
        if code:
            return code
        print(bundle_hash.parse((out / bundle_hash.MANIFEST).read_text(encoding="utf-8"))[1])
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "validate":
        return validate(Path(args[1]))
    if len(args) == 3 and args[0] == "build":
        return build(Path(args[1]), Path(args[2]))
    if len(args) == 2 and args[0] == "hash":
        return hash_bundle(Path(args[1]))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

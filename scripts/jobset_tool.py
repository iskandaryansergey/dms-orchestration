#!/usr/bin/env python3
"""Validate and build job set bundles.

    jobset_tool.py validate <bundle-dir>             # every jobset*.yaml against the schema
    jobset_tool.py build <bundle-dir> <out-dir>      # copy the bundle, write jobset*.json next to it

A bundle is a directory with ``jobset.yaml`` (job set "default") and optional
``jobset.<name>.yaml`` files plus the SQL/scripts they reference. The build
output is what ``sync-jobs.sh`` uploads to ``s3://<artifacts>/jobs/<project>/<version>/``.

Checks beyond the JSON Schema: duplicate job names, lambda/fargate fields,
non-idempotent retries (shared.jobset.normalize), 'when' expressions parse, and
every ``--file``/``--template`` path in a psql-run/mysql-run command exists in the bundle.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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


def build(bundle: Path, out: Path) -> int:
    if validate(bundle):
        return 1
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(bundle, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    for f in jobset_files(bundle):
        raw, _ = check(f)
        name = f.stem.removeprefix("jobset").lstrip(".") or "default"
        (out / jobset.jobset_file_name(name)).write_text(json.dumps(raw, indent=2), encoding="utf-8")
    print(f"built {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "validate":
        return validate(Path(args[1]))
    if len(args) == 3 and args[0] == "build":
        return build(Path(args[1]), Path(args[2]))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

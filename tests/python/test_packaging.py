"""The module is self-contained: everything it packages lives under its own directory."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULES = ROOT / "modules"


def test_no_module_reads_outside_its_directory():
    outside = [
        f"{tf.relative_to(ROOT).as_posix()}:{n}"
        for tf in MODULES.glob("*/*.tf")
        for n, line in enumerate(tf.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\$\{path\.module\}/\.\.", line)
    ]
    assert not outside, f"paths outside the module directory: {outside}"


def test_lambda_and_ssm_runtime_sources_are_in_the_module():
    module = MODULES / "dms-orchestration"
    lambda_tf = (module / "lambda.tf").read_text(encoding="utf-8")
    ssm_tf = (module / "ssm.tf").read_text(encoding="utf-8")
    assert 'source_dir  = "${path.module}/src"' in lambda_tf
    assert '"${path.module}/src/jobs/sql-runner"' in ssm_tf
    assert (module / "src" / "lambdas").is_dir() and (module / "src" / "shared").is_dir()
    assert not (ROOT / "src").exists(), "src/ moved under modules/dms-orchestration/"

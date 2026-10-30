"""Lambda SQL runtime: vendored driver, shared/sqlclient.py, the sql_jobs handler (fake pg8000)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest
import vendor_sql_jobs

ROOT = Path(__file__).resolve().parents[2]


# --- vendored driver and CA bundle (make vendor-check) --------------------------------


def test_the_committed_ca_bundle_matches_its_pin():
    from shared import sqlclient

    assert vendor_sql_jobs.check_ca(sqlclient.CA_BUNDLE, sqlclient.CA_BUNDLE_SHA256) == []
    assert sqlclient.CA_BUNDLE.read_bytes().count(b"BEGIN CERTIFICATE") > 10


def test_a_changed_ca_bundle_is_refused(tmp_path):
    pem = tmp_path / "ca.pem"
    pem.write_bytes(b"-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    problems = vendor_sql_jobs.check_ca(pem, "0" * 64)
    assert problems and "sha256" in problems[0]
    assert vendor_sql_jobs.check_ca(tmp_path / "missing.pem", "0" * 64)


def test_the_committed_vendor_tree_is_pure_python_and_pinned():
    vendor = vendor_sql_jobs.VENDOR
    names = {p.name.split("-")[0].lower() for p in vendor.glob("*.dist-info")}
    assert {"pg8000", "scramp", "asn1crypto"} <= names
    assert vendor_sql_jobs.impure(vendor) == []
    pinned = vendor_sql_jobs.REQUIREMENTS.read_text(encoding="utf-8")
    for dist in vendor.glob("*.dist-info"):
        name, version = dist.name.removesuffix(".dist-info").rsplit("-", 1)
        assert f"{name.lower().replace('_', '-')}=={version}" in pinned.lower(), dist.name
    assert "--hash=sha256:" in pinned
    assert not list(vendor.rglob("__pycache__")) and not (vendor / ".lock").exists()


def test_impure_wheels_are_reported(tmp_path):
    dist = tmp_path / "native-1.0.dist-info"
    dist.mkdir()
    (dist / "WHEEL").write_text("Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp313-cp313-manylinux_2_17_x86_64\n")
    assert vendor_sql_jobs.impure(tmp_path) == ["native-1.0.dist-info"]


def _fake_install(source: Path, extra: dict[str, bytes] | None = None) -> Any:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kw: Any) -> Any:
        from types import SimpleNamespace

        calls.append(argv)
        target = Path(argv[argv.index("--target") + 1])
        shutil.copytree(source, target, dirs_exist_ok=True)
        (target / ".lock").write_text("")
        for rel, data in (extra or {}).items():
            (target / rel).write_bytes(data)
        return SimpleNamespace(returncode=0)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_vendor_check_rebuilds_with_hashes_and_compares_every_file(tmp_path, capsys):
    vendor = tmp_path / "vendor"
    (vendor / "pkg").mkdir(parents=True)
    (vendor / "pkg" / "__init__.py").write_bytes(b"x = 1\n")
    runner = _fake_install(vendor)
    assert vendor_sql_jobs.check(vendor=vendor, runner=runner, ca_ok=True) == 0
    argv = runner.calls[0]
    assert "--require-hashes" in argv and "--no-deps" in argv and "--only-binary" in argv
    assert str(vendor_sql_jobs.REQUIREMENTS) in argv

    changed = _fake_install(vendor, {"pkg/__init__.py": b"x = 2\n", "pkg/extra.py": b""})
    assert vendor_sql_jobs.check(vendor=vendor, runner=changed, ca_ok=True) == 1
    err = capsys.readouterr().err
    assert "pkg/__init__.py" in err and "pkg/extra.py" in err


def test_vendor_build_replaces_the_tree_without_lock_files(tmp_path):
    source = tmp_path / "src"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "a.py").write_bytes(b"")
    vendor = tmp_path / "vendor"
    (vendor / "stale").mkdir(parents=True)
    assert vendor_sql_jobs.build(vendor=vendor, runner=_fake_install(source)) == 0
    assert sorted(vendor_sql_jobs.tree(vendor)) == ["pkg/a.py"]


def test_sha256_helper_matches_hashlib(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"abc")
    assert vendor_sql_jobs.tree(tmp_path) == {"f": hashlib.sha256(b"abc").hexdigest()}


def test_vendor_dir_is_excluded_from_lint_and_coverage():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "src/lambdas/sql_jobs/vendor" in text
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "src/lambdas/sql_jobs/vendor/** -text" in attrs


@pytest.mark.parametrize("target", ["vendor-check", "vendor"])
def test_make_targets_exist(target):
    assert f"\n{target}:" in (ROOT / "Makefile").read_text(encoding="utf-8")

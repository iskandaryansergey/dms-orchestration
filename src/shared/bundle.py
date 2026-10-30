"""Job set bundle hashes (``bundle.sha256``), shared by the build tool, resolve_context and the runtimes.

A built bundle carries ``bundle.sha256``: one line per file, ``<sha256>  <path>`` sorted by path
(the ``sha256sum`` format), then ``total <sha256>``. The total is the SHA-256 of the file lines,
so it covers every path and every byte; Terraform registers it per version
(``projects.<p>.tasks.<t>.jobs.bundles``) and the run pins it.

Pure standard library: the sql-runner image copies this file.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from shared.errors import JobSetTampered

MANIFEST = "bundle.sha256"
_HEX = re.compile(r"^[0-9a-f]{64}$")
_FILE_LINE = re.compile(r"^([0-9a-f]{64})  (\S(?:.*\S)?)$")
_TOTAL_LINE = re.compile(r"^total ([0-9a-f]{64})$")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX.match(value))


def check_path(rel: str) -> str:
    """A bundle path: relative, forward slashes, no empty, ``.`` or ``..`` segment, not the manifest."""
    parts = rel.split("/")
    if (
        not rel
        or rel == MANIFEST
        or "\\" in rel
        or any(ord(c) < 32 for c in rel)
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise JobSetTampered(f"bad path in the job set bundle: {rel!r}")
    return rel


def total(files: Mapping[str, str]) -> str:
    return digest("".join(f"{files[p]}  {p}\n" for p in sorted(files)).encode())


def render(files: Mapping[str, str]) -> str:
    """The manifest text for ``{path: sha256}``."""
    for rel in files:
        check_path(rel)
    lines = [f"{files[p]}  {p}\n" for p in sorted(files)]
    return "".join(lines) + f"total {total(files)}\n"


def parse(text: str) -> tuple[dict[str, str], str]:
    """``({path: sha256}, total)`` of a manifest whose total matches its file lines."""
    lines = text.splitlines()
    if not lines or not _TOTAL_LINE.match(lines[-1]):
        raise JobSetTampered(f"{MANIFEST} has no total line")
    files: dict[str, str] = {}
    for line in lines[:-1]:
        m = _FILE_LINE.match(line)
        if not m:
            raise JobSetTampered(f"{MANIFEST}: bad line {line[:80]!r}")
        rel = check_path(m.group(2))
        if rel in files:
            raise JobSetTampered(f"{MANIFEST}: {rel} listed twice")
        files[rel] = m.group(1)
    stated = _TOTAL_LINE.match(lines[-1]).group(1)  # type: ignore[union-attr]
    if total(files) != stated:
        raise JobSetTampered(f"{MANIFEST}: the total does not match its file lines")
    return files, stated


def check_manifest(data: bytes | None, expected: str | None) -> dict[str, str] | None:
    """The file hashes a runtime checks the bundle against.

    ``expected`` is the total the run pinned (``bundleSha256`` of the job). A pinned run needs a
    manifest with exactly that total. An unpinned run (``allow_unpinned``) still checks the files
    against a manifest it finds; without one it gets None (no check).
    """
    if data is None:
        if expected:
            raise JobSetTampered(f"no {MANIFEST} in the job set bundle, but the run pinned {expected[:12]}…")
        return None
    files, found = parse(data.decode("utf-8", errors="replace"))
    if expected and found != expected:
        raise JobSetTampered(f"{MANIFEST} does not match the bundle hash the run pinned ({expected[:12]}…)")
    return files


def verify(rel: str, data: bytes, files: Mapping[str, str]) -> bytes:
    """``data`` if it is the file the manifest lists under ``rel``."""
    expected = files.get(rel)
    if expected is None:
        raise JobSetTampered(f"{rel} is not in the job set bundle ({MANIFEST})")
    if digest(data) != expected:
        raise JobSetTampered(f"{rel} does not match its hash in {MANIFEST}")
    return data

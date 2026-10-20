#!/usr/bin/env python3
"""Check the offline, reference-only Kimi provenance manifest.

The upstream checkout is intentionally not required.  Only the hashes of
Open Clank's checked-in generic fixtures are verified during normal CI.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "contracts/openclank/kimi-reference-fixtures-v1.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if data.get("schema") != "openclank.kimi-reference-fixtures.v1":
        raise ValueError("unexpected Kimi reference manifest schema")
    source = data.get("source") or {}
    if source.get("commit") != "44a6c70e66762ea9e122f8dceae16dc759086a7c":
        raise ValueError("manifest must remain pinned to the audited public commit")
    if source.get("license") != "MIT":
        raise ValueError("reference license must remain explicit")
    if source.get("network_required_for_validation") is not False:
        raise ValueError("normal validation must remain offline")
    if source.get("private_web_source_used") or source.get("dist_web_decompiled"):
        raise ValueError("private or decompiled web artifacts are forbidden")
    paths = source.get("paths") or []
    if not paths or any(not item.get("path") or len(item.get("sha256", "")) != 64 for item in paths):
        raise ValueError("source provenance rows need path and SHA-256")
    fixtures = data.get("openclank_fixtures") or []
    if not fixtures:
        raise ValueError("generic Open Clank fixture list is empty")
    for item in fixtures:
        relative = item.get("path", "")
        path = ROOT / relative
        if not relative or not path.is_file():
            raise ValueError(f"missing checked-in fixture: {relative}")
        expected = item.get("sha256", "")
        if len(expected) != 64 or _sha256(path) != expected:
            raise ValueError(f"fixture hash drift: {relative}")


if __name__ == "__main__":
    check()
    print("Kimi reference manifest valid: offline provenance and generic fixture hashes match")

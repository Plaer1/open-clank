from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "odysseus_filesystem_fixture.py"
SPEC = importlib.util.spec_from_file_location("odysseus_filesystem_fixture", SCRIPT)
assert SPEC and SPEC.loader
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


def test_generator_refuses_non_temporary_destination():
    with pytest.raises(ValueError, match="temporary"):
        fixture.validated_output(ROOT / "unsafe-fixture-target")


def test_generator_creates_only_synthetic_disposable_data(tmp_path):
    output = tmp_path / "fixture"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--tree-count",
            "0",
            "--skip-copal",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    public_summary = json.loads(result.stdout)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert public_summary == {
        "copal_documents": 0,
        "schema": "open-clank.odysseus-audit-fixture/v1",
        "synthetic_only": True,
        "tree_entries": 0,
    }
    assert manifest["synthetic_only"] is True
    assert (output / "files" / "sparse-250m.bin").stat().st_size == 250 * 1024 * 1024
    assert (output / "permission-matrix" / "allowed-root" / "exact-file.txt").is_file()
    assert str(tmp_path) not in result.stdout

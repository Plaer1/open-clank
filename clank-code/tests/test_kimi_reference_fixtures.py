from __future__ import annotations

import importlib.util
from pathlib import Path


def test_reference_manifest_is_offline_and_fixture_hashes_match():
    path = Path("scripts/check_kimi_reference_fixtures.py")
    spec = importlib.util.spec_from_file_location("check_kimi_reference_fixtures", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.check()

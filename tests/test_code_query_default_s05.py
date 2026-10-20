"""Explicit default-build child: fixture gates are absent from production builds.

Requires an isolated env-i startup and a caller-supplied binary SHA256. No
installed fallback, app lifespan, live owner data, model, or Files child.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

if not os.environ.get("OPEN_CLANK_TEST_DEFAULT_FM_SHA256"):
    pytest.skip("Explicit default-build binary and digest required", allow_module_level=True)

from tests.test_code_query_root_transport import _assert_isolated_startup
from tests import test_code_index_lifecycle as lifecycle

_assert_isolated_startup()
scope = lifecycle.scope


async def test_default_index_and_query_ignore_fixture_gate(scope, tmp_path, monkeypatch):
    binary = Path(os.environ["OPEN_CLANK_TEST_FM_MCP_BIN"]).resolve()
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == os.environ["OPEN_CLANK_TEST_DEFAULT_FM_SHA256"]
    provider, gate = await lifecycle.native_provider(scope, tmp_path, monkeypatch)
    try:
        assert Path(provider._command).resolve() == binary
        assert provider._env["FM_S03_FIXTURE_GATE"] == str(gate)
        assert list(gate.iterdir()) == []
        root = Path(lifecycle.native_request(scope)["root"])
        (root / "default.py").write_text(
            "def without_fixture_gate():\n    return 1\n", encoding="utf-8"
        )
        await provider.code_index_lifecycle(**lifecycle.native_request(scope))

        async def ready():
            status = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
            return status if status["lifecycle"] == "ready" else False

        status = await lifecycle.wait_native(ready, "ungated default-build publication")
        assert status["head_generation"] is not None
        binding = lifecycle.native_request(scope)
        binding.pop("action")
        # Deliberately never create a release marker: both the indexing and
        # query workers must ignore the feature-only gate in this build.
        result = await asyncio.wait_for(provider.code_query(binding=binding, query={"operation": "catalog"}), 5)
        assert not result.get("error"), result
        assert result["receipt"]["workspace_id"] == binding["workspace_id"]
        assert result["hits"] and "default.py" in json.dumps(result["hits"])
        assert result["cost"]["native_rpc_calls_completed"] >= 2
        found = await asyncio.wait_for(provider.code_query(binding=binding, query={
            "operation": "search", "query": "without_fixture_gate", "channels": ["exact"]
        }), 5)
        assert not found.get("error"), found
        assert found["hits"] and "without_fixture_gate" in json.dumps(found["hits"])
        assert await asyncio.wait_for(provider.recall("nothing stored", owner="alice"), 3) == []
        assert list(gate.iterdir()) == [], "Default binary must create no fixture hold/finish/release markers"
    finally:
        await provider.shutdown()

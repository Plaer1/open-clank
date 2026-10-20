"""Opt-in actual MCP/provider timing; synthetic private store, never app speed."""
import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

binary = os.environ.get("OPENCLANK_S00_NATIVE_METRICS_BIN")
expected = os.environ.get("OPENCLANK_S00_NATIVE_METRICS_SHA256")
if not binary or not expected:
    pytest.skip("Explicit S00 native binary and SHA required; no installed fallback", allow_module_level=True)
binary = Path(binary).resolve(strict=True)
assert binary.is_file() and hashlib.sha256(binary.read_bytes()).hexdigest() == expected
startup = Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve()
assert startup.name.startswith("openclank-s00-") and startup.parent == Path("/private/tmp")
assert Path(os.environ["FM_DB_PATH"]).resolve().is_relative_to(startup)
assert os.environ["DATABASE_URL"] == "sqlite:///:memory:"
assert "OPEN_CLANK_AGENT_HOME" not in os.environ and "THESIUS_AGENT_HOME" not in os.environ

from tests import test_code_index_lifecycle as lifecycle
from src.openclank.code_performance import capture_code_performance

scope = lifecycle.scope


async def test_actual_owner_queue_metrics_preserve_query_contract(scope, tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "NATIVE_BINARY", str(binary))
    # The native partition cache derives from the child process home, not just
    # FM_DB_PATH. Keep its cache inside this fixture too; never use user caches.
    isolated_home = tmp_path / "native-home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))
    root = Path(lifecycle.native_request(scope)["root"])
    for n in range(133):
        (root / f"module_{n:03}.py").write_text(f"def target_{n}():\n    return {n}\n", encoding="utf8")
    provider, gate = await lifecycle.native_provider(scope, tmp_path, monkeypatch)
    (gate / "release").touch()
    try:
        await provider.code_index_lifecycle(**lifecycle.native_request(scope))
        async def ready():
            status = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
            assert status["lifecycle"] not in {"failed", "erased"}
            return status if status["lifecycle"] == "ready" else None
        await lifecycle.wait_native(ready, "S00 synthetic partition head")
        binding = lifecycle.native_request(scope)
        binding.pop("action")
        query = {"operation": "search", "query": "target_0", "channels": ["exact"],
                 "filter": {"kinds": ["declaration"]}, "fields": ["label", "source"]}
        plain = await provider.code_query(binding=binding, query=query)
        assert plain["error"] is None and plain["hits"]
        rows = []
        for iteration in range(5):
            with capture_code_performance() as capture:
                measured = await provider.code_query(binding=binding, query=query)
            # RPC counts vary with async scheduling, independently of metrics.
            def logical(result):
                result = {**result, "cost": dict(result.get("cost", {}))}
                result["cost"].pop("native_rpc_calls_completed", None)
                return result
            assert logical(measured) == logical(plain)
            metrics = capture.snapshot()
            calls = measured["cost"]["native_rpc_calls_completed"]
            assert metrics["provider_queue_wait"]["count"] == calls
            assert metrics["provider_service"]["count"] == calls
            assert metrics["provider_decode"]["count"] == calls
            assert metrics["query_start_wait"]["count"] == 1
            assert metrics["query_poll_wait"]["count"] == calls - 1
            assert metrics["query_total"]["count"] == 1
            assert all(row["failed"] == row["cancelled"] == 0 for row in metrics.values())
            rows.append({"iteration": iteration, "native_rpc_calls": calls, "stages": metrics})
        print("S00_PROVIDER " + json.dumps({"native_sha256": expected, "synthetic_files": 133,
              "result_equality": True, "normal_app_qualified": False, "rows": rows}, sort_keys=True))
    finally:
        await provider.shutdown()

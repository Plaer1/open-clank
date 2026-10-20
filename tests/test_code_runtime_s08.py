"""Root C08 repeat measurement, actual single FM owner, isolated fixtures only."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time

import pytest

if not os.environ.get("OPEN_CLANK_TEST_FM_MCP_BIN"):
    pytest.skip("Explicit matching native fixture binary required", allow_module_level=True)

# Guard before application imports; never fall back to configured/live state.
_root = Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve()
assert _root.name.startswith("openclank-s03-")
for _key in ("ODYSSEUS_DATA_DIR", "OPEN_CLANK_AUTHORITY_DB_PATH", "OPEN_CLANK_AUTHORITY_AUTH_PATH",
             "FM_DB_PATH", "OPENCLANK_CONVERSATION_ARCHIVE_DB"):
    assert Path(os.environ[_key]).resolve().is_relative_to(_root), _key
assert os.environ["DATABASE_URL"] == "sqlite:///:memory:"
assert os.environ["ODYSSEUS_INPROCESS_POLLERS"] == "0"
assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
assert "OPEN_CLANK_AGENT_HOME" not in os.environ and "THESIUS_AGENT_HOME" not in os.environ
_binary = Path(os.environ["OPEN_CLANK_TEST_FM_MCP_BIN"]).resolve()
assert Path(os.environ["FM_MCP_COMMAND"]).resolve() == _binary
assert hashlib.sha256(_binary.read_bytes()).hexdigest() == os.environ["OPEN_CLANK_TEST_FM_MCP_SHA256"]

from tests import test_code_index_lifecycle as lifecycle  # noqa: E402
from tests.test_code_query_transport_s05 import prepared  # noqa: E402
from src.openclank.files_service_client import close_all_clients  # noqa: E402

scope = lifecycle.scope


async def test_same_owner_repeated_recall_during_real_large_queries(scope, tmp_path, monkeypatch):
    root = Path(lifecycle.native_request(scope)["root"])
    for index in range(512):
        (root / f"unit_{index:04}.py").write_text(f"def workload_{index}():\n    return {index}\n", encoding="utf8")
    provider, _ = await prepared(scope, tmp_path, monkeypatch, ready_timeout=120)
    authority = lifecycle.native_request(scope, subscriber="query:c08-repeat")
    authority.pop("action")
    owner_task = provider._owner_task
    corpus = hashlib.sha256()
    for path in sorted(root.glob("*.py")):
        corpus.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    samples, truth, query_task = [], None, None
    try:
        for repeat in range(21):
            started = time.perf_counter()
            query_task = asyncio.create_task(provider.code_query(binding=authority, query={
                "operation": "search", "query": "workload_511", "fields": ["label"],
                "budgets": {"rows": 1, "bytes": 8192, "tokens": 8192, "work": 100000}}))
            await asyncio.sleep(0)
            recall_started = time.perf_counter()
            recalled = await provider.recall("nothing stored", owner="alice")
            recall_ms = (time.perf_counter() - recall_started) * 1000
            pending = not query_task.done()
            result = await query_task
            elapsed_ms = (time.perf_counter() - started) * 1000
            assert recalled == [] and result["error"] is None
            assert result["hits"][0]["label"] == "workload_511"
            assert result["cost"]["metadata_bytes"] > 1024 * 1024
            logical = {key: result[key] for key in ("hits", "receipt", "channels", "omissions")}
            if truth is None:
                truth = logical
            assert logical == truth, "Warm repetitions must preserve exact scoped evidence"
            assert provider._owner_task is owner_task, "No second MCP owner may explain responsiveness"
            if repeat:
                samples.append(dict(recall_ms=recall_ms, query_ms=elapsed_ms, query_pending=pending))
        def stats(key):
            values = sorted(row[key] for row in samples)
            return dict(p50=round(values[9], 3), p95=round(values[18], 3), maximum=round(values[-1], 3))
        recall = stats("recall_ms")
        report = dict(files=513, corpus_sha256=corpus.hexdigest(), corpus_encoding="sorted_filename_NUL_bytes_NUL",
                      warmups=1, samples=20, recall_ms=recall, query_ms=stats("query_ms"),
                      query_pending_at_recall_return=sum(row["query_pending"] for row in samples),
                      metadata_bytes=result["cost"]["metadata_bytes"], logical_results_equal=True)
        print("S08_SAME_OWNER " + json.dumps(report, sort_keys=True))
        assert report["query_pending_at_recall_return"] > 0
        assert recall["p95"] <= 100 and recall["maximum"] <= 250, report
    finally:
        if query_task is not None and not query_task.done():
            query_task.cancel()
            await asyncio.gather(query_task, return_exceptions=True)
        close_all_clients()
        await provider.shutdown()

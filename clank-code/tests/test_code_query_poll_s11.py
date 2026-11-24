"""Actual native status polling must not compete with its snapshot fence."""
import asyncio
import sqlite3

import pytest

from tests.test_code_query_transport_s05 import prepared, scope
from tests import test_code_index_lifecycle as lifecycle
from src.memory_provider import MemoryRequestRejectedError
from src.openclank.files_service_client import close_all_clients


async def test_running_poll_reads_authority_while_writer_held_and_still_rejects_revocation(scope, tmp_path, monkeypatch):
    provider, _ = await prepared(scope, tmp_path, monkeypatch)
    gate = tmp_path / "native-gate"
    (gate / "release").unlink()
    binding = lifecycle.native_request(scope, subscriber="query:s11-poll")
    binding.pop("action")
    try:
        ticket = await provider._call_tool("code_query", dict(action="start", binding=binding, query=dict(operation="catalog")))
        token = ticket["token"]
        await lifecycle.wait_native(lambda: (gate / (token + ".held")).exists(), "queued query held")
        # The real worker uses this same exclusive authority fence. The
        # fixture gate makes the contention deterministic without timing a CPU.
        with sqlite3.connect(scope[0].db_path) as authority:
            authority.execute("BEGIN IMMEDIATE")
            second = await asyncio.wait_for(provider._call_tool("code_query", dict(action="start", binding=binding, query=dict(operation="capabilities"))), 1)
            assert second["state"] == "running" and second["token"] != token
            for _ in range(3):
                result = await asyncio.wait_for(provider._call_tool("code_query", dict(action="poll", binding=binding, token=token)), 1)
                assert result == dict(state="running", token=token)
            authority.rollback()
        assert scope[0].revoke_binding(scope[2]["alice", "people"].id, actor_subject_id="s03-admin")
        with pytest.raises(MemoryRequestRejectedError):
            await provider._call_tool("code_query", dict(action="poll", binding=binding, token=token))
        with pytest.raises(MemoryRequestRejectedError):
            await provider._call_tool("code_query", dict(action="start", binding=binding, query=dict(operation="catalog")))
    finally:
        (gate / "release").touch()
        close_all_clients()
        await provider.shutdown()


async def test_four_queued_queries_complete_with_one_receipt_and_preserve_cancellation(scope, tmp_path, monkeypatch):
    provider, _ = await prepared(scope, tmp_path, monkeypatch)
    gate = tmp_path / "native-gate"
    (gate / "release").unlink()
    binding = lifecycle.native_request(scope, subscriber="query:s11-concurrent")
    binding.pop("action")
    jobs = []
    try:
        for operation in ("catalog", "capabilities", "catalog", "capabilities"):
            jobs.append(await provider._call_tool("code_query", dict(action="start", binding=binding, query=dict(operation=operation))))
        await provider._call_tool("code_query", dict(action="cancel", binding=binding, token=jobs[-1]["token"]))
        (gate / "release").touch()
        async def finish(ticket):
            async with asyncio.timeout(10):
                while True:
                    value = await provider._call_tool("code_query", dict(action="poll", binding=binding, token=ticket["token"]))
                    if value["state"] != "running": return value
                    await asyncio.sleep(.01)
        results = await asyncio.gather(*(finish(job) for job in jobs))
        assert [r["state"] for r in results] == ["completed", "completed", "completed", "failed"]
        assert results[-1]["error"]["code"] == "cancelled"
        assert all(r["result"]["receipt"] == results[0]["result"]["receipt"] for r in results[:3])
    finally:
        (gate / "release").touch()
        close_all_clients()
        await provider.shutdown()

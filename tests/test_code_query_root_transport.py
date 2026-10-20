"""Root-owned actual-child code-query checks; no app, live data or installed fallback.

Run only with the matching s03-fixtures fm-mcp binary and fully isolated env-i
startup. These checks qualify private native jobs, not friendly tools/context.
"""
import asyncio
import os
from pathlib import Path

import pytest

if not os.environ.get("OPEN_CLANK_TEST_FM_MCP_BIN"):
    pytest.skip("Explicit matching s03-fixtures binary is required; no installed fallback", allow_module_level=True)


def _assert_isolated_startup():
    root = Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve()
    assert root.name.startswith("openclank-s03-"), "explicit isolated startup root required"
    for key in ("ODYSSEUS_DATA_DIR", "OPEN_CLANK_AUTHORITY_DB_PATH",
                "OPEN_CLANK_AUTHORITY_AUTH_PATH", "FM_DB_PATH",
                "OPENCLANK_CONVERSATION_ARCHIVE_DB"):
        assert Path(os.environ[key]).resolve().is_relative_to(root), key
    assert os.environ["DATABASE_URL"] == "sqlite:///:memory:"
    assert os.environ["ODYSSEUS_INPROCESS_POLLERS"] == "0"
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
    binary = Path(os.environ["OPEN_CLANK_TEST_FM_MCP_BIN"]).resolve()
    assert binary.is_file()
    assert Path(os.environ["FM_MCP_COMMAND"]).resolve() == binary
    assert "OPEN_CLANK_AGENT_HOME" not in os.environ
    assert "THESIUS_AGENT_HOME" not in os.environ


_assert_isolated_startup()

from tests import test_code_index_lifecycle as lifecycle  # noqa: E402
from src.memory_provider import MemoryRequestRejectedError  # noqa: E402

scope = lifecycle.scope


def binding(scope, *, owner="alice", subscriber="session:root-query-a"):
    value = lifecycle.native_request(scope, owner=owner, subscriber=subscriber)
    value.pop("action")
    return value


async def job(provider, authority, action, **arguments):
    return await asyncio.wait_for(provider._call_tool("code_query", {
        "action": action, "binding": authority, **arguments,
    }), 3)


async def ready_provider(scope, tmp_path, monkeypatch):
    provider, gate = await lifecycle.native_provider(scope, tmp_path, monkeypatch)
    try:
        for owner in ("alice", "bob"):
            (Path(binding(scope, owner=owner)["root"]) / "source.py").write_text(
                "def target():\n    return 1\ndef caller():\n    target()\n", encoding="utf-8")
            await provider.code_index_lifecycle(**lifecycle.native_request(scope, owner=owner))
        (gate / "release").touch()

        async def ready(owner):
            result = await provider.code_index_lifecycle(**lifecycle.native_request(scope, owner=owner, action="status"))
            return result if result["lifecycle"] == "ready" else False

        for owner in ("alice", "bob"):
            await lifecycle.wait_native(lambda owner=owner: ready(owner), owner + " indexed fixture")
        return provider, gate
    except BaseException:
        (gate / "release").touch()
        await provider.shutdown()
        raise


async def completed(provider, authority, token):
    async def poll():
        result = await job(provider, authority, "poll", token=token)
        return result if result["state"] != "running" else False
    return await lifecycle.wait_native(poll, "bounded code job completion")


async def test_held_query_keeps_real_recall_responsive_and_tokens_chat_scoped(scope, tmp_path, monkeypatch):
    provider, gate = await ready_provider(scope, tmp_path, monkeypatch)
    authority = binding(scope)
    try:
        (gate / "release").unlink()  # Exact disposable fixture gate only.
        started = await job(provider, authority, "start", query={"operation": "catalog"})
        assert started["state"] == "running"
        token = started["token"]
        await lifecycle.wait_native(lambda: (gate / (token + ".held")).exists(), "query held before work")
        assert await asyncio.wait_for(provider.recall("nothing stored", owner="alice"), 3) == []
        assert not (gate / "release").exists(), "recall must finish while query remains held"
        for wrong_binding in (binding(scope, owner="bob"), binding(scope, subscriber="session:root-query-b")):
            for action in ("poll", "cancel"):
                with pytest.raises(MemoryRequestRejectedError):
                    await job(provider, wrong_binding, action, token=token)
        assert (await job(provider, authority, "poll", token=token))["state"] == "running"
        cancelled = await job(provider, authority, "cancel", token=token)
        assert cancelled["state"] == "cancelled"
        (gate / "release").touch()
        result = await completed(provider, authority, token)
        assert result["state"] in {"failed", "cancelled"}
        assert result["error"]["code"] == "cancelled"
        assert "result" not in result
    finally:
        (gate / "release").touch()
        await provider.shutdown()


async def test_cancel_after_completion_never_replays_saved_success(scope, tmp_path, monkeypatch):
    provider, gate = await ready_provider(scope, tmp_path, monkeypatch)
    authority = binding(scope)
    try:
        started = await job(provider, authority, "start", query={"operation": "catalog"})
        token = started["token"]
        result = await completed(provider, authority, token)
        assert result["state"] == "completed"
        assert result["result"]["receipt"]["workspace_id"] == authority["workspace_id"]
        assert result["result"]["hits"]
        assert (await job(provider, authority, "cancel", token=token))["state"] == "cancelled"
        try:
            after = await job(provider, authority, "poll", token=token)
        except MemoryRequestRejectedError:
            pass  # Consuming/removing a cancelled receipt is also fail-closed.
        else:
            assert after["state"] in {"failed", "cancelled"}
            assert after["error"]["code"] == "cancelled"
            assert "result" not in after
    finally:
        (gate / "release").touch()
        await provider.shutdown()


async def test_erasure_fences_completed_query_and_reads_do_not_readmit(scope, tmp_path, monkeypatch):
    provider, gate = await ready_provider(scope, tmp_path, monkeypatch)
    authority = binding(scope)
    try:
        started = await job(provider, authority, "start", query={"operation": "catalog"})
        token = started["token"]
        assert (await completed(provider, authority, token))["state"] == "completed"
        erased = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="remove"))
        assert erased["lifecycle"] == "erased"
        with pytest.raises(MemoryRequestRejectedError):
            await job(provider, authority, "poll", token=token)
        started = await job(provider, authority, "start", query={"operation": "catalog"})
        denied = await completed(provider, authority, started["token"])
        assert denied["state"] == "failed"
        assert denied["error"]["code"] == "erased"
        status = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
        assert status["lifecycle"] == "erased"
        other = await provider.code_index_lifecycle(**lifecycle.native_request(scope, owner="bob", action="status"))
        assert other["lifecycle"] == "ready", "selected-owner erasure must preserve the other fixture"
    finally:
        (gate / "release").touch()
        await provider.shutdown()


async def test_private_query_queue_has_finite_capacity_and_recovers_after_cancel(scope, tmp_path, monkeypatch):
    provider, gate = await ready_provider(scope, tmp_path, monkeypatch)
    authority = binding(scope)
    try:
        (gate / "release").unlink()
        tokens = [(await job(provider, authority, "start", query={"operation": "catalog"}))["token"]
                  for _ in range(4)]
        with pytest.raises(MemoryRequestRejectedError):
            await job(provider, authority, "start", query={"operation": "catalog"})
        for token in tokens:
            await job(provider, authority, "cancel", token=token)
        (gate / "release").touch()
        for token in tokens:
            await lifecycle.wait_native(lambda token=token: (gate / (token + ".finished")).exists(), "cancelled worker exit")
        started = await job(provider, authority, "start", query={"operation": "catalog"})
        assert (await completed(provider, authority, started["token"]))["state"] == "completed"
    finally:
        (gate / "release").touch()
        await provider.shutdown()

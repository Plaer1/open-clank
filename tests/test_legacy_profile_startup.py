"""Default legacy-v16 profile over the real transport: Python provider -> real memery-worker -> fresh store.

Built like app_initializer: prepare_memery_database() then MemeryProvider with
FM_SCOPE_AUTHORITY=trusted-caller and no fixed FM_OWNER. No model, no network.
"""
import asyncio
import os
import atexit
import shutil
import tempfile


def _scratch(prefix):
    """mkdtemp that is removed at interpreter exit (RAM-backed /tmp); the exact path only."""
    path = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def _run(body):
    from src.memery_database import prepare_memery_database
    from src.memery_provider import MemeryProvider
    db = os.path.join(_scratch("fd1-legacy-"), "fm.db")
    database_id = prepare_memery_database(db)
    provider = MemeryProvider(
        command=os.environ["OPEN_CLANK_MEMERY_WORKER"], workspace_id="fd1-ws",
        env={"FM_DB_PATH": db, "FM_DB_ID": database_id, "FM_SCOPE_AUTHORITY": "trusted-caller"},
        broker_url="", broker_token="")

    async def go():
        await provider.initialize()  # (a) health check without any owner
        try:
            await body(provider)
        finally:
            await provider.shutdown()
    asyncio.run(go())


def test_legacy_startup_and_first_chat_calls_accepted_scopeless_refused():
    async def body(p):
        # (b) calls of the first chat / settings pages, one each, with an owner
        rec = await p.remember("fd1 alpha secret fact", owner="owner-a")
        assert rec.id
        q = await p.memory_quality()
        assert "counts" in q
        assert await p.digest(owner="owner-a") is not None
        # accepted (scope sent); a fresh manual capture is not necessarily a curated row, so no count is asserted
        await p.list_memories(owner="owner-a")
        for tier in ("raw", "candidate", "curated", "quarantine"):
            await p.inspect_tier(tier, owner="owner-a")
        hits = await p.recall("alpha secret", owner="owner-a")
        assert any(h.memory.id == rec.id for h in hits)
        assert await p.list_memories(owner="owner-b") == []  # (d) owner isolation
        assert not await p.recall("alpha secret", owner="owner-b")
        # (c) hardening stays: a scoped Native tool without scope is refused
        from src.memory_provider import MemoryRequestRejectedError
        try:
            await p._call_tool("memory_quality", {"rebuild_graph_fts": False})
        except MemoryRequestRejectedError as exc:
            assert "authenticated memory scope unavailable" in str(exc)
        else:
            raise AssertionError("scope-less memory_quality was accepted")
    _run(body)

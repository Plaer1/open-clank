"""Real transport: fresh legacy store -> real memery-worker -> the export route over the panel's list reader."""
import asyncio
import os
import atexit
import shutil
import tempfile
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from routes.memory.markdown_export import register_markdown_export
from src import memery_formats as f


def _scratch(prefix):
    """mkdtemp that is removed at interpreter exit (RAM-backed /tmp); the exact path only."""
    path = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def test_legacy_export_matches_the_panel_list_and_never_shows_another_owner():
    from src.memery_database import prepare_memery_database
    from src.memery_provider import MemeryProvider
    db = os.path.join(_scratch("f11-legacy-"), "fm.db")
    database_id = prepare_memery_database(db)
    provider = MemeryProvider(
        command=os.environ["OPEN_CLANK_MEMERY_WORKER"], workspace_id="f11-ws",
        env={"FM_DB_PATH": db, "FM_DB_ID": database_id, "FM_SCOPE_AUTHORITY": "trusted-caller"},
        broker_url="", broker_token="")
    state = {}

    async def setup():
        await provider.initialize()
        await provider.remember("f11 text of owner a\n  with indent", owner="alice")
        await provider.remember("f11 text of owner b", owner="bob")
        state["panel"] = [r.text for r in await provider.list_memories(owner="alice", limit=100)]
        state["panel_b"] = [r.text for r in await provider.list_memories(owner="bob", limit=100)]
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(setup())
        assert provider._guarded_v17 is False
        # TestClient runs the handler on its own loop; the provider's transport is bound to `loop`, so bridge the call.
        async def bridge(**kw):
            return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(provider.list_memories(**kw), loop))
        import threading
        t = threading.Thread(target=loop.run_forever, daemon=True); t.start()
        shim = SimpleNamespace(_guarded_v17=False, list_memories=bridge)

        async def owned(fn, *a, **k): return fn(*a, **k)
        router = APIRouter(prefix="/api/memory")
        register_markdown_export(router, require_human=lambda request: "alice", capture_context=None, owned=owned,
                                 provider=shim, as_record=lambda r: {"id": r.id, "text": r.text, "owner": r.owner, "kind": r.kind,
                                                                      "source_message_ids": list(r.source_message_ids)})
        app = FastAPI(); app.include_router(router)
        r = TestClient(app).get("/api/memory/export/markdown")
        assert r.status_code == 200
        texts = [i.text for i in f.parse_markdown(r.content).items]
        # Consistency with the panel list (a separate defect may hide new manual memories from it; then both are empty).
        assert sorted(texts) == sorted(state["panel"])
        assert "f11 text of owner b" not in r.text and not any("bob" in t for t in texts)
        assert all(not i.source_message_ids or not any(m.startswith("memory:") for m in i.source_message_ids)
                   for i in f.parse_markdown(r.content).items)
        print("panel list:", state["panel"], "export:", texts)
    finally:
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(provider.shutdown(), loop).result(15)
            loop.call_soon_threadsafe(loop.stop)
        else:
            loop.run_until_complete(provider.shutdown())

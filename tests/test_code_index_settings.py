"""Synthetic settings/provider tests; never launch a configured native owner."""
import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.openclank.code_index_settings import (
    MAX_REVISION, SCHEMA, THREADS_KEY, REVISION_KEY, configured,
    prepare_settings, preserve_scheduler_revision, readback, validate_status,
)


def native(**patch):
    return {"schema": SCHEMA, "revision": 0, "effective_revision": 0,
            "desired": 2, "effective": 2, "active": 0, "queued": 0,
            "drain_pending": False, **patch}


@pytest.mark.parametrize("bad", [True, False, None, "2", 2.0, 1.5, 0, -1, 33, [], {}])
def test_strict_threads_reject_without_mutating_input(bad):
    old = {THREADS_KEY: 2, REVISION_KEY: 0, "other": {"nested": True}}
    before = copy.deepcopy(old)
    with pytest.raises(ValueError):
        prepare_settings(old, {THREADS_KEY: bad, "other": "must not change"})
    assert old == before


def test_revision_default_idempotence_change_exhaustion_and_stale_snapshot():
    assert configured({}) == (2, 0)
    old = {THREADS_KEY: 2, REVISION_KEY: 0}
    assert prepare_settings(old, {THREADS_KEY: 2}) == old
    saved = prepare_settings(old, {THREADS_KEY: 4})
    assert configured(saved) == (4, 1)
    assert configured(prepare_settings(saved, {THREADS_KEY: 1})) == (1, 2)
    with pytest.raises(ValueError):
        prepare_settings(saved, {REVISION_KEY: 3})
    with pytest.raises(ValueError):
        prepare_settings({THREADS_KEY: 4, REVISION_KEY: MAX_REVISION}, {THREADS_KEY: 3})
    assert configured(preserve_scheduler_revision({**old, "unrelated": True}, saved)) == (4, 1)
    assert preserve_scheduler_revision({"unrelated": True}, saved)[THREADS_KEY] == 4
    with pytest.raises(ValueError):
        preserve_scheduler_revision({THREADS_KEY: 3, REVISION_KEY: 1}, saved)


@pytest.mark.parametrize("patch", [
    {"schema": "wrong"}, {"active": True}, {"queued": 33}, {"path": "/private"},
    {"revision": 1.0}, {"effective": 0}, {"desired": 33}, {"active": 3},
    {"revision": 1}, {"drain_pending": "true"}, {"effective_revision": 1},
])
def test_native_status_rejects_unsafe_or_inconsistent_output(patch):
    with pytest.raises(ValueError):
        validate_status(native(**patch))


@pytest.mark.asyncio
async def test_readback_distinguishes_saved_pending_draining_and_applied():
    saved = {THREADS_KEY: 1, REVISION_KEY: 1}
    pending = await readback(None, saved)
    assert pending["desired"] == 1 and pending["effective"] is None
    calls = []
    class Provider:
        async def code_index_scheduler(self, **kw):
            calls.append(kw)
            return native(revision=1, desired=1, active=2, drain_pending=True)
    draining = await readback(Provider(), saved, apply=True)
    assert draining["status"] == "draining" and draining["effective"] == 2
    assert calls == [{"action": "configure", "revision": 1, "max_threads": 1, "ready_only": True}]
    class Applied:
        async def code_index_scheduler(self, **kw):
            assert kw == {"action": "status", "ready_only": True}
            return native(revision=1, effective_revision=1, desired=1, effective=1)
    assert (await readback(Applied(), saved))["status"] == "applied"
    class Bad:
        async def code_index_scheduler(self, **kw):
            raise RuntimeError("private path/credential must not be returned")
    assert await readback(Bad(), saved) == pending


@pytest.mark.asyncio
async def test_provider_status_never_initializes_and_broker_never_forwards(monkeypatch):
    from src.frankenmemory_provider import FrankenmemoryProvider
    from src.memory_provider import MemoryTransportError, MemoryRequestRejectedError
    provider = FrankenmemoryProvider(command="must-not-execute")
    async def forbidden():
        pytest.fail("Settings must not cold-start a native owner")
    monkeypatch.setattr(provider, "initialize", forbidden)
    with pytest.raises(MemoryTransportError):
        await provider.code_index_scheduler(action="status")
    assert (await readback(provider, {}))["effective"] is None
    provider._broker_url = "http://127.0.0.1:1"
    with pytest.raises(MemoryRequestRejectedError):
        await provider.code_index_scheduler(action="configure", revision=1, max_threads=2)


@pytest.mark.asyncio
async def test_ready_provider_uses_actual_queue_and_strict_app_wire(monkeypatch):
    from src.frankenmemory_provider import FrankenmemoryProvider
    provider = FrankenmemoryProvider(command="must-not-execute")
    provider._requests = asyncio.Queue()
    provider._ready = asyncio.get_running_loop().create_future()
    provider._ready.set_result(None)
    calls = []
    async def fake_owner():
        name, arguments, future = await provider._requests.get()
        calls.append((name, arguments))
        future.set_result(SimpleNamespace(isError=False, content=[SimpleNamespace(text=json.dumps(native()))]))
        await asyncio.Future()
    provider._owner_task = asyncio.create_task(fake_owner())
    try:
        assert await provider.code_index_scheduler(action="status") == native()
        assert calls == [("code_index_scheduler", {"action": "status", "origin": "app"})]
        with pytest.raises(ValueError):
            await provider.code_index_scheduler(action="configure", revision=1, max_threads=True)
        assert len(calls) == 1
    finally:
        provider._owner_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await provider._owner_task


@pytest.mark.asyncio
async def test_normal_index_admission_restores_latest_saved_value_first(monkeypatch):
    from src.frankenmemory_provider import FrankenmemoryProvider
    from src.memory_provider import MemoryTransportError
    from src import settings
    provider = FrankenmemoryProvider(command="must-not-execute")
    saved = {THREADS_KEY: 1, REVISION_KEY: 3}
    calls = []
    monkeypatch.setattr(settings, "load_settings", lambda: dict(saved))
    async def initialize():
        calls.append("initialize")
    async def scheduler(**arguments):
        calls.append(arguments)
        return native(desired=saved[THREADS_KEY], effective=saved[THREADS_KEY],
                      revision=saved[REVISION_KEY], effective_revision=saved[REVISION_KEY])
    async def transport(name, arguments):
        calls.append(name)
        return {"lifecycle": "queued"}
    monkeypatch.setattr(provider, "initialize", initialize)
    monkeypatch.setattr(provider, "code_index_scheduler", scheduler)
    monkeypatch.setattr(provider, "_call_tool", transport)
    binding = dict(owner="alice", owner_subject_id="subject", workspace_id="workspace",
                   location_id="location", workspace_revision=1, policy_generation=1,
                   root="/synthetic/not/read", subscriber_id="subscriber", origin="app")
    await provider.code_index_lifecycle(action="ensure", **binding)
    assert calls == ["initialize", {"action": "configure", "revision": 3, "max_threads": 1, "ready_only": True}, "code_index"]
    saved.update({THREADS_KEY: 4, REVISION_KEY: 4})
    await provider.code_index_lifecycle(action="admit", **binding)
    assert calls[-2]["max_threads"] == 4 and calls[-2]["revision"] == 4
    calls.clear()
    await provider.code_index_lifecycle(action="status", **binding)
    assert calls == ["code_index"]
    async def stale(**arguments):
        return native()
    monkeypatch.setattr(provider, "code_index_scheduler", stale)
    calls.clear()
    with pytest.raises(MemoryTransportError):
        await provider.code_index_lifecycle(action="ensure", **binding)
    assert calls == ["initialize"]


@pytest.mark.asyncio
async def test_slow_settings_observation_times_out_without_cancelling_index():
    cancelled = []
    class BusyOwner:
        async def code_index_scheduler(self, **arguments):
            assert arguments == {"action": "status", "ready_only": True}
            try:
                await asyncio.Future()
            finally:
                cancelled.append("readback only")
    result = await readback(BusyOwner(), {})
    assert result["status"] == "pending" and result["active"] is None
    assert cancelled == ["readback only"]


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", ["stalled", "old_engine", "applied", "cancelled", "queued_stalled"])
async def test_optional_startup_restore_cannot_block_memory_or_swallow_cancellation(monkeypatch, tmp_path, restore):
    import mcp
    import mcp.client.stdio
    from src import constants, settings
    from src.frankenmemory_provider import FrankenmemoryProvider
    entered, abandoned = asyncio.Event(), asyncio.Event()
    closed, calls = [], []
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "load_settings", lambda: {THREADS_KEY: 3, REVISION_KEY: 1})
    monkeypatch.delenv("FM_DB_ID", raising=False)

    class Session:
        def __init__(self, *_):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            closed.append("session")
        async def initialize(self):
            pass
        async def call_tool(self, name, arguments, **kwargs):
            calls.append((name, arguments))
            if name != "code_index_scheduler":
                assert not kwargs  # No session-wide or indexing deadline.
                value = {"schema_version": 10}
            else:
                first = sum(name == "code_index_scheduler" for name, _ in calls) == 1
                assert arguments == ({"origin": "app", "action": "configure", "revision": 1, "max_threads": 3}
                                     if first else {"origin": "app", "action": "status"})
                timeout = kwargs["read_timeout_seconds"].total_seconds()
                assert timeout == 1.5
                entered.set()
                if restore in {"stalled", "cancelled"} or (restore == "queued_stalled" and not first):
                    try:
                        # Simulate the SDK's per-request wait, with real
                        # suspension rather than a canned immediate timeout.
                        async with asyncio.timeout(timeout):
                            await asyncio.Future()
                    finally:
                        abandoned.set()
                if restore == "old_engine":
                    raise ValueError("Unknown tool; private engine detail")
                value = native(desired=3, effective=3, revision=1, effective_revision=1)
            return SimpleNamespace(isError=False, content=[SimpleNamespace(text=json.dumps(value))])

    @asynccontextmanager
    async def stdio(*_, **__):
        try:
            yield (None, None)
        finally:
            closed.append("stdio")

    monkeypatch.setattr(mcp, "ClientSession", Session)
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", stdio)
    provider = FrankenmemoryProvider(command="must-not-execute")
    initializing = asyncio.create_task(provider.initialize())
    await asyncio.wait_for(entered.wait(), 3)
    try:
        if restore == "cancelled":
            provider._owner_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await provider._owner_task
            with pytest.raises(ConnectionError):
                await initializing
            assert abandoned.is_set()
            assert calls[-1][0] == "code_index_scheduler"
        else:
            await asyncio.wait_for(initializing, 3)
            assert provider._ready.done() and provider._ready.exception() is None
            if restore == "queued_stalled":
                pending = await readback(provider, {THREADS_KEY: 3, REVISION_KEY: 1})
                assert pending["status"] == "pending" and pending["effective"] is None
            # A cancelled observation queued behind work is never dispatched.
            cancelled = asyncio.get_running_loop().create_future()
            cancelled.cancel()
            provider._requests.put_nowait(("code_index_scheduler", {"action": "must-not-dispatch"}, cancelled))
            assert await asyncio.wait_for(provider._call_tool("memory_quality", {"rebuild_graph_fts": False}), 3) == {"schema_version": 10}
            if restore in {"stalled", "queued_stalled"}:
                assert abandoned.is_set()
            await provider.shutdown()
    finally:
        if provider._owner_task and not provider._owner_task.done():
            provider._owner_task.cancel()
            await asyncio.gather(provider._owner_task, return_exceptions=True)
        await asyncio.gather(initializing, return_exceptions=True)
    assert closed == ["session", "stdio"]


class Auth:
    is_configured = True
    def get_username_for_token(self, token):
        return token if token in {"admin", "regular"} else None
    def is_admin(self, user):
        return user == "admin"


class Request:
    def __init__(self, owner="admin", body=None, provider=None):
        from routes.auth_routes import SESSION_COOKIE
        self.cookies = {SESSION_COOKIE: owner}
        self.body = body or {}
        self.app = SimpleNamespace(state=SimpleNamespace(memory_provider=provider, mimo_supervisor=None))
    async def json(self):
        return self.body


@pytest.fixture
def routes(monkeypatch):
    import routes.auth_routes as auth
    from src import settings
    store = dict(settings.DEFAULT_SETTINGS)
    writes, prefs = [], []
    monkeypatch.setattr(auth, "_load_settings", lambda: store)  # Deliberately shared cached object.
    def save(value):
        writes.append(copy.deepcopy(value))
        store.clear()
        store.update(copy.deepcopy(value))
    monkeypatch.setattr(auth, "_save_settings", save)
    monkeypatch.setattr(auth, "_load_for_user", lambda _: {})
    monkeypatch.setattr(auth, "_save_for_user", lambda user, value: prefs.append((user, value)))
    monkeypatch.setattr(auth, "_validate_model_settings_update", lambda *args: None)
    router = auth.setup_auth_routes(Auth())
    endpoints = {m: r.endpoint for r in router.routes if r.path == "/api/auth/settings" for m in r.methods}
    return endpoints, store, writes, prefs


@pytest.mark.parametrize("owner,body,status", [
    ("regular", {THREADS_KEY: 3}, 403), ("unknown", {THREADS_KEY: 3}, 403),
    ("admin", {REVISION_KEY: 1}, 400), ("admin", {"code_index_scheduler": {}}, 400),
    ("admin", {THREADS_KEY: True, "search_provider": "changed", "tts_voice": "changed"}, 400),
    ("admin", {THREADS_KEY: "3"}, 400), ("admin", {THREADS_KEY: 2.5}, 400),
])
def test_route_rejects_complete_patch_before_any_save(routes, owner, body, status):
    endpoint, store, writes, prefs = routes
    before = copy.deepcopy(store)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(endpoint["POST"](Request(owner, body)))
    assert caught.value.status_code == status
    assert store == before and not writes and not prefs


def test_route_save_pending_then_ready_readback_and_no_duplicate_revision(routes):
    endpoints, store, writes, _ = routes
    value = asyncio.run(endpoints["POST"](Request(body={THREADS_KEY: 3})))
    assert configured(store) == (3, 1) and value["code_index_scheduler"]["effective"] is None
    asyncio.run(endpoints["POST"](Request(body={THREADS_KEY: 3})))
    assert configured(store) == (3, 1)
    assert len(writes) == 2
    visible = asyncio.run(endpoints["GET"](Request()))
    assert visible["code_index_scheduler"]["status"] == "pending"
    assert "code_index_scheduler" not in store
    assert asyncio.run(endpoints["GET"](Request("unknown"))) == {}


def test_concurrent_route_updates_keep_distinct_monotonic_revisions(routes):
    endpoints, store, writes, _ = routes
    def update(value):
        return asyncio.run(endpoints["POST"](Request(body={THREADS_KEY: value})))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(update, [3, 4]))
    assert [row[REVISION_KEY] for row in writes] == [1, 2]
    assert store[REVISION_KEY] == 2


def test_failed_save_does_not_change_shared_settings_cache(routes, monkeypatch):
    import routes.auth_routes as auth
    endpoints, store, _, prefs = routes
    before = copy.deepcopy(store)
    def failed(_value):
        raise OSError("Synthetic atomic write failure")
    monkeypatch.setattr(auth, "_save_settings", failed)
    with pytest.raises(OSError):
        asyncio.run(endpoints["POST"](Request(body={THREADS_KEY: 4, "search_provider": "changed"})))
    assert store == before and not prefs


def test_atomic_store_preserves_newer_scheduler_during_unrelated_stale_save(tmp_path, monkeypatch):
    from src import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings._invalidate_caches()
    original = dict(settings.DEFAULT_SETTINGS)
    settings.save_settings(prepare_settings(original, {THREADS_KEY: 4}))
    settings.save_settings({**original, "search_provider": "fixture"})
    assert configured(settings.load_settings()) == (4, 1)
    assert settings.load_settings()["search_provider"] == "fixture"
    before = (tmp_path / "settings.json").read_bytes()
    with pytest.raises(ValueError):
        settings.save_settings({THREADS_KEY: 3, REVISION_KEY: 1})
    assert (tmp_path / "settings.json").read_bytes() == before
    settings._invalidate_caches()


@pytest.mark.parametrize("action", ["set", "reset", "delete"])
@pytest.mark.parametrize("key", [THREADS_KEY, REVISION_KEY])
def test_agent_cannot_change_global_scheduler_or_internal_revision(monkeypatch, action, key):
    from src import settings
    from src.agent_tools.admin_tools import do_manage_settings
    monkeypatch.setattr(settings, "save_settings", lambda _: pytest.fail("Agent wrote global scheduler"))
    result = asyncio.run(do_manage_settings(json.dumps({"action": action, "key": key, "value": 3}), owner="admin"))
    assert result["exit_code"] == 1 and "administrator" in result["error"]

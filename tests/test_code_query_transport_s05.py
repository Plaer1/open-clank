"""Actual bounded Files owner + current FM child, synthetic isolated scope only."""
import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from tests.test_native_session_fork import context as fork_context

from tests import test_code_index_lifecycle as lifecycle
from tests.test_code_query_root_transport import _assert_isolated_startup
from src.openclank.code_query import query_code
from src.openclank.files_service_client import OdysseusFilesClient, close_all_clients

_assert_isolated_startup()
assert Path(os.environ["ODYSSEUS_FILES_SERVICE_BIN"]).is_file()
scope = lifecycle.scope


async def test_initial_query_observation_backoff_is_finite_cooperative_then_steady(monkeypatch):
    from src.frankenmemory_provider import FrankenmemoryProvider
    provider = FrankenmemoryProvider.__new__(FrankenmemoryProvider)
    provider._broker_url = None
    provider._scope = lambda owner: None
    polls = 0
    async def call(name, arguments):
        nonlocal polls
        if arguments["action"] == "start": return dict(token="synthetic-owned-token")
        assert arguments["action"] == "poll"
        polls += 1
        if polls <= 9: return dict(state="running")
        return dict(state="completed", result=dict(error=None, receipt={"same": "logical-head"}, cost={}))
    provider._call_tool = call
    original_sleep, delays = asyncio.sleep, []
    async def sleep(delay):
        delays.append(delay)
        await original_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    result = await provider.code_query(binding=dict(owner="alice"), query=dict(operation="catalog"))
    assert delays == [.001, .002, .004, .008, .016, .032, .064, .100, .100]
    assert result["receipt"] == {"same": "logical-head"}
    assert result["cost"]["native_rpc_calls_completed"] == 11


async def prepared(scope, tmp_path, monkeypatch, *, body=None, indexed=True, ready_timeout=10):
    root = Path(lifecycle.native_request(scope)["root"])
    (root / "source.py").write_bytes(body or "# α literal * outside declarations\ndef target():\n return 1\n".encode())
    provider, gate = await lifecycle.native_provider(scope, tmp_path, monkeypatch)
    (gate / "release").touch()
    if indexed:
        await provider.code_index_lifecycle(**lifecycle.native_request(scope))
        async def ready():
            value = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
            return value if value["lifecycle"] == "ready" else False
        try:
            async with asyncio.timeout(ready_timeout):
                while not await ready():
                    await asyncio.sleep(0.02)
        except BaseException:
            await provider.shutdown()
            raise
    return provider, root


async def read(scope, provider, *, rel="source.py", operation="read", text=None, length=100, continuation=None, start=0, auth=None):
    query = dict(operation=operation, channels=["raw", "exact"], fields=["source", "excerpt", "properties"],
        read=dict(rel_path=rel, start_byte=start, length=length), budgets=dict(bytes=8192, tokens=8192))
    if text is not None:
        query["query"] = text
    if continuation is not None:
        query["read"]["continuation"] = continuation
    return await query_code(repository=scope[0], provider=provider, auth_manager=auth or lifecycle.Auth(), owner="alice",
        workspace_id=scope[1]["alice"].id, purpose="app_folder", query=query)


async def test_actual_files_window_validates_exact_returned_unicode_bytes(scope, tmp_path, monkeypatch):
    provider, root = await prepared(scope, tmp_path, monkeypatch)
    try:
        result = await read(scope, provider, operation="search", text="literal *")
        assert result["error"] is None, result
        hit = result["hits"][0]
        assert hit["current_source_verified"] and hit["source"]["rel_path"] == "source.py"
        body = (root / "source.py").read_bytes()
        span = hit["source"]["span"]
        assert body[span["start_byte"]:span["end_byte"]].decode() == hit["excerpt"]
        assert result["cost"]["files_bytes_read"] == 2 * len(body)
        assert result["cost"]["validation_bytes_read"] == 2 * len(body)
        assert result["cost"]["source_fence_checks_completed"] >= 5
        assert str(root) not in json.dumps(result)
    finally:
        close_all_clients(); await provider.shutdown()


async def test_shutdown_owns_uncancelled_start_ack_without_poll_or_restart(scope, tmp_path, monkeypatch):
    provider, _ = await prepared(scope, tmp_path, monkeypatch)
    gate = tmp_path / "native-gate"
    (gate / "release").unlink()
    authority = lifecycle.native_request(scope, subscriber="query:shutdown-ack"); authority.pop("action")
    original = provider._call_tool
    queued, release_ack = asyncio.Event(), asyncio.Event()
    calls, issued = [], []
    async def delayed(name, arguments):
        if name == "code_query": calls.append(arguments.get("action"))
        result = await original(name, arguments)
        if name == "code_query" and arguments.get("action") == "start":
            issued.append(result["token"]); queued.set(); await release_ack.wait()
        return result
    monkeypatch.setattr(provider, "_call_tool", delayed)
    task = asyncio.create_task(provider.code_query(binding=authority, query={"operation": "catalog"}))
    shutdown = None
    try:
        await asyncio.wait_for(queued.wait(), 3)
        token = issued[0]
        await lifecycle.wait_native(lambda: (gate / (token + ".held")).exists(), "shutdown job held")
        owner = provider._owner_task
        shutdown = asyncio.create_task(provider.shutdown())
        await asyncio.sleep(0)
        assert not shutdown.done()
        release_ack.set()
        result = await asyncio.wait_for(task, 3)
        assert result["error"]["code"] == "cancelled"
        await lifecycle.wait_native(lambda: "cancel" in calls, "shutdown cancelled exact acknowledged job")
        (gate / "release").touch()
        await asyncio.wait_for(shutdown, 3)
        assert owner.done() and provider._owner_task is None
        assert calls == ["start", "cancel"]
        assert not provider._code_query_cleanups and not provider._code_query_pending_starts
    finally:
        release_ack.set(); (gate / "release").touch()
        await asyncio.gather(task, return_exceptions=True)
        if shutdown is not None: await asyncio.gather(shutdown, return_exceptions=True)
        close_all_clients(); await provider.shutdown()


async def test_missing_raw_terminal_nohit_retains_window_and_full_success(scope, tmp_path, monkeypatch):
    provider, _ = await prepared(scope, tmp_path, monkeypatch, indexed=False)
    try:
        result = await read(scope, provider, operation="search", text="not present")
        assert result["error"] is None and result["receipt"] is None and result["hits"] == []
        assert result["source_window"]["continuation"] is None
        assert result["operation_capabilities"] and result["source_window"]["end_byte"] > 0
        assert next(c for c in result["channels"] if c["channel"] == "exact")["executed"] is False
    finally:
        close_all_clients(); await provider.shutdown()


@pytest.mark.parametrize("admin", [False, True])
async def test_two_visible_locations_symlink_cannot_escape_selected_workspace(scope, tmp_path, monkeypatch, admin):
    repo, workspaces, _, _ = scope
    bob_location = workspaces["bob"].location_id
    repo.create_binding(actor_subject_id="s03-admin", binding_class="people", subject_id=lifecycle.Auth().account_id("alice"), location_id=bob_location, capabilities=("read",))
    if admin:
        users = {name: dict(value) for name, value in lifecycle.Auth.users.items()}
        users["alice"]["is_admin"] = True
        repo.sync_subjects(users)
        class Admin(lifecycle.Auth):
            def is_admin(self, owner): return owner == "alice"
        auth = Admin()
    else:
        auth = lifecycle.Auth()
    provider, root = await prepared(scope, tmp_path, monkeypatch, indexed=False)
    secret = Path(lifecycle.native_request(scope, owner="bob")["root"]) / "outside.txt"
    secret.write_text("PRIVATE OTHER WORKSPACE", encoding="utf8")
    (root / "escape.txt").symlink_to(secret)
    try:
        positive = await read(scope, provider, auth=auth)
        assert positive["error"] is None, positive
        denied = await read(scope, provider, rel="escape.txt", auth=auth)
        assert denied["error"] is not None and not denied.get("hits")
        assert "PRIVATE OTHER WORKSPACE" not in json.dumps(denied)
    finally:
        close_all_clients(); await provider.shutdown()


@pytest.mark.parametrize("mode", ["large_erase", "missing_erase_admit", "grant_expiry"])
async def test_actual_inter_read_fence_blocks_second_files_acquisition(scope, tmp_path, monkeypatch, mode):
    provider, _ = await prepared(scope, tmp_path, monkeypatch, body=b"x" * 1048577 if mode == "large_erase" else None, indexed=mode == "large_erase")
    original = OdysseusFilesClient.request_handle
    acquisitions = []
    async def delayed(client, operation, handle, payload=None):
        result = await original(client, operation, handle, payload)
        acquisitions.append(operation)
        if len(acquisitions) == 1:
            if mode == "grant_expiry":
                # Synthetic canonical expiry, deliberately NO generation bump:
                # per-operation current grant validation must still reject.
                with sqlite3.connect(scope[0].db_path) as db:
                    db.execute("UPDATE file_policy_bindings SET expires_unix_ms=? WHERE id=?", (int(time.time() * 1000) - 1, scope[2]["alice", "people"].id))
            else:
                await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="remove"))
                if mode == "missing_erase_admit":
                    # Admission can queue an index but the existing epoch is
                    # already different before any successor scan is run.
                    request = lifecycle.native_request(scope, action="admit")
                    (tmp_path / "native-gate" / "release").unlink()
                    await provider.code_index_lifecycle(**request)
                    await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="cancel"))
                    (tmp_path / "native-gate" / "release").touch()
        return result
    monkeypatch.setattr(OdysseusFilesClient, "request_handle", delayed)
    try:
        result = await read(scope, provider, length=20)
        assert result["error"] is not None and not result.get("hits"), result
        if mode == "missing_erase_admit":
            assert result["error"]["code"] == "stale_cursor"
        assert acquisitions == ["read_range"]
        assert result["cost"]["files_bytes_read"] > 0
    finally:
        (tmp_path / "native-gate" / "release").touch()
        close_all_clients(); await provider.shutdown()


async def test_actual_missing_continuation_rejects_between_page_erase_admit_before_files(scope, tmp_path, monkeypatch):
    provider, _ = await prepared(scope, tmp_path, monkeypatch, indexed=False)
    try:
        first = await read(scope, provider, length=10)
        assert first["error"] is None and first["receipt"] is None
        progress = first["source_window"]
        await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="remove"))
        (tmp_path / "native-gate" / "release").unlink()
        await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="admit"))
        await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="cancel"))
        (tmp_path / "native-gate" / "release").touch()
        opens = []
        original = OdysseusFilesClient.open_handle
        async def counted(client, path):
            opens.append(path)
            return await original(client, path)
        monkeypatch.setattr(OdysseusFilesClient, "open_handle", counted)
        second = await read(scope, provider, length=10, start=progress["end_byte"], continuation=progress["continuation"])
        assert second["error"]["code"] == "stale_cursor" and not second.get("hits")
        assert opens == []
    finally:
        (tmp_path / "native-gate" / "release").touch()
        close_all_clients(); await provider.shutdown()


async def test_actual_validation_stale_cost_counts_successful_early_acquisition(scope, tmp_path, monkeypatch):
    provider, root = await prepared(scope, tmp_path, monkeypatch)
    invoke = provider.code_query
    async def mutate_before_validation(**kwargs):
        if kwargs.get("source_claim"):
            (root / "source.py").write_bytes(b"# changed after bounded Files observation\ndef target(): return 2\n")
        return await invoke(**kwargs)
    monkeypatch.setattr(provider, "code_query", mutate_before_validation)
    try:
        result = await read(scope, provider)
        assert result["error"] is None, result
        hit = result["hits"][0]
        assert not hit["current_source_verified"] and "source" not in hit
        assert hit["evidence_state"] == "stale"
        assert result["cost"]["validation_bytes_read"] == (root / "source.py").stat().st_size
        assert result["cost"]["files_bytes_read"] > 0
    finally:
        close_all_clients(); await provider.shutdown()


async def test_actual_managed_callback_cancel_propagates_to_matching_native_job(scope, fork_context, tmp_path, monkeypatch):
    provider, root = await prepared(scope, tmp_path, monkeypatch)
    sessions, worker, _ = fork_context
    bridge = worker._bridge
    from core.database import Session
    with sessions() as db:
        row = db.query(Session).filter_by(id="chat").one()
        state = dict(row.mimo_state)
        state["managed_binding"] = {**state["managed_binding"], "authorityWorkspaceID": scope[1]["alice"].id, "physicalCwd": str(root)}
        row.mimo_state = state; db.commit()
    bridge._memory_provider = provider
    bridge._session_context["ses_source"].update(memory_read_allowed=True, incognito=False, auxiliary=False)
    from types import SimpleNamespace
    import core.models
    session = SimpleNamespace(id="chat", owner="alice", workspace_id=scope[1]["alice"].id, incognito=False, archived=False)
    monkeypatch.setattr(core.models, "get_session_manager_instance", lambda: SimpleNamespace(get_session=lambda sid: session if sid == "chat" else None))
    import routes.prefs_routes
    monkeypatch.setattr(routes.prefs_routes, "_load_for_user", lambda owner: {"code_context_enabled": True})
    request = dict(sessionID="ses_source", bindingRevision=0, requestID="b" * 32, requestFingerprint="a" * 64,
        query="Explain function target", availableByteTokens=2048, retainedMessages=[{"role": "user", "content": "Explain function target"}])
    (tmp_path / "native-gate" / "release").unlink()
    task = asyncio.create_task(bridge._handle_code_context_read(request))
    try:
        gate = tmp_path / "native-gate"
        async def held():
            matches = list(gate.glob("query_*.held"))
            return matches[0].name.removesuffix(".held") if matches else False
        token = await lifecycle.wait_native(held, "actual context query held")
        cancelled = await bridge._handle_code_context_read({**request, "action": "cancel", "query": "", "retainedMessages": [], "availableByteTokens": 0})
        assert cancelled["reason"] == "cancelled"
        assert (await task)["reason"] == "cancelled" and bridge._code_context_requests == {}
        authority = lifecycle.native_request(scope, subscriber="query:chat"); authority.pop("action")
        reply = await provider._call_tool("code_query", {"action": "poll", "binding": authority, "token": token})
        assert reply["state"] in {"failed", "cancelled"} and reply["error"]["code"] == "cancelled" and "result" not in reply
        (gate / "release").touch()
        await lifecycle.wait_native(lambda: (gate / (token + ".finished")).exists(), "actual cancelled context worker returned")
    finally:
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        (tmp_path / "native-gate" / "release").touch()
        close_all_clients(); await provider.shutdown()


async def test_large_corpus_query_and_ordinary_recall_share_real_owner_responsively(scope, tmp_path, monkeypatch):
    root = Path(lifecycle.native_request(scope)["root"])
    for index in range(512):
        (root / f"unit_{index:04}.py").write_text(f"def workload_{index}():\n    return {index}\n", encoding="utf8")
    provider, _ = await prepared(scope, tmp_path, monkeypatch, ready_timeout=120)
    authority = lifecycle.native_request(scope, subscriber="query:large-fixture"); authority.pop("action")
    try:
        query_started = time.perf_counter()
        query_task = asyncio.create_task(provider.code_query(binding=authority, query={"operation": "search", "query": "workload_511", "fields": ["label"], "budgets": {"rows": 1, "bytes": 8192, "tokens": 8192, "work": 100000}}))
        # Both calls use exactly the same provider owner queue. No second MCP
        # process or artificial parse gate makes the recall result possible.
        await asyncio.sleep(0)
        recall_started = time.perf_counter()
        recalled = await asyncio.wait_for(provider.recall("nothing stored", owner="alice"), 3)
        recall_ms = (time.perf_counter() - recall_started) * 1000
        pending_when_recall_returned = not query_task.done()
        result = await query_task
        assert recalled == [] and result["error"] is None
        assert result["cost"]["metadata_bytes"] > 1024 * 1024
        assert result["hits"] and result["hits"][0]["label"] == "workload_511"
        print(json.dumps({"s05_large_owner_observation": {"files": 513, "metadata_bytes": result["cost"]["metadata_bytes"], "metadata_records_validated": result["cost"]["metadata_records_validated"], "native_rpc_calls_completed": result["cost"]["native_rpc_calls_completed"], "recall_ms": round(recall_ms, 3), "query_total_ms": round((time.perf_counter()-query_started)*1000, 3), "query_pending_on_recall_return": pending_when_recall_returned}}))
    finally:
        close_all_clients(); await provider.shutdown()


@pytest.mark.parametrize("repeat_and_shutdown", [False, True])
async def test_cancel_before_actual_start_ack_owns_late_token_and_cleanup(scope, tmp_path, monkeypatch, repeat_and_shutdown):
    provider, _ = await prepared(scope, tmp_path, monkeypatch)
    gate = tmp_path / "native-gate"
    (gate / "release").unlink()
    authority = lifecycle.native_request(scope, subscriber="query:late-ack"); authority.pop("action")
    original = provider._call_tool
    queued, release_ack = asyncio.Event(), asyncio.Event()
    issued = []
    cancelled = []
    async def delayed(name, arguments):
        result = await original(name, arguments)
        if name == "code_query" and arguments.get("action") == "start":
            issued.append(result["token"]); queued.set(); await release_ack.wait()
        if name == "code_query" and arguments.get("action") == "cancel":
            cancelled.append(arguments["token"])
        return result
    monkeypatch.setattr(provider, "_call_tool", delayed)
    task = asyncio.create_task(provider.code_query(binding=authority, query={"operation": "catalog"}))
    shutdown = None
    try:
        await asyncio.wait_for(queued.wait(), 3)
        token = issued[0]
        await lifecycle.wait_native(lambda: (gate / (token + ".held")).exists(), "late-ack native job held")
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "first cancellation waits for its owned late ACK"
        if repeat_and_shutdown:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert provider._code_query_cleanups
            shutdown = asyncio.create_task(provider.shutdown())
            await asyncio.sleep(0)
            shutdown.cancel()
            with pytest.raises(asyncio.CancelledError): await shutdown
            assert provider._code_query_cleanups, "cancelled shutdown must not cancel token cleanup"
        release_ack.set()
        if not repeat_and_shutdown:
            with pytest.raises(asyncio.CancelledError): await task
        async def sent(): return cancelled == [token]
        await lifecycle.wait_native(sent, "exact late token cancellation completed")
        reply = await original("code_query", {"action": "poll", "binding": authority, "token": token})
        assert reply["state"] == "failed" and reply["error"]["code"] == "cancelled" and "result" not in reply
        (gate / "release").touch()
        await lifecycle.wait_native(lambda: (gate / (token + ".finished")).exists(), "late-ack cancelled worker actually returned")
        assert not provider._code_query_cleanups and not provider._code_query_start_cleanups
    finally:
        release_ack.set(); (gate / "release").touch()
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        close_all_clients(); await provider.shutdown()

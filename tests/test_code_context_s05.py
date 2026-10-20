"""Request-retained C05 cards, no provider inference or archival history."""
import json
import asyncio
from types import SimpleNamespace

import pytest
from src.openclank import code_context as cc
from tests.test_native_session_fork import context as fork_context


def fixture(monkeypatch):
    calls = []
    source = dict(file_id="f" * 64, rel_path="source.py", source_hash="a" * 64,
        span=dict(start_byte=0, end_byte=len("def target():\n pass\n".encode()), start_line=1, end_line=2))
    async def query(**kw):
        calls.append(kw)
        if kw["query"]["operation"] == "search":
            return dict(receipt={"fixture": "receipt"}, hits=[dict(kind="declaration", evidence_state="exact", source=source, properties={}, matches=[{"channel": "exact"}])])
        return dict(receipt={"fixture": "receipt"}, hits=[dict(current_source_verified=True, source=source, excerpt="def target():\n pass\n")])
    monkeypatch.setattr(cc, "query_code", query)
    args = dict(messages=[dict(role="user", content="Explain function target")], message="Explain function target",
        session=SimpleNamespace(id="chat", owner="alice", workspace_id="workspace", archived=False), provider=object(),
        repository=object(), auth_manager=object(), prefs={"code_context_enabled": True}, owner="alice", context_length=32000)
    return args, calls, source


async def test_verified_card_near_latest_user_and_actual_retained_dedup_reset(monkeypatch):
    args, calls, source = fixture(monkeypatch)
    first = await cc.append_code_context(**args)
    assert len(first) == 2 and first[0]["role"] == "user" and cc.SENTINEL in first[0]["content"]
    assert args["messages"] == [dict(role="user", content="Explain function target")]
    again = await cc.append_code_context(**{**args, "messages": first})
    assert again is first
    reset = await cc.append_code_context(**args)
    assert len(reset) == 2
    manual = [dict(role="tool", content=json.dumps({"schema": "open-clank.code-query/v1", "receipt": {"fixture": "receipt"}, "hits": [{"id": "file:" + source["file_id"], "current_source_verified": True, "source": source, "excerpt": "def target():\n pass\n"}]})), args["messages"][0]]
    assert await cc.append_code_context(**{**args, "messages": manual}) is manual
    assert all(c["purpose"] == "agent_workspace" and c["session"] is args["session"] for c in calls)


@pytest.mark.parametrize("change", [dict(prefs={}), dict(prefs={"code_context_enabled": "true"}), dict(incognito=True), dict(use_memory=False), dict(message="hello"), dict(context_length=100), dict(owner="bob"), dict(prefs={"code_context_enabled": True, "memory_enabled": False})])
async def test_default_off_authority_signal_and_remaining_capacity_skip_query(monkeypatch, change):
    args, calls, _ = fixture(monkeypatch)
    assert await cc.append_code_context(**{**args, **change}) is args["messages"]
    assert calls == []


async def test_stale_source_never_auto_injected(monkeypatch):
    args, _, _ = fixture(monkeypatch)
    async def stale(**kw):
        return {"hits": [{"kind": "declaration", "evidence_state": "stale", "matches": [{"channel": "exact"}]}]}
    monkeypatch.setattr(cc, "query_code", stale)
    assert await cc.append_code_context(**args) is args["messages"]


@pytest.fixture
def managed(fork_context, monkeypatch):
    sessions, worker, _ = fork_context
    from core.database import Session
    with sessions() as db:
        row = db.query(Session).filter_by(id="chat").one()
        state = dict(row.mimo_state)
        state["managed_binding"] = {**state["managed_binding"], "authorityWorkspaceID": "workspace"}
        row.mimo_state = state
        db.commit()
    bridge = worker._bridge
    bridge._memory_provider = object()
    bridge._session_context["ses_source"].update(memory_read_allowed=True, incognito=False, auxiliary=False)
    session = SimpleNamespace(id="chat", owner="alice", workspace_id="workspace", incognito=False, archived=False)
    import core.models
    monkeypatch.setattr(core.models, "get_session_manager_instance", lambda: SimpleNamespace(get_session=lambda sid: session if sid == "chat" else None))
    import routes.prefs_routes
    monkeypatch.setattr(routes.prefs_routes, "_load_for_user", lambda owner: {"code_context_enabled": True})
    import src.tool_execution
    monkeypatch.setattr(src.tool_execution, "_read_only_auth_snapshot", lambda path: object())
    request = dict(sessionID="ses_source", bindingRevision=0, requestID="b" * 32, requestFingerprint="a" * 64,
        query="Explain function target", availableByteTokens=2048, retainedMessages=[{"role": "user", "content": "Explain function target"}])
    return bridge, session, request


async def test_actual_managed_callback_uses_current_binding_and_retained_request(managed, monkeypatch):
    bridge, _, request = managed
    fixture(monkeypatch)
    result = await bridge._handle_code_context_read(request)
    assert result["card"] and cc.SENTINEL in result["card"]
    assert result["requestID"] == request["requestID"] and result["requestFingerprint"] == request["requestFingerprint"]
    assert bridge._code_context_requests == {}
    request["retainedMessages"].insert(0, {"role": "user", "content": result["card"]})
    assert (await bridge._handle_code_context_read({**request, "requestID": "c" * 32}))["card"] is None
    request["retainedMessages"] = [{"role": "user", "content": "Explain function target"}]
    assert (await bridge._handle_code_context_read({**request, "requestID": "d" * 32}))["card"]


@pytest.mark.parametrize("flag", ["incognito", "auxiliary", "memory_read_allowed"])
async def test_actual_managed_callback_rechecks_late_context_flags(managed, monkeypatch, flag):
    bridge, _, request = managed
    _, _, source = fixture(monkeypatch)
    async def query(**kw):
        if kw["query"]["operation"] == "search":
            return {"hits": [dict(kind="declaration", evidence_state="exact", source=source, properties={}, matches=[{"channel": "exact"}])]}
        bridge._session_context["ses_source"][flag] = flag != "memory_read_allowed"
        return {"hits": [dict(current_source_verified=True, source=source, excerpt="def target():\n pass\n")]}
    monkeypatch.setattr(cc, "query_code", query)
    assert (await bridge._handle_code_context_read(request))["card"] is None


async def test_managed_cancel_nonce_before_binding_resolves_cleans_owned_task(managed, monkeypatch):
    bridge, _, request = managed
    original = bridge._handle_session_binding_read
    entered = asyncio.Event()
    async def binding(params):
        if not entered.is_set():
            entered.set()
            await asyncio.Event().wait()
        return await original(params)
    monkeypatch.setattr(bridge, "_handle_session_binding_read", binding)
    task = asyncio.create_task(bridge._handle_code_context_read(request))
    await entered.wait()
    wrong = {**request, "action": "cancel", "requestID": "c" * 32, "retainedMessages": [], "query": "", "availableByteTokens": 0}
    await bridge._handle_code_context_read(wrong)
    assert not task.done()
    await bridge._handle_code_context_read({**wrong, "requestID": request["requestID"]})
    assert (await task)["reason"] == "cancelled"
    assert bridge._code_context_requests == {}
    # An old cancel for identical contents cannot kill a later nonce.
    async def hold(_):
        await asyncio.Event().wait()
    monkeypatch.setattr(bridge, "_handle_session_binding_read", original)
    monkeypatch.setattr(bridge, "_run_code_context_read", hold)
    successor = asyncio.create_task(bridge._handle_code_context_read({**request, "requestID": "d" * 32}))
    await asyncio.sleep(0)
    await bridge._handle_code_context_read({**wrong, "requestID": request["requestID"]})
    assert not successor.done()
    await bridge._cancel_code_context_requests("ses_source")
    assert (await successor)["reason"] == "cancelled" and bridge._code_context_requests == {}


async def test_real_acp_dispatch_orders_read_registration_before_immediate_cancel(managed, monkeypatch):
    bridge, _, request = managed
    from src.openclank.acp_client import ACPClient
    client = ACPClient(None, None)
    client.register_callback("_openclank/session/v1/code-context/read", bridge._handle_code_context_read)
    replies = []
    async def write(reply): replies.append(reply)
    monkeypatch.setattr(client, "_write", write)
    original = bridge._handle_session_binding_read
    count = 0
    async def binding(params):
        nonlocal count
        count += 1
        if count == 1:
            await asyncio.Event().wait()
        return await original(params)
    monkeypatch.setattr(bridge, "_handle_session_binding_read", binding)
    method = "_openclank/session/v1/code-context/read"
    await client._dispatch({"id": 1, "method": method, "params": request})
    await client._dispatch({"id": 2, "method": method, "params": {**request, "action": "cancel", "query": "", "retainedMessages": [], "availableByteTokens": 0}})
    await asyncio.gather(*list(client._callback_tasks))
    assert {reply["id"] for reply in replies} == {1, 2}
    assert all(reply["result"]["reason"] == "cancelled" for reply in replies)
    assert bridge._code_context_requests == {}


@pytest.mark.parametrize("part", [{"type": "image_url", "image_url": {"url": "https://not-fetched.invalid/image"}}, {"type": "image", "image": "opaque"}, {"type": "input_audio", "input_audio": {"data": "opaque"}}, {"type": "file", "data": "opaque"}, {"type": "unrecognized-media"}])
async def test_unknown_media_cost_skips_auto_source_queries(monkeypatch, part):
    args, calls, _ = fixture(monkeypatch)
    args["messages"] = [{"role": "user", "content": [{"type": "text", "text": args["message"]}, part]}]
    assert await cc.append_code_context(**args) is args["messages"]
    assert calls == []


async def test_normal_chatprocessor_media_guard_and_plain_tool_metadata(monkeypatch):
    args, calls, _ = fixture(monkeypatch)
    from src.chat_processor import ChatProcessor
    import src.openclank.file_policy
    monkeypatch.setattr(src.openclank.file_policy, "FilePolicyRepository", lambda: object())
    processor = ChatProcessor.__new__(ChatProcessor)
    processor.memory_provider = object()
    processor._trust_prefs = lambda owner: {"code_context_enabled": True}
    images = [{"role": "user", "content": [{"type": "text", "text": args["message"]}, {"type": "image_url", "image_url": {"url": "https://not-fetched.invalid/image"}}]}]
    assert await processor.append_code_context(images, message=args["message"], session=args["session"], owner="alice", context_length=32000, auth_manager=object()) is images
    assert calls == []
    metadata = [{"role": "tool", "content": [{"type": "tool-result", "output": {"type": "json", "value": {"type": "file", "filename": "image.png", "mime_type": "image/png"}}}]}, args["messages"][0]]
    assert len(await cc.append_code_context(**{**args, "messages": metadata})) == len(metadata) + 1
    assert len(calls) == 2

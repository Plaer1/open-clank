"""Question ownership and transport terminal paths with isolated synthetic peers."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src.openclank.acp_bridge import ACPBridge, PendingQuestion, QuestionHandler
from src.openclank.acp_client import ACPClient


def pending(session="native-a", request="q1", turn="turn-a"):
    return PendingQuestion(mimo_request_id=request, mimo_session_id=session,
        odysseus_session_id="chat-a", owner="alice", turn_id=turn,
        revision=2, plan_revision=3, questions=[{"question": "Choose", "options": [
            {"label": "A"}], "multiple": False, "custom": False}])


def bridge_state(req):
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._session_state = {req.mimo_session_id: {"pending_question": req.payload()}}
    bridge._session_map = {"chat-a": req.mimo_session_id}
    bridge.question_handler = QuestionHandler()
    writes = []
    bridge._persist_session_state = lambda session: writes.append(session)
    return bridge, writes


async def test_cancelled_waiter_still_clears_and_persists_matching_question():
    req = pending()
    bridge, writes = bridge_state(req)
    waiter = asyncio.create_task(req.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await bridge._clear_question(req)
    assert "pending_question" not in bridge._session_state[req.mimo_session_id]
    assert writes == [req.mimo_session_id]


async def test_old_question_cleanup_cannot_remove_or_approve_new_question():
    old = pending()
    newer = pending(request="q2")
    bridge, writes = bridge_state(newer)
    await bridge._clear_question(old)
    assert bridge._session_state[old.mimo_session_id]["pending_question"] == newer.payload()
    assert writes == []


class Writer:
    def __init__(self): self.closed = 0
    def write(self, _data): pass
    async def drain(self): pass
    def close(self): self.closed += 1


@pytest.mark.parametrize("terminal", ["eof", "error", "close"])
async def test_transport_terminal_cleans_waiting_callback_and_writer_once(terminal):
    reader, writer = asyncio.StreamReader(), Writer()
    client = ACPClient(reader, writer)
    entered, cleaned = asyncio.Event(), asyncio.Event()
    async def callback(_params):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.set()
    client.register_callback("_odysseus/question", callback)
    await client.start_reader()
    reader.feed_data(json.dumps({"id": 1, "method": "_odysseus/question", "params": {}}).encode() + b"\n")
    await asyncio.wait_for(entered.wait(), 1)
    if terminal == "eof": reader.feed_eof()
    if terminal == "error": reader.set_exception(OSError("synthetic reader failure"))
    if terminal != "close": await asyncio.wait_for(client._close_event.wait(), 1)
    await asyncio.gather(client.close(), client.close())
    assert cleaned.is_set()
    assert not client._callback_tasks
    assert writer.closed == 1


async def test_turn_rejection_only_settles_matching_session_and_turn():
    handler = QuestionHandler()
    old, newer, other = pending(), pending(request="q2", turn="turn-b"), pending(session="native-b")
    handler.pending_requests = {req.request_id: req for req in (old, newer, other)}
    await handler.reject_session("native-a", turn_id="turn-a")
    assert await old.wait() == {"rejected": True}
    assert not newer._future.done()
    assert not other._future.done()


async def test_negotiated_state_hides_dead_persisted_question():
    req = pending()
    bridge, _writes = bridge_state(req)
    assert "pending_question" not in bridge.negotiated_state("chat-a")
    bridge.question_handler.pending_requests[req.request_id] = req
    assert bridge.negotiated_state("chat-a")["pending_question"] == req.payload()
    req.resolve({"rejected": True})
    assert "pending_question" not in bridge.negotiated_state("chat-a")


@pytest.mark.parametrize("answers", [None, [], [[]], [["A", "B"]], [[{}]], [["x" * 2049]], [["invalid"]]])
async def test_invalid_answer_is_retryable_and_corrected_reply_is_one_use(answers):
    handler = QuestionHandler()
    handler.set_context_resolver(lambda _session: {"incognito": True})
    req = pending()
    handler.pending_requests[req.request_id] = req
    args = dict(owner="alice", session_id="chat-a")
    assert handler.resolve_result(req.request_id, answers=answers, **args) == "invalid_answer"
    assert not req._future.done()
    assert handler.resolve_result(req.request_id, answers=[["A"]], **args) == "accepted"
    assert handler.resolve_result(req.request_id, answers=[["A"]], **args) == "stale"


async def test_reply_endpoint_distinguishes_invalid_and_stale(monkeypatch):
    import routes.chat_routes as routes
    from fastapi import HTTPException
    handler = QuestionHandler()
    handler.set_context_resolver(lambda _session: {"incognito": True})
    req = pending()
    handler.pending_requests[req.request_id] = req
    monkeypatch.setattr(routes, "_verify_session_owner", lambda *_args: None)
    router = routes.setup_chat_routes(*([None] * 6))
    endpoint = next(route.endpoint for route in router.routes if getattr(route, "path", "") == "/api/session/{session_id}/question")
    body = {"request_id": req.request_id, "answers": [["A", "B"]]}
    async def read_json(): return body
    request = SimpleNamespace(state=SimpleNamespace(current_user="alice"), json=read_json,
        app=SimpleNamespace(state=SimpleNamespace(mimo_supervisor=SimpleNamespace(question_handler=handler))))
    with pytest.raises(HTTPException) as error:
        await endpoint(request, "chat-a")
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "question_invalid_answer"
    body["answers"] = [["A"]]
    assert await endpoint(request, "chat-a") == {"ok": True}
    with pytest.raises(HTTPException) as error:
        await endpoint(request, "chat-a")
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "question_stale"


async def test_real_turn_stop_releases_question_before_new_prompt(monkeypatch, tmp_path):
    import src.openclank.acp_bridge as module
    from unittest.mock import AsyncMock
    monkeypatch.setattr(module, "odysseus_mcp_descriptors", lambda **_kwargs: ([], []))
    monkeypatch.setattr("src.permission_mode.suppresses_questions", lambda _owner: False)
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._session_map, bridge._session_context, bridge._session_state = {}, {}, {}
    bridge._turns, bridge._queues, bridge._session_models = {}, {}, {}
    bridge._owner, bridge._cwd = "alice", str(tmp_path)
    bridge._memory_provider = bridge._managed_provider_context = bridge._delete_session_callback = None
    bridge._logging_callbacks = None
    bridge._persist_session_state = lambda *_args, **_kwargs: None
    bridge.mapped_session_workspace = lambda _chat: str(tmp_path)
    bridge.set_config_option = AsyncMock(return_value={})
    bridge._cancel_code_context_requests = AsyncMock()
    async def open_session(**_kwargs):
        bridge._session_state["native-a"] = {}
        return "native-a"
    bridge.open_session = open_session
    bridge.question_handler = QuestionHandler()
    bridge.question_handler.set_context_resolver(lambda native: bridge._session_context[native])
    bridge.question_handler.on_request(bridge._surface_question)
    bridge.question_handler.on_resolved(bridge._clear_question)
    callbacks = []
    async def prompt(native, _parts, **_kwargs):
        task = asyncio.create_task(bridge.question_handler.handle({"sessionId": native,
            "requestId": str(len(callbacks)), "questions": [{"question": "Choose", "options": [{"label": "A"}]}]}))
        callbacks.append(task)
        return await asyncio.shield(task)
    bridge._client = SimpleNamespace(prompt=prompt, cancel=AsyncMock())
    for turn in ("turn-one", "turn-two"):
        stream = bridge.run_turn("chat-a", [{"role": "user", "content": "hi"}], owner="alice",
            turn_envelope={"incognito": True, "root_operation_id": turn})
        update = await asyncio.wait_for(anext(stream), 2)
        assert "ask_user" in update
        await stream.aclose()
        assert await asyncio.wait_for(callbacks[-1], 1) == {"rejected": True}
        assert not bridge.question_handler.pending_requests
    assert bridge._client.cancel.await_count == 2

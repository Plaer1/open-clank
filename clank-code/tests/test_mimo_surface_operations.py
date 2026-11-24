"""Bounded owner routes exercise real FastAPI route wiring without inference."""
import asyncio
from types import SimpleNamespace
import pytest
from fastapi import HTTPException, APIRouter
import routes.session_routes as routes

class Reply:
    status_code = 200
    def __init__(self, value): self.value = value
    def json(self): return self.value

@pytest.fixture
def controls(monkeypatch):
    session = SimpleNamespace(id="chat", owner="alice", provider_model_route_id="route", endpoint_id="provider")
    manager = SimpleNamespace(get_session=lambda sid: session)
    monkeypatch.setattr(routes, "_verify_session_owner", lambda request, sid, *args: None if sid == "chat" else (_ for _ in ()).throw(HTTPException(404)))
    monkeypatch.setattr(routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(routes, "_reject_compact_during_active_run", lambda sid: None)
    monkeypatch.setattr(routes, "resolve_chat_route", lambda **kwargs: SimpleNamespace(connection_id="provider", provider_model_id="model"))
    monkeypatch.setattr(routes, "router", APIRouter(prefix="/api"))
    router = routes.setup_session_routes(manager, {})
    endpoint = next(item.endpoint for item in router.routes if item.path == "/api/session/{sid}/mimo-operation")
    calls = []; replies = []
    async def proxy(sid, method, suffix, **kwargs):
        calls.append((sid, method, suffix, kwargs)); return Reply(replies.pop(0))
    def request(body):
        async def json(): return body
        return SimpleNamespace(json=json, app=SimpleNamespace(state=SimpleNamespace(mimo_supervisor=SimpleNamespace(session_http_request=proxy))))
    return endpoint, request, calls, replies

def test_compact_calls_native_owner_with_canonical_model(controls):
    endpoint, request, calls, replies = controls; replies.append(True)
    assert asyncio.run(endpoint(request({"operation": "compact"}), "chat"))["result"] is True
    assert calls[0][1:3] == ("POST", "summarize")
    assert calls[0][3]["payload"] == {"providerID": "provider", "modelID": "model", "auto": False}

def test_btw_does_not_mutate_host_history_or_require_idle(controls, monkeypatch):
    endpoint, request, calls, replies = controls; replies.append({"answer": "answer"})
    monkeypatch.setattr(routes, "_reject_compact_during_active_run", lambda sid: pytest.fail("side query must remain available"))
    result = asyncio.run(endpoint(request({"operation": "btw", "question": "  exact question  "}), "chat"))
    assert result["result"]["answer"] == "answer"
    assert calls[0][2] == "ask" and calls[0][3]["payload"]["question"] == "  exact question  "

def test_recover_requires_explicit_current_candidate(controls):
    endpoint, request, calls, replies = controls
    replies.extend([[{"assistantMessageID": "msg_abc"}], {"resumed": True}])
    assert asyncio.run(endpoint(request({"operation": "recover", "assistantMessageID": "msg_abc"}), "chat"))["result"]["resumed"] is True
    assert [item[2] for item in calls] == ["recovery", "turn/msg_abc/resume"]
    replies.append([])
    with pytest.raises(HTTPException) as exc: asyncio.run(endpoint(request({"operation": "recover", "assistantMessageID": "msg_missing"}), "chat"))
    assert exc.value.status_code == 409

def test_operations_reject_other_owner_and_unbounded_config(controls):
    endpoint, request, calls, replies = controls
    for sid, body, status in [("other", {"operation": "compact"}, 404), ("chat", {"operation": "config"}, 400), ("chat", {"operation": "compact", "providerID": "other"}, 400)]:
        with pytest.raises(HTTPException) as exc: asyncio.run(endpoint(request(body), sid))
        assert exc.value.status_code == status
    assert calls == []


def test_restore_draft_validates_upload_owner_without_binary_read(controls, monkeypatch):
    endpoint, request, calls, replies = controls
    import inspect
    manager = next(cell.cell_contents for name, cell in zip(endpoint.__code__.co_freevars, endpoint.__closure__) if name == "session_manager")
    manager.get_session = lambda sid, **kwargs: SimpleNamespace(provider_model_route_id="route")
    received = []
    class Uploads:
        def resolve_upload(self, identifier, **kwargs):
            received.append((identifier, kwargs)); return object() if identifier == "present" else None
    closure = dict(zip(endpoint.__code__.co_freevars, endpoint.__closure__))
    closure["upload_handler"].cell_contents = Uploads()
    async def control(*args, **kwargs): return {"restore_draft": {"raw_content": '  {"json":"prompt"}  ', "content_kind": "text", "attachment_refs": [{"attachment_id": "present"}, {"attachment_id": "missing"}]}}
    req = request({"operation": "undo", "expected_transcript_revision": 0})
    req.app.state.mimo_supervisor.session_history_operation = control
    draft = asyncio.run(endpoint(req, "chat"))["result"]["restore_draft"]
    assert draft["raw_content"] == '  {"json":"prompt"}  '
    assert draft["available"] is False and draft["unavailable_attachment_ids"] == ["missing"]
    assert received == [("present", {"owner": "alice", "allow_admin": False}), ("missing", {"owner": "alice", "allow_admin": False})]

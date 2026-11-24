"""O08 native idle resume retires the prior Browser turn policy first."""

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routes.orchestration_routes import setup_orchestration_routes
from src.openclank.session_map import OwnerSessionMap


class _BrowserService:
    def __init__(self, events):
        self.events = events

    def retire_chat_turn_policies(self, *, owner, host_session_id):
        self.events.append(("retire", owner, host_session_id))


class _Supervisor:
    def __init__(self, root, events):
        self.root = root
        self.events = events

    def _runtime_home(self, _owner):
        return self.root

    async def session_http_request(self, chat_id, method, suffix, *, owner, payload=None):
        self.events.append(("native", method, suffix))
        if suffix == "actors":
            value = [{"sessionID": "engine-a", "actorID": "main", "status": "idle",
                      "executionRevision": 7, "hostContext": {"chatID": "chat-a"},
                      "effectiveModel": {"providerID": "fixture", "modelID": "model-a"}}]
        elif suffix == "message?limit=1000&agent_id=main":
            value = []
        elif suffix == "actors/main/input" and method == "GET":
            value = []
        elif suffix == "actors/main/input" and method == "POST":
            value = [{"requestID": payload["requestID"], "sessionID": "engine-a", "actorID": "main",
                      "text": payload["text"], "attachmentRefs": [], "attachmentUploadIDs": [],
                      "questionRefs": [], "questionAnswers": [],
                      "modelRequestOptions": payload["modelRequestOptions"],
                      "mode": payload["mode"], "state": "pending-boundary"}]
        elif suffix.endswith("/resume") and method == "POST":
            assert any(event[0] == "retire" for event in self.events)
            value = {"kind": "existing-claim"}
        else:
            return httpx.Response(404, json={"error": "unexpected native path"})
        return httpx.Response(200, json=value)


def test_idle_main_native_resume_retires_browser_policy_before_post(tmp_path, monkeypatch):
    from routes import orchestration_routes
    from src.openclank import browser_control_service

    events = []
    home = tmp_path / "alice"
    home.mkdir()
    owner_map = OwnerSessionMap(home / "session-map.json", "alice")
    owner_map.bind("chat-a", "engine-a")
    supervisor = _Supervisor(home, events)
    app = FastAPI()
    app.state.mimo_supervisor = supervisor

    @app.middleware("http")
    async def authenticated(request: Request, call_next):
        request.state.authenticated = True
        return await call_next(request)

    monkeypatch.setattr(orchestration_routes, "effective_user", lambda _request: "alice")
    monkeypatch.setattr(browser_control_service, "configured_browser_control_service", lambda: _BrowserService(events))
    app.include_router(setup_orchestration_routes(
        object(), owner_verifier=lambda *_args: None,
        option_resolver=lambda *_args: {"present": False},
    ))

    with TestClient(app) as client:
        response = client.post("/api/orchestration/chat-a/actors/main/input?map_revision=1", json={
            "requestID": "request-after-turn", "text": "continue", "mode": "after-turn", "handoffID": "handoff-1",
        })
    assert response.status_code == 200, response.text
    retired = next(index for index, event in enumerate(events) if event[0] == "retire")
    resumed = next(index for index, event in enumerate(events) if event[0] == "native" and event[1] == "POST" and event[2].endswith("/resume"))
    assert events[retired] == ("retire", "alice", "chat-a")
    assert retired < resumed

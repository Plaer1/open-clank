"""Real FastAPI boundary fixture for owner-qualified optional questions."""
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routes.orchestration_routes import setup_orchestration_routes
from src.openclank.session_map import OwnerSessionMap


class Supervisor:
    def __init__(self, root: Path, questions, *, actor_status="idle"):
        self.root = root
        self.questions = questions
        self.actor_status = actor_status
        self.calls = []
        self.receipts = []
        self.admissions = []

    def _runtime_home(self, owner):
        return self.root / owner

    async def session_http_request(self, chat_id, method, suffix, *, owner, payload=None):
        self.calls.append((chat_id, method, suffix, owner, payload))
        if suffix == "actors":
            value = [{"sessionID": "engine-a", "actorID": "main", "status": self.actor_status,
                      "executionRevision": 7, "effectiveModel": {"providerID": "fixture", "modelID": "model-a"},
                      "hostContext": {"chatID": "chat-a"}}]
        elif suffix == "actors/main/questions?limit=50":
            value = self.questions
        elif suffix == "actors/main/input" and method == "GET":
            value = self.receipts
        elif suffix == "message?limit=1000&agent_id=main":
            value = []
        elif suffix == "actors/main/input" and method == "POST":
            self.admissions.append(payload)
            self.receipts = [{
                "requestID": payload["requestID"], "sessionID": "engine-a", "actorID": "main",
                "text": '"Choose?"="A"', "attachmentRefs": [], "attachmentUploadIDs": [],
                "questionRefs": payload["questionRefs"], "questionAnswers": payload["questionAnswers"],
                "modelRequestOptions": payload["modelRequestOptions"], "mode": payload["mode"], "state": "persisted",
            }]
            value = self.receipts[0]
        else:
            return httpx.Response(404, json={"error": "unexpected native path"})
        return httpx.Response(200, json=value)


def test_optional_question_projection_is_owner_actor_and_map_qualified(tmp_path, monkeypatch):
    from routes import orchestration_routes

    owner_map = OwnerSessionMap(tmp_path / "alice" / "session-map.json", "alice")
    owner_map.bind("chat-a", "engine-a")
    item = {
        "requestID": "question-native-1",
        "questionRef": "question-native-1@1",
        "sessionID": "engine-a",
        "actorID": "main",
        "originTurnID": "msg-origin-a",
        "questionRevision": 1,
        "blocking": False,
        "questions": [{"question": "Which path?", "options": [{"label": "A"}], "custom": False}],
        "state": "pending",
    }
    supervisor = Supervisor(tmp_path, {"items": [item], "nextCursor": None})
    app = FastAPI()
    app.state.mimo_supervisor = supervisor

    @app.middleware("http")
    async def authenticated(request: Request, call_next):
        request.state.authenticated = True
        return await call_next(request)

    monkeypatch.setattr(orchestration_routes, "effective_user", lambda _request: "alice")
    app.include_router(setup_orchestration_routes(
        object(), owner_verifier=lambda *_args: None,
    ))
    client = TestClient(app)

    response = client.get("/api/orchestration/chat-a/actors/main/questions?map_revision=1&limit=50")
    assert response.status_code == 200
    assert response.json()["items"] == [item]
    assert supervisor.calls == [
        ("chat-a", "GET", "actors", "alice", None),
        ("chat-a", "GET", "actors/main/questions?limit=50", "alice", None),
    ]

    stale = client.get("/api/orchestration/chat-a/actors/main/questions?map_revision=0")
    assert stale.status_code == 409
    assert len(supervisor.calls) == 2


def test_structured_answer_post_uses_native_receipt_and_same_http_retry(tmp_path, monkeypatch):
    from routes import orchestration_routes

    owner_map = OwnerSessionMap(tmp_path / "alice" / "session-map.json", "alice")
    owner_map.bind("chat-a", "engine-a")
    supervisor = Supervisor(tmp_path, [], actor_status="running")
    app = FastAPI()
    app.state.mimo_supervisor = supervisor

    @app.middleware("http")
    async def authenticated(request: Request, call_next):
        request.state.authenticated = True
        return await call_next(request)

    monkeypatch.setattr(orchestration_routes, "effective_user", lambda _request: "alice")
    app.include_router(setup_orchestration_routes(
        object(), owner_verifier=lambda *_args: None,
        option_resolver=lambda *_args: {"present": False},
    ))
    client = TestClient(app)
    request = {
        "requestID": "answer-receipt-1", "text": "", "uploadIDs": [], "attachmentRefs": [],
        "questionRefs": ["question-native-1@1"],
        "questionAnswers": [{"requestID": "question-native-1", "questionRevision": 1,
                             "disposition": "answered", "answers": [["A"]]}],
        "actorSessionId": "engine-a", "mode": "steer",
    }
    first = client.post("/api/orchestration/chat-a/actors/main/input?map_revision=1", json=request)
    assert first.status_code == 200, first.text
    assert first.json()["receipt"]["requestID"] == "answer-receipt-1"
    assert len(supervisor.admissions) == 1
    assert supervisor.admissions[0]["questionRefs"] == ["question-native-1@1"]
    assert supervisor.admissions[0]["questionAnswers"] == request["questionAnswers"]

    retry = client.post("/api/orchestration/chat-a/actors/main/input?map_revision=1", json=request)
    assert retry.status_code == 200, retry.text
    assert retry.json()["receipt"]["requestID"] == "answer-receipt-1"
    assert len(supervisor.admissions) == 1, "same HTTP payload reuses the saved native receipt"

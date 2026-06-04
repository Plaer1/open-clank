from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.models import ChatMessage
import routes.history_routes as history_routes


class _FakeQuery:
    def __init__(self, rows=None, first_row=None):
        self._rows = rows or []
        self._first_row = first_row

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._first_row


class _FakeDb:
    def __init__(self):
        self.added = []
        self.deleted = []
        self.session_row = SimpleNamespace(message_count=0, updated_at=None)

    def query(self, model):
        if model is history_routes.DbSession:
            return _FakeQuery(first_row=self.session_row)
        return _FakeQuery(rows=[])

    def add(self, row):
        self.added.append(row)

    def delete(self, row):
        self.deleted.append(row)

    def commit(self):
        pass

    def close(self):
        pass


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session
        self.saved = False

    def get_session(self, session_id):
        if session_id != self.session.id:
            raise KeyError(session_id)
        return self.session

    def save_sessions(self):
        self.saved = True


class _FakeSession:
    id = "session-1"
    name = "Tool session"
    endpoint_url = "http://example.test/v1"
    model = "test-model"
    headers = {}

    def __init__(self, history):
        self.history = history
        self.message_count = len(history)

    def get_context_messages(self):
        return [
            msg.to_dict() if isinstance(msg, ChatMessage) else msg
            for msg in self.history
        ]


def _compact_prompt_for(monkeypatch, history):
    captured = {}

    async def fake_llm_call_async(endpoint_url, model, messages, **kwargs):
        captured["messages"] = messages
        return "Summary text"

    monkeypatch.setattr(history_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(history_routes, "SessionLocal", lambda: _FakeDb())

    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core
    import src.model_context as model_context

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda kind: (None, None, {}))
    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm_call_async)
    monkeypatch.setattr(model_context, "estimate_tokens", lambda messages: 100)
    monkeypatch.setattr(model_context, "get_context_length", lambda endpoint_url, model: 1000)

    session = _FakeSession(history)
    manager = _FakeSessionManager(session)
    app = FastAPI()
    app.include_router(history_routes.setup_history_routes(manager))

    response = TestClient(app).post("/api/session/session-1/compact")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert manager.saved is True
    return captured["messages"][1]["content"]


def test_manual_compact_tolerates_chatmessage_with_none_content(monkeypatch):
    compact_prompt = _compact_prompt_for(
        monkeypatch,
        [
            ChatMessage(role="user", content="start"),
            ChatMessage(role="assistant", content=None),
            ChatMessage(role="tool", content="tool result"),
            ChatMessage(role="assistant", content="done"),
            ChatMessage(role="user", content="next"),
            ChatMessage(role="assistant", content="final"),
        ],
    )
    assert "ASSISTANT: None" not in compact_prompt
    assert "ASSISTANT: " in compact_prompt


def test_manual_compact_tolerates_dict_message_with_none_content(monkeypatch):
    compact_prompt = _compact_prompt_for(
        monkeypatch,
        [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": None},
            ChatMessage(role="tool", content="tool result"),
            ChatMessage(role="assistant", content="done"),
            ChatMessage(role="user", content="next"),
            ChatMessage(role="assistant", content="final"),
        ],
    )
    assert "ASSISTANT: None" not in compact_prompt
    assert "ASSISTANT: " in compact_prompt

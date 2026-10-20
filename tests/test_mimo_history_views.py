"""Canonical settled views are shared by exports, paging, and cached reads."""
import asyncio, json
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from fastapi import HTTPException, APIRouter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.database import Base, Session, ChatMessage
import core.session_manager as manager_module
import routes.session_routes as session_routes
import routes.history.history_routes as history_routes

@pytest.fixture
def views(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'view.db'}"); Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    for module in (manager_module, session_routes, history_routes): monkeypatch.setattr(module, "SessionLocal", factory)
    import src.openclank.conversation_archive as archive
    monkeypatch.setattr(archive, "get_conversation_archive", lambda: SimpleNamespace(chat_erasure_started=lambda **kwargs: False))
    with factory() as db:
        db.add(Session(id="chat", owner="alice", name="Source", endpoint_url="openclank://engine", model="canonical", mimo_state={"history_operation": {"id": "settled", "status": "settled", "hidden_message_ids": ["two", "answer_two"]}}))
        for index, (mid, role) in enumerate([("one", "user"), ("answer_one", "assistant"), ("two", "user"), ("answer_two", "assistant")]):
            db.add(ChatMessage(id=mid, session_id="chat", role=role, content=mid, timestamp=datetime(2026, 10, 13) + timedelta(seconds=index)))
        db.commit()
    manager = manager_module.SessionManager.__new__(manager_module.SessionManager)
    manager.sessions = {"chat": SimpleNamespace(id="chat", owner="alice", name="Source", history=[SimpleNamespace(role="user", content="stale tail", metadata={})], message_count=4, incognito=False)}
    manager._touch_session = lambda sid: None
    actor = ["alice"]
    for module in (session_routes, history_routes): monkeypatch.setattr(module, "effective_user", lambda request: actor[0])
    req = SimpleNamespace(state=SimpleNamespace(authenticated=True))
    monkeypatch.setattr(session_routes, "router", APIRouter(prefix="/api"))
    monkeypatch.setattr(history_routes, "actor_accounting_for_assistant", lambda message_id: None)
    router = session_routes.setup_session_routes(manager, {})
    export = next(item.endpoint for item in router.routes if item.path == "/api/session/{sid}/export")
    hrouter = history_routes.setup_history_routes(manager)
    history = next(item.endpoint for item in hrouter.routes if item.path == "/api/history/{session_id}")
    return factory, manager, actor, req, export, history

def test_export_and_paged_history_hide_settled_tail_and_owner_check(views):
    factory, manager, actor, request, export, history = views
    data = json.loads(export(request, "chat", fmt="json").body)
    assert [row["content"] for row in data["messages"]] == ["one", "answer_one"]
    page = asyncio.run(history(request, "chat", limit=1, offset=1))
    assert page["total"] == 2 and [row["content"] for row in page["history"]] == ["answer_one"]
    actor[0] = "bob"
    with pytest.raises(HTTPException) as exc: export(request, "chat", fmt="json")
    assert exc.value.status_code == 404

def test_pending_history_never_exports_stale_cached_tail(views):
    factory, manager, actor, request, export, history = views
    with factory() as db:
        row = db.query(Session).filter_by(id="chat").one(); row.mimo_state = {"history_operation": {"id": "pending", "status": "pending", "before_hidden": []}}; db.commit()
    for read in (lambda: export(request, "chat", fmt="json"), lambda: asyncio.run(history(request, "chat", limit=2))):
        with pytest.raises(HTTPException) as exc: read()
        assert exc.value.status_code == 409 and exc.value.detail["code"] == "HISTORY_RECONCILIATION_REQUIRED"

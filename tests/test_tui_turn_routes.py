from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, Session, TuiTurnSubmission
import routes.tui_routes as tui_routes


def _app(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def database():
        db = factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr(tui_routes, "get_db_session", database)
    with database() as db:
        db.add(
            Session(
                id="session-1",
                owner="alice",
                name="TUI",
                endpoint_url="mimo://acp",
                endpoint_id="mimo:auto",
                model="provider/model",
            )
        )

    app = FastAPI()
    calls = []

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.api_token = True
        request.state.api_token_client_kind = "tui"
        request.state.api_token_owner = "alice"
        request.state.api_token_scopes = ("tui:sessions",)
        return await call_next(request)

    async def canonical_chat(request: Request):
        calls.append(await request.json())

        async def stream():
            yield 'data: {"delta":"hello"}\n\n'
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    app.state.openclank_chat_stream_handler = canonical_chat
    app.include_router(tui_routes.setup_tui_routes())
    return app, factory, calls


def test_turn_submission_is_durable_and_idempotent(monkeypatch):
    app, factory, calls = _app(monkeypatch)
    body = {
        "message": "hello",
        "idempotency_key": "turn-idempotency-0001",
        "attachments": [],
    }
    with TestClient(app) as client:
        first = client.post("/api/tui/v1/sessions/session-1/turns", json=body)
        assert first.status_code == 200
        assert '"delta":"hello"' in first.text
        second = client.post("/api/tui/v1/sessions/session-1/turns", json=body)
        assert second.status_code == 200
        assert '"status":"complete"' in second.text
    assert len(calls) == 1
    assert calls[0]["mode"] == "agent"
    with factory() as db:
        row = db.query(TuiTurnSubmission).one()
        assert row.owner == "alice"
        assert row.state == "started"


def test_idempotency_key_cannot_be_rebound(monkeypatch):
    app, _factory, calls = _app(monkeypatch)
    with TestClient(app) as client:
        first = client.post(
            "/api/tui/v1/sessions/session-1/turns",
            json={"message": "first", "idempotency_key": "turn-idempotency-0002"},
        )
        assert first.status_code == 200
        conflict = client.post(
            "/api/tui/v1/sessions/session-1/turns",
            json={"message": "changed", "idempotency_key": "turn-idempotency-0002"},
        )
        assert conflict.status_code == 409
    assert len(calls) == 1

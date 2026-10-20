from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, Session, TuiTurnSubmission
import routes.tui_routes as tui_routes
from src import agent_runs


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

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Agent-Run-ID": "turn-run-1"},
        )

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
        assert first.headers["x-agent-run-id"] == "turn-run-1"
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


def test_tui_run_identity_reaches_replay_active_stream_and_stop(monkeypatch):
    """A stale TUI controller cannot stop the run that replaced its turn.

    This drives the TUI router's public responses: initial turn forwards the
    canonical chat header, replay and active-stream publish the retained
    identity, and stop forwards that exact identity into the run manager.
    ``agent_runs`` separately verifies the manager rejects the stale ID; this
    test covers the controller-to-router wiring that makes that protection live.
    """
    app, _factory, _calls = _app(monkeypatch)
    run = SimpleNamespace(run_id="replacement-run")
    seen_stop_ids = []

    async def subscribe(_session_id, *, expected_run=None, after_seq=0):
        assert expected_run is run
        assert after_seq == 0
        yield "data: [DONE]\\n\\n"

    def stop(_session_id, *, expected_run_id=None):
        seen_stop_ids.append(expected_run_id)
        return expected_run_id == run.run_id

    monkeypatch.setattr(agent_runs, "get_run", lambda _session_id: run)
    monkeypatch.setattr(agent_runs, "get_run_id", lambda _session_id: run.run_id)
    monkeypatch.setattr(agent_runs, "get_status", lambda _session_id: "running")
    monkeypatch.setattr(agent_runs, "is_active", lambda _session_id: True)
    monkeypatch.setattr(agent_runs, "subscribe", subscribe)
    monkeypatch.setattr(agent_runs, "stop", stop)

    body = {"message": "hello", "idempotency_key": "turn-idempotency-run-id"}
    with TestClient(app) as client:
        initial = client.post("/api/tui/v1/sessions/session-1/turns", json=body)
        assert initial.headers["x-agent-run-id"] == "turn-run-1"

        replay = client.post("/api/tui/v1/sessions/session-1/turns", json=body)
        assert replay.headers["x-agent-run-id"] == "replacement-run"
        active = client.get("/api/tui/v1/sessions/session-1/turns/active")
        assert active.json()["run_id"] == "replacement-run"
        stream = client.get("/api/tui/v1/sessions/session-1/turns/active/stream")
        assert stream.headers["x-agent-run-id"] == "replacement-run"

        stale = client.post(
            "/api/tui/v1/sessions/session-1/turns/active/stop",
            headers={"X-Agent-Run-ID": "turn-run-1"},
        )
        assert stale.json() == {
            "session_id": "session-1",
            "stopped": False,
            "run_id": "replacement-run",
        }
        current = client.post(
            "/api/tui/v1/sessions/session-1/turns/active/stop",
            headers={"X-Agent-Run-ID": "replacement-run"},
        )
        assert current.json()["stopped"] is True

    assert seen_stop_ids == ["turn-run-1", "replacement-run"]


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

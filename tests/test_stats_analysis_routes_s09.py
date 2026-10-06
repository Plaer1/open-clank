from datetime import datetime
import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ChatMessage, Session as DbSession
from core.stats_models import StatsEvent
from routes.stats_analysis_routes import DatabaseStatsAnalysisLoader
import routes.stats_analysis_routes as analysis_routes

from routes.stats_analysis_routes import setup_stats_analysis_routes


class Loader:
    def __init__(self):
        self.owners = []

    def load_quality(self, owner, *, deadline, cancel_event=None):
        self.owners.append(("quality", owner))
        return ([{"owner": owner, "outcome": "success", "evidence_id": "q1",
                  "prompt_unverified": False, "context_pressure": False,
                  "abandoned": False, "tool_failure": False}],
                {"covered": 1, "total": 1})

    def load_messages(self, owner, *, deadline, cancel_event=None):
        self.owners.append(("messages", owner))
        return [{"owner": owner, "text": "seam", "event_time": "2026-09-01T00:00:00Z"}]


def _client(owner="alice"):
    app = FastAPI()
    app.state.stats_analysis_loader = Loader()

    @app.middleware("http")
    async def trusted_scope(request: Request, call_next):
        request.state.stats_owner = owner
        return await call_next(request)

    app.include_router(setup_stats_analysis_routes(allow_test_owner_state=True))
    return TestClient(app)


def test_quality_and_trends_are_owner_scoped_and_body_only():
    http = _client()
    quality = http.get("/api/stats/v1/analysis/quality")
    assert quality.status_code == 200
    assert quality.json()["state"] == "scored"

    response = http.post("/api/stats/v1/analysis/trends/query",
                         json={"terms": "seam", "content_opt_in": True})
    assert response.status_code == 200
    assert response.json()["groups"][0]["occurrences"] == 1
    assert "seam" not in str(response.url)


def test_trends_require_explicit_content_consent_and_owner_cannot_be_body_selected():
    http = _client("alice")
    denied = http.post("/api/stats/v1/analysis/trends/query",
                       json={"terms": "secret", "owner": "bob"})
    assert denied.status_code == 422
    assert "owner" not in denied.json().get("detail", "").lower()


def test_capabilities_are_typed_unavailable_and_missing_trust_is_rejected(monkeypatch):
    http = _client()
    capabilities = http.get("/api/stats/v1/analysis/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["vcs"]["state"] == "unavailable"
    assert capabilities.json()["insight"]["reason"] == "explicit_gate_required"

    app = FastAPI()
    app.state.stats_analysis_loader = Loader()
    app.include_router(setup_stats_analysis_routes(allow_test_owner_state=True))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    assert TestClient(app).get("/api/stats/v1/analysis/quality").status_code == 401


def test_loader_owner_is_authoritative_and_cross_owner_rows_fail_closed():
    class BadLoader(Loader):
        def load_messages(self, owner, *, deadline, cancel_event=None):
            return [{"owner": "bob", "text": "seam"}]

    app = FastAPI()
    app.state.stats_analysis_loader = BadLoader()

    @app.middleware("http")
    async def trusted_scope(request: Request, call_next):
        request.state.stats_owner = "alice"
        return await call_next(request)

    app.include_router(setup_stats_analysis_routes(allow_test_owner_state=True))
    response = TestClient(app).post("/api/stats/v1/analysis/trends/query",
                                    json={"terms": "seam", "content_opt_in": True})
    assert response.status_code == 422
    assert "owner" in response.json()["detail"]


def test_default_loader_joins_owner_and_keeps_content_opt_in(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stats.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        db.add_all([
            DbSession(id="sa", name="a", endpoint_url="", model="m", owner="alice"),
            DbSession(id="sb", name="b", endpoint_url="", model="m", owner="bob"),
            StatsEvent(id="ea", replay_key="ra", owner="alice", event_kind="chat",
                       event_time=datetime(2026, 9, 1), source="test", producer_revision="r",
                       event_metadata={"tool_failure": True}),
        ])
        db.add_all([
            ChatMessage(id="ma", session_id="sa", role="user", content="alice private seam"),
            ChatMessage(id="mb", session_id="sb", role="user", content="bob private seam"),
        ])
        db.commit()
    finally:
        db.close()

    loader = DatabaseStatsAnalysisLoader(factory)
    events, coverage = loader.load_quality("alice", deadline=9999999999)
    assert len(events) == 1
    assert events[0]["owner"] == "alice"
    assert events[0]["source_state"] == "unavailable"  # The event has no owned session binding.
    assert events[0]["session_handle"] is None
    assert events[0]["evidence_id"].startswith("event_")
    assert len(events[0]["evidence_id"]) == 30
    assert {key: coverage[key] for key in ("covered", "total", "truncated")} == {"covered": 1, "total": 1, "truncated": False}
    assert coverage["ordinary_evidence"]["state"] == "unavailable"
    messages, message_meta = loader.load_messages("alice", deadline=9999999999)
    assert len(messages) == 1 and messages[0]["owner"] == "alice"
    assert messages[0]["text"] == "alice private seam"

    app = FastAPI()
    app.state.stats_analysis_loader = loader

    @app.middleware("http")
    async def trusted_scope(request: Request, call_next):
        request.state.stats_owner = "alice"
        return await call_next(request)

    app.include_router(setup_stats_analysis_routes(allow_test_owner_state=True))
    http = TestClient(app)
    assert http.get("/api/stats/v1/analysis/quality").json()["coverage"] == {"covered": 1, "total": 1, "truncated": False}
    assert http.get("/api/stats/v1/analysis/quality").json()["state"] == "unscored"
    trend = http.post("/api/stats/v1/analysis/trends/query",
                      json={"terms": "seam", "content_opt_in": True})
    assert trend.status_code == 200
    assert trend.json()["message_count"] == 1


def test_worker_timeout_releases_capacity_and_reports_scan_bound(monkeypatch):
    class SlowLoader(Loader):
        def load_quality(self, owner, *, deadline, cancel_event=None):
            time.sleep(0.15)
            return super().load_quality(owner, deadline=deadline, cancel_event=cancel_event)

    app = FastAPI()
    app.state.stats_analysis_loader = SlowLoader()

    @app.middleware("http")
    async def trusted_scope(request: Request, call_next):
        request.state.stats_owner = "alice"
        return await call_next(request)

    app.include_router(setup_stats_analysis_routes(allow_test_owner_state=True))
    monkeypatch.setattr(analysis_routes, "_ANALYSIS_TIMEOUT", 0.02)
    http = TestClient(app)
    assert http.get("/api/stats/v1/analysis/quality").status_code == 504
    time.sleep(0.2)
    monkeypatch.setattr(analysis_routes, "_ANALYSIS_TIMEOUT", 1.0)
    assert http.get("/api/stats/v1/analysis/quality").status_code == 200

    class CappedLoader(Loader):
        def load_quality(self, owner, *, deadline, cancel_event=None):
            events, _ = super().load_quality(owner, deadline=deadline, cancel_event=cancel_event)
            return events, {"covered": 1, "total": 2, "truncated": True}

    capped_app = FastAPI()
    capped_app.state.stats_analysis_loader = CappedLoader()

    @capped_app.middleware("http")
    async def capped_scope(request: Request, call_next):
        request.state.stats_owner = "alice"
        return await call_next(request)

    capped_app.include_router(setup_stats_analysis_routes(allow_test_owner_state=True))
    capped_response = TestClient(capped_app).get("/api/stats/v1/analysis/quality")
    assert capped_response.status_code == 200
    assert capped_response.json()["truncated"] is True
    assert capped_response.json()["warnings"]

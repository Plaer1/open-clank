from datetime import datetime
import json

from core.database import Base
from core.stats_models import StatsEvent
from core.database import Session as DbSession
from services.stats.query import discover_filters, export_csv, export_json, parse_scope, query_summary
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routes.stats_routes import setup_stats_routes


def _factory(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{tmp_path / 'privacy-query.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_query_public_projection_uses_owner_bound_handles(tmp_path):
    factory = _factory(tmp_path)
    with factory() as db:
        db.add(DbSession(id="session-secret", name="fixture", endpoint_url="local",
                         model="model-secret", owner="alice"))
        db.add(StatsEvent(
            id="event-secret-1", replay_key="replay-secret-1", owner="alice",
            session_id="session-secret", provider_id="provider-secret",
            actual_model="model-secret", route_id="route-secret",
            event_kind="response", event_time=datetime(2026, 1, 2), source="fixture",
            producer_revision="test", observation_scope="message", status="complete",
            output_tokens=3, output_tokens_state="reported", event_metadata={}))
        db.commit()
        scope = parse_scope(owner="alice", period="all")
        report = query_summary(db, scope)
        discovered = discover_filters(db, scope, "provider_id")
    encoded = json.dumps(report, sort_keys=True)
    assert "secret" not in encoded
    assert report["owner_scope"]
    assert report["rows"][0]["session_identity"]["handle"].startswith("session_")
    assert discovered["choices"][0]["handle"].startswith("provider_")
    assert "provider-secret" not in json.dumps(discovered)
    exported = json.loads(export_json(report))
    assert exported["owner_scope"] == report["owner_scope"]
    assert exported["rows"][0]["session_identity"] == report["rows"][0]["session_identity"]


def test_exports_redact_identity_and_formula_cells(tmp_path):
    report = {
        "owner_scope": "owner-handle",
        "schema": "open-clank.stats.v1",
        "formula_revision": "test",
        "scope": {"owner": "alice-secret", "filters": {"provider_id": "provider-secret"}},
        "rows": [{"id": "event-secret", "session_id": "session-secret",
                   "actual_model": "=danger", "event_time": "2026-01-01Z",
                   "scope": "message", "status": "ok", "output_tokens": {"value": 2}}],
    }
    data = export_json(report).decode()
    csv_data = export_csv(report).decode()
    assert "alice-secret" not in data and "session-secret" not in data
    assert "provider-secret" not in csv_data and "=danger" not in csv_data
    assert "actual-model_" in csv_data


def test_open_session_authorizes_handle_without_returning_raw_session_id(tmp_path, monkeypatch):
    factory = _factory(tmp_path)
    with factory() as db:
        db.add(DbSession(id="session-secret", name="fixture", endpoint_url="local",
                         model="model", owner="local-installation"))
        db.commit()
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI(); app.include_router(setup_stats_routes(session_factory=factory))
    client = TestClient(app)
    from services.stats.privacy import identity_handle
    handle = identity_handle("local-installation", "session_id", "session-secret")
    response = client.post("/api/stats/v1/sessions/open", json={"handle": handle})
    assert response.status_code == 200
    assert response.json()["handle"] == handle
    assert response.json()["session_id"] == "session-secret"
    assert response.headers["cache-control"] == "no-store"


def test_raw_identity_inputs_are_rejected_by_filter_drilldown_and_quota_routes(tmp_path, monkeypatch):
    factory = _factory(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI(); app.include_router(setup_stats_routes(session_factory=factory))
    client = TestClient(app)
    assert client.get("/api/stats/v1/filters/provider_id", params={"provider_id": "raw-provider"}).status_code == 422
    assert client.get("/api/stats/v1/sessions/raw-session").status_code == 422
    assert client.get("/api/stats/v1/quota", params={"account_id": "raw-account"}).status_code == 422

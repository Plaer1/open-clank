from datetime import datetime
import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import AgentTurn, Base, ChatMessage, Session as DbSession, TurnActor
from core.stats_models import StatsEvent
from routes.stats_activity_routes import setup_stats_activity_routes
from services.stats.activity import load_activity, project_activity


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'activity.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_activity_real_owner_joins_timezone_heatmap_and_redaction(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    try:
        db.add_all([
            DbSession(id="sa", name="a", endpoint_url="", model="secret-model-a", owner="alice", workspace_id="private-a"),
            DbSession(id="sb", name="b", endpoint_url="", model="secret-model-b", owner="bob", workspace_id="private-b"),
        ])
        db.add_all([
            ChatMessage(id="ma", session_id="sa", role="user", content="PRIVATE BODY", timestamp=datetime(2026, 3, 8, 9)),
            ChatMessage(id="mb", session_id="sb", role="user", content="BOB BODY", timestamp=datetime(2026, 3, 8, 9)),
            ChatMessage(id="aa", session_id="sa", role="assistant", content="reply", timestamp=datetime(2026, 3, 8, 10)),
        ])
        db.add(StatsEvent(id="event-a", replay_key="replay-a", owner="alice", event_kind="response",
                          event_time=datetime(2026, 3, 8, 10), duration_ms=120,
                          source="test", producer_revision="r"))
        db.commit()
        db.add(AgentTurn(root_turn_id="ma", mimo_session_id="mimo-a"))
        db.add(TurnActor(root_turn_id="ma", actor_id="actor-a", mimo_session_id="mimo-a",
                         mode="agent", agent="secret-agent", description="private"))
        db.commit()
    finally:
        db.close()

    result = load_activity(factory(), owner="alice", timezone_name="America/Los_Angeles", resolution="day")
    assert result["summary"]["messages"] == 2
    assert result["summary"]["sessions"] == 1
    assert result["summary"]["assistant_outputs"] == 1
    assert result["velocity"]["p50_ms"]["value"] == 120.0
    assert next(point for point in result["contributions"]["points"] if point["messages"])["messages"] == 2
    assert result["actor_comparison"][0]["count"] == 1
    assert result["sessions"]["rows"][0]["active_seconds"] == 1800.0
    assert result["concurrency"]["peak"] == 1
    assert result["timeline"]["points"]
    assert len(result["heatmap"]["points"]) == 168
    assert result["breakdowns"]["model"][0]["label"] == "model 1"
    encoded = str(result)
    assert "PRIVATE BODY" not in encoded and "secret-model-a" not in encoded
    assert "private-a" not in encoded and "secret-agent" not in encoded


def test_activity_route_auth_paging_and_unavailable_capabilities(tmp_path, monkeypatch):
    factory = _db(tmp_path)
    db = factory()
    try:
        db.add(DbSession(id="sa", name="a", endpoint_url="", model="m", owner="alice"))
        for index in range(3):
            db.add(ChatMessage(id=f"m{index}", session_id="sa", role="user", content="hidden",
                               timestamp=datetime(2026, 1, index + 1)))
        db.commit()
    finally:
        db.close()

    app = FastAPI()

    @app.middleware("http")
    async def trusted_scope(request: Request, call_next):
        request.state.stats_owner = "alice"
        return await call_next(request)

    app.include_router(setup_stats_activity_routes(session_factory=factory, allow_test_owner_state=True))
    http = TestClient(app)
    response = http.get("/api/stats/v1/activity?period=all&timezone=America/Los_Angeles&resolution=week&page=1&page_size=1")
    assert response.status_code == 200
    data = response.json()
    assert data["sessions"]["total"] == 1
    assert data["sessions"]["rows"][0]["open_handle"].startswith("session_")
    assert data["capabilities"]["tools"]["state"] == "unavailable"
    assert "hidden" not in str(data)
    scoped = http.get("/api/stats/v1/activity?period=custom&start=2020-01-01T00:00:00&end=2020-02-01T00:00:00")
    assert scoped.status_code == 200
    assert scoped.json()["summary"]["messages"] == 0
    assert scoped.json()["scope"]["period"] == "custom"

    unauth = FastAPI()
    unauth.include_router(setup_stats_activity_routes(session_factory=factory))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    assert TestClient(unauth).get("/api/stats/v1/activity").status_code == 401


def test_activity_projection_cancellation_and_cap(tmp_path):
    import threading
    from services.stats.activity import ActivityError, MAX_ROWS, project_activity

    event = threading.Event(); event.set()
    try:
        project_activity(owner="alice", messages=[], sessions=[], events=[], actors=[], cancel_event=event)
    except ActivityError as exc:
        assert str(exc) == "cancelled"
    else:
        raise AssertionError("cancellation was ignored")
    assert MAX_ROWS >= 1000


def test_activity_top_other_pagination_and_untimed_percentiles():
    sessions = [{"session_id": f"s{i}", "workspace": f"w{i}", "model": f"m{i}",
                 "mode": "agent" if i % 2 else "chat", "actor_shape": "automation" if i % 2 else "human"}
                for i in range(17)]
    messages = [{"session_id": row["session_id"], "role": "user",
                 "timestamp": datetime(2026, 1, i + 1)} for i, row in enumerate(sessions)]
    result = project_activity(owner="alice", messages=messages, sessions=sessions,
                              events=[{"duration_ms": None}], actors=[], page=2, page_size=5)
    assert len(result["sessions"]["rows"]) == 5
    assert result["sessions"]["total"] == 17
    assert result["breakdowns"]["workspace"][-1]["label"] == "Other"
    assert result["velocity"]["p50_ms"]["state"] == "unavailable"
    assert all("s0" not in str(row) and "w0" not in str(row) and "m0" not in str(row)
               for row in result["sessions"]["rows"])


def test_activity_large_projection_stays_within_local_budget():
    sessions = [{"session_id": f"session-{index}", "workspace": "w", "model": "m", "mode": "chat"}
                for index in range(2000)]
    messages = [{"session_id": session["session_id"], "role": "user",
                 "timestamp": datetime(2026, 1, 1)} for session in sessions]
    started = time.monotonic()
    result = project_activity(owner="alice", messages=messages, sessions=sessions,
                              events=[], actors=[], page_size=10)
    assert time.monotonic() - started < 2.0
    assert result["sessions"]["total"] == 2000


def test_activity_mixed_token_state_and_event_only_year():
    result = project_activity(
        owner="alice", messages=[], sessions=[], actors=[],
        events=[{"event_time": datetime(2025, 5, 1), "output_tokens": 10, "output_tokens_state": "reported",
                 "duration_ms": 2},
                {"event_time": datetime(2025, 5, 1), "output_tokens": 20, "output_tokens_state": "estimated",
                 "duration_ms": None}],
    )
    assert result["contributions"]["year"] == 2025
    output_point = next(point for point in result["contributions"]["points"] if point["output_tokens"] is not None)
    assert output_point["output_tokens"] == 30
    assert output_point["output_tokens_state"] == "estimated"
    assert result["summary"]["output_tokens_state"] == "estimated"


def test_activity_keeps_old_session_for_in_range_evidence_and_distinguishes_actor_depth(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    try:
        db.add(DbSession(id="old", name="old", endpoint_url="", model="m", owner="alice",
                         created_at=datetime(2025, 1, 1)))
        db.add(DbSession(id="old-unreferenced", name="old", endpoint_url="", model="m", owner="alice",
                         created_at=datetime(2025, 1, 1)))
        db.add(DbSession(id="future", name="future", endpoint_url="", model="m", owner="alice",
                         created_at=datetime(2027, 1, 1)))
        db.add(ChatMessage(id="old-msg", session_id="old", role="user", content="hidden",
                           timestamp=datetime(2026, 2, 1)))
        db.add(StatsEvent(id="old-event", replay_key="old-replay", owner="alice", event_kind="response",
                          event_time=datetime(2026, 2, 1), source="test", producer_revision="r"))
        db.commit()
    finally:
        db.close()
    scoped = load_activity(factory(), owner="alice", start_utc=datetime(2026, 1, 1), end_utc=datetime(2026, 3, 1))
    assert scoped["summary"]["messages"] == 1
    assert scoped["summary"]["sessions"] == 1

    result = project_activity(owner="alice", messages=[], sessions=[{"session_id": "s1", "mode": "agent"},
                                                                      {"session_id": "s2", "mode": "chat"}],
                              events=[], actors=[{"session_id": "s1", "mode": "agent", "nested": False},
                                                 {"session_id": "s2", "mode": "agent", "nested": True}])
    assert result["automation"]["automation"] == 1
    assert result["automation"]["subagent"] == 1


def test_activity_typed_sum_handles_reported_generators_and_huge_ints():
    result = project_activity(owner="alice", messages=[], sessions=[], actors=[], events=[
        {"event_time": datetime(2026, 1, 1), "output_tokens": 7, "output_tokens_state": "reported", "duration_ms": None},
    ])
    assert result["summary"]["output_tokens"] == 7
    huge = project_activity(owner="alice", messages=[], sessions=[], actors=[], events=[
        {"event_time": datetime(2026, 1, 1), "output_tokens": 10**100, "output_tokens_state": "reported", "duration_ms": None},
    ])
    assert huge["summary"]["output_tokens"] == str(10**100)


def test_activity_contribution_window_rolls_across_year_boundary_without_future_cells():
    result = project_activity(
        owner="alice", messages=[
            {"session_id": "s", "role": "user", "timestamp": datetime(2025, 12, 31)},
            {"session_id": "s", "role": "user", "timestamp": datetime(2026, 1, 1)},
            {"session_id": "s", "role": "user", "timestamp": datetime(2026, 1, 2)},
        ], sessions=[], events=[], actors=[])
    points = result["contributions"]["points"]
    assert result["contributions"]["year"] == 2026
    assert len(points) == 365
    assert points[0]["day"] == "2025-01-03"
    assert points[-1]["day"] == "2026-01-02"
    assert points[-1]["messages"] == 1

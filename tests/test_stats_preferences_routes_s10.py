from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from concurrent.futures import ThreadPoolExecutor

from routes.stats_preferences_routes import setup_stats_preferences_routes


class _Session:
    def close(self):
        pass


def _app(store, observations, owner="alice"):
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.stats_owner = owner
        return await call_next(request)

    def load(user):
        return dict(store.get(user, {}))

    def save(user, value):
        store[user] = dict(value)

    def snapshot(_db, *, owner):
        return {"observations": observations.get(owner, [])}

    app.include_router(setup_stats_preferences_routes(
        session_factory=_Session, pref_loader=load, pref_saver=save,
        snapshot_loader=snapshot, allow_test_owner_state=True,
    ))
    return TestClient(app)


def _observation(owner="alice", percent=91):
    return {"account_id": f"private-{owner}", "provider_id": "openai", "window_id": "5h",
            "window_kind": "discrete", "state": "official", "utilization_numerator": percent,
            "utilization_denominator": 100, "observed_at": "2026-09-01T12:00:00Z",
            "reset_at": "2099-09-28T00:00:00Z"}


def test_owner_preferences_are_validated_scoped_and_persisted():
    store = {}
    client = _app(store, {"alice": []})
    saved = client.put("/api/stats/v1/preferences", json={"refresh_seconds": 2, "quota_alerts": True})
    assert saved.status_code == 200
    assert saved.json()["preferences"]["refresh_seconds"] == 30
    assert store["alice"]["stats_preferences"]["quota_alerts"] is True
    assert client.put("/api/stats/v1/preferences", json={"quota_alerts": "yes"}).status_code == 422
    assert "alice" not in str(client.get("/api/stats/v1/preferences").json())


def test_alert_candidates_require_client_permission_ack_and_dedupe_across_reload():
    store = {"alice": {"stats_preferences": {"quota_alerts": True}}}
    observations = {"alice": [_observation()]}
    client = _app(store, observations)
    first = client.post("/api/stats/v1/alerts/evaluate").json()
    assert first["delivery"] == "permission_required"
    assert [row["threshold"] for row in first["candidates"]] == [80, 90]
    assert "private-alice" not in str(first)
    repeated = _app(store, observations).post("/api/stats/v1/alerts/evaluate").json()
    assert repeated["candidates"] == first["candidates"]
    tokens = [row["token"] for row in repeated["candidates"]]
    ack = client.post("/api/stats/v1/alerts/ack", json={"tokens": tokens})
    assert ack.status_code == 200 and ack.json()["acknowledged"] == 2
    assert _app(store, observations).post("/api/stats/v1/alerts/evaluate").json()["candidates"] == []


def test_off_and_owner_switch_do_not_cross_deliver_or_reuse_state():
    store = {
        "alice": {"stats_preferences": {"quota_alerts": False}},
        "bob": {"stats_preferences": {"quota_alerts": True}},
    }
    observations = {"alice": [_observation("alice")], "bob": [_observation("bob", 81)]}
    assert _app(store, observations, "alice").post("/api/stats/v1/alerts/evaluate").json()["candidates"] == []
    bob = _app(store, observations, "bob").post("/api/stats/v1/alerts/evaluate").json()
    assert [row["threshold"] for row in bob["candidates"]] == [80]
    assert "alice" not in str(bob) and "bob" not in str(bob)


def test_unauthenticated_route_fails_closed(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.include_router(setup_stats_preferences_routes(session_factory=_Session))
    assert TestClient(app).get("/api/stats/v1/preferences").status_code == 401


def test_corrupt_stored_preferences_fail_closed_to_defaults_and_concurrent_writes_stay_typed():
    store = {"alice": {"stats_preferences": {"refresh_seconds": "corrupt", "quota_thresholds": "bad"}}}
    client = _app(store, {"alice": []})
    recovered = client.get("/api/stats/v1/preferences")
    assert recovered.status_code == 200
    assert recovered.json()["preferences"]["refresh_seconds"] == 300
    assert recovered.json()["preferences"]["quota_thresholds"] == [80, 90, 100]

    def write(value):
        return client.put("/api/stats/v1/preferences", json={"refresh_seconds": value, "quota_alerts": True})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(write, [60, 120]))
    assert all(response.status_code == 200 for response in responses)
    assert store["alice"]["stats_preferences"]["refresh_seconds"] in {60, 120}
    assert isinstance(store["alice"]["stats_preferences"]["quota_alerts"], bool)


def test_corrupt_preferences_evaluate_and_ack_use_recovered_defaults():
    store = {"alice": {"stats_preferences": {"quota_alerts": True, "refresh_seconds": "corrupt"}}}
    observations = {"alice": [_observation(percent=91)]}
    client = _app(store, observations)
    evaluated = client.post("/api/stats/v1/alerts/evaluate")
    assert evaluated.status_code == 200
    tokens = [row["token"] for row in evaluated.json()["candidates"]]
    assert tokens == []
    assert isinstance(store["alice"]["stats_preferences"], dict)
    acknowledged = client.post("/api/stats/v1/alerts/ack", json={"tokens": tokens})
    assert acknowledged.status_code == 200

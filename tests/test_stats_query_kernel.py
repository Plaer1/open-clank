from datetime import datetime, timedelta, timezone
import asyncio
import threading
import time
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from core.database import Base
from core.stats_models import StatsEvent, StatsPriceSchedule
from services.stats.query import (StatsQueryError, _install_progress_handler, discover_filters, export_csv,
                                  parse_scope, percentile, prior_period, query_summary, temporal_buckets, top_n)
from routes.stats_routes import _run_bounded
from routes.stats_routes import setup_stats_routes
from services.stats.privacy import identity_handle


def _db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{tmp_path / 'query.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_query_is_owner_first_and_typed(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    common = dict(event_kind="response", event_time=datetime(2026, 1, 2), source="test",
                  producer_revision="s", observation_scope="message", status="complete",
                  event_metadata={})
    db.add_all([
        StatsEvent(id="alice", replay_key="alice", owner="alice", output_tokens=4, output_tokens_state="reported", **common),
        StatsEvent(id="bob", replay_key="bob", owner="bob", output_tokens=99, output_tokens_state="reported", **common),
    ])
    db.commit()
    report = query_summary(db, parse_scope(owner="alice", period="all"))
    assert report["totals"]["output_tokens"]["value"] == 4
    assert report["fact_count"]["value"] == 1
    assert report["owner_scope"] and "owner" not in report["scope"]
    db.close()


def test_cost_and_cache_http_reconcile_inclusive_and_separated_producers(tmp_path, monkeypatch):
    import routes.stats_routes as routes
    from services.stats.pricing import InclusionProfile, admit_schedule
    categories = frozenset({"input_tokens", "cache_read_tokens", "cache_write_tokens"})
    profiles = {
        "fixture-inclusive": InclusionProfile("fixture-inclusive", categories,
            input_includes_cache_read=True, input_includes_cache_write=True,
            billable_categories=categories),
        "fixture-separated": InclusionProfile("fixture-separated", categories,
            billable_categories=categories),
    }
    monkeypatch.setattr(routes, "_PRICING_PROFILES", profiles)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    factory = _db(tmp_path)
    with factory() as db:
        for category, rate in (("input_tokens", 1), ("cache_read_tokens", 2), ("cache_write_tokens", 3)):
            admit_schedule(db, schedule_id=category, provider_id="p", billing_lane="api",
                model_identity="model", route_id=None, token_category=category, currency="USD",
                rate_numerator=rate, rate_denominator=1, effective_start=datetime(2026, 1, 1),
                effective_end=None, source_url="https://example.invalid/fictional-rates",
                source_hash="fixture-only", admission_revision="fixture-r1")
        for identity, marker, count, owner in (
            ("inclusive", "fixture-inclusive", 100, "local-installation"),
            ("separated", "fixture-separated", 50, "local-installation"),
            ("other-owner", "fixture-inclusive", 100, "bob"),
        ):
            db.add(StatsEvent(id=identity, replay_key=identity, owner=owner, event_kind="response",
                event_time=datetime(2026, 3, 1), observation_scope="message", source="fixture",
                producer_revision="fixture-v1", provider_id="p", actual_model="model",
                input_tokens=count, input_tokens_state="reported", cache_read_tokens=40,
                cache_read_tokens_state="reported", cache_write_tokens=10, cache_write_tokens_state="reported",
                event_metadata={"billing_lane": "api", "normalization_profile": marker}))
        db.commit()
    app = FastAPI()
    app.include_router(setup_stats_routes(session_factory=factory))
    client = TestClient(app)
    params = {"period": "custom", "start": "2026-03-01", "end": "2026-03-02", "provider_id": "p"}
    cost = client.get("/api/stats/v1/cost", params=params)
    assert cost.status_code == 200, cost.text
    assert cost.json()["totals"]["estimated"] == {"USD": "320"}
    cache = client.get("/api/stats/v1/cache", params=params)
    assert cache.status_code == 200, cache.text
    body = cache.json()
    assert body["currencies"]["USD"] == {"observed": "320", "uncached": "200", "savings": "-120"}
    assert body["cache_rate"]["value"] == "4/9"
    # A future/unrecognized producer must not inherit either known profile.
    with factory() as db:
        row = db.get(StatsEvent, "separated")
        row.event_metadata = {"billing_lane": "api", "normalization_profile": "future-unknown"}
        db.commit()
    unknown_cost = client.get("/api/stats/v1/cost", params=params).json()
    assert unknown_cost["totals"]["estimated"] == {"USD": "160"}
    assert unknown_cost["coverage"]["unpriced"] > 0
    assert client.get("/api/stats/v1/cache", params=params).json()["state"] == "unavailable"


def test_scope_uses_half_open_dst_window_and_bounds():
    scope = parse_scope(owner="alice", period="custom", timezone_name="America/New_York",
                        start="2026-03-08T00:00:00", end="2026-03-09T00:00:00")
    assert (scope.end_utc - scope.start_utc).total_seconds() == 23 * 3600
    with pytest.raises(StatsQueryError):
        parse_scope(owner="alice", period="custom", timezone_name="UTC",
                    start="2020-01-01", end="2022-01-01")


def test_csv_formula_values_are_escaped():
    report = {"rows": [{"event_time": "2026-01-01Z", "session_id": "=secret",
                         "scope": "message", "actual_model": "@model", "status": "ok",
                         "output_tokens": {"value": 2}}]}
    data = export_csv(report).decode()
    assert "=secret" not in data
    assert "@model" not in data


def test_query_capacity_recovers_after_concurrent_workers():
    async def run_batch():
        def work(value):
            time.sleep(0.03)
            return value
        return await asyncio.gather(*(_run_bounded(lambda value=value: work(value)) for value in range(4)))
    assert asyncio.run(run_batch()) == [0, 1, 2, 3]
    # The capacity primitive is threading-based and remains usable on a new
    # event loop after the prior workers have actually exited.
    assert asyncio.run(_run_bounded(lambda: "recovered")) == "recovered"


def test_cancelled_caller_retains_worker_lease_until_exit(monkeypatch):
    monkeypatch.setattr("routes.stats_routes._QUERY_DEADLINE_SECONDS", 1.0)
    release = threading.Event()
    started = [threading.Event(), threading.Event(), threading.Event()]

    def held(index, _cancel, _deadline):
        started[index].set()
        release.wait(2)
        return index

    async def run():
        first = asyncio.create_task(_run_bounded(lambda c, d: held(0, c, d)))
        second = asyncio.create_task(_run_bounded(lambda c, d: held(1, c, d)))
        await asyncio.gather(*(asyncio.to_thread(item.wait, 1) for item in started[:2]))
        third = asyncio.create_task(_run_bounded(lambda c, d: held(2, c, d)))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.sleep(0.03)
        assert not started[2].is_set(), "cancelled caller must not release a live worker lease"
        release.set()
        assert await second == 1
        assert await third == 2

    asyncio.run(run())


def test_sqlite_progress_handler_cancels_recursive_query_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr("routes.stats_routes._QUERY_DEADLINE_SECONDS", 1.0)
    factory = _db(tmp_path)
    cancel = threading.Event()
    worker_cancel = {}
    entered = threading.Event()

    def worker(cancel_event, deadline):
        worker_cancel["event"] = cancel_event
        db = factory()
        raw = _install_progress_handler(db, cancel_event, deadline)
        try:
            entered.set()
            try:
                db.execute(text("WITH RECURSIVE loop(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM loop WHERE x < 100000000) SELECT sum(x) FROM loop")).scalar()
            except Exception as exc:
                raise TimeoutError("recursive SQLite query cancelled") from exc
        finally:
            if raw is not None:
                raw.set_progress_handler(None, 0)
            assert db.execute(text("SELECT 1")).scalar() == 1
            db.close()

    async def run():
        task = asyncio.create_task(_run_bounded(worker))
        await asyncio.to_thread(entered.wait, 1)
        worker_cancel["event"].set()
        with pytest.raises(HTTPException) as caught:
            await task
        assert caught.value.status_code == 504
        # Capacity and the owned DB connection both recover after cancellation.
        assert await _run_bounded(lambda: "reused") == "reused"

    asyncio.run(run())


def test_base_primitives_are_bounded_and_versioned(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    scope = parse_scope(owner="alice", period="custom", timezone_name="UTC",
                        start="2026-03-01", end="2026-03-03", resolution="day")
    rows = [SimpleNamespace(owner="alice", event_time=datetime(2026, 3, 1, 12), output_tokens=2,
                            output_tokens_state="reported", actual_model="a", provider_id="p",
                            observation_scope="message"),
            SimpleNamespace(owner="alice", event_time=datetime(2026, 3, 2, 12), output_tokens=None,
                            output_tokens_state="unavailable", actual_model="b", provider_id="q",
                            observation_scope="message")]
    buckets = temporal_buckets(rows, scope)
    assert len(buckets) == 2 and buckets[0]["value"]["value"] == 2
    assert top_n(rows, "actual_model", limit=1)[-1]["key"] == "Other"
    assert percentile([1, 2, 3], .5)["value"] == 2
    prior = prior_period(scope)
    assert prior.end_utc == scope.start_utc
    db.close()


def test_hour_buckets_step_utc_and_retain_fall_back_labels():
    scope = parse_scope(owner="alice", period="custom", timezone_name="America/New_York",
                        start="2026-11-01T00:00:00-04:00", end="2026-11-01T03:00:00-05:00", resolution="hour")
    events = [SimpleNamespace(event_time=datetime(2026, 11, 1, 5 + index, 30), output_tokens=1) for index in range(4)]
    buckets = temporal_buckets(events, scope)
    assert len(buckets) == 4
    assert sum("-04:00" in bucket["local_start"] or "-05:00" in bucket["local_start"] for bucket in buckets) == 4


def test_missing_and_unsafe_numeric_values_remain_truthful():
    rows = [SimpleNamespace(actual_model="a", output_tokens=None),
            SimpleNamespace(actual_model="a", output_tokens=2**54)]
    grouped = top_n(rows, "actual_model")
    assert grouped[0]["value"]["state"] == "partial"
    exact = percentile([2**54, 2**54 + 2], .5)
    assert exact["value"] is None and exact["exact"] == str(2**54 + 1)
    with pytest.raises(StatsQueryError):
        percentile([1, 2], 2)


def test_query_aggregation_distinguishes_gaps_missing_estimates_and_other():
    scope = parse_scope(owner="alice", period="custom", timezone_name="UTC",
                        start="2026-03-01", end="2026-03-02", resolution="day")
    missing = SimpleNamespace(event_time=datetime(2026, 3, 1, 12), output_tokens=None,
                              output_tokens_state="unavailable", actual_model="missing")
    estimated = SimpleNamespace(event_time=datetime(2026, 3, 1, 13), output_tokens=4,
                                output_tokens_state="estimated", actual_model="estimated")
    mixed = temporal_buckets([missing, estimated], scope)
    assert mixed[0]["value"]["state"] == "partial" and mixed[0]["value"]["value"] == 4
    gap = temporal_buckets([], scope, coverage_known=True)
    assert gap[0]["value"] == {"value": 0, "unit": "tokens", "state": "reported"}
    other = top_n([SimpleNamespace(actual_model="a", output_tokens=None, output_tokens_state="unavailable"),
                   SimpleNamespace(actual_model="b", output_tokens=None, output_tokens_state="unavailable")],
                  "actual_model", limit=1)
    assert other[-1]["kind"] == "other" and other[-1]["value"]["state"] == "unavailable"


def test_prior_custom_window_uses_utc_elapsed_duration_across_dst():
    scope = parse_scope(owner="alice", period="custom", timezone_name="America/New_York",
                        start="2026-11-01T00:30:00-04:00", end="2026-11-01T02:30:00-05:00", resolution="hour")
    prior = prior_period(scope)
    assert prior.end_utc == scope.start_utc
    assert prior.start_utc == scope.start_utc - (scope.end_utc - scope.start_utc)


def test_prior_preset_uses_local_calendar_day_independent_of_host_timezone(monkeypatch):
    scope = parse_scope(owner="alice", period="today", timezone_name="America/New_York",
                        resolution="day")
    old = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "Pacific/Honolulu")
        if hasattr(time, "tzset"):
            time.tzset()
        prior = prior_period(scope)
        local_start = scope.start_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/New_York"))
        expected = (local_start - timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
        assert prior.start_utc == expected
    finally:
        if old is None: monkeypatch.delenv("TZ", raising=False)
        else: monkeypatch.setenv("TZ", old)
        if hasattr(time, "tzset"):
            time.tzset()


def test_filter_and_session_routes_apply_owner_before_limit(tmp_path, monkeypatch):
    factory = _db(tmp_path)
    db = factory()
    from core.database import Session as DbSession
    db.add(DbSession(id="owned-session", name="x", endpoint_url="local", model="m", owner="local-installation"))
    db.commit(); db.close()
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI(); app.include_router(setup_stats_routes(session_factory=factory))
    client = TestClient(app)
    filters = client.get("/api/stats/v1/filters/provider_id")
    assert filters.status_code == 200
    missing = client.get("/api/stats/v1/sessions/" + identity_handle("local-installation", "session_id", "other"))
    assert missing.status_code == 404
    owned = client.get("/api/stats/v1/sessions/" + identity_handle("local-installation", "session_id", "owned-session"))
    assert owned.status_code == 200


def test_populated_routes_preserve_owner_scope_filters_and_custom_window(tmp_path, monkeypatch):
    factory = _db(tmp_path)
    db = factory()
    from core.database import Session as DbSession
    db.add(DbSession(id="owned-session", name="x", endpoint_url="local", model="m", owner="local-installation"))
    common = dict(event_kind="response", event_time=datetime(2026, 3, 1, 12), source="test",
                  producer_revision="s", observation_scope="message", status="complete", event_metadata={})
    db.add_all([
        StatsEvent(id="local-1", replay_key="local-1", owner="local-installation", session_id="owned-session", provider_id="p", actual_model="m1", output_tokens=7, output_tokens_state="reported", **common),
            StatsEvent(id="local-2", replay_key="local-2", owner="local-installation", session_id="owned-session", provider_id="q", actual_model="m2", output_tokens=99, output_tokens_state="reported", **common),
            StatsEvent(id="local-huge", replay_key="local-huge", owner="local-installation", session_id="owned-session", provider_id="r", actual_model="m3", output_tokens=2**53, output_tokens_state="reported", **common),
            StatsEvent(id="bob-1", replay_key="bob-1", owner="bob", provider_id="p", actual_model="m1", output_tokens=1000, output_tokens_state="reported", **common),
    ])
    db.commit(); db.close()
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI(); app.include_router(setup_stats_routes(session_factory=factory))
    client = TestClient(app)
    params = {"period": "custom", "start": "2026-03-01", "end": "2026-03-02", "timezone": "UTC", "provider_id": "p"}
    summary = client.get("/api/stats/v1/summary", params=params)
    assert summary.status_code == 200
    body = summary.json()
    assert body["totals"]["output_tokens"]["value"] == 7
    assert body["scope"]["filters"]["provider_identity"]["handle"].startswith("provider_")
    groups = client.get("/api/stats/v1/groups", params={"field": "actual_model", **params})
    assert groups.status_code == 200 and groups.json()["groups"][0]["identity"]["handle"].startswith("actual-model_")
    buckets = client.get("/api/stats/v1/buckets", params={"resolution": "day", **params})
    assert buckets.status_code == 200 and buckets.json()["scope"]["filters"]
    exported = client.get("/api/stats/v1/export.json", params=params)
    assert exported.status_code == 200 and exported.json()["scope"]["filters"]["provider_identity"]["handle"].startswith("provider_")
    csv_export = client.get("/api/stats/v1/export.csv", params=params)
    assert csv_export.status_code == 200 and "actual-model_" in csv_export.text and "m2" not in csv_export.text
    exact_csv = client.get("/api/stats/v1/export.csv", params={**params, "provider_id": "r"})
    assert exact_csv.status_code == 200 and str(2**53) in exact_csv.text
    discovered = client.get("/api/stats/v1/filters/provider_id", params={k: v for k, v in params.items() if k != "provider_id"})
    assert discovered.status_code == 200 and len(discovered.json()["choices"]) == 3
    session = client.get("/api/stats/v1/sessions/" + identity_handle("local-installation", "session_id", "owned-session"), params={k: v for k, v in params.items() if k != "provider_id"})
    assert session.status_code == 200 and session.json()["rows"][0]["actual_model_identity"]["handle"].startswith("actual-model_")
    cost = client.get("/api/stats/v1/cost", params=params)
    assert cost.status_code == 200 and cost.json()["scope"]["filters"]["provider_identity"]["handle"].startswith("provider_")
    cache = client.get("/api/stats/v1/cache", params=params)
    assert cache.status_code == 200 and cache.json()["scope"]["filters"]["provider_identity"]["handle"].startswith("provider_")


def test_filter_handles_round_trip_and_foreign_handles_fail_closed(tmp_path, monkeypatch):
    factory = _db(tmp_path)
    with factory() as db:
        db.add(StatsEvent(id="route-event", replay_key="route-event", owner="local-installation",
                          provider_id="private-provider", event_kind="response",
                          event_time=datetime(2026, 3, 1), source="fixture",
                          producer_revision="v1", observation_scope="message",
                          status="complete", output_tokens=2,
                          output_tokens_state="reported", event_metadata={}))
        db.commit()
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI(); app.include_router(setup_stats_routes(session_factory=factory))
    client = TestClient(app)
    params = {"period": "custom", "start": "2026-03-01", "end": "2026-03-02"}
    choices = client.get("/api/stats/v1/filters/provider_id", params=params).json()["choices"]
    handle = choices[0]["handle"]
    accepted = client.get("/api/stats/v1/summary", params={**params, "provider_id": handle})
    assert accepted.status_code == 200 and accepted.json()["fact_count"]["value"] == 1
    foreign = "provider_000000000000000000000000"
    rejected = client.get("/api/stats/v1/summary", params={**params, "provider_id": foreign})
    assert rejected.status_code == 422


def test_cost_cache_routes_use_admitted_profile_per_event(tmp_path, monkeypatch):
    factory = _db(tmp_path); db = factory()
    common = dict(owner="local-installation", event_kind="response", event_time=datetime(2026, 3, 1, 12), source="test", producer_revision="s", observation_scope="message", status="complete", provider_id="p", actual_model="m", route_id="r", input_tokens=100, cache_read_tokens=40, cache_write_tokens=10, input_tokens_state="reported", cache_read_tokens_state="reported", cache_write_tokens_state="reported")
    db.add_all([
        StatsEvent(id="profiled", replay_key="profiled", event_metadata={"billing_lane": "api", "normalization_profile": "managed-sdk-inclusive-v1"}, **common),
        StatsEvent(id="unknown-profile", replay_key="unknown-profile", event_metadata={"billing_lane": "api", "normalization_profile": "future-v99"}, **common),
    ])
    for category in ("input_tokens", "cache_read_tokens", "cache_write_tokens"):
        db.add(StatsPriceSchedule(id="price-" + category, provider_id="p", billing_lane="api", model_identity="m", route_id="r", token_category=category, currency="USD", rate_numerator=1, rate_denominator=1, effective_start=datetime(2026, 1, 1), effective_end=None, source_url="https://example.invalid/fictional", source_hash="fictional", admission_revision="fixture-v1"))
    db.commit(); db.close(); monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI(); app.include_router(setup_stats_routes(session_factory=factory)); client = TestClient(app)
    params = {"period": "custom", "start": "2026-03-01", "end": "2026-03-02", "provider_id": "p"}
    cost = client.get("/api/stats/v1/cost", params=params)
    assert cost.status_code == 200
    body = cost.json(); assert body["totals"]["estimated"]["USD"] == "100"
    assert body["coverage"]["unpriced"] > 0 and body["scope"]["filters"]["provider_identity"]["handle"].startswith("provider_")
    cache = client.get("/api/stats/v1/cache", params=params)
    assert cache.status_code == 200 and cache.json()["cache_rate"]["state"] == "unavailable"

    import routes.stats_routes as stats_routes
    monkeypatch.setattr(stats_routes, "_QUERY_DEADLINE_SECONDS", 0.01)
    def slow_factory():
        time.sleep(0.1)
        return factory()
    slow_app = FastAPI(); slow_app.include_router(setup_stats_routes(session_factory=slow_factory))
    assert TestClient(slow_app).get("/api/stats/v1/summary").status_code == 504

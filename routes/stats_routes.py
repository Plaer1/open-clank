"""Owner-scoped, bounded Stats S04 base API."""

from __future__ import annotations

import asyncio
import inspect
import time
import threading
import hashlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, HTTPException, Query, Request, Body
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import OperationalError

from core.database import Session as DbSession, SessionLocal
from core.stats_models import StatsPriceSchedule, StatsEvent
from services.stats.query import (StatsQueryError, discover_filters, export_csv, export_json,
                                  admitted_events, parse_scope, percentile, prior_period,
                                  query_summary, session_drilldown, temporal_buckets, top_n, _aggregate)
from services.stats.query import _public_report
from services.stats.pricing import InclusionProfile, PricingError, cache_counterfactual, cache_rate, cost_envelope, price_event, resolve_profile
from services.stats.privacy import IdentityCatalog, identity_handle, safe_scope
from services.stats.quota import QuotaError, quota_snapshot
from services.stats.query import _install_progress_handler
from src.auth_helpers import _auth_disabled, effective_user, require_user
from src.owner_identity import LOCAL_INSTALLATION_OWNER

_QUERY_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stats-query")
_QUERY_CAPACITY = threading.BoundedSemaphore(2)
_QUERY_DEADLINE_SECONDS = 10.0
_PRICING_PROFILES = {"managed-sdk-inclusive-v1": InclusionProfile(
    "managed-sdk-inclusive-v1", frozenset({"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"}),
    input_includes_cache_read=True, input_includes_cache_write=True,
    output_includes_reasoning=True, reasoning_billing="separate")}
_FILTER_NAMES = ("provider_id", "account_id", "workspace_id", "route_id", "requested_model",
                 "actual_model", "actor_kind", "source", "status")
_OPAQUE_FILTERS = {"provider_id", "account_id", "workspace_id", "route_id", "requested_model", "actual_model"}


def _require_opaque_filters(filters: dict[str, str]) -> None:
    for field, value in filters.items():
        if field in _OPAQUE_FILTERS and value and not value.startswith(field.removesuffix("_id").replace("_", "-") + "_"):
            raise StatsQueryError(f"{field} must be an opaque identity handle")


def _request_scope(owner, *, period, timezone, start, end, resolution, values):
    scope = parse_scope(owner=owner, period=period, timezone_name=timezone,
                        start=start, end=end, resolution=resolution)
    return scope, {key: values.get(key) for key in _FILTER_NAMES if values.get(key) is not None}


def _active_price_schedules(db, cancel_event, deadline):
    """Load the bounded catalog while preserving real database failures."""
    raw = _install_progress_handler(db, cancel_event, deadline)
    try:
        try:
            return db.query(StatsPriceSchedule).filter(
                StatsPriceSchedule.active.is_(True)
            ).limit(10001).all()
        except OperationalError as exc:
            if cancel_event.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("price schedule query cancelled or deadline exceeded") from exc
            raise
    finally:
        if raw is not None:
            raw.set_progress_handler(None, 0)


async def _run_bounded(function):
    """Bound queue + execution; capacity is released only after worker exit."""
    deadline = time.monotonic() + _QUERY_DEADLINE_SECONDS
    cancel_event = threading.Event()
    while not _QUERY_CAPACITY.acquire(blocking=False):
        if time.monotonic() >= deadline:
            raise HTTPException(503, "Stats query capacity is busy")
        await asyncio.sleep(0.01)
    try:
        if len(inspect.signature(function).parameters) >= 2:
            future = _QUERY_EXECUTOR.submit(function, cancel_event, deadline)
        else:
            future = _QUERY_EXECUTOR.submit(function)
    except Exception:
        _QUERY_CAPACITY.release()
        raise
    wrapped = asyncio.wrap_future(future)
    future.add_done_callback(lambda _done: _QUERY_CAPACITY.release())
    try:
        remaining = max(0.0, deadline - time.monotonic())
        return await asyncio.wait_for(wrapped, timeout=remaining)
    except asyncio.TimeoutError as exc:
        cancel_event.set()
        # The worker retains its lease and will release it when its DB call
        # actually exits; this prevents cancelled requests from overfilling
        # the executor. SQLite work has a bounded fact query and no model call.
        raise HTTPException(504, "Stats query deadline exceeded") from exc
    except TimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    except asyncio.CancelledError:
        cancel_event.set()
        raise


def _owner(request: Request) -> str:
    user = str(effective_user(request) or "").strip().lower()
    if user:
        return user
    if _auth_disabled():
        return LOCAL_INSTALLATION_OWNER
    require_user(request)
    raise HTTPException(401, "Stats owner scope is unavailable")


def setup_stats_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(prefix="/api/stats/v1", tags=["stats"])

    @router.get("/capabilities")
    async def capabilities(request: Request):
        _owner(request)
        return {
            "schema": "open-clank.stats.v1",
            "resources": {
                "summary": {"status": "implemented", "owner_scoped": True},
                "export": {"status": "implemented", "owner_scoped": True},
                "quota": {"status": "partial", "owner": "S02",
                           "details": "owner-scoped passive API capacity adapters and snapshots are implemented; subscription quota, provider cache refresh, and single-flight wiring remain unavailable"},
                "pricing": {"status": "implemented", "owner": "S03", "details": "admitted schedules only; missing or mixed-currency facts remain explicitly unpriced"},
                "activity": {"status": "implemented", "owner": "S08", "details": "owner-scoped bounded activity projection; tools and skills evidence remain unavailable"},
                "trends": {"status": "implemented", "owner": "S09", "details": "explicit content opt-in and owner-scoped POST body terms"},
                "quality": {"status": "implemented", "owner": "S09", "details": "formula-versioned quality projection with opaque evidence handles"},
                "precision_logging": {"status": "unavailable", "details": "optional precision logging is disabled; no raw diagnostic payloads are retained"},
            },
            "limits": {"max_facts": 100000, "max_buckets": 1000, "max_page": 100,
                       "max_top": 20, "max_export_bytes": 5 * 1024 * 1024},
        }

    @router.get("/summary")
    async def summary(request: Request, period: str = "7d", timezone: str = "UTC",
                      start: str | None = None, end: str | None = None,
                      resolution: str = "day", page: int = Query(1, ge=1),
                      page_size: int = Query(100, ge=1, le=100),
                      provider_id: str | None = None, account_id: str | None = None,
                      workspace_id: str | None = None, route_id: str | None = None,
                      requested_model: str | None = None, actual_model: str | None = None,
                      actor_kind: str | None = None, source: str | None = None,
                      status: str | None = None):
        try:
            scope, filters = _request_scope(_owner(request), period=period, timezone=timezone,
                                            start=start, end=end, resolution=resolution, values=locals())
            def run(cancel_event, deadline):
                db = session_factory()
                try:
                    return query_summary(db, scope, filters=filters, page=page, page_size=page_size,
                                         cancel_event=cancel_event, deadline=deadline)
                finally:
                    db.close()
            return await _run_bounded(run)
        except TimeoutError as exc:
            raise HTTPException(504, str(exc)) from exc
        except StatsQueryError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/export.csv")
    async def export(request: Request, period: str = "7d", timezone: str = "UTC",
                     start: str | None = None, end: str | None = None,
                     resolution: str = "day", provider_id: str | None = None,
                     account_id: str | None = None, workspace_id: str | None = None,
                     route_id: str | None = None, requested_model: str | None = None,
                     actual_model: str | None = None, actor_kind: str | None = None,
                     source: str | None = None, status: str | None = None):
        try:
            scope, filters = _request_scope(_owner(request), period=period, timezone=timezone,
                                            start=start, end=end, resolution=resolution, values=locals())
            def run(cancel_event, deadline):
                db = session_factory()
                try:
                    return query_summary(db, scope, filters=filters, page=1, page_size=100, all_rows=True,
                                         cancel_event=cancel_event, deadline=deadline)
                finally:
                    db.close()
            report = await _run_bounded(run)
            if report["coverage"].get("truncated"):
                raise StatsQueryError("export fact cap reached; narrow the scope")
            return Response(export_csv(report), media_type="text/csv",
                            headers={"Content-Disposition": "attachment; filename=stats.csv"})
        except TimeoutError as exc:
            raise HTTPException(504, str(exc)) from exc
        except StatsQueryError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/export.json")
    async def export_json_route(request: Request, period: str = "7d", timezone: str = "UTC",
                                start: str | None = None, end: str | None = None,
                                resolution: str = "day", provider_id: str | None = None,
                                account_id: str | None = None, workspace_id: str | None = None,
                                route_id: str | None = None, requested_model: str | None = None,
                                actual_model: str | None = None, actor_kind: str | None = None,
                                source: str | None = None, status: str | None = None):
        try:
            scope, filters = _request_scope(_owner(request), period=period, timezone=timezone,
                                            start=start, end=end, resolution=resolution, values=locals())
            def run(cancel_event, deadline):
                db = session_factory()
                try:
                    return query_summary(db, scope, filters=filters, page=1, page_size=100, all_rows=True, cancel_event=cancel_event, deadline=deadline)
                finally:
                    db.close()
            report = await _run_bounded(run)
            if report["coverage"].get("truncated"):
                raise StatsQueryError("export fact cap reached; narrow the scope")
            return Response(export_json(report), media_type="application/json")
        except TimeoutError as exc:
            raise HTTPException(504, str(exc)) from exc
        except StatsQueryError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/filters/{field}")
    async def filters(request: Request, field: str, period: str = "7d", timezone: str = "UTC",
                      start: str | None = None, end: str | None = None, resolution: str = "day",
                      provider_id: str | None = None, account_id: str | None = None,
                      workspace_id: str | None = None, route_id: str | None = None,
                      requested_model: str | None = None, actual_model: str | None = None,
                      actor_kind: str | None = None, source: str | None = None,
                      status: str | None = None):
        try:
            scope, filters = _request_scope(_owner(request), period=period, timezone=timezone,
                                            start=start, end=end, resolution=resolution,
                                            values=locals())
            _require_opaque_filters(filters)
            def run(cancel_event, deadline):
                db = session_factory()
                try:
                    return discover_filters(db, scope, field, filters=filters,
                                            cancel_event=cancel_event, deadline=deadline)
                finally:
                    db.close()
            return await _run_bounded(run)
        except TimeoutError as exc:
            raise HTTPException(504, str(exc)) from exc
        except StatsQueryError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/sessions/top")
    async def top_sessions_early(request: Request, period: str = "30d", timezone: str = "UTC", rank: str = Query("tokens", pattern="^(tokens|cost)$"), limit: int = Query(10, ge=1, le=20)):
        """Keep the collection resource ahead of the owner-checked item route."""
        owner = _owner(request)
        def run(cancel_event, deadline):
            db = session_factory()
            try:
                scope, filters = _request_scope(owner, period=period, timezone=timezone, start=None, end=None, resolution="day", values=locals())
                projection, truncated = admitted_events(db, scope, filters=filters, cancel_event=cancel_event, deadline=deadline)
                events = projection.events
                schedules = _active_price_schedules(db, cancel_event, deadline)
                totals = defaultdict(lambda: {"token_records": [], "cost": 0, "cost_currency": None, "priced": True})
                for event in events:
                    if event.session_id:
                        known = event.input_tokens is not None or event.output_tokens is not None
                        token_state = "estimated" if "estimated" in {event.input_tokens_state, event.output_tokens_state} else "reported"
                        if not known: token_state = "unavailable"
                        elif event.input_tokens is None or event.output_tokens is None: token_state = "partial"
                        totals[event.session_id]["token_records"].append((((event.input_tokens or 0) + (event.output_tokens or 0)) if known else None, token_state))
                        components = price_event(event, schedules, inclusion_profile=resolve_profile(event, _PRICING_PROFILES), cancel_event=cancel_event, deadline=deadline)
                        priced_components = [component for component in components if component.amount is not None]
                        currencies = {component.currency for component in priced_components}
                        blocking = {component.reason for component in components if component.state == "unpriced"} & {"missing_required_subset", "invalid_count", "no_exact_effective_schedule"}
                        if not priced_components or len(currencies) != 1 or blocking:
                            totals[event.session_id]["priced"] = False
                        else:
                            currency = next(iter(currencies))
                            if totals[event.session_id]["cost_currency"] not in (None, currency): totals[event.session_id]["priced"] = False
                            totals[event.session_id]["cost_currency"] = currency
                            totals[event.session_id]["cost"] += sum(component.amount for component in priced_components)
                for summary in totals.values(): summary["tokens"] = _aggregate(summary.pop("token_records"))
                candidate_ids = sorted(totals, key=lambda session_id: (totals[session_id].get("tokens", {}).get("value", 0) if rank == "tokens" else (totals[session_id].get("cost", 0) if totals[session_id].get("priced") else -1)), reverse=True)[:1001]
                rows = db.query(DbSession).filter(DbSession.owner == owner, DbSession.id.in_(candidate_ids)).limit(1001).all()
                candidate_truncated = len(totals) > 1001
                result = []
                for row in sorted(rows, key=lambda item: (totals.get(item.id, {}).get("tokens", {}).get("value", 0) if rank == "tokens" else (totals.get(item.id, {}).get("cost", 0) if totals.get(item.id, {}).get("priced") else -1)), reverse=True)[:limit]:
                    if cancel_event.is_set() or time.monotonic() >= deadline: raise TimeoutError("session query deadline exceeded")
                    summary = totals.get(row.id, {})
                    token = summary.get("tokens", {"value": None, "state": "unavailable"})
                    result.append({"handle": identity_handle(owner, "session_id", row.id), "label": "Session", "status": "available", "tokens": {**token, "unit": "tokens"}, "cost": {"value": str(summary.get("cost")), "currency": summary.get("cost_currency"), "state": "reported" if summary.get("priced") and summary.get("cost_currency") else "unpriced"}, "coverage": {"state": "partial_truncated" if truncated else projection.coverage}})
                choices = {}
                for dimension, label in (("actual_model", "Model"), ("workspace_id", "Workspace")):
                    values = sorted({getattr(event, dimension, None) for event in events if getattr(event, dimension, None)})
                    choices[dimension] = [dict(IdentityCatalog(owner, dimension, values).project(value), label=f"{label} {index + 1}") for index, value in enumerate(values)]
                catalogs = {dimension: IdentityCatalog(owner, dimension, [getattr(event, dimension, None) for event in events]) for dimension in ("actual_model", "workspace_id")}
                return {"schema":"open-clank.stats.v1", "scope":safe_scope(owner, {"period": scope.period, "timezone": scope.timezone, "start": scope.start_utc.isoformat() if scope.start_utc else None, "end": scope.end_utc.isoformat() if scope.end_utc else None, "resolution": scope.resolution, "filters": filters}, catalogs=catalogs), "sessions": result, "choices": choices, "coverage":{"state":"partial_truncated" if truncated or candidate_truncated else "complete", "truncated":truncated or candidate_truncated}, "warnings":["session_candidate_cap_reached"] if candidate_truncated else []}
            finally:
                db.close()
        try:
            return await _run_bounded(run)
        except TimeoutError as exc:
            raise HTTPException(504, str(exc)) from exc
        except (StatsQueryError, PricingError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/sessions/{session_id}")
    async def session(request: Request, session_id: str, period: str = "7d", timezone: str = "UTC",
                      start: str | None = None, end: str | None = None, resolution: str = "day",
                      page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=100),
                      provider_id: str | None = None, account_id: str | None = None,
                      workspace_id: str | None = None, route_id: str | None = None,
                      requested_model: str | None = None, actual_model: str | None = None,
                      actor_kind: str | None = None, source: str | None = None,
                      status: str | None = None):
        try:
            scope, filters = _request_scope(_owner(request), period=period, timezone=timezone,
                                            start=start, end=end, resolution=resolution,
                                            values=locals())
            _require_opaque_filters(filters)
            def run(cancel_event, deadline):
                db = session_factory()
                try:
                    owner = scope.owner
                    requested_session_id = session_id
                    if not requested_session_id.startswith("session_"):
                        raise HTTPException(422, "session_id must be an opaque identity handle")
                    ids = [row[0] for row in db.query(DbSession.id).filter(DbSession.owner == owner).limit(10001).all()]
                    try:
                        requested_session_id = IdentityCatalog(owner, "session_id", ids).resolve(requested_session_id)
                    except Exception as exc:
                        raise HTTPException(404, "Stats session handle unavailable") from exc
                    raw = _install_progress_handler(db, cancel_event, deadline)
                    try:
                        try:
                            found = db.query(DbSession.id).filter(DbSession.id == requested_session_id, DbSession.owner == owner).first()
                        except OperationalError as exc:
                            if cancel_event.is_set() or time.monotonic() >= deadline:
                                raise TimeoutError("Stats session lookup cancelled or deadline exceeded") from exc
                            raise
                    finally:
                        if raw is not None:
                            raw.set_progress_handler(None, 0)
                    if found is None:
                        raise HTTPException(404, "Stats session not found")
                    return session_drilldown(db, scope, session_id=requested_session_id, filters=filters, page=page, page_size=page_size,
                                             cancel_event=cancel_event, deadline=deadline)
                finally:
                    db.close()
            return await _run_bounded(run)
        except TimeoutError as exc:
            raise HTTPException(504, str(exc)) from exc
        except StatsQueryError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/buckets")
    async def buckets(request: Request, period: str = "7d", timezone: str = "UTC", resolution: str = "day", start: str | None = None, end: str | None = None, provider_id: str | None = None, account_id: str | None = None, workspace_id: str | None = None, route_id: str | None = None, requested_model: str | None = None, actual_model: str | None = None, actor_kind: str | None = None, source: str | None = None, status: str | None = None):
        try:
            scope, filters = _request_scope(_owner(request), period=period, timezone=timezone, start=start, end=end, resolution=resolution, values=locals())
            def run(cancel_event, deadline):
                db = session_factory()
                try:
                    projection, truncated = admitted_events(db, scope, filters=filters, cancel_event=cancel_event, deadline=deadline)
                    return _public_report({"schema": "open-clank.stats.v1", "scope": {"owner": scope.owner, "timezone": scope.timezone, "start": scope.start_utc.isoformat() + "Z" if scope.start_utc else None, "end": scope.end_utc.isoformat() + "Z" if scope.end_utc else None, "resolution": scope.resolution, "filters": filters}, "formula_revision": "s04-base-v1", "provenance": {"source": "stats_events", "projection": "s01-ledger"}, "buckets": temporal_buckets(projection.events, scope, coverage_known=projection.coverage == "complete" and not truncated, cancel_event=cancel_event, deadline=deadline)}, owner=scope.owner) | {"coverage": {"state": "partial_truncated" if truncated else projection.coverage, "truncated": truncated}, "warnings": ["fact_cap_reached"] if truncated else []}
                finally: db.close()
            return await _run_bounded(run)
        except TimeoutError as exc: raise HTTPException(504, str(exc)) from exc
        except (StatsQueryError, PricingError) as exc: raise HTTPException(422, str(exc)) from exc

    @router.get("/groups")
    async def groups(request: Request, field: str, limit: int = Query(10, ge=1, le=20), period: str = "7d", timezone: str = "UTC", start: str | None = None, end: str | None = None, resolution: str = "day", provider_id: str | None = None, account_id: str | None = None, workspace_id: str | None = None, route_id: str | None = None, requested_model: str | None = None, actual_model: str | None = None, actor_kind: str | None = None, source: str | None = None, status: str | None = None):
        try:
            scope, filters = _request_scope(_owner(request), period=period, timezone=timezone, start=start, end=end, resolution=resolution, values=locals())
            def run(cancel_event, deadline):
                db = session_factory()
                try:
                    projection, truncated = admitted_events(db, scope, filters=filters, cancel_event=cancel_event, deadline=deadline)
                    groups = top_n(projection.events, field, limit=limit)
                    if field in {"provider_id", "account_id", "workspace_id", "route_id", "requested_model", "actual_model"}:
                        catalog = IdentityCatalog(scope.owner, field, [row.get("key") for row in groups if row.get("kind") == "value"])
                        for row in groups:
                            projected = catalog.project(row.get("key"))
                            if projected:
                                row["identity"] = projected
                                row["key"] = projected["label"]
                    return _public_report({"schema": "open-clank.stats.v1", "scope": {"owner": scope.owner, "timezone": scope.timezone, "start": scope.start_utc.isoformat() + "Z" if scope.start_utc else None, "end": scope.end_utc.isoformat() + "Z" if scope.end_utc else None, "resolution": scope.resolution, "filters": filters}, "formula_revision": "s04-base-v1", "provenance": {"source": "stats_events", "projection": "s01-ledger"}, "groups": groups}, owner=scope.owner) | {"coverage": {"state": "partial_truncated" if truncated else projection.coverage, "truncated": truncated}, "warnings": ["fact_cap_reached"] if truncated else []}
                finally: db.close()
            return await _run_bounded(run)
        except TimeoutError as exc: raise HTTPException(504, str(exc)) from exc
        except StatsQueryError as exc: raise HTTPException(422, str(exc)) from exc

    @router.get("/cost")
    async def cost(request: Request, period: str = "7d", timezone: str = "UTC", start: str | None = None,
                   end: str | None = None, resolution: str = "day", provider_id: str | None = None,
                   account_id: str | None = None, workspace_id: str | None = None, route_id: str | None = None,
                   requested_model: str | None = None, actual_model: str | None = None, actor_kind: str | None = None,
                   source: str | None = None, status: str | None = None):
        try:
            scope, filters = _request_scope(_owner(request), period=period, timezone=timezone,
                                            start=start, end=end, resolution=resolution, values=locals())
            def run(cancel_event, deadline):
                db = session_factory()
                try:
                    projection, truncated = admitted_events(db, scope, filters=filters, cancel_event=cancel_event, deadline=deadline)
                    schedules = _active_price_schedules(db, cancel_event, deadline)
                    if len(schedules) > 10000: raise StatsQueryError("price schedule cap reached")
                    report = cost_envelope(projection.events, schedules, profiles=_PRICING_PROFILES, cancel_event=cancel_event, deadline=deadline, scope={"owner": scope.owner, "timezone": scope.timezone, "start": scope.start_utc.isoformat() + "Z" if scope.start_utc else None, "end": scope.end_utc.isoformat() + "Z" if scope.end_utc else None, "resolution": scope.resolution, "filters": filters})
                    report["coverage"].update({"truncated": truncated, "state": "partial_truncated" if truncated else report["coverage"].get("state", "complete")})
                    if truncated: report["warnings"].append("fact_cap_reached")
                    return _public_report(report, owner=scope.owner)
                finally:
                    db.close()
            return await _run_bounded(run)
        except TimeoutError as exc: raise HTTPException(504, str(exc)) from exc
        except (StatsQueryError, PricingError) as exc: raise HTTPException(422, str(exc)) from exc

    @router.get("/cache")
    async def cache(request: Request, period: str = "7d", timezone: str = "UTC", start: str | None = None,
                    end: str | None = None, resolution: str = "day", provider_id: str | None = None,
                    account_id: str | None = None, workspace_id: str | None = None, route_id: str | None = None,
                    requested_model: str | None = None, actual_model: str | None = None, actor_kind: str | None = None,
                    source: str | None = None, status: str | None = None):
        try:
            scope, filters = _request_scope(_owner(request), period=period, timezone=timezone, start=start, end=end, resolution=resolution, values=locals())
            def run(cancel_event, deadline):
                db = session_factory()
                try:
                    projection, truncated = admitted_events(db, scope, filters=filters, cancel_event=cancel_event, deadline=deadline)
                    schedules = _active_price_schedules(db, cancel_event, deadline)
                    if len(schedules) > 10000: raise StatsQueryError("price schedule cap reached")
                    result = cache_counterfactual(projection.events, schedules, profiles=_PRICING_PROFILES, cancel_event=cancel_event, deadline=deadline)
                    result["cache_rate"] = cache_rate(projection.events, profiles=_PRICING_PROFILES, cancel_event=cancel_event, deadline=deadline)
                    result.update({"schema": "open-clank.stats.v1", "scope": {"owner": scope.owner, "timezone": scope.timezone, "start": scope.start_utc.isoformat() + "Z" if scope.start_utc else None, "end": scope.end_utc.isoformat() + "Z" if scope.end_utc else None, "resolution": scope.resolution, "filters": filters}, "coverage": {"truncated": truncated, "state": "partial_truncated" if truncated else projection.coverage}, "formula_revision": "s03-cache-v1", "warnings": (["fact_cap_reached"] if truncated else []) + (["cache_unavailable"] if result.get("state") == "unavailable" else [])})
                    return _public_report(result, owner=scope.owner)
                finally: db.close()
            return await _run_bounded(run)
        except TimeoutError as exc: raise HTTPException(504, str(exc)) from exc
        except (StatsQueryError, PricingError) as exc: raise HTTPException(422, str(exc)) from exc

    @router.post("/sessions/open")
    async def open_stats_session(request: Request, payload: dict = Body(...)):
        owner = _owner(request); handle = payload.get("handle")
        if not isinstance(handle, str) or len(handle) < 16 or len(handle) > 64: raise HTTPException(422, "invalid session handle")
        db = session_factory()
        try:
            rows = db.query(DbSession).filter(DbSession.owner == owner).limit(10001).all()
            for row in rows:
                if identity_handle(owner, "session_id", row.id) == handle:
                    # This ID is a one-request authenticated navigation payload.
                    # It is never included in Stats projections, URLs, storage,
                    # or logs; no-store prevents intermediary persistence.
                    return JSONResponse(
                        {"schema":"open-clank.stats.v1", "handle":handle,
                         "session_id":row.id,
                         "owner_scope":hashlib.sha256(owner.encode()).hexdigest()[:16],
                         "status":"authorized"},
                        headers={"Cache-Control": "no-store"},
                    )
            raise HTTPException(404, "session handle unavailable")
        finally: db.close()

    @router.post("/compare")
    async def compare_post(request: Request, payload: dict = Body(...)):
        owner = _owner(request)
        dimension = payload.get("dimension", "actual_model")
        if dimension not in {"actual_model", "workspace_id"}: raise HTTPException(422, "unsupported comparison dimension")
        handles = payload.get("handles") or []
        if not isinstance(handles, list) or len(handles) != 2: raise HTTPException(422, "two comparison handles are required")
        def run(cancel_event, deadline):
            db = session_factory()
            try:
                scope, filters = _request_scope(owner, period=payload.get("period", "30d"), timezone=payload.get("timezone", "UTC"), start=payload.get("start"), end=payload.get("end"), resolution="day", values={})
                projection, truncated = admitted_events(db, scope, filters=filters, cancel_event=cancel_event, deadline=deadline)
                rows = projection.events
                choices = sorted({getattr(row, dimension, None) for row in rows if getattr(row, dimension, None)})
                catalog = IdentityCatalog(owner, dimension, choices)
                schedules = _active_price_schedules(db, cancel_event, deadline)
                resolved=[]
                for handle in handles:
                    try: choice = catalog.resolve(handle)
                    except ValueError as exc: raise HTTPException(422, "comparison choice unavailable") from exc
                    selected=[row for row in rows if getattr(row, dimension, None)==choice]
                    token_records=[]
                    for row in selected:
                        known = row.input_tokens is not None or row.output_tokens is not None
                        state = "estimated" if "estimated" in {row.input_tokens_state, row.output_tokens_state} else "reported"
                        if not known: state = "unavailable"
                        elif row.input_tokens is None or row.output_tokens is None: state = "partial"
                        token_records.append((((row.input_tokens or 0) + (row.output_tokens or 0)) if known else None, state))
                    token_total = _aggregate(token_records)
                    costs = defaultdict(int); priced = True
                    for row in selected:
                        components = price_event(row, schedules, inclusion_profile=resolve_profile(row, _PRICING_PROFILES), cancel_event=cancel_event, deadline=deadline)
                        payable = [component for component in components if component.amount is not None]
                        currencies = {component.currency for component in payable}
                        blocking = {component.reason for component in components if component.state == "unpriced"} & {"missing_required_subset", "invalid_count", "no_exact_effective_schedule"}
                        if not payable or len(currencies) != 1 or blocking:
                            priced = False
                        else: costs[next(iter(currencies))] += sum(component.amount for component in payable)
                    public = catalog.project(choice)
                    resolved.append({"label": public["label"] if public["label"] else ("Model" if dimension=="actual_model" else "Workspace"), "tokens":{**token_total,"unit":"tokens"},"sessions":len({row.session_id for row in selected if row.session_id}), "cost": {"state": "reported", "currency": next(iter(costs), None), "amount": str(next(iter(costs.values()))) } if priced and len(costs) == 1 else {"state": "unpriced"}})
                currencies = {side["cost"].get("currency") for side in resolved if side["cost"].get("state") == "reported"}
                if len(resolved) == 2 and all(side["cost"].get("state") == "reported" for side in resolved) and len(currencies) == 1:
                    from fractions import Fraction
                    left_amount, right_amount = (Fraction(side["cost"]["amount"]) for side in resolved)
                    delta = right_amount - left_amount
                    ratio = right_amount / left_amount if left_amount else None
                    comparison = {"state": "reported", "currency": next(iter(currencies)), "delta": str(delta), "ratio": ({"numerator": str(ratio.numerator), "denominator": str(ratio.denominator)} if ratio is not None else None)}
                    coverage = {"state": "complete"}
                else:
                    comparison = {"state":"unavailable","reason":"unpriced_or_incompatible_currency"}
                    coverage = {"state":"partial_unpriced"}
                return {"schema":"open-clank.stats.v1","dimension":dimension,"sides":resolved,"comparison":comparison,"coverage": {**coverage, "truncated": truncated}}
            finally: db.close()
        try:
            return await _run_bounded(run)
        except TimeoutError as exc:
            raise HTTPException(504, str(exc)) from exc
        except (StatsQueryError, PricingError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/compare")
    async def compare_legacy() -> None:
        raise HTTPException(405, "comparison requires opaque POST handles")

    @router.post("/session-handle")
    async def session_handle(request: Request):
        """Resolve the in-memory current session into a no-store Stats handle.

        The canonical session id is accepted only in this bounded POST body;
        it is never placed in a Stats URL, response, or durable browser state.
        """
        owner = _owner(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(422, "session handle payload is invalid") from exc
        raw_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(raw_id, str) or not raw_id or len(raw_id) > 256 or "\x00" in raw_id:
            raise HTTPException(422, "session handle payload is invalid")

        def run(cancel_event, deadline):
            db = session_factory()
            raw = _install_progress_handler(db, cancel_event, deadline)
            try:
                found = db.query(DbSession.id).filter(
                    DbSession.id == raw_id,
                    DbSession.owner == owner,
                ).first()
                if not found:
                    raise HTTPException(404, "session is unavailable")
                return {"handle": identity_handle(owner, "session_id", raw_id)}
            finally:
                if raw is not None:
                    raw.set_progress_handler(None, 0)
                db.close()

        result = await _run_bounded(run)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get("/quota")
    async def quota(request: Request, account_id: str | None = None, session_id: str | None = None,
                    period: str = "30d", timezone: str = "UTC"):
        try:
            owner = _owner(request)
            def run(cancel_event, deadline):
                db = session_factory()
                raw = _install_progress_handler(db, cancel_event, deadline)
                try:
                    requested_account_id = account_id
                    requested_session_id = session_id
                    if requested_account_id and not requested_account_id.startswith("account_"):
                        raise QuotaError("account_id must be an opaque identity handle")
                    if requested_session_id and not requested_session_id.startswith("session_"):
                        raise QuotaError("session_id must be an opaque identity handle")
                    if requested_session_id:
                        ids = [row[0] for row in db.query(DbSession.id).filter(DbSession.owner == owner).limit(10001).all()]
                        try:
                            requested_session_id = IdentityCatalog(owner, "session_id", ids).resolve(requested_session_id)
                        except Exception as exc:
                            raise QuotaError("requested session is unavailable") from exc
                    if requested_account_id:
                        from core.stats_models import StatsEvent
                        ids = [row[0] for row in db.query(StatsEvent.account_id).filter(
                            StatsEvent.owner == owner, StatsEvent.account_id.isnot(None)).distinct().limit(10001).all()]
                        try:
                            requested_account_id = IdentityCatalog(owner, "account_id", ids).resolve(requested_account_id)
                        except Exception as exc:
                            raise QuotaError("requested account is unavailable") from exc
                    return _public_report(quota_snapshot(db, owner=owner, account_id=requested_account_id, session_id=requested_session_id,
                                          period=period, timezone_name=timezone), owner=owner)
                except OperationalError as exc:
                    if cancel_event.is_set() or time.monotonic() >= deadline: raise TimeoutError("quota query deadline exceeded") from exc
                    raise
                finally:
                    if raw is not None: raw.set_progress_handler(None, 0)
                    db.close()
            return await _run_bounded(run)
        except TimeoutError as exc: raise HTTPException(504, str(exc)) from exc
        except (StatsQueryError, QuotaError) as exc: raise HTTPException(422, str(exc)) from exc

    return router

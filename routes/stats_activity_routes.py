"""Owner-scoped S08 activity API."""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Request, Query

from core.database import SessionLocal
from services.stats.activity import ActivityError, load_activity
from services.stats.query import StatsQueryError, parse_scope
from src.auth_helpers import effective_user, require_authenticated_request

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stats-activity")
_CAPACITY = threading.BoundedSemaphore(2)
_TIMEOUT = 10.0


def _owner(request: Request, *, test_state=False) -> str:
    require_authenticated_request(request)
    owner = str(effective_user(request) or "").strip().lower()
    if not owner:
        raise HTTPException(401, "trusted Stats owner scope is required")
    return owner


async def _run(function):
    deadline = time.monotonic() + _TIMEOUT
    cancel_event = threading.Event()
    while not _CAPACITY.acquire(blocking=False):
        if time.monotonic() >= deadline:
            raise HTTPException(503, "Stats activity capacity is busy")
        await asyncio.sleep(.01)
    try:
        future = _EXECUTOR.submit(function, cancel_event, deadline)
    except Exception:
        _CAPACITY.release()
        raise
    future.add_done_callback(lambda _: _CAPACITY.release())
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), max(0.0, deadline - time.monotonic()))
    except asyncio.TimeoutError as exc:
        cancel_event.set()
        raise HTTPException(504, "Stats activity deadline exceeded") from exc
    except asyncio.CancelledError:
        cancel_event.set()
        raise


def setup_stats_activity_routes(*, session_factory=SessionLocal, allow_test_owner_state=False) -> APIRouter:
    router = APIRouter(prefix="/api/stats/v1/activity", tags=["stats-activity"])

    @router.get("")
    async def activity(request: Request, timezone: str = "UTC", resolution: str = "day",
                       page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                       sort: str = "messages", direction: str = "desc", period: str = "all",
                       start: str | None = None, end: str | None = None,
                       provider_id: str | None = None, account_id: str | None = None,
                       actual_model: str | None = None, workspace_id: str | None = None):
        owner = _owner(request, test_state=allow_test_owner_state)
        filters = {key: value for key, value in {"provider_id": provider_id, "account_id": account_id, "actual_model": actual_model, "workspace_id": workspace_id}.items() if value}
        from routes.stats_routes import _require_opaque_filters
        try:
            _require_opaque_filters(filters)
            scope = parse_scope(owner=owner, period=period, timezone_name=timezone,
                                start=start, end=end, resolution=resolution)
            def work(cancel_event, deadline):
                db = session_factory()
                try:
                        return load_activity(db, owner=owner, timezone_name=timezone, resolution=resolution,
                                         page=page, page_size=page_size, sort=sort, direction=direction,
                                         cancel_event=cancel_event, deadline=deadline,
                                         start_utc=scope.start_utc, end_utc=scope.end_utc, filters=filters,
                                         scope={"period": scope.period, "timezone": scope.timezone,
                                                "start": scope.start_utc.isoformat() if scope.start_utc else None,
                                                "end": scope.end_utc.isoformat() if scope.end_utc else None,
                                                "resolution": scope.resolution})
                finally:
                    db.close()
            return await _run(work)
        except ActivityError as exc:
            raise HTTPException(422, str(exc)) from exc
        except StatsQueryError as exc:
            raise HTTPException(422, str(exc)) from exc
        except HTTPException:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise HTTPException(503, "Stats activity source unavailable") from exc

    return router


router = setup_stats_activity_routes()

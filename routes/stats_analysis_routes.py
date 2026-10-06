"""Standalone owner-scoped S09 analysis routes.

The loader is deliberately injected by the application boundary.  It must
return rows already authorized for the trusted owner; this module never scans
arbitrary text or accepts an owner supplied in a query/body.
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from typing import Any, Protocol
from datetime import timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.database import ChatMessage, Session as DbSession, SessionLocal
from core.stats_models import StatsEvent
from src.auth_helpers import effective_user, require_authenticated_request
from services.stats.quality import QualityError, capability, project_quality
from services.stats.trends import TrendError, project_trends
from services.stats.query import _install_progress_handler, StatsQueryError
from services.stats.query import admitted_events, parse_scope
from services.stats.tool_evidence import load_evidence, quality_events
from services.stats.privacy import identity_handle

_ANALYSIS_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stats-analysis")
_ANALYSIS_CAPACITY = threading.BoundedSemaphore(2)
_ANALYSIS_TIMEOUT = 10.0


class StatsAnalysisLoader(Protocol):
    def load_quality(self, owner: str, *, deadline: float, cancel_event=None) -> tuple[Any, Mapping[str, int] | None]: ...
    def load_messages(self, owner: str, *, deadline: float, cancel_event=None) -> Any: ...


class DatabaseStatsAnalysisLoader:
    """Bounded owner-joined loader for the S09 projections.

    Stats rows are content-free metadata. Chat content is queried only for an
    explicitly opted-in trend request and is reduced immediately to the
    in-memory projection input; it is never returned by this route.
    """

    MAX_ROWS = 100_000

    def __init__(self, session_factory=SessionLocal):
        self.session_factory = session_factory

    @staticmethod
    def _check(deadline, cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise TimeoutError("Stats analysis cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Stats analysis deadline exceeded")

    @staticmethod
    def _flags(row: StatsEvent, *, session_owner_verified=False) -> dict[str, object]:
        metadata = row.event_metadata if isinstance(row.event_metadata, Mapping) else {}
        allowed = {"prompt_unverified", "prompt_missing_verification", "context_pressure",
                    "compacted", "mid_task_compaction", "abandoned", "edit_churn",
                    "tool_failure", "retry", "streak_failure"}
        flags = {key: metadata[key] for key in allowed if isinstance(metadata.get(key), bool)}
        outcome = "success" if row.status == "complete" else "failure" if row.status == "failed" else "unknown"
        evidence = hashlib.sha256(f"s09\0{row.owner}\0{row.id}".encode()).hexdigest()[:24]
        flags.update({"owner": row.owner, "outcome": outcome, "evidence_id": f"event_{evidence}",
                      "event_time_ms": (row.event_time.replace(tzinfo=timezone.utc) if row.event_time.tzinfo is None else row.event_time).timestamp() * 1000 if row.event_time else None})
        session = identity_handle(row.owner, "session_id", row.session_id) if row.session_id and session_owner_verified else None
        flags.update(session_handle=session, source_state="available" if session else "unavailable", body_state="unavailable",
                     source_ref={"authority": "stats_event", "session_handle": session, "event_handle": f"event_{evidence}"} if session else None)
        return flags

    def load_quality(self, owner: str, *, deadline: float, cancel_event=None, scope=None):
        self._check(deadline, cancel_event)
        db = self.session_factory()
        raw = _install_progress_handler(db, cancel_event, deadline)
        try:
            if scope is not None:
                projection, truncated = admitted_events(db, scope, cancel_event=cancel_event, deadline=deadline)
                rows = list(projection.events)
            else:
                rows = (db.query(StatsEvent).filter(StatsEvent.owner == owner)
                        .order_by(StatsEvent.event_time.desc(), StatsEvent.id.desc())
                        .limit(self.MAX_ROWS + 1).all())
                self._check(deadline, cancel_event)
                truncated = len(rows) > self.MAX_ROWS
                rows = rows[:self.MAX_ROWS]
            identifiers = sorted({row.session_id for row in rows if row.session_id})
            owned = set()
            for index in range(0, len(identifiers), 500):
                self._check(deadline, cancel_event)
                owned.update(value[0] for value in db.query(DbSession.id).filter(
                    DbSession.owner == owner, DbSession.id.in_(identifiers[index:index + 500])).all())
            events = [self._flags(row, session_owner_verified=row.session_id in owned) for row in reversed(rows)]
            ordinary = load_evidence(owner, start_utc=scope.start_utc if scope else None,
                                     end_utc=scope.end_utc if scope else None,
                                     cancel_event=cancel_event, deadline=deadline)
            events.extend(quality_events(owner, ordinary))
            events.sort(key=lambda event: (event.get("event_time_ms") or 0, event["evidence_id"]))
            if len(events) > self.MAX_ROWS:
                events = events[-self.MAX_ROWS:]
                truncated = True
            truncated = truncated or ordinary["coverage"].get("state") == "partial"
            return events, {"covered": len(events), "total": len(events) + int(truncated),
                            "truncated": truncated, "ordinary_evidence": ordinary["coverage"]}
        finally:
            if raw is not None:
                raw.set_progress_handler(None, 0)
            db.close()

    def load_messages(self, owner: str, *, deadline: float, cancel_event=None):
        self._check(deadline, cancel_event)
        db = self.session_factory()
        raw = _install_progress_handler(db, cancel_event, deadline)
        try:
            rows = (db.query(ChatMessage, DbSession)
                    .join(DbSession, DbSession.id == ChatMessage.session_id)
                    .filter(DbSession.owner == owner)
                    .order_by(ChatMessage.timestamp.desc(), ChatMessage.id.desc())
                    .limit(self.MAX_ROWS + 1).all())
            self._check(deadline, cancel_event)
            truncated = len(rows) > self.MAX_ROWS
            rows = rows[:self.MAX_ROWS]
            messages = [{"owner": session.owner, "text": message.content,
                         "event_time": message.timestamp} for message, session in reversed(rows)]
            return messages, {"loaded": len(messages), "truncated": truncated}
        finally:
            if raw is not None:
                raw.set_progress_handler(None, 0)
            db.close()


class TrendQuery(BaseModel):
    # Terms exist only in the request body, so they cannot enter URL logs.
    terms: str | list[str] | None = None
    content_opt_in: bool = False
    resolution: str = Field(default="day", pattern="^(day|week|month)$")


def _trusted_owner(request: Request, *, allow_test_owner_state: bool = False) -> str:
    require_authenticated_request(request)
    owner = str(effective_user(request) or "").strip().lower()
    if not owner:
        raise HTTPException(401, "trusted Stats owner scope is required")
    return owner


def _loader(request: Request) -> StatsAnalysisLoader:
    loader = getattr(request.app.state, "stats_analysis_loader", None)
    if loader is None:
        loader = DatabaseStatsAnalysisLoader()
        request.app.state.stats_analysis_loader = loader
    if not hasattr(loader, "load_quality") or not hasattr(loader, "load_messages"):
        raise HTTPException(status_code=503, detail="Stats analysis data source unavailable")
    return loader


async def _run_bounded(function):
    deadline = time.monotonic() + _ANALYSIS_TIMEOUT
    cancel_event = threading.Event()
    while not _ANALYSIS_CAPACITY.acquire(blocking=False):
        if time.monotonic() >= deadline:
            raise HTTPException(status_code=503, detail="Stats analysis capacity is busy")
        await asyncio.sleep(0.01)
    try:
        future = _ANALYSIS_EXECUTOR.submit(function, cancel_event, deadline)
    except Exception:
        _ANALYSIS_CAPACITY.release()
        raise
    future.add_done_callback(lambda _: _ANALYSIS_CAPACITY.release())
    wrapped = asyncio.wrap_future(future)
    try:
        return await asyncio.wait_for(wrapped, timeout=max(0.0, deadline - time.monotonic()))
    except asyncio.TimeoutError as exc:
        cancel_event.set()
        raise HTTPException(status_code=504, detail="Stats analysis deadline exceeded") from exc
    except asyncio.CancelledError:
        cancel_event.set()
        raise


def setup_stats_analysis_routes(*, allow_test_owner_state: bool = False) -> APIRouter:
    router = APIRouter(prefix="/api/stats/v1/analysis", tags=["stats-analysis"])

    @router.get("/capabilities")
    async def analysis_capabilities(request: Request):
        _trusted_owner(request, allow_test_owner_state=allow_test_owner_state)
        return {
            "schema": "open-clank.stats.analysis.s09",
            "trends": {"state": "implemented", "content_opt_in_required": True},
            "quality": {"state": "implemented", "owner_scoped": True},
            "vcs": capability("vcs"),
            "insight": capability("insight"),
        }

    @router.get("/quality")
    async def quality(request: Request, period: str = "30d", timezone: str = "UTC", start: str | None = None, end: str | None = None):
        owner = _trusted_owner(request, allow_test_owner_state=allow_test_owner_state)
        loader = _loader(request)
        try:
            scope = parse_scope(owner=owner, period=period, timezone_name=timezone, start=start, end=end, resolution="day")
            def work(cancel_event, deadline):
                if isinstance(loader, DatabaseStatsAnalysisLoader):
                    loaded = loader.load_quality(owner, deadline=deadline, cancel_event=cancel_event, scope=scope)
                else:
                    loaded = loader.load_quality(owner, deadline=deadline, cancel_event=cancel_event)
                events, coverage = loaded if isinstance(loaded, tuple) else (loaded, None)
                # The projector verifies the trusted owner on each internal row.
                # Keep that join boundary private; evidence returned to clients
                # remains opaque and contains no owner field.
                internal_events = [dict(event, owner=owner) if isinstance(event, Mapping) and "owner" not in event else event for event in events]
                result = project_quality(internal_events, owner=owner, coverage=coverage,
                                         cancel_event=cancel_event, deadline=deadline)
                if isinstance(coverage, Mapping):
                    result["coverage"]["truncated"] = bool(coverage.get("truncated"))
                if isinstance(coverage, Mapping) and coverage.get("truncated"):
                    result["truncated"] = True
                    result["warnings"] = ["quality event scan reached its bound"]
                return result
            return await _run_bounded(work)
        except QualityError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except StatsQueryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Stats analysis deadline exceeded") from exc
        except HTTPException:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Stats analysis source unavailable") from exc

    @router.post("/trends/query")
    async def trends(request: Request, query: TrendQuery):
        owner = _trusted_owner(request, allow_test_owner_state=allow_test_owner_state)
        if not query.content_opt_in:
            raise HTTPException(status_code=422, detail="content_opt_in is required for trends")
        loader = _loader(request)
        try:
            def work(cancel_event, deadline):
                loaded = loader.load_messages(owner, deadline=deadline, cancel_event=cancel_event)
                messages, metadata = loaded if isinstance(loaded, tuple) else (loaded, {})
                if metadata.get("truncated"):
                    messages = list(messages)
                result = project_trends(messages, owner=owner, terms=query.terms,
                                        content_opt_in=True, resolution=query.resolution,
                                        cancel_event=cancel_event, deadline=deadline)
                if metadata.get("truncated"):
                    result["truncated"] = True
                    result["warnings"] = ["trend message scan reached its bound"]
                return result
            return await _run_bounded(work)
        except TrendError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Stats analysis deadline exceeded") from exc
        except HTTPException:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Stats analysis source unavailable") from exc

    return router


router = setup_stats_analysis_routes()

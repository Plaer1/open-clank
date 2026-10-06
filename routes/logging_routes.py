"""Native ordinary logging routes; integration supplies existing lifecycle owners."""
from __future__ import annotations

import asyncio
import inspect
import threading
import time
from typing import Callable
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Body, HTTPException, Request

from services.logging.projection import LoggingError, LoggingProjection, action_availability, semantic_search_readiness


_QUERY_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="logging-query")
_QUERY_CAPACITY = threading.BoundedSemaphore(2)


def _trusted_owner(request):
    # Preserve Stats' authenticated owner and canonical local-installation
    # identity. It never trusts request body/query owner labels.
    from routes.stats_routes import _owner
    return _owner(request)


def setup_logging_routes(*, projection: LoggingProjection | None = None,
                         session_factory=None, session_manager=None,
                         owner_resolver: Callable = _trusted_owner,
                         action_handler=None, advanced_pruner=None,
                         semantic_searcher=None, archive_owner_resolver=None, semantic_index=None):
    if projection is None:
        from src.openclank.conversation_archive import default_db_path
        if session_factory is None:
            from core.database import SessionLocal
            session_factory = SessionLocal
        projection = LoggingProjection(default_db_path(), session_factory=session_factory, archive_owner_resolver=archive_owner_resolver)
    from services.logging.semantic import ConversationSemanticSearch
    semantic = ConversationSemanticSearch(projection, semantic_index) if semantic_index is not None else None
    if semantic is not None:
        semantic_searcher = semantic.search
    router = APIRouter(prefix="/api/logging/v1", tags=["logging"])

    def identity(request):
        owner = str(owner_resolver(request) or "").strip().lower()
        if not owner:
            raise HTTPException(401, "Trusted logging owner required")
        return owner

    def expose_session_actions(item):
        item["action_availability"] = action_availability(
            item.get("host_session_available") is True,
            session_manager_available=session_manager is not None or action_handler is not None,
        )
        return item

    def translate(exc):
        code = {"not_found": 404, "stale_cursor": 409, "archive_unavailable": 503,
                "stats_unavailable": 503, "cancelled": 499, "deadline_exceeded": 504,
                "advanced_prune_required": 422}.get(exc.code, 422)
        return HTTPException(code, {"error": exc.code, "message": str(exc)})

    async def bounded(function, *args, **kwargs):
        cancelled = threading.Event()
        deadline = time.monotonic() + 5
        if not _QUERY_CAPACITY.acquire(blocking=False):
            raise HTTPException(503, {"error": "logging_query_busy"})
        try:
            future = _QUERY_EXECUTOR.submit(function, *args, **kwargs, cancel_event=cancelled, deadline=deadline)
        except Exception:
            _QUERY_CAPACITY.release()
            raise
        future.add_done_callback(lambda _done: _QUERY_CAPACITY.release())
        try:
            # Shield retains the lease until the worker exits even when HTTP
            # callers cancel a filter query while SQLite is still unwinding.
            return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), 5)
        except LoggingError as exc:
            raise translate(exc) from exc
        except ValueError as exc:
            raise HTTPException(409 if "stale" in str(exc) or "preview" in str(exc) else 422, {"error": "invalid_capture_operation", "message": str(exc)}) from exc
        except asyncio.TimeoutError as exc:
            cancelled.set()
            raise HTTPException(504, {"error": "deadline_exceeded"}) from exc
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            cancelled.set()

    def query_filters(request):
        return {k: v for k, v in request.query_params.items() if k not in {"cursor", "limit", "include_bodies", "sort", "direction", "query", "collection"}}

    @router.get("/sessions")
    async def sessions(request: Request, cursor: str | None = None, limit: int = 50,
                       sort: str = "latest", direction: str = "desc", query: str = "", collection: str = "all"):
        result = await bounded(projection.sessions, identity(request), query_filters(request), cursor=cursor, limit=limit,
                               sort=sort, direction=direction, query_text=query, collection=collection)
        result["items"] = [expose_session_actions(item) for item in result.get("items", [])]
        return result

    @router.get("/sessions/{handle}")
    async def session(request: Request, handle: str, cursor: str | None = None, limit: int = 50):
        owner = identity(request)
        session_id = await bounded(projection.resolve_session, owner, handle)
        filters = {**query_filters(request), "session_id": handle}
        result = await bounded(projection.parts, owner, filters, cursor=cursor, limit=limit)
        result["session_handle"] = handle
        result["actors"] = await bounded(projection.actor_tree, owner, handle)
        metadata = await bounded(projection._metadata, owner, [session_id])
        session_info = metadata.get(session_id, {
            "name": "Saved archive", "archived": False, "pinned": False,
            "lifecycle_state": "archive_only", "host_session_available": False,
        })
        vitals = await bounded(projection.sessions, owner, filters, limit=1)
        result["session"] = {**session_info, **next(iter(vitals.get("items", [])), {}), "handle": handle}
        expose_session_actions(result["session"])
        return result

    @router.post("/search")
    async def search(request: Request, payload: dict = Body(...)):
        owner = identity(request)
        if payload.get("mode", "text") == "semantic":
            if semantic_searcher is None:
                return await bounded(semantic_search_readiness, owner=owner, payload=payload)
            # Preserve the callback's existing owner/payload interface. The
            # query worker owns capacity until a synchronous reader exits.
            def semantic_read(*, cancel_event=None, deadline=None):
                return semantic_searcher(owner=owner, payload=payload, cancel_event=cancel_event, deadline=deadline) if semantic is not None else semantic_searcher(owner=owner, payload=payload)
            result = await bounded(semantic_read)
            if inspect.isawaitable(result):
                try:
                    return await asyncio.wait_for(result, 5)
                except asyncio.TimeoutError as exc:
                    raise HTTPException(504, {"error": "deadline_exceeded"}) from exc
            return result
        if payload.get("mode", "text") != "text":
            raise HTTPException(422, "Unknown search mode")
        return await bounded(projection.parts, owner, payload.get("filters"), cursor=payload.get("cursor"),
                             limit=payload.get("limit", 50), query_text=payload.get("query", ""), include_bodies=True)

    @router.get("/semantic/status")
    async def semantic_status(request: Request):
        if semantic is None:
            return {"health": "unavailable", "reason": "semantic_index_unavailable"}
        return await bounded(semantic.status, identity(request))

    @router.post("/semantic/build")
    async def semantic_build(request: Request, payload: dict = Body(...)):
        owner = identity(request)
        if set(payload) != {"publish"} or payload.get("publish") is not True:
            raise HTTPException(422, "Explicit publish=true is required; the index uses only the selected owner embedding binding")
        if semantic is None:
            raise HTTPException(503, {"error": "semantic_index_unavailable"})
        # Explicit generation builds may exceed the bounded read deadline.
        # Await the existing durable build/publication lifecycle, with no
        # background restart/job subsystem or hidden model selection.
        from starlette.concurrency import run_in_threadpool
        try:
            return await run_in_threadpool(semantic.build, owner=owner)
        except LoggingError as exc:
            raise translate(exc) from exc
        except Exception as exc:
            raise HTTPException(503, {"error": "semantic_build_unavailable", "message": "The conversation generation could not be published. Refresh generation status and review the selected embedding binding."}) from exc

    @router.post("/parts/get")
    async def part(request: Request, payload: dict = Body(...)):
        return await bounded(projection.get_part, identity(request), payload.get("source_ref"),
                             offset=payload.get("offset", 0), length=payload.get("length", 16384))

    @router.post("/export")
    async def export(request: Request, payload: dict = Body(...)):
        if not isinstance(payload.get("include_bodies", False), bool):
            raise HTTPException(422, "include_bodies must be boolean")
        return await bounded(projection.export, identity(request), payload.get("filters"),
                             include_bodies=payload.get("include_bodies", False), query_text=payload.get("query_text"), cursor=payload.get("cursor"),
                             limit=payload.get("limit", 100))

    @router.post("/prune/preview")
    async def prune_preview(request: Request, payload: dict = Body(default={})):
        owner = identity(request)
        if payload.get("target", "ordinary_indexes") != "ordinary_indexes":
            if advanced_pruner is None:
                raise HTTPException(503, {"error": "advanced_capture_unavailable"})
            result = await bounded(advanced_pruner, owner=owner, action="preview", payload=payload)
            return await result if inspect.isawaitable(result) else result
        return await bounded(projection.prune_preview, owner, payload)

    @router.post("/prune/apply")
    async def prune_apply(request: Request, payload: dict = Body(...)):
        owner = identity(request)
        if payload.get("target", "ordinary_indexes") != "ordinary_indexes":
            if advanced_pruner is None:
                raise HTTPException(503, {"error": "advanced_capture_unavailable"})
            result = await bounded(advanced_pruner, owner=owner, action="apply", payload=payload)
            return await result if inspect.isawaitable(result) else result
        if not payload.get("preview_id"):
            raise HTTPException(422, "preview_id required")
        return await bounded(projection.prune_apply, owner, payload["preview_id"])

    async def apply_action(request, owner, handle, payload):
        session_id = await bounded(projection.resolve_session, owner, handle)
        # Recheck current host row after archive resolution. Archive-only history
        # remains readable, but cannot establish active session authority.
        metadata = await bounded(projection._metadata, owner, [session_id])
        if session_id not in metadata:
            raise HTTPException(404, "Session unavailable")
        action = payload.get("action")
        if action_handler is not None:
            result = action_handler(request=request, owner=owner, session_id=session_id, payload=payload)
            return await result if inspect.isawaitable(result) else result
        if action in {"status", "resume"}:
            # UI follows the same owned open/resume flow; this read never sends
            # a prompt or reconstructs engine history.
            return {"status": metadata[session_id]["lifecycle_state"], "session_handle": handle,
                    "session_id": session_id, "action": action, "open_url": f"/api/history/{session_id}"}
        if action in {"trash", "archive", "restore"}:
            from src.openclank.chat_lifecycle import ChatLifecycleError, ChatLifecycleService
            service = ChatLifecycleService(session_factory=projection.session_factory, session_manager=session_manager,
                                           mimo_supervisor=getattr(request.app.state, "mimo_supervisor", None))
            try:
                await service.set_archived(owner=owner, session_id=session_id, archived=action != "restore")
            except ChatLifecycleError as exc:
                raise HTTPException(409 if exc.code == "active_run" else 503 if exc.code == "projection_busy" else 404,
                                    {"error": exc.code, "message": str(exc)}) from exc
            return {"status": "restored" if action == "restore" else "archived", "session_handle": handle}
        if action in {"pin", "unpin", "bookmark", "rename"}:
            if session_manager is None:
                raise HTTPException(503, "Session manager unavailable")
            if action == "rename":
                name = payload.get("name")
                if not isinstance(name, str) or not name.strip() or len(name) > 256:
                    raise HTTPException(422, "Name must be 1–256 characters")
                session_manager.get_session(session_id)
                session_manager.update_session_name(session_id, name.strip())
            else:
                # Existing important/star flag is the canonical pin/bookmark
                # equivalent; it retains existing deletion protection.
                session_manager.mark_important(session_id, action != "unpin")
            return {"status": "updated", "session_handle": handle}
        raise HTTPException(422, "Action requires the existing session lifecycle handler")

    @router.post("/sessions/{handle}/actions")
    async def action(request: Request, handle: str, payload: dict = Body(...)):
        return await apply_action(request, identity(request), handle, payload)

    @router.post("/sessions/actions")
    async def batch_actions(request: Request, payload: dict = Body(...)):
        handles = payload.get("handles")
        if not isinstance(handles, list) or not handles or len(handles) > 100 or any(not isinstance(h, str) for h in handles):
            raise HTTPException(422, "1–100 session handles required")
        owner = identity(request)
        # All targets are authorized before the first mutation. Each canonical
        # action still rechecks live lifecycle state and can report a conflict.
        for handle in handles:
            sid = await bounded(projection.resolve_session, owner, handle)
            if sid not in await bounded(projection._metadata, owner, [sid]):
                raise HTTPException(404, "Session unavailable")
        results = []
        for handle in dict.fromkeys(handles):
            try:
                results.append({"handle": handle, "result": await apply_action(request, owner, handle, payload)})
            except HTTPException as exc:
                results.append({"handle": handle, "status_code": exc.status_code, "error": exc.detail})
        return {"schema": "open-clank.logging.v1", "items": results}

    return router

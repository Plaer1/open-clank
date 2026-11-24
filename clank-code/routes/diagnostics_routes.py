"""Diagnostics routes — /api/db/stats, /api/rag/stats, /api/test/youtube, /api/test-research."""

import logging
import os
from typing import Dict, Any

from fastapi import APIRouter, HTTPException, Form, Request, Response

from services.youtube.youtube_handler import extract_youtube_id, extract_transcript_async
from core.constants import DEFAULT_HOST, DATA_DIR
from core.middleware import require_admin
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)


def setup_diagnostics_routes(
    rag_manager,
    rag_available: bool,
    research_handler,
    memory_vector=None,
) -> APIRouter:
    router = APIRouter(tags=["diagnostics"])

    @router.get("/api/diagnostics/buddy")
    async def buddy_diagnostics_availability(request: Request, response: Response):
        from src.auth_helpers import is_internal_tool_identity, require_user
        from src.constants import is_openthesius_debug

        owner = require_user(request)
        if is_internal_tool_identity(owner) or not is_openthesius_debug():
            raise HTTPException(404, "Buddy diagnostics unavailable")
        require_admin(request)
        auth = request.app.state.auth_manager
        account = auth.account_id(owner)
        if not account or auth.username_for_account_id(account) != owner or auth.is_account_lifecycle_fenced(owner):
            raise HTTPException(404, "Buddy diagnostics unavailable")
        response.headers["Cache-Control"] = "private, no-store"
        return {"available": True}

    @router.get("/api/diagnostics/storage")
    async def storage_inventory(request: Request, response: Response):
        import asyncio
        import threading
        import time
        from routes.stats_routes import _owner
        from routes.logging_routes import _QUERY_CAPACITY, _QUERY_EXECUTOR
        from src.openclank.logging_capture_store import CAPTURE
        from src.openclank.storage_inventory import inventory

        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Cookie, Authorization"
        owner = _owner(request)
        state = request.app.state
        auth = getattr(state, 'auth_manager', None)
        barrier = getattr(state, 'account_request_barrier', None)
        lifecycle = getattr(state, 'account_owner_lifecycle', None)
        store = getattr(lifecycle, 'artifact_store', None)
        if auth is None or barrier is None or lifecycle is None or store is None:
            raise HTTPException(503, 'Storage inventory unavailable')
        account = auth.account_id(owner)
        def current():
            try:
                return (bool(account) and auth.account_id(owner) == account
                        and auth.username_for_account_id(account) == owner
                        and not lifecycle.owner_has_active_operation(owner)
                        and getattr(request.app.state, 'account_owner_lifecycle', None) is lifecycle
                        and lifecycle.artifact_store is store)
            except Exception:
                return False
        cancelled = threading.Event()
        try:
            async with barrier.admitted(owner):
                if not current() or not _QUERY_CAPACITY.acquire(blocking=False):
                    raise HTTPException(503, 'Storage inventory unavailable')
                try:
                    future = _QUERY_EXECUTOR.submit(inventory, owner=owner, account_id=account,
                        logging_factory=CAPTURE.session_factory, artifact_factory=store._session_factory,
                        cancel=cancelled, deadline=time.monotonic()+5)
                except BaseException:
                    _QUERY_CAPACITY.release()
                    raise
                future.add_done_callback(lambda _: _QUERY_CAPACITY.release())
                task = asyncio.wrap_future(future)
                try:
                    result = await asyncio.wait_for(asyncio.shield(task), 5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    cancelled.set()
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if task.done() and not task.cancelled():
                        try: task.result()
                        except Exception: pass
                    raise
                if not current():
                    raise HTTPException(409, 'Storage authority changed')
                return result
        except asyncio.TimeoutError as exc:
            raise HTTPException(504, 'Storage inventory timed out') from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, 'Storage inventory unavailable') from exc

    @router.get("/api/diagnostics/services")
    async def get_service_health(request: Request) -> Dict[str, Any]:
        """Consolidated degraded-state report for memory/RAG, SearXNG, email,
        ntfy, and provider endpoints. Non-intrusive probes — safe to poll.
        """
        require_admin(request)
        from src.service_health import collect_service_health
        report = await collect_service_health(
            rag_manager, memory_vector, owner=effective_user(request)
        )
        app_state = request.app.state
        memory_declared = hasattr(app_state, "memory_provider")
        mimo_declared = hasattr(app_state, "mimo_supervisor")
        memory_provider = getattr(app_state, "memory_provider", None)
        memory_alive = bool(memory_provider)
        if getattr(memory_provider, "provider_id", "") == "memery":
            owner_task = getattr(memory_provider, "_owner_task", None)
            memory_alive = bool(owner_task and not owner_task.done())
        supervisor = getattr(app_state, "mimo_supervisor", None)
        mimo_alive = bool(supervisor and supervisor.is_alive())
        report["services"].extend([
            {
                "name": "memory",
                "status": "ok" if memory_alive else ("down" if memory_declared else "disabled"),
                "detail": f"{getattr(memory_provider, 'provider_id', 'none')} provider; owner-scoped facade active" if memory_alive else "Memory provider unavailable",
            },
            {
                "name": "memery",
                "status": "ok" if memory_alive and getattr(memory_provider, "provider_id", "") == "memery" else ("down" if memory_declared else "disabled"),
                "detail": "Canonical tenant-scoped memory and RAG authority" if memory_alive and getattr(memory_provider, "provider_id", "") == "memery" else "Memery provider unavailable",
            },
            {
                "name": "mimo",
                "status": "ok" if mimo_alive else ("down" if mimo_declared else "disabled"),
                "detail": "Owner-partitioned ACP runtime; automatic dream/distill disabled by default" if mimo_alive else "Open Clank agent ACP runtime unavailable",
            },
        ])
        if (memory_declared and not memory_alive) or (mimo_declared and not mimo_alive):
            report["overall"] = "down" if report["overall"] == "ok" else report["overall"]
        return report

    @router.get("/api/diagnostics/logs")
    async def get_diagnostics_logs(request: Request, limit: int = 200) -> Dict[str, Any]:
        require_admin(request)
        limit = max(1, min(limit, 1000))
        try:
            log_file = os.path.join(DATA_DIR, "logs", "app.log")
            if not os.path.exists(log_file):
                return {"status": "success", "logs": []}

            # Safe tail read of the log file (max 5MB via rotation)
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            tail_lines = lines[-limit:] if len(lines) > limit else lines
            tail_lines = [line.rstrip('\r\n') for line in tail_lines]

            return {
                "status": "success",
                "logs": tail_lines
            }
        except Exception as e:
            logger.error(f"Diagnostics logs retrieval error: {e}")
            raise HTTPException(500, f"Failed to retrieve logs: {str(e)}")

    @router.get("/api/db/stats")
    async def get_database_stats(request: Request) -> Dict[str, Any]:
        require_admin(request)
        try:
            from core.database import get_detailed_stats
            return get_detailed_stats()
        except Exception as e:
            logger.error(f"DB stats error: {e}")
            raise HTTPException(500, "Failed to retrieve database statistics")

    @router.get("/api/rag/stats")
    async def get_rag_stats(request: Request) -> Dict[str, Any]:
        require_admin(request)
        if rag_available and rag_manager:
            return rag_manager.get_stats()
        return {"error": "RAG system not available"}

    @router.get("/api/test/youtube")
    async def test_youtube(request: Request, url: str) -> Dict[str, Any]:
        require_admin(request)
        try:
            video_id = extract_youtube_id(url)
            if not video_id:
                return {"error": "Invalid YouTube URL"}

            data = await extract_transcript_async(url, video_id)
            return {
                "video_id": video_id,
                "transcript_success": data.get("success", False),
                "transcript_length": len(data.get("transcript", "")) if data.get("success") else 0,
                "transcript_preview": (data.get("transcript", "")[:500] + "...")
                    if data.get("success") and len(data.get("transcript", "")) > 500
                    else data.get("transcript", ""),
                "error": data.get("error") if not data.get("success") else None,
            }
        except Exception as e:
            return {"error": str(e)}

    @router.post("/api/test-research")
    async def test_research(request: Request, query: str = Form("What is machine learning?")) -> Dict[str, Any]:
        require_admin(request)
        try:
            endpoint = f"http://{DEFAULT_HOST}:8000/v1/chat/completions"
            model = "gpt-oss-120b"
            result = await research_handler.call_research_service(query, endpoint, model)
            return {
                "status": "success",
                "query": query,
                "result_preview": result[:200] + "..." if len(result) > 200 else result,
                "result_length": len(result),
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "query": query}

    return router

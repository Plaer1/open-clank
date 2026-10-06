"""Search routes — /api/search/config GET, /api/search POST."""

import logging
from typing import Dict, Any

from fastapi import APIRouter, Request, HTTPException

import time

from services.search import get_search_config, PROVIDER_INFO, SearchService
from services.search.service import ANTIGRAVITY_PREREQUISITES, ANTIGRAVITY_UNAVAILABLE_REASON
from services.search.core import _call_provider
from services.search.providers import _get_provider_key, _get_search_instance, _get_search_settings
from src.auth_helpers import effective_user, require_authenticated_request

logger = logging.getLogger(__name__)


async def _request_values(request: Request) -> Dict[str, Any]:
    """Accept JSON, form data, or query params for search endpoints.

    The browser UI posts FormData, while the agent's generic app_api tool
    posts JSON. FastAPI Form(...) rejects JSON with a 422 before our handler
    runs, which made the model think SearXNG was broken.
    """
    values: Dict[str, Any] = dict(request.query_params)
    content_type = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in content_type:
            body = await request.json()
            if isinstance(body, dict):
                values.update(body)
        else:
            form = await request.form()
            values.update(dict(form))
    except Exception:
        pass
    return values


def setup_search_routes(config) -> APIRouter:
    router = APIRouter(tags=["search"])

    @router.get("/api/search/config")
    async def get_search_settings(request: Request) -> Dict[str, Any]:
        require_authenticated_request(request)
        return get_search_config()

    @router.post("/api/search")
    async def do_web_search(request: Request) -> Dict[str, Any]:
        """Standalone web search — returns context string + source list.

        Used by Compare mode to pre-search once and share results across panes.
        """
        require_authenticated_request(request)
        owner = str(effective_user(request) or "").strip().lower()
        if not owner:
            raise HTTPException(401, "Authentication required")
        values = await _request_values(request)
        query = str(values.get("query") or values.get("q") or "").strip()
        if not query:
            return {"context": "", "sources": [], "error": "query is required"}
        time_filter = values.get("time_filter") or values.get("freshness")
        if time_filter is not None:
            time_filter = str(time_filter).strip() or None
        mode = str(values.get("mode") or "results").strip().lower()
        intent = str(values.get("intent") or "web").strip().lower()
        if mode not in {"results", "read", "answer"}:
            raise HTTPException(status_code=422, detail="unsupported search mode")
        if intent not in {"web", "image"}:
            raise HTTPException(status_code=422, detail="unsupported search intent")
        if time_filter is not None and time_filter.lower() not in {"day", "week", "month", "year", "pd", "pw", "pm", "py"}:
            raise HTTPException(status_code=422, detail="unsupported search freshness")
        try:
            provider = values.get("provider")
            response = await SearchService().search(query, mode=mode, provider=str(provider).strip() if provider else None,
                                                    freshness=time_filter, caller="chat", owner=owner, intent=intent,
                                                    request_id=values.get("request_id"), operation_id=values.get("operation_id"))
            sources = [result.as_dict() for result in response.results]
            context = "\n\n".join(f"{item['title']}\n{item['snippet']}" for item in sources)
            return {"context": context, "sources": sources, "result": response.as_dict(),
                    "error": response.error}
        except Exception as e:
            logger.error(f"Standalone web search failed: {e}")
            return {"context": "", "sources": [], "error": str(e)}

    @router.get("/api/search/providers")
    async def list_search_providers(request: Request):
        """Return available search providers with config status."""
        require_authenticated_request(request)
        owner = str(effective_user(request) or "").strip().lower()
        if not owner:
            raise HTTPException(401, "Authentication required")
        settings = _get_search_settings()
        mimo_preflight = {"configured": False, "selected_provider_family": None}
        if owner:
            try:
                from src.openclank.operation_router import get_operation_router
                mimo_preflight = get_operation_router().route_preflight(
                    owner=owner, purpose="search", operation="web.search",
                    include_provider_family=True,
                )
            except Exception:
                mimo_preflight = {"configured": False, "selected_provider_family": None}
        providers = []
        for pid, (label, needs_key, needs_url) in PROVIDER_INFO.items():
            if pid == "disabled":
                continue
            available = True
            has_key = bool(_get_provider_key(pid))
            has_cx = bool(str(settings.get("google_pse_cx") or "").strip()) if pid == "google_pse" else True
            if needs_key and not has_key:
                available = False
            if pid == "google_pse" and not has_cx:
                available = False
            if needs_url and pid == "searxng" and not _get_search_instance():
                available = False
            configured = available
            providers.append({
                "id": pid,
                "label": label,
                "available": available,
                "configured": configured,
                "status": "unverified" if configured else "unavailable",
                "unverified": configured,
                "reason": None if available else (
                    "Google PSE requires both an API key and CX" if pid == "google_pse"
                    else "provider credentials or endpoint are not configured"
                ),
                "selected": settings.get("search_provider") == pid,
            })
        providers.append({
            "id": "mimo",
            "label": "MiMo hosted search",
            "available": bool(mimo_preflight.get("configured") and mimo_preflight.get("selected_provider_family") == "mimo"),
            "configured": bool(mimo_preflight.get("configured") and mimo_preflight.get("selected_provider_family") == "mimo"),
            "status": "unverified" if mimo_preflight.get("configured") and mimo_preflight.get("selected_provider_family") == "mimo" else "unavailable",
            "unverified": bool(mimo_preflight.get("configured") and mimo_preflight.get("selected_provider_family") == "mimo"),
            "selected": settings.get("search_provider") == "mimo",
            "reason": None if mimo_preflight.get("configured") and mimo_preflight.get("selected_provider_family") == "mimo" else "select a qualified MiMo Search purpose binding in Provider Control",
        })
        providers.append({
            "id": "antigravity",
            "label": "Antigravity subscription search",
            "available": False,
            "configured": False,
            "status": "unavailable",
            "unverified": True,
            "selected": settings.get("search_provider") == "antigravity",
            "reason": ANTIGRAVITY_UNAVAILABLE_REASON,
            "prerequisites": list(ANTIGRAVITY_PREREQUISITES),
            "qualification": "adapter_unshipped",
        })
        return providers

    @router.post("/api/search/query")
    async def search_with_provider(request: Request) -> Dict[str, Any]:
        """Search using a specific provider. Used by compare search mode."""
        require_authenticated_request(request)
        owner = str(effective_user(request) or "").strip().lower()
        if not owner:
            raise HTTPException(401, "Authentication required")
        values = await _request_values(request)
        query = str(values.get("query") or values.get("q") or "").strip()
        provider = str(values.get("provider") or "").strip()
        intent = str(values.get("intent") or "web")
        mode = str(values.get("mode") or "results").strip().lower()
        freshness = values.get("freshness") or values.get("time_filter")
        if mode not in {"results", "read", "answer"}:
            raise HTTPException(status_code=422, detail="unsupported search mode")
        if intent not in {"web", "image"}:
            raise HTTPException(status_code=422, detail="unsupported search intent")
        if freshness is not None and str(freshness).strip().lower() not in {"day", "week", "month", "year", "pd", "pw", "pm", "py"}:
            raise HTTPException(status_code=422, detail="unsupported search freshness")
        raw_count = values.get("count") if values.get("count") is not None else values.get("limit")
        count = None
        if raw_count not in (None, ""):
            if isinstance(raw_count, bool):
                raise HTTPException(status_code=422, detail="count must be an integer from 1 to 50")
            try:
                count = int(str(raw_count))
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="count must be an integer from 1 to 50")
            if str(raw_count).strip() != str(count) or not 1 <= count <= 50:
                raise HTTPException(status_code=422, detail="count must be an integer from 1 to 50")
        if not query:
            return {"results": [], "provider": provider, "error": "query is required"}
        if provider not in PROVIDER_INFO and provider not in {"mimo", "antigravity"} or provider == "disabled":
            return {"results": [], "provider": provider, "error": "Unknown provider"}
        t0 = time.time()
        try:
            response = await SearchService().search(query, provider=provider, count=count, caller="chat", owner=owner,
                                                    mode=mode, intent=intent, freshness=freshness,
                                                    request_id=values.get("request_id"), operation_id=values.get("operation_id"))
            elapsed = round(time.time() - t0, 2)
            return {"results": [r.as_dict() for r in response.results], "provider": provider,
                    "actual_provider": response.actual_provider, "selected_provider": response.selected_provider,
                    "warnings": response.warnings, "time": elapsed, "status": response.status,
                    "error": response.error, "result": response.as_dict()}
        except Exception as e:
            elapsed = round(time.time() - t0, 2)
            logger.error(f"Search provider {provider} failed: {e}")
            return {"results": [], "provider": provider, "time": elapsed, "error": str(e)}

    return router

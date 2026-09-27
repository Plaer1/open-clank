# services/search/service.py
"""Versioned, owner-scoped search authority with legacy projections."""

import asyncio
import hashlib
import time
import copy
import json
import threading
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

from . import fetch_webpage_content, get_search_config, comprehensive_web_search
from .core import _call_provider
from .providers import PROVIDER_INFO, _get_provider_key, _get_search_settings, _get_result_count
from src.constants import SEARXNG_INSTANCE

SCHEMA_VERSION = 1
_VALID_CALLERS = {"chat", "research", "code"}
_VALID_MODES = {"results", "read", "answer"}
_VALID_INTENTS = {"web", "image"}
_CACHE: Dict[str, tuple[float, "SearchResponse"]] = {}
_CACHE_MAX = 256
_CACHE_TTL = 300.0
_FRESHNESS = {None, "day", "week", "month", "year"}
_FRESHNESS_ALIASES = {"pd": "day", "pw": "week", "pm": "month", "py": "year"}
_AUTH_FINGERPRINT = None
_AUTH_REVISION = 0
_AUTH_REVISIONS: Dict[str, tuple[str, int]] = {}
_SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="search")
_ADMISSION = threading.BoundedSemaphore(8)
_DEFAULT_CALL_PROVIDER = _call_provider
_DEFAULT_GET_SETTINGS = _get_search_settings
_DEFAULT_GET_COUNT = _get_result_count
_DEFAULT_FETCH_WEBPAGE = fetch_webpage_content


def _credential_from_snapshot(settings: dict, provider: str) -> str:
    fields = {"brave": "brave_api_key", "kagi": "kagi_api_key", "google_pse": "google_pse_key",
              "tavily": "tavily_api_key", "serper": "serper_api_key"}
    value = str(settings.get(fields.get(provider, "")) or "").strip()
    if value:
        return value
    value = str(settings.get("search_api_key") or "").strip()
    if value:
        return value
    env_names = {"brave": "DATA_BRAVE_API_KEY", "kagi": "KAGI_API_TOKEN", "google_pse": "GOOGLE_API_KEY",
                 "tavily": "TAVILY_API_KEY", "serper": "SERPER_API_KEY"}
    return os.environ.get(env_names.get(provider, ""), "").strip() if provider in env_names else ""


@dataclass(frozen=True)
class SearchRouteSnapshot:
    selected_provider: str
    actual_provider: str
    account_scope: str
    account_id: str
    revision: str
    endpoint: str = ""
    timeout: float = 15.0
    locale: str = "en"
    safe_search: str = "strict"
    extraction: str = "none"
    cancelled: Any = field(default=None, compare=False, repr=False)
    credential: Optional[str] = field(default=None, compare=False, repr=False)
    google_cx: str = ""
    expires_at: float = 0.0
    intent: str = "web"

    def remaining_timeout(self) -> float:
        if self.cancelled is not None and self.cancelled.is_set():
            raise TimeoutError("search operation cancelled")
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("search operation deadline exceeded")
        return min(self.timeout, remaining)


@dataclass
class SearchResult:
    """A single search result."""
    url: str
    title: str
    snippet: str
    content: Optional[str] = None
    provider_metadata: Dict[str, Any] = field(default_factory=dict)
    fetched_status: str = "not_requested"
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None

    def as_dict(self) -> dict:
        return {"url": self.url, "title": self.title, "snippet": self.snippet,
                "content": self.content, "provider_metadata": dict(self.provider_metadata),
                "fetched_status": self.fetched_status, "image_url": self.image_url,
                "thumbnail_url": self.thumbnail_url}


@dataclass
class SearchResponse:
    """Response from a search query."""
    query: str
    results: List[SearchResult]
    total: int
    cached: bool = False
    schema_version: int = SCHEMA_VERSION
    caller: str = "chat"
    mode: str = "results"
    selected_provider: str = ""
    actual_provider: str = ""
    account_scope: str = "deployment-service"
    account_id: str = ""
    model_route: Optional[str] = None
    status: str = "complete"
    elapsed_ms: int = 0
    answer: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    error: Optional[Dict[str, str]] = None
    search_queries: List[str] = field(default_factory=list)
    support_links: List[str] = field(default_factory=list)
    web_search_usage: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"schema_version": self.schema_version, "query": self.query,
                "caller": self.caller, "mode": self.mode,
                "selected_provider": self.selected_provider, "actual_provider": self.actual_provider,
                "account_scope": self.account_scope, "account_id": self.account_id,
                "model_route": self.model_route, "status": self.status,
                "sources": [r.as_dict() for r in self.results], "total": self.total,
                "cache": {"hit": self.cached}, "elapsed_ms": self.elapsed_ms,
                "answer": self.answer, "warnings": list(self.warnings), "error": self.error,
                "search_queries": list(self.search_queries), "support_links": list(self.support_links),
                "web_search_usage": dict(self.web_search_usage)}


class SearchService:
    """
    Web search service.

    Usage:
        service = SearchService()
        result = await service.search("python async patterns")
        for r in result.results:
            print(f"{r.title}: {r.url}")
    """

    def __init__(self, default_depth: int = 1, fetch_content: bool = True):
        self.default_depth = default_depth
        self.fetch_content_enabled = fetch_content

    async def _acquire_admission(self, snapshot: SearchRouteSnapshot) -> None:
        """Acquire shared capacity without blocking the event loop."""
        while True:
            if snapshot.cancelled is not None and snapshot.cancelled.is_set():
                raise asyncio.CancelledError()
            if time.monotonic() >= snapshot.expires_at:
                raise asyncio.TimeoutError()
            if _ADMISSION.acquire(blocking=False):
                return
            await asyncio.sleep(min(0.01, max(0.001, snapshot.expires_at - time.monotonic())))

    async def search(
        self,
        query: str,
        depth: Optional[int] = None,
        fetch_content: Optional[bool] = None,
        *, owner: str = "", caller: str = "chat", mode: Optional[str] = None,
        provider: Optional[str] = None, account_id: Optional[str] = None,
        model_route: Optional[str] = None, count: Optional[int] = None,
        freshness: Optional[str] = None, deadline: float = 30.0,
        request_id: Optional[str] = None, operation_id: Optional[str] = None,
        locale: str = "en", extraction: str = "none",
        intent: str = "web", managed_route_id: Optional[str] = None,
        preferred_account_id: Optional[str] = None,
    ) -> SearchResponse:
        """
        Search the web.

        Args:
            query: Search query
            depth: Search depth (1=quick, 2=thorough, 3=comprehensive)
            fetch_content: Whether to fetch full page content

        Returns:
            SearchResponse with results
        """
        started = time.monotonic()
        if isinstance(freshness, str):
            freshness = _FRESHNESS_ALIASES.get(freshness.strip().lower(), freshness.strip().lower())
        if mode is None:
            effective_fetch = self.fetch_content_enabled if fetch_content is None else fetch_content
            mode = "read" if effective_fetch else "results"
        if not isinstance(query, str) or not query.strip():
            return self._failure(query or "", caller, mode, provider or "", "invalid_request", "query is required", started)
        if (not isinstance(caller, str) or not isinstance(mode, str)
                or caller not in _VALID_CALLERS or mode not in _VALID_MODES):
            return self._failure(query, caller, mode, provider or "", "invalid_request", "unsupported caller or mode", started)
        if provider is not None and not isinstance(provider, str):
            return self._failure(query, caller, mode, "", "invalid_request", "provider must be a string", started)
        if account_id is not None or model_route is not None:
            return self._failure(query, caller, mode, provider or "", "unauthorized_identity", "account and model route selectors require an authority", started)
        if (not isinstance(freshness, (str, type(None))) or freshness not in _FRESHNESS
                or not isinstance(locale, str) or not locale
                or not isinstance(extraction, str) or extraction not in {"none", "text"}
                or not isinstance(intent, str) or intent not in _VALID_INTENTS):
            return self._failure(query, caller, mode, provider or "", "invalid_request", "invalid freshness, locale, or extraction", started)
        try:
            if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 50):
                raise ValueError
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not 0.01 <= deadline <= 120:
                raise ValueError
        except (TypeError, ValueError):
            return self._failure(query, caller, mode, provider or "", "invalid_request", "count or deadline out of range", started)
        settings = _get_search_settings()
        purpose_key = {"research": "research_search_provider", "code": "code_search_provider"}.get(caller, "search_provider")
        selected = provider if provider is not None else str(settings.get(purpose_key) or settings.get("search_provider") or "searxng")
        if selected == "antigravity":
            return self._failure(query, caller, mode, selected, "unsupported_capability", "Antigravity search is unverified and unavailable until an owner-bound subscription adapter is qualified", started, status="unavailable")
        if mode == "answer" and selected != "mimo":
            return self._failure(query, caller, mode, selected, "unsupported_mode", "grounded answer helper is not qualified", started)
        if selected not in PROVIDER_INFO and selected != "mimo":
            return self._failure(query, caller, mode, selected, "unsupported_provider", "provider is not registered", started)
        if intent == "image" and selected != "brave":
            return self._failure(query, caller, mode, selected, "unsupported_capability",
                                 "image intent is qualified only for Brave Search", started,
                                 status="unavailable")
        if selected == "kagi" and freshness is not None:
            return self._failure(query, caller, mode, selected, "unsupported_capability",
                                 "Kagi Search API does not expose a documented freshness parameter", started,
                                 status="unavailable")
        if selected == "mimo" and freshness is not None:
            return self._failure(query, caller, mode, selected, "unsupported_capability",
                                 "MiMo hosted search does not expose a documented freshness filter", started,
                                 status="unavailable")
        # The administrator's result count is the default for every canonical
        # caller.  Depth remains a compatibility hint only when no setting is
        # available.
        if count is None:
            configured_count = settings.get("search_result_count")
            if configured_count in (None, ""):
                count = 10 * (depth or self.default_depth)
            elif isinstance(configured_count, bool) or not isinstance(configured_count, int):
                return self._failure(query, caller, mode, selected, "invalid_request", "configured result count must be an integer", started)
            else:
                count = configured_count
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 50:
            return self._failure(query, caller, mode, selected, "invalid_request", "count out of range", started)
        account = "deployment-service"
        credential = _credential_from_snapshot(settings, selected) if selected != "duckduckgo" else ""
        revision_fields = {"provider": selected, "endpoint": str(settings.get("search_url") or ""),
                           "credential": credential,
                           "cx": str(settings.get("google_pse_cx") or "") if selected == "google_pse" else ""}
        global _AUTH_FINGERPRINT, _AUTH_REVISION
        # Keep only a one-way revision in process state.  Provider adapters
        # still receive the credential through the short-lived route snapshot,
        # but neither the auth revision table nor cache identity retains it.
        auth_material = json.dumps(revision_fields, sort_keys=True, default=str).encode("utf-8")
        auth_fingerprint = hashlib.sha256(auth_material).hexdigest()
        _AUTH_FINGERPRINT = auth_fingerprint
        if selected not in _AUTH_REVISIONS or _AUTH_REVISIONS[selected][0] != auth_fingerprint:
            _AUTH_REVISION += 1
            _AUTH_REVISIONS[selected] = (auth_fingerprint, _AUTH_REVISION)
        revision = f"config-{_AUTH_REVISIONS[selected][1]}"
        google_cx = str(settings.get("google_pse_cx") or "").strip() if selected == "google_pse" else ""
        cancelled = threading.Event()
        endpoint = str(settings.get("search_url") or SEARXNG_INSTANCE).strip().rstrip("/") if selected == "searxng" else str(settings.get("search_url") or "")
        snapshot = SearchRouteSnapshot(selected, selected, "deployment-service", account, revision,
                                       endpoint, min(15.0, float(deadline)), locale,
                                       str(settings.get("search_safesearch") or "strict"), extraction, cancelled,
                                       credential, google_cx, started + float(deadline), intent)

        # Fallbacks are a property of implicit deployment selection.  An
        # explicit provider (including a disabled or unavailable one) is never
        # substituted.  The recursive call uses an explicit provider, so it
        # cannot itself fall through this chain.
        fallback_chain = []
        if provider is None:
            configured = settings.get("search_fallback_chain") or []
            if isinstance(configured, str):
                configured = [item.strip() for item in configured.split(",")]
            if isinstance(configured, (list, tuple)):
                seen = {selected, "disabled"}
                for item in configured:
                    if isinstance(item, str) and item in PROVIDER_INFO and item not in seen:
                        fallback_chain.append(item)
                        seen.add(item)

        async def _fallback(warning: str):
            remaining = deadline - (time.monotonic() - started)
            if remaining < 0.01:
                return None
            for candidate in fallback_chain:
                remaining = deadline - (time.monotonic() - started)
                if remaining < 0.01:
                    break
                alternate = await self.search(
                    query, depth=depth, fetch_content=fetch_content,
                    owner=owner, caller=caller, mode=mode, provider=candidate,
                    count=count, freshness=freshness, deadline=remaining,
                    request_id=request_id, locale=locale, extraction=extraction,
                    intent=intent,
                )
                if alternate.status in {"complete", "partial"} and alternate.results:
                    alternate.selected_provider = selected
                    alternate.warnings.insert(0, warning)
                    alternate.elapsed_ms = int((time.monotonic() - started) * 1000)
                    return alternate
            return None
        # Request/correlation IDs identify a caller trace, not the semantic
        # query.  A provider/fetch implementation seam revision remains in the
        # key so test/runtime adapter replacement cannot reuse stale content.
        fetch_revision = "default" if fetch_webpage_content is _DEFAULT_FETCH_WEBPAGE else f"seam-{id(fetch_webpage_content)}"
        key_data = "\0".join(map(str, (owner or "", snapshot.actual_provider, snapshot.account_id, snapshot.revision,
                                         mode, intent, query.strip(), count, freshness or "", locale,
                                         snapshot.safe_search, extraction, fetch_revision,
                                         managed_route_id or "", preferred_account_id or "")))
        key = hashlib.sha256(key_data.encode()).hexdigest()
        if selected == "disabled":
            return self._failure(query, caller, mode, selected, "unavailable", "search is disabled", started, status="unavailable")
        if selected == "mimo":
            # The binder chooses the actual account only during dispatch. Until
            # that identity is known, semantic caching could cross account
            # boundaries. Durable operation replay remains the exactly-once
            # mechanism, so managed results deliberately skip this cache.
            managed_cache_allowed = False
            if not owner:
                return self._failure(query, caller, mode, selected, "unavailable",
                                     "managed MiMo search requires an authenticated owner",
                                     started, status="unavailable")
            from src.openclank.operation_router import get_operation_router
            authority = get_operation_router()
            try:
                preflight = authority.route_preflight(owner=owner, purpose="search", operation="web.search")
            except Exception:
                preflight = {"configured": False}
            selected_binding = preflight.get("selected_model_route_id")
            if managed_route_id and managed_route_id != selected_binding:
                return self._failure(query, caller, mode, selected, "unauthorized_identity",
                                     "search route is selected by the owner binding", started, status="unavailable")
            managed_route_id = selected_binding
            if not managed_route_id or not preflight.get("configured"):
                return self._failure(query, caller, mode, selected, "unavailable",
                                     "configure a Search provider binding before using managed MiMo search",
                                     started, status="unavailable")
            # Route resolution is authoritative and therefore part of the
            # semantic cache identity; never reuse an entry made before the
            # owner binding was resolved.
            key_data = "\0".join(map(str, (owner or "", snapshot.actual_provider, snapshot.account_id, snapshot.revision,
                                             mode, intent, query.strip(), count, freshness or "", locale,
                                             snapshot.safe_search, extraction, fetch_revision,
                                             managed_route_id, preferred_account_id or "")))
            key = hashlib.sha256(key_data.encode()).hexdigest()
            cached_entry = _CACHE.get(key) if managed_cache_allowed else None
            if managed_cache_allowed and cached_entry and time.monotonic() - cached_entry[0] <= _CACHE_TTL:
                cached = cached_entry[1]
                return copy.deepcopy(SearchResponse(**{**cached.__dict__, "caller": caller, "mode": mode,
                                                       "cached": True, "elapsed_ms": int((time.monotonic() - started) * 1000)}))
            try:
                from src.openclank.operation_router import ManagedOperationRequest
                operation_key = operation_id if operation_id and len(operation_id) >= 16 else f"search-{uuid.uuid4().hex}"
                managed = await authority.execute(ManagedOperationRequest(
                    owner=owner, operation="web.search", purpose="search",
                    model_route_id=managed_route_id, preferred_account_id=preferred_account_id,
                    input={"query": query.strip(), "count": count, "answer": mode == "answer"},
                    options={"deadlineMs": max(10, int(max(0.01, deadline - (time.monotonic() - started)) * 1000))},
                    root_operation_id=f"search-{hashlib.sha256(operation_key.encode()).hexdigest()[:32]}", idempotency_key=operation_key,
                ))
            except Exception as error:
                return self._failure(query, caller, mode, selected, "provider_failed", type(error).__name__, started)
            payload = managed.output
            rows = payload.get("results") if isinstance(payload, dict) else []
            results = [SearchResult(
                str(row.get("url") or ""), str(row.get("title") or ""), str(row.get("snippet") or ""),
                provider_metadata={"provider": str(row.get("provider") or "mimo"), "managed": True,
                                   **({"publishedAt": str(row["publishedAt"])} if row.get("publishedAt") else {})},
            ) for row in rows if isinstance(row, dict) and row.get("url")]
            response = SearchResponse(query.strip(), results, len(results), caller=caller, mode=mode,
                                      selected_provider=selected, actual_provider=selected,
                                      account_id=managed.selected_account_id or "", model_route=managed_route_id,
                                      status=str(payload.get("status") or "ungrounded"),
                                      answer=str(payload["answer"]) if payload.get("answer") is not None else None,
                                      search_queries=list(payload.get("searchQueries") or []),
                                      support_links=list(payload.get("supportLinks") or []),
                                      web_search_usage=dict(payload.get("webSearchUsage") or {}),
                                      elapsed_ms=int((time.monotonic() - started) * 1000))
            if managed_cache_allowed:
                _CACHE[key] = (time.monotonic(), copy.deepcopy(response))
            return response
        if selected == "google_pse" and not snapshot.google_cx:
            alternate = await _fallback("configured fallback used because Google PSE is missing a CX")
            if alternate is not None:
                return alternate
            return self._failure(query, caller, mode, selected, "unavailable", "selected provider is not configured", started, status="unavailable")
        if selected not in ("searxng", "duckduckgo") and not credential:
            alternate = await _fallback("configured fallback used because the selected provider is unavailable")
            if alternate is not None:
                return alternate
            return self._failure(query, caller, mode, selected, "unavailable", "selected provider is not configured", started, status="unavailable")
        cached_entry = _CACHE.get(key)
        if cached_entry and time.monotonic() - cached_entry[0] <= _CACHE_TTL:
            cached = cached_entry[1]
            return copy.deepcopy(SearchResponse(**{**cached.__dict__, "caller": caller, "mode": mode,
                                                   "cached": True, "elapsed_ms": int((time.monotonic() - started) * 1000)}))
        try:
            loop = asyncio.get_running_loop()
            await self._acquire_admission(snapshot)
            def run_provider():
                snapshot.remaining_timeout()
                return _call_provider(selected, query.strip(), count, freshness, snapshot)
            try:
                # Attach release to the executor future itself.  An
                # asyncio Future's callback is scheduled on the caller loop;
                # that loop may close immediately after cancellation while
                # the bounded worker is still running, leaking admission.
                worker_future = _SEARCH_EXECUTOR.submit(run_provider)
                worker_future.add_done_callback(lambda _future: _ADMISSION.release())
                future = asyncio.wrap_future(worker_future, loop=loop)
            except Exception:
                _ADMISSION.release()
                raise
            raw = await asyncio.wait_for(asyncio.shield(future), timeout=snapshot.remaining_timeout())
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except asyncio.TimeoutError:
            cancelled.set()
            return self._failure(query, caller, mode, selected, "deadline_exceeded", "search deadline exceeded", started, status="cancelled")
        except Exception as exc:
            alternate = await _fallback(f"configured fallback used after {selected} failed")
            if alternate is not None:
                return alternate
            return self._failure(query, caller, mode, selected, "provider_failed", type(exc).__name__, started)

        if not raw:
            alternate = await _fallback(f"configured fallback used after {selected} returned no results")
            if alternate is not None:
                return alternate
        results = []
        for row in (raw or []):
            if not isinstance(row, dict) or not row.get("url"):
                continue
            metadata = dict(row.get("provider_metadata") or row.get("metadata") or {})
            metadata.setdefault("provider", selected)
            results.append(SearchResult(
                str(row.get("url") or ""), str(row.get("title") or ""),
                str(row.get("snippet") or row.get("content") or ""),
                provider_metadata=metadata,
                image_url=str(row.get("image_url") or "") or None,
                thumbnail_url=str(row.get("thumbnail_url") or "") or None,
            ))
        read_failures = 0
        if mode == "read":
            for result in results:
                remaining = deadline - (time.monotonic() - started)
                if remaining <= 0:
                    cancelled.set()
                    return SearchResponse(query.strip(), results, len(results), caller=caller, mode=mode,
                                          selected_provider=selected, actual_provider=selected, account_id=account,
                                          status="partial", elapsed_ms=int((time.monotonic() - started) * 1000),
                                          warnings=["read deadline exceeded"],
                                          error={"code": "deadline_exceeded", "message": "read deadline exceeded"})
                try:
                    loop = asyncio.get_running_loop()
                    from functools import partial
                    await self._acquire_admission(snapshot)
                    def run_fetch(url=result.url, timeout=remaining):
                        snapshot.remaining_timeout()
                        return fetch_webpage_content(url, timeout=timeout, route=snapshot)
                    try:
                        worker_future = _SEARCH_EXECUTOR.submit(run_fetch)
                        worker_future.add_done_callback(lambda _future: _ADMISSION.release())
                        future = asyncio.wrap_future(worker_future, loop=loop)
                    except Exception:
                        _ADMISSION.release()
                        raise
                    fetched = await asyncio.wait_for(asyncio.shield(future), timeout=snapshot.remaining_timeout())
                    if isinstance(fetched, dict) and fetched.get("url"):
                        result.url = str(fetched["url"])
                    result.content = fetched.get("content") if isinstance(fetched, dict) else fetched
                    result.fetched_status = "fetched" if result.content else "failed"
                    if not result.content:
                        read_failures += 1
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                except (asyncio.TimeoutError, TimeoutError):
                    cancelled.set()
                    return SearchResponse(query.strip(), results, len(results), caller=caller, mode=mode,
                                          selected_provider=selected, actual_provider=selected, account_id=account,
                                          status="partial", elapsed_ms=int((time.monotonic() - started) * 1000),
                                          warnings=["read deadline exceeded"],
                                          error={"code": "deadline_exceeded", "message": "read deadline exceeded"})
                except Exception:
                    result.fetched_status = "failed"
                    read_failures += 1
        response = SearchResponse(query.strip(), results, len(results), caller=caller, mode=mode,
                                  selected_provider=selected, actual_provider=selected, account_id=account,
                                  status="partial" if read_failures and results else "complete",
                                  elapsed_ms=int((time.monotonic() - started) * 1000))
        if (not cancelled.is_set() and not snapshot.cancelled.is_set()
                and time.monotonic() < snapshot.expires_at):
            _CACHE[key] = (time.monotonic(), copy.deepcopy(response))
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        return response

    def _failure(self, query, caller, mode, provider, code, message, started, status="failed"):
        return SearchResponse(query, [], 0, caller=caller, mode=mode, selected_provider=provider,
                              actual_provider=provider, status=status,
                              elapsed_ms=int((time.monotonic() - started) * 1000),
                              error={"code": code, "message": str(message)[:300]})

    async def fetch_content(self, url: str) -> Optional[str]:
        """Fetch content from a URL."""
        result = await asyncio.to_thread(fetch_webpage_content, url)
        return result.get("content") if isinstance(result, dict) else result

    def get_config(self) -> Dict[str, Any]:
        """Get current search configuration."""
        return get_search_config()

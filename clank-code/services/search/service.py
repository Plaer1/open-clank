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
from typing import List, Optional, Dict, Any, Mapping

from . import fetch_webpage_content, get_search_config, comprehensive_web_search
from .core import _call_provider
from .providers import PROVIDER_INFO, _get_provider_key, _get_search_settings, _get_result_count
from src.constants import SEARXNG_INSTANCE

ANTIGRAVITY_PREREQUISITES = (
    "subscription_selected_account_credential_lease",
    "provider_authoritative_retrieval_source_events",
)
ANTIGRAVITY_UNAVAILABLE_REASON = (
    "Antigravity subscription adapter unshipped: upstream must expose selected-account OAuth credential leases "
    "and authoritative retrieval source events. Installed CLI keyring sign-in and Gemini API-key SDK mode do not supply this authority."
)

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


@dataclass(frozen=True)
class SearchSelection:
    snapshot: SearchRouteSnapshot
    provider: str
    count: int
    freshness: Optional[str]
    query: str


@dataclass(frozen=True)
class PreparedSearch:
    query: str
    caller: str
    mode: str
    selected: str
    owner: str
    count: int
    freshness: Optional[str]
    deadline: float
    started: float
    snapshots: tuple[SearchRouteSnapshot, ...]
    managed_prepared: Any = field(default=None, compare=False, repr=False)
    authority: Any = field(default=None, compare=False, repr=False)



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

    def prepare_managed(self, *, owner: str, route_id: Optional[str], authority: Any) -> str:
        if not owner:
            raise ValueError("managed MiMo search requires an authenticated owner")
        preflight = authority.route_preflight(owner=owner, purpose="search", operation="web.search")
        selected = preflight.get("selected_model_route_id")
        if route_id and route_id != selected:
            raise ValueError("search route is selected by the owner binding")
        if not selected or not preflight.get("configured"):
            raise ValueError("configure a Search provider binding before using managed MiMo search")
        return str(selected)

    def prepare_external(self, *, selected: str, query: str, count: int,
                         freshness: Optional[str], snapshot: SearchRouteSnapshot) -> SearchSelection:
        if selected == "mimo":
            raise ValueError("managed MiMo must use managed preparation")
        return SearchSelection(snapshot, selected, count, freshness, query)

    def execute_external(self, selection: SearchSelection) -> Any:
        if selection.provider == "mimo":
            raise ValueError("managed MiMo cannot enter external execution")
        selection.snapshot.remaining_timeout()
        return _call_provider(selection.provider, selection.query, selection.count,
                              selection.freshness, selection.snapshot)

    def project_managed(self, prepared: Any, managed: Any) -> SearchResponse:
        payload = managed.output if hasattr(managed, "output") else dict(managed or {})
        rows = payload.get("results") if isinstance(payload, Mapping) else []
        results = [SearchResult(str(row.get("url") or ""), str(row.get("title") or ""),
                                str(row.get("snippet") or ""),
                                provider_metadata={"provider": str(row.get("provider") or "mimo"), "managed": True,
                                                   **({"publishedAt": str(row["publishedAt"])} if row.get("publishedAt") else {})})
                   for row in (rows or []) if isinstance(row, Mapping) and row.get("url")]
        state = getattr(managed, "state", "complete")
        terminal_error = None
        if state in {"failed", "cancelled"}:
            code = payload.get("errorCode") if isinstance(payload, Mapping) else None
            if not isinstance(code, str) or not 1 <= len(code) <= 80 or not all(c.isascii() and (c.isalnum() or c in "_-") for c in code):
                code = "provider_failed" if state == "failed" else "cancelled"
            terminal_error = {"code": code, "message": "Managed search failed" if state == "failed" else "Managed search cancelled"}
        return SearchResponse(getattr(prepared, "query", ""), results, len(results),
                              caller=getattr(prepared, "caller", "chat"), mode=getattr(prepared, "mode", "results"),
                              selected_provider="mimo", actual_provider="mimo",
                              account_scope="model-account",
                              account_id=getattr(managed, "selected_account_id", "") or "",
                              model_route=getattr(managed, "model_route_id", None) or getattr(prepared, "managed_route_id", None),
                              status=state if state in {"failed", "cancelled"} else str(payload.get("status") or "complete"),
                              error=terminal_error,
                              answer=(str(payload["answer"]) if payload.get("answer") is not None else None),
                              search_queries=list(payload.get("searchQueries") or []),
                              support_links=list(payload.get("supportLinks") or []),
                              web_search_usage=dict(payload.get("webSearchUsage") or {}),
                              elapsed_ms=int((time.monotonic() - getattr(prepared, "started", time.monotonic())) * 1000))

    def prepare_search(
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
        preferred_account_id: Optional[str] = None, _settings_snapshot: Optional[Mapping[str, Any]] = None,
        root_operation_id: Optional[str] = None, idempotency_key: Optional[str] = None,
    ) -> SearchResponse | PreparedSearch:
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
        def _early_failure(query, caller, mode, selected, code, message, started, status="failed"):
            response = self._failure(query, caller, mode, selected, code, message, started, status=status)
            if selected == "mimo":
                response.account_scope = "subscription-unbound"
                response.actual_provider = ""
            return response
        if isinstance(freshness, str):
            freshness = _FRESHNESS_ALIASES.get(freshness.strip().lower(), freshness.strip().lower())
        if mode is None:
            effective_fetch = self.fetch_content_enabled if fetch_content is None else fetch_content
            mode = "read" if effective_fetch else "results"
        if not isinstance(query, str) or not query.strip():
            return _early_failure(query or "", caller, mode, provider or "", "invalid_request", "query is required", started)
        if (not isinstance(caller, str) or not isinstance(mode, str)
                or caller not in _VALID_CALLERS or mode not in _VALID_MODES):
            return _early_failure(query, caller, mode, provider or "", "invalid_request", "unsupported caller or mode", started)
        if provider is not None and not isinstance(provider, str):
            return _early_failure(query, caller, mode, "", "invalid_request", "provider must be a string", started)
        if account_id is not None or model_route is not None:
            return _early_failure(query, caller, mode, provider or "", "unauthorized_identity", "account and model route selectors require an authority", started)
        if (not isinstance(freshness, (str, type(None))) or freshness not in _FRESHNESS
                or not isinstance(locale, str) or not locale
                or not isinstance(extraction, str) or extraction not in {"none", "text"}
                or not isinstance(intent, str) or intent not in _VALID_INTENTS):
            return _early_failure(query, caller, mode, provider or "", "invalid_request", "invalid freshness, locale, or extraction", started)
        try:
            if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 50):
                raise ValueError
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not 0.01 <= deadline <= 120:
                raise ValueError
        except (TypeError, ValueError):
            return _early_failure(query, caller, mode, provider or "", "invalid_request", "count or deadline out of range", started)
        settings = dict(_settings_snapshot) if _settings_snapshot is not None else dict(_get_search_settings())
        purpose_key = {"research": "research_search_provider", "code": "code_search_provider"}.get(caller, "search_provider")
        selected = provider if provider is not None else str(settings.get(purpose_key) or settings.get("search_provider") or "searxng")
        if selected == "antigravity":
            response = _early_failure(query, caller, mode, selected, "unsupported_capability", ANTIGRAVITY_UNAVAILABLE_REASON, started, status="unavailable")
            response.actual_provider = ""  # No provider was dispatched.
            response.account_scope = "subscription-unbound"
            return response
        if mode == "answer" and selected != "mimo":
            return _early_failure(query, caller, mode, selected, "unsupported_mode", "grounded answer helper is not qualified", started)
        if selected not in PROVIDER_INFO and selected != "mimo":
            return _early_failure(query, caller, mode, selected, "unsupported_provider", "provider is not registered", started)
        if intent == "image" and selected != "brave":
            return _early_failure(query, caller, mode, selected, "unsupported_capability",
                                 "image intent is qualified only for Brave Search", started,
                                 status="unavailable")
        if selected == "kagi" and freshness is not None:
            return _early_failure(query, caller, mode, selected, "unsupported_capability",
                                 "Kagi Search API does not expose a documented freshness parameter", started,
                                 status="unavailable")
        if selected == "mimo" and freshness is not None:
            return _early_failure(query, caller, mode, selected, "unsupported_capability",
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
                return _early_failure(query, caller, mode, selected, "invalid_request", "configured result count must be an integer", started)
            else:
                count = configured_count
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 50:
            return _early_failure(query, caller, mode, selected, "invalid_request", "count out of range", started)
        if selected == "disabled":
            return _early_failure(query, caller, mode, selected, "unavailable", "search is disabled", started, status="unavailable")
        candidates = [] if selected == "mimo" else [selected]
        if provider is None and selected != "mimo":
            configured = settings.get("search_fallback_chain") or []
            if isinstance(configured, str):
                configured = [item.strip() for item in configured.split(",")]
            if isinstance(configured, (list, tuple)):
                for item in configured:
                    if isinstance(item, str) and item in PROVIDER_INFO and item not in candidates and item not in {"disabled", "mimo"}:
                        candidates.append(item)
        cancelled = threading.Event()
        snapshots = []
        global _AUTH_FINGERPRINT, _AUTH_REVISION
        for candidate in candidates:
            credential = _credential_from_snapshot(settings, candidate) if candidate != "duckduckgo" else ""
            endpoint = str(settings.get("search_url") or SEARXNG_INSTANCE).strip().rstrip("/") if candidate == "searxng" else str(settings.get("search_url") or "")
            cx = str(settings.get("google_pse_cx") or "").strip() if candidate == "google_pse" else ""
            material = json.dumps({"provider": candidate, "endpoint": str(settings.get("search_url") or ""), "credential": credential, "cx": cx}, sort_keys=True).encode()
            fingerprint = hashlib.sha256(material).hexdigest()
            _AUTH_FINGERPRINT = fingerprint
            if candidate not in _AUTH_REVISIONS or _AUTH_REVISIONS[candidate][0] != fingerprint:
                _AUTH_REVISION += 1
                _AUTH_REVISIONS[candidate] = (fingerprint, _AUTH_REVISION)
            snapshots.append(SearchRouteSnapshot(selected, candidate, "deployment-service", "deployment-service",
                f"config-{_AUTH_REVISIONS[candidate][1]}", endpoint, min(15.0, float(deadline)), locale,
                str(settings.get("search_safesearch") or "strict"), extraction, cancelled, credential, cx,
                started + float(deadline), intent))
        managed_prepared = authority = None
        if selected == "mimo":
            if not owner:
                return _early_failure(query, caller, mode, selected, "unavailable", "managed MiMo search requires an authenticated owner", started, status="unavailable")
            from src.openclank.operation_router import get_operation_router, ManagedOperationRequest
            authority = get_operation_router()
            try:
                route = self.prepare_managed(owner=owner, route_id=managed_route_id, authority=authority)
                operation_key = idempotency_key or (operation_id if operation_id and len(operation_id) >= 16 else f"search-{uuid.uuid4().hex}")
                root = root_operation_id or f"search-{hashlib.sha256(operation_key.encode()).hexdigest()[:32]}"
                managed_prepared = authority.prepare(ManagedOperationRequest(
                    owner=owner, operation="web.search", purpose="search", model_route_id=route,
                    preferred_account_id=preferred_account_id,
                    input={"query": query.strip(), "count": count, "answer": mode == "answer"},
                    options={"deadlineMs": max(10, int(max(0.01, deadline - (time.monotonic() - started)) * 1000))},
                    root_operation_id=root, idempotency_key=operation_key))
            except Exception as error:
                code = "unauthorized_identity" if managed_route_id and "selected by" in str(error) else "unavailable"
                return _early_failure(query, caller, mode, selected, code, str(error), started, status="unavailable")
        return PreparedSearch(query.strip(), caller, mode, selected, owner, count, freshness, float(deadline), started,
                              tuple(snapshots), managed_prepared, authority)

    async def execute_prepared_external(self, prepared: PreparedSearch) -> SearchResponse:
        if prepared.selected == "mimo" or prepared.managed_prepared is not None:
            raise ValueError("managed MiMo cannot enter external execution")
        async def run(index: int) -> SearchResponse:
            snapshot = prepared.snapshots[index]
            selected = snapshot.actual_provider
            query, caller, mode, owner = prepared.query, prepared.caller, prepared.mode, prepared.owner
            count, freshness, deadline, started = prepared.count, prepared.freshness, prepared.deadline, prepared.started
            cancelled, credential, account = snapshot.cancelled, snapshot.credential, snapshot.account_id
            intent, locale, extraction = snapshot.intent, snapshot.locale, snapshot.extraction
            async def _fallback(warning: str):
                if index:
                    return None
                for next_index in range(index + 1, len(prepared.snapshots)):
                    if time.monotonic() >= snapshot.expires_at:
                        break
                    candidate = prepared.snapshots[next_index].actual_provider
                    if (intent == "image" and candidate != "brave") or (freshness is not None and candidate == "kagi"):
                        continue
                    alternate = await run(next_index)
                    if alternate.status in {"complete", "partial"} and alternate.results:
                        alternate.selected_provider = prepared.selected
                        alternate.warnings.insert(0, warning)
                        alternate.elapsed_ms = int((time.monotonic() - started) * 1000)
                        return alternate
                return None
            fetch_revision = "default" if fetch_webpage_content is _DEFAULT_FETCH_WEBPAGE else f"seam-{id(fetch_webpage_content)}"
            key_data = "\0".join(map(str, (owner or "", selected, account, snapshot.revision, mode, intent,
                query, count, freshness or "", locale, snapshot.safe_search, extraction, fetch_revision, "", "")))
            key = hashlib.sha256(key_data.encode()).hexdigest()
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
                selection = self.prepare_external(
                    selected=selected, query=query.strip(), count=count,
                    freshness=freshness, snapshot=snapshot,
                )
                try:
                    # Attach release to the executor future itself.  An
                    # asyncio Future's callback is scheduled on the caller loop;
                    # that loop may close immediately after cancellation while
                    # the bounded worker is still running, leaking admission.
                    worker_future = _SEARCH_EXECUTOR.submit(self.execute_external, selection)
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

        return await run(0)

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
        preferred_account_id: Optional[str] = None, _settings_snapshot: Optional[Mapping[str, Any]] = None,
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
        prepared = self.prepare_search(query, depth, fetch_content, owner=owner, caller=caller, mode=mode,
            provider=provider, account_id=account_id, model_route=model_route, count=count, freshness=freshness,
            deadline=deadline, request_id=request_id, operation_id=operation_id, locale=locale, extraction=extraction,
            intent=intent, managed_route_id=managed_route_id, preferred_account_id=preferred_account_id,
            _settings_snapshot=_settings_snapshot)
        if isinstance(prepared, SearchResponse):
            return prepared
        if prepared.managed_prepared is not None:
            try:
                managed = await prepared.authority.execute_prepared(prepared.managed_prepared)
            except Exception as error:
                return self._failure(prepared.query, prepared.caller, prepared.mode, prepared.selected,
                                     "provider_failed", type(error).__name__, prepared.started)
            return self.project_managed(prepared, managed)
        return await self.execute_prepared_external(prepared)

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

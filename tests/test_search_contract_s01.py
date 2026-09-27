import asyncio
import time
import threading
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
import pytest

from services.search import service
from services.search import providers
from routes.search import search_routes
from services.search import core


def _provider(_name, query, count, time_filter, route=None):
    return [{"url": "https://example.test/one", "title": query, "snippet": "answer", "count": count}]


def test_result_contract_and_owner_cache_isolation(monkeypatch):
    service._CACHE.clear()
    monkeypatch.setattr(service, "_call_provider", _provider)
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "searxng"})
    first = asyncio.run(service.SearchService().search("q", owner="alice", mode="results"))
    second = asyncio.run(service.SearchService().search("q", owner="bob", mode="results"))
    repeat = asyncio.run(service.SearchService().search("q", owner="alice", mode="results"))
    assert first.schema_version == 1
    assert first.actual_provider == "searxng"
    assert first.status == "complete"
    assert second.cached is False
    assert repeat.cached is True


def test_explicit_provider_failure_does_not_fallback(monkeypatch):
    calls = []
    def fail(name, *_args):
        calls.append(name)
        raise RuntimeError("boom")
    monkeypatch.setattr(service, "_call_provider", fail)
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "duckduckgo"})
    result = asyncio.run(service.SearchService().search("q", provider="searxng"))
    assert result.status == "failed"
    assert result.error["code"] == "provider_failed"
    assert calls == ["searxng"]


def test_implicit_configured_fallback_reports_actual_provider_and_warning(monkeypatch):
    service._CACHE.clear()
    calls = []
    settings = {"search_provider": "searxng", "search_fallback_chain": ["duckduckgo"]}
    monkeypatch.setattr(service, "_get_search_settings", lambda: settings)
    def providers(name, *_args):
        calls.append(name)
        return [] if name == "searxng" else [{"url": "https://fallback.test", "title": "fallback"}]
    monkeypatch.setattr(service, "_call_provider", providers)
    result = asyncio.run(service.SearchService(fetch_content=False).search("fallback", mode="results"))
    assert calls == ["searxng", "duckduckgo"]
    assert result.selected_provider == "searxng"
    assert result.actual_provider == "duckduckgo"
    assert result.warnings and "fallback" in result.warnings[0]


def test_disabled_default_never_calls_configured_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_get_search_settings", lambda: {
        "search_provider": "disabled", "search_fallback_chain": ["duckduckgo"],
    })
    monkeypatch.setattr(service, "_call_provider", lambda *args: calls.append(args) or [])
    result = asyncio.run(service.SearchService(fetch_content=False).search("disabled", mode="results"))
    assert result.status == "unavailable"
    assert calls == []


def test_default_count_uses_search_setting(monkeypatch):
    seen = []
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "searxng", "search_result_count": 3})
    monkeypatch.setattr(service, "_call_provider", lambda _name, _query, count, *_args: (seen.append(count), [{"url": "https://count.test"}])[1])
    result = asyncio.run(service.SearchService(fetch_content=False).search("count", mode="results"))
    assert result.status == "complete"
    assert seen == [3]


def test_invalid_configured_count_is_typed_error(monkeypatch):
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "searxng", "search_result_count": 3.5})
    result = asyncio.run(service.SearchService(fetch_content=False).search("count", mode="results"))
    assert result.error["code"] == "invalid_request"


def test_brave_freshness_alias_is_normalized(monkeypatch):
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "brave", "brave_api_key": "fixture"})
    monkeypatch.setattr(service, "_call_provider", lambda _name, _query, _count, freshness, _route: [{"url": "https://fresh.test", "freshness": freshness}])
    result = asyncio.run(service.SearchService(fetch_content=False).search("fresh", provider="brave", freshness="pd", mode="results"))
    assert result.status == "complete"


def test_search_http_contract_validates_and_preserves_canonical_fields(monkeypatch):
    monkeypatch.setattr(search_routes, "_get_search_settings", lambda: {"google_pse_key": "key"})
    monkeypatch.setattr(search_routes, "_get_provider_key", lambda provider: "key" if provider == "google_pse" else "")
    app = FastAPI()
    app.include_router(search_routes.setup_search_routes(None))

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            providers_response = await client.get("/api/search/providers")
            google = next(item for item in providers_response.json() if item["id"] == "google_pse")
            assert google["configured"] is False
            assert google["status"] == "unavailable"
            mimo = next(item for item in providers_response.json() if item["id"] == "mimo")
            assert mimo["configured"] is False and mimo["status"] == "unavailable"
            antigravity = next(item for item in providers_response.json() if item["id"] == "antigravity")
            assert antigravity["configured"] is False and antigravity["available"] is False
            invalid_mode = await client.post("/api/search/query", json={"query": "q", "provider": "duckduckgo", "mode": "bogus"})
            invalid_intent = await client.post("/api/search/query", json={"query": "q", "provider": "duckduckgo", "intent": "bogus"})
            assert invalid_mode.status_code == 422
            assert invalid_intent.status_code == 422
            antigravity = await client.post("/api/search/query", json={"query": "q", "provider": "antigravity"})
            assert antigravity.status_code == 200
            assert antigravity.json()["status"] == "unavailable"
            assert antigravity.json()["error"]["code"] == "unsupported_capability"
    asyncio.run(exercise())


def test_search_provider_status_uses_owner_binding_without_exposing_route(monkeypatch):
    monkeypatch.setattr(search_routes, "_get_search_settings", lambda: {"search_provider": "mimo"})
    monkeypatch.setattr(search_routes, "effective_user", lambda _request: "alice")
    monkeypatch.setattr(search_routes, "_auth_disabled", lambda: False)
    import src.openclank.operation_router as operation_router
    class FakeRouter:
        def route_preflight(self, **kwargs):
            assert kwargs["owner"] == "alice"
            assert kwargs["purpose"] == "search" and kwargs["operation"] == "web.search"
            assert kwargs["include_provider_family"] is True
            return {"configured": True, "selected_model_route_id": "hidden-route",
                    "selected_provider_family": "mimo"}
    monkeypatch.setattr(operation_router, "get_operation_router", lambda: FakeRouter())
    app = FastAPI(); app.include_router(search_routes.setup_search_routes(None))
    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/search/providers")
            mimo = next(item for item in response.json() if item["id"] == "mimo")
            assert mimo["configured"] is True and mimo["available"] is True
            assert mimo["status"] == "unverified"
            assert "hidden-route" not in response.text
    asyncio.run(exercise())


def test_search_query_http_uses_configured_default_count(monkeypatch):
    seen = {}
    class FakeService:
        async def search(self, query, **kwargs):
            seen.update(query=query, **kwargs)
            return SimpleNamespace(
                results=[], actual_provider="duckduckgo", selected_provider="duckduckgo",
                status="complete", warnings=["fixture"], error=None,
                as_dict=lambda: {"actual_provider": "duckduckgo", "warnings": ["fixture"]},
            )
    monkeypatch.setattr(search_routes, "SearchService", FakeService)
    app = FastAPI()
    app.include_router(search_routes.setup_search_routes(None))
    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/search/query", json={"query": "q", "provider": "duckduckgo"})
            assert response.status_code == 200
            assert seen["count"] is None
            assert response.json()["actual_provider"] == "duckduckgo"
            assert response.json()["warnings"] == ["fixture"]
    asyncio.run(exercise())


def test_search_http_resolves_owner_search_binding_for_mimo(monkeypatch):
    seen = {}
    class FakeService:
        async def search(self, query, **kwargs):
            seen.update(query=query, **kwargs)
            return SimpleNamespace(results=[], actual_provider="mimo", selected_provider="mimo",
                                   status="complete", warnings=[], error=None,
                                   as_dict=lambda: {"status": "complete", "model_route": "route-bound"})
    monkeypatch.setattr(search_routes, "SearchService", FakeService)
    monkeypatch.setattr(search_routes, "effective_user", lambda _request: "alice")
    app = FastAPI()
    app.include_router(search_routes.setup_search_routes(None))
    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/search/query", json={
                "query": "managed", "provider": "mimo", "mode": "answer", "operation_id": "op-search-123456",
            })
            assert response.status_code == 200
            assert seen["provider"] == "mimo"
            assert "model_route" not in seen and "account_id" not in seen
            assert seen["operation_id"] == "op-search-123456"
    asyncio.run(exercise())


def test_comprehensive_reuses_canonical_read_content_once(monkeypatch):
    service._CACHE.clear()
    fetches = []
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "searxng"})
    monkeypatch.setattr(service, "_call_provider", lambda *_args: [{
        "url": "https://once.test/page", "title": "Once", "snippet": "summary",
    }])
    monkeypatch.setattr(service, "fetch_webpage_content", lambda url, **_kwargs: fetches.append(url) or {"content": "canonical body"})
    output = core.comprehensive_web_search("once", max_pages=1, min_content_length=1)
    assert "canonical body" in output
    assert fetches == ["https://once.test/page"]


def test_results_mode_does_not_fetch_and_read_mode_marks_content(monkeypatch):
    monkeypatch.setattr(service, "_call_provider", _provider)
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "searxng"})
    fetched = []
    monkeypatch.setattr(service, "fetch_webpage_content", lambda url, **_kwargs: fetched.append(url) or {"content": "body"})
    results = asyncio.run(service.SearchService().search("results", mode="results"))
    assert fetched == []
    read = asyncio.run(service.SearchService().search("read", mode="read"))
    assert read.results[0].fetched_status == "fetched"
    assert fetched


def test_disabled_is_explicit_and_answer_is_unsupported(monkeypatch):
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "disabled"})
    disabled = asyncio.run(service.SearchService().search("q"))
    assert disabled.status == "unavailable"
    answer = asyncio.run(service.SearchService().search("q", mode="answer"))
    assert answer.error["code"] == "unsupported_mode"


def test_empty_is_complete_and_provider_error_is_failed(monkeypatch):
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "searxng"})
    monkeypatch.setattr(service, "_call_provider", lambda *_args: [])
    empty = asyncio.run(service.SearchService().search("empty", owner="empty-owner"))
    assert empty.status == "complete" and empty.total == 0
    monkeypatch.setattr(service, "_call_provider", lambda *_args: (_ for _ in ()).throw(RuntimeError("down")))
    failed = asyncio.run(service.SearchService().search("failed", owner="failed-owner"))
    assert failed.status == "failed" and failed.error["code"] == "provider_failed"


def test_cache_is_deep_copied_and_config_revision_invalidates(monkeypatch):
    service._CACHE.clear()
    settings = {"search_provider": "searxng", "search_url": "https://one.test"}
    monkeypatch.setattr(service, "_get_search_settings", lambda: settings)
    monkeypatch.setattr(service, "_call_provider", _provider)
    first = asyncio.run(service.SearchService().search("cache", owner="alice"))
    first.results[0].title = "mutated"
    second = asyncio.run(service.SearchService().search("cache", owner="alice"))
    assert second.cached and second.results[0].title != "mutated"
    settings["search_url"] = "https://two.test"
    third = asyncio.run(service.SearchService().search("cache", owner="alice"))
    assert third.cached is False


def test_cache_reuses_semantic_query_across_request_ids_without_secret_retention(monkeypatch):
    service._CACHE.clear()
    service._AUTH_REVISIONS.clear()
    settings = {"search_provider": "brave", "brave_api_key": "CACHE_SECRET_ONE"}
    calls = []
    monkeypatch.setattr(service, "_get_search_settings", lambda: settings)
    monkeypatch.setattr(service, "_call_provider", lambda name, query, *_args: calls.append((name, query)) or [{"url": "https://cache.test"}])

    first = asyncio.run(service.SearchService(fetch_content=False).search("same", request_id="trace-a", mode="results"))
    second = asyncio.run(service.SearchService(fetch_content=False).search("same", request_id="trace-b", mode="results"))

    assert first.cached is False
    assert second.cached is True
    assert calls == [("brave", "same")]
    assert "CACHE_SECRET_ONE" not in repr(service._AUTH_REVISIONS)
    assert "CACHE_SECRET_ONE" not in repr(service._CACHE)
    assert "CACHE_SECRET_ONE" not in repr(service._AUTH_FINGERPRINT)


def test_credential_rotation_invalidates_semantic_cache_without_leaking_secret(monkeypatch):
    service._CACHE.clear()
    service._AUTH_REVISIONS.clear()
    settings = {"search_provider": "brave", "brave_api_key": "ROTATE_SECRET_ONE"}
    calls = []
    monkeypatch.setattr(service, "_get_search_settings", lambda: settings)
    monkeypatch.setattr(service, "_call_provider", lambda _name, _query, *_args: calls.append(1) or [{"url": "https://rotate.test"}])

    first = asyncio.run(service.SearchService(fetch_content=False).search("rotate", request_id="one", mode="results"))
    settings["brave_api_key"] = "ROTATE_SECRET_TWO"
    second = asyncio.run(service.SearchService(fetch_content=False).search("rotate", request_id="two", mode="results"))

    assert first.cached is False and second.cached is False
    assert calls == [1, 1]
    assert "ROTATE_SECRET_ONE" not in repr(service._AUTH_REVISIONS)
    assert "ROTATE_SECRET_TWO" not in repr(service._AUTH_REVISIONS)
    assert "ROTATE_SECRET_ONE" not in repr(service._CACHE)
    assert "ROTATE_SECRET_TWO" not in repr(service._CACHE)


def test_managed_mimo_search_does_not_cross_cache_unknown_binder_accounts(monkeypatch):
    service._CACHE.clear()
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "mimo"})
    calls = []

    class FakeRouter:
        def route_preflight(self, **_kwargs):
            return {"configured": True, "selected_model_route_id": "route-mimo"}

        async def execute(self, request):
            calls.append(request)
            return SimpleNamespace(selected_account_id="mimo-account", output={
                "status": "complete", "results": [{"url": "https://mimo.example", "title": "MiMo", "snippet": "annotation"}],
            })

    import src.openclank.operation_router as operation_router
    monkeypatch.setattr(operation_router, "get_operation_router", lambda: FakeRouter())
    first = asyncio.run(service.SearchService(fetch_content=False).search(
        "mimo", owner="alice", provider="mimo", request_id="trace-mimo-one", mode="results"))
    second = asyncio.run(service.SearchService(fetch_content=False).search(
        "mimo", owner="alice", provider="mimo", request_id="trace-mimo-two", mode="results"))
    assert first.actual_provider == "mimo" and first.results[0].provider_metadata["managed"] is True
    assert second.cached is False
    assert len(calls) == 2 and calls[0].model_route_id == "route-mimo"


def test_managed_mimo_answer_is_forwarded_and_fresh_operation_ids_are_not_replayed(monkeypatch):
    service._CACHE.clear()
    monkeypatch.setattr(service, "_CACHE_TTL", 0.0)
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "mimo"})
    calls = []

    class FakeRouter:
        def route_preflight(self, **_kwargs):
            return {"configured": True, "selected_model_route_id": "route-mimo"}

        async def execute(self, request):
            calls.append(request)
            return SimpleNamespace(selected_account_id="mimo-account", output={
                "status": "complete", "answer": "grounded answer",
                "results": [{"url": "https://mimo.example", "title": "MiMo", "snippet": "annotation", "publishedAt": "2026-09-26"}],
                "searchQueries": ["expanded query"], "supportLinks": ["https://support.example"],
                "webSearchUsage": {"searches": 1},
            })

    import src.openclank.operation_router as operation_router
    monkeypatch.setattr(operation_router, "get_operation_router", lambda: FakeRouter())
    first = asyncio.run(service.SearchService(fetch_content=False).search(
        "mimo answer", owner="alice", provider="mimo", managed_route_id="route-mimo", mode="answer"))
    second = asyncio.run(service.SearchService(fetch_content=False).search(
        "mimo answer", owner="alice", provider="mimo", managed_route_id="route-mimo", mode="answer"))
    assert first.answer == "grounded answer"
    assert first.search_queries == ["expanded query"] and first.support_links == ["https://support.example"]
    assert first.web_search_usage == {"searches": 1}
    assert first.results[0].provider_metadata["publishedAt"] == "2026-09-26"
    assert len(calls) == 2
    assert calls[0].idempotency_key != calls[1].idempotency_key


def test_managed_mimo_explicit_operation_id_replays_and_freshness_is_typed(monkeypatch):
    service._CACHE.clear()
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "mimo"})
    calls = []

    class FakeRouter:
        def route_preflight(self, **_kwargs):
            return {"configured": True, "selected_model_route_id": "route-mimo"}

        async def execute(self, request):
            calls.append(request)
            return SimpleNamespace(selected_account_id="mimo-account", output={"status": "complete", "results": []})

    import src.openclank.operation_router as operation_router
    monkeypatch.setattr(operation_router, "get_operation_router", lambda: FakeRouter())
    unsupported = asyncio.run(service.SearchService(fetch_content=False).search(
        "mimo", owner="alice", provider="mimo", managed_route_id="route-mimo", freshness="day"))
    assert unsupported.error["code"] == "unsupported_capability"
    first = asyncio.run(service.SearchService(fetch_content=False).search(
        "mimo", owner="alice", provider="mimo", managed_route_id="route-mimo", operation_id="operation-id-123456", request_id="trace-one"))
    second = asyncio.run(service.SearchService(fetch_content=False).search(
        "mimo", owner="alice", provider="mimo", managed_route_id="route-mimo", operation_id="operation-id-123456", request_id="trace-two"))
    assert first.status == "complete" and second.cached is False
    assert len(calls) == 2 and all(call.idempotency_key == "operation-id-123456" for call in calls)


def test_managed_mimo_without_owner_search_binding_is_actionable(monkeypatch):
    service._CACHE.clear()
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "mimo"})
    class FakeRouter:
        def route_preflight(self, **_kwargs):
            return {"configured": False, "selected_model_route_id": None}
    import src.openclank.operation_router as operation_router
    monkeypatch.setattr(operation_router, "get_operation_router", lambda: FakeRouter())
    result = asyncio.run(service.SearchService(fetch_content=False).search("mimo", owner="alice", provider="mimo"))
    assert result.status == "unavailable"
    assert result.error["code"] == "unavailable"
    assert "Search provider binding" in result.error["message"]


def test_forged_identity_and_invalid_limits_are_rejected(monkeypatch):
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "searxng"})
    forged = asyncio.run(service.SearchService().search("q", account_id="someone-else"))
    assert forged.error["code"] == "unauthorized_identity"
    invalid = asyncio.run(service.SearchService().search("q", count=0, deadline=0))
    assert invalid.error["code"] == "invalid_request"


def test_unhashable_request_types_return_typed_errors(monkeypatch):
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "searxng"})
    for kwargs in ({"caller": []}, {"mode": {}}, {"freshness": []}, {"extraction": []}, {"intent": {}}):
        result = asyncio.run(service.SearchService().search("q", **kwargs))
        assert result.error["code"] == "invalid_request"


def test_provider_metadata_survives_canonical_projection(monkeypatch):
    service._CACHE.clear()
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "searxng"})
    monkeypatch.setattr(service, "_call_provider", lambda *_args: [{
        "url": "https://metadata.test", "title": "T", "snippet": "S",
        "provider_metadata": {"rank": 2, "trace": "fixture"},
    }])
    result = asyncio.run(service.SearchService().search("metadata", mode="results"))
    assert result.results[0].provider_metadata == {"rank": 2, "trace": "fixture", "provider": "searxng"}


def test_deadline_does_not_publish_late_provider_result(monkeypatch):
    service._CACHE.clear()
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "searxng"})
    calls = {"count": 0}
    def slow(*_args):
        time.sleep(0.2)
        calls["count"] += 1
        return [{"url": "https://late.test", "title": "late"}]
    monkeypatch.setattr(service, "_call_provider", slow)
    result = asyncio.run(service.SearchService().search("slow", owner="slow-owner", deadline=0.05))
    assert result.status == "cancelled"
    time.sleep(0.25)
    assert not service._CACHE


def test_global_admission_is_shared_across_services_and_queue_deadline_expires(monkeypatch):
    service._CACHE.clear()
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "searxng"})
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = {"active": 0, "peak": 0, "total": 0}

    def blocked(*_args):
        with lock:
            calls["active"] += 1
            calls["total"] += 1
            calls["peak"] = max(calls["peak"], calls["active"])
            if calls["active"] == 8:
                entered.set()
        release.wait(1)
        with lock:
            calls["active"] -= 1
        return [{"url": "https://admission.test", "title": "ok", "snippet": "ok"}]

    monkeypatch.setattr(service, "_call_provider", blocked)

    async def exercise():
        holders = [asyncio.create_task(service.SearchService().search(f"hold-{i}", mode="results", deadline=1)) for i in range(8)]
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        queued = await service.SearchService().search("queued", mode="results", deadline=0.03)
        assert queued.status == "cancelled"
        release.set()
        return await asyncio.gather(*holders)

    results = asyncio.run(exercise())
    assert all(result.status == "complete" for result in results)
    assert calls["peak"] == 8
    assert calls["total"] == 8


def test_real_searx_adapter_uses_snapshot_timeout_and_cancel(monkeypatch):
    route = service.SearchRouteSnapshot(
        "searxng", "searxng", "deployment-service", "deployment-service", "config-1",
        endpoint="https://search.test", timeout=0.25, locale="fr", safe_search="off",
        expires_at=time.monotonic() + 0.25, cancelled=threading.Event(),
    )
    seen = {}
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"results": [{"url": "https://result.test", "title": "R", "content": "S"}]}
    def fake_get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()
    monkeypatch.setattr(providers.httpx, "get", fake_get)
    rows = providers.searxng_search_api("q", 1, route=route)
    assert rows[0]["url"] == "https://result.test"
    assert seen["timeout"] <= 0.25
    assert seen["params"]["language"] == "fr"
    route.cancelled.set()
    with pytest.raises(TimeoutError):
        providers.searxng_search_api("q", 1, route=route)


def test_kagi_v1_uses_bot_authorization_and_web_projection(monkeypatch):
    route = service.SearchRouteSnapshot(
        "kagi", "kagi", "deployment-service", "deployment-service", "config-1",
        timeout=2, credential="kagi-fixture", expires_at=time.monotonic() + 2,
    )
    seen = {}
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"data": [{"t": 0, "url": "https://kagi.test", "title": "K", "snippet": "S"}, {"t": 1, "url": "https://other.test"}]}
    def fake_get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()
    monkeypatch.setattr(providers.httpx, "get", fake_get)
    rows = providers.kagi_search("q", 3, time_filter="day", route=route)
    assert rows[0]["url"] == "https://kagi.test"
    assert seen["url"] == "https://kagi.com/api/v1/search"
    assert seen["headers"]["Authorization"] == "Bot kagi-fixture"
    assert "date_range" not in seen["params"]


def test_image_intent_requires_brave_and_kagi_rejects_undocumented_freshness(monkeypatch):
    monkeypatch.setattr(service, "_get_search_settings", lambda: {
        "search_provider": "kagi", "kagi_api_key": "fixture-token",
    })
    image = asyncio.run(service.SearchService(fetch_content=False).search("cats", intent="image"))
    assert image.status == "unavailable"
    assert image.error["code"] == "unsupported_capability"
    freshness = asyncio.run(service.SearchService(fetch_content=False).search("news", provider="kagi", freshness="day"))
    assert freshness.status == "unavailable"
    assert freshness.error["code"] == "unsupported_capability"


def test_kagi_error_envelope_and_malformed_rows_are_typed_failures(monkeypatch):
    route = service.SearchRouteSnapshot(
        "kagi", "kagi", "deployment-service", "deployment-service", "config-1",
        timeout=2, credential="kagi-fixture", expires_at=time.monotonic() + 2,
    )
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"error": {"code": "RATE_LIMITED"}}
    monkeypatch.setattr(providers.httpx, "get", lambda *args, **kwargs: Response())
    with pytest.raises(ValueError):
        providers.kagi_search("q", 3, route=route)

    class MalformedResponse(Response):
        def json(self): return {"data": [{"t": 0, "url": "https://valid.test"}, "bad-row"]}
    monkeypatch.setattr(providers.httpx, "get", lambda *args, **kwargs: MalformedResponse())
    with pytest.raises(ValueError):
        providers.kagi_search("q", 3, route=route)


def test_brave_image_roles_preserve_source_original_and_thumbnail(monkeypatch):
    route = service.SearchRouteSnapshot(
        "brave", "brave", "deployment-service", "deployment-service", "config-1",
        timeout=2, credential="brave-fixture", intent="image", expires_at=time.monotonic() + 2,
    )
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"results": [{"title": "Image", "url": "https://page.test",
                                  "properties": {"url": "https://cdn.test/original.jpg"},
                                  "thumbnail": {"src": "https://cdn.test/thumb.jpg"}}]}
    monkeypatch.setattr(providers.httpx, "get", lambda *args, **kwargs: Response())
    rows = providers.brave_search("cats", 1, route=route)
    assert rows[0]["url"] == "https://page.test"
    assert rows[0]["image_url"] == "https://cdn.test/original.jpg"
    assert rows[0]["thumbnail_url"] == "https://cdn.test/thumb.jpg"
    assert rows[0]["provider_metadata"]["provenance"]["source_page"] == "https://page.test"


def test_read_deadline_returns_all_discovered_rows_as_partial(monkeypatch):
    service._CACHE.clear()
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "searxng"})
    monkeypatch.setattr(service, "_call_provider", lambda *_args: [
        {"url": "https://first.test", "title": "First", "provider_metadata": {"rank": 1}},
        {"url": "https://second.test", "title": "Second", "provider_metadata": {"rank": 2}},
    ])
    def fake_fetch(url, **_kwargs):
        if url.endswith("second.test"):
            time.sleep(0.2)
        return {"content": "first body"}
    monkeypatch.setattr(service, "fetch_webpage_content", fake_fetch)
    response = asyncio.run(service.SearchService().search("read deadline", mode="read", deadline=0.05))
    assert response.status == "partial"
    assert [item.url for item in response.results] == ["https://first.test", "https://second.test"]
    assert response.results[0].content == "first body"
    assert response.results[1].fetched_status == "not_requested"
    assert response.error["code"] == "deadline_exceeded"


def test_antigravity_is_explicitly_unavailable_until_bound_adapter(monkeypatch):
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "antigravity"})
    result = asyncio.run(service.SearchService(fetch_content=False).search("audit", mode="results"))
    assert result.status == "unavailable"
    assert result.actual_provider == "antigravity"
    assert result.error["code"] == "unsupported_capability"

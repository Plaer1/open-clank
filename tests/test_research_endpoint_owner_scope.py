"""Managed-provider regressions for Deep Research route selection.

Research used to resolve ``ModelEndpoint`` rows and pass decrypted transport
details into the background handler.  The route layer now projects only stable
normalized identities; the managed operation router resolves provider authority
again at execution time under the authenticated owner.
"""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from routes.research import research_routes
from src import deep_research as deep_research_module
from src import research_handler as research_handler_module
from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL


def _managed_summary(**kwargs):
    assert kwargs == {
        "owner": "alice",
        "purpose": "research",
        "operation": "chat.complete",
    }
    return {
        "model_route_id": "pmr_research",
        "model_id": "research-model",
        "model_name": "Research Model",
        "connection_id": "pcn_research",
    }


def _request():
    return SimpleNamespace(
        headers={},
        state=SimpleNamespace(current_user="alice", api_token=False),
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_manager=SimpleNamespace(
                    is_configured=True,
                    get_privileges=lambda _owner: {},
                ),
            ),
        ),
    )


def _route(router, path, method):
    return next(
        route.endpoint
        for route in router.routes
        if route.path == path and method in route.methods
    )


def _body(**overrides):
    values = {
        "query": "What changed?",
        "max_rounds": 3,
        "search_provider": None,
        "endpoint_id": None,
        "model": None,
        "max_time": 300,
        "extraction_timeout": None,
        "extraction_concurrency": None,
        "category": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_chat_compat_projection_is_managed_and_credential_free(monkeypatch):
    calls = []

    def summary(**kwargs):
        calls.append(kwargs)
        return _managed_summary(**kwargs)

    monkeypatch.setattr(research_routes, "managed_route_summary", summary)
    sess = SimpleNamespace(
        owner="alice",
        endpoint_url="https://legacy.invalid/v1",
        model="legacy-model",
        headers={"Authorization": "must-not-project"},
    )

    endpoint, model, headers = research_routes._resolve_research_endpoint(sess)

    assert endpoint == MANAGED_ENGINE_PUBLIC_URL
    assert model == "Research Model"
    assert headers == {}
    assert calls[0]["owner"] == "alice"


def test_exact_panel_selection_resolves_only_normalized_owner_identity(monkeypatch):
    selected = SimpleNamespace(
        model_route_id="pmr_selected",
        provider_model_id="selected-model",
        provider_grant_id="psg_selected",
        operations=("chat.complete",),
    )
    calls = []

    def resolve(**kwargs):
        calls.append(kwargs)
        return selected

    monkeypatch.setattr(research_routes, "resolve_chat_route", resolve)

    result = research_routes._requested_research_route(
        "alice",
        "share:psg_selected",
        "selected-model",
    )

    assert result is selected
    assert calls == [{
        "owner": "alice",
        "endpoint_id": "share:psg_selected",
        "model_id": "selected-model",
    }]


def test_research_start_uses_owner_purpose_binding_without_transport(monkeypatch):
    handler = MagicMock()
    handler._active_tasks = {}
    monkeypatch.setattr(research_routes, "managed_route_summary", _managed_summary)
    monkeypatch.setattr(
        "src.auth_helpers.require_privilege",
        lambda _request, privilege: "alice" if privilege == "can_use_research" else None,
    )
    router = research_routes.setup_research_routes(handler)
    endpoint = _route(router, "/api/research/start", "POST")

    result = asyncio.run(endpoint(body=_body(), request=_request()))

    assert result["status"] == "running"
    kwargs = handler.start_research.call_args.kwargs
    assert kwargs["owner"] == "alice"
    assert kwargs["llm_model"] == "research-model"
    assert kwargs["model_route_id"] is None
    assert kwargs["grant_id"] is None
    assert kwargs["root_operation_id"] == result["session_id"]
    assert "llm_endpoint" not in kwargs
    assert "llm_headers" not in kwargs


def test_research_start_preserves_explicit_route_and_share_grant(monkeypatch):
    handler = MagicMock()
    handler._active_tasks = {}
    selected = SimpleNamespace(
        model_route_id="pmr_shared",
        provider_model_id="shared-model",
        provider_grant_id="psg_shared",
    )
    monkeypatch.setattr(
        research_routes,
        "_requested_research_route",
        lambda *_args: selected,
    )
    monkeypatch.setattr(
        "src.auth_helpers.require_privilege",
        lambda _request, _privilege: "alice",
    )
    router = research_routes.setup_research_routes(handler)
    endpoint = _route(router, "/api/research/start", "POST")

    asyncio.run(endpoint(
        body=_body(endpoint_id="share:psg_shared", model="shared-model"),
        request=_request(),
    ))

    kwargs = handler.start_research.call_args.kwargs
    assert kwargs["llm_model"] == "shared-model"
    assert kwargs["model_route_id"] == "pmr_shared"
    assert kwargs["grant_id"] == "psg_shared"


def test_research_route_source_has_no_retired_provider_transport():
    source = inspect.getsource(research_routes)
    for retired in (
        "llm_call_async",
        "ModelEndpoint",
        "ProviderAuthSession",
        "api_key",
        "base_url",
        "resolve_endpoint_runtime",
    ):
        assert retired not in source


def test_research_completion_sources_have_no_direct_llm_transport():
    for source in (
        inspect.getsource(deep_research_module),
        inspect.getsource(research_handler_module),
    ):
        assert "llm_call_async" not in source
        assert "ModelEndpoint" not in source
        assert "complete_text(" in source
        assert 'purpose="research"' in source or "purpose=self.purpose" in source


@pytest.mark.parametrize("blocked", [True, False])
def test_research_start_enforces_model_privileges(monkeypatch, blocked):
    handler = MagicMock()
    handler._active_tasks = {}
    monkeypatch.setattr(research_routes, "managed_route_summary", _managed_summary)
    monkeypatch.setattr(
        "src.auth_helpers.require_privilege",
        lambda _request, _privilege: "alice",
    )
    request = _request()
    request.app.state.auth_manager.get_privileges = lambda _owner: (
        {"block_all_models": True}
        if blocked
        else {"allowed_models_restricted": True, "allowed_models": ["pmr_research"]}
    )
    endpoint = _route(
        research_routes.setup_research_routes(handler),
        "/api/research/start",
        "POST",
    )

    if blocked:
        with pytest.raises(Exception) as exc_info:
            asyncio.run(endpoint(body=_body(), request=request))
        assert getattr(exc_info.value, "status_code", None) == 403
    else:
        asyncio.run(endpoint(body=_body(), request=request))
        handler.start_research.assert_called_once()

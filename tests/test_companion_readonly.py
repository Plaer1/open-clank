"""Owner, share, privilege, and secrecy tests for companion model inventory."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import companion.routes as companion_routes
from companion.routes import setup_companion_routes, token_owner
from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL


ROOT = Path(__file__).resolve().parents[1]


def _request(*, privileges=None, **state):
    auth_manager = SimpleNamespace(
        get_privileges=lambda _owner: privileges or {},
    )
    return SimpleNamespace(
        state=SimpleNamespace(**state),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_manager)),
    )


def _route(
    endpoint_id: str,
    route_id: str,
    model: str,
    *,
    label: str,
    tools: bool = False,
    shared: bool = False,
    disclosed_owner: str | None = None,
):
    return SimpleNamespace(
        public_endpoint_id=endpoint_id,
        model_route_id=route_id,
        provider_model_id=model,
        display_name=model,
        connection_label=label,
        share_label=label if shared else None,
        disclosed_owner=disclosed_owner,
        capabilities={"tools": tools},
    )


def _models_route():
    for route in setup_companion_routes().routes:
        if getattr(route, "path", "") == "/api/companion/models":
            assert "GET" in getattr(route, "methods", set())
            return route.endpoint
    raise AssertionError("GET /api/companion/models route not found")


def _call_models_route(monkeypatch, request, *, own=(), shared=()):
    captured = {}

    def list_routes(owner):
        captured["owner"] = owner
        return list(own), list(shared)

    monkeypatch.setattr(companion_routes, "list_chat_routes", list_routes)
    response = _models_route()(request)
    return response["endpoints"], captured


def test_token_owner_bearer_resolves_to_token_owner():
    request = _request(api_token=True, api_token_owner="alice", current_user="api")
    assert token_owner(request) == "alice"


def test_token_owner_cookie_uses_logged_in_user():
    request = _request(api_token=False, current_user="alice")
    assert token_owner(request) == "alice"


def test_token_owner_none_when_unresolved():
    request = _request(api_token=True, api_token_owner=None, current_user="api")
    assert token_owner(request) is None


def test_models_route_scopes_cookie_user_and_includes_accepted_shares(monkeypatch):
    monkeypatch.setattr(companion_routes, "get_current_user", lambda _request: "alice")
    own = _route("pcn_alice", "pmr_alice", "own-model", label="Alice provider")
    shared = _route(
        "share:grant-1",
        "pmr_shared",
        "shared-model",
        label="Team plan",
        shared=True,
    )

    endpoints, captured = _call_models_route(
        monkeypatch,
        _request(api_token=False, current_user="alice"),
        own=[own],
        shared=[shared],
    )

    assert captured == {"owner": "alice"}
    assert [item["endpoint_id"] for item in endpoints] == [
        "pcn_alice",
        "share:grant-1",
    ]


def test_models_route_scopes_api_token_to_real_owner(monkeypatch):
    monkeypatch.setattr(companion_routes, "get_current_user", lambda _request: "api")
    route = _route("pcn_alice", "pmr_alice", "model-1", label="Alice provider")

    endpoints, captured = _call_models_route(
        monkeypatch,
        _request(
            api_token=True,
            api_token_owner="alice",
            api_token_scopes=["chat"],
            current_user="api",
        ),
        own=[route],
    )

    assert captured == {"owner": "alice"}
    assert [item["models"] for item in endpoints] == [["model-1"]]


def test_models_route_rejects_api_token_without_chat_scope(monkeypatch):
    monkeypatch.setattr(companion_routes, "get_current_user", lambda _request: "api")

    with pytest.raises(HTTPException) as exc:
        _models_route()(
            _request(
                api_token=True,
                api_token_owner="alice",
                api_token_scopes=["todos:read"],
                current_user="api",
            )
        )

    assert exc.value.status_code == 403
    assert "chat scope" in exc.value.detail


def test_models_route_rejects_api_token_without_owner(monkeypatch):
    monkeypatch.setattr(companion_routes, "get_current_user", lambda _request: None)

    with pytest.raises(HTTPException) as exc:
        _models_route()(
            _request(
                api_token=True,
                api_token_owner=None,
                api_token_scopes=["chat"],
                current_user="api",
            )
        )

    assert exc.value.status_code == 403
    assert "owner" in exc.value.detail


def test_models_route_projects_only_managed_secret_free_fields(monkeypatch):
    monkeypatch.setattr(companion_routes, "get_current_user", lambda _request: "alice")
    routes = [
        _route("pcn_alice", "pmr_b", "model-b", label="Private URL", tools=True),
        _route("pcn_alice", "pmr_a", "model-a", label="Private URL"),
    ]

    endpoints, _ = _call_models_route(
        monkeypatch,
        _request(api_token=False, current_user="alice"),
        own=routes,
    )

    assert endpoints == [{
        "endpoint_id": "pcn_alice",
        "name": "Private URL",
        "endpoint_url": MANAGED_ENGINE_PUBLIC_URL,
        "models": ["model-a", "model-b"],
        "supports_tools": True,
    }]
    assert set(endpoints[0]) == {
        "endpoint_id",
        "name",
        "endpoint_url",
        "models",
        "supports_tools",
    }
    assert "api_key" not in repr(endpoints)
    assert "Authorization" not in repr(endpoints)
    assert "https://" not in repr(endpoints)


@pytest.mark.parametrize(
    "allowed",
    [
        ["allowed-model"],
        ["pmr_allowed"],
    ],
)
def test_models_route_enforces_model_or_stable_route_allowlist(monkeypatch, allowed):
    monkeypatch.setattr(companion_routes, "get_current_user", lambda _request: "alice")
    routes = [
        _route("pcn_alice", "pmr_allowed", "allowed-model", label="Provider"),
        _route("pcn_alice", "pmr_denied", "denied-model", label="Provider"),
    ]

    endpoints, _ = _call_models_route(
        monkeypatch,
        _request(
            api_token=False,
            current_user="alice",
            privileges={
                "allowed_models_restricted": True,
                "allowed_models": allowed,
            },
        ),
        own=routes,
    )

    assert endpoints[0]["models"] == ["allowed-model"]


def test_models_route_block_all_models_returns_empty_catalogue(monkeypatch):
    monkeypatch.setattr(companion_routes, "get_current_user", lambda _request: "alice")
    route = _route("pcn_alice", "pmr_alice", "model-1", label="Provider")

    endpoints, _ = _call_models_route(
        monkeypatch,
        _request(
            api_token=False,
            current_user="alice",
            privileges={"block_all_models": True},
        ),
        own=[route],
    )

    assert endpoints == []


def test_models_route_fails_closed_when_normalized_catalogue_fails(monkeypatch):
    monkeypatch.setattr(companion_routes, "get_current_user", lambda _request: "alice")
    monkeypatch.setattr(
        companion_routes,
        "list_chat_routes",
        lambda _owner: (_ for _ in ()).throw(RuntimeError("private failure")),
    )

    with pytest.raises(HTTPException) as exc:
        _models_route()(_request(api_token=False, current_user="alice"))

    assert exc.value.status_code == 503
    assert exc.value.detail == "Provider catalogue is unavailable"


def test_companion_source_has_no_legacy_provider_authority():
    source = (ROOT / "companion" / "routes.py").read_text(encoding="utf-8")

    assert "ModelEndpoint" not in source
    assert "endpoint_resolver" not in source
    assert "base_url" not in source
    assert "api_key" not in source

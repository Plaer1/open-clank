"""Durable default-chat selection uses normalized provider route bindings."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import routes.provider_v1_routes as provider_routes
from core.provider_models import ProviderBase
from routes.provider_v1_routes import setup_provider_v1_routes
from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL
from src.openclank.provider_store import ProviderStore


@pytest.fixture()
def provider_store():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield ProviderStore(factory)
    engine.dispose()


def _request(owner: str = "alice", *, privileges: dict | None = None):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=owner, api_token=False),
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_manager=SimpleNamespace(
                    is_configured=True,
                    get_privileges=lambda _owner: privileges or {},
                    is_admin=lambda _owner: False,
                ),
            ),
        ),
    )


def _endpoint(router, path: str, method: str = "GET"):
    pending = list(reversed(router.routes))
    while pending:
        route = pending.pop(0)
        if getattr(route, "path", None) == path and method in route.methods:
            return route.endpoint
        included = getattr(route, "original_router", None)
        if included is not None:
            pending[:0] = reversed(included.routes)
    raise LookupError(f"No {method} route for {path}")


def _default_chat(store: ProviderStore, request):
    router = setup_provider_v1_routes(store, object())
    return _endpoint(router, "/api/default-chat")(request)


def _add_route(
    store: ProviderStore,
    *,
    owner: str,
    connection_id: str,
    model_route_id: str,
    model_id: str,
    operations=("chat.stream", "chat.complete"),
):
    store.create_connection(
        owner=owner,
        connection_id=connection_id,
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label=connection_id,
    )
    return store.create_model_route(
        owner=owner,
        connection_id=connection_id,
        model_route_id=model_route_id,
        provider_model_id=model_id,
        display_name=model_id,
        operations=operations,
    )


def test_default_chat_is_empty_without_a_normalized_chat_route(provider_store):
    assert _default_chat(provider_store, _request()) == {
        "endpoint_id": "",
        "endpoint_url": "",
        "model": "",
    }


def test_chat_binding_order_is_the_durable_default_and_fallback(provider_store):
    first = _add_route(
        provider_store,
        owner="alice",
        connection_id="pcn_first",
        model_route_id="pmr_first",
        model_id="model-first",
    )
    preferred = _add_route(
        provider_store,
        owner="alice",
        connection_id="pcn_preferred",
        model_route_id="pmr_preferred",
        model_id="model-preferred",
    )
    bindings = provider_store.put_route_binding(
        owner="alice",
        purpose="chat",
        model_route_ids=[preferred.id, first.id],
        expected_revision=0,
        enabled_by_route={preferred.id: False},
    )

    assert [binding.model_route_id for binding in bindings] == [
        preferred.id,
        first.id,
    ]
    assert _default_chat(provider_store, _request()) == {
        "endpoint_id": "pcn_first",
        "endpoint_url": MANAGED_ENGINE_PUBLIC_URL,
        "model": "model-first",
    }


def test_enabled_bound_route_precedes_catalog_order(provider_store):
    first = _add_route(
        provider_store,
        owner="alice",
        connection_id="pcn_a_catalog_first",
        model_route_id="pmr_catalog_first",
        model_id="catalog-first",
    )
    preferred = _add_route(
        provider_store,
        owner="alice",
        connection_id="pcn_z_preferred",
        model_route_id="pmr_preferred",
        model_id="preferred",
    )
    provider_store.put_route_binding(
        owner="alice",
        purpose="chat",
        model_route_ids=[preferred.id, first.id],
        expected_revision=0,
    )

    assert _default_chat(provider_store, _request()) == {
        "endpoint_id": "pcn_z_preferred",
        "endpoint_url": MANAGED_ENGINE_PUBLIC_URL,
        "model": "preferred",
    }


def test_legacy_global_default_settings_do_not_override_route_bindings(
    monkeypatch,
    provider_store,
):
    route = _add_route(
        provider_store,
        owner="alice",
        connection_id="pcn_normalized",
        model_route_id="pmr_normalized",
        model_id="normalized-model",
    )
    provider_store.put_route_binding(
        owner="alice",
        purpose="chat",
        model_route_ids=[route.id],
        expected_revision=0,
    )
    monkeypatch.setattr(
        provider_routes,
        "load_settings",
        lambda: {
            "default_endpoint_id": "legacy-global-endpoint",
            "default_model": "legacy-global-model",
            "share_defaults_with_users": True,
        },
    )

    result = _default_chat(provider_store, _request())

    assert result == {
        "endpoint_id": "pcn_normalized",
        "endpoint_url": MANAGED_ENGINE_PUBLIC_URL,
        "model": "normalized-model",
    }
    assert "legacy" not in str(result)


def test_default_chat_bindings_are_owner_scoped(provider_store):
    alice = _add_route(
        provider_store,
        owner="alice",
        connection_id="pcn_alice",
        model_route_id="pmr_alice",
        model_id="alice-model",
    )
    bob = _add_route(
        provider_store,
        owner="bob",
        connection_id="pcn_bob",
        model_route_id="pmr_bob",
        model_id="bob-model",
    )
    provider_store.put_route_binding(
        owner="alice",
        purpose="chat",
        model_route_ids=[alice.id],
        expected_revision=0,
    )
    provider_store.put_route_binding(
        owner="bob",
        purpose="chat",
        model_route_ids=[bob.id],
        expected_revision=0,
    )

    assert _default_chat(provider_store, _request("bob")) == {
        "endpoint_id": "pcn_bob",
        "endpoint_url": MANAGED_ENGINE_PUBLIC_URL,
        "model": "bob-model",
    }


def test_allowed_models_filter_advances_to_the_next_bound_route(provider_store):
    blocked = _add_route(
        provider_store,
        owner="alice",
        connection_id="pcn_blocked",
        model_route_id="pmr_blocked",
        model_id="blocked-model",
    )
    allowed = _add_route(
        provider_store,
        owner="alice",
        connection_id="pcn_allowed",
        model_route_id="pmr_allowed",
        model_id="allowed-model",
    )
    provider_store.put_route_binding(
        owner="alice",
        purpose="chat",
        model_route_ids=[blocked.id, allowed.id],
        expected_revision=0,
    )

    result = _default_chat(
        provider_store,
        _request(
            privileges={
                "allowed_models_restricted": True,
                "allowed_models": [allowed.id],
            },
        ),
    )

    assert result == {
        "endpoint_id": "pcn_allowed",
        "endpoint_url": MANAGED_ENGINE_PUBLIC_URL,
        "model": "allowed-model",
    }


def test_non_chat_routes_never_become_the_chat_default(provider_store):
    speech = _add_route(
        provider_store,
        owner="alice",
        connection_id="pcn_speech",
        model_route_id="pmr_speech",
        model_id="mimo-tts",
        operations=("speech.synthesize",),
    )
    chat = _add_route(
        provider_store,
        owner="alice",
        connection_id="pcn_chat",
        model_route_id="pmr_chat",
        model_id="mimo-chat",
    )
    provider_store.put_route_binding(
        owner="alice",
        purpose="chat",
        model_route_ids=[speech.id, chat.id],
        expected_revision=0,
    )

    assert _default_chat(provider_store, _request()) == {
        "endpoint_id": "pcn_chat",
        "endpoint_url": MANAGED_ENGINE_PUBLIC_URL,
        "model": "mimo-chat",
    }

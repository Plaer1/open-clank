"""Active chat and picker routes use only normalized provider authority."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import routes.chat_routes as chat_routes
from routes.provider_v1_routes import setup_provider_v1_routes
import routes.session_routes as session_routes
import src.openclank.chat_routing as normalized
import core.database as core_database
import core.session_manager as session_manager_module
from core.models import ChatMessage
from core.provider_models import ProviderBase, ProviderShareGrant
from core.session_manager import SessionManager
from routes.chat_helpers import add_user_message
from src.openclank.acp_bridge import _message_root_operation_id, _turn_source_id
from src.openclank.provider_store import ProviderStore


@pytest.fixture()
def provider_topology(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    store = ProviderStore(factory)

    own_connection = store.create_connection(
        owner="alice",
        connection_id="pcn_alice_api",
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label="Alice API",
        normalized_url="https://api.example.test/v1?never-project=this",
        settings={"safe": True},
    )
    own_route = store.create_model_route(
        owner="alice",
        connection_id=own_connection.id,
        model_route_id="pmr_alice_chat",
        provider_model_id="gpt-test",
        display_name="GPT Test",
        operations=("chat.stream", "chat.complete"),
        capabilities={"tools": True},
    )

    source_connection = store.create_connection(
        owner="carol",
        connection_id="pcn_carol_subscription",
        family_id="openai",
        adapter_id="openai-responses",
        kind="subscription",
        billing_lane="subscription",
        label="Private source label",
        normalized_url="https://private-source.example.test/v1",
    )
    shared_route = store.create_model_route(
        owner="carol",
        connection_id=source_connection.id,
        model_route_id="pmr_carol_chat",
        provider_model_id="shared-gpt",
        display_name="Shared GPT",
        operations=("chat.stream", "chat.complete"),
        capabilities={"tools": False},
    )
    grant = store.create_share_grant(
        owner="carol",
        recipient="alice",
        connection_id=source_connection.id,
        label="Team subscription",
        account_selector={"mode": "all_live_accounts"},
        model_selector={
            "mode": "explicit_models",
            "model_route_ids": [shared_route.id],
        },
        disclosure_fields=[],
        grant_id="psg_team",
    )
    # Simulate an active pre-simplification row that never received explicit
    # recipient acceptance.  Active legacy grants must project immediately.
    with factory() as db:
        db.query(ProviderShareGrant).filter(
            ProviderShareGrant.id == grant.id,
        ).update({ProviderShareGrant.accepted_revision: None})
        db.commit()
    grant = store.get_share_grant(owner="carol", grant_id=grant.id)
    assert grant.accepted_revision is None

    monkeypatch.setattr(normalized, "ProviderStore", lambda: store)
    monkeypatch.setattr(normalized, "SessionLocal", factory)
    yield SimpleNamespace(
        store=store,
        own_connection=own_connection,
        own_route=own_route,
        shared_connection=source_connection,
        shared_route=shared_route,
        grant_id=grant.id,
    )
    engine.dispose()


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


def _request(owner: str = "alice"):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=owner, api_token=False),
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_manager=SimpleNamespace(
                    is_configured=True,
                    get_privileges=lambda _owner: {},
                    is_admin=lambda _owner: False,
                ),
                mimo_supervisor=None,
            ),
        ),
    )


def test_exact_own_and_shared_routes_resolve_to_connection_qualified_runtime_ids(
    provider_topology,
):
    own = normalized.resolve_chat_route(
        owner="alice",
        endpoint_id=provider_topology.own_connection.id,
        model_id=provider_topology.own_route.provider_model_id,
    )
    assert own.model_route_id == provider_topology.own_route.id
    assert own.runtime_model == "pcn_alice_api/gpt-test"
    assert own.provider_grant_id is None

    shared = normalized.resolve_chat_route(
        owner="alice",
        endpoint_id=f"share:{provider_topology.grant_id}",
        model_id=provider_topology.shared_route.provider_model_id,
    )
    assert shared.runtime_model == "pcn_carol_subscription/shared-gpt"
    assert shared.provider_grant_id == provider_topology.grant_id
    assert shared.disclosed_owner == "carol"

    # Pre-app migration removes legacy endpoint selectors. A canonical shared
    # session can still recover the one active grant from its stable route.
    migrated = normalized.resolve_chat_route(
        owner="alice",
        model_route_id=provider_topology.shared_route.id,
        model_id=provider_topology.shared_route.provider_model_id,
    )
    assert migrated.provider_grant_id == provider_topology.grant_id


def test_api_models_is_secret_free_normalized_projection(provider_topology):
    router = setup_provider_v1_routes(provider_topology.store, object())
    result = _endpoint(router, "/api/models")(_request())

    by_endpoint = {item["endpoint_id"]: item for item in result["items"]}
    assert set(by_endpoint) == {
        "pcn_alice_api",
        f"share:{provider_topology.grant_id}",
    }
    own = by_endpoint["pcn_alice_api"]
    assert own["url"] == normalized.MANAGED_ENGINE_PUBLIC_URL
    assert own["models"] == ["gpt-test"]
    assert own["catalog"][0]["provider_model_route_id"] == "pmr_alice_chat"
    shared = by_endpoint[f"share:{provider_topology.grant_id}"]
    assert shared["shared"] is True
    assert shared["endpoint_name"] == "Team subscription · shared by carol"
    assert shared["endpoint_kind"] == "shared"
    assert shared["shared_by"] == "carol"
    assert "billing_lane" not in shared
    assert "family_id" not in shared
    assert "provider_model_route_id" not in shared["catalog"][0]

    wire = json.dumps(result, sort_keys=True)
    assert "private-source.example.test" not in wire
    assert "Private source label" not in wire
    assert provider_topology.shared_connection.id not in wire
    assert provider_topology.shared_route.id not in wire
    assert "credential" not in wire.casefold()
    assert "mimo:" not in wire
    assert "ody-" not in wire

    restricted = _request()
    restricted.app.state.auth_manager.get_privileges = lambda _owner: {
        "allowed_models_restricted": True,
        "allowed_models": [provider_topology.shared_route.id],
    }
    restricted_result = _endpoint(router, "/api/models")(restricted)
    assert [item["endpoint_id"] for item in restricted_result["items"]] == [
        f"share:{provider_topology.grant_id}"
    ]
    assert provider_topology.shared_route.id not in json.dumps(restricted_result)


def test_revoked_share_leaves_catalog_and_cannot_resolve(provider_topology):
    grant = provider_topology.store.get_share_grant(
        owner="carol",
        grant_id=provider_topology.grant_id,
    )
    provider_topology.store.revoke_share_grant(
        owner="carol",
        grant_id=grant.id,
        expected_revision=grant.revision,
    )

    own, shared = normalized.list_chat_routes(
        "alice",
        provider_store=provider_topology.store,
    )
    assert [route.model_route_id for route in own] == [provider_topology.own_route.id]
    assert shared == []
    with pytest.raises(normalized.ChatRouteUnavailable):
        normalized.resolve_chat_route(
            owner="alice",
            endpoint_id=f"share:{provider_topology.grant_id}",
            model_id=provider_topology.shared_route.provider_model_id,
            provider_store=provider_topology.store,
        )


def test_default_chat_uses_normalized_route_binding(provider_topology):
    provider_topology.store.put_route_binding(
        owner="alice",
        purpose="chat",
        model_route_ids=[provider_topology.own_route.id],
        expected_revision=0,
    )
    router = setup_provider_v1_routes(provider_topology.store, object())

    result = _endpoint(router, "/api/default-chat")(_request())

    assert result == {
        "endpoint_id": provider_topology.own_connection.id,
        "endpoint_url": normalized.MANAGED_ENGINE_PUBLIC_URL,
        "model": provider_topology.own_route.provider_model_id,
    }


def test_session_target_and_reconciliation_preserve_route_and_grant(
    monkeypatch,
    provider_topology,
):
    sess = SimpleNamespace(
        id="session-1",
        owner="alice",
        model="old-model",
        endpoint_url="https://legacy.invalid/v1",
        endpoint_id="legacy-endpoint",
        provider_model_route_id=None,
        headers={"Authorization": "must-be-erased"},
    )
    persisted = SimpleNamespace(
        model=sess.model,
        endpoint_url=sess.endpoint_url,
        endpoint_id=sess.endpoint_id,
        provider_model_route_id=None,
        headers=dict(sess.headers),
        updated_at=None,
    )

    class Query:
        def filter(self, *_args):
            return self

        def first(self):
            return persisted

    class Db:
        def query(self, _model):
            return Query()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(chat_routes, "SessionLocal", Db)
    changed = chat_routes._reconcile_selected_route_from_request(
        _request(),
        sess,
        sess.id,
        {
            "selected_model": "shared-gpt",
            "selected_endpoint_id": f"share:{provider_topology.grant_id}",
            "selected_endpoint_url": normalized.MANAGED_ENGINE_PUBLIC_URL,
        },
        owner="alice",
    )
    assert changed is True
    assert persisted.provider_model_route_id == provider_topology.shared_route.id
    assert persisted.headers == {}
    assert sess.provider_grant_id == provider_topology.grant_id

    target = chat_routes._resolved_session_target(sess)
    assert target.transport == "acp"
    assert target.model_id == "pcn_carol_subscription/shared-gpt"
    assert target.endpoint_id == target.provider_id == "pcn_carol_subscription"
    assert target.headers == {}


def test_root_operation_identity_is_persisted_and_forwarded():
    captured = []
    sess = SimpleNamespace(add_message=captured.append)
    handler = SimpleNamespace(update_session_name_if_needed=lambda *_args: None)
    preprocessed = SimpleNamespace(
        attachment_meta=[],
        user_content="hello",
        text_for_context="hello",
    )
    root_id = "root_0123456789abcdef0123456789abcdef"

    add_user_message(
        sess,
        handler,
        preprocessed,
        root_operation_id=root_id,
    )
    messages = [{
        "role": captured[0].role,
        "content": captured[0].content,
        "metadata": captured[0].metadata,
    }]
    envelope = chat_routes._turn_envelope(
        session_id="session-1",
        owner="alice",
        workspace="/tmp/workspace",
        authority_workspace_id="workspace-a",
        model="gpt-test",
        mode="agent",
        incognito=False,
        root_operation_id=root_id,
        provider_grant_id="psg_team",
    )

    assert captured[0].metadata["root_operation_id"] == root_id
    assert captured[0].persistence_id == root_id
    assert _message_root_operation_id(messages) == root_id
    assert _turn_source_id(messages) == root_id
    assert envelope["root_operation_id"] == root_id
    assert envelope["provider_grant_id"] == "psg_team"
    assert envelope["workspace"] == "/tmp/workspace"
    assert envelope["authority_workspace_id"] == "workspace-a"


def test_root_operation_identity_is_the_durable_user_message_id(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_database.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session_id = "session-root-id"
    root_id = "root_abcdef0123456789abcdef0123456789"
    with factory() as db:
        db.add(core_database.Session(
            id=session_id,
            name="Root identity",
            endpoint_url=normalized.MANAGED_ENGINE_PUBLIC_URL,
            model="gpt-test",
            owner="alice",
        ))
        db.commit()

    manager = object.__new__(SessionManager)
    manager.sessions = {}
    manager.upload_handler = None
    monkeypatch.setattr(session_manager_module, "SessionLocal", factory)
    message = ChatMessage(
        "user",
        "hello",
        metadata={"root_operation_id": root_id},
        persistence_id=root_id,
    )

    assert manager._persist_message(session_id, message) is True
    with factory() as db:
        row = db.get(core_database.ChatMessage, root_id)
        assert row is not None
        assert json.loads(row.meta_data)["root_operation_id"] == root_id
    assert message.metadata["_db_id"] == root_id

    monkeypatch.setattr("src.agent_actor_accounting.SessionLocal", factory)
    from src.agent_actor_accounting import begin_agent_turn

    assert begin_agent_turn(root_id, "engine-session") is True
    engine.dispose()


@pytest.mark.asyncio
async def test_session_create_persists_stable_route_and_rejects_inline_key(
    monkeypatch,
    provider_topology,
):
    created = {}

    class Manager:
        sessions = {}

        def create_session(self, **kwargs):
            created.update(kwargs)
            return SimpleNamespace(
                id=kwargs["session_id"],
                name=kwargs["name"],
                model=kwargs["model"],
                endpoint_url=kwargs["endpoint_url"],
                endpoint_id=kwargs["endpoint_id"],
                provider_model_route_id=kwargs["provider_model_route_id"],
                headers={},
            )

    monkeypatch.setattr(session_routes, "effective_user", lambda _request: "alice")
    monkeypatch.setattr("src.event_bus.fire_event", lambda *_args, **_kwargs: None)
    router = session_routes.setup_session_routes(Manager(), {})
    create = _endpoint(router, "/api/session", "POST")
    response = await create(
        _request(),
        name="Normalized",
        endpoint_url=normalized.MANAGED_ENGINE_PUBLIC_URL,
        model="gpt-test",
        rag=None,
        skip_validation=None,
        incognito=None,
        api_key="",
        endpoint_id="pcn_alice_api",
    )
    assert response.endpoint_url == normalized.MANAGED_ENGINE_PUBLIC_URL
    assert created["provider_model_route_id"] == provider_topology.own_route.id
    assert created["endpoint_id"] == provider_topology.own_connection.id

    with pytest.raises(HTTPException) as exc:
        await create(
            _request(),
            name="Bad key",
            endpoint_url=normalized.MANAGED_ENGINE_PUBLIC_URL,
            model="gpt-test",
            rag=None,
            skip_validation=None,
            incognito=None,
            api_key="do-not-accept",
            endpoint_id="pcn_alice_api",
        )
    assert exc.value.status_code == 400
    assert "Providers interface" in str(exc.value.detail)

    with pytest.raises(HTTPException) as legacy_exc:
        await create(
            _request(),
            name="Retired endpoint",
            endpoint_url="https://legacy.invalid/v1",
            model="gpt-test",
            rag=None,
            skip_validation=None,
            incognito=None,
            api_key="",
            endpoint_id="ody-retired",
        )
    assert legacy_exc.value.status_code == 400
    assert "provider" in str(legacy_exc.value.detail).casefold()


@pytest.mark.asyncio
async def test_session_patch_replaces_route_atomically_with_empty_headers(
    monkeypatch,
    provider_topology,
):
    active = SimpleNamespace(
        id="session-2",
        owner="alice",
        name="Existing",
        model="old",
        endpoint_url="old",
        endpoint_id="old",
        provider_model_route_id="old-route",
        headers={"Authorization": "old"},
    )
    persisted = SimpleNamespace(
        model="old",
        endpoint_url="old",
        endpoint_id="old",
        provider_model_route_id="old-route",
        headers={"Authorization": "old"},
        updated_at=None,
        folder=None,
    )

    class Manager:
        def get_session(self, _sid):
            return active

    class Query:
        def filter(self, *_args):
            return self

        def first(self):
            return persisted

    class Db:
        def query(self, _model):
            return Query()

        def commit(self):
            pass

        def close(self):
            pass

    async def no_prepare(*_args, **_kwargs):
        return None

    monkeypatch.setattr(session_routes, "effective_user", lambda _request: "alice")
    monkeypatch.setattr(session_routes, "_verify_session_owner", lambda *_args: None)
    monkeypatch.setattr(session_routes, "_prepare_context_mutation", no_prepare)
    monkeypatch.setattr(session_routes, "SessionLocal", Db)
    router = session_routes.setup_session_routes(Manager(), {})
    patch = _endpoint(router, "/api/session/{sid}", "PATCH")
    result = await patch(
        _request(),
        "session-2",
        name=None,
        folder=None,
        model="gpt-test",
        endpoint_url=normalized.MANAGED_ENGINE_PUBLIC_URL,
        endpoint_id="pcn_alice_api",
    )
    assert result["endpoint_id"] == "pcn_alice_api"
    assert persisted.provider_model_route_id == "pmr_alice_chat"
    assert persisted.headers == {}
    assert active.provider_model_route_id == "pmr_alice_chat"
    assert active.headers == {}

"""Focused S06A host authority regressions."""

from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as core_database
from core.provider_models import ProviderBase, ProviderModelRoute
from src import secret_storage
from src.openclank.mimo_projection import build_projection_snapshot
from src.openclank.provider_store import (
    ProviderNotFound,
    ProviderRevisionConflict,
    ProviderStore,
)
from routes.provider_v1_routes import _validated_account_discovery


@pytest.fixture()
def host_store(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_storage, "_KEY_PATH", Path(tmp_path) / ".app-key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(core_database, "SessionLocal", factory)
    store = ProviderStore(factory, clock=lambda: __import__("datetime").datetime(2026, 9, 22))
    yield store, factory
    engine.dispose()


def _connection(store, *, connection_id="pcn_authority", kind="official"):
    return store.create_connection(
        owner="alice",
        connection_id=connection_id,
        family_id="openai",
        adapter_id="openai-responses",
        kind=kind,
        billing_lane="metered_api",
        label=connection_id,
    )


def _route(route_id="pmr_authority", model_id="gpt-authority"):
    return {
        "id": route_id,
        "provider_model_id": model_id,
        "display_name": model_id,
        "operations": ["chat.stream", "chat.complete"],
        "capabilities": {},
        "provenance": {"authority": "managed-engine"},
    }


def _persist(store, *, account_id, connection_id, revision=1, expected_account_revision=None, routes=()):
    return store.persist_account_discovery(
        owner="alice",
        account_id=account_id,
        connection_id=connection_id,
        label=account_id,
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api_key", "key": f"secret-{account_id}-{revision}"},
        safe_identity={},
        model_routes=list(routes),
        status="complete",
        authoritative=True,
        expected_account_revision=expected_account_revision,
        expected_credential_revision=revision,
    )


def test_atomic_oauth_style_persistence_rolls_back_and_retry_is_idempotent(
    host_store, monkeypatch
):
    store, _factory = host_store
    connection = _connection(store)

    original_detach = store._detach

    def fail_after_flush(db, row):
        raise RuntimeError("injected persistence failure")

    monkeypatch.setattr(store, "_detach", fail_after_flush)
    with pytest.raises(RuntimeError):
        _persist(store, account_id="pac_atomic", connection_id=connection.id, routes=[_route()])

    assert store.list_accounts(owner="alice", connection_id=connection.id) == []

    monkeypatch.setattr(store, "_detach", original_detach)
    row = _persist(store, account_id="pac_atomic", connection_id=connection.id, routes=[_route()])
    assert row.id == "pac_atomic"
    assert row.credential_version == 1
    retried_add = _persist(
        store, account_id="pac_atomic", connection_id=connection.id, routes=[_route()]
    )
    assert retried_add.id == row.id
    assert retried_add.credential_version == row.credential_version

    monkeypatch.setattr(store, "_detach", fail_after_flush)
    with pytest.raises(RuntimeError):
        _persist(
            store,
            account_id="pac_atomic",
            connection_id=connection.id,
            revision=2,
            expected_account_revision=row.revision,
            routes=[_route()],
        )
    monkeypatch.setattr(store, "_detach", original_detach)
    unchanged = store.get_account(owner="alice", account_id="pac_atomic")
    assert unchanged.credential_version == 1
    assert unchanged.revision == row.revision

    monkeypatch.setattr(store, "_detach", original_detach)
    retried = _persist(
        store,
        account_id="pac_atomic",
        connection_id=connection.id,
        revision=2,
        expected_account_revision=row.revision,
        routes=[_route()],
    )
    assert retried.credential_version == 2


def test_reconciliation_is_account_scoped_and_authoritative_empty_preserves_sibling(
    host_store,
):
    store, _factory = host_store
    connection = _connection(store)
    first = _persist(
        store, account_id="pac_first", connection_id=connection.id, routes=[_route()]
    )
    second = _persist(
        store, account_id="pac_second", connection_id=connection.id, routes=[_route()]
    )

    store.reconcile_account_discovery(
        owner="alice",
        account_id=first.id,
        model_routes=[],
        status="complete",
        authoritative=True,
        expected_credential_revision=first.credential_version,
    )
    route = store.list_model_routes(owner="alice", connection_id=connection.id)[0]
    assert route.deleted_at is None
    assert store.route_has_permitted_account(owner="alice", model_route_id=route.id)

    store.reconcile_account_discovery(
        owner="alice",
        account_id=first.id,
        model_routes=[],
        status="partial",
        authoritative=False,
        expected_credential_revision=first.credential_version,
    )
    assert store.route_has_permitted_account(owner="alice", model_route_id=route.id)
    assert second.id != first.id

    store.reconcile_account_discovery(
        owner="alice",
        account_id=second.id,
        model_routes=[],
        status="complete",
        authoritative=True,
        expected_credential_revision=second.credential_version,
    )
    removed = store.list_model_routes(
        owner="alice", connection_id=connection.id, include_deleted=True
    )[0]
    assert removed.deleted_at is not None
    with pytest.raises(ProviderNotFound):
        store.route_has_permitted_account(owner="alice", model_route_id=removed.id)


def test_managed_unknown_is_unavailable_keyless_is_explicit_and_hidden_is_not_projected(
    host_store,
):
    store, factory = host_store
    managed = _connection(store, connection_id="pcn_managed", kind="official")
    managed_route = store.create_model_route(
        owner="alice",
        connection_id=managed.id,
        model_route_id="pmr_managed",
        provider_model_id="managed-model",
        display_name="Managed",
        operations=("chat.stream", "chat.complete"),
    )
    account = store.create_account(
        owner="alice",
        connection_id=managed.id,
        account_id="pac_managed",
        label="Managed",
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api_key", "key": "managed-secret"},
    )
    assert not store.route_has_permitted_account(
        owner="alice", model_route_id=managed_route.id
    )
    assert not store.permitted_model_route_ids(
        owner="alice", connection_id=managed.id
    )

    local = _connection(store, connection_id="pcn_local", kind="local")
    store.create_model_route(
        owner="alice",
        connection_id=local.id,
        model_route_id="pmr_visible",
        provider_model_id="visible-local",
        display_name="Visible",
        operations=("chat.stream", "chat.complete"),
    )
    hidden = store.create_model_route(
        owner="alice",
        connection_id=local.id,
        model_route_id="pmr_hidden",
        provider_model_id="hidden-local",
        display_name="Hidden",
        operations=("chat.stream", "chat.complete"),
    )
    with factory() as db:
        db.get(ProviderModelRoute, hidden.id).visibility = "hidden"
        db.commit()

    snapshot = build_projection_snapshot("alice")
    assert "pcn_managed" not in snapshot.providers
    assert set(snapshot.providers["pcn_local"]["models"]) == {"visible-local"}
    assert account.id == "pac_managed"


def test_reconciliation_rejects_stale_credential_revision(host_store):
    store, _factory = host_store
    connection = _connection(store)
    account = _persist(
        store, account_id="pac_revision", connection_id=connection.id, routes=[_route()]
    )
    with pytest.raises(ProviderRevisionConflict):
        store.reconcile_account_discovery(
            owner="alice",
            account_id=account.id,
            model_routes=[_route()],
            status="complete",
            authoritative=True,
            expected_credential_revision=account.credential_version - 1,
        )


def test_discovery_requires_exact_v2_account_and_revision_echoes():
    with pytest.raises(HTTPException):
        _validated_account_discovery(
            {"modelRoutes": []}, account_id="pac_strict", credential_revision=3
        )
    with pytest.raises(HTTPException):
        _validated_account_discovery(
            {
                "accountID": "pac_other",
                "credentialRevision": 3,
                "discovery": {
                    "status": "complete",
                    "accountID": "pac_other",
                    "credentialRevision": 3,
                    "authoritative": True,
                },
            },
            account_id="pac_strict",
            credential_revision=3,
        )


def test_disabled_or_deleted_last_account_denies_dispatch(host_store):
    store, _factory = host_store
    connection = _connection(store)
    account = _persist(
        store, account_id="pac_last", connection_id=connection.id, routes=[_route()]
    )
    route = store.list_model_routes(owner="alice", connection_id=connection.id)[0]
    assert store.route_has_permitted_account(owner="alice", model_route_id=route.id)

    disabled = store.update_account(
        owner="alice", account_id=account.id, expected_revision=account.revision, enabled=False
    )
    assert not store.route_has_permitted_account(owner="alice", model_route_id=route.id)

    deleted = store.delete_account(
        owner="alice", account_id=account.id, expected_revision=disabled.revision
    )
    assert deleted.deleted_at is not None
    assert not store.route_has_permitted_account(owner="alice", model_route_id=route.id)

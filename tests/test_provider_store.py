"""Provider core: encrypted CRUD, rotation, leases, and granular sharing."""

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import threading
import os
from time import perf_counter

import pytest
import jsonschema
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.provider_models import (
    PROVIDER_TABLES,
    ProviderAccount,
    ProviderAccountHealth,
    ProviderAccountModelHealth,
    ProviderBase,
    ProviderCredentialLease,
    ProviderIdempotencyRecord,
    ProviderLegacyAlias,
    ProviderOperationBinding,
    ProviderRefreshLease,
    ProviderRouteBinding,
    ProviderRotationCursor,
    ProviderShareGrant,
)
from src import secret_storage
from src.openclank.provider_store import (
    AccountSelector,
    BillingLaneMismatch,
    ModelSelector,
    NoEligibleAccount,
    ProviderConflict,
    ProviderNotFound,
    ProviderRevisionConflict,
    ProviderStore,
    ProviderValidationError,
    RefreshLeaseBusy,
    RefreshLeaseInvalid,
    ShareDenied,
)


NOW = datetime(2026, 8, 9, 12, 0, 0)


@pytest.fixture()
def provider_store(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(bind=engine)
    factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
    )
    store = ProviderStore(factory, clock=lambda: NOW)
    yield store, factory
    engine.dispose()


def _connection(
    store: ProviderStore,
    *,
    connection_id: str = "connection-api",
    billing_lane: str = "metered_api",
):
    return store.create_connection(
        owner="alice",
        connection_id=connection_id,
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane=billing_lane,
        label=connection_id,
        normalized_url="https://api.example.test/v1",
    )


def _account(
    store: ProviderStore,
    *,
    connection_id: str,
    account_id: str,
    sort_order: int,
    key: str,
):
    return store.create_account(
        owner="alice",
        connection_id=connection_id,
        account_id=account_id,
        label=account_id,
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api_key", "key": key},
        sort_order=sort_order,
        safe_identity={"provider_display_identity": f"{account_id}@example.test"},
    )


def _route(
    store: ProviderStore,
    *,
    connection_id: str,
    route_id: str = "route-chat",
    model_id: str = "gpt-test",
):
    return store.create_model_route(
        owner="alice",
        connection_id=connection_id,
        model_route_id=route_id,
        provider_model_id=model_id,
        display_name=model_id,
        operations=("chat.stream", "chat.complete"),
    )


def _entitle(
    store: ProviderStore,
    *,
    account_id: str,
    route_id: str,
    eligible: bool = True,
):
    return store.set_entitlement(
        owner="alice",
        account_id=account_id,
        model_route_id=route_id,
        eligible=eligible,
        capability_fingerprint="chat-v1",
    )


def test_account_crud_encrypts_with_scope_and_never_returns_plaintext_at_rest(
    provider_store,
):
    store, factory = provider_store
    connection = _connection(store)
    original_secret = "first-secret-value"
    account = _account(
        store,
        connection_id=connection.id,
        account_id="account-a",
        sort_order=10,
        key=original_secret,
    )

    with factory() as db:
        raw = db.query(ProviderAccount).filter(ProviderAccount.id == account.id).one()
        assert raw.credential_envelope.startswith("{")
        assert original_secret not in raw.credential_envelope
        assert raw.credential_fingerprint != original_secret

    access = store.credential_access(owner="alice", account_id=account.id)
    assert access.credentials == {"type": "api_key", "key": original_secret}
    assert original_secret not in repr(access)

    with pytest.raises(ProviderConflict, match="already registered"):
        _account(
            store,
            connection_id=connection.id,
            account_id="account-duplicate",
            sort_order=20,
            key=original_secret,
        )

    updated = store.update_account(
        owner="alice",
        account_id=account.id,
        expected_revision=account.revision,
        label="Primary",
        sort_order=1,
    )
    assert updated.label == "Primary"
    assert updated.revision == account.revision + 1
    with pytest.raises(ProviderRevisionConflict):
        store.update_account(
            owner="alice",
            account_id=account.id,
            expected_revision=account.revision,
            label="Stale update",
        )

    replaced = store.replace_account_credential(
        owner="alice",
        account_id=account.id,
        expected_credential_version=1,
        credentials={"type": "api_key", "key": "rotated-secret-value"},
    )
    assert replaced.credential_version == 2
    assert store.credential_access(
        owner="alice",
        account_id=account.id,
    ).credentials["key"] == "rotated-secret-value"

    tombstone = store.delete_account(
        owner="alice",
        account_id=account.id,
        expected_revision=replaced.revision,
    )
    assert tombstone.deleted_at == NOW
    assert tombstone.credential_envelope is None
    with pytest.raises(ProviderNotFound):
        store.credential_access(owner="alice", account_id=account.id)
    deleted_rows = store.list_accounts(
        owner="alice",
        connection_id=connection.id,
        include_deleted=True,
    )
    assert [row.id for row in deleted_rows] == [account.id]


def test_owner_rename_reseals_credentials_and_migrates_every_identity_reference(
    provider_store,
):
    store, factory = provider_store
    connection = _connection(store)
    account = _account(
        store,
        connection_id=connection.id,
        account_id="account-a",
        sort_order=0,
        key="rename-secret",
    )
    route = _route(store, connection_id=connection.id)
    _entitle(store, account_id=account.id, route_id=route.id)
    store.put_route_binding(
        owner="alice",
        purpose="chat.default",
        model_route_ids=[route.id],
        expected_revision=0,
    )
    binding = store.bind_account(
        owner="alice",
        root_operation_id="rename-root",
        connection_id=connection.id,
        model_route_id=route.id,
    )
    store.lease_credential(
        owner="alice",
        holder_id="rename-worker",
        root_operation_id="rename-root",
        connection_id=connection.id,
        account_id=account.id,
        model_id=route.provider_model_id,
        expected_credential_revision=binding.credential_version,
        now=NOW,
    )
    store.acquire_refresh_lease(
        owner="alice",
        account_id=account.id,
        holder_id="refresh-worker",
        now=NOW,
    )
    outgoing = store.create_share_grant(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        account_selector=AccountSelector.all_live(),
        model_selector=ModelSelector.explicit([route.id]),
    )
    carol_connection = store.create_connection(
        owner="carol",
        connection_id="carol-connection",
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label="Carol",
    )
    with factory() as db:
        incoming = ProviderShareGrant(
            id="incoming-grant",
            owner="carol",
            recipient="alice",
            connection_id=carol_connection.id,
            billing_lane=carol_connection.billing_lane,
            label="Incoming",
            account_selector=AccountSelector.all_live().as_json(),
            model_selector=ModelSelector.all_live().as_json(),
            disclosure_fields=[],
            state="active",
            revision=1,
            accepted_revision=1,
        )
        db.add(incoming)
        db.add(
            ProviderLegacyAlias(
                id="legacy-alias",
                owner="alice",
                legacy_kind="connection",
                legacy_id="old-connection",
                connection_id=connection.id,
                provenance={},
            )
        )
        db.commit()
        before = db.get(ProviderAccount, account.id)
        old_envelope = before.credential_envelope
        old_fingerprint = before.credential_fingerprint

    digest = store.idempotency_request_digest(
        owner="alice",
        operation="connections.create",
        payload={"label": "Example"},
    )
    store.record_idempotency(
        owner="alice",
        operation="connections.create",
        idempotency_key="rename-idempotency-key",
        request_digest=digest,
        status_code=201,
        response_body={"id": connection.id},
        resource_id=connection.id,
    )

    counts = store.rename_owner("Alice", "ALICE2")

    assert counts["accounts"] == 1
    assert counts["credential_leases_invalidated"] == 1
    assert counts["refresh_leases_invalidated"] == 1
    assert counts["idempotency_records_invalidated"] == 1
    assert store.credential_access(
        owner="alice2",
        account_id=account.id,
    ).credentials == {"type": "api_key", "key": "rename-secret"}
    with pytest.raises(ProviderNotFound):
        store.credential_access(owner="alice", account_id=account.id)

    with factory() as db:
        renamed = db.get(ProviderAccount, account.id)
        assert renamed.owner == "alice2"
        assert renamed.credential_envelope != old_envelope
        assert renamed.credential_fingerprint != old_fingerprint
        persisted_binding = db.get(ProviderOperationBinding, binding.binding_id)
        assert (persisted_binding.owner, persisted_binding.credential_owner) == (
            "alice2",
            "alice2",
        )
        persisted_outgoing = db.get(ProviderShareGrant, outgoing.id)
        assert (persisted_outgoing.owner, persisted_outgoing.recipient) == (
            "alice2",
            "bob",
        )
        persisted_incoming = db.get(ProviderShareGrant, "incoming-grant")
        assert (persisted_incoming.owner, persisted_incoming.recipient) == (
            "carol",
            "alice2",
        )
        assert db.get(ProviderLegacyAlias, "legacy-alias").owner == "alice2"
        assert db.query(ProviderCredentialLease).count() == 0
        assert db.query(ProviderRefreshLease).count() == 0
        assert db.query(ProviderIdempotencyRecord).count() == 0
        assert db.query(ProviderRouteBinding).filter_by(owner="alice2").count() == 1
        assert db.query(ProviderRotationCursor).filter_by(owner="alice2").count() == 1


def test_owner_rename_fails_closed_before_mutation_when_destination_has_state(
    provider_store,
):
    store, factory = provider_store
    connection = _connection(store)
    account = _account(
        store,
        connection_id=connection.id,
        account_id="account-a",
        sort_order=0,
        key="must-survive-conflict",
    )
    store.create_connection(
        owner="bob",
        connection_id="bob-connection",
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label="Bob",
    )
    with factory() as db:
        envelope = db.get(ProviderAccount, account.id).credential_envelope

    with pytest.raises(ProviderConflict, match="target provider owner"):
        store.rename_owner("alice", "bob")

    with factory() as db:
        persisted = db.get(ProviderAccount, account.id)
        assert persisted.owner == "alice"
        assert persisted.credential_envelope == envelope
        assert db.query(ProviderCredentialLease).count() == 0
    assert store.credential_access(
        owner="alice",
        account_id=account.id,
    ).credentials["key"] == "must-survive-conflict"


def test_owner_purge_removes_owned_received_and_credential_source_state_only(
    provider_store,
):
    store, factory = provider_store
    alice_connection = _connection(store)
    alice_account = _account(
        store,
        connection_id=alice_connection.id,
        account_id="alice-account",
        sort_order=0,
        key="alice-secret",
    )
    alice_route = _route(store, connection_id=alice_connection.id)
    _entitle(store, account_id=alice_account.id, route_id=alice_route.id)
    store.create_share_grant(
        owner="alice",
        recipient="bob",
        connection_id=alice_connection.id,
        account_selector=AccountSelector.all_live(),
        model_selector=ModelSelector.explicit([alice_route.id]),
    )

    bob_connection = store.create_connection(
        owner="bob",
        connection_id="bob-connection",
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label="Bob",
    )
    bob_account = store.create_account(
        owner="bob",
        connection_id=bob_connection.id,
        account_id="bob-account",
        label="Bob account",
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api_key", "key": "bob-secret"},
    )
    bob_route = store.create_model_route(
        owner="bob",
        connection_id=bob_connection.id,
        model_route_id="bob-route",
        provider_model_id="bob-model",
        operations=("chat.complete",),
    )
    store.set_entitlement(
        owner="bob",
        account_id=bob_account.id,
        model_route_id=bob_route.id,
        eligible=True,
    )
    store.create_share_grant(
        owner="bob",
        recipient="alice",
        connection_id=bob_connection.id,
        account_selector=AccountSelector.all_live(),
        model_selector=ModelSelector.explicit([bob_route.id]),
    )

    counts = store.purge_owner("ALICE")

    assert counts["connections"] == 1
    assert counts["accounts"] == 1
    assert counts["share_grants"] == 2
    assert store.credential_access(
        owner="bob",
        account_id=bob_account.id,
    ).credentials["key"] == "bob-secret"
    with factory() as db:
        for model in PROVIDER_TABLES:
            for row in db.query(model).all():
                for attribute in ("owner", "credential_owner", "recipient"):
                    if hasattr(row, attribute):
                        assert getattr(row, attribute) != "alice"
        assert db.query(ProviderAccount).filter_by(owner="bob").count() == 1
        assert db.query(ProviderShareGrant).count() == 0


def test_account_without_explicit_order_appends_after_connection_pool(provider_store):
    store, _ = provider_store
    connection = _connection(store)
    first = _account(
        store,
        connection_id=connection.id,
        account_id="account-first",
        sort_order=7,
        key="key-first",
    )
    second = store.create_account(
        owner="alice",
        connection_id=connection.id,
        account_id="account-second",
        label="Second",
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api_key", "key": "key-second"},
    )
    third = store.create_account(
        owner="alice",
        connection_id=connection.id,
        account_id="account-third",
        label="Third",
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api_key", "key": "key-third"},
    )

    assert (first.sort_order, second.sort_order, third.sort_order) == (7, 8, 9)
    assert [row.id for row in store.list_accounts(
        owner="alice",
        connection_id=connection.id,
    )] == [first.id, second.id, third.id]


def test_equal_round_robin_is_durable_and_root_operations_are_sticky(provider_store):
    store, _ = provider_store
    connection = _connection(store)
    route = _route(store, connection_id=connection.id)
    first = _account(
        store,
        connection_id=connection.id,
        account_id="account-a",
        sort_order=10,
        key="key-a",
    )
    second = _account(
        store,
        connection_id=connection.id,
        account_id="account-b",
        sort_order=20,
        key="key-b",
    )
    _entitle(store, account_id=first.id, route_id=route.id)
    _entitle(store, account_id=second.id, route_id=route.id)

    selection_1 = store.bind_account(
        owner="alice",
        root_operation_id="root-1",
        connection_id=connection.id,
        model_route_id=route.id,
    )
    same_root = store.bind_account(
        owner="alice",
        root_operation_id="root-1",
        connection_id=connection.id,
        model_route_id=route.id,
        preferred_account_id=second.id,
    )
    selection_2 = store.bind_account(
        owner="alice",
        root_operation_id="root-2",
        connection_id=connection.id,
        model_route_id=route.id,
    )
    selection_3 = store.bind_account(
        owner="alice",
        root_operation_id="root-3",
        connection_id=connection.id,
        model_route_id=route.id,
    )

    assert [selection_1.account_id, selection_2.account_id, selection_3.account_id] == [
        first.id,
        second.id,
        first.id,
    ]
    assert same_root.account_id == first.id
    assert same_root.sticky is True
    committed = store.mark_binding_committed(
        owner="alice",
        root_operation_id="root-1",
        connection_id=connection.id,
        billing_lane=connection.billing_lane,
    )
    assert committed.state == "committed"
    assert committed.committed_at == NOW


def test_sticky_binding_rejects_provider_or_model_drift(provider_store):
    store, _ = provider_store
    connection = _connection(store)
    account = _account(
        store,
        connection_id=connection.id,
        account_id="account-a",
        sort_order=10,
        key="key-a",
    )
    route_a = _route(
        store,
        connection_id=connection.id,
        route_id="route-a",
        model_id="model-a",
    )
    route_b = _route(
        store,
        connection_id=connection.id,
        route_id="route-b",
        model_id="model-b",
    )
    _entitle(store, account_id=account.id, route_id=route_a.id)
    _entitle(store, account_id=account.id, route_id=route_b.id)

    original = store.bind_account(
        owner="alice",
        root_operation_id="fixed-model-root",
        connection_id=connection.id,
        provider_id="openai",
        model_route_id=route_a.id,
    )
    assert original.model_id == "model-a"

    with pytest.raises(ProviderConflict, match="different provider model"):
        store.bind_account(
            owner="alice",
            root_operation_id="fixed-model-root",
            connection_id=connection.id,
            provider_id="openai",
            model_route_id=route_b.id,
        )
    with pytest.raises(ProviderConflict, match="different provider model"):
        store.bind_account(
            owner="alice",
            root_operation_id="fixed-model-root",
            connection_id=connection.id,
            provider_id="not-openai",
            model_route_id=route_a.id,
        )


def test_credential_lease_rejects_a_different_entitled_model(provider_store):
    store, factory = provider_store
    connection = _connection(store)
    account = _account(
        store,
        connection_id=connection.id,
        account_id="account-a",
        sort_order=10,
        key="key-a",
    )
    route_a = _route(
        store,
        connection_id=connection.id,
        route_id="route-a",
        model_id="model-a",
    )
    route_b = _route(
        store,
        connection_id=connection.id,
        route_id="route-b",
        model_id="model-b",
    )
    _entitle(store, account_id=account.id, route_id=route_a.id)
    _entitle(store, account_id=account.id, route_id=route_b.id)
    binding = store.bind_account(
        owner="alice",
        root_operation_id="fixed-lease-root",
        connection_id=connection.id,
        provider_id="openai",
        model_route_id=route_a.id,
    )

    with pytest.raises(ShareDenied, match="operation binding"):
        store.lease_credential(
            owner="alice",
            holder_id="worker",
            root_operation_id="fixed-lease-root",
            connection_id=connection.id,
            account_id=account.id,
            model_id=route_b.provider_model_id,
            expected_credential_revision=binding.credential_version,
        )
    with factory() as db:
        persisted = db.get(ProviderOperationBinding, binding.binding_id)
        persisted.provider_id = "wrong-provider"
        db.commit()
    with pytest.raises(ShareDenied, match="operation binding"):
        store.lease_credential(
            owner="alice",
            holder_id="worker",
            root_operation_id="fixed-lease-root",
            connection_id=connection.id,
            account_id=account.id,
            model_id=route_a.provider_model_id,
            expected_credential_revision=binding.credential_version,
        )
    with factory() as db:
        persisted = db.get(ProviderOperationBinding, binding.binding_id)
        persisted.provider_id = connection.family_id
        persisted.billing_lane = "wrong-lane"
        db.commit()
    with pytest.raises(ShareDenied, match="operation binding"):
        store.lease_credential(
            owner="alice",
            holder_id="worker",
            root_operation_id="fixed-lease-root",
            connection_id=connection.id,
            account_id=account.id,
            model_id=route_a.provider_model_id,
            expected_credential_revision=binding.credential_version,
        )
    with factory() as db:
        assert db.query(ProviderCredentialLease).count() == 0


def test_preferred_pin_falls_back_only_to_eligible_same_pool_account(provider_store):
    store, _ = provider_store
    connection = _connection(store)
    route = _route(store, connection_id=connection.id)
    first = _account(
        store,
        connection_id=connection.id,
        account_id="account-a",
        sort_order=10,
        key="key-a",
    )
    second = _account(
        store,
        connection_id=connection.id,
        account_id="account-b",
        sort_order=20,
        key="key-b",
    )
    _entitle(store, account_id=first.id, route_id=route.id)
    _entitle(store, account_id=second.id, route_id=route.id)
    store.set_account_health(
        owner="alice",
        account_id=first.id,
        state="cooldown",
        cooldown_until=NOW + timedelta(minutes=5),
    )

    fallback = store.bind_account(
        owner="alice",
        root_operation_id="preferred-fallback",
        connection_id=connection.id,
        model_route_id=route.id,
        preferred_account_id=first.id,
    )
    assert fallback.account_id == second.id
    assert fallback.used_preference is False
    assert fallback.fallback_reason == "preferred_account_unavailable"

    store.set_account_model_health(
        owner="alice",
        account_id=second.id,
        model_route_id=route.id,
        state="ineligible",
    )
    with pytest.raises(NoEligibleAccount):
        store.bind_account(
            owner="alice",
            root_operation_id="none-left",
            connection_id=connection.id,
            model_route_id=route.id,
        )

    eligible_after_cooldown = store.eligible_accounts(
        owner="alice",
        connection_id=connection.id,
        model_route_id=route.id,
        now=NOW + timedelta(minutes=6),
    )
    assert [row.id for row in eligible_after_cooldown] == [first.id]


def test_precommit_failover_is_conservative_and_each_account_is_tried_once(
    provider_store,
):
    store, factory = provider_store
    connection = _connection(store)
    route = _route(store, connection_id=connection.id)
    accounts = [
        _account(
            store,
            connection_id=connection.id,
            account_id=f"account-{suffix}",
            sort_order=index * 10,
            key=f"key-{suffix}",
        )
        for index, suffix in enumerate(("a", "b", "c"))
    ]
    for account in accounts:
        _entitle(store, account_id=account.id, route_id=route.id)

    first = store.bind_account(
        owner="alice",
        root_operation_id="root-failover",
        connection_id=connection.id,
        model_route_id=route.id,
    )
    assert first.account_id == accounts[0].id

    quota = store.record_account_attempt(
        owner="alice",
        binding_id=first.binding_id,
        expected_revision=first.binding_revision,
        account_id=first.account_id,
        outcome="quota",
        retry_after_ms=120_000,
    )
    assert quota.account_id == accounts[1].id
    assert quota.source == "failover"
    assert quota.attempt == 2
    assert quota.fallback_reason == "quota_failover"
    assert store.bind_account(
        owner="alice",
        root_operation_id="root-failover",
        connection_id=connection.id,
        model_route_id=route.id,
    ).account_id == accounts[1].id

    entitlement = store.record_account_attempt(
        owner="alice",
        binding_id=quota.binding_id,
        expected_revision=quota.binding_revision,
        account_id=quota.account_id,
        outcome="entitlement",
        model_eligible=False,
    )
    assert entitlement.account_id == accounts[2].id
    assert entitlement.attempt == 3
    assert entitlement.fallback_reason == "entitlement_failover"

    with pytest.raises(NoEligibleAccount, match="every eligible"):
        store.record_account_attempt(
            owner="alice",
            binding_id=entitlement.binding_id,
            expected_revision=entitlement.binding_revision,
            account_id=entitlement.account_id,
            outcome="auth",
        )

    with factory() as db:
        binding = db.query(ProviderOperationBinding).filter_by(
            id=first.binding_id
        ).one()
        assert binding.selected_account_id == accounts[2].id
        assert binding.fallback_reason == "auth_all_accounts_exhausted"
        assert [item["account_id"] for item in binding.attempts] == [
            account.id for account in accounts
        ]
        assert db.query(ProviderAccountHealth).filter_by(
            account_id=accounts[0].id
        ).one().state == "cooldown"
        assert db.query(ProviderAccountModelHealth).filter_by(
            account_id=accounts[1].id,
            model_route_id=route.id,
        ).one().state == "ineligible"
        assert db.query(ProviderAccountHealth).filter_by(
            account_id=accounts[2].id
        ).one().state == "reauth_required"


def test_transient_retry_stays_on_account_and_commit_prevents_failover(
    provider_store,
):
    store, _ = provider_store
    connection = _connection(store)
    route = _route(store, connection_id=connection.id)
    first = _account(
        store,
        connection_id=connection.id,
        account_id="account-a",
        sort_order=0,
        key="key-a",
    )
    second = _account(
        store,
        connection_id=connection.id,
        account_id="account-b",
        sort_order=10,
        key="key-b",
    )
    for account in (first, second):
        _entitle(store, account_id=account.id, route_id=route.id)

    selected = store.bind_account(
        owner="alice",
        root_operation_id="root-transient",
        connection_id=connection.id,
        model_route_id=route.id,
    )
    transient = store.record_account_attempt(
        owner="alice",
        binding_id=selected.binding_id,
        expected_revision=selected.binding_revision,
        account_id=selected.account_id,
        outcome="transient",
    )
    assert transient.account_id == selected.account_id
    assert transient.attempt == 2
    assert transient.source == selected.source

    committed = store.commit_account_binding(
        owner="alice",
        binding_id=transient.binding_id,
        expected_revision=transient.binding_revision,
    )
    assert committed.committed is True
    after_quota = store.record_account_attempt(
        owner="alice",
        binding_id=committed.binding_id,
        expected_revision=committed.binding_revision,
        account_id=committed.account_id,
        outcome="quota",
        retry_after_ms=1_000,
    )
    assert after_quota.committed is True
    assert after_quota.account_id == committed.account_id
    assert after_quota.account_id != second.id
    with pytest.raises(ProviderRevisionConflict):
        store.commit_account_binding(
            owner="alice",
            binding_id=committed.binding_id,
            expected_revision=transient.binding_revision,
        )


def test_billing_lanes_are_connection_confined_even_for_same_root(provider_store):
    store, _ = provider_store
    api_connection = _connection(store)
    subscription_connection = _connection(
        store,
        connection_id="connection-subscription",
        billing_lane="subscription",
    )
    api_account = _account(
        store,
        connection_id=api_connection.id,
        account_id="api-account",
        sort_order=0,
        key="api-key",
    )
    subscription_account = _account(
        store,
        connection_id=subscription_connection.id,
        account_id="subscription-account",
        sort_order=0,
        key="subscription-token",
    )

    api_selection = store.bind_account(
        owner="alice",
        root_operation_id="same-root",
        connection_id=api_connection.id,
        billing_lane="metered_api",
    )
    subscription_selection = store.bind_account(
        owner="alice",
        root_operation_id="same-root",
        connection_id=subscription_connection.id,
        billing_lane="subscription",
    )
    assert api_selection.account_id == api_account.id
    assert subscription_selection.account_id == subscription_account.id

    with pytest.raises(BillingLaneMismatch):
        store.bind_account(
            owner="alice",
            root_operation_id="wrong-lane",
            connection_id=subscription_connection.id,
            billing_lane="metered_api",
        )
    with pytest.raises(NoEligibleAccount):
        store.bind_account(
            owner="alice",
            root_operation_id="wrong-pool",
            connection_id=subscription_connection.id,
            allowed_account_ids=[api_account.id],
        )


def test_refresh_lease_is_single_flight_and_commit_uses_credential_cas(provider_store):
    store, factory = provider_store
    connection = _connection(store)
    account = _account(
        store,
        connection_id=connection.id,
        account_id="oauth-account",
        sort_order=0,
        key="refresh-v1",
    )
    lease = store.acquire_refresh_lease(
        owner="alice",
        account_id=account.id,
        holder_id="worker-a",
        now=NOW,
    )
    assert lease.credential_revision == 1
    assert "worker-a" in repr(lease)
    assert lease.token not in repr(lease)
    with factory() as db:
        stored = db.query(ProviderRefreshLease).filter_by(account_id=account.id).one()
        assert stored.token_digest != lease.token

    with pytest.raises(RefreshLeaseBusy):
        store.acquire_refresh_lease(
            owner="alice",
            account_id=account.id,
            holder_id="worker-b",
            now=NOW + timedelta(seconds=1),
        )
    with pytest.raises(RefreshLeaseInvalid):
        store.commit_refresh(
            owner="alice",
            account_id=account.id,
            token="wrong-token",
            expected_credential_revision=1,
            credentials={"type": "oauth", "refresh": "never-stored"},
            now=NOW + timedelta(seconds=2),
        )

    concurrent = store.replace_account_credential(
        owner="alice",
        account_id=account.id,
        expected_credential_version=1,
        credentials={"type": "oauth", "refresh": "concurrent-v2"},
    )
    assert concurrent.credential_version == 2
    with pytest.raises(ProviderRevisionConflict):
        store.commit_refresh(
            owner="alice",
            account_id=account.id,
            token=lease.token,
            expected_credential_revision=1,
            credentials={"type": "oauth", "refresh": "stale-refresh"},
            now=NOW + timedelta(seconds=3),
        )
    store.abort_refresh(
        owner="alice",
        account_id=account.id,
        token=lease.token,
        now=NOW + timedelta(seconds=4),
    )

    current_lease = store.acquire_refresh_lease(
        owner="alice",
        account_id=account.id,
        holder_id="worker-b",
        now=NOW + timedelta(seconds=5),
    )
    route = _route(store, connection_id=connection.id, route_id="refresh-route")
    _entitle(store, account_id=account.id, route_id=route.id)
    binding = store.bind_account(
        owner="alice",
        root_operation_id="refresh-root",
        connection_id=connection.id,
        model_route_id=route.id,
        inherited_account_id=account.id,
    )
    assert binding.credential_version == 2
    store.lease_credential(
        owner="alice",
        holder_id="worker-b",
        root_operation_id="refresh-root",
        connection_id=connection.id,
        account_id=account.id,
        model_id=route.provider_model_id,
        expected_credential_revision=2,
    )
    renewed = store.renew_refresh_lease(
        owner="alice",
        account_id=account.id,
        token=current_lease.token,
        now=NOW + timedelta(seconds=30),
    )
    assert renewed.expires_at == NOW + timedelta(seconds=90)
    with pytest.raises(RefreshLeaseInvalid, match="already renewed"):
        store.renew_refresh_lease(
            owner="alice",
            account_id=account.id,
            token=current_lease.token,
            now=NOW + timedelta(seconds=31),
        )

    refreshed = store.commit_refresh(
        owner="alice",
        account_id=account.id,
        token=current_lease.token,
        expected_credential_revision=2,
        credentials={"type": "oauth", "refresh": "refresh-v3"},
        now=NOW + timedelta(seconds=32),
    )
    assert refreshed.credential_version == 3
    assert store.credential_access(
        owner="alice",
        account_id=account.id,
    ).credentials == {"type": "oauth", "refresh": "refresh-v3"}
    with factory() as db:
        advanced = db.get(ProviderOperationBinding, binding.binding_id)
        assert advanced.credential_revision == 3
        assert advanced.revision == binding.binding_revision
        assert db.query(ProviderCredentialLease).filter_by(account_id=account.id).count() == 0
    refreshed_binding = store.record_account_attempt(
        owner="alice",
        binding_id=binding.binding_id,
        expected_revision=binding.binding_revision,
        account_id=account.id,
        outcome="success",
    )
    assert refreshed_binding.credential_version == 3


def test_explicit_and_live_share_selectors_are_granular_and_owner_authorized(
    provider_store,
):
    store, factory = provider_store
    connection = _connection(store)
    account_a = _account(
        store,
        connection_id=connection.id,
        account_id="account-a",
        sort_order=10,
        key="key-a",
    )
    account_b = _account(
        store,
        connection_id=connection.id,
        account_id="account-b",
        sort_order=20,
        key="key-b",
    )
    route_a = _route(
        store,
        connection_id=connection.id,
        route_id="route-a",
        model_id="model-a",
    )
    route_b = _route(
        store,
        connection_id=connection.id,
        route_id="route-b",
        model_id="model-b",
    )
    for account in (account_a, account_b):
        for route in (route_a, route_b):
            _entitle(store, account_id=account.id, route_id=route.id)

    grant = store.create_share_grant(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        account_selector=AccountSelector.explicit([account_a.id]),
        model_selector=ModelSelector.explicit([route_a.id]),
        disclosure_fields=(),
        grant_id="explicit-grant",
    )
    assert grant.accepted_revision == grant.revision
    with factory() as db:
        legacy = db.get(ProviderShareGrant, grant.id)
        legacy.accepted_revision = None
        db.commit()
    scope = store.assert_share_selection(
        recipient="bob",
        grant_id=grant.id,
        account_id=account_a.id,
        model_route_id=route_a.id,
    )
    assert scope.account_ids == (account_a.id,)
    assert scope.model_route_ids == (route_a.id,)
    assert scope.disclosure_fields == ()
    shared_binding = store.bind_account(
        owner="bob",
        root_operation_id="legacy-share-root",
        connection_id=connection.id,
        model_route_id=route_a.id,
        grant_id=grant.id,
    )
    shared_lease = store.lease_credential(
        owner="bob",
        holder_id="legacy-share-worker",
        root_operation_id="legacy-share-root",
        connection_id=connection.id,
        account_id=shared_binding.account_id,
        model_id=route_a.provider_model_id,
        grant_id=grant.id,
        expected_credential_revision=shared_binding.credential_version,
    )
    assert shared_lease.account_id == account_a.id
    with pytest.raises(ShareDenied, match="outside"):
        store.assert_share_selection(
            recipient="bob",
            grant_id=grant.id,
            account_id=account_b.id,
            model_route_id=route_a.id,
        )

    widened = store.replace_share_selectors(
        owner="alice",
        grant_id=grant.id,
        expected_revision=grant.revision,
        account_selector=AccountSelector.explicit([account_a.id, account_b.id]),
        model_selector=ModelSelector.explicit([route_a.id]),
        disclosure_fields=("account_label",),
    )
    assert widened.accepted_revision == widened.revision
    assert account_b.id in store.resolve_share_scope(
        recipient="bob",
        grant_id=grant.id,
    ).account_ids
    narrowed = store.replace_share_selectors(
        owner="alice",
        grant_id=grant.id,
        expected_revision=widened.revision,
        account_selector=AccountSelector.explicit([account_a.id]),
        model_selector=ModelSelector.explicit([route_a.id]),
        disclosure_fields=(),
    )
    assert narrowed.accepted_revision == narrowed.revision

    live = store.create_share_grant(
        owner="alice",
        recipient="carol",
        connection_id=connection.id,
        account_selector=AccountSelector.all_live(),
        model_selector=ModelSelector.all_live(),
        grant_id="live-grant",
    )
    assert live.accepted_revision == live.revision
    account_c = _account(
        store,
        connection_id=connection.id,
        account_id="account-c",
        sort_order=30,
        key="key-c",
    )
    route_c = _route(
        store,
        connection_id=connection.id,
        route_id="route-c",
        model_id="model-c",
    )
    _entitle(store, account_id=account_c.id, route_id=route_c.id)
    live_scope = store.resolve_share_scope(recipient="carol", grant_id=live.id)
    assert account_c.id in live_scope.account_ids
    assert route_c.id in live_scope.model_route_ids

    with pytest.raises(ProviderValidationError, match="not permitted"):
        store.create_share_grant(
            owner="alice",
            recipient="mallory",
            connection_id=connection.id,
            account_selector=AccountSelector.all_live(),
            model_selector=ModelSelector.all_live(),
            disclosure_fields=("credential_fingerprint",),
        )


def test_set_model_share_is_idempotent_and_reuses_its_aggregate(provider_store):
    store, _ = provider_store
    connection = _connection(store)
    account = _account(
        store,
        connection_id=connection.id,
        account_id="account-a",
        sort_order=10,
        key="key-a",
    )
    route_a = _route(
        store,
        connection_id=connection.id,
        route_id="route-a",
        model_id="model-a",
    )
    route_b = _route(
        store,
        connection_id=connection.id,
        route_id="route-b",
        model_id="model-b",
    )
    _entitle(store, account_id=account.id, route_id=route_a.id)
    _entitle(store, account_id=account.id, route_id=route_b.id)

    assert store.set_model_share(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        model_route_id=route_a.id,
        enabled=False,
    ) is None
    created = store.set_model_share(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        model_route_id=route_a.id,
        enabled=True,
    )
    assert created.state == "active"
    assert created.accepted_revision == created.revision
    assert AccountSelector.parse(created.account_selector) == AccountSelector.all_live()
    assert ModelSelector.parse(created.model_selector).model_route_ids == (route_a.id,)

    replayed = store.set_model_share(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        model_route_id=route_a.id,
        enabled=True,
    )
    assert replayed.id == created.id
    assert replayed.revision == created.revision

    merged = store.set_model_share(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        model_route_id=route_b.id,
        enabled=True,
    )
    assert merged.id == created.id
    assert merged.revision == created.revision + 1
    assert set(ModelSelector.parse(merged.model_selector).model_route_ids) == {
        route_a.id,
        route_b.id,
    }

    kept = store.set_model_share(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        model_route_id=route_a.id,
        enabled=False,
    )
    assert kept.state == "active"
    assert ModelSelector.parse(kept.model_selector).model_route_ids == (route_b.id,)
    replayed_off = store.set_model_share(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        model_route_id=route_a.id,
        enabled=False,
    )
    assert replayed_off.id == kept.id
    assert replayed_off.revision == kept.revision

    revoked = store.set_model_share(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        model_route_id=route_b.id,
        enabled=False,
    )
    assert revoked.state == "revoked"
    assert revoked.accepted_revision == revoked.revision
    replayed_revocation = store.set_model_share(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        model_route_id=route_b.id,
        enabled=False,
    )
    assert replayed_revocation.id == revoked.id
    assert replayed_revocation.revision == revoked.revision

    reactivated = store.set_model_share(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        model_route_id=route_a.id,
        enabled=True,
    )
    assert reactivated.id == created.id
    assert reactivated.state == "active"
    assert ModelSelector.parse(reactivated.model_selector).model_route_ids == (
        route_a.id,
    )
    assert len(store.list_share_grants(owner="alice", include_revoked=True)) == 1


def test_set_model_share_controls_existing_manual_grants_effectively(provider_store):
    store, _ = provider_store
    connection = _connection(store)
    account_a = _account(
        store,
        connection_id=connection.id,
        account_id="account-a",
        sort_order=10,
        key="key-a",
    )
    account_b = _account(
        store,
        connection_id=connection.id,
        account_id="account-b",
        sort_order=20,
        key="key-b",
    )
    route_a = _route(
        store,
        connection_id=connection.id,
        route_id="route-a",
        model_id="model-a",
    )
    route_b = _route(
        store,
        connection_id=connection.id,
        route_id="route-b",
        model_id="model-b",
    )
    route_c = _route(
        store,
        connection_id=connection.id,
        route_id="route-c",
        model_id="model-c",
    )
    explicit = store.create_share_grant(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        account_selector=AccountSelector.explicit([account_a.id]),
        model_selector=ModelSelector.explicit([route_a.id, route_b.id]),
        disclosure_fields=("account_label",),
        grant_id="manual-explicit",
    )
    live = store.create_share_grant(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        account_selector=AccountSelector.explicit([account_b.id]),
        model_selector=ModelSelector.all_live(),
        disclosure_fields=("owner_alias",),
        grant_id="manual-live",
    )

    already_on = store.set_model_share(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        model_route_id=route_a.id,
        enabled=True,
    )
    assert already_on.id == explicit.id
    assert already_on.revision == explicit.revision
    assert len(store.list_share_grants(owner="alice", include_revoked=True)) == 2

    store.set_model_share(
        owner="alice",
        recipient="bob",
        connection_id=connection.id,
        model_route_id=route_a.id,
        enabled=False,
    )
    explicit_after = store.get_share_grant(owner="alice", grant_id=explicit.id)
    live_after = store.get_share_grant(owner="alice", grant_id=live.id)
    assert explicit_after.state == "active"
    assert ModelSelector.parse(explicit_after.model_selector).model_route_ids == (
        route_b.id,
    )
    assert AccountSelector.parse(explicit_after.account_selector) == AccountSelector.explicit(
        [account_a.id]
    )
    assert explicit_after.disclosure_fields == ["account_label"]
    assert live_after.state == "active"
    assert set(ModelSelector.parse(live_after.model_selector).model_route_ids) == {
        route_b.id,
        route_c.id,
    }
    assert AccountSelector.parse(live_after.account_selector) == AccountSelector.explicit(
        [account_b.id]
    )
    assert live_after.disclosure_fields == ["owner_alias"]
    for grant in (explicit_after, live_after):
        assert route_a.id not in store.resolve_share_scope(
            recipient="bob",
            grant_id=grant.id,
        ).model_route_ids


def test_set_model_share_rejects_deterministic_id_collision(provider_store):
    store, _ = provider_store
    connection = _connection(store)
    route = _route(store, connection_id=connection.id)
    canonical_id = store.deterministic_resource_id(
        prefix="psg",
        owner="alice",
        operation="shares.model-toggle",
        idempotency_key='["bob","connection-api"]',
    )
    store.create_share_grant(
        owner="alice",
        recipient="carol",
        connection_id=connection.id,
        account_selector=AccountSelector.all_live(),
        model_selector=ModelSelector.explicit([route.id]),
        grant_id=canonical_id,
    )

    with pytest.raises(ProviderConflict, match="deterministic model-share ID"):
        store.set_model_share(
            owner="alice",
            recipient="bob",
            connection_id=connection.id,
            model_route_id=route.id,
            enabled=True,
        )


def test_managed_provider_v1_wire_binding_and_exact_credential_lease(
    provider_store,
):
    store, factory = provider_store
    connection = _connection(store)
    account = store.create_account(
        owner="alice",
        connection_id=connection.id,
        account_id="wire-account",
        label="Wire account",
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api", "key": "wire-secret"},
    )
    route = _route(
        store,
        connection_id=connection.id,
        route_id="wire-route",
        model_id="wire-model",
    )
    _entitle(store, account_id=account.id, route_id=route.id)

    binding = store.handle_managed_method(
        owner="alice",
        holder_id="worker-generation-1",
        method="_openclank/provider-store/v1/account/bind",
        params={
            "rootOperationID": "wire-root",
            "connectionID": connection.id,
            "providerID": "openai",
            "billingLane": "metered_api",
            "modelID": "wire-model",
            "inheritedAccountID": account.id,
        },
    )
    assert binding == {
        "bindingID": binding["bindingID"],
        "bindingRevision": 1,
        "rootOperationID": "wire-root",
        "connectionID": connection.id,
        "providerID": "openai",
        "billingLane": "metered_api",
        "modelID": "wire-model",
        "accountID": account.id,
        "credentialRevision": 1,
        "credentialRequired": True,
        "source": "inherited",
        "attempt": 1,
        "committed": False,
    }

    credential_lease = store.handle_managed_method(
        owner="alice",
        holder_id="worker-generation-1",
        method="_openclank/provider-store/v1/credential/lease",
        params={
            "rootOperationID": "wire-root",
            "connectionID": connection.id,
            "accountID": account.id,
            "modelID": "wire-model",
            "expectedCredentialRevision": 1,
        },
    )
    assert credential_lease["credential"] == {
        "type": "api",
        "key": "wire-secret",
    }
    assert credential_lease["leaseID"].startswith("pcl_")
    assert credential_lease["expiresAt"] == int(
        (NOW + timedelta(seconds=30)).replace(tzinfo=timezone.utc).timestamp() * 1000
    )

    with factory() as db:
        stored = db.query(ProviderCredentialLease).one()
        assert stored.holder_id == "worker-generation-1"
        assert stored.lease_id_digest != credential_lease["leaseID"]
        assert "wire-secret" not in repr(stored.__dict__)

    from pathlib import Path
    import json

    schema = json.loads(
        Path("contracts/openclank/managed-provider-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.Draft202012Validator(schema).validate(binding)
    jsonschema.Draft202012Validator(schema).validate(credential_lease)


def test_driver_callback_consumes_exact_credential_lease_once(provider_store):
    store, factory = provider_store
    connection = _connection(store, connection_id="callback-connection")
    account = _account(
        store,
        connection_id=connection.id,
        account_id="callback-account",
        sort_order=1,
        key="callback-secret",
    )
    route = _route(
        store,
        connection_id=connection.id,
        route_id="callback-route",
        model_id="callback-model",
    )
    _entitle(store, account_id=account.id, route_id=route.id)
    binding = store.bind_account(
        owner="alice",
        root_operation_id="callback-root",
        connection_id=connection.id,
        model_route_id=route.id,
        inherited_account_id=account.id,
    )
    lease = store.lease_credential(
        owner="alice",
        holder_id="callback-driver",
        root_operation_id="callback-root",
        connection_id=connection.id,
        account_id=account.id,
        model_id=route.provider_model_id,
        expected_credential_revision=binding.credential_version,
        now=NOW,
    )

    with pytest.raises(ProviderNotFound):
        store.consume_credential_lease(
            owner="alice",
            lease_id=lease.lease_id,
            holder_id="other-driver",
            now=NOW,
        )
    consumed = store.consume_credential_lease(
        owner="alice",
        lease_id=lease.lease_id,
        holder_id="callback-driver",
        now=NOW + timedelta(seconds=1),
    )
    assert consumed.credentials == {"type": "api_key", "key": "callback-secret"}
    with factory() as db:
        assert db.query(ProviderCredentialLease).count() == 0
    with pytest.raises(ProviderNotFound):
        store.consume_credential_lease(
            owner="alice",
            lease_id=lease.lease_id,
            holder_id="callback-driver",
            now=NOW + timedelta(seconds=2),
        )

    expired = store.lease_credential(
        owner="alice",
        holder_id="callback-driver",
        root_operation_id="callback-root",
        connection_id=connection.id,
        account_id=account.id,
        model_id=route.provider_model_id,
        expected_credential_revision=account.credential_version,
        now=NOW,
        ttl_seconds=1,
    )
    expired_result = store.consume_credential_lease(
        owner="alice",
        lease_id=expired.lease_id,
        holder_id="callback-driver",
        now=NOW + timedelta(seconds=2),
    )
    assert expired_result.credentials == {}
    assert expired_result.expires_at == NOW + timedelta(seconds=1)


def test_public_revision_claim_is_a_database_cas(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'provider-cas.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    ProviderBase.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    store = ProviderStore(factory, clock=lambda: NOW)
    connection = _connection(store)
    barrier = threading.Barrier(2)

    def update_label(label: str):
        barrier.wait(timeout=5)
        try:
            return store.update_connection(
                owner="alice",
                connection_id=connection.id,
                expected_revision=1,
                label=label,
            )
        except Exception as exc:  # result is asserted below
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update_label, ("first", "second")))

    successes = [row for row in results if not isinstance(row, Exception)]
    failures = [row for row in results if isinstance(row, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ProviderRevisionConflict)
    assert store.get_connection(
        owner="alice",
        connection_id=connection.id,
    ).revision == 2
    engine.dispose()


def test_installation_key_first_use_is_atomic(monkeypatch, tmp_path):
    key_path = tmp_path / "nested" / ".app_key"
    monkeypatch.setattr(secret_storage, "_KEY_PATH", key_path)
    monkeypatch.setattr(secret_storage, "_fernet", None)
    barrier = threading.Barrier(8)

    def load_key(_index: int):
        barrier.wait(timeout=5)
        return secret_storage._load_or_create_key()

    with ThreadPoolExecutor(max_workers=8) as executor:
        keys = list(executor.map(load_key, range(8)))

    assert len(set(keys)) == 1
    assert key_path.read_bytes() == keys[0]
    if os.name != "nt":
        assert key_path.stat().st_mode & 0o777 == 0o600


def test_account_tombstone_fences_refresh_and_removes_credential(provider_store):
    store, factory = provider_store
    connection = _connection(store)
    account = _account(
        store,
        connection_id=connection.id,
        account_id="delete-during-refresh",
        sort_order=0,
        key="before-delete",
    )
    lease = store.acquire_refresh_lease(
        owner="alice",
        account_id=account.id,
        holder_id="worker-a",
        now=NOW,
    )
    tombstone = store.delete_account(
        owner="alice",
        account_id=account.id,
        expected_revision=account.revision,
    )
    assert tombstone.credential_version == account.credential_version + 1
    assert tombstone.credential_envelope is None
    with pytest.raises((ProviderNotFound, RefreshLeaseInvalid)):
        store.commit_refresh(
            owner="alice",
            account_id=account.id,
            token=lease.token,
            expected_credential_revision=account.credential_version,
            credentials={"type": "oauth", "refresh": "must-not-resurrect"},
            now=NOW + timedelta(seconds=1),
        )
    with factory() as db:
        raw = db.query(ProviderAccount).filter_by(id=account.id).one()
        assert raw.credential_envelope is None
        assert raw.credential_fingerprint is None


def test_model_sync_preserves_ids_retires_missing_and_entitles_account(provider_store):
    store, _ = provider_store
    connection = _connection(store)
    account = _account(
        store,
        connection_id=connection.id,
        account_id="catalog-account",
        sort_order=0,
        key="catalog-secret",
    )
    connection = store.get_connection(owner="alice", connection_id=connection.id)

    connection, first = store.sync_model_routes(
        owner="alice",
        connection_id=connection.id,
        expected_revision=connection.revision,
        eligible_account_id=account.id,
        model_routes=[
            {
                "id": "route-stable-a",
                "provider_model_id": "model-a",
                "display_name": "Model A",
                "operations": ["chat.complete", "chat.stream"],
                "capabilities": {"reasoning": True},
                "provenance": {"authority": "managed-engine"},
            },
            {
                "id": "route-stable-b",
                "provider_model_id": "model-b",
                "display_name": "Model B",
                "operations": ["chat.complete", "chat.stream"],
                "capabilities": {},
                "provenance": {"authority": "managed-engine"},
            },
        ],
    )
    assert {row.id for row in first} == {"route-stable-a", "route-stable-b"}
    assert store.model_eligibility(
        owner="alice",
        model_route_id="route-stable-a",
    )[0]["eligible"] is True

    connection, second = store.sync_model_routes(
        owner="alice",
        connection_id=connection.id,
        expected_revision=connection.revision,
        eligible_account_id=account.id,
        model_routes=[
            {
                "id": "ignored-replacement-id",
                "provider_model_id": "model-a",
                "display_name": "Model A refreshed",
                "operations": ["chat.complete", "chat.stream"],
                "capabilities": {"reasoning": False},
                "provenance": {"authority": "managed-engine"},
            }
        ],
    )
    assert [row.id for row in second] == ["route-stable-a"]
    assert second[0].display_name == "Model A refreshed"
    all_rows = store.list_model_routes(
        owner="alice",
        connection_id=connection.id,
        include_deleted=True,
    )
    retired = next(row for row in all_rows if row.provider_model_id == "model-b")
    assert retired.deleted_at == NOW
    assert retired.enabled is False

    _, restored = store.sync_model_routes(
        owner="alice",
        connection_id=connection.id,
        expected_revision=connection.revision,
        eligible_account_id=account.id,
        model_routes=[
            {
                "id": "another-ignored-id",
                "provider_model_id": "model-b",
                "display_name": "Model B restored",
                "operations": ["chat.complete", "chat.stream"],
                "capabilities": {},
                "provenance": {"authority": "managed-engine"},
            }
        ],
    )
    assert restored[0].id == "route-stable-b"
    assert restored[0].deleted_at is None


def test_keyless_model_sync_needs_no_account_entitlement(provider_store):
    store, _ = provider_store
    connection = store.create_connection(
        owner="alice",
        connection_id="local-ollama",
        family_id="ollama",
        adapter_id="ollama",
        kind="local",
        billing_lane="local",
        label="Ollama",
        normalized_url="http://127.0.0.1:11434",
    )
    _, routes = store.sync_model_routes(
        owner="alice",
        connection_id=connection.id,
        expected_revision=connection.revision,
        model_routes=[
            {
                "id": "route-keyless",
                "provider_model_id": "llama3.2",
                "display_name": "llama3.2",
                "operations": ["chat.complete", "chat.stream"],
                "capabilities": {},
                "provenance": {"authority": "managed-engine"},
            }
        ],
    )
    assert store.model_eligibility(
        owner="alice",
        model_route_id=routes[0].id,
    ) == []


def test_connection_eligibility_query_count_is_route_cardinality_independent(
    provider_store,
):
    store, factory = provider_store
    connection = _connection(store, connection_id="eligibility-summary")
    accounts = [
        _account(
            store,
            connection_id=connection.id,
            account_id=f"summary-account-{index}",
            sort_order=index,
            key=f"summary-key-{index}",
        )
        for index in range(2)
    ]
    for index in range(50):
        route = _route(
            store,
            connection_id=connection.id,
            route_id=f"summary-route-{index:03d}",
            model_id=f"summary-model-{index:03d}",
        )
        for account in accounts:
            _entitle(store, account_id=account.id, route_id=route.id)

    statements: list[str] = []
    engine = factory.kw["bind"]

    def record_select(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_select)
    try:
        result = store.connection_eligibility(
            owner="alice",
            connection_id=connection.id,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_select)

    assert len(result) == 50
    assert all(len(item["accounts"]) == 2 for item in result)
    assert all(row["eligible"] for item in result for row in item["accounts"])
    assert len(statements) == 6


@pytest.mark.parametrize("route_count", [1, 50, 500])
def test_management_snapshot_query_budget_is_model_cardinality_independent(
    provider_store,
    route_count,
):
    store, factory = provider_store
    connection = _connection(
        store,
        connection_id=f"snapshot-models-{route_count}",
    )
    for index in range(route_count):
        _route(
            store,
            connection_id=connection.id,
            route_id=f"snapshot-route-{index:03d}",
            model_id=f"snapshot-model-{index:03d}",
        )

    statements: list[str] = []
    engine = factory.kw["bind"]

    def record_select(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_select)
    started = perf_counter()
    try:
        snapshot = store.management_snapshot(owner="alice")
    finally:
        elapsed = perf_counter() - started
        event.remove(engine, "before_cursor_execute", record_select)

    assert len(snapshot.model_routes) == route_count
    assert len(statements) == 4
    # This is intentionally generous for shared/slow CI.  The tighter local
    # acceptance measurement is recorded in the S07 closure evidence.
    assert elapsed < 5.0


@pytest.mark.parametrize("share_count", [1, 30, 300])
def test_received_share_projection_query_budget_is_share_cardinality_independent(
    provider_store,
    share_count,
):
    store, factory = provider_store
    connection = _connection(
        store,
        connection_id=f"received-shares-{share_count}",
    )
    account = _account(
        store,
        connection_id=connection.id,
        account_id=f"received-account-{share_count}",
        sort_order=0,
        key=f"received-secret-{share_count}",
    )
    route = _route(
        store,
        connection_id=connection.id,
        route_id=f"received-route-{share_count}",
        model_id=f"received-model-{share_count}",
    )
    _entitle(store, account_id=account.id, route_id=route.id)
    for index in range(share_count):
        store.create_share_grant(
            owner="alice",
            recipient="bob",
            connection_id=connection.id,
            account_selector=AccountSelector.all_live(),
            model_selector=ModelSelector.all_live(),
            disclosure_fields=("detailed_health",),
            grant_id=f"received-grant-{share_count}-{index:03d}",
        )

    statements: list[str] = []
    engine = factory.kw["bind"]

    def record_select(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_select)
    started = perf_counter()
    try:
        projections = store.received_share_projections(recipient="bob")
    finally:
        elapsed = perf_counter() - started
        event.remove(engine, "before_cursor_execute", record_select)

    assert len(projections) == share_count
    assert all(len(item.accounts) == 1 for item in projections)
    assert all(len(item.model_routes) == 1 for item in projections)
    assert all(item.health_by_account[account.id][0]["eligible"] for item in projections)
    assert len(statements) == 7
    assert elapsed < 5.0

"""Cross-store account owner lifecycle and compensation contracts."""

from datetime import datetime
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.operation_models import (
    AccountLifecycleOperation,
    ArtifactRecord,
    OperationBase,
    OperationJournal,
)
from core.provider_models import ProviderBase
from src import secret_storage
from src.default_persona import get_default_persona, set_default_persona
from src.openclank import account_lifecycle as lifecycle_module
from src.openclank.account_lifecycle import (
    AccountOwnerLifecycle,
    AccountOwnerLifecycleError,
)
from src.openclank.artifacts import ArtifactStore
from src.openclank.operation_journal import OperationJournalStore
from src.openclank.provider_store import ProviderNotFound, ProviderStore
from src.preset_manager import PresetManager


NOW = datetime(2026, 8, 26, 12, 0, 0)


@pytest.fixture()
def lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(engine)
    OperationBase.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    provider = ProviderStore(factory, clock=lambda: NOW)
    journal = OperationJournalStore(factory, clock=lambda: NOW)
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        session_factory=factory,
        clock=lambda: NOW,
    )
    preset_root = tmp_path / "presets"
    preset_root.mkdir()
    presets = PresetManager(str(preset_root))
    coordinator = AccountOwnerLifecycle(
        provider_store=provider,
        operation_journal=journal,
        artifact_store=artifacts,
        preset_manager=presets,
        operation_path=tmp_path / "account-operations.json",
    )
    yield coordinator, provider, journal, artifacts, presets, factory
    engine.dispose()


def _seed_owner(lifecycle, *, owner="alice"):
    coordinator, provider, journal, artifacts, presets, _factory = lifecycle
    connection = provider.create_connection(
        owner=owner,
        connection_id=f"connection-{owner}",
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label=f"{owner} connection",
    )
    account = provider.create_account(
        owner=owner,
        connection_id=connection.id,
        account_id=f"account-{owner}",
        label=f"{owner} account",
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api_key", "key": f"secret-{owner}"},
    )
    route = provider.create_model_route(
        owner=owner,
        connection_id=connection.id,
        model_route_id=f"route-{owner}",
        provider_model_id=f"model-{owner}",
        display_name=f"Model {owner}",
        operations=("chat.complete",),
    )
    operation, _ = journal.begin(
        owner=owner,
        root_operation_id=f"root-{owner}",
        operation="chat.complete",
        idempotency_key=f"owner-lifecycle-{owner}-0001",
        request={"prompt": owner},
        connection_id=connection.id,
        billing_lane=connection.billing_lane,
        model_route_id=route.id,
    )
    artifact = artifacts.put(
        owner=owner,
        chunks=[f"artifact-{owner}".encode()],
        media_type="text/plain",
    )
    set_default_persona(
        owner,
        name=f"Persona {owner}",
        system_prompt=f"Speak for {owner}.",
        preset_manager=presets,
        sync_assistant=False,
    )
    return connection, account, route, operation, artifact


def test_coordinator_renames_then_purges_every_non_generic_owner_store(lifecycle):
    coordinator, provider, _journal, artifacts, presets, factory = lifecycle
    _connection, account, _route, operation, artifact = _seed_owner(lifecycle)
    _seed_owner(lifecycle, owner="bob")

    renamed = coordinator.rename_owner("ALICE", "alice2")

    assert renamed.source_owner == "alice"
    assert renamed.target_owner == "alice2"
    assert renamed.stores["provider"]["accounts"] == 1
    assert renamed.stores["operation_journal"] == 1
    assert renamed.stores["artifacts"] == 1
    assert renamed.stores["default_persona"] == 1
    assert provider.credential_access(
        owner="alice2",
        account_id=account.id,
    ).credentials["key"] == "secret-alice"
    with pytest.raises(ProviderNotFound):
        provider.credential_access(owner="alice", account_id=account.id)
    assert b"".join(
        artifacts.read_chunks(owner="alice2", artifact_id=artifact.id)
    ) == b"artifact-alice"
    assert get_default_persona("alice2", preset_manager=presets)["name"] == "Persona alice"
    with factory() as db:
        assert db.get(OperationJournal, operation.id).owner == "alice2"
        assert db.get(ArtifactRecord, artifact.id).owner == "alice2"

    purged = coordinator.purge_owner("ALICE2")

    assert purged.stores["provider"]["accounts"] == 1
    assert purged.stores["operation_journal"] == 1
    assert purged.stores["artifacts"] == 1
    assert purged.stores["default_persona"] == 1
    assert get_default_persona("alice2", preset_manager=presets)["is_factory"] is True
    assert provider.get_connection(owner="bob", connection_id="connection-bob")
    assert b"".join(
        artifacts.read_chunks(owner="bob", artifact_id=next(
            row.id
            for row in _artifact_rows(factory)
            if row.owner == "bob"
        ))
    ) == b"artifact-bob"
    with factory() as db:
        assert db.query(OperationJournal).filter_by(owner="alice2").count() == 0
        assert db.query(ArtifactRecord).filter_by(owner="alice2").count() == 0


def _artifact_rows(factory):
    with factory() as db:
        rows = db.query(ArtifactRecord).all()
        for row in rows:
            db.expunge(row)
        return rows


def test_rename_compensates_prior_stores_when_a_late_store_fails(
    lifecycle,
    monkeypatch,
):
    coordinator, provider, _journal, artifacts, presets, factory = lifecycle
    _connection, account, _route, operation, artifact = _seed_owner(lifecycle)

    def fail_persona(*_args, **_kwargs):
        raise RuntimeError("persona store unavailable")

    monkeypatch.setattr(
        lifecycle_module,
        "rename_default_persona_owner",
        fail_persona,
    )

    with pytest.raises(AccountOwnerLifecycleError) as caught:
        coordinator.rename_owner("alice", "alice2")

    assert caught.value.failed_store == "default_persona"
    assert caught.value.rollback_errors == {}
    assert provider.credential_access(
        owner="alice",
        account_id=account.id,
    ).credentials["key"] == "secret-alice"
    assert b"".join(
        artifacts.read_chunks(owner="alice", artifact_id=artifact.id)
    ) == b"artifact-alice"
    assert get_default_persona("alice", preset_manager=presets)["name"] == "Persona alice"
    with factory() as db:
        assert db.get(OperationJournal, operation.id).owner == "alice"
        assert db.get(ArtifactRecord, artifact.id).owner == "alice"


def test_partial_tombstone_purge_is_idempotently_retryable(lifecycle):
    coordinator, provider, journal, artifacts, presets, factory = lifecycle
    _connection, _account, _route, operation, artifact = _seed_owner(lifecycle)

    class FailOnceJournal:
        def __init__(self, delegate):
            self.delegate = delegate
            self.failed = False

        def rename_owner(self, old_owner, new_owner):
            return self.delegate.rename_owner(old_owner, new_owner)

        def purge_owner(self, owner):
            if not self.failed:
                self.failed = True
                raise RuntimeError("temporary journal failure")
            return self.delegate.purge_owner(owner)

    coordinator.operation_journal = FailOnceJournal(journal)
    coordinator.rename_owner("alice", "deleted:account-stable")

    with pytest.raises(AccountOwnerLifecycleError) as caught:
        coordinator.purge_owner("deleted:account-stable")

    assert caught.value.failed_store == "operation_journal"
    assert "provider" in caught.value.receipt.stores
    retry = coordinator.purge_owner("deleted:account-stable")
    assert retry.stores["provider"]["connections"] == 0
    assert retry.stores["operation_journal"] == 1
    assert retry.stores["artifacts"] == 1
    assert retry.stores["default_persona"] == 1
    with factory() as db:
        assert db.get(OperationJournal, operation.id) is None
        assert db.get(ArtifactRecord, artifact.id) is None
    assert get_default_persona(
        "deleted:account-stable",
        preset_manager=presets,
    )["is_factory"] is True


def test_owner_inventory_is_content_free_and_spans_every_coordinated_store(lifecycle):
    coordinator, _provider, _journal, _artifacts, _presets, _factory = lifecycle
    _seed_owner(lifecycle)

    inventory = coordinator.owner_inventory("ALICE")

    assert inventory["owner"] == "alice"
    assert inventory["count"] >= 4
    assert set(inventory["stores"]) == {
        "provider",
        "operation_journal",
        "artifacts",
        "default_persona",
    }
    encoded = json.dumps(inventory)
    assert "secret-alice" not in encoded
    assert "artifact-alice" not in encoded
    assert len(inventory["fingerprint"]) == 64


def test_reconcile_rename_closes_effect_before_checkpoint_crash(lifecycle):
    coordinator, provider, journal, artifacts, presets, factory = lifecycle
    _connection, account, _route, operation, artifact = _seed_owner(lifecycle)
    # Simulate a process death after the first store committed but before the
    # enclosing account operation could checkpoint it.
    assert journal.rename_owner("alice", "deleted:stable") == 1

    receipt = coordinator.reconcile_rename("alice", "deleted:stable")

    assert receipt.stores["operation_journal"] == {"already_applied": True}
    assert provider.credential_access(
        owner="deleted:stable", account_id=account.id
    ).credentials["key"] == "secret-alice"
    assert b"".join(
        artifacts.read_chunks(owner="deleted:stable", artifact_id=artifact.id)
    ) == b"artifact-alice"
    assert get_default_persona(
        "deleted:stable", preset_manager=presets
    )["is_factory"] is False
    with factory() as db:
        assert db.get(OperationJournal, operation.id).owner == "deleted:stable"


def test_reconcile_rename_rejects_split_source_and_target_state(lifecycle):
    coordinator, _provider, journal, _artifacts, _presets, _factory = lifecycle
    _seed_owner(lifecycle)
    _seed_owner(lifecycle, owner="deleted:stable")

    with pytest.raises(AccountOwnerLifecycleError) as caught:
        coordinator.reconcile_rename("alice", "deleted:stable")

    assert caught.value.failed_store == "operation_journal"
    assert journal.owner_inventory("alice")["count"] == 1
    assert journal.owner_inventory("deleted:stable")["count"] == 1


def test_delete_operation_survives_restart_and_reclaims_dead_claim(
    lifecycle,
    monkeypatch,
):
    coordinator, provider, journal, artifacts, presets, _factory = lifecycle
    operation = coordinator.begin_delete_operation(
        actor="admin",
        source_owner="alice",
        subject_id="account-stable",
        tombstone_owner="deleted:account-stable",
    )
    coordinator.claim_delete_operation(operation["operation_id"])

    restarted = AccountOwnerLifecycle(
        provider_store=provider,
        operation_journal=journal,
        artifact_store=artifacts,
        preset_manager=presets,
        operation_path=coordinator.operation_path,
    )
    monkeypatch.setattr(restarted, "_process_running", lambda _pid: False)
    claimed = restarted.claim_delete_operation(operation["operation_id"])
    claim_token = claimed["receipt"]["claim"]["token"]
    restarted.checkpoint_delete_operation(
        operation["operation_id"],
        "auth_deleted",
        True,
        claim_token=claim_token,
    )
    completed = restarted.checkpoint_delete_operation(
        operation["operation_id"],
        "token_cache_invalidated",
        True,
        complete=True,
        claim_token=claim_token,
    )

    assert claimed["state"] == "prepared"
    assert completed["state"] == "complete"
    assert "claim" not in completed
    stored_steps = restarted.get_delete_operation(operation["operation_id"])["steps"]
    assert stored_steps["auth_deleted"]["state"] == "applied"
    assert stored_steps["token_cache_invalidated"]["state"] == "applied"


def test_delete_operation_rejects_a_live_foreign_claim(lifecycle):
    coordinator, provider, journal, artifacts, presets, _factory = lifecycle
    operation = coordinator.begin_delete_operation(
        actor="admin",
        source_owner="alice",
        subject_id="account-stable",
        tombstone_owner="deleted:account-stable",
    )
    coordinator.claim_delete_operation(operation["operation_id"])
    concurrent = AccountOwnerLifecycle(
        provider_store=provider,
        operation_journal=journal,
        artifact_store=artifacts,
        preset_manager=presets,
        operation_path=coordinator.operation_path,
    )

    with pytest.raises(AccountOwnerLifecycleError, match="already in progress"):
        concurrent.claim_delete_operation(operation["operation_id"])


def test_aborted_operation_is_terminal_and_cannot_be_reclaimed(lifecycle):
    coordinator, _provider, _journal, _artifacts, _presets, _factory = lifecycle
    operation = coordinator.begin_delete_operation(
        actor="admin",
        source_owner="alice",
        subject_id="account-stable",
        tombstone_owner="deleted:account-stable",
    )
    claimed = coordinator.claim_delete_operation(operation["operation_id"])
    claim_token = claimed["receipt"]["claim"]["token"]

    aborted = coordinator.abort_operation(
        operation["operation_id"],
        RuntimeError("preflight refused"),
        claim_token=claim_token,
    )

    assert aborted["state"] == "aborted"
    assert "claim" not in aborted["receipt"]
    assert coordinator.list_active_operations() == []
    assert coordinator.owner_has_active_operation("alice") is False
    for force in (False, True):
        with pytest.raises(AccountOwnerLifecycleError, match="terminal"):
            coordinator.claim_delete_operation(
                operation["operation_id"],
                force_foreign_takeover=force,
            )


def test_claim_token_prevents_stale_handler_checkpoint_and_release(lifecycle):
    coordinator, provider, journal, artifacts, presets, _factory = lifecycle
    operation = coordinator.begin_delete_operation(
        actor="admin",
        source_owner="alice",
        subject_id="account-stable",
        tombstone_owner="deleted:account-stable",
    )
    first = coordinator.claim_delete_operation(operation["operation_id"])
    first_token = first["receipt"]["claim"]["token"]

    restarted = AccountOwnerLifecycle(
        provider_store=provider,
        operation_journal=journal,
        artifact_store=artifacts,
        preset_manager=presets,
        operation_path=coordinator.operation_path,
    )
    restarted._process_running = lambda _pid: False
    second = restarted.claim_delete_operation(operation["operation_id"])
    second_token = second["receipt"]["claim"]["token"]
    assert second_token != first_token

    with pytest.raises(AccountOwnerLifecycleError, match="no longer belongs"):
        coordinator.checkpoint_delete_operation(
            operation["operation_id"],
            "stale",
            claim_token=first_token,
        )
    with pytest.raises(AccountOwnerLifecycleError, match="no longer belongs"):
        coordinator.fail_delete_operation(
            operation["operation_id"],
            RuntimeError("stale"),
            claim_token=first_token,
        )
    restarted.checkpoint_delete_operation(
        operation["operation_id"],
        "current",
        claim_token=second_token,
    )


def test_foreign_host_claim_requires_explicit_takeover_but_live_local_never_does(
    lifecycle,
    monkeypatch,
):
    coordinator, provider, journal, artifacts, presets, factory = lifecycle
    operation = coordinator.begin_delete_operation(
        actor="admin",
        source_owner="alice",
        subject_id="account-stable",
        tombstone_owner="deleted:account-stable",
    )
    first = coordinator.claim_delete_operation(operation["operation_id"])

    with factory() as db:
        row = db.get(AccountLifecycleOperation, operation["operation_id"])
        receipt = dict(row.receipt or {})
        claim = dict(receipt["claim"])
        claim["host"] = "retired-worker.example"
        receipt["claim"] = claim
        row.receipt = receipt
        db.commit()

    restarted = AccountOwnerLifecycle(
        provider_store=provider,
        operation_journal=journal,
        artifact_store=artifacts,
        preset_manager=presets,
        operation_path=coordinator.operation_path,
    )
    with pytest.raises(AccountOwnerLifecycleError, match="already in progress"):
        restarted.claim_delete_operation(operation["operation_id"])

    recovered = restarted.claim_delete_operation(
        operation["operation_id"],
        force_foreign_takeover=True,
    )
    assert recovered["receipt"]["claim"]["token"] != first["receipt"]["claim"]["token"]

    # Even the explicit recovery path cannot steal a live handler on this
    # host.  This protects two concurrent HTTP resume requests in one process.
    monkeypatch.setattr(restarted, "_process_running", lambda _pid: True)
    with pytest.raises(AccountOwnerLifecycleError, match="already in progress"):
        restarted.claim_delete_operation(
            operation["operation_id"],
            force_foreign_takeover=True,
        )


def test_lifecycle_operation_is_db_authoritative_unique_and_marks_applying(lifecycle):
    coordinator, _provider, _journal, _artifacts, _presets, factory = lifecycle
    operation = coordinator.begin_rename_operation(
        actor_account_id="admin-stable",
        source_owner="alice",
        target_owner="alice2",
        subject_id="account-stable",
    )

    applying = coordinator.start_operation_step(operation["operation_id"], "memory")

    assert applying["steps"]["memory"] == {"state": "applying", "attempts": 1}
    with factory() as db:
        row = db.get(AccountLifecycleOperation, operation["operation_id"])
        assert row.account_id == "account-stable"
        assert row.active_account_id == "account-stable"
        assert row.kind == "rename"
        assert row.steps["memory"]["state"] == "applying"
    with pytest.raises(AccountOwnerLifecycleError, match="already has an active"):
        coordinator.begin_delete_operation(
            actor="admin-stable",
            source_owner="alice",
            subject_id="account-stable",
            tombstone_owner="deleted:account-stable",
        )

    coordinator.checkpoint_delete_operation(
        operation["operation_id"],
        "memory",
        {"count": 2},
        complete=True,
    )
    replacement = coordinator.begin_delete_operation(
        actor="admin-stable",
        source_owner="alice2",
        subject_id="account-stable",
        tombstone_owner="deleted:account-stable",
    )
    assert replacement["state"] == "prepared"


def test_active_operation_reserves_every_owner_key_across_account_ids(lifecycle):
    coordinator, _provider, _journal, _artifacts, _presets, _factory = lifecycle
    active = coordinator.begin_rename_operation(
        actor_account_id="admin-stable",
        source_owner="Alice",
        target_owner="Alice2",
        subject_id="account-a",
    )

    conflicting_operations = (
        lambda: coordinator.begin_delete_operation(
            actor="admin-stable",
            source_owner="bob",
            subject_id="account-b",
            tombstone_owner="ALICE2",
        ),
        lambda: coordinator.begin_rename_operation(
            actor_account_id="admin-stable",
            source_owner="bob",
            target_owner="ALICE",
            subject_id="account-c",
        ),
    )
    for begin in conflicting_operations:
        with pytest.raises(AccountOwnerLifecycleError, match="active lifecycle"):
            begin()

    assert [
        operation["operation_id"]
        for operation in coordinator.list_active_operations()
    ] == [active["operation_id"]]


def test_checkpoint_preserves_kind_specific_post_auth_states(lifecycle):
    coordinator, _provider, _journal, _artifacts, _presets, _factory = lifecycle
    rename = coordinator.begin_rename_operation(
        actor_account_id="admin-stable",
        source_owner="alice",
        target_owner="alice2",
        subject_id="rename-stable",
    )
    coordinator.start_operation_step(rename["operation_id"], "auth_renamed")
    renamed = coordinator.checkpoint_delete_operation(
        rename["operation_id"], "auth_renamed", True
    )
    converging = coordinator.checkpoint_delete_operation(
        rename["operation_id"], "memory", {"count": 1}
    )
    assert renamed["state"] == "converging"
    assert converging["state"] == "converging"

    delete = coordinator.begin_delete_operation(
        actor="admin-stable",
        source_owner="bob",
        subject_id="delete-stable",
        tombstone_owner="deleted:delete-stable",
    )
    coordinator.start_operation_step(delete["operation_id"], "auth_deleted")
    committed = coordinator.checkpoint_delete_operation(
        delete["operation_id"], "auth_deleted", True
    )
    purging = coordinator.checkpoint_delete_operation(
        delete["operation_id"], "memory_purged", {"count": 1}
    )
    assert committed["state"] == "purging"
    assert purging["state"] == "purging"

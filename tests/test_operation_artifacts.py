from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.operation_models import ArtifactRecord, OperationBase, OperationJournal
from core.provider_models import (
    ProviderBase,
    ProviderConnection,
    ProviderModelRoute,
    ProviderOperationBinding,
)
from src.openclank.artifacts import (
    ArtifactError,
    ArtifactIntegrityError,
    ArtifactNotFound,
    ArtifactStore,
)
from src.openclank.local_executor import (
    ExecutorOutput,
    LocalExecutorBroker,
    LocalExecutorError,
)
from src.openclank.operation_journal import OperationConflict, OperationJournalStore


@pytest.fixture
def operation_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'operations.db'}")
    OperationBase.metadata.create_all(engine)
    ProviderBase.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_artifacts_are_owner_scoped_hashed_and_acknowledged(tmp_path, operation_db):
    now = [datetime(2026, 8, 9, 12, 0, 0)]
    store = ArtifactStore(
        tmp_path / "spool",
        session_factory=operation_db,
        clock=lambda: now[0],
    )
    row = store.put(owner="alice", chunks=[b"abc", b"123"], media_type="image/png")
    assert row.id.startswith("art_")
    assert b"".join(store.read_chunks(owner="alice", artifact_id=row.id)) == b"abc123"
    with pytest.raises(ArtifactNotFound):
        store.get(owner="bob", artifact_id=row.id)

    store.acknowledge(owner="alice", artifact_id=row.id)
    now[0] += timedelta(days=2)
    assert store.cleanup_expired() == 0


def test_unacknowledged_artifact_expires_and_tampering_is_detected(tmp_path, operation_db):
    now = [datetime(2026, 8, 9, 12, 0, 0)]
    store = ArtifactStore(
        tmp_path / "spool",
        session_factory=operation_db,
        clock=lambda: now[0],
    )
    row = store.put(owner="alice", chunks=[b"payload"], media_type="application/octet-stream")
    path = store.root / row.relative_path
    path.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        b"".join(store.read_chunks(owner="alice", artifact_id=row.id))

    now[0] += timedelta(hours=25)
    assert store.cleanup_expired() == 1
    assert not path.exists()


def test_operation_journal_idempotency_and_commit_barrier(operation_db):
    store = OperationJournalStore(operation_db)
    kwargs = {
        "owner": "alice",
        "root_operation_id": "root-1",
        "operation": "image.generate",
        "idempotency_key": "operation-key-0001",
        "request": {"prompt": "tree"},
        "connection_id": "connection-1",
        "billing_lane": "metered_api",
        "model_route_id": "route-1",
    }
    row, replay = store.begin(**kwargs)
    assert replay is False
    same, replay = store.begin(**kwargs)
    assert replay is True and same.id == row.id
    with pytest.raises(OperationConflict):
        store.begin(**{**kwargs, "request": {"prompt": "different"}})

    committed = store.cas(
        owner="alice",
        operation_id=row.id,
        expected_revision=row.revision,
        selected_account_id="account-1",
        commit_reason="first_stream_event",
    )
    with pytest.raises(OperationConflict):
        store.cas(
            owner="alice",
            operation_id=row.id,
            expected_revision=committed.revision,
            selected_account_id="account-2",
        )


def test_operation_and_artifact_owner_lifecycle_is_exact(tmp_path, operation_db):
    artifacts = ArtifactStore(tmp_path / "spool", session_factory=operation_db)
    journal = OperationJournalStore(operation_db)
    alice_artifact = artifacts.put(
        owner="alice",
        chunks=[b"alice-payload"],
        media_type="image/png",
    )
    bob_artifact = artifacts.put(
        owner="bob",
        chunks=[b"bob-payload"],
        media_type="image/png",
    )
    alice_operation, _ = journal.begin(
        owner="alice",
        root_operation_id="alice-root",
        operation="image.generate",
        idempotency_key="alice-operation-key",
        request={"prompt": "alice"},
        connection_id="connection-a",
        billing_lane="metered_api",
        model_route_id="route-a",
    )

    assert journal.rename_owner("ALICE", "alice2") == 1
    assert artifacts.rename_owner("ALICE", "alice2") == 1
    assert b"".join(
        artifacts.read_chunks(owner="alice2", artifact_id=alice_artifact.id)
    ) == b"alice-payload"
    with pytest.raises(ArtifactNotFound):
        artifacts.get(owner="alice", artifact_id=alice_artifact.id)
    with operation_db() as db:
        assert db.get(OperationJournal, alice_operation.id).owner == "alice2"
        renamed = db.get(ArtifactRecord, alice_artifact.id)
        assert renamed.owner == "alice2"
        assert renamed.relative_path.startswith(
            artifacts._owner_path("alice2").name + "/"
        )

    assert journal.purge_owner("alice2") == 1
    assert artifacts.purge_owner("alice2") == 1
    assert b"".join(
        artifacts.read_chunks(owner="bob", artifact_id=bob_artifact.id)
    ) == b"bob-payload"
    with operation_db() as db:
        assert db.get(OperationJournal, alice_operation.id) is None
        assert db.get(ArtifactRecord, alice_artifact.id) is None
        assert db.get(ArtifactRecord, bob_artifact.id).owner == "bob"


def test_operation_and_artifact_rename_refuse_destination_state(
    tmp_path,
    operation_db,
):
    artifacts = ArtifactStore(tmp_path / "spool", session_factory=operation_db)
    journal = OperationJournalStore(operation_db)
    alice = artifacts.put(owner="alice", chunks=[b"a"], media_type="image/png")
    artifacts.put(owner="bob", chunks=[b"b"], media_type="image/png")
    journal.begin(
        owner="alice",
        root_operation_id="alice-root",
        operation="chat.complete",
        idempotency_key="alice-operation-key",
        request={"messages": []},
        connection_id="connection-a",
        billing_lane="metered_api",
        model_route_id="route-a",
    )
    journal.begin(
        owner="bob",
        root_operation_id="bob-root",
        operation="chat.complete",
        idempotency_key="bob-operation-key-1",
        request={"messages": []},
        connection_id="connection-b",
        billing_lane="metered_api",
        model_route_id="route-b",
    )

    with pytest.raises(OperationConflict, match="target operation owner"):
        journal.rename_owner("alice", "bob")
    with pytest.raises(ArtifactError, match="target artifact owner"):
        artifacts.rename_owner("alice", "bob")
    assert b"".join(artifacts.read_chunks(owner="alice", artifact_id=alice.id)) == b"a"


def test_operation_journal_rejects_binding_from_a_different_model(operation_db):
    with operation_db() as db:
        db.add(
            ProviderConnection(
                id="connection-1",
                owner="alice",
                family_id="openai",
                adapter_id="openai",
                kind="api",
                billing_lane="metered_api",
                label="OpenAI",
            )
        )
        db.add(
            ProviderModelRoute(
                id="route-1",
                connection_id="connection-1",
                owner="alice",
                provider_model_id="model-a",
                display_name="Model A",
            )
        )
        db.add(
            ProviderOperationBinding(
                id="binding-wrong-model",
                owner="alice",
                credential_owner="alice",
                root_operation_id="root-2",
                connection_id="connection-1",
                provider_id="openai",
                billing_lane="metered_api",
                model_id="model-b",
                source="round_robin",
                state="uncommitted",
            )
        )
        db.commit()

    store = OperationJournalStore(operation_db)
    row, _ = store.begin(
        owner="alice",
        root_operation_id="root-2",
        operation="chat.complete",
        idempotency_key="operation-key-model-0002",
        request={"messages": []},
        connection_id="connection-1",
        billing_lane="metered_api",
        model_route_id="route-1",
    )
    with pytest.raises(OperationConflict, match="selected model"):
        store.cas(
            owner="alice",
            operation_id=row.id,
            expected_revision=row.revision,
            binding_id="binding-wrong-model",
        )


@pytest.mark.asyncio
async def test_local_executor_accepts_only_registered_recipe_fields(tmp_path, operation_db):
    artifacts = ArtifactStore(tmp_path / "spool", session_factory=operation_db)
    source = artifacts.put(owner="alice", chunks=[b"input"], media_type="image/png")
    broker = LocalExecutorBroker(artifacts)

    def handler(operation, inputs, options):
        assert operation == "image.upscale"
        assert inputs[0].read() == b"input"
        assert options == {"scale": 2}
        return ExecutorOutput(data=b"output", media_type="image/png")

    broker.register(
        executor_id="upscaler-v1",
        model_id="local/upscaler-v1",
        operations={"image.upscale"},
        handler=handler,
    )
    output = await broker.invoke(
        owner="alice",
        executor_id="upscaler-v1",
        operation="image.upscale",
        artifact_ids=[source.id],
        options={"scale": 2},
    )
    assert b"".join(artifacts.read_chunks(owner="alice", artifact_id=output.id)) == b"output"

    with pytest.raises(LocalExecutorError, match="not permitted"):
        await broker.invoke(
            owner="alice",
            executor_id="upscaler-v1",
            operation="image.upscale",
            artifact_ids=[source.id],
            options={"url": "https://example.test/model"},
        )

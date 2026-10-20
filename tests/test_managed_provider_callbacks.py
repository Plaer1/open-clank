from __future__ import annotations

import base64
from datetime import datetime
import hashlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.operation_models import OperationBase
from core.provider_models import ProviderBase
from src import secret_storage
from src.openclank.artifacts import ArtifactStore
from src.openclank.local_executor import (
    ExecutorOutput,
    LocalExecutorBroker,
)
from src.openclank.operation_journal import OperationJournalStore
from src.openclank.provider_callbacks import (
    MANAGED_CALLBACK_METHODS,
    ManagedProviderCallbackError,
    ManagedProviderCallbacks,
    ManagedProviderRouteError,
)
from src.openclank.generated.managed_provider_contract import SESSION_METHODS
from src.openclank.provider_store import ProviderStore


@pytest.fixture()
def managed_callbacks(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(bind=engine)
    OperationBase.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    store = ProviderStore(factory, clock=lambda: datetime(2026, 8, 9, 12, 0, 0))
    connection = store.create_connection(
        owner="alice",
        connection_id="openai-api",
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label="OpenAI API",
    )
    account = store.create_account(
        owner="alice",
        connection_id=connection.id,
        account_id="account-a",
        label="Account A",
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api", "key": "secret-a"},
    )
    route = store.create_model_route(
        owner="alice",
        connection_id=connection.id,
        model_route_id="route-gpt-test",
        provider_model_id="gpt-test",
        display_name="GPT Test",
    )
    store.set_entitlement(
        owner="alice",
        account_id=account.id,
        model_route_id=route.id,
        eligible=True,
    )
    callbacks = ManagedProviderCallbacks(
        owner="Alice",
        holder_id="worker-generation-1",
        store=store,
        session_factory=factory,
        operation_journal=OperationJournalStore(factory),
        artifact_store=ArtifactStore(tmp_path / "model-artifacts", session_factory=factory),
    )
    yield callbacks, store, factory, callbacks._artifact_store
    engine.dispose()


@pytest.mark.asyncio
async def test_owner_bound_callbacks_bind_lease_and_refresh_with_cas(managed_callbacks):
    callbacks, store, _factory, _artifacts = managed_callbacks
    context = await callbacks.resolve_route_context("openai-api/gpt-test")
    assert context.connection_id == "openai-api"
    assert context.provider_id == "openai"

    binding = await callbacks.dispatch(
        "_openclank/provider-store/v1/account/bind",
        {
            "rootOperationID": "root-1",
            "connectionID": context.connection_id,
            "providerID": context.provider_id,
            "billingLane": context.billing_lane,
            "modelID": context.model_id,
            "inheritedAccountID": "account-a",
        },
    )
    assert binding["accountID"] == "account-a"

    leased = await callbacks.dispatch(
        "_openclank/provider-store/v1/credential/lease",
        {
            "rootOperationID": "root-1",
            "connectionID": "openai-api",
            "accountID": "account-a",
            "modelID": "gpt-test",
            "expectedCredentialRevision": 1,
        },
    )
    assert leased["credential"] == {"type": "api", "key": "secret-a"}

    refresh = await callbacks.dispatch(
        "_openclank/provider-store/v1/refresh/acquire",
        {
            "connectionID": "openai-api",
            "accountID": "account-a",
            "expectedRevision": 1,
        },
    )
    committed = await callbacks.dispatch(
        "_openclank/provider-store/v1/refresh/commit",
        {
            "leaseID": refresh["leaseID"],
            "connectionID": "openai-api",
            "accountID": "account-a",
            "expectedRevision": 1,
            "credential": {"type": "api", "key": "secret-b"},
        },
    )
    assert committed["credentialRevision"] == 2
    assert store.credential_access(owner="alice", account_id="account-a").credentials == {
        "type": "api",
        "key": "secret-b",
    }


@pytest.mark.asyncio
async def test_callback_schema_rejects_child_owner_and_never_echoes_secret(managed_callbacks):
    callbacks, _store, _factory, _artifacts = managed_callbacks
    secret = "must-not-appear-in-errors"
    with pytest.raises(ManagedProviderCallbackError) as caught:
        await callbacks.dispatch(
            "_openclank/provider-store/v1/credential/replace",
            {
                "owner": "mallory",
                "connectionID": "openai-api",
                "accountID": "account-a",
                "expectedRevision": 1,
                "credential": {"type": "api", "key": secret},
            },
        )
    assert secret not in str(caught.value)
    assert "mallory" not in str(caught.value)


@pytest.mark.asyncio
async def test_route_resolution_fails_closed_when_family_is_ambiguous(managed_callbacks):
    callbacks, store, _factory, _artifacts = managed_callbacks
    second = store.create_connection(
        owner="alice",
        connection_id="openai-proxy",
        family_id="openai",
        adapter_id="openai-compatible",
        kind="proxy",
        billing_lane="custom",
        label="Proxy",
    )
    store.create_model_route(
        owner="alice",
        connection_id=second.id,
        provider_model_id="gpt-test",
        display_name="GPT Test",
    )
    with pytest.raises(ManagedProviderRouteError, match="one enabled connection"):
        await callbacks.resolve_route_context("openai/gpt-test")
    exact = await callbacks.resolve_route_context("openai-proxy/gpt-test")
    assert exact.connection_id == "openai-proxy"


@pytest.mark.asyncio
async def test_route_wire_carries_real_share_revision_and_omits_direct_owner_revision(managed_callbacks):
    callbacks, store, factory, artifacts = managed_callbacks
    grant = store.create_share_grant(
        owner="alice",
        recipient="bob",
        connection_id="openai-api",
        account_selector={"mode": "all_live_accounts"},
        model_selector={"mode": "explicit_models", "model_route_ids": ["route-gpt-test"]},
        grant_id="grant-revision-test",
    )
    recipient = ManagedProviderCallbacks(
        owner="bob",
        holder_id="worker-generation-bob",
        store=store,
        session_factory=factory,
        operation_journal=OperationJournalStore(factory),
        artifact_store=artifacts,
    )
    shared = await recipient.resolve_route_context(
        "openai-api/gpt-test", grant_id=grant.id
    )
    assert shared.grant_revision == grant.revision == 1
    assert shared.to_wire(root_operation_id="root-grant")["grantRevision"] == 1

    direct = await callbacks.resolve_route_context("openai-api/gpt-test")
    assert direct.grant_id is None
    assert direct.grant_revision is None
    assert "grantRevision" not in direct.to_wire(root_operation_id="root-direct")


@pytest.mark.asyncio
async def test_keyless_local_connection_binds_without_an_account_or_credential_lease(
    managed_callbacks,
):
    callbacks, store, _factory, _artifacts = managed_callbacks
    connection = store.create_connection(
        owner="alice",
        connection_id="ollama-local",
        family_id="ollama",
        adapter_id="ollama",
        kind="local_server",
        billing_lane="local",
        label="Local Ollama",
        normalized_url="http://127.0.0.1:11434",
    )
    store.create_model_route(
        owner="alice",
        connection_id=connection.id,
        model_route_id="route-qwen-local",
        provider_model_id="qwen-local",
        display_name="Qwen Local",
    )

    binding = await callbacks.dispatch(
        "_openclank/provider-store/v1/account/bind",
        {
            "rootOperationID": "root-local-1",
            "connectionID": connection.id,
            "providerID": "ollama",
            "billingLane": "local",
            "modelID": "qwen-local",
        },
    )
    assert binding["credentialRequired"] is False
    assert binding["source"] == "keyless"
    assert "accountID" not in binding
    assert "credentialRevision" not in binding

    sticky = await callbacks.dispatch(
        "_openclank/provider-store/v1/account/bind",
        {
            "rootOperationID": "root-local-1",
            "connectionID": connection.id,
            "providerID": "ollama",
            "billingLane": "local",
            "modelID": "qwen-local",
        },
    )
    assert sticky["bindingID"] == binding["bindingID"]


@pytest.mark.asyncio
async def test_nonlocal_connection_never_becomes_keyless(managed_callbacks):
    callbacks, store, _factory, _artifacts = managed_callbacks
    connection = store.create_connection(
        owner="alice",
        connection_id="uncredentialed-proxy",
        family_id="compatible",
        adapter_id="openai-compatible",
        kind="custom_gateway",
        billing_lane="custom",
        label="Uncredentialed Proxy",
    )
    store.create_model_route(
        owner="alice",
        connection_id=connection.id,
        provider_model_id="proxy-model",
        display_name="Proxy Model",
    )
    with pytest.raises(ManagedProviderCallbackError, match="no eligible account"):
        await callbacks.dispatch(
            "_openclank/provider-store/v1/account/bind",
            {
                "rootOperationID": "root-proxy-1",
                "connectionID": connection.id,
                "providerID": "compatible",
                "billingLane": "custom",
                "modelID": "proxy-model",
            },
        )


def test_register_exposes_only_pinned_managed_callbacks(managed_callbacks):
    callbacks, _store, _factory, _artifacts = managed_callbacks

    class FakeClient:
        def __init__(self):
            self.handlers = {}

        def register_callback(self, method, handler):
            self.handlers[method] = handler

    client = FakeClient()
    callbacks.register(client)
    assert tuple(client.handlers) == MANAGED_CALLBACK_METHODS


@pytest.mark.asyncio
async def test_operation_journal_begin_replay_and_cas_are_owner_bound(
    managed_callbacks,
):
    callbacks, _store, factory, artifacts = managed_callbacks
    request = {
        "action": "begin",
        "rootOperationID": "root-image-1",
        "operation": "image.generate",
        "idempotencyKey": "idempotency-key-image-0001",
        "request": {"promptHash": "a" * 64},
        "connectionID": "openai-api",
        "billingLane": "metered_api",
        "modelRouteID": "route-gpt-test",
    }
    begun = await callbacks.dispatch(
        "_openclank/operations/v1/journal/cas",
        request,
    )
    assert begun["state"] == "pending"
    assert begun["replayed"] is False
    replay = await callbacks.dispatch(
        "_openclank/operations/v1/journal/cas",
        request,
    )
    assert replay["operationID"] == begun["operationID"]
    assert replay["replayed"] is True

    updated = await callbacks.dispatch(
        "_openclank/operations/v1/journal/cas",
        {
            "action": "cas",
            "operationID": begun["operationID"],
            "expectedRevision": begun["revision"],
            "state": "running",
            "selectedAccountID": "account-a",
            "attempt": {"accountSlot": "A", "outcome": "started"},
        },
    )
    assert updated["selectedAccountID"] == "account-a"
    assert updated["attempts"] == [{"accountSlot": "A", "outcome": "started"}]
    assert updated["revision"] == begun["revision"] + 1

    other = ManagedProviderCallbacks(
        owner="bob",
        holder_id="worker-generation-bob",
        store=ProviderStore(factory),
        session_factory=factory,
        operation_journal=OperationJournalStore(factory),
        artifact_store=artifacts,
    )
    with pytest.raises(ManagedProviderCallbackError, match="not found"):
        await other.dispatch(
            "_openclank/operations/v1/journal/cas",
            {
                "action": "cas",
                "operationID": begun["operationID"],
                "expectedRevision": updated["revision"],
                "state": "complete",
            },
        )


@pytest.mark.asyncio
async def test_artifact_callbacks_hash_chunks_and_enforce_owner(managed_callbacks):
    callbacks, _store, factory, artifacts = managed_callbacks
    pieces = [b"managed ", b"artifact"]
    payload = b"".join(pieces)
    written = await callbacks.dispatch(
        "_openclank/operations/v1/artifact/write",
        {
            "action": "put",
            "mediaType": "text/plain",
            "contentSHA256": hashlib.sha256(payload).hexdigest(),
            "sizeBytes": len(payload),
            "chunks": [
                {
                    "index": index,
                    "dataBase64": base64.b64encode(piece).decode("ascii"),
                    "sha256": hashlib.sha256(piece).hexdigest(),
                }
                for index, piece in enumerate(pieces)
            ],
        },
    )
    assert written["contentSHA256"] == hashlib.sha256(payload).hexdigest()
    assert written["state"] == "staged"

    partial = await callbacks.dispatch(
        "_openclank/operations/v1/artifact/read",
        {"artifactID": written["artifactID"], "offset": 3, "limit": 7},
    )
    selected = payload[3:10]
    assert base64.b64decode(partial["dataBase64"]) == selected
    assert partial["chunkSHA256"] == hashlib.sha256(selected).hexdigest()
    assert partial["eof"] is False

    acknowledged = await callbacks.dispatch(
        "_openclank/operations/v1/artifact/write",
        {"action": "acknowledge", "artifactID": written["artifactID"]},
    )
    assert acknowledged["state"] == "acknowledged"

    other = ManagedProviderCallbacks(
        owner="bob",
        holder_id="worker-generation-bob",
        store=ProviderStore(factory),
        session_factory=factory,
        operation_journal=OperationJournalStore(factory),
        artifact_store=artifacts,
    )
    with pytest.raises(ManagedProviderCallbackError, match="not found"):
        await other.dispatch(
            "_openclank/operations/v1/artifact/read",
            {"artifactID": written["artifactID"], "offset": 0, "limit": 32},
        )

    with pytest.raises(ManagedProviderCallbackError, match="content hash"):
        await callbacks.dispatch(
            "_openclank/operations/v1/artifact/write",
            {
                "action": "put",
                "mediaType": "text/plain",
                "contentSHA256": hashlib.sha256(b"wrong").hexdigest(),
                "sizeBytes": len(payload),
                "chunks": [
                    {
                        "index": 0,
                        "dataBase64": base64.b64encode(payload).decode("ascii"),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            },
        )


@pytest.mark.asyncio
async def test_local_executor_callback_is_injected_or_fails_closed(managed_callbacks):
    callbacks, store, factory, artifacts = managed_callbacks
    connection = store.create_connection(
        owner="alice",
        connection_id="local-executors",
        family_id="openclank-local",
        adapter_id="host-executor",
        kind="local_executor",
        billing_lane="local",
        label="Local Executors",
    )
    store.create_model_route(
        owner="alice",
        connection_id=connection.id,
        model_route_id="route-local-upscaler",
        provider_model_id="local-upscaler-v1",
        display_name="Local Upscaler",
        operations=["image.upscale"],
    )
    binding = await callbacks.dispatch(
        "_openclank/provider-store/v1/account/bind",
        {
            "rootOperationID": "root-local-upscale",
            "connectionID": connection.id,
            "providerID": "openclank-local",
            "billingLane": "local",
            "modelID": "local-upscaler-v1",
        },
    )
    assert binding["credentialRequired"] is False
    assert binding["source"] == "keyless"

    method = "_openclank/operations/v1/executor/invoke"
    request = {
        "executorID": "registered-upscaler",
        "operation": "image.upscale",
        "artifactIDs": [],
        "options": {"scale": 2},
    }
    with pytest.raises(ManagedProviderCallbackError, match="unavailable"):
        await callbacks.dispatch(method, request)

    broker = LocalExecutorBroker(artifacts)
    broker.register(
        executor_id="registered-upscaler",
        model_id="local-upscaler-v1",
        operations={"image.upscale"},
        handler=lambda operation, inputs, options: ExecutorOutput(
            data=f"{operation}:{options['scale']}:{len(inputs)}".encode(),
            media_type="text/plain",
        ),
    )
    injected = ManagedProviderCallbacks(
        owner="alice",
        holder_id="worker-generation-2",
        store=store,
        session_factory=factory,
        operation_journal=OperationJournalStore(factory),
        artifact_store=artifacts,
        executor_broker=broker,
    )
    result = await injected.dispatch(method, request)
    assert result["state"] == "staged"
    assert b"".join(
        artifacts.read_chunks(owner="alice", artifact_id=result["artifactID"])
    ) == b"image.upscale:2:0"


def test_managed_supervisor_does_not_seed_auth_file_or_provider_secret_fd(monkeypatch):
    import inspect

    from src.openclank.mimo_supervisor import MimoSupervisor, _mimo_child_environment

    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MIMOCODE_AUTH_CONTENT",
        "MIMOCODE_PROVIDER_AUTH_FD",
    ):
        monkeypatch.setenv(name, "must-not-cross-worker-boundary")
    spawn_source = inspect.getsource(MimoSupervisor._spawn_and_init)
    assert "MIMOCODE_PROVIDER_AUTH_FD" not in spawn_source
    assert "_reconcile_auth_store()" not in spawn_source
    assert "ManagedProviderCallbacks(" in spawn_source
    assert "artifact_store=artifact_store" in spawn_source
    assert "executor_broker=self._local_executor_broker" in spawn_source
    assert "self._managed_callbacks.register(self._client)" in spawn_source
    child_env = _mimo_child_environment()
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MIMOCODE_AUTH_CONTENT",
        "MIMOCODE_PROVIDER_AUTH_FD",
    ):
        assert name not in child_env


def test_session_cwd_is_a_separate_engine_to_host_callback_family(managed_callbacks):
    callbacks, _store, _factory, _artifacts = managed_callbacks

    class Client:
        def __init__(self):
            self.methods = []

        def register_callback(self, method, handler):
            self.methods.append((method, handler))

    client = Client()
    callbacks.register(client)
    names = {method for method, _handler in client.methods}
    assert "_openclank/session/v1/cwd/change" in SESSION_METHODS
    assert "_openclank/session/v1/cwd/change" not in names
    assert len(names) == len(client.methods)


@pytest.mark.asyncio
async def test_callback_journal_terminal_winner_is_first_writer(managed_callbacks):
    callbacks, _store, _factory, _artifacts = managed_callbacks
    begin = await callbacks.dispatch(
        "_openclank/operations/v1/journal/cas",
        {
            "action": "begin", "rootOperationID": "callback-race",
            "operation": "web.search", "idempotencyKey": "callback-race-key-0001",
            "request": {"operation": "web.search", "input": {"query": "x"}, "routes": [], "artifactInputs": [], "options": {}},
            "connectionID": "openai-api", "billingLane": "metered_api", "modelRouteID": "route-gpt-test",
        },
    )
    complete = await callbacks.dispatch(
        "_openclank/operations/v1/journal/cas",
        {"action": "cas", "operationID": begin["operationID"], "expectedRevision": begin["revision"], "state": "complete", "commitReason": "provider_result"},
    )
    assert complete["state"] == "complete" and complete["committed"] is True
    with pytest.raises(ManagedProviderCallbackError):
        await callbacks.dispatch(
            "_openclank/operations/v1/journal/cas",
            {"action": "cas", "operationID": begin["operationID"], "expectedRevision": complete["revision"], "state": "cancelled"},
        )
    replay = await callbacks.dispatch(
        "_openclank/operations/v1/journal/cas",
        {
            "action": "begin", "rootOperationID": "callback-race",
            "operation": "web.search", "idempotencyKey": "callback-race-key-0001",
            "request": {"operation": "web.search", "input": {"query": "x"}, "routes": [], "artifactInputs": [], "options": {}},
            "connectionID": "openai-api", "billingLane": "metered_api", "modelRouteID": "route-gpt-test",
        },
    )
    assert replay["replayed"] is True and replay["state"] == "complete" and replay["revision"] == complete["revision"]

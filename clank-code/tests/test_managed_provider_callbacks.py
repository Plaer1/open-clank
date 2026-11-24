from __future__ import annotations

import asyncio
import base64
import copy
from datetime import datetime
import hashlib
import json
import time

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
from src.openclank.mimo_supervisor import _managed_search_delegate
from src.openclank.operation_router import ManagedOperationRouter


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


def _admit_auxiliary(callbacks, *, root, connection, route, operation="chat.complete"):
    row, _ = callbacks._operation_journal.begin(
        owner=callbacks.owner,
        root_operation_id=root,
        operation=operation,
        idempotency_key=f"test-operation-{root}",
        request={"kind": "test", "root": root},
        connection_id=connection.id,
        billing_lane=connection.billing_lane,
        model_route_id=route.id,
    )
    return row.id


@pytest.mark.asyncio
async def test_private_preflight_binding_rejects_changed_generation(managed_callbacks, monkeypatch):
    callbacks, _store, _factory, _artifacts = managed_callbacks
    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", inline)
    generation = {"value": "a" * 64}
    callbacks.set_actor_execution_context_resolver(lambda **identity: {
        "owner": "alice", "holderID": callbacks.holder_id, "stableChatID": "ghost",
        "rootSessionID": "native", "rootOperationID": identity["root_operation_id"],
        "sessionID": identity["session_id"], "actorID": identity["actor_id"], "parentActorID": None,
        "executionRevision": identity["execution_revision"], "lineage": [],
        "effectiveModel": {"providerID": "openai-api", "modelID": "gpt-test"},
        "authority": {"kind": "private", "generation": generation["value"]},
    })
    preflight = await callbacks.dispatch("_openclank/provider-store/v1/actor-route/preflight", {
        "sessionID": "native", "rootOperationID": "root-private", "actorID": "main", "expectedExecutionRevision": 0,
        "model": {"providerID": "openai-api", "modelID": "gpt-test"}, "modelSource": "inherited"})
    bind = {"rootOperationID": "root-private", "scopeKind": "actor_execution", "sessionID": "native", "actorID": "main",
        "executionRevision": 1, "bootstrap": preflight["bootstrap"], "modelRouteID": "route-gpt-test", "connectionID": "openai-api",
        "providerID": "openai", "billingLane": "metered_api", "modelID": "gpt-test"}
    generation["value"] = "b" * 64
    with pytest.raises(ManagedProviderCallbackError, match="bootstrap authority changed"):
        await callbacks.dispatch("_openclank/provider-store/v1/account/bind", bind)
    generation["value"] = "a" * 64
    selected = await callbacks.dispatch("_openclank/provider-store/v1/account/bind", bind)
    generation["value"] = "b" * 64
    with pytest.raises(ManagedProviderCallbackError, match="private authority changed"):
        await callbacks.dispatch("_openclank/provider-store/v1/credential/lease", {"bindingID": selected["bindingID"],
            "expectedBindingRevision": selected["bindingRevision"], "expectedCredentialRevision": 1})


@pytest.mark.asyncio
async def test_owner_bound_callbacks_bind_lease_and_refresh_with_cas(managed_callbacks):
    callbacks, store, _factory, _artifacts = managed_callbacks
    context = await callbacks.resolve_route_context("openai-api/gpt-test")
    assert context.connection_id == "openai-api"
    assert context.provider_id == "openai"
    callbacks.set_actor_execution_context_resolver(
        lambda **identity: {
            "owner": "alice",
            "holderID": callbacks.holder_id,
            "stableChatID": "host-chat",
            "rootSessionID": "engine-session",
            "mapRevision": 1,
            "mappingRevision": 1,
            "workspaceRevision": 1,
            "rootOperationID": identity["root_operation_id"],
            "sessionID": identity["session_id"],
            "actorID": identity["actor_id"],
            "parentActorID": None,
            "effectiveModel": {"providerID": "openai-api", "modelID": "gpt-test"},
            "executionRevision": identity["execution_revision"],
            "lineage": [],
        }
    )

    preflight = await callbacks.dispatch(
        "_openclank/provider-store/v1/actor-route/preflight",
        {"sessionID": "engine-session", "rootOperationID": "root-1", "actorID": "main",
         "expectedExecutionRevision": 0, "model": {"providerID": "openai-api", "modelID": "gpt-test"},
         "modelSource": "inherited"},
    )
    assert preflight["bootstrap"]["modelRouteID"] == "route-gpt-test"
    binding = await callbacks.dispatch(
        "_openclank/provider-store/v1/account/bind",
        {
            "rootOperationID": "root-1",
            "scopeKind": "actor_execution",
            "sessionID": "engine-session",
            "actorID": "main",
            "executionRevision": 1,
            "bootstrap": preflight["bootstrap"],
            "modelRouteID": "route-gpt-test",
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
            "bindingID": binding["bindingID"],
            "expectedBindingRevision": binding["bindingRevision"],
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
            "scopeKind": "auxiliary_operation",
            "operationID": _admit_auxiliary(
                callbacks, root="root-local-1", connection=connection,
                route=store.get_model_route(owner="alice", model_route_id="route-qwen-local"),
            ),
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
            "scopeKind": "auxiliary_operation",
            "operationID": _admit_auxiliary(
                callbacks, root="root-local-1", connection=connection,
                route=store.get_model_route(owner="alice", model_route_id="route-qwen-local"),
            ),
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
                "scopeKind": "auxiliary_operation",
                "operationID": _admit_auxiliary(
                    callbacks, root="root-proxy-1", connection=connection,
                    route=store.get_model_route(owner="alice", model_route_id="proxy-model"),
                ),
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
            "scopeKind": "auxiliary_operation",
            "operationID": _admit_auxiliary(
                callbacks, root="root-local-upscale", connection=connection,
                route=store.get_model_route(owner="alice", model_route_id="route-local-upscaler"),
                operation="image.upscale",
            ),
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
        "OPENCLANK_AUTH_CONTENT",
        "OPENCLANK_PROVIDER_AUTH_FD",
    ):
        monkeypatch.setenv(name, "must-not-cross-worker-boundary")
    spawn_source = inspect.getsource(MimoSupervisor._spawn_and_init)
    assert "OPENCLANK_PROVIDER_AUTH_FD" not in spawn_source
    assert "_reconcile_auth_store()" not in spawn_source
    assert "ManagedProviderCallbacks(" in spawn_source
    assert "artifact_store=artifact_store" in spawn_source
    assert "executor_broker=self._local_executor_broker" in spawn_source
    assert "self._managed_callbacks.register(self._client)" in spawn_source
    child_env = _mimo_child_environment()
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENCLANK_AUTH_CONTENT",
        "OPENCLANK_PROVIDER_AUTH_FD",
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


def _search_envelope(action="dispatch", **extra):
    options = {
        "executorID": "openclank.search.v1",
        "contractVersion": 1,
        "action": action,
        "claims": {
            "sessionID": "engine-a",
            "correlationID": "tool-search-1",
            "rootOperationID": "parent-root-1",
            "idempotencyKey": "search-idempotency-1",
        },
    }
    if action == "dispatch":
        options["input"] = {
            "query": "open clank",
            "count": 5,
            "intent": "web",
            "extraction": "text",
            "deadlineAt": (time.time() + 30) * 1000,
        }
    options.update(extra)
    return {
        "executorID": "openclank.search.v1",
        "operation": "web.search",
        "artifactIDs": [],
        "options": options,
    }


@pytest.mark.asyncio
async def test_search_callback_binds_context_and_replays_terminal(managed_callbacks):
    callbacks, _store, _factory, _artifacts = managed_callbacks
    callbacks.set_search_context_resolver(
        lambda session, root: {
            "owner": "alice", "holderID": "worker-generation-1",
            "sessionID": session, "rootOperationID": root, "caller": "chat",
        }
    )
    calls = []

    async def delegate(**kwargs):
        calls.append(kwargs)
        if kwargs["action"] == "dispatch":
            return {
                "kind": "managed_request",
                "prepared": {"opaque": "host-only"},
                "preparedRequest": {"rootOperationID": kwargs["child_root_operation_id"],
                    "idempotencyKey": kwargs["child_idempotency_key"], "operation": "web.search",
                    "routes": [{"connectionID": "openai-api", "providerID": "openai", "billingLane": "metered_api",
                                "modelRouteID": "route-gpt-test", "modelID": "gpt-test"}],
                    "input": {"query": "open clank", "count": 5},
                    "artifactInputs": [], "options": {"deadlineMs": 30000}},
            }
        return {"kind": "finalized", "terminal": kwargs["terminal"]}

    callbacks.set_search_delegate(delegate)
    dispatched = await callbacks.dispatch(
        "_openclank/operations/v1/executor/invoke", _search_envelope()
    )
    assert dispatched["mediaType"] == "application/json"
    payload = json.loads(b"".join(callbacks._artifact_store.read_chunks(owner="alice", artifact_id=dispatched["artifactID"])))
    assert payload["kind"] == "managed_request" and "request" in payload and "preparedRequest" not in payload
    assert len(calls) == 1 and calls[0]["child_root_operation_id"] != "parent-root-1"
    handoff_id = next(iter(callbacks._search_handoffs))
    terminal = {
        "operationID": "search-operation-1", "rootOperationID": calls[0]["child_root_operation_id"],
        "operation": "web.search", "modelRouteID": "route-gpt-test", "connectionID": "openai-api",
        "billingLane": "metered_api", "state": "complete", "committed": True,
        "output": {"results": []}, "artifacts": [], "replayed": False,
    }
    finalized = await callbacks.dispatch(
        "_openclank/operations/v1/executor/invoke",
        _search_envelope("finalize", handoffID=handoff_id, terminal=terminal),
    )
    replayed = await callbacks.dispatch(
        "_openclank/operations/v1/executor/invoke",
        _search_envelope("finalize", handoffID=handoff_id, terminal=terminal),
    )
    assert finalized["mediaType"] == replayed["mediaType"] == "application/json"
    assert len(calls) == 2
    conflicting = {**terminal, "operationID": "different-operation"}
    with pytest.raises(ManagedProviderCallbackError, match="conflicting"):
        await callbacks.dispatch(
            "_openclank/operations/v1/executor/invoke",
            _search_envelope("finalize", handoffID=handoff_id, terminal=conflicting),
        )


@pytest.mark.asyncio
async def test_search_callback_rejects_foreign_generation_and_conflicting_terminal(managed_callbacks):
    callbacks, _store, _factory, _artifacts = managed_callbacks
    callbacks.set_search_context_resolver(lambda _session, _root: {
        "owner": "alice", "holderID": "stale-generation", "sessionID": "engine-a",
        "rootOperationID": "parent-root-1", "caller": "chat",
    })
    callbacks.set_search_delegate(lambda **_kwargs: {"kind": "external_result", "response": {"status": "complete"}})
    with pytest.raises(ManagedProviderCallbackError, match="generation"):
        await callbacks.dispatch("_openclank/operations/v1/executor/invoke", _search_envelope())


@pytest.mark.asyncio
async def test_search_callback_expired_deadline_does_not_dispatch(managed_callbacks):
    callbacks, _store, _factory, _artifacts = managed_callbacks
    callbacks.set_search_context_resolver(lambda session, root: {
        "owner": "alice", "holderID": "worker-generation-1", "sessionID": session,
        "rootOperationID": root, "caller": "chat",
    })
    calls = []
    callbacks.set_search_delegate(lambda **kwargs: calls.append(kwargs))
    request = _search_envelope()
    request["options"]["input"]["deadlineAt"] = (time.time() - 1) * 1000
    with pytest.raises(ManagedProviderCallbackError, match="deadline"):
        await callbacks.dispatch("_openclank/operations/v1/executor/invoke", request)
    assert calls == []


@pytest.mark.asyncio
async def test_search_callback_duplicate_dispatch_reserves_once(managed_callbacks):
    callbacks, _store, _factory, _artifacts = managed_callbacks
    callbacks.set_search_context_resolver(lambda session, root: {
        "owner": "alice", "holderID": "worker-generation-1", "sessionID": session,
        "rootOperationID": root, "caller": "chat",
    })
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def delegate(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return {"kind": "external_result", "response": {
            "schema_version": 1, "query": "open clank", "caller": "chat", "mode": "results",
            "selected_provider": "brave", "actual_provider": "brave", "account_scope": "deployment-service",
            "account_id": "", "status": "complete", "sources": [{"url": "https://example.test/a", "title": "A", "snippet": "s"}],
            "total": 1, "cache": {"hit": False}, "elapsed_ms": 1, "warnings": [],
            "search_queries": [], "support_links": [], "web_search_usage": {},
        }}

    callbacks.set_search_delegate(delegate)
    original = _search_envelope()
    first = asyncio.create_task(callbacks.dispatch("_openclank/operations/v1/executor/invoke", original))
    await started.wait()
    duplicate = copy.deepcopy(original)
    # Retry carries the exact original normalized input, including deadline.
    second = asyncio.create_task(callbacks.dispatch("_openclank/operations/v1/executor/invoke", duplicate))
    await asyncio.sleep(0)
    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert len(calls) == 1
    assert first_result["artifactID"] == second_result["artifactID"]
    payload = json.loads(b"".join(callbacks._artifact_store.read_chunks(owner="alice", artifact_id=first_result["artifactID"])))
    assert payload["kind"] == "external_result"
    assert payload["response"]["sources"][0]["url"] == "https://example.test/a"


@pytest.mark.asyncio
async def test_search_callback_cancel_receipt_cannot_be_overwritten(managed_callbacks):
    callbacks, _store, _factory, _artifacts = managed_callbacks
    callbacks.set_search_context_resolver(lambda session, root: {
        "owner": "alice", "holderID": "worker-generation-1", "sessionID": session,
        "rootOperationID": root, "caller": "chat",
    })

    async def delegate(**kwargs):
        if kwargs["action"] == "dispatch":
            return {"kind": "managed_request", "prepared": {"opaque": True}, "preparedRequest": {
                "rootOperationID": kwargs["child_root_operation_id"], "idempotencyKey": kwargs["child_idempotency_key"],
                "operation": "web.search", "routes": [{"connectionID": "openai-api", "providerID": "openai", "billingLane": "metered_api", "modelRouteID": "route-gpt-test", "modelID": "gpt-test"}],
                "input": {"query": "open clank", "count": 5}, "artifactInputs": [], "options": {"deadlineMs": 30000},
            }}
        if kwargs["action"] == "cancel":
            return {"kind": "cancelled", "reason": "aborted"}
        raise AssertionError("finalize must not run after cancellation")

    callbacks.set_search_delegate(delegate)
    await callbacks.dispatch("_openclank/operations/v1/executor/invoke", _search_envelope())
    handoff_id = next(iter(callbacks._search_handoffs))
    cancel = await callbacks.dispatch("_openclank/operations/v1/executor/invoke", _search_envelope("cancel", handoffID=handoff_id, reason="aborted"))
    duplicate = await callbacks.dispatch("_openclank/operations/v1/executor/invoke", _search_envelope("cancel", handoffID=handoff_id, reason="aborted"))
    assert cancel["artifactID"] == duplicate["artifactID"]
    terminal = {
        "operationID": "search-operation-cancel-late", "rootOperationID": "search-child", "operation": "web.search",
        "modelRouteID": "route-gpt-test", "connectionID": "openai-api", "billingLane": "metered_api",
        "state": "complete", "committed": True, "output": {"results": []}, "artifacts": [], "replayed": False,
    }
    with pytest.raises(ManagedProviderCallbackError, match="conflicting"):
        await callbacks.dispatch("_openclank/operations/v1/executor/invoke", _search_envelope("finalize", handoffID=handoff_id, terminal=terminal))


@pytest.mark.asyncio
async def test_search_callback_delegate_failure_is_tombstoned(managed_callbacks):
    callbacks, _store, _factory, _artifacts = managed_callbacks
    callbacks.set_search_context_resolver(lambda session, root: {
        "owner": "alice", "holderID": "worker-generation-1", "sessionID": session,
        "rootOperationID": root, "caller": "chat",
    })
    calls = []

    async def delegate(**_kwargs):
        calls.append(True)
        raise RuntimeError("synthetic host failure")

    callbacks.set_search_delegate(delegate)
    request = _search_envelope()
    with pytest.raises(ManagedProviderCallbackError):
        await callbacks.dispatch("_openclank/operations/v1/executor/invoke", request)
    with pytest.raises(ManagedProviderCallbackError):
        await callbacks.dispatch("_openclank/operations/v1/executor/invoke", copy.deepcopy(request))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_search_callback_uses_real_service_for_external_and_managed_routes(
    managed_callbacks, monkeypatch
):
    callbacks, store, factory, artifacts = managed_callbacks
    callbacks.set_search_context_resolver(lambda session, root: {
        "owner": "alice", "holderID": "worker-generation-1", "sessionID": session,
        "rootOperationID": root, "caller": "chat",
    })
    from services.search import service as search_service

    monkeypatch.setattr(search_service, "_get_search_settings", lambda: {
        "search_provider": "brave", "brave_api_key": "fixture-key", "search_result_count": 5,
    })
    monkeypatch.setattr(search_service, "_call_provider", lambda *_args: [{
        "url": "https://example.test/brave", "title": "Brave fixture", "snippet": "ok",
    }])
    callbacks.set_search_delegate(_managed_search_delegate)
    external = await callbacks.dispatch(
        "_openclank/operations/v1/executor/invoke", _search_envelope()
    )
    external_payload = json.loads(b"".join(callbacks._artifact_store.read_chunks(
        owner="alice", artifact_id=external["artifactID"]
    )))
    assert external_payload["kind"] == "external_result"
    assert external_payload["response"]["sources"][0]["url"] == "https://example.test/brave"

    connection = store.create_connection(
        owner="alice", connection_id="mimo-search", family_id="xiaomi",
        adapter_id="xiaomi-managed", kind="official", billing_lane="subscription",
        label="MiMo Search",
    )
    route = store.create_model_route(
        owner="alice", connection_id=connection.id, model_route_id="route-mimo-search",
        provider_model_id="mimo-v2.6-flash", display_name="MiMo Search",
        operations=("web.search",),
    )
    store.put_route_binding(owner="alice", purpose="search", model_route_ids=(route.id,), expected_revision=0)
    router = ManagedOperationRouter(session_factory=factory, artifact_store=artifacts)
    from src.openclank import operation_router
    monkeypatch.setattr(operation_router, "get_operation_router", lambda: router)
    managed_request = copy.deepcopy(_search_envelope())
    managed_request["options"]["claims"].update({
        "correlationID": "tool-search-mimo", "idempotencyKey": "search-idempotency-mimo",
    })
    managed_request["options"]["input"]["query"] = "managed MiMo"
    monkeypatch.setattr(search_service, "_get_search_settings", lambda: {
        "search_provider": "mimo", "search_result_count": 5,
    })
    managed = await callbacks.dispatch(
        "_openclank/operations/v1/executor/invoke", managed_request
    )
    managed_payload = json.loads(b"".join(callbacks._artifact_store.read_chunks(
        owner="alice", artifact_id=managed["artifactID"]
    )))
    assert managed_payload["kind"] == "managed_request"
    assert managed_payload["request"]["operation"] == "web.search"
    assert managed_payload["request"]["routes"][0]["modelRouteID"] == route.id


@pytest.mark.asyncio
async def test_search_callback_expiry_retires_identity_and_reuses_capacity(managed_callbacks):
    callbacks, _store, _factory, _artifacts = managed_callbacks
    callbacks._max_search_handoffs = 1
    callbacks.set_search_context_resolver(lambda session, root: {
        "owner": "alice", "holderID": "worker-generation-1", "sessionID": session,
        "rootOperationID": root, "caller": "chat",
    })
    calls = []

    async def delegate(**kwargs):
        calls.append(kwargs)
        return {"kind": "external_result", "response": {
            "schema_version": 1, "query": "open clank", "caller": "chat", "mode": "results",
            "selected_provider": "brave", "actual_provider": "brave", "account_scope": "deployment-service",
            "account_id": "", "status": "complete", "sources": [{"url": "https://example.test/a", "title": "A", "snippet": "s"}],
            "total": 1, "cache": {"hit": False}, "elapsed_ms": 1, "warnings": [],
            "search_queries": [], "support_links": [], "web_search_usage": {},
        }}

    callbacks.set_search_delegate(delegate)
    first = await callbacks.dispatch("_openclank/operations/v1/executor/invoke", _search_envelope())
    handoff_id = next(iter(callbacks._search_handoffs))
    callbacks._expire_search_handoff(handoff_id)
    with pytest.raises(ManagedProviderCallbackError, match="expired"):
        await callbacks.dispatch("_openclank/operations/v1/executor/invoke", _search_envelope())
    callbacks.retire_search_turn("engine-a", "parent-root-1")
    assert callbacks._search_dispatch_turns == {}
    second_request = _search_envelope()
    second_request["options"]["claims"].update({
        "correlationID": "tool-search-2", "idempotencyKey": "search-idempotency-2",
    })
    second = await callbacks.dispatch("_openclank/operations/v1/executor/invoke", second_request)
    assert first["artifactID"] and second["artifactID"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_search_callback_cancel_aborts_slow_external_delegate(managed_callbacks):
    callbacks, _store, _factory, _artifacts = managed_callbacks
    callbacks.set_search_context_resolver(lambda session, root: {
        "owner": "alice", "holderID": "worker-generation-1", "sessionID": session,
        "rootOperationID": root, "caller": "chat",
    })
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def delegate(**kwargs):
        if kwargs["action"] == "dispatch":
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return {"kind": "cancelled", "reason": "aborted"}

    callbacks.set_search_delegate(delegate)
    request = _search_envelope()
    dispatch = asyncio.create_task(callbacks.dispatch(
        "_openclank/operations/v1/executor/invoke", request
    ))
    await started.wait()
    cancel = await callbacks.dispatch(
        "_openclank/operations/v1/executor/invoke",
        _search_envelope("cancel", reason="aborted"),
    )
    completed = await dispatch
    assert cancelled.is_set()
    assert cancel["artifactID"] == completed["artifactID"]


@pytest.mark.asyncio
async def test_search_callback_outer_cancellation_aborts_delegate_without_hanging(managed_callbacks):
    callbacks, _store, _factory, _artifacts = managed_callbacks
    callbacks.set_search_context_resolver(lambda session, root: {
        "owner": "alice", "holderID": "worker-generation-1", "sessionID": session,
        "rootOperationID": root, "caller": "chat",
    })
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def delegate(**kwargs):
        if kwargs["action"] == "dispatch":
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        raise AssertionError("outer cancellation must not invoke host cancel")

    callbacks.set_search_delegate(delegate)
    dispatch = asyncio.create_task(callbacks.dispatch(
        "_openclank/operations/v1/executor/invoke", _search_envelope()
    ))
    await started.wait()
    dispatch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch
    assert cancelled.is_set()

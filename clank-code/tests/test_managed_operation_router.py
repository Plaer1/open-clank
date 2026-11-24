from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.operation_models import OperationBase
from core.provider_models import ProviderBase, ProviderConnection, ProviderOperationBinding
from src.openclank.artifacts import ArtifactStore
from src.openclank.operation_journal import OperationConflict, OperationJournalStore
from src.openclank.operation_router import (
    ManagedOperationDenied,
    ManagedOperationProtocolError,
    ManagedOperationRequest,
    ManagedOperationRouter,
)
from src.openclank.provider_store import ProviderStore


def test_complete_text_preserves_terminal_engine_error_code(monkeypatch):
    from src.openclank import modality_facade
    from src.openclank.operation_router import ManagedOperationResult

    async def failed_operation(**_kwargs):
        return ManagedOperationResult(
            operation_id="op_failed",
            root_operation_id="root_failed",
            operation="chat.complete",
            state="failed",
            committed=False,
            replayed=False,
            model_route_id="route-shared",
            connection_id="connection-shared",
            billing_lane="metered_api",
            output={"errorCode": "model_not_found"},
            artifacts=(),
        )

    monkeypatch.setattr(
        modality_facade,
        "execute_model_operation",
        failed_operation,
    )

    with pytest.raises(modality_facade.ManagedTextCompletionError) as exc:
        asyncio.run(
            modality_facade.complete_text(
                owner="alice",
                messages=({"role": "user", "content": "hello"},),
            )
        )

    assert exc.value.code == "model_not_found"
    assert exc.value.committed is False
    assert str(exc.value) == (
        "The selected model is not available in the managed runtime."
    )


@pytest.fixture()
def operation_environment(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'router.db'}")
    ProviderBase.metadata.create_all(engine)
    OperationBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    artifacts = ArtifactStore(tmp_path / "spool", session_factory=factory)
    store = ProviderStore(factory)
    yield store, factory, artifacts
    engine.dispose()


def _route(
    store: ProviderStore,
    *,
    connection_id: str,
    route_id: str,
    model_id: str,
    operation: str,
    lane: str = "local",
):
    store.create_connection(
        owner="alice",
        connection_id=connection_id,
        family_id="local",
        adapter_id="openclank-local-executor",
        kind="local_executor",
        billing_lane=lane,
        label=connection_id,
    )
    return store.create_model_route(
        owner="alice",
        connection_id=connection_id,
        model_route_id=route_id,
        provider_model_id=model_id,
        display_name=model_id,
        operations=(operation,),
    )


def test_recover_prepared_discovers_only_exact_unique_journal(operation_environment):
    from dataclasses import replace
    import json
    from src.openclank.operation_router import _thaw_json
    store, factory, artifacts = operation_environment
    _route(store,connection_id='conn-recover',route_id='route-recover',model_id='model-recover',operation='chat.complete')
    router = ManagedOperationRouter(session_factory=factory,artifact_store=artifacts)
    request = ManagedOperationRequest(owner='alice',operation='chat.complete',model_route_id='route-recover',
        root_operation_id='root-recover',idempotency_key='recover-operation-001',input={'messages':[{'role':'user','content':'x'}]})
    prepared = router.prepare(request)
    payload = _thaw_json(prepared.payload)
    journal_request = {key:payload[key] for key in ('operation','routes','input','artifactInputs','options')}
    row, _ = OperationJournalStore(factory).begin(owner='alice',root_operation_id=request.root_operation_id,
        operation=request.operation,idempotency_key=request.idempotency_key,request=journal_request,
        connection_id='conn-recover',billing_lane='local',model_route_id='route-recover')
    result = router.recover_prepared(prepared,authenticated_owner='alice')
    assert result == {'state':'unknown','operationID':row.id,'revision':row.revision}
    changed = router.prepare(replace(request,input={'messages':[{'role':'user','content':'changed'}]}))
    assert router.recover_prepared(changed,authenticated_owner='alice') == {'state':'unknown'}
    with pytest.raises(ManagedOperationProtocolError):
        router.recover_prepared(prepared,authenticated_owner='bob')
    receipt = {'operationID':row.id,'rootOperationID':request.root_operation_id,'operation':'chat.complete',
        'modelRouteID':'route-recover','connectionID':'conn-recover','billingLane':'local','state':'cancelled',
        'committed':False,'output':{'errorCode':'cancelled'},'artifacts':[],'replayed':False}
    artifact = artifacts.put(owner='alice',chunks=(json.dumps(receipt).encode(),),media_type='application/vnd.openclank.operation-result+json')
    artifacts.acknowledge(owner='alice',artifact_id=artifact.id)
    journal = OperationJournalStore(factory)
    row = journal.cas(owner='alice',operation_id=row.id,expected_revision=row.revision,state='cancelled',artifact_id=artifact.id)
    recovered = router.recover_prepared(prepared,authenticated_owner='alice')
    assert recovered['state'] == 'terminal' and recovered['operationID'] == row.id and recovered['result'].state == 'cancelled'


def test_route_preflight_exposes_only_safe_enabled_owner_choices(
    operation_environment,
):
    store, factory, artifacts = operation_environment
    _route(
        store,
        connection_id="connection-chat",
        route_id="route-chat",
        model_id="chat-a",
        operation="chat.complete",
    )
    _route(
        store,
        connection_id="connection-image",
        route_id="route-image",
        model_id="image-a",
        operation="image.generate",
    )
    _route(
        store,
        connection_id="connection-disabled",
        route_id="route-disabled",
        model_id="chat-disabled",
        operation="chat.complete",
    )
    with factory() as db:
        disabled = db.get(ProviderConnection, "connection-disabled")
        disabled.enabled = False
        db.commit()

    router = ManagedOperationRouter(
        session_factory=factory,
        artifact_store=artifacts,
        executor=None,
    )
    expected_choice = {
        "model_route_id": "route-chat",
        "display_name": "chat-a",
        "connection_label": "connection-chat",
    }
    assert router.route_preflight(
        owner="alice",
        purpose="memory",
        operation="chat.complete",
        ) == {
            "configured": False,
            "selected_model_route_id": None,
            "binding_revision": 0,
        "eligible_routes": [expected_choice],
    }

    store.put_route_binding(
        owner="alice",
        purpose="memory",
        model_route_ids=("route-chat",),
        expected_revision=0,
    )
    configured = router.route_preflight(
        owner="alice",
        purpose="memory",
        operation="chat.complete",
    )
    assert configured == {
        "configured": True,
        "selected_model_route_id": "route-chat",
        "binding_revision": 1,
        "eligible_routes": [expected_choice],
    }
    serialized = repr(configured).lower()
    assert "url" not in serialized
    assert "credential" not in serialized


def test_memory_inherits_the_live_utility_binding_until_overridden(
    operation_environment,
):
    store, factory, artifacts = operation_environment
    _route(
        store,
        connection_id="connection-a",
        route_id="route-a",
        model_id="model-a",
        operation="chat.complete",
    )
    _route(
        store,
        connection_id="connection-b",
        route_id="route-b",
        model_id="model-b",
        operation="chat.complete",
    )
    store.put_route_binding(
        owner="alice",
        purpose="utility",
        model_route_ids=("route-a",),
        expected_revision=0,
    )
    router = ManagedOperationRouter(
        session_factory=factory,
        artifact_store=artifacts,
        executor=None,
    )
    request = ManagedOperationRequest(
        owner="alice",
        operation="chat.complete",
        purpose="memory",
        input={"messages": []},
    )

    assert router._resolve_routes(request)[0].model_route_id == "route-a"
    assert router.route_preflight(
        owner="alice",
        purpose="memory",
        operation="chat.complete",
    )["configured"] is True

    store.put_route_binding(
        owner="alice",
        purpose="utility",
        model_route_ids=("route-b",),
        expected_revision=1,
    )
    assert router._resolve_routes(request)[0].model_route_id == "route-b"

    store.put_route_binding(
        owner="alice",
        purpose="memory",
        model_route_ids=("route-a",),
        expected_revision=0,
    )
    assert router._resolve_routes(request)[0].model_route_id == "route-a"


@pytest.mark.asyncio
async def test_router_sends_only_the_selected_model_route_and_artifacts(
    operation_environment,
):
    store, factory, artifacts = operation_environment
    _route(
        store,
        connection_id="connection-a",
        route_id="route-a",
        model_id="upscale-a",
        operation="image.upscale",
    )
    _route(
        store,
        connection_id="connection-b",
        route_id="route-b",
        model_id="upscale-b",
        operation="image.upscale",
    )
    store.put_route_binding(
        owner="alice",
        purpose="images",
        model_route_ids=("route-b", "route-a"),
        expected_revision=0,
    )
    captured = {}

    async def execute(owner, payload):
        captured.update({"owner": owner, "payload": payload})
        output = artifacts.put(
            owner=owner,
            chunks=(b"result-image",),
            media_type="image/png",
        )
        return {
            "operationID": "op_result_1",
            "rootOperationID": payload["rootOperationID"],
            "operation": payload["operation"],
            "state": "complete",
            "committed": True,
            "commitReason": "artifact_returned",
            "modelRouteID": "route-b",
            "connectionID": "connection-b",
            "billingLane": "local",
            "output": {},
            "artifacts": [
                {
                    "artifactID": output.id,
                    "contentSHA256": output.content_sha256,
                    "sizeBytes": output.size_bytes,
                    "mediaType": output.media_type,
                    "state": output.state,
                }
            ],
            "replayed": False,
        }

    router = ManagedOperationRouter(
        session_factory=factory,
        artifact_store=artifacts,
        executor=execute,
    )
    source = router.stage_artifact(
        owner="alice",
        name="source",
        data=b"source-image",
        media_type="image/png",
    )
    result = await router.execute(
        ManagedOperationRequest(
            owner="alice",
            operation="image.upscale",
            purpose="images",
            input={"scale": 2},
            artifacts=(source,),
            root_operation_id="root_image_1",
            idempotency_key="operation-image-key-0001",
        )
    )

    assert captured["owner"] == "alice"
    assert [route["modelRouteID"] for route in captured["payload"]["routes"]] == [
        "route-b",
    ]
    assert "url" not in repr(captured["payload"]).lower()
    assert captured["payload"]["artifactInputs"][0]["artifactID"] == source.artifact_id
    assert result.committed is True
    assert router.read_artifact(
        owner="alice",
        artifact=result.artifacts[0],
        acknowledge=True,
    ) == b"result-image"


@pytest.mark.asyncio
async def test_router_rejects_route_escape_and_precommit_completion(
    operation_environment,
):
    store, factory, artifacts = operation_environment
    _route(
        store,
        connection_id="connection-a",
        route_id="route-a",
        model_id="chat-a",
        operation="chat.complete",
    )
    store.put_route_binding(
        owner="alice",
        purpose="chat",
        model_route_ids=("route-a",),
        expected_revision=0,
    )

    async def escaped(_owner, payload):
        return {
            "operationID": "op_escape",
            "rootOperationID": payload["rootOperationID"],
            "operation": payload["operation"],
            "state": "complete",
            "committed": True,
            "modelRouteID": "route-foreign",
            "connectionID": "connection-foreign",
            "billingLane": "local",
            "output": {"text": "no"},
            "artifacts": [],
            "replayed": False,
        }

    router = ManagedOperationRouter(
        session_factory=factory,
        artifact_store=artifacts,
        executor=escaped,
    )
    request = ManagedOperationRequest(
        owner="alice",
        operation="chat.complete",
        input={"messages": [{"role": "user", "content": "hello"}]},
        root_operation_id="root_chat_1",
        idempotency_key="operation-chat-key-0001",
    )
    with pytest.raises(ManagedOperationProtocolError, match="outside"):
        await router.execute(request)

    async def uncommitted(_owner, payload):
        return {
            "operationID": "op_uncommitted",
            "rootOperationID": payload["rootOperationID"],
            "operation": payload["operation"],
            "state": "complete",
            "committed": False,
            "modelRouteID": "route-a",
            "connectionID": "connection-a",
            "billingLane": "local",
            "output": {"text": "no"},
            "artifacts": [],
            "replayed": False,
        }

    router._executor = uncommitted
    with pytest.raises(ManagedOperationProtocolError, match="commit barrier"):
        await router.execute(request)


def test_result_validation_accepts_persisted_pin_over_request_preference(
    operation_environment,
):
    store, factory, artifacts = operation_environment
    connection = store.create_connection(
        owner="alice",
        connection_id="connection-policy",
        family_id="test-provider",
        adapter_id="test-adapter",
        kind="api",
        billing_lane="subscription",
        label="Policy test",
    )
    route = store.create_model_route(
        owner="alice",
        connection_id=connection.id,
        model_route_id="route-policy",
        provider_model_id="model-policy",
        display_name="Policy model",
        operations=("chat.complete",),
    )
    selected = store.create_account(
        owner="alice",
        connection_id=connection.id,
        account_id="policy-pinned-account",
        label="Pinned account",
        auth_method="api_key",
        auth_class="api_key",
        credentials={"api_key": "synthetic-key"},
    )
    store.create_account(
        owner="alice",
        connection_id=connection.id,
        account_id="request-preferred-account",
        label="Request preferred account",
        auth_method="api_key",
        auth_class="api_key",
        credentials={"api_key": "synthetic-other-key"},
    )
    with factory() as db:
        db.add(
            ProviderOperationBinding(
                id="binding-policy-pin",
                owner="alice",
                credential_owner="alice",
                root_operation_id="root-policy-pin",
                connection_id=connection.id,
                provider_id="test-provider",
                billing_lane="subscription",
                model_id="model-policy",
                selected_account_id=selected.id,
                credential_revision=1,
                source="pinned",
                policy_snapshot={
                    "version": 1,
                    "source": "explicit",
                    "policy_revision": 4,
                    "mode": "pinned",
                    "account_id": selected.id,
                    "same_model_fallback": False,
                    "ordered_account_ids": [selected.id],
                },
            )
        )
        db.commit()

    router = ManagedOperationRouter(
        session_factory=factory,
        artifact_store=artifacts,
        executor=None,
    )
    request = ManagedOperationRequest(
        owner="alice",
        operation="chat.complete",
        model_route_id=route.id,
        preferred_account_id="request-preferred-account",
        input={"messages": []},
        root_operation_id="root-policy-pin",
    )
    raw = {
        "operationID": "op-policy-pin",
        "rootOperationID": request.root_operation_id,
        "operation": request.operation,
        "state": "complete",
        "committed": True,
        "commitReason": "response_returned",
        "modelRouteID": route.id,
        "connectionID": connection.id,
        "billingLane": connection.billing_lane,
        "bindingID": "binding-policy-pin",
        "selectedAccountID": selected.id,
        "output": {"text": "synthetic result"},
        "artifacts": [],
        "replayed": False,
    }
    result = router._validate_result(
        owner="alice",
        request=request,
        routes=router._resolve_routes(request),
        raw=raw,
    )
    assert result.selected_account_id == selected.id

    with factory() as db:
        binding = db.get(ProviderOperationBinding, "binding-policy-pin")
        binding.model_id = "different-model"
        db.commit()
    with pytest.raises(ManagedOperationProtocolError, match="different from the bound request"):
        router._validate_result(
            owner="alice",
            request=request,
            routes=router._resolve_routes(request),
            raw=raw,
        )


@pytest.mark.asyncio
async def test_embeddings_require_stable_fingerprint_and_dimension(
    operation_environment,
):
    store, factory, artifacts = operation_environment
    _route(
        store,
        connection_id="connection-embed",
        route_id="route-embed",
        model_id="embed-v1",
        operation="embeddings.create",
    )
    store.put_route_binding(
        owner="alice",
        purpose="embeddings",
        model_route_ids=("route-embed",),
        expected_revision=0,
    )

    async def execute(_owner, payload):
        return {
            "operationID": "op_embed",
            "rootOperationID": payload["rootOperationID"],
            "operation": payload["operation"],
            "state": "complete",
            "committed": True,
            "commitReason": "response_returned",
            "modelRouteID": "route-embed",
            "connectionID": "connection-embed",
            "billingLane": "local",
            "output": {"embeddings": [[0.1, 0.2], [0.3, 0.4]]},
            "artifacts": [],
            "modelFingerprint": "local:embed-v1:2",
            "dimension": 2,
            "replayed": False,
        }

    router = ManagedOperationRouter(
        session_factory=factory,
        artifact_store=artifacts,
        executor=execute,
    )
    result = await router.execute(
        ManagedOperationRequest(
            owner="alice",
            operation="embeddings.create",
            input={"texts": ["a", "b"]},
            root_operation_id="root_embed_1",
            idempotency_key="operation-embed-key-0001",
        )
    )
    assert result.dimension == 2
    assert result.model_fingerprint == "local:embed-v1:2"


@pytest.mark.asyncio
async def test_router_refuses_urls_credentials_and_inline_binary(
    operation_environment,
):
    store, factory, artifacts = operation_environment
    _route(
        store,
        connection_id="connection-a",
        route_id="route-a",
        model_id="chat-a",
        operation="chat.complete",
    )
    store.put_route_binding(
        owner="alice",
        purpose="chat",
        model_route_ids=("route-a",),
        expected_revision=0,
    )

    async def never(_owner, _payload):
        raise AssertionError("engine must not be called")

    router = ManagedOperationRouter(
        session_factory=factory,
        artifact_store=artifacts,
        executor=never,
    )
    base = dict(
        owner="alice",
        operation="chat.complete",
        root_operation_id="root_chat_2",
        idempotency_key="operation-chat-key-0002",
    )
    with pytest.raises(ManagedOperationDenied, match="forbidden"):
        await router.execute(ManagedOperationRequest(input={"api_key": "secret"}, **base))
    with pytest.raises(ManagedOperationDenied, match="artifact"):
        await router.execute(
            ManagedOperationRequest(input={"image": "data:image/png;base64,AA=="}, **base)
        )


@pytest.mark.asyncio
async def test_web_search_requires_explicit_mimo_route_and_accepts_annotation_rows(operation_environment):
    store, factory, artifacts = operation_environment
    store.create_connection(owner="alice", connection_id="connection-mimo", family_id="xiaomi",
                            adapter_id="mimo-native", kind="api", billing_lane="metered_api", label="MiMo")
    store.create_model_route(owner="alice", connection_id="connection-mimo", model_route_id="route-search",
                             provider_model_id="mimo-v2.5", display_name="MiMo Search", operations=("web.search",))
    calls = []

    async def execute(_owner, payload):
        calls.append(payload)
        return {"operationID": "op-search-0001", "rootOperationID": payload["rootOperationID"],
                "operation": "web.search", "state": "complete", "committed": True,
                "commitReason": "response_returned", "modelRouteID": "route-search",
                "connectionID": "connection-mimo", "billingLane": "metered_api",
                "output": {"status": "complete", "results": [{"url": "https://source.example/a",
                "title": "A", "snippet": "from annotation", "provider": "xiaomi"}],
                "answer": "A grounded answer with https://prose.example ignored as a source."},
                "artifacts": [], "replayed": False}

    router = ManagedOperationRouter(session_factory=factory, artifact_store=artifacts, executor=execute)
    with pytest.raises(ManagedOperationDenied, match="explicitly selected"):
        await router.execute(ManagedOperationRequest(owner="alice", operation="web.search", purpose="search",
            input={"query": "typed"}, root_operation_id="root-search-no-route", idempotency_key="search-no-route-0001"))
    result = await router.execute(ManagedOperationRequest(owner="alice", operation="web.search", purpose="search",
        model_route_id="route-search", input={"query": "typed", "count": 5, "answer": True},
        root_operation_id="root-search-0001", idempotency_key="search-operation-0001"))
    assert result.output["results"][0]["url"] == "https://source.example/a"
    assert result.output["answer"].startswith("A grounded answer")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_web_search_rejects_arbitrary_authority_and_malformed_source(operation_environment):
    store, factory, artifacts = operation_environment
    store.create_connection(owner="alice", connection_id="connection-mimo", family_id="xiaomi",
                            adapter_id="mimo-native", kind="api", billing_lane="metered_api", label="MiMo")
    store.create_model_route(owner="alice", connection_id="connection-mimo", model_route_id="route-search",
                             provider_model_id="mimo-v2.5", display_name="MiMo Search", operations=("web.search",))

    async def malformed(_owner, payload):
        return {"operationID": "op-search-bad", "rootOperationID": payload["rootOperationID"],
                "operation": "web.search", "state": "complete", "committed": True,
                "modelRouteID": "route-search", "connectionID": "connection-mimo", "billingLane": "metered_api",
                "output": {"results": [{"url": "javascript:alert(1)"}]}, "artifacts": [], "replayed": False}

    router = ManagedOperationRouter(session_factory=factory, artifact_store=artifacts, executor=malformed)
    base = dict(owner="alice", operation="web.search", purpose="search", model_route_id="route-search",
                root_operation_id="root-search-bad", idempotency_key="search-operation-bad")
    with pytest.raises(ManagedOperationDenied, match="unsupported fields"):
        await router.execute(ManagedOperationRequest(input={"query": "q", "endpoint": "https://evil"}, **base))
    with pytest.raises(ManagedOperationProtocolError, match="source URL"):
        await router.execute(ManagedOperationRequest(input={"query": "q"}, **base))


def test_cancelled_artifact_replay_is_uncommitted_and_single_terminal_transition(operation_environment):
    _store, factory, _artifacts = operation_environment
    journal = OperationJournalStore(factory)
    row, replayed = journal.begin(
        owner="alice", root_operation_id="root-cancel", operation="web.search",
        idempotency_key="cancel-operation-0001", request={"query": "x"},
        connection_id="connection-1", billing_lane="local", model_route_id="route-1",
    )
    assert replayed is False
    attached = journal.cas(
        owner="alice", operation_id=row.id, expected_revision=row.revision,
        artifact_id="artifact-cancelled",
    )
    assert attached.committed is False
    assert attached.state == "pending"
    terminal = journal.cas(
        owner="alice", operation_id=row.id, expected_revision=attached.revision,
        state="cancelled", artifact_id="artifact-cancelled",
    )
    assert terminal.committed is False
    assert terminal.state == "cancelled"
    assert terminal.revision == row.revision + 2
    replay, replayed = journal.begin(
        owner="alice", root_operation_id="root-cancel", operation="web.search",
        idempotency_key="cancel-operation-0001", request={"query": "x"},
        connection_id="connection-1", billing_lane="local", model_route_id="route-1",
    )
    assert replayed is True
    assert replay.state == "cancelled"
    assert replay.committed is False
    assert replay.artifact_ids == ["artifact-cancelled"]


def test_journal_first_terminal_wins_across_completion_and_cancellation(operation_environment):
    _store, factory, _artifacts = operation_environment
    journal = OperationJournalStore(factory)

    row, _ = journal.begin(
        owner="alice", root_operation_id="root-race-a", operation="web.search",
        idempotency_key="race-operation-00001", request={"query": "a"},
        connection_id="connection-1", billing_lane="local", model_route_id="route-1",
    )
    complete = journal.cas(owner="alice", operation_id=row.id, expected_revision=row.revision, state="complete", commit_reason="provider_result")
    assert complete.state == "complete"
    assert complete.revision == row.revision + 1
    with pytest.raises(OperationConflict):
        journal.cas(owner="alice", operation_id=row.id, expected_revision=complete.revision, state="cancelled")
    replay, replayed = journal.begin(
        owner="alice", root_operation_id="root-race-a", operation="web.search",
        idempotency_key="race-operation-00001", request={"query": "a"},
        connection_id="connection-1", billing_lane="local", model_route_id="route-1",
    )
    assert replayed is True and replay.state == "complete" and replay.revision == complete.revision

    row, _ = journal.begin(
        owner="alice", root_operation_id="root-race-b", operation="web.search",
        idempotency_key="race-operation-00002", request={"query": "b"},
        connection_id="connection-1", billing_lane="local", model_route_id="route-1",
    )
    cancelled = journal.cas(owner="alice", operation_id=row.id, expected_revision=row.revision, state="cancelled")
    assert cancelled.state == "cancelled" and cancelled.committed is False
    with pytest.raises(OperationConflict):
        journal.cas(owner="alice", operation_id=row.id, expected_revision=cancelled.revision, state="complete", commit_reason="provider_result")
    replay, replayed = journal.begin(
        owner="alice", root_operation_id="root-race-b", operation="web.search",
        idempotency_key="race-operation-00002", request={"query": "b"},
        connection_id="connection-1", billing_lane="local", model_route_id="route-1",
    )
    assert replayed is True and replay.state == "cancelled" and replay.committed is False


def test_artifact_attachment_never_fabricates_commitment(operation_environment):
    _store, factory, _artifacts = operation_environment
    journal = OperationJournalStore(factory)
    row, _ = journal.begin(owner="alice", root_operation_id="root-commit", operation="web.search", idempotency_key="commit-operation-0001", request={"query": "x"}, connection_id="connection-1", billing_lane="local", model_route_id="route-1")
    complete = journal.cas(owner="alice", operation_id=row.id, expected_revision=row.revision, state="complete", artifact_id="artifact-result")
    assert complete.committed is False
    explicit = journal.cas(owner="alice", operation_id=row.id, expected_revision=complete.revision, commit_reason="provider_result")
    assert explicit.committed is True
    assert explicit.commit_reason == "provider_result"


@pytest.mark.asyncio
async def test_search_service_prepares_real_managed_operation_and_finalizes_once(operation_environment, monkeypatch):
    from services.search import service
    import src.openclank.operation_router as operation_router
    store, factory, artifacts = operation_environment
    store.create_connection(owner="alice", connection_id="connection-mimo", family_id="xiaomi",
                            adapter_id="mimo-native", kind="api", billing_lane="metered_api", label="MiMo")
    store.create_model_route(owner="alice", connection_id="connection-mimo", model_route_id="route-search",
                             provider_model_id="mimo-v2.5", display_name="MiMo Search", operations=("web.search",))
    calls = []
    async def execute(owner, payload):
        calls.append((owner, payload))
        return {"operationID": "op-search-0001", "rootOperationID": payload["rootOperationID"],
                "operation": "web.search", "state": "complete", "committed": True,
                "commitReason": "response_returned", "modelRouteID": "route-search",
                "connectionID": "connection-mimo", "billingLane": "metered_api",
                "output": {"status": "complete", "results": [{"url": "https://source.example/a", "title": "A"}]},
                "artifacts": [], "replayed": False}
    router = ManagedOperationRouter(session_factory=factory, artifact_store=artifacts, executor=execute)
    monkeypatch.setattr(router, "route_preflight", lambda **kwargs: {"configured": True, "selected_model_route_id": "route-search"})
    monkeypatch.setattr(operation_router, "get_operation_router", lambda: router)
    monkeypatch.setattr(service, "_get_search_settings", lambda: {"search_provider": "mimo", "search_result_count": 4})
    svc = service.SearchService(fetch_content=False)
    prepared = svc.prepare_search("  actual query  ", owner="alice", mode="answer", deadline=10,
                                  root_operation_id="root-search-fixture", idempotency_key="search-fixture-00001")
    assert isinstance(prepared, service.PreparedSearch) and calls == []
    assert prepared.snapshots == ()
    wire = prepared.managed_prepared.payload
    assert wire["input"]["query"] == "actual query" and wire["input"]["count"] == 4 and wire["input"]["answer"] is True
    assert wire["rootOperationID"] == "root-search-fixture" and wire["idempotencyKey"] == "search-fixture-00001"
    assert 10 <= wire["options"]["deadlineMs"] <= 10000
    result = await router.execute_prepared(prepared.managed_prepared)
    assert len(calls) == 1 and svc.project_managed(prepared, result).results[0].url == "https://source.example/a"
    raw = await execute(*calls[0])
    assert router.finalize(prepared.managed_prepared, raw) is result
    raw["operationID"] = "different-result"
    with pytest.raises(ManagedOperationProtocolError, match="conflicting"):
        router.finalize(prepared.managed_prepared, raw)


@pytest.mark.parametrize("state,code", [("failed", "model_not_found"), ("cancelled", "cancelled")])
def test_search_projection_preserves_managed_terminal_failure(state, code):
    from types import SimpleNamespace
    from services.search.service import SearchService
    prepared = SimpleNamespace(query="q", caller="chat", mode="results", started=0)
    output = {"errorCode": "model_not_found"} if state == "failed" else {}
    managed = SimpleNamespace(state=state, output=output, selected_account_id="a", model_route_id="r")
    response = SearchService().project_managed(prepared, managed)
    assert response.status == state and response.error["code"] == code
    assert response.account_id == "a" and response.model_route == "r"

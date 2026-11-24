"""Reconcile synthetic journal output, attach it, and ask the host to verify it."""
import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def seed_completed_result(directory):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.operation_models import OperationBase
    from core.provider_models import ProviderBase
    from src.openclank.artifacts import ArtifactStore
    from src.openclank.operation_journal import OperationJournalStore
    from src.openclank.operation_router import ManagedOperationRouter, ManagedOperationRequest, _thaw_json
    from src.openclank.provider_store import ProviderStore

    engine = create_engine("sqlite:///" + str(directory / "router.sqlite"))
    ProviderBase.metadata.create_all(engine)
    OperationBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = ProviderStore(factory)
    store.create_connection(owner="alice", connection_id="local", family_id="local",
                            adapter_id="openclank-local-executor", kind="local_executor",
                            billing_lane="local", label="Local fixture")
    store.create_model_route(owner="alice", connection_id="local", model_route_id="fixture-route",
                             provider_model_id="fixture-model", operations=["chat.complete"])
    artifacts = ArtifactStore(directory / "artifacts", session_factory=factory)
    router = ManagedOperationRouter(session_factory=factory, artifact_store=artifacts)
    request = ManagedOperationRequest(owner="alice", operation="chat.complete", model_route_id="fixture-route",
                                      root_operation_id="fixture-root", idempotency_key="fixture-summary-request",
                                      input={"messages": [{"role": "user", "content": "Summarize the example."}]})
    payload = _thaw_json(router.prepare(request).payload)
    exact = {key: payload[key] for key in ("operation", "routes", "input", "artifactInputs", "options")}
    journal = OperationJournalStore(factory)
    row, _ = journal.begin(owner="alice", root_operation_id=request.root_operation_id,
                           idempotency_key=request.idempotency_key, request=exact, operation=request.operation,
                           connection_id="local", billing_lane="local", model_route_id=request.model_route_id)
    receipt = {"operationID": row.id, "rootOperationID": request.root_operation_id, "operation": request.operation,
               "state": "complete", "committed": True, "replayed": False, "commitReason": "output_visible",
               "modelRouteID": request.model_route_id, "connectionID": "local", "billingLane": "local",
               "output": {"text": "A synthetic interpretation."}, "artifacts": []}
    artifact = artifacts.put(owner="alice", chunks=[json.dumps(receipt).encode()],
                             media_type="application/vnd.openclank.operation-result+json")
    artifacts.acknowledge(owner="alice", artifact_id=artifact.id)
    journal.cas(owner="alice", operation_id=row.id, expected_revision=row.revision, state="complete",
                commit_reason=receipt["commitReason"], artifact_id=artifact.id)
    return engine, router, request


async def verify_resource(resource, upload_id, handler):
    from src.openclank.acp_bridge import ACPBridge

    envelope = resource["_meta"]["openclankGeneratedSummary"]
    hashes = {"uploadID": upload_id, "originSHA256": digest(envelope), "textSHA256": digest(resource["text"])}
    turn = {"sessionID": "session", "rootOperationID": "fixture-root", "actorID": "main",
            "executionRevision": 1, "innerTurnID": "turn"}
    binding = {"owner": "alice", "stableChatID": "chat", "authorityWorkspaceID": "workspace",
               "mappingRevision": 1, "workspaceRevision": 1, "mapRevision": 1}
    entry = {**binding, "state": "active", "ownerAccountID": "account", "policyRevision": 1,
             "generatedSummaryBoundary": [{**hashes, "envelope": envelope}]}
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._managed_execution_lock = asyncio.Lock()
    bridge._managed_actor_turns = {bridge._managed_turn_key(turn): entry}
    bridge._managed_provider_generation = "fixture-holder"
    bridge.resolve_managed_actor_execution = AsyncMock(return_value={"rootSessionID": "session"})
    bridge._handle_session_binding_read = AsyncMock(return_value=binding)
    worker = SimpleNamespace(_bridge=bridge, is_alive=lambda: True,
                             _managed_callbacks=SimpleNamespace(holder_id=bridge._managed_provider_generation))
    pool = SimpleNamespace(worker_for_owner=lambda owner: worker if owner == "alice" else None)
    lifecycle = SimpleNamespace(mimo_supervisor=SimpleNamespace(owner_lifecycle_pool=lambda: pool),
                                auth_manager=SimpleNamespace(account_id=lambda owner: "account" if owner == "alice" else None,
                                                             username_for_account_id=lambda key: "alice" if key == "account" else None))
    with patch("core.models.get_session_manager_instance", return_value=SimpleNamespace(lifecycle_context=lifecycle)), \
            patch("src.tool_utils.get_upload_handler", return_value=handler):
        proof = await bridge._handle_generated_attachment_source({**turn, **hashes, "messageID": "message", "partID": "part"})
    if proof.get("state") != "ready":
        raise AssertionError("host did not verify the generated attachment")
    return proof


async def generate(directory):
    from src import secret_storage
    from src.document_processor import build_user_content
    from src.openclank import generated_summary_attachment as attachment
    from src.upload_handler import UploadHandler

    engine, router, request = seed_completed_result(directory)
    try:
        material = {"account_id": "account", "request": {"receipt": {"revision": 1}}, "sources": []}
        with patch.object(attachment, "get_operation_router", return_value=router), \
                patch.object(attachment, "_generation_material", AsyncMock(return_value=(material, request))), \
                patch.object(secret_storage, "_KEY_PATH", directory / "upload.key"):
            summary = await attachment.verify_result(repository=None, provider=None, auth_manager=None, owner="alice",
                                                     body={"preview_hash": digest("synthetic preview")}, chat_id="chat")
            handler = UploadHandler(str(directory), str(directory / "uploads"))
            info = attachment.create_upload(handler, summary)
            upload_id = info["id"]
            info = handler.resolve_upload(upload_id, owner="alice")
            origin = attachment.verify_upload(info, handler=handler, owner="alice", account_id="account", chat_id="chat")
            if not origin:
                raise AssertionError("generated upload did not verify")
            resolved = {**info, "_generated_summary_state": True, "_generated_summary_envelope": info["generated_summary_origin"]}
            content = build_user_content("", [upload_id], handler.upload_dir, handler, owner="alice",
                                         resolved_uploads={upload_id: resolved}, structured_resources=True)
            resource = next(item["resource"] for item in content if item["type"] == "resource")
            return {"resource": resource, "proof": await verify_resource(resource, upload_id, handler)}
    finally:
        engine.dispose()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="generated-summary-") as temporary:
        root = Path(temporary)
        os.environ.update(OPEN_CLANK_DATA_DIR=temporary, FM_DB_PATH=str(root / "memory.sqlite"),
                          DATABASE_URL="sqlite:///" + str(root / "app.sqlite"))
        print(json.dumps(asyncio.run(generate(root))))

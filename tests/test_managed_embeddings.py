from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.operation_models import OperationBase
from core.provider_models import ProviderBase
from routes.embedding_routes import setup_embedding_routes
from routes.provider_v1_routes import setup_provider_v1_routes
from src.embeddings import ManagedEmbeddingClient, ManagedEmbeddingError
from src.openclank.artifacts import ArtifactStore
from src.openclank.operation_router import (
    ManagedOperationProtocolError,
    ManagedOperationRequest,
    ManagedOperationResult,
    ManagedOperationRouter,
)
from src.openclank.provider_store import ProviderStore


def _result(*, fingerprint: str = "engine:model:3", dimension: int = 3):
    return ManagedOperationResult(
        operation_id="op_embed_1",
        root_operation_id="root_embed_1",
        operation="embeddings.create",
        state="complete",
        committed=True,
        replayed=False,
        model_route_id="route-embed",
        connection_id="connection-embed",
        billing_lane="metered_api",
        output={"embeddings": [[1.0, 0.0, 0.0], [0.0, 2.0, 0.0]]},
        artifacts=(),
        model_fingerprint=fingerprint,
        dimension=dimension,
    )


def test_sync_embedding_client_uses_only_managed_operation_and_pins_fingerprint(monkeypatch):
    calls = []

    async def managed(**kwargs):
        calls.append(kwargs)
        return _result()

    monkeypatch.setattr("src.openclank.modality_facade.create_embeddings", managed)
    monkeypatch.setattr(
        "src.embeddings._route_identity",
        lambda owner, model_route_id, connection_id: {
            "connection_id": connection_id,
            "adapter_id": "openai-responses",
            "kind": "official",
            "billing_lane": "metered_api",
            "model_route_id": model_route_id,
            "model_id": "text-embedding-3-small",
            "route_revision": 4,
            "catalog_revision": 8,
        },
    )

    client = ManagedEmbeddingClient(owner="Alice")
    vectors = client.encode(["one", "two"])

    assert calls == [
        {
            "owner": "alice",
            "texts": ["one", "two"],
            "content_purpose": "index",
            "model_route_id": None,
        }
    ]
    assert vectors.shape == (2, 3)
    assert np.allclose(np.linalg.norm(vectors, axis=1), [1.0, 1.0])
    assert client.model_route_id == "route-embed"
    assert client.adapter_id == "openai-responses"
    assert client.provider_ref.startswith("managed-embedding:")

    with pytest.raises(ManagedEmbeddingError, match="fingerprint changed"):
        monkeypatch.setattr(
            "src.embeddings._route_identity",
            lambda *_args: {
                "connection_id": "connection-embed",
                "adapter_id": "openai-chat",
                "kind": "official",
                "billing_lane": "metered_api",
                "model_route_id": "route-embed",
                "model_id": "text-embedding-3-small",
                "route_revision": 4,
                "catalog_revision": 8,
            },
        )
        client.encode(["one", "two"])


@pytest.fixture()
def embedding_router_environment(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'router.db'}")
    ProviderBase.metadata.create_all(engine)
    OperationBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = ProviderStore(factory)
    store.create_connection(
        owner="alice",
        connection_id="connection-embed",
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label="Embeddings",
    )
    store.create_model_route(
        owner="alice",
        connection_id="connection-embed",
        model_route_id="route-embed",
        provider_model_id="text-embedding-3-small",
        operations=("embeddings.create",),
    )
    store.put_route_binding(
        owner="alice",
        purpose="embeddings",
        model_route_ids=("route-embed",),
        expected_revision=0,
    )
    artifacts = ArtifactStore(tmp_path / "spool", session_factory=factory)
    yield factory, artifacts
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vectors,error",
    [
        ([[1.0, 0.0]], "row count"),
        ([[1.0, 0.0], [1.0, float("nan")]], "declared dimension"),
    ],
)
async def test_router_rejects_malformed_embedding_rows(
    embedding_router_environment,
    vectors,
    error,
):
    factory, artifacts = embedding_router_environment

    async def execute(_owner, payload):
        return {
            "operationID": "op_embed_bad",
            "rootOperationID": payload["rootOperationID"],
            "operation": "embeddings.create",
            "state": "complete",
            "committed": True,
            "modelRouteID": "route-embed",
            "connectionID": "connection-embed",
            "billingLane": "metered_api",
            "output": {"embeddings": vectors},
            "artifacts": [],
            "modelFingerprint": "connection:model:2",
            "dimension": 2,
            "replayed": False,
        }

    router = ManagedOperationRouter(
        session_factory=factory,
        artifact_store=artifacts,
        executor=execute,
    )
    with pytest.raises(ManagedOperationProtocolError, match=error):
        await router.execute(
            ManagedOperationRequest(
                owner="alice",
                operation="embeddings.create",
                input={"texts": ["one", "two"]},
                root_operation_id="root_embed_bad",
                idempotency_key="embedding-malformed-0001",
            )
        )


def test_retired_embedding_endpoint_route_is_absent():
    router = setup_embedding_routes()
    paths = {route.path for route in router.routes if hasattr(route, "path")}
    assert "/api/embeddings/endpoint" not in paths
    assert "/api/embeddings/models" in paths


def test_engine_declared_local_fastembed_route_is_persisted_atomically():
    class LocalControl:
        async def call(self, *, method, payload, **_kwargs):
            assert method.endswith("/connection/validate")
            return {
                "familyID": "local-executor",
                "adapterID": "openclank-local-executor",
                "kind": "local",
                "billingLane": "local",
                "settings": {},
                "credentialRequired": False,
                "modelRoutes": [
                    {
                        "modelID": "fastembed/default",
                        "displayName": "FastEmbed (local)",
                        "operations": ["embeddings.create"],
                        "capabilities": {
                            "localExecutorID": "openclank.fastembed.v1",
                        },
                        "provenance": {"authority": "managed-engine"},
                    }
                ],
            }

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = ProviderStore(factory)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.current_user = "alice"
        request.state.api_token = False
        return await call_next(request)

    app.include_router(setup_provider_v1_routes(store, LocalControl()))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/providers/connections",
            headers={"Idempotency-Key": "local-fastembed-create"},
            json={
                "family_id": "local-executor",
                "adapter_id": "openclank-local-executor",
                "kind": "local",
                "billing_lane": "local",
                "label": "Local executors",
            },
        )
    assert response.status_code == 201
    body = response.json()
    assert body["url"] is None
    assert body["models"][0]["model_id"] == "fastembed/default"
    assert body["models"][0]["operations"] == ["embeddings.create"]
    assert len(store.list_connections(owner="alice")) == 1
    assert len(store.list_model_routes(owner="alice")) == 1
    engine.dispose()


def test_embedding_network_ledger_has_no_python_or_frankenmemory_bypass():
    root = Path(__file__).resolve().parents[1]
    embeddings_source = (root / "src/embeddings.py").read_text(encoding="utf-8")
    routes_source = (root / "routes/embedding_routes.py").read_text(encoding="utf-8")
    bridge_source = (root / "src/openclank/acp_bridge.py").read_text(encoding="utf-8")
    rust_main = (
        root / "mcp_servers/frankenmemory/crates/fm-mcp/src/main.rs"
    ).read_text(encoding="utf-8")
    rust_embed = (
        root / "mcp_servers/frankenmemory/crates/fm-core/src/embed.rs"
    ).read_text(encoding="utf-8")

    assert "import httpx" not in embeddings_source
    assert "EMBEDDING_API_KEY" not in embeddings_source
    assert "EMBEDDING_URL" not in embeddings_source
    assert "@router.post(\"/endpoint\")" not in routes_source
    assert "name.startswith(\"FM_EMBED_\")" not in bridge_source
    assert "HttpEmbeddingClient" not in rust_main
    assert "HttpEmbeddingClient" not in rust_embed
    assert "reqwest::" not in rust_embed
    assert "DisabledEmbeddingClient" in rust_main


def test_local_fastembed_recipe_ignores_caller_model_and_environment(monkeypatch):
    from src.openclank import local_model_executors

    observed = []

    class FakeFastEmbed:
        def __init__(self, *, model):
            observed.append(model)

        def encode(self, texts, normalize_embeddings=True):
            assert normalize_embeddings is True
            return np.asarray([[1.0, 0.0] for _ in texts], dtype="float32")

    monkeypatch.setattr("src.embeddings.FastEmbedClient", FakeFastEmbed)
    local_model_executors._instances.clear()
    output = local_model_executors._fastembed(
        "embeddings.create",
        [],
        {"texts": ["hello"], "model": "attacker/model"},
    )

    assert observed == ["sentence-transformers/all-MiniLM-L6-v2"]
    assert output.media_type == "application/vnd.openclank.embeddings+json"

"""Image and vision surfaces must use typed managed operations only."""

from __future__ import annotations

import base64
import inspect
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routes.gallery import gallery_routes
from src import ai_interaction
from src.openclank import modality_facade
from src.openclank.local_model_executors import build_local_executor_broker
from src.openclank.operation_router import ManagedOperationResult


def _result(operation: str, *, output=None) -> ManagedOperationResult:
    return ManagedOperationResult(
        operation_id="op_image",
        root_operation_id="root_image",
        operation=operation,
        state="complete",
        committed=True,
        replayed=False,
        model_route_id="pmr_image",
        connection_id="pcn_image",
        billing_lane="metered_api",
        output=output or {},
        artifacts=(),
    )


def _gallery_client() -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.current_user = "alice"
        request.state.api_token = False
        return await call_next(request)

    app.include_router(gallery_routes.setup_gallery_routes())
    return TestClient(app)


def test_gallery_inpaint_uses_managed_artifacts_and_rejects_endpoint_authority(
    monkeypatch,
):
    calls = []

    async def transform(**kwargs):
        calls.append(kwargs)
        return b"managed-png", "image/png", _result("image.inpaint")

    monkeypatch.setattr(gallery_routes, "transform_image", transform)
    client = _gallery_client()
    body = {
        "image": base64.b64encode(b"source").decode(),
        "mask": base64.b64encode(b"mask").decode(),
        "prompt": "replace the sky",
        "model_route_id": "pmr_image",
    }
    response = client.post("/api/image/inpaint", json=body)

    assert response.status_code == 200
    assert base64.b64decode(response.json()["image"]) == b"managed-png"
    assert calls[0]["owner"] == "alice"
    assert calls[0]["operation"] == "image.inpaint"
    assert calls[0]["image"] == b"source"
    assert calls[0]["mask"] == b"mask"
    assert calls[0]["model_route_id"] == "pmr_image"

    rejected = client.post("/api/image/inpaint", json={**body, "_endpoint": "http://127.0.0.1:9"})
    assert rejected.status_code == 400
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_agent_image_generation_preserves_root_grant_and_route(monkeypatch):
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return b"png", "image/png", _result("image.generate")

    monkeypatch.setattr(modality_facade, "generate_image", generate)
    monkeypatch.setattr(
        ai_interaction,
        "_save_managed_gallery_image",
        lambda **_kwargs: ("/api/generated-image/test.png", "gallery-1"),
    )

    result = await ai_interaction.do_generate_image(
        "a lighthouse\npmr_image\n1024x1024\nhigh",
        session_id="session-1",
        owner="alice",
        root_operation_id="turn_root_1",
        grant_id="grant-1",
    )

    assert result["image_model"] == "pmr_image"
    assert calls == [
        {
            "owner": "alice",
            "prompt": "a lighthouse",
            "size": "1024x1024",
            "quality": "high",
            "model_route_id": "pmr_image",
            "grant_id": "grant-1",
            "root_operation_id": "turn_root_1",
            "idempotency_key": None,
        }
    ]


def test_no_gallery_or_mcp_provider_transport_remains():
    gallery_source = inspect.getsource(gallery_routes)
    image_tool_source = inspect.getsource(ai_interaction.do_generate_image)
    mcp_source = __import__("pathlib").Path(
        "mcp_servers/image_gen_server.py"
    ).read_text(encoding="utf-8")

    for source in (gallery_source, image_tool_source, mcp_source):
        assert "httpx" not in source
        assert "ModelEndpoint" not in source
        assert "api_key" not in source
        assert "_resolve_model" not in source


def test_gallery_editor_uses_normalized_model_route_ids():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    selector = (root / "static/js/editor/ai-models.js").read_text(encoding="utf-8")
    runner = (root / "static/js/editor/ai-tool-runner.js").read_text(encoding="utf-8")
    inpaint = (root / "static/js/editor/ai-inpaint.js").read_text(encoding="utf-8")

    assert "/api/v1/providers/models" in selector
    assert "/api/model-endpoints" not in selector
    assert "option.value = route.id" in selector
    assert "extraPayload.model_route_id" in runner
    assert "model_route_id: sel.modelRouteId" in inpaint
    for source in (selector, runner, inpaint):
        assert "_endpoint" not in source


def test_local_broker_registers_exact_image_recipes(tmp_path, monkeypatch):
    artifact_store = SimpleNamespace()
    broker = build_local_executor_broker(artifact_store)

    assert broker._specs["openclank.diffusion.v1"].model_id == "diffusion/default"
    assert broker._specs["openclank.diffusion.v1"].operations == {
        "image.generate",
        "image.edit",
        "image.inpaint",
        "image.img2img",
    }
    assert broker._specs["openclank.realesrgan.v1"].operations == {
        "image.upscale",
        "image.denoise",
    }
    assert broker._specs["openclank.rmbg.v1"].operations == {
        "image.segment",
        "image.remove_background",
    }
    assert broker._specs["openclank.gfpgan.v1"].operations == {
        "image.restore_face",
    }

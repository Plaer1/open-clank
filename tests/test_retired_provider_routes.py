from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.openclank.retired_provider_routes import RetiredProviderRouteMiddleware


def test_every_retired_provider_route_is_uniformly_404():
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def legacy_catchall(path: str):
        return {"legacy_handler_ran": path}

    app.add_middleware(RetiredProviderRouteMiddleware)
    client = TestClient(app)
    paths = (
        "/api/model-endpoints",
        "/api/model-endpoints/old/probe",
        "/api/mimo/providers/openai/connect",
        "/api/copilot/device/start",
        "/api/chatgpt-subscription/status",
        "/api/model-shares/old",
        "/api/embeddings/endpoint",
        "/api/discover",
        "/api/ping",
        "/api/probe",
        "/api/probe-selected",
        "/api/providers",
        "/session/openai",
    )
    for path in paths:
        response = client.post(
            path,
            headers={"Authorization": "Bearer ody_not-a-real-token"},
            json={"api_key": "must-not-be-parsed"},
        )
        assert response.status_code == 404, path
        assert response.json() == {"detail": "Not Found"}


def test_normalized_and_read_only_model_routes_are_not_shadowed():
    app = FastAPI()

    @app.get("/api/v1/providers/families")
    def families():
        return {"ok": True}

    @app.get("/api/models")
    def models():
        return {"ok": True}

    app.add_middleware(RetiredProviderRouteMiddleware)
    client = TestClient(app)
    assert client.get("/api/v1/providers/families").status_code == 200
    assert client.get("/api/models").status_code == 200


def test_app_registers_no_legacy_model_router():
    source = Path("app.py").read_text(encoding="utf-8")
    assert "routes.model_routes" not in source
    assert "setup_model_routes" not in source
    assert "backfill_api_endpoint_pins" not in source

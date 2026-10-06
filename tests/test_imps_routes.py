import base64
import io
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image

import routes.imps_routes as imps_routes


def _png(mode, color):
    out = io.BytesIO()
    Image.new(mode, (2, 2), color).save(out, format="PNG")
    return out.getvalue()


def _client():
    app = FastAPI()
    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.current_user = "alice"
        request.state.api_token = False
        return await call_next(request)
    app.include_router(imps_routes.setup_imps_routes())
    return TestClient(app)


def test_managed_mask_returns_model_and_rejects_direct_endpoint(monkeypatch):
    async def transform(**_kwargs):
        return _png("L", 255), "image/png", SimpleNamespace(model_route_id="route-mask")
    monkeypatch.setattr(imps_routes, "transform_image", transform)
    payload = {"image": base64.b64encode(_png("RGBA", (1, 2, 3, 255))).decode(), "text": "cat", "model_route_id": "route-mask"}
    with _client() as client:
        response = client.post("/api/image/mask", json=payload)
        denied = client.post("/api/image/mask", json={**payload, "_endpoint": "http://127.0.0.1"})
    assert response.status_code == 200
    assert response.json()["model"] == "route-mask"
    assert denied.status_code == 400


def test_remove_background_multiplies_user_hint_into_alpha(monkeypatch):
    async def transform(**_kwargs):
        return _png("RGBA", (8, 9, 10, 255)), "image/png", SimpleNamespace(model_route_id="route-remove")
    monkeypatch.setattr(imps_routes, "transform_image", transform)
    payload = {"image": base64.b64encode(_png("RGBA", (1, 2, 3, 255))).decode(), "hint_mask": base64.b64encode(_png("L", 0)).decode()}
    with _client() as client:
        response = client.post("/api/image/remove-bg", json=payload)
    assert response.status_code == 200
    with Image.open(io.BytesIO(base64.b64decode(response.json()["image"]))) as result:
        assert result.getchannel("A").getextrema() == (0, 0)

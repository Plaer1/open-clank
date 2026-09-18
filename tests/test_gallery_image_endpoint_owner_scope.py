"""Owner-scope regressions for the managed Gallery image router.

Legacy Gallery code selected ``ModelEndpoint`` rows and copied their API keys
into direct HTTP requests.  The hard cut instead delegates owner-scoped route
selection and credential leasing to the managed operation router.
"""

import inspect

import pytest

import routes.gallery_routes as gallery_routes


def _source(value) -> str:
    return inspect.getsource(value)


def test_gallery_module_has_no_legacy_endpoint_or_secret_transport():
    body = inspect.getsource(gallery_routes)

    assert "ModelEndpoint" not in body
    assert "api_key" not in body
    assert "httpx" not in body
    assert "_first_visible_image_endpoint" not in body
    assert "_visible_image_endpoint_for_base" not in body


@pytest.mark.asyncio
async def test_managed_transform_passes_owner_and_affinity(monkeypatch):
    seen = {}

    async def capture(**kwargs):
        seen.update(kwargs)
        return b"managed", "image/png", object()

    monkeypatch.setattr(gallery_routes, "transform_image", capture)

    content, media_type, _result = await gallery_routes._managed_gallery_transform(
        owner="alice",
        operation="image.edit",
        image=b"source",
        input={"prompt": "make it blue"},
        model_route_id="route_image",
        root_operation_id="op_root",
        grant_id="grant_shared",
        idempotency_key="idem-1",
    )

    assert (content, media_type) == (b"managed", "image/png")
    assert seen["owner"] == "alice"
    assert seen["model_route_id"] == "route_image"
    assert seen["root_operation_id"] == "op_root"
    assert seen["grant_id"] == "grant_shared"
    assert seen["idempotency_key"] == "idem-1"


def test_gallery_model_selectors_are_normalized_route_ids_only():
    assert gallery_routes._managed_route_id({"model_route_id": "route_123"}) == "route_123"
    assert gallery_routes._managed_route_id({"modelRouteID": "route_456"}) == "route_456"

    with pytest.raises(Exception) as caught:
        gallery_routes._managed_route_id({"endpoint": "https://provider.invalid/v1"})

    assert getattr(caught.value, "status_code", None) == 400

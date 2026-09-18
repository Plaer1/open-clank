"""Direct Gallery endpoint authority is rejected after provider cutover."""

import pytest
from fastapi import HTTPException

from routes.gallery import gallery_routes


def test_retired_endpoint_is_rejected_before_managed_dispatch(monkeypatch):
    called = False

    async def transform(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("managed dispatch should not run")

    monkeypatch.setattr(gallery_routes, "transform_image", transform)
    with pytest.raises(HTTPException) as caught:
        gallery_routes._managed_route_id(
            {"_endpoint": "http://169.254.169.254/latest/meta-data"}
        )
    assert caught.value.status_code == 400
    assert called is False


def test_url_safety_still_blocks_metadata_for_non_model_integrations():
    from src.url_safety import check_outbound_url

    ok, _ = check_outbound_url("http://169.254.169.254/latest/meta-data")
    assert ok is False

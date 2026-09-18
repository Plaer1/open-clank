"""Managed Gallery results are host artifacts, never fetched URLs."""

import inspect

import routes.gallery_routes as gallery_routes


def test_gallery_never_fetches_provider_result_urls():
    source = inspect.getsource(gallery_routes)

    assert "_fetch_result_image_b64" not in source
    assert "AsyncClient" not in source
    assert "httpx" not in source
    assert "result_url" not in source
    assert "transform_image" in source

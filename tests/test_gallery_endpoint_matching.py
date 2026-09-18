"""Gallery execution no longer matches or normalizes provider URLs."""

import inspect

import routes.gallery_routes as gallery_routes


def test_gallery_has_no_provider_url_matching_surface():
    source = inspect.getsource(gallery_routes)

    assert "_normalize_image_endpoint_base" not in source
    assert "_visible_image_endpoint_for_base" not in source
    assert "rstrip('/v1')" not in source
    assert "model_route_id" in source

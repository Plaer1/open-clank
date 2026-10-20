"""Owner-scope follow-up guards for secondary model workflows."""

from pathlib import Path


def test_compare_uses_only_normalized_managed_routes():
    body = Path("routes/compare/compare_routes.py").read_text(encoding="utf-8")
    start_body = body.split("def start_comparison", 1)[1].split(
        "# Store comparison record", 1
    )[0]

    assert "resolve_chat_route(" in start_body
    assert "endpoint_id=eid" in start_body
    assert "provider_model_route_id=route.model_route_id" in start_body
    assert "endpoint_url=MANAGED_ENGINE_PUBLIC_URL" in start_body
    assert "api_key" not in start_body
    assert "headers" not in start_body


def test_imps_operations_use_managed_owner_scoped_routes():
    body = Path("routes/imps_routes.py").read_text(encoding="utf-8")
    assert "ModelEndpoint" not in body
    assert "Direct image endpoints are retired" in body
    assert 'require_privilege(request, "can_generate_images")' in body
    for operation in ("image.inpaint", "image.img2img", "image.segment", "image.remove_background"):
        assert operation in body


def test_research_endpoint_resolution_uses_managed_owner_binding():
    body = Path("routes/research/research_routes.py").read_text(encoding="utf-8")

    assert "def _resolve_research_endpoint(sess, owner:" in body
    assert "managed_route_summary(" in body
    assert 'owner=owner or getattr(sess, "owner", None) or ""' in body
    assert 'purpose="research"' in body
    assert 'operation="chat.complete"' in body
    assert "MANAGED_ENGINE_PUBLIC_URL" in body
    assert "ModelEndpoint" not in body
    assert "resolve_endpoint_runtime" not in body

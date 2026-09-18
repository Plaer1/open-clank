"""Security contract for managed Gallery image operations."""

import ast
import re
from pathlib import Path

import pytest

from routes.gallery import gallery_routes


SRC = Path(__file__).resolve().parents[1] / "routes" / "gallery" / "gallery_routes.py"


def _function_source(name: str) -> str:
    source = SRC.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} is missing")


def test_retired_endpoint_authority_is_rejected():
    with pytest.raises(Exception) as caught:
        gallery_routes._managed_route_id(
            {
                "_endpoint": "http://169.254.169.254/latest/meta-data",
                "model_route_id": "pmr_image",
            }
        )
    assert getattr(caught.value, "status_code", None) == 400


def test_gallery_has_no_provider_transport_or_credential_access():
    source = SRC.read_text(encoding="utf-8")
    for retired in (
        "httpx",
        "ModelEndpoint",
        "api_key",
        "_resolve_vl_model",
        "llm_call",
        "check_outbound_url",
        "_join_checked_gallery_endpoint",
    ):
        assert retired not in source


def test_every_model_backed_editor_action_uses_managed_transform():
    expected = {
        "gallery_ai_upscale": "image.upscale",
        "gallery_style_transfer": "image.img2img",
        "inpaint_proxy": "image.inpaint",
        "harmonize_image": "image.img2img",
        "denoise_image": "image.denoise",
        "upscale_image_local": "image.upscale",
        "smart_mask": "image.segment",
        "remove_background": "image.remove_background",
        "enhance_face": "image.restore_face",
    }
    for function, operation in expected.items():
        body = _function_source(function)
        assert "_managed_gallery_transform" in body
        assert f'operation="{operation}"' in body


def test_no_raw_exception_string_in_client_responses():
    source = SRC.read_text(encoding="utf-8")
    for pattern in (
        r'return \{"error": str\(',
        r'HTTPException\(\d+, str\(',
        r'HTTPException\(\d+, f"[^"]*\{exc\}',
    ):
        assert not re.findall(pattern, source)

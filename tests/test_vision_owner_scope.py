"""Managed vision operations retain owner, root, grant, and route scope."""

from pathlib import Path

import pytest

from src import document_processor as dp
from src import chat_handler
from src.openclank import modality_facade
from src.openclank.operation_router import ManagedOperationResult


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_vision_analysis_uses_managed_owner_route_and_affinity(monkeypatch, tmp_path):
    seen = {}

    async def describe(**kwargs):
        seen.update(kwargs)
        return ManagedOperationResult(
            operation_id="op_vision",
            root_operation_id="turn_root",
            operation="vision.describe",
            state="complete",
            committed=True,
            replayed=False,
            model_route_id="pmr_vision",
            connection_id="pcn_vision",
            billing_lane="subscription",
            output={"text": "managed description"},
            artifacts=(),
        )

    monkeypatch.setattr(dp, "_load_vl_settings", lambda: {"vision_enabled": True})
    monkeypatch.setattr(modality_facade, "describe_image_path", describe)
    image = tmp_path / "image.png"
    image.write_bytes(b"image")

    result = await dp.analyze_image_with_vl_result_async(
        str(image),
        owner="alice",
        root_operation_id="turn_root",
        grant_id="grant-1",
        model_route_id="pmr_vision",
    )

    assert result == {"text": "managed description", "model": "pmr_vision"}
    assert seen["owner"] == "alice"
    assert seen["root_operation_id"] == "turn_root"
    assert seen["grant_id"] == "grant-1"
    assert seen["model_route_id"] == "pmr_vision"


def test_request_vision_call_sites_use_managed_facade():
    processor_source = (ROOT / "src" / "document_processor.py").read_text()
    upload_source = (ROOT / "routes" / "upload_routes.py").read_text()
    document_source = (ROOT / "routes" / "document_routes.py").read_text()
    gallery_source = (
        ROOT / "routes" / "gallery" / "gallery_routes.py"
    ).read_text()

    assert "analyze_image_with_vl_result_async" in upload_source
    assert "describe_image_path" in processor_source
    assert "root_operation_id=root_operation_id" in processor_source
    assert "describe_image(" in document_source
    assert "describe_image(" in gallery_source
    for source in (processor_source, gallery_source):
        assert "_resolve_vl_model" not in source
        assert "llm_call" not in source


def test_gallery_caption_sync_requires_a_durable_owner(monkeypatch):
    from core import database

    monkeypatch.setenv("AUTH_ENABLED", "true")

    def unexpected_session():
        raise AssertionError("ownerless caption sync must not query Gallery")

    monkeypatch.setattr(database, "SessionLocal", unexpected_session)

    chat_handler._sync_upload_vision_to_gallery(
        {"hash": "same-photo"},
        None,
        "private caption",
    )


def test_gallery_caption_sync_has_an_unconditional_owner_predicate():
    chat_source = (ROOT / "src" / "chat_handler.py").read_text()
    upload_source = (ROOT / "routes" / "upload_routes.py").read_text()

    assert "GalleryImage.owner == owner_key" in chat_source
    assert "GalleryImage.owner == owner_key" in upload_source
    assert "if owner:\n                q = q.filter(GalleryImage.owner" not in chat_source
    assert "if owner:\n                q = q.filter(GalleryImage.owner" not in upload_source

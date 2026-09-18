"""Managed image generation returns only host-owned artifact URLs."""

from src import ai_interaction
from src.openclank import modality_facade
from src.openclank.operation_router import ManagedOperationResult


async def test_generate_image_never_accepts_or_downloads_provider_urls(monkeypatch):
    seen = {}

    async def generate(**kwargs):
        seen.update(kwargs)
        return (
            b"managed",
            "image/png",
            ManagedOperationResult(
                operation_id="op",
                root_operation_id="root",
                operation="image.generate",
                state="complete",
                committed=True,
                replayed=False,
                model_route_id="pmr_image",
                connection_id="pcn_image",
                billing_lane="metered_api",
                output={},
                artifacts=(),
            ),
        )

    monkeypatch.setattr(modality_facade, "generate_image", generate)
    monkeypatch.setattr(
        ai_interaction,
        "_save_managed_gallery_image",
        lambda **_kwargs: ("/api/generated-image/managed.png", "gallery-1"),
    )

    result = await ai_interaction.do_generate_image(
        "draw a chair\npmr_image",
        owner="alice",
    )

    assert result["image_url"] == "/api/generated-image/managed.png"
    assert result["image_model"] == "pmr_image"
    assert seen["owner"] == "alice"
    assert "url" not in seen
    assert "headers" not in seen

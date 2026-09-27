"""Coordinate and revision helpers shared by the native OCR boundary."""

from __future__ import annotations

from typing import Mapping


def vision_box_to_original(
    box: Mapping[str, float],
    *,
    width: int,
    height: int,
    crop: tuple[float, float, float, float] = (0, 0, 0, 0),
    scale: float = 1.0,
) -> dict[str, float]:
    """Convert Vision lower-left normalized coordinates to original pixels."""
    if width <= 0 or height <= 0 or scale <= 0:
        raise ValueError("image dimensions and scale must be positive")
    crop_x, crop_y, _, _ = crop
    return {
        "x": crop_x + float(box["x"]) * width / scale,
        "y": crop_y + (1 - float(box["y"]) - float(box["height"])) * height / scale,
        "width": float(box["width"]) * width / scale,
        "height": float(box["height"]) * height / scale,
    }


def corrected_revision(original_id: str, revision: int, text: str, boxes: list[dict]) -> dict:
    if not original_id or revision < 1:
        raise ValueError("corrections require an immutable original and positive revision")
    return {"original_id": original_id, "revision": revision, "source": "user", "text": text, "boxes": boxes}

"""Owner-bound managed image editing tools."""

from __future__ import annotations

import base64
import hashlib
from typing import Dict, Optional

from src.tools._common import _parse_tool_args


async def do_edit_image(
    content: str,
    owner: Optional[str] = None,
    *,
    root_operation_id: Optional[str] = None,
    grant_id: Optional[str] = None,
) -> Dict:
    """Edit a Files image through the typed MiMo operation router."""

    from src.ai_interaction import _save_managed_files_image
    from src.generated_images import gallery_owner_key, resolve_gallery_image_path
    from src.openclank.files_image_store import FilesImageStore
    from src.openclank.modality_facade import transform_image
    from src.openclank.operation_router import (
        ManagedOperationDenied,
        ManagedOperationUnavailable,
    )

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    image_id = str(args.get("image_id") or "").strip()
    action = str(args.get("action") or "").strip().lower()
    if not image_id or action not in {"upscale", "rembg", "inpaint", "harmonize"}:
        return {
            "error": "image_id and a supported action are required",
            "exit_code": 1,
        }

    owner_key = gallery_owner_key(owner)
    if owner_key is None:
        return {"error": "Files image not found", "exit_code": 1}

    try:
        source = FilesImageStore().image(owner_key, image_id)
    except Exception:
        return {"error": "Files image not found", "exit_code": 1}
    filename = source.locator
    session_id = next(iter((source.provenance or {}).get("session_ids", [])), None)

    try:
        path = resolve_gallery_image_path(filename, require_exists=True)
        image = path.read_bytes()
    except Exception:
        return {"error": "Files image file not found", "exit_code": 1}

    operation = {
        "upscale": "image.upscale",
        "rembg": "image.remove_background",
        "inpaint": "image.inpaint",
        "harmonize": "image.img2img",
    }[action]
    prompt = str(args.get("prompt") or "").strip()
    mask = None
    if action == "inpaint":
        encoded_mask = str(args.get("mask") or "").strip()
        if not prompt or not encoded_mask:
            return {
                "error": "inpaint requires prompt and mask",
                "exit_code": 1,
            }
        if encoded_mask.startswith("data:") and "," in encoded_mask:
            encoded_mask = encoded_mask.split(",", 1)[1]
        try:
            mask = base64.b64decode(encoded_mask, validate=True)
        except Exception:
            return {"error": "inpaint mask is invalid", "exit_code": 1}
        if not mask or len(mask) > 32 * 1024 * 1024:
            return {"error": "inpaint mask exceeds its size limit", "exit_code": 1}

    try:
        scale = int(args.get("scale") or 2)
    except (TypeError, ValueError):
        scale = 2
    input_payload = {
        "prompt": (
            prompt
            or (
                "Remove the background and preserve the foreground"
                if action == "rembg"
                else "Preserve the image while applying the requested transform"
            )
        ),
        "scale": 2 if scale not in (2, 4) else scale,
        "strength": 0.45,
    }
    try:
        output, media_type, result = await transform_image(
            owner=owner_key,
            operation=operation,
            image=image,
            media_type=(
                "image/jpeg"
                if image.startswith(b"\xff\xd8\xff")
                else "image/webp"
                if image.startswith(b"RIFF") and image[8:12] == b"WEBP"
                else "image/png"
            ),
            input=input_payload,
            mask=mask,
            model_route_id=(
                str(args.get("model_route_id") or "").strip() or None
            ),
            grant_id=grant_id,
            root_operation_id=root_operation_id,
            idempotency_key=(
                "tool_image_" + hashlib.sha256(
                    f"{root_operation_id}\0{image_id}\0{action}\0{content}".encode(
                        "utf-8"
                    )
                ).hexdigest()
                if root_operation_id
                else None
            ),
        )
    except ManagedOperationDenied:
        return {"error": "The selected image route denied this edit", "exit_code": 1}
    except ManagedOperationUnavailable:
        return {"error": "No managed Images route supports this edit", "exit_code": 1}
    except Exception:
        return {"error": "Managed image edit failed", "exit_code": 1}

    image_url, new_id = _save_managed_files_image(
        image_bytes=output,
        media_type=media_type,
        prompt=prompt or action,
        model_route_id=result.model_route_id,
        size="",
        quality="",
        session_id=session_id,
        owner=owner_key,
    )
    return {
        "output": f"Image edited ({action}). New image ID: {new_id or '?'}",
        "exit_code": 0,
        "image_id": new_id,
        "image_url": image_url,
        "image_prompt": prompt or action,
        "image_model": result.model_route_id,
        "image_size": "",
        "image_quality": "",
    }

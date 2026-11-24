"""Imps image-transform routes, independent of the retired Gallery authority."""
from __future__ import annotations

import base64
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_privilege
from src.generated_images import gallery_owner_key
from src.openclank.modality_facade import transform_image
from src.openclank.operation_router import ManagedOperationDenied, ManagedOperationUnavailable
from src.upload_limits import GALLERY_TRANSFORM_UPLOAD_MAX_BYTES, read_upload_limited


def _owner(request: Request) -> str:
    return gallery_owner_key(require_privilege(request, "can_generate_images")) or "local"


def _decode(value: Any, label: str) -> bytes:
    value = str(value or "").strip()
    if value.startswith("data:") and "," in value:
        value = value.split(",", 1)[1]
    try:
        data = base64.b64decode(value, validate=True)
    except Exception:
        raise HTTPException(400, f"Invalid {label}") from None
    if not data:
        raise HTTPException(400, f"Missing {label}")
    if len(data) > GALLERY_TRANSFORM_UPLOAD_MAX_BYTES:
        raise HTTPException(413, f"{label.capitalize()} exceeds its size limit")
    return data


def _media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if data.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")): return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP": return "image/webp"
    return "application/octet-stream"


async def _transform(owner: str, operation: str, image: bytes, input: dict, body: dict, mask: bytes | None = None) -> bytes:
    output, _result = await _transform_with_result(owner, operation, image, input, body, mask)
    return output


async def _transform_with_result(owner: str, operation: str, image: bytes, input: dict, body: dict, mask: bytes | None = None) -> tuple[bytes, Any]:
    route = str(body.get("model_route_id") or body.get("modelRouteID") or body.get("_model") or "").strip() or None
    if body.get("_endpoint") or body.get("endpoint"):
        raise HTTPException(400, "Direct image endpoints are retired; choose a managed model route.")
    try:
        output, _type, result = await transform_image(owner=owner, operation=operation, image=image,
            media_type=_media_type(image), input=input, mask=mask, mask_media_type="image/png",
            model_route_id=route, root_operation_id=body.get("root_operation_id") or body.get("rootOperationID"),
            grant_id=body.get("grant_id") or body.get("grantID"), idempotency_key=body.get("idempotency_key") or body.get("idempotencyKey"))
    except ManagedOperationDenied as exc:
        raise HTTPException(403, f"The selected route cannot perform {operation}") from exc
    except ManagedOperationUnavailable as exc:
        raise HTTPException(503, f"No managed Imps route supports {operation}") from exc
    except Exception as exc:
        raise HTTPException(502, f"Managed Imps image {operation} failed") from exc
    return output, result


def _encoded(data: bytes) -> dict[str, str]: return {"image": base64.b64encode(data).decode("ascii")}


def setup_imps_routes() -> APIRouter:
    router = APIRouter(tags=["imps"])

    async def body(request: Request) -> dict:
        payload = await request.json()
        if not isinstance(payload, dict): raise HTTPException(400, "Request body must be an object")
        return payload

    @router.post("/api/image/inpaint")
    async def inpaint(request: Request):
        owner, p = _owner(request), await body(request); image, mask = _decode(p.get("image"), "image"), _decode(p.get("mask"), "mask")
        try: width, height, strength, steps, feather = max(64,min(4096,int(p.get("width") or 1024))), max(64,min(4096,int(p.get("height") or 1024))), max(0,min(1,float(p.get("strength",.75)))), max(1,min(100,int(p.get("steps") or 12))), max(0,min(60,int(p.get("feather") or 8)))
        except (TypeError, ValueError): raise HTTPException(400, "Invalid inpaint options") from None
        return _encoded(await _transform(owner, "image.inpaint", image, {"prompt":str(p.get("prompt") or "").strip(),"size":f"{width}x{height}","strength":strength,"steps":steps,"feather":feather}, p, mask))

    @router.post("/api/image/harmonize")
    async def harmonize(request: Request):
        owner, p = _owner(request), await body(request); image = _decode(p.get("image"), "image")
        try: strength=max(0,min(1,float(p.get("strength",.45)))); color=max(0,min(1,float(p.get("color_match",strength)))); seam=max(0,min(1,float(p.get("seam_fix",0))))
        except (TypeError, ValueError): raise HTTPException(400, "Invalid harmonize options") from None
        return _encoded(await _transform(owner,"image.img2img",image,{"prompt":str(p.get("prompt") or "natural lighting, harmonious color, seamless blend").strip(),"strength":strength,"colorMatch":color,"seamFix":seam},p))

    @router.post("/api/imps/style-transfer")
    async def style_transfer(request: Request):
        owner = _owner(request)
        form = await request.form()
        upload = form.get("image")
        prompt = str(form.get("prompt") or "").strip()
        if not upload: raise HTTPException(400, "No image")
        if not prompt: raise HTTPException(400, "Prompt is required")
        try: strength = max(0, min(1, float(form.get("strength", .55))))
        except (TypeError, ValueError): strength = .55
        image = await read_upload_limited(upload, GALLERY_TRANSFORM_UPLOAD_MAX_BYTES, "Image upload")
        return _encoded(await _transform(owner, "image.img2img", image, {"prompt": prompt, "strength": strength}, dict(form)))

    @router.post("/api/image/denoise")
    @router.post("/api/image/upscale-local")
    @router.post("/api/image/remove-bg")
    @router.post("/api/image/enhance-face")
    async def managed_tool(request: Request):
        owner, p = _owner(request), await body(request); image = _decode(p.get("image"), "image"); path=request.url.path
        if path.endswith("denoise"):
            try: input={"strength":max(0,min(1,float(p.get("strength",.5)))),"scale":1}
            except (TypeError,ValueError): raise HTTPException(400,"Invalid denoise strength") from None
            operation="image.denoise"
        elif path.endswith("upscale-local"): operation,input="image.upscale",{"scale": 4 if str(p.get("scale")) == "4" else 2}
        elif path.endswith("remove-bg"): operation,input="image.remove_background",{"prompt":"Remove the background and preserve the foreground"}
        else: operation,input="image.restore_face",{}
        output = await _transform(owner,operation,image,input,p)
        if path.endswith("remove-bg") and p.get("hint_mask"):
            output = _apply_hint_mask(output, _decode(p["hint_mask"], "hint mask"))
        return _encoded(output)

    @router.post("/api/image/mask")
    async def mask(request: Request):
        owner,p=_owner(request),await body(request); image=_decode(p.get("image"),"image"); points,box,query=p.get("points") or [],p.get("box"),str(p.get("text") or p.get("query") or "").strip()
        if not points and not box and not query: raise HTTPException(400,"Provide at least one point, box, or object text")
        output, result = await _transform_with_result(owner,"image.segment",image,{"prompt":f"Return a segmentation mask for {query}" if query else "Return the requested segmentation mask","points":points,"box":box},p)
        try:
            from PIL import Image
            with Image.open(io.BytesIO(output)) as decoded:
                mask=decoded.getchannel("A") if decoded.mode == "RGBA" else decoded.convert("L"); bbox=mask.getbbox(); data=_encoded(_png(mask))["image"]
        except Exception: raise HTTPException(502,"Managed segmentation returned an invalid mask") from None
        return {"mask":data,"bbox":list(bbox) if bbox else None,"model":result.model_route_id,"device":"managed"}

    @router.post("/api/image/sharpen")
    async def sharpen(request: Request):
        require_privilege(request,"can_generate_images"); p=await body(request)
        try:
            from PIL import Image, ImageFilter
            image=Image.open(io.BytesIO(_decode(p.get("image"),"image"))).convert("RGB"); amount=float(p.get("amount",50))/100
            return _encoded(_png(image.filter(ImageFilter.UnsharpMask(radius=2,percent=int(amount*200),threshold=3))))
        except HTTPException: raise
        except Exception: raise HTTPException(400,"Invalid image") from None

    return router


def _png(image) -> bytes:
    out=io.BytesIO(); image.save(out,format="PNG"); return out.getvalue()


def _apply_hint_mask(output: bytes, hint_bytes: bytes) -> bytes:
    try:
        from PIL import Image, ImageChops
        with Image.open(io.BytesIO(output)) as source, Image.open(io.BytesIO(hint_bytes)) as raw_hint:
            result = source.convert("RGBA")
            hint = raw_hint.convert("L")
            if hint.size != result.size:
                hint = hint.resize(result.size, Image.Resampling.NEAREST)
            red, green, blue, alpha = result.split()
            result = Image.merge("RGBA", (red, green, blue, ImageChops.multiply(alpha, hint)))
            return _png(result)
    except Exception:
        raise HTTPException(400, "Invalid hint mask") from None

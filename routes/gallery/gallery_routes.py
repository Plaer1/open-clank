"""Gallery routes — browsable library for photos and AI-generated images."""

import base64
import hashlib
import io
import logging
import re
import uuid
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from core.database import SessionLocal, GalleryImage, GalleryAlbum
from core.database import Session as DbSession
from src.auth_helpers import get_current_user, require_privilege, require_user
from src.upload_limits import (
    read_upload_limited,
    GALLERY_UPLOAD_MAX_BYTES,
    GALLERY_TRANSFORM_UPLOAD_MAX_BYTES,
)
from src.generated_images import (
    GENERATED_IMAGE_DIR,
    LOCAL_GALLERY_OWNER,
    discard_staged_gallery_image,
    gallery_owner_key,
    publish_staged_gallery_image,
    resolve_gallery_image_path,
    stage_gallery_image_bytes,
)
from src.openclank.modality_facade import describe_image, transform_image
from src.openclank.operation_router import (
    ManagedOperationDenied,
    ManagedOperationUnavailable,
)

from routes.gallery.gallery_helpers import (
    GalleryPatch, _extract_exif, _image_to_dict, _owner_filter, _human_size,
)

logger = logging.getLogger(__name__)

def _pil_image_to_b64(img, *, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _sanitize_gallery_filename(filename: str) -> str:
    """Return a local filename safe to join under generated_images."""
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(str(filename or "")).name)[:128]
    if not safe_name or safe_name in {".", ".."}:
        safe_name = uuid.uuid4().hex[:12]
    return safe_name


GALLERY_IMAGE_DIR = GENERATED_IMAGE_DIR


def _gallery_image_path(filename: str, *, require_exists: bool = False) -> Path:
    """Compatibility alias for the shared Gallery storage resolver."""
    return resolve_gallery_image_path(
        filename,
        root=GALLERY_IMAGE_DIR,
        require_exists=require_exists,
    )


def _gallery_owner(request: Request) -> Optional[str]:
    """Resolve a durable principal without opening auth-enabled null states."""

    return gallery_owner_key(get_current_user(request))


def _require_gallery_owner(request: Request) -> str:
    """Require an authorized request and return its durable Gallery owner."""

    owner = _gallery_owner(request)
    if owner:
        return owner
    # ``require_user`` returns an empty string only for an explicitly allowed
    # local mode (auth disabled, loopback first-run, or localhost bypass).
    require_user(request)
    return LOCAL_GALLERY_OWNER


def _require_gallery_privilege(request: Request, privilege: str) -> str:
    owner = gallery_owner_key(require_privilege(request, privilege))
    return owner or LOCAL_GALLERY_OWNER


def _commit_gallery_replacement(
    db,
    *,
    filename: str,
    content: bytes,
    error_message: str,
) -> None:
    """Atomically replace bytes and restore them if metadata commit fails."""

    destination = _gallery_image_path(filename, require_exists=True)
    previous = destination.read_bytes()
    staged = stage_gallery_image_bytes(content, root=GALLERY_IMAGE_DIR)
    published = False
    try:
        publish_staged_gallery_image(
            staged,
            filename,
            root=GALLERY_IMAGE_DIR,
            replace=True,
        )
        published = True
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        if published:
            restore = None
            try:
                restore = stage_gallery_image_bytes(previous, root=GALLERY_IMAGE_DIR)
                publish_staged_gallery_image(
                    restore,
                    filename,
                    root=GALLERY_IMAGE_DIR,
                    replace=True,
                )
            except Exception:
                logger.critical(
                    "Gallery byte rollback failed for %r",
                    filename,
                    exc_info=True,
                )
            finally:
                discard_staged_gallery_image(restore, root=GALLERY_IMAGE_DIR)
        logger.exception("Gallery metadata commit failed for %r", filename)
        raise HTTPException(500, error_message) from None
    finally:
        discard_staged_gallery_image(staged, root=GALLERY_IMAGE_DIR)


def _decode_managed_image(value: Any, label: str = "image") -> bytes:
    raw = str(value or "").strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    if not raw:
        raise HTTPException(400, f"Missing {label}")
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        raise HTTPException(400, f"Invalid {label}") from None
    if not data or len(data) > GALLERY_TRANSFORM_UPLOAD_MAX_BYTES:
        raise HTTPException(413, f"{label.capitalize()} exceeds its size limit")
    return data


def _managed_route_id(payload: Any) -> Optional[str]:
    if not hasattr(payload, "get"):
        return None
    if payload.get("_endpoint") or payload.get("endpoint"):
        raise HTTPException(
            400,
            "Direct image endpoints are retired; choose a managed model route.",
        )
    return str(
        payload.get("model_route_id")
        or payload.get("modelRouteID")
        or payload.get("_model")
        or ""
    ).strip() or None


def _managed_operation_context(payload: Any) -> Dict[str, Optional[str]]:
    """Extract non-secret managed routing context from JSON or form data."""

    if not hasattr(payload, "get"):
        return {
            "root_operation_id": None,
            "grant_id": None,
            "idempotency_key": None,
        }

    def _value(*names: str) -> Optional[str]:
        for name in names:
            candidate = str(payload.get(name) or "").strip()
            if candidate:
                return candidate
        return None

    return {
        "root_operation_id": _value("root_operation_id", "rootOperationID"),
        "grant_id": _value("grant_id", "grantID"),
        "idempotency_key": _value("idempotency_key", "idempotencyKey"),
    }


def _managed_image_media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _managed_image_failure(action: str, exc: Exception) -> HTTPException:
    logger.warning("Managed Gallery %s failed (%s)", action, type(exc).__name__)
    if isinstance(exc, ManagedOperationDenied):
        return HTTPException(403, f"The selected route cannot perform {action}")
    if isinstance(exc, ManagedOperationUnavailable):
        return HTTPException(
            503,
            f"No managed Images route supports {action}",
        )
    return HTTPException(502, f"Managed image {action} failed")


async def _managed_gallery_transform(
    *,
    owner: str | None,
    operation: str,
    image: bytes,
    input: Dict[str, Any],
    model_route_id: Optional[str] = None,
    mask: Optional[bytes] = None,
    root_operation_id: Optional[str] = None,
    grant_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> tuple[bytes, str, Any]:
    try:
        return await transform_image(
            owner=owner or "",
            operation=operation,
            image=image,
            media_type=_managed_image_media_type(image),
            input=input,
            mask=mask,
            mask_media_type="image/png",
            model_route_id=model_route_id,
            root_operation_id=root_operation_id,
            grant_id=grant_id,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        raise _managed_image_failure(operation, exc) from None


def _encoded_image(data: bytes) -> Dict[str, str]:
    return {"image": base64.b64encode(data).decode("ascii")}


def setup_gallery_routes() -> APIRouter:
    router = APIRouter(tags=["gallery"])

    # ---- POST /api/gallery/upload ----
    @router.post("/api/gallery/upload")
    async def gallery_upload(request: Request):
        """Upload an image file to the gallery with EXIF extraction and dedup."""
        import uuid
        from pathlib import Path

        form = await request.form()
        file = form.get("file")
        if not file or not hasattr(file, 'filename'):
            raise HTTPException(400, "No file provided")

        user = _require_gallery_owner(request)
        album_id = form.get("album_id") or None
        content = await read_upload_limited(file, GALLERY_UPLOAD_MAX_BYTES, "Gallery upload")

        # Duplicate detection via SHA-256
        file_hash = hashlib.sha256(content).hexdigest()
        db = SessionLocal()
        try:
            if album_id:
                _get_or_404_album(db, album_id, user)

            # SECURITY: scope the dup-detect to THIS user — otherwise a
            # caller can probe whether someone else uploaded the same
            # file (the response leaks the existing row's id+filename).
            _dup_q = db.query(GalleryImage).filter(
                GalleryImage.file_hash == file_hash,
                GalleryImage.is_active == True,
            )
            _dup_q = _dup_q.filter(GalleryImage.owner == user)
            existing = _dup_q.first()
            if existing:
                return {"ok": False, "duplicate": True, "filename": existing.filename,
                        "id": existing.id, "message": "Duplicate photo skipped"}

            ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "png"
            VIDEO_EXTS = {"mp4", "mov", "webm", "mkv", "m4v"}
            IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}
            if ext not in VIDEO_EXTS and ext not in IMAGE_EXTS:
                raise HTTPException(400, f"Unsupported file type: .{ext}")
            is_video = ext in VIDEO_EXTS
            filename = f"{uuid.uuid4().hex[:12]}.{ext}"

            # Extract EXIF for images only — PIL can't parse video containers
            # and the failure path logs a noisy WARNING. We'll add ffprobe-based
            # video metadata extraction in a follow-up.
            exif = {} if is_video else _extract_exif(content)
            original_name = file.filename.rsplit(".", 1)[0] if "." in file.filename else file.filename

            img_id = str(uuid.uuid4())
            staged = stage_gallery_image_bytes(content, root=GALLERY_IMAGE_DIR)
            image = GalleryImage(
                id=img_id,
                filename=filename,
                prompt=original_name,
                model="imported",
                owner=user,
                file_hash=file_hash,
                file_size=len(content),
                width=exif.get("width"),
                height=exif.get("height"),
                taken_at=exif.get("taken_at"),
                camera_make=exif.get("camera_make"),
                camera_model=exif.get("camera_model"),
                gps_lat=exif.get("gps_lat"),
                gps_lng=exif.get("gps_lng"),
                album_id=album_id,
            )
            try:
                db.add(image)
                db.commit()
                try:
                    publish_staged_gallery_image(
                        staged,
                        filename,
                        root=GALLERY_IMAGE_DIR,
                    )
                except Exception:
                    # A committed row without bytes is fail-closed, but remove
                    # it immediately so a retry does not inherit a phantom.
                    try:
                        db.delete(image)
                        db.commit()
                    except Exception:
                        db.rollback()
                        logger.exception(
                            "gallery_upload: failed to retract unpublished metadata"
                        )
                    raise HTTPException(500, "Gallery image publication failed") from None
            except HTTPException:
                raise
            except Exception:
                db.rollback()
                logger.exception("gallery_upload: metadata commit failed")
                raise HTTPException(500, "Gallery image publication failed") from None
            finally:
                discard_staged_gallery_image(staged, root=GALLERY_IMAGE_DIR)
            resp = {"ok": True, "filename": filename, "id": img_id}
            if exif.get("exif_error"):
                resp["exif_warning"] = exif["exif_error"]
            return resp
        finally:
            db.close()

    # ---- POST /api/gallery/{id}/replace ----
    @router.post("/api/gallery/{image_id}/replace")
    async def gallery_replace(request: Request, image_id: str):
        """Replace an existing gallery image file with a new one."""
        user = _require_gallery_owner(request)
        db = SessionLocal()
        try:
            img = db.query(GalleryImage).filter(
                GalleryImage.id == image_id,
                GalleryImage.owner == user,
                GalleryImage.is_active == True,
            ).first()
            if not img:
                raise HTTPException(404, "Image not found")

            form = await request.form()
            file = form.get("image")
            if not file or not hasattr(file, 'read'):
                raise HTTPException(400, "No image provided")

            content = await read_upload_limited(file, GALLERY_UPLOAD_MAX_BYTES, "Gallery replacement")
            # Refresh dimensions in case the editor resized the canvas.
            # updated_at auto-bumps via TimestampMixin's onupdate hook.
            try:
                from PIL import Image
                from io import BytesIO
                with Image.open(BytesIO(content)) as new_im:
                    img.width = new_im.width
                    img.height = new_im.height
            except Exception:
                pass
            img.file_hash = hashlib.sha256(content).hexdigest()
            img.file_size = len(content)
            _commit_gallery_replacement(
                db,
                filename=img.filename,
                content=content,
                error_message="Image update failed",
            )
            return {"ok": True, "width": img.width, "height": img.height}
        finally:
            db.close()

    # ---- POST /api/gallery/{image_id}/rename ----
    @router.post("/api/gallery/{image_id}/rename")
    async def gallery_rename(request: Request, image_id: str):
        """Rename a gallery photo. Stores the new name in the `prompt`
        column (which serves as the user-facing label for uploaded
        photos that have no AI prompt)."""
        user = _require_gallery_owner(request)
        data = await request.json()
        new_name = (data.get("name") or "").strip()
        if not new_name:
            raise HTTPException(400, "Name cannot be empty")
        if len(new_name) > 500:
            raise HTTPException(400, "Name too long")
        db = SessionLocal()
        try:
            img = db.query(GalleryImage).filter(
                GalleryImage.id == image_id,
                GalleryImage.owner == user,
                GalleryImage.is_active == True,
            ).first()
            if not img:
                raise HTTPException(404, "Image not found")
            img.prompt = new_name
            db.commit()
            return {"ok": True, "name": new_name}
        finally:
            db.close()

    # ---- POST /api/gallery/{image_id}/rotate ----
    @router.post("/api/gallery/{image_id}/rotate")
    async def gallery_rotate(request: Request, image_id: str):
        """Rotate an image by ±90° or 180°. Updates the file on disk and the
        width/height in the DB. Body: {angle: 90 | -90 | 180}."""
        from pathlib import Path
        from PIL import Image
        from io import BytesIO

        data = await request.json()
        try:
            angle = int(data.get("angle", 90))
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid angle")
        if angle not in (90, -90, 180, 270):
            raise HTTPException(400, "Angle must be 90, -90, 180, or 270")

        user = _require_gallery_owner(request)
        db = SessionLocal()
        try:
            img = db.query(GalleryImage).filter(
                GalleryImage.id == image_id,
                GalleryImage.owner == user,
                GalleryImage.is_active == True,
            ).first()
            if not img:
                raise HTTPException(404, "Image not found")

            img_path = _gallery_image_path(img.filename, require_exists=True)
            previous = img_path.read_bytes()

            # PIL rotates counter-clockwise; the API takes "clockwise"
            # convention so we negate to match user expectation.
            with Image.open(BytesIO(previous)) as pil:
                rotated = pil.rotate(-angle, expand=True)
                # Recompute hash so dedupe stays accurate.
                buf = BytesIO()
                ext = img.filename.rsplit(".", 1)[-1].lower()
                save_kwargs = {}
                if ext in ("jpg", "jpeg"):
                    save_kwargs["quality"] = 95
                    fmt = "JPEG"
                elif ext == "webp":
                    fmt = "WEBP"
                    save_kwargs["quality"] = 95
                else:
                    fmt = "PNG"
                rotated.save(buf, format=fmt, **save_kwargs)
                content = buf.getvalue()
                img.file_hash = hashlib.sha256(content).hexdigest()
                img.file_size = len(content)
                img.width, img.height = rotated.size
            _commit_gallery_replacement(
                db,
                filename=img.filename,
                content=content,
                error_message="Image rotation failed",
            )
            return {"ok": True, "width": img.width, "height": img.height}
        finally:
            db.close()

    # ---- Managed Gallery model transforms ----
    @router.post("/api/gallery/ai-upscale")
    async def gallery_ai_upscale(request: Request):
        user = _require_gallery_privilege(request, "can_generate_images")
        form = await request.form()
        file = form.get("image")
        if not file:
            raise HTTPException(400, "No image")
        try:
            scale = int(form.get("scale", "2"))
        except (TypeError, ValueError):
            scale = 2
        scale = 2 if scale not in (2, 4) else scale
        image_bytes = await read_upload_limited(
            file,
            GALLERY_TRANSFORM_UPLOAD_MAX_BYTES,
            "Image upload",
        )
        output, _media_type, _result = await _managed_gallery_transform(
            owner=user,
            operation="image.upscale",
            image=image_bytes,
            input={"scale": scale},
            model_route_id=_managed_route_id(dict(form)),
            **_managed_operation_context(form),
        )
        return _encoded_image(output)

    @router.post("/api/gallery/style-transfer")
    async def gallery_style_transfer(request: Request):
        user = _require_gallery_privilege(request, "can_generate_images")
        form = await request.form()
        file = form.get("image")
        if not file:
            raise HTTPException(400, "No image")
        prompt = str(form.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(400, "Prompt is required")
        try:
            strength = float(form.get("strength", "0.55"))
        except (TypeError, ValueError):
            strength = 0.55
        strength = max(0.0, min(1.0, strength))
        image_bytes = await read_upload_limited(
            file,
            GALLERY_TRANSFORM_UPLOAD_MAX_BYTES,
            "Image upload",
        )
        output, _media_type, _result = await _managed_gallery_transform(
            owner=user,
            operation="image.img2img",
            image=image_bytes,
            input={"prompt": prompt, "strength": strength},
            model_route_id=_managed_route_id(dict(form)),
            **_managed_operation_context(form),
        )
        return _encoded_image(output)

    # ---- GET /api/gallery/tags ----
    @router.get("/api/gallery/tags")
    async def gallery_tags(request: Request) -> Dict[str, Any]:
        """Return distinct tags across all active gallery images."""
        user = _gallery_owner(request)
        db = SessionLocal()
        try:
            q = db.query(GalleryImage.tags).filter(
                GalleryImage.is_active == True, GalleryImage.tags != None, GalleryImage.tags != ""
            )
            q = _owner_filter(q, user)
            rows = q.all()
            tag_set = set()
            for (raw,) in rows:
                for t in raw.split(","):
                    t = t.strip()
                    if t:
                        tag_set.add(t)
            return {"tags": sorted(tag_set)}
        finally:
            db.close()

    # ---- GET /api/gallery/library ----
    @router.get("/api/gallery/library")
    async def gallery_library(
        request: Request,
        search: Optional[str] = Query(None),
        tag: Optional[str] = Query(None),
        model: Optional[str] = Query(None),
        album: Optional[str] = Query(None),
        favorites: bool = Query(False),
        sort: str = Query("recent"),
        seed: Optional[int] = Query(None),
        offset: int = Query(0, ge=0),
        limit: int = Query(24, ge=1, le=100),
    ) -> Dict[str, Any]:
        user = _gallery_owner(request)
        db = SessionLocal()
        try:
            # Distinct tags for filter UI
            tag_q = db.query(GalleryImage.tags).filter(
                GalleryImage.is_active == True, GalleryImage.tags != None, GalleryImage.tags != ""
            )
            tag_q = _owner_filter(tag_q, user)
            tag_rows = tag_q.all()
            all_tags = set()
            for (raw,) in tag_rows:
                for t in raw.split(","):
                    t = t.strip()
                    if t:
                        all_tags.add(t)

            # Distinct models for filter UI
            model_q = db.query(GalleryImage.model).filter(
                GalleryImage.is_active == True, GalleryImage.model != None
            )
            model_q = _owner_filter(model_q, user)
            model_rows = model_q.distinct().all()
            all_models = sorted([m for (m,) in model_rows if m])

            # Base query with left join to sessions for session_name
            q = (
                db.query(GalleryImage, DbSession.name)
                .outerjoin(DbSession, GalleryImage.session_id == DbSession.id)
                .filter(GalleryImage.is_active == True)
            )
            q = _owner_filter(q, user)

            # Search filter (prompt + tags + ai_tags)
            if search:
                term = f"%{search}%"
                from sqlalchemy import or_
                q = q.filter(or_(
                    GalleryImage.prompt.ilike(term),
                    GalleryImage.tags.ilike(term),
                    GalleryImage.ai_tags.ilike(term),
                ))

            # Tag filter. The UI stacks multiple tag pills by passing them
            # comma-separated — each tag adds a separate AND-filter so the
            # result set narrows as the user piles tags on. A single tag
            # (no commas) is the original behaviour.
            if tag:
                from sqlalchemy import or_ as _or
                for one in (t.strip() for t in tag.split(",")):
                    if not one:
                        continue
                    q = q.filter(_or(
                        GalleryImage.tags.ilike(f"%{one}%"),
                        GalleryImage.ai_tags.ilike(f"%{one}%"),
                    ))

            # Model filter
            if model:
                q = q.filter(GalleryImage.model == model)

            # Album filter
            if album:
                q = q.filter(GalleryImage.album_id == album)

            # Favorites filter
            if favorites:
                q = q.filter(GalleryImage.favorite == True)

            # Total before pagination
            total = q.count()
            # How many of those have AI tags — surfaced as "X/Y photos tagged"
            # in the AI-tagging settings header.
            total_tagged = q.filter(
                GalleryImage.ai_tags.isnot(None), GalleryImage.ai_tags != ""
            ).count()

            # Sorting
            if sort == "shuffle":
                # Seeded shuffle: fetch all matching IDs, shuffle them
                # deterministically with `seed`, then re-query for just the
                # page we want. Stable across pagination as long as the
                # client keeps the same seed.
                import random as _random
                id_rows = q.with_entities(GalleryImage.id).all()
                all_ids = [r[0] for r in id_rows]
                rng = _random.Random(seed if seed is not None else 0)
                rng.shuffle(all_ids)
                page_ids = all_ids[offset:offset + limit]
                if page_ids:
                    page_q = (
                        db.query(GalleryImage, DbSession.name)
                        .outerjoin(DbSession, GalleryImage.session_id == DbSession.id)
                        .filter(GalleryImage.id.in_(page_ids))
                    )
                    page_rows = _owner_filter(page_q, user).all()
                    # Restore the shuffled order
                    by_id = {img.id: (img, session_name) for img, session_name in page_rows}
                    rows = [by_id[i] for i in page_ids if i in by_id]
                else:
                    rows = []
            else:
                if sort == "oldest":
                    q = q.order_by(GalleryImage.created_at.asc())
                else:  # recent
                    q = q.order_by(GalleryImage.created_at.desc())
                rows = q.offset(offset).limit(limit).all()

            items = []
            for img, session_name in rows:
                items.append(_image_to_dict(img, session_name))

            return {
                "items": items,
                "total": total,
                "total_tagged": total_tagged,
                "tags": sorted(all_tags),
                "models": all_models,
            }
        except Exception:
            logger.exception("Failed to fetch gallery library")
            raise HTTPException(500, "Failed to fetch gallery library")
        finally:
            db.close()

    # ---- Album CRUD (must be before {image_id} catch-all) ----

    @router.get("/api/gallery/albums")
    async def list_albums(request: Request):
        user = _gallery_owner(request)
        db = SessionLocal()
        try:
            q = db.query(GalleryAlbum)
            q = _owner_filter(q, user, GalleryAlbum)
            albums = q.order_by(GalleryAlbum.created_at.desc()).all()
            result = []
            for a in albums:
                _count_q = db.query(GalleryImage).filter(
                    GalleryImage.album_id == a.id, GalleryImage.is_active == True
                )
                _count_q = _owner_filter(_count_q, user)
                count = _count_q.count()
                cover_url = None
                if a.cover_id:
                    cover_q = db.query(GalleryImage).filter(GalleryImage.id == a.cover_id)
                    cover = _owner_filter(cover_q, user).first()
                    if cover:
                        cover_url = f"/api/generated-image/{cover.filename}"
                elif count > 0:
                    _cover_q = db.query(GalleryImage).filter(
                        GalleryImage.album_id == a.id, GalleryImage.is_active == True
                    )
                    _cover_q = _owner_filter(_cover_q, user)
                    first = _cover_q.order_by(GalleryImage.created_at.desc()).first()
                    if first:
                        cover_url = f"/api/generated-image/{first.filename}"
                result.append({
                    "id": a.id, "name": a.name, "description": a.description or "",
                    "cover_url": cover_url, "count": count,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                })
            return {"albums": result}
        finally:
            db.close()

    @router.post("/api/gallery/albums")
    async def create_album(request: Request):
        import uuid
        user = _require_gallery_owner(request)
        data = await request.json()
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "Album name required")
        db = SessionLocal()
        try:
            a = GalleryAlbum(
                id=str(uuid.uuid4()), name=name,
                description=data.get("description", ""),
                owner=user,
            )
            db.add(a)
            db.commit()
            return {"ok": True, "id": a.id, "name": a.name}
        finally:
            db.close()

    @router.get("/api/gallery/stats")
    async def gallery_stats(request: Request):
        user = _gallery_owner(request)
        db = SessionLocal()
        try:
            from sqlalchemy import func
            base = db.query(GalleryImage).filter(GalleryImage.is_active == True)
            size_q = db.query(func.sum(GalleryImage.file_size)).filter(GalleryImage.is_active == True)
            album_q = db.query(GalleryAlbum)
            base = _owner_filter(base, user)
            size_q = _owner_filter(size_q, user)
            album_q = _owner_filter(album_q, user, GalleryAlbum)
            total = base.count()
            total_size = size_q.scalar() or 0
            fav_count = base.filter(GalleryImage.favorite == True).count()
            album_count = album_q.count()
            return {
                "total_photos": total,
                "total_size": total_size,
                "total_size_human": _human_size(total_size),
                "favorites": fav_count,
                "albums": album_count,
            }
        finally:
            db.close()

    @router.post("/api/gallery/ai-tag-batch")
    async def ai_tag_batch(
        request: Request,
        album_id: Optional[str] = Query(None),
        limit: int = Query(200),
    ):
        user = _gallery_owner(request)
        db = SessionLocal()
        try:
            q = db.query(GalleryImage).filter(
                GalleryImage.is_active == True,
                (GalleryImage.ai_tags == None) | (GalleryImage.ai_tags == ""),
            )
            q = _owner_filter(q, user)
            if album_id:
                q = q.filter(GalleryImage.album_id == album_id)
            untagged = q.count()
            ids = [img.id for img in q.limit(max(1, min(limit, 500))).all()]
            return {"ok": True, "queued": len(ids), "total_untagged": untagged, "image_ids": ids}
        finally:
            db.close()

    # ---- GET /api/gallery/{image_id} ----
    @router.get("/api/gallery/{image_id}")
    async def get_gallery_image(request: Request, image_id: str) -> Dict[str, Any]:
        user = _gallery_owner(request)
        if user is None:
            raise HTTPException(404, "Image not found")
        db = SessionLocal()
        try:
            row = (
                db.query(GalleryImage, DbSession.name)
                .outerjoin(DbSession, GalleryImage.session_id == DbSession.id)
                .filter(
                    GalleryImage.id == image_id,
                    GalleryImage.owner == user,
                    GalleryImage.is_active == True,
                )
                .first()
            )
            if not row:
                raise HTTPException(404, "Image not found")
            img, session_name = row
            return _image_to_dict(img, session_name)
        finally:
            db.close()

    # ---- PATCH /api/gallery/{image_id} ----
    @router.patch("/api/gallery/{image_id}")
    async def patch_gallery_image(request: Request, image_id: str, req: GalleryPatch) -> Dict[str, Any]:
        user = _require_gallery_owner(request)
        db = SessionLocal()
        try:
            img = db.query(GalleryImage).filter(
                GalleryImage.id == image_id,
                GalleryImage.owner == user,
                GalleryImage.is_active == True,
            ).first()
            if not img:
                raise HTTPException(404, "Image not found")
            if req.tags is not None:
                # Drop any tag from the user-tags field that already lives in
                # ai_tags — earlier flows wrote AI suggestions to both fields
                # and the UI showed every photo with the same chips twice.
                ai_set = {t.strip().lower() for t in (img.ai_tags or '').split(',') if t.strip()}
                cleaned = []
                seen = set()
                for raw in (req.tags or '').split(','):
                    t = raw.strip()
                    k = t.lower()
                    if not t or k in seen or k in ai_set:
                        continue
                    seen.add(k)
                    cleaned.append(t)
                img.tags = ', '.join(cleaned)
            if req.favorite is not None:
                img.favorite = req.favorite
            if req.album_id is not None:
                if req.album_id:
                    # Validate the target album belongs to the caller before
                    # moving the image into it — mirrors add_to_album, so you
                    # cannot file your image into another user's album.
                    _get_or_404_album(db, req.album_id, user)
                    img.album_id = req.album_id
                else:
                    img.album_id = None
            db.commit()
            db.refresh(img)
            return _image_to_dict(img)
        except HTTPException:
            raise
        except Exception:
            db.rollback()
            logger.exception("patch_gallery_image: update failed")
            raise HTTPException(500, "Image update failed")
        finally:
            db.close()

    # ---- POST /api/gallery/download-zip ----
    # Bundle the given image ids into a single .zip for download. Used by the
    # gallery's bulk "Download" when many photos are selected (one file instead
    # of a flood of individual downloads).
    @router.post("/api/gallery/download-zip")
    async def gallery_download_zip(request: Request):
        user = _require_gallery_owner(request)
        try:
            data = await request.json()
        except Exception:
            data = {}
        ids = data.get("ids") or []
        if not ids:
            raise HTTPException(400, "No images specified")
        db = SessionLocal()
        try:
            imgs = db.query(GalleryImage).filter(
                GalleryImage.id.in_(ids),
                GalleryImage.owner == user,
                GalleryImage.is_active == True,
            ).all()
            if not imgs:
                raise HTTPException(404, "No images found")
            import io
            import re
            import zipfile
            buf = io.BytesIO()
            used = set()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for img in imgs:
                    try:
                        src = _gallery_image_path(img.filename, require_exists=True)
                    except HTTPException:
                        continue
                    ext = src.suffix or ".png"
                    base = (img.prompt or "").strip() or src.stem
                    base = re.sub(r"[^\w\-. ]+", "", base)[:60].strip() or img.id
                    name = f"{base}{ext}"
                    i = 1
                    while name in used:
                        name = f"{base}-{i}{ext}"
                        i += 1
                    used.add(name)
                    zf.write(src, arcname=name)
            if not used:
                raise HTTPException(404, "No image files found on disk")
            from fastapi import Response
            return Response(
                content=buf.getvalue(),
                media_type="application/zip",
                headers={
                    "Content-Disposition": 'attachment; filename="gallery-photos.zip"',
                    "Cache-Control": "private, no-store",
                    "Pragma": "no-cache",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        finally:
            db.close()

    # ---- POST /api/gallery/clear-user-tags ----
    # Wipe the `tags` field on every image owned by the current user.
    # Leaves `ai_tags` intact. Use after a bug populated user-tags with
    # AI-suggested values you never added.
    @router.post("/api/gallery/clear-user-tags")
    async def clear_gallery_user_tags(request: Request) -> Dict[str, Any]:
        user = _require_gallery_owner(request)
        db = SessionLocal()
        try:
            q = db.query(GalleryImage).filter(GalleryImage.is_active == True)
            q = _owner_filter(q, user)
            cleared = 0
            for img in q.all():
                if img.tags:
                    img.tags = ''
                    cleared += 1
            db.commit()
            return {"ok": True, "cleared": cleared}
        except Exception:
            db.rollback()
            logger.exception("clear_gallery_user_tags: failed")
            raise HTTPException(500, "Tag update failed")
        finally:
            db.close()

    # ---- POST /api/gallery/clear-ai-tags ----
    # Wipe the `ai_tags` field on every image owned by the current user.
    # Leaves user `tags` intact. Use when AI-suggested tags like "dog" /
    # "woman" have leaked into the gallery and you want them gone.
    @router.post("/api/gallery/clear-ai-tags")
    async def clear_gallery_ai_tags(request: Request, image_id: Optional[str] = Query(None)) -> Dict[str, Any]:
        user = _require_gallery_owner(request)
        db = SessionLocal()
        try:
            q = db.query(GalleryImage).filter(GalleryImage.is_active == True)
            q = _owner_filter(q, user)
            if image_id:  # clear just one photo's AI tags
                q = q.filter(GalleryImage.id == image_id)
            cleared = 0
            for img in q.all():
                if img.ai_tags:
                    img.ai_tags = ''
                    cleared += 1
            db.commit()
            return {"ok": True, "cleared": cleared}
        except Exception:
            db.rollback()
            logger.exception("clear_gallery_ai_tags: failed")
            raise HTTPException(500, "Tag update failed")
        finally:
            db.close()

    # ---- POST /api/gallery/dedupe-tags ----
    # One-shot cleanup: for every image owned by the current user, drop any
    # tag from `tags` that also appears in `ai_tags` (case-insensitive).
    # Returns how many rows were touched + how many tags removed.
    @router.post("/api/gallery/dedupe-tags")
    async def dedupe_gallery_tags(request: Request) -> Dict[str, Any]:
        user = _require_gallery_owner(request)
        db = SessionLocal()
        try:
            q = db.query(GalleryImage).filter(GalleryImage.is_active == True)
            q = _owner_filter(q, user)
            rows_touched = 0
            tags_removed = 0
            for img in q.all():
                ai_set = {t.strip().lower() for t in (img.ai_tags or '').split(',') if t.strip()}
                if not ai_set:
                    continue
                original = [t.strip() for t in (img.tags or '').split(',') if t.strip()]
                cleaned = []
                seen = set()
                for t in original:
                    k = t.lower()
                    if k in ai_set or k in seen:
                        continue
                    seen.add(k)
                    cleaned.append(t)
                if len(cleaned) != len(original):
                    rows_touched += 1
                    tags_removed += len(original) - len(cleaned)
                    img.tags = ', '.join(cleaned)
            db.commit()
            return {"ok": True, "rows_touched": rows_touched, "tags_removed": tags_removed}
        except Exception:
            db.rollback()
            logger.exception("dedupe_gallery_tags: failed")
            raise HTTPException(500, "Tag deduplication failed")
        finally:
            db.close()

    # ---- DELETE /api/gallery/{image_id} ----
    @router.delete("/api/gallery/{image_id}")
    async def delete_gallery_image(request: Request, image_id: str) -> Dict[str, str]:
        user = _require_gallery_owner(request)
        db = SessionLocal()
        try:
            img = db.query(GalleryImage).filter(
                GalleryImage.id == image_id,
                GalleryImage.owner == user,
                GalleryImage.is_active == True,
            ).first()
            if not img:
                raise HTTPException(404, "Image not found")

            img_filename = img.filename
            # Soft-delete the record first; the DB is the source of truth.
            img.is_active = False
            db.commit()

            # Only after the soft-delete commit succeeds do we remove the file.
            # If the file were deleted first and the commit then failed/rolled
            # back, the still-active record would point at a missing file.
            # Best-effort so a missing or locked file can't 500 a delete that
            # already succeeded logically. Uses the path-confined resolver so a
            # malformed stored filename can't escape generated_images.
            try:
                # A corrupt duplicate row must not let one owner's delete
                # remove bytes still referenced by any active Gallery row.
                still_referenced = db.query(GalleryImage.id).filter(
                    GalleryImage.filename == img_filename,
                    GalleryImage.is_active == True,
                ).first()
                img_path = _gallery_image_path(img_filename)
                if still_referenced is None and img_path.exists():
                    img_path.unlink()
            except Exception as e:
                logger.warning(f"Could not remove gallery image file for {img_filename}: {e}")

            # Strip stale chat-history references so the image bubble
            # (and its prompt caption) doesn't come back after a server
            # reboot replays the session. We remove the matching tool
            # event entirely; if that leaves the message with no other
            # tool events AND a "Generated image for: …" body, drop the
            # whole row so there's no remnant.
            try:
                from core.database import ChatMessage as _ChatMessage
                from sqlalchemy import or_ as _or
                import json as _json
                # Match by image_id OR by filename — older messages
                # (saved before we threaded image_id through the SSE)
                # only carry image_url containing the filename.
                msgs = db.query(_ChatMessage).join(
                    DbSession,
                    _ChatMessage.session_id == DbSession.id,
                ).filter(
                    DbSession.owner == user,
                    _ChatMessage.meta_data.isnot(None),
                    _or(
                        _ChatMessage.meta_data.like(f"%{image_id}%"),
                        _ChatMessage.meta_data.like(f"%{img_filename}%"),
                    ),
                ).all()
                rows_to_delete = []
                for m in msgs:
                    if not m.meta_data:
                        continue
                    try:
                        meta = _json.loads(m.meta_data)
                    except Exception:
                        continue
                    events = meta.get("tool_events") or []
                    new_events = []
                    removed_any = False
                    for ev in events:
                        if not isinstance(ev, dict):
                            new_events.append(ev)
                            continue
                        is_match = ev.get("image_id") == image_id or (
                            ev.get("image_url") and img_filename in ev["image_url"]
                        )
                        if is_match:
                            removed_any = True
                            continue
                        new_events.append(ev)
                    if not removed_any:
                        continue
                    # If the message has no other tool events left, drop
                    # it AND the immediately preceding user prompt that
                    # asked for the image, so no remnant of the exchange
                    # survives.
                    if not new_events:
                        rows_to_delete.append(m)
                        prev = (
                            db.query(_ChatMessage)
                            .filter(
                                _ChatMessage.session_id == m.session_id,
                                _ChatMessage.timestamp < m.timestamp,
                            )
                            .order_by(_ChatMessage.timestamp.desc())
                            .first()
                        )
                        if prev and prev.role == "user":
                            prev_meta = {}
                            try:
                                prev_meta = _json.loads(prev.meta_data) if prev.meta_data else {}
                            except Exception:
                                prev_meta = {}
                            # Only purge the prompt if it has no tool
                            # events of its own (i.e. it's a pure user
                            # message, not an agent step).
                            if not (prev_meta.get("tool_events") or []):
                                rows_to_delete.append(prev)
                    else:
                        meta["tool_events"] = new_events
                        m.meta_data = _json.dumps(meta)
                for m in rows_to_delete:
                    db.delete(m)
                if msgs:
                    db.commit()
            except Exception as _e:
                # Cleanup is best-effort — never block the delete itself.
                logger.warning(f"chat-history cleanup after image delete failed: {_e}")

            return {"status": "deleted", "id": image_id}
        except HTTPException:
            raise
        except Exception:
            db.rollback()
            logger.exception("delete_gallery_image: failed")
            raise HTTPException(500, "Image deletion failed")
        finally:
            db.close()

    # ---- Managed inpaint and image-to-image ----
    @router.post("/api/image/inpaint")
    async def inpaint_proxy(request: Request):
        user = _require_gallery_privilege(request, "can_generate_images")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Request body must be an object")
        image = _decode_managed_image(body.get("image"), "image")
        mask = _decode_managed_image(body.get("mask"), "mask")
        try:
            width = max(64, min(4096, int(body.get("width") or 1024)))
            height = max(64, min(4096, int(body.get("height") or 1024)))
            strength = max(0.0, min(1.0, float(body.get("strength", 0.75))))
            steps = max(1, min(100, int(body.get("steps") or 12)))
            feather = max(0, min(60, int(body.get("feather") or 8)))
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid inpaint options") from None
        output, _media_type, _result = await _managed_gallery_transform(
            owner=user,
            operation="image.inpaint",
            image=image,
            mask=mask,
            input={
                "prompt": str(body.get("prompt") or "").strip(),
                "size": f"{width}x{height}",
                "strength": strength,
                "steps": steps,
                "feather": feather,
            },
            model_route_id=_managed_route_id(body),
            **_managed_operation_context(body),
        )
        return _encoded_image(output)

    @router.post("/api/image/harmonize")
    async def harmonize_image(request: Request):
        user = _require_gallery_privilege(request, "can_generate_images")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Request body must be an object")
        image = _decode_managed_image(body.get("image"), "image")
        prompt = str(
            body.get("prompt")
            or "natural lighting, harmonious color, seamless blend"
        ).strip()
        try:
            strength = max(0.0, min(1.0, float(body.get("strength", 0.45))))
            color_match = max(
                0.0,
                min(1.0, float(body.get("color_match", strength))),
            )
            seam_fix = max(0.0, min(1.0, float(body.get("seam_fix", 0.0))))
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid harmonize options") from None
        output, _media_type, _result = await _managed_gallery_transform(
            owner=user,
            operation="image.img2img",
            image=image,
            input={
                "prompt": prompt,
                "strength": strength,
                "colorMatch": color_match,
                "seamFix": seam_fix,
            },
            model_route_id=_managed_route_id(body),
            **_managed_operation_context(body),
        )
        return _encoded_image(output)

    # ---- POST /api/image/sharpen ----
    @router.post("/api/image/sharpen")
    async def sharpen_image(request: Request):
        """Apply unsharp-mask sharpening to an image."""
        require_privilege(request, "can_generate_images")
        body = await request.json()
        image_b64 = body.get("image")
        amount = body.get("amount", 50) / 100.0

        from PIL import Image, ImageFilter
        import base64, io

        img_bytes = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")

        # Unsharp mask: radius=2, percent=amount*200, threshold=3
        sharpened = img.filter(ImageFilter.UnsharpMask(radius=2, percent=int(amount * 200), threshold=3))

        buf = io.BytesIO()
        sharpened.save(buf, format="PNG")
        return {"image": base64.b64encode(buf.getvalue()).decode()}

    # ---- Managed denoise and upscale ----
    @router.post("/api/image/denoise")
    async def denoise_image(request: Request):
        user = _require_gallery_privilege(request, "can_generate_images")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Request body must be an object")
        image = _decode_managed_image(body.get("image"), "image")
        try:
            strength = max(0.0, min(1.0, float(body.get("strength", 0.5))))
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid denoise strength") from None
        output, _media_type, _result = await _managed_gallery_transform(
            owner=user,
            operation="image.denoise",
            image=image,
            input={"strength": strength, "scale": 1},
            model_route_id=_managed_route_id(body),
            **_managed_operation_context(body),
        )
        return _encoded_image(output)

    @router.post("/api/image/upscale-local")
    async def upscale_image_local(request: Request):
        user = _require_gallery_privilege(request, "can_generate_images")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Request body must be an object")
        image = _decode_managed_image(body.get("image"), "image")
        try:
            scale = int(body.get("scale", 2))
        except (TypeError, ValueError):
            scale = 2
        scale = 2 if scale not in (2, 4) else scale
        output, _media_type, _result = await _managed_gallery_transform(
            owner=user,
            operation="image.upscale",
            image=image,
            input={"scale": scale},
            model_route_id=_managed_route_id(body),
            **_managed_operation_context(body),
        )
        return _encoded_image(output)

    # ---- Managed segmentation ----
    @router.post("/api/image/mask")
    async def smart_mask(request: Request):
        user = _require_gallery_privilege(request, "can_generate_images")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Request body must be an object")
        image = _decode_managed_image(body.get("image"), "image")
        points = body.get("points") or []
        box = body.get("box")
        query = str(body.get("text") or body.get("query") or "").strip()
        if not points and not box and not query:
            raise HTTPException(400, "Provide at least one point, box, or object text")
        output, _media_type, result = await _managed_gallery_transform(
            owner=user,
            operation="image.segment",
            image=image,
            input={
                "prompt": (
                    f"Return a segmentation mask for {query}"
                    if query
                    else "Return the requested segmentation mask"
                ),
                "points": points,
                "box": box,
            },
            model_route_id=_managed_route_id(body),
            **_managed_operation_context(body),
        )
        try:
            from PIL import Image

            with Image.open(io.BytesIO(output)) as decoded:
                if decoded.mode == "RGBA":
                    mask = decoded.getchannel("A")
                else:
                    mask = decoded.convert("L")
                bbox = mask.getbbox()
                encoded = _pil_image_to_b64(mask)
        except Exception:
            raise HTTPException(502, "Managed segmentation returned an invalid mask") from None
        return {
            "mask": encoded,
            "bbox": list(bbox) if bbox else None,
            "model": result.model_route_id,
            "device": "managed",
        }

    @router.post("/api/image/remove-bg")
    async def remove_background(request: Request):
        user = _require_gallery_privilege(request, "can_generate_images")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Request body must be an object")
        image = _decode_managed_image(body.get("image"), "image")
        hint_raw = body.get("hint_mask")
        output, _media_type, _result = await _managed_gallery_transform(
            owner=user,
            operation="image.remove_background",
            image=image,
            input={"prompt": "Remove the background and preserve the foreground"},
            model_route_id=_managed_route_id(body),
            **_managed_operation_context(body),
        )

        # Applying a user-authored hint to an already managed model artifact is
        # a pure Pillow transform, so it stays outside the model router.
        if hint_raw:
            hint_bytes = _decode_managed_image(hint_raw, "hint mask")
            try:
                from PIL import Image, ImageChops

                result_image = Image.open(io.BytesIO(output)).convert("RGBA")
                hint = Image.open(io.BytesIO(hint_bytes)).convert("L")
                if hint.size != result_image.size:
                    hint = hint.resize(result_image.size, Image.Resampling.NEAREST)
                red, green, blue, alpha = result_image.split()
                alpha = ImageChops.multiply(alpha, hint)
                result_image = Image.merge("RGBA", (red, green, blue, alpha))
                buffer = io.BytesIO()
                result_image.save(buffer, format="PNG")
                output = buffer.getvalue()
            except Exception:
                raise HTTPException(400, "Invalid hint mask") from None
        return _encoded_image(output)

    # ---- Managed face restoration ----
    @router.post("/api/image/enhance-face")
    async def enhance_face(request: Request):
        user = _require_gallery_privilege(request, "can_generate_images")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Request body must be an object")
        image = _decode_managed_image(body.get("image"), "image")
        output, _media_type, _result = await _managed_gallery_transform(
            owner=user,
            operation="image.restore_face",
            image=image,
            input={},
            model_route_id=_managed_route_id(body),
            **_managed_operation_context(body),
        )
        return _encoded_image(output)

    # ---- Album management (path-param routes) ----

    def _get_or_404_album(db, album_id: str, user):
        if not user:
            raise HTTPException(404, "Album not found")
        album = db.query(GalleryAlbum).filter(
            GalleryAlbum.id == album_id,
            GalleryAlbum.owner == user,
        ).first()
        if not album:
            raise HTTPException(404, "Album not found")
        return album

    def _get_or_404_image(db, image_id: str, user):
        if not user:
            raise HTTPException(404, "Image not found")
        img = db.query(GalleryImage).filter(
            GalleryImage.id == image_id,
            GalleryImage.owner == user,
            GalleryImage.is_active == True,  # noqa: E712
        ).first()
        if not img:
            raise HTTPException(404, "Image not found")
        return img

    @router.put("/api/gallery/albums/{album_id}")
    async def update_album(request: Request, album_id: str):
        user = _require_gallery_owner(request)
        data = await request.json()
        db = SessionLocal()
        try:
            album = _get_or_404_album(db, album_id, user)
            if data.get("name") is not None:
                album.name = data["name"]
            if data.get("description") is not None:
                album.description = data["description"]
            if data.get("cover_id") is not None:
                cover_id = data["cover_id"] or None
                if cover_id:
                    _get_or_404_image(db, cover_id, user)
                album.cover_id = cover_id
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.delete("/api/gallery/albums/{album_id}")
    async def delete_album(request: Request, album_id: str):
        user = _require_gallery_owner(request)
        db = SessionLocal()
        try:
            album = _get_or_404_album(db, album_id, user)
            q = db.query(GalleryImage).filter(GalleryImage.album_id == album_id)
            q = q.filter(GalleryImage.owner == user)
            q.update({"album_id": None}, synchronize_session=False)
            db.delete(album)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.post("/api/gallery/albums/{album_id}/add")
    async def add_to_album(request: Request, album_id: str):
        user = _require_gallery_owner(request)
        data = await request.json()
        ids = data.get("image_ids", [])
        db = SessionLocal()
        try:
            _get_or_404_album(db, album_id, user)
            # Only move images the caller owns
            q = db.query(GalleryImage).filter(GalleryImage.id.in_(ids))
            q = q.filter(GalleryImage.owner == user)
            updated = q.update({"album_id": album_id}, synchronize_session=False)
            db.commit()
            return {"ok": True, "count": updated}
        finally:
            db.close()

    @router.post("/api/gallery/albums/{album_id}/remove")
    async def remove_from_album(request: Request, album_id: str):
        user = _require_gallery_owner(request)
        data = await request.json()
        ids = data.get("image_ids", [])
        db = SessionLocal()
        try:
            _get_or_404_album(db, album_id, user)
            q = db.query(GalleryImage).filter(
                GalleryImage.id.in_(ids), GalleryImage.album_id == album_id
            )
            q = q.filter(GalleryImage.owner == user)
            q.update({"album_id": None}, synchronize_session=False)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    # ---- Favorite toggle ----

    @router.post("/api/gallery/{image_id}/favorite")
    async def toggle_favorite(request: Request, image_id: str):
        user = _require_gallery_owner(request)
        db = SessionLocal()
        try:
            img = _get_or_404_image(db, image_id, user)
            img.favorite = not img.favorite
            db.commit()
            return {"ok": True, "favorite": img.favorite}
        finally:
            db.close()

    # ---- Managed vision auto-tag ----
    @router.post("/api/gallery/{image_id}/ai-tag")
    async def ai_tag_image(request: Request, image_id: str):
        user = _require_gallery_owner(request)
        db = SessionLocal()
        try:
            image_row = _get_or_404_image(db, image_id, user)
            image_path = _gallery_image_path(image_row.filename, require_exists=True)
            from src.settings import get_user_setting

            if not get_user_setting("vision_enabled", user or "", True):
                return {"error": "Vision is disabled — enable it in Settings → Vision"}
            extension = image_row.filename.rsplit(".", 1)[-1].lower()
            media_type = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "webp": "image/webp",
                "gif": "image/gif",
            }.get(extension, "image/png")
            prompt = (
                "Analyze this photo. Return only a comma-separated list of 10-25 "
                "specific tags covering objects, people by appearance, scene, "
                "activities, mood, colors, location type, time, weather, and text."
            )
            try:
                result = await describe_image(
                    owner=user or "",
                    image=image_path.read_bytes(),
                    media_type=media_type,
                    prompt=prompt,
                )
            except Exception as exc:
                raise _managed_image_failure("vision.describe", exc) from None
            content = str(result.output.get("text") or "")
            tags = [value.strip().lower() for value in content.split(",") if value.strip()]
            tag_string = ", ".join(tags[:30])
            image_row.ai_tags = tag_string
            db.commit()
            return {"ok": True, "ai_tags": tag_string}
        except HTTPException:
            raise
        except Exception:
            db.rollback()
            logger.exception("Managed Gallery auto-tagging failed")
            return {"error": "Auto-tagging failed"}
        finally:
            db.close()

    return router

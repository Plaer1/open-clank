"""Editor draft routes — persisted in-progress gallery-editor sessions.

The gallery editor (image canvas) lets users layer edits on top of a
photo (or a blank canvas). Persisting those layered sessions to the
server makes them survive cache clears and roams across devices —
unlike the legacy per-image localStorage drafts.

Each draft carries:
  - id           — opaque uuid (the client never sees gallery-image ids
                    as draft ids, so blank-canvas drafts work too)
  - source_image_id (nullable) — back-pointer for "this draft started as
                    an edit of Files image X"
  - payload      — full JSON snapshot (layers as base64 PNG dataURLs,
                    offsets, opacities, etc.) the editor knows how to
                    rehydrate
  - thumbnail    — small data URL for the landing-list grid
"""

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from core.database import EditorDraft, SessionLocal
from src.auth_helpers import is_internal_tool_identity, require_user

logger = logging.getLogger(__name__)


class DraftCreate(BaseModel):
    request_id: Optional[uuid.UUID] = None
    name: Optional[str] = None
    source_image_id: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    payload: Dict[str, Any]
    thumbnail: Optional[str] = None


class DraftUpdate(BaseModel):
    name: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    payload: Optional[Dict[str, Any]] = None
    thumbnail: Optional[str] = None


def _owner(request: Request) -> str:
    user = require_user(request)
    if is_internal_tool_identity(user):
        raise HTTPException(403, "Editor drafts require a user session")
    return user


def _owns(d: EditorDraft, user: str) -> bool:
    return bool(user) and d.owner == user


def _summary(d: EditorDraft) -> Dict[str, Any]:
    """List-view representation — omits the bulky payload."""
    return {
        "id": d.id,
        "name": d.name or "Untitled",
        "source_image_id": d.source_image_id,
        "width": d.width,
        "height": d.height,
        "thumbnail": d.thumbnail,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def _load_payload(raw: Optional[str]) -> Dict[str, Any]:
    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def setup_editor_draft_routes() -> APIRouter:
    router = APIRouter(tags=["editor-drafts"])

    @router.get("/api/editor-drafts")
    async def list_drafts(request: Request) -> Dict[str, List[Dict[str, Any]]]:
        user = _owner(request)
        db = SessionLocal()
        try:
            q = db.query(EditorDraft).filter(EditorDraft.is_active == True)
            q = q.filter(EditorDraft.owner == user)
            rows = q.order_by(EditorDraft.updated_at.desc()).limit(200).all()
            return {"drafts": [_summary(d) for d in rows]}
        finally:
            db.close()

    @router.get("/api/editor-drafts/{draft_id}")
    async def get_draft(request: Request, draft_id: str) -> Dict[str, Any]:
        user = _owner(request)
        db = SessionLocal()
        try:
            d = db.query(EditorDraft).filter(
                EditorDraft.id == draft_id, EditorDraft.is_active == True
            ).first()
            if not d or not _owns(d, user):
                raise HTTPException(404, "Draft not found")
            return {
                **_summary(d),
                "payload": _load_payload(d.payload),
            }
        finally:
            db.close()

    @router.post("/api/editor-drafts")
    async def create_draft(request: Request, body: DraftCreate) -> Dict[str, Any]:
        user = _owner(request)
        draft_id = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["openclank:image-draft", user, str(body.request_id)]))) if body.request_id else str(uuid.uuid4())
        db = SessionLocal()
        try:
            d = EditorDraft(
                id=draft_id,
                owner=user,
                name=(body.name or "Untitled")[:200],
                source_image_id=body.source_image_id,
                width=body.width,
                height=body.height,
                payload=json.dumps(body.payload or {}),
                thumbnail=body.thumbnail,
            )
            db.add(d)
            db.commit()
            db.refresh(d)
            return _summary(d)
        except IntegrityError:
            db.rollback()
            existing = db.get(EditorDraft, draft_id) if body.request_id else None
            if existing and existing.is_active and _owns(existing, user) and (
                existing.name == (body.name or "Untitled")[:200]
                and existing.width == body.width and existing.height == body.height
                and existing.source_image_id == body.source_image_id and existing.thumbnail == body.thumbnail
                and _load_payload(existing.payload) == body.payload
            ):
                return _summary(existing)
            raise HTTPException(409, "Draft recovery no longer matches the original request")
        except Exception as e:
            db.rollback()
            logger.warning(f"editor-draft create failed: {e}")
            raise HTTPException(500, "Could not save draft")
        finally:
            db.close()

    @router.put("/api/editor-drafts/{draft_id}")
    async def update_draft(request: Request, draft_id: str, body: DraftUpdate) -> Dict[str, Any]:
        user = _owner(request)
        db = SessionLocal()
        try:
            d = db.query(EditorDraft).filter(
                EditorDraft.id == draft_id, EditorDraft.is_active == True
            ).first()
            if not d or not _owns(d, user):
                raise HTTPException(404, "Draft not found")
            if body.name is not None:
                d.name = body.name[:200]
            if body.width is not None:
                d.width = body.width
            if body.height is not None:
                d.height = body.height
            if body.payload is not None:
                d.payload = json.dumps(body.payload)
            if body.thumbnail is not None:
                d.thumbnail = body.thumbnail
            db.commit()
            db.refresh(d)
            return _summary(d)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.warning(f"editor-draft update failed: {e}")
            raise HTTPException(500, "Could not update draft")
        finally:
            db.close()

    @router.delete("/api/editor-drafts/{draft_id}")
    async def delete_draft(request: Request, draft_id: str) -> Dict[str, str]:
        user = _owner(request)
        db = SessionLocal()
        try:
            d = db.query(EditorDraft).filter(EditorDraft.id == draft_id).first()
            if not d or not _owns(d, user):
                raise HTTPException(404, "Draft not found")
            d.is_active = False
            db.commit()
            return {"status": "deleted", "id": draft_id}
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(500, str(e))
        finally:
            db.close()

    return router

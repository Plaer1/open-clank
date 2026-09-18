"""Cleanup helpers for images attached to chat sessions."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from fastapi import HTTPException

from src.generated_images import (
    GENERATED_IMAGE_DIR,
    gallery_owner_key,
    resolve_gallery_image_path,
)

logger = logging.getLogger(__name__)
GENERATED_IMAGES_DIR = GENERATED_IMAGE_DIR


def _database_models():
    """Import DB models at call time so early import stubs cannot stick here."""
    from core.database import ChatMessage, GalleryImage, SessionLocal

    return ChatMessage, GalleryImage, SessionLocal


def _generated_image_path_for_cleanup(filename: str) -> Path | None:
    try:
        return resolve_gallery_image_path(filename, root=GENERATED_IMAGES_DIR)
    except HTTPException:
        return None


def _image_filename_from_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        return ""
    match = re.search(r"/api/generated-image/([^?#/]+)", url)
    return match.group(1) if match else ""


def session_image_refs(db, session_id: str, owner: str | None) -> tuple[set[str], set[str]]:
    """Return gallery image ids and generated-image filenames referenced by a chat."""
    ChatMessage, GalleryImage, _ = _database_models()
    image_ids: set[str] = set()
    filenames: set[str] = set()
    owner_key = gallery_owner_key(owner)
    if owner_key is None:
        return image_ids, filenames

    from core.database import Session as DbSession

    owned_session = db.query(DbSession.id).filter(
        DbSession.id == session_id,
        DbSession.owner == owner_key,
    ).first()
    if owned_session is None:
        return image_ids, filenames

    rows = db.query(GalleryImage).filter(
        GalleryImage.session_id == session_id,
        GalleryImage.owner == owner_key,
    ).all()
    for img in rows:
        if img.id:
            image_ids.add(str(img.id))
        if img.filename:
            filenames.add(str(img.filename))

    messages = db.query(ChatMessage.meta_data).filter(ChatMessage.session_id == session_id).all()
    for row in messages:
        raw = getattr(row, "meta_data", None)
        if not raw:
            continue
        try:
            meta = json.loads(raw)
        except Exception:
            continue
        events = meta.get("tool_events") if isinstance(meta, dict) else None
        if not isinstance(events, list):
            continue
        for ev in events:
            if not isinstance(ev, dict):
                continue
            image_id = ev.get("image_id")
            if image_id:
                image_ids.add(str(image_id))
            filename = _image_filename_from_url(ev.get("image_url") or ev.get("url") or "")
            if filename:
                filenames.add(filename)

    return image_ids, filenames


def cleanup_session_image_files(filenames: set[str]) -> int:
    """Unlink only filenames with no active Gallery metadata reference."""

    _, GalleryImage, SessionLocal = _database_models()
    if not filenames:
        return 0
    db = SessionLocal()
    removed = 0
    try:
        for filename in filenames:
            referenced = db.query(GalleryImage.id).filter(
                GalleryImage.filename == filename,
                GalleryImage.is_active == True,
            ).first()
            if referenced is not None:
                continue
            path = _generated_image_path_for_cleanup(filename)
            if path and path.exists():
                try:
                    path.unlink()
                    removed += 1
                except Exception as exc:
                    logger.warning(
                        "Could not remove unreferenced generated image %s: %s",
                        filename,
                        exc,
                    )
        return removed
    finally:
        db.close()


def cleanup_session_images(session_id: str, owner: str | None, db=None) -> int:
    """Soft-delete Gallery rows and unlink generated files owned by a chat."""
    _, GalleryImage, SessionLocal = _database_models()
    owner_key = gallery_owner_key(owner)
    if owner_key is None:
        return 0
    owns_db = db is None
    db = db or SessionLocal()
    try:
        image_ids, filenames = session_image_refs(db, session_id, owner_key)
        query = db.query(GalleryImage).filter(
            GalleryImage.session_id == session_id,
            GalleryImage.owner == owner_key,
        )
        if image_ids or filenames:
            from sqlalchemy import and_, or_

            clauses = [and_(
                GalleryImage.session_id == session_id,
                GalleryImage.owner == owner_key,
            )]
            if image_ids:
                clauses.append(and_(
                    GalleryImage.id.in_(list(image_ids)),
                    GalleryImage.owner == owner_key,
                ))
            if filenames:
                clauses.append(and_(
                    GalleryImage.filename.in_(list(filenames)),
                    GalleryImage.owner == owner_key,
                ))
            query = db.query(GalleryImage).filter(or_(*clauses))

        images = query.all()
        for img in images:
            img.is_active = False
            if img.filename:
                filenames.add(str(img.filename))

        if owns_db and images:
            db.commit()
            cleanup_session_image_files(filenames)
        return len(images)
    except Exception as exc:
        if owns_db:
            db.rollback()
        logger.warning("Failed to clean images for deleted session %s: %s", session_id, exc)
        return 0
    finally:
        if owns_db:
            db.close()

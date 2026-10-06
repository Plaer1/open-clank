"""Files-owned cleanup for images attached to chat sessions."""

from __future__ import annotations

import json
import logging

from src.generated_images import gallery_owner_key
from src.openclank.files_image_store import FilesImageStore

logger = logging.getLogger(__name__)


def _database_models():
    """Import models at call time so early import stubs cannot stick here."""
    from core.database import ChatMessage, Session as DbSession, SessionLocal

    return ChatMessage, DbSession, SessionLocal


def _event_image_ids(db, session_id: str) -> set[str]:
    ChatMessage, _, _ = _database_models()
    result: set[str] = set()
    for row in db.query(ChatMessage.meta_data).filter(ChatMessage.session_id == session_id).all():
        try:
            metadata = json.loads(getattr(row, "meta_data", "") or "")
        except Exception:
            continue
        for event in metadata.get("tool_events", []) if isinstance(metadata, dict) else []:
            if isinstance(event, dict) and event.get("image_id"):
                result.add(str(event["image_id"]).removeprefix("image:"))
    return result


def session_image_refs(db, session_id: str, owner: str | None) -> set[str]:
    """Return Files image IDs durably associated with one owned chat."""
    _, DbSession, SessionLocal = _database_models()
    owner_key = gallery_owner_key(owner)
    if owner_key is None:
        return set()
    if db.query(DbSession.id).filter(DbSession.id == session_id, DbSession.owner == owner_key).first() is None:
        return set()
    store = FilesImageStore(session_factory=SessionLocal)
    resource_ids = {row.id for row in store.list_images(owner_key, session_id=session_id)}
    # Tool-event IDs are only accepted when Files also proves the same owner
    # and session provenance, so a copied event cannot retire another chat's
    # shared image.
    for resource_id in _event_image_ids(db, session_id):
        try:
            row = store.image(owner_key, resource_id)
        except Exception:
            continue
        sessions = set(str(value) for value in (row.provenance or {}).get("session_ids", []))
        sessions.add(str((row.provenance or {}).get("session_id") or ""))
        if session_id in sessions:
            resource_ids.add(row.id)
    return resource_ids


def retire_session_image_refs(session_id: str, owner: str | None, image_ids: set[str], *, strict: bool = False) -> int:
    """Durably detach precomputed Files refs after the session transaction."""
    owner_key = gallery_owner_key(owner)
    if owner_key is None:
        return 0
    _, _, SessionLocal = _database_models()
    store = FilesImageStore(session_factory=SessionLocal)
    retired = 0
    for resource_id in image_ids:
        try:
            retired += int(store.detach_session(owner_key, resource_id, session_id))
        except Exception:
            if strict:
                # Keep the live chat row until every selected image transition
                # finishes; a later request can retry the same owned scope.
                raise
            logger.warning("Failed to retire Files image %s for deleted session %s", resource_id, session_id, exc_info=True)
    return retired


def mark_session_image_source_removed(db, *, owner: str, session_id: str, image_ids: set[str]) -> int:
    """Detach exact chat provenance while independent reference coverage is unknown.

    Files remains the byte owner. Native Memery/Skills/Copal can hold image
    references not represented by session_ids, so this path preserves bytes.
    """
    from core.database import FilesImageResource
    changed = 0
    for row in db.query(FilesImageResource).filter(FilesImageResource.owner == owner, FilesImageResource.id.in_(image_ids)):
        provenance = dict(row.provenance or {})
        sessions = {str(value) for value in provenance.get("session_ids", [])}
        legacy = str(provenance.get("session_id") or "")
        if legacy:
            sessions.add(legacy)
        if session_id not in sessions:
            continue
        sessions.discard(session_id)
        provenance["session_ids"] = sorted(sessions)
        provenance.pop("session_id", None)
        removed = {str(value) for value in provenance.get("removed_session_ids", [])}
        removed.add(session_id)
        provenance["removed_session_ids"] = sorted(removed)
        provenance["source_conversation_removed"] = True
        row.provenance = provenance
        row.revision += 1
        changed += 1
    return changed


def cleanup_session_images(session_id: str, owner: str | None, db=None) -> int:
    """Detach a chat from Files provenance and retire only unshared bytes.

    The caller must invoke this after its session-delete transaction commits;
    Files retirement is itself durable and compensates byte staging on DB
    failure.
    """
    _, _, SessionLocal = _database_models()
    owner_key = gallery_owner_key(owner)
    if owner_key is None:
        return 0
    owns_db = db is None
    current_db = db or SessionLocal()
    try:
        image_ids = session_image_refs(current_db, session_id, owner_key)
    finally:
        if owns_db:
            current_db.close()
    return retire_session_image_refs(session_id, owner_key, image_ids)


def retire_reconciled_session_images(*, owner: str, session_id: str, upload_handler, image_store=None) -> int:
    """Retry exact durable candidates after the core erasure commit."""
    if not upload_handler or not upload_handler.attachment_inventory_status().get("complete"):
        return 0
    store = image_store or FilesImageStore()
    intent = upload_handler.conversation_attachment_intent(owner=owner, session_id=session_id)
    from core.database import Base, FilesImageResource
    from sqlalchemy import String, Text, JSON, inspect
    from src.openclank.attachment_inventory import resource_references
    db = store.session_factory()
    try:
        revisions = {row.id: row.revision for row in db.query(FilesImageResource).filter(FilesImageResource.owner == owner, FilesImageResource.id.in_(intent["image_ids"]))}
        shared = set()
        installed = set(inspect(db.get_bind()).get_table_names())
        for table in Base.metadata.sorted_tables:
            if table.name not in installed:
                continue
            columns = [column for column in table.columns if isinstance(column.type, (String, Text, JSON))]
            if not columns:
                continue
            for row in db.query(*columns):
                shared.update(identifier for provider, identifier in resource_references(list(row)) if provider == "image")
    finally:
        db.close()
    retired = 0
    for identifier in intent["image_ids"]:
        if identifier in shared or identifier not in revisions:
            continue
        retired += int(store.retire(owner, identifier, only_unreferenced=True, session_id=session_id, expected_revision=revisions[identifier]))
    return retired

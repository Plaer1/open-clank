"""Exact conversation deletion impact and independent-reference preservation."""
from __future__ import annotations

import json
from sqlalchemy import String, Text, JSON, inspect, not_, and_
from core.database import Base, ChatMessage, Memory, Document, Session as DbSession
from src.openclank.attachment_inventory import resource_references
from src.openclank.conversation_archive import get_conversation_archive
from src.openclank.logging_capture_store import CAPTURE


def attachment_scope(db, *, owner, session_id, archive, upload_handler=None):
    """Conservative complete first-party SQL plus retained-history reference scan."""
    candidates, shared, hashes, shared_images = set(), set(), set(), set()
    for content, metadata in db.query(ChatMessage.content, ChatMessage.meta_data).filter(ChatMessage.session_id == session_id):
        candidates.update(identifier for provider, identifier in resource_references([content, metadata]) if provider == "upload")
    if upload_handler:
        candidates.update(upload_handler.conversation_attachment_intent(owner=owner, session_id=session_id)["candidate_ids"])
    account_id = archive.resolve_owner(owner)
    with archive._connect() as conn:
        for table, columns in (("conversation_parts", ("content",)), ("conversation_part_assets", ("asset_id", "provenance")), ("conversation_archive_migration_envelopes", ("payload_json",))):
            for row in conn.execute(f"SELECT owner, chat_id, {', '.join(columns)} FROM {table}"):
                target = candidates if row[0] == account_id and row[1] == session_id else shared
                for value in row[2:]:
                    target.update(identifier for provider, identifier in resource_references(value) if provider == "upload")
                    if target is shared:
                        shared_images.update(identifier for provider, identifier in resource_references(value) if provider == "image")
                if table == "conversation_part_assets" and str(row[2] or "").startswith("upload:"):
                    target.add(str(row[2]).removeprefix("upload:"))
    # Every independently persisted textual reference protects an upload,
    # including retained Documents versions, Memery, Skills and Files.
    installed_tables = set(inspect(db.get_bind()).get_table_names())
    for table in Base.metadata.sorted_tables:
        # Some registered models (such as the engine credential store) live
        # in a different database. They cannot own core-store attachments.
        if table.name not in installed_tables:
            continue
        columns = [column for column in table.columns if isinstance(column.type, (String, Text, JSON))]
        if not columns:
            continue
        query = db.query(*columns)
        if table.name == ChatMessage.__tablename__:
            query = query.filter(table.c.session_id != session_id)
        elif table.name == DbSession.__tablename__:
            query = query.filter(table.c.id != session_id)
        elif table.name == "logging_attempt_details":
            query = query.filter(not_(and_(table.c.owner == owner, table.c.attribution["session_id"].as_string() == session_id)))
        elif table.name in {"logging_capture_bodies", "logging_capture_events"}:
            from src.openclank.logging_models import LoggingAttemptDetail
            selected_details = db.query(LoggingAttemptDetail.id).filter(LoggingAttemptDetail.owner == owner, LoggingAttemptDetail.attribution["session_id"].as_string() == session_id)
            query = query.filter(table.c.attempt_detail_id.not_in(selected_details))
        for row in query.yield_per(500):
            for value in row:
                shared.update(identifier for provider, identifier in resource_references(value) if provider == "upload")
                shared_images.update(identifier for provider, identifier in resource_references(value) if provider == "image")
    from core.database import FilesImageResource
    selected_images = set()
    if upload_handler and upload_handler.attachment_inventory_status().get("complete"):
        from src.session_image_cleanup import session_image_refs
        selected_images = session_image_refs(db, session_id, owner)
        selected_images.update(upload_handler.conversation_attachment_intent(owner=owner, session_id=session_id)["image_ids"])
    for image in db.query(FilesImageResource).filter(FilesImageResource.kind == "image", FilesImageResource.is_active.is_(True)):
        provenance = dict(image.provenance or {})
        sessions = set(provenance.get("session_ids") or [])
        if provenance.get("session_id"):
            sessions.add(provenance["session_id"])
        claims = set(provenance.get("reference_claims") or []) - {f"chat:{session_id}", "domain:core"}
        selected_exclusive = (image.owner == owner and image.id in selected_images
                              and not (sessions - {session_id}) and not claims
                              and image.id not in shared_images)
        if image.digest and not selected_exclusive:
            hashes.add(str(image.digest))
    # SQL is not the sole independent-reference authority: native Memery,
    # Skills files and native Copal/Lore may still reference these IDs. Until
    # their canonical owner inventory proves absence, retain unknown refs.
    coverage = upload_handler.attachment_inventory_status() if upload_handler else {"complete": False}
    unknown = candidates - shared if not coverage.get("complete") else set()
    shared.update(unknown)
    return candidates, shared, hashes, unknown


def deletion_impact(db, *, owner, session_id, upload_handler=None):
    generations = upload_handler.attachment_reference_generations() if upload_handler else None
    archive = get_conversation_archive()
    retained = archive.chat_inventory(owner=owner, chat_id=session_id)
    candidates, shared, hashes, unknown = attachment_scope(db, owner=owner, session_id=session_id, archive=archive, upload_handler=upload_handler)
    from src.session_image_cleanup import session_image_refs
    retained_images = session_image_refs(db, session_id, owner)
    coverage = upload_handler.attachment_inventory_status() if upload_handler else {"complete": False}
    attachments = upload_handler.conversation_attachment_impact(owner=owner, candidate_ids=candidates, shared_ids=shared, shared_hashes=hashes, session_id=session_id, observed_generations=generations) if upload_handler else {"exclusive_ids": [], "shared_ids": sorted(candidates), "exclusive_bytes": 0}
    return {
        "session_id": session_id,
        "erasure_started": retained["erasure_started"],
        "live_messages": db.query(ChatMessage).filter(ChatMessage.session_id == session_id).count(),
        "retained": retained["tables"],
        "logs": CAPTURE.session_inventory(db, owner=owner, session_id=session_id),
        "exclusive_attachments": len(attachments["exclusive_ids"]),
        "exclusive_attachment_bytes": attachments["exclusive_bytes"],
        "shared_attachments_preserved": len(set(attachments["shared_ids"]) - unknown),
        "attachments_retained_unknown_coverage": len(unknown),
        "generated_images_retained_unknown_coverage": 0 if coverage.get("complete") else len(retained_images),
        "attachment_coverage": "current typed references + reconciled retained domains" if coverage.get("complete") else "explicit offline retained-reference inventory required",
        "attachment_inventory_errors": coverage.get("errors", []),
        "independent_memory_preserved": db.query(Memory).filter(Memory.owner == owner, Memory.session_id == session_id).count(),
        "documents_preserved": db.query(Document).filter(Document.session_id == session_id).count(),
        "preserved": ["shared media", "independent Memery", "Skills", "Hexes", "Lore recovery", "content-free usage totals"],
        "reversible": False,
    }


def erase_retained_content(db, *, owner, session_id, upload_handler=None):
    generations = upload_handler.attachment_reference_generations() if upload_handler else None
    archive = get_conversation_archive()
    candidates, shared, hashes, unknown = attachment_scope(db, owner=owner, session_id=session_id, archive=archive, upload_handler=upload_handler)
    # Refuse active capture work before any irreversible retained-data change.
    inventory = CAPTURE.session_inventory(db, owner=owner, session_id=session_id)
    if inventory["active_scopes"]:
        raise RuntimeError("Conversation has active provider work")
    if candidates and upload_handler is None:
        raise RuntimeError("Conversation attachment owner is unavailable")
    # Use the same application-owned configured index; an unavailable existing
    # index cannot be silently skipped while claiming complete erasure.
    from src.rag_singleton import get_rag_manager
    from services.logging.semantic import ARCHIVE_INDEX_SCOPE
    rag = get_rag_manager()
    if rag is None or not rag.healthy:
        raise RuntimeError("Conversation search-index owner is unavailable")
    archive.begin_chat_erasure(owner=owner, chat_id=session_id)
    rag.erase_external_chat_references(owner=owner, workspace_id=ARCHIVE_INDEX_SCOPE, chat_id=session_id)
    if upload_handler:
        upload_handler.conversation_attachment_intent(owner=owner, session_id=session_id, candidate_ids=candidates, persist=True)
        upload_handler.erase_conversation_attachments(owner=owner, candidate_ids=candidates, shared_ids=shared, shared_hashes=hashes, session_id=session_id, observed_generations=generations)
    CAPTURE.erase_session(db, owner=owner, session_id=session_id)
    archive.erase_chat(owner=owner, chat_id=session_id)
    from services.stats.ledger import detach_session_events
    detach_session_events(db, owner=owner, session_id=session_id)
    for memory in db.query(Memory).filter(Memory.owner == owner, Memory.session_id == session_id):
        memory.session_id = None
        memory.source = "conversation_deleted"

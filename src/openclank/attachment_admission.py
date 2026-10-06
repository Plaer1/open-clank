"""Publish typed claims at the existing byte owners before domain writes.

These claims contain only resource identities. Retained Copal, Skills and
Memery revisions keep their claims; interrupted writes are reconciled by the
explicit offline inventory tool, never by a clock or app-startup migration.
"""
from __future__ import annotations

from src.openclank.attachment_inventory import resource_references


def admit_references(owner, domain: str, *payloads, upload_handler=None, image_store=None):
    import uuid
    refs = set()
    for payload in payloads:
        refs.update(resource_references(payload))
    if not refs:
        return []
    if upload_handler is None:
        from src.tool_utils import get_upload_handler
        upload_handler = get_upload_handler()
    if image_store is None:
        from src.openclank.files_image_store import FilesImageStore
        image_store = FilesImageStore()
    admitted = []
    try:
        for provider, identifier in sorted(refs):
            scope = "pending:" + uuid.uuid4().hex
            if provider == "upload":
                if upload_handler is None or not upload_handler.claim_upload_reference(identifier, owner=owner, reference_scope=scope):
                    raise ValueError(f"Referenced attachment is unavailable: {identifier}")
                admitted.append((upload_handler, identifier, scope, f"domain:{domain}"))
            else:
                image_store.claim_reference(owner, identifier, scope)
                admitted.append((image_store, identifier, scope, f"domain:{domain}"))
    except Exception:
        # Admission failed before any domain publication could begin.
        settle_references(admitted, committed=False)
        raise
    return admitted


def settle_references(admitted, *, committed=True):
    import logging
    for backend, identifier, token, domain in admitted:
        try:
            backend.settle_reference_claim(identifier, token, committed=committed, settled_scope=domain)
        except Exception:
            # Content already committed. Retain pending byte-owner identity for
            # explicit reconciliation rather than misreport a rolled-back write.
            logging.getLogger(__name__).warning("Attachment publication remains pending: %s", identifier, exc_info=True)


def refresh_domain_references(domain, inventory_reader, *, upload_handler=None, image_store=None):
    """Release settled claims only when the canonical owner proves absence.

    Pending writers stay protected. A claim generation changed during the
    inventory is left intact, so a concurrent commit cannot be erased by a
    stale inventory publication. Run after a domain's actual purge/removal.
    """
    if upload_handler is None:
        from src.tool_utils import get_upload_handler
        upload_handler = get_upload_handler()
    if upload_handler is None:
        return
    if image_store is None:
        from src.openclank.files_image_store import FilesImageStore
        image_store = FilesImageStore()
    from core.database import FilesImageResource
    from sqlalchemy import text
    generations = upload_handler.attachment_reference_generations()
    db = image_store.session_factory()
    try:
        revisions = {row.id: row.revision for row in db.query(FilesImageResource).filter(FilesImageResource.kind == "image")}
    finally:
        db.close()
    inventory = inventory_reader()
    if inventory.get("complete") is not True:
        return
    refs = {(ref["provider"], ref["resource_id"]) for ref in inventory["references"]}
    scopes = {"domain:" + domain}
    if domain == "copal":
        scopes.update({"domain:copal-loose", "domain:copal-native"})
    import os
    with upload_handler._index_guard():
        current_generations = upload_handler.attachment_reference_generations()
        index = upload_handler._load_upload_index(fail_on_error=True)
        for key, row in list(index.items()):
            if not isinstance(row, dict) or row.get("id") not in generations:
                continue
            if generations[row["id"]] != current_generations.get(row["id"]):
                continue
            old = set(row.get("reference_claims") or [])
            claims = old - scopes
            if ("upload", row["id"]) in refs:
                claims.add("domain:" + domain)
            if old != claims:
                updated = dict(row)
                updated["reference_claims"] = sorted(claims)
                updated["reference_generation"] = int(updated.get("reference_generation") or 0) + 1
                index[key] = updated
        upload_handler._atomic_write_json(os.path.join(upload_handler.upload_dir, "uploads.json"), index, sync_backup=True)
    db = image_store.session_factory()
    try:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        for row in db.query(FilesImageResource).filter(FilesImageResource.kind == "image"):
            if revisions.get(row.id) != row.revision:
                continue
            provenance = dict(row.provenance or {})
            old = set(provenance.get("reference_claims") or [])
            claims = old - scopes
            if ("image", row.id) in refs:
                claims.add("domain:" + domain)
            if old != claims:
                provenance["reference_claims"] = sorted(claims)
                row.provenance = provenance
                row.revision += 1
        db.commit()
    finally:
        db.close()

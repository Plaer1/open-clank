"""Inactive v3 representation adapters; not registered as a runtime transport.

Callers own the SQLite transaction and authenticated scope. This module does
not install schema, commit, create a change set, or grant field authority.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from src.memery_v2 import V2OperationError, _hash, _json, _scope_is_visible, _scope_keys, _validate_typed_value, _provider_dossier_evidence_blocked

METADATA_SCHEMA = "openclank.memory-revision-metadata.v1"
_METADATA_KEYS = {"name", "aliases", "domain_refs", "types", "processing_role"}


def validate_v3_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise V2OperationError("validation", "value must be a typed object")
    if value.get("type") != "knowledge_ref":
        return _validate_typed_value(value)
    payload = value.get("value")
    if set(value) != {"type", "value"} or not isinstance(payload, Mapping) or not {"block_id"}.issubset(payload) or set(payload) - {"block_id", "revision"}:
        raise V2OperationError("validation", "invalid knowledge reference")
    block = payload.get("block_id")
    revision = payload.get("revision")
    if not isinstance(block, str) or not block.strip() or block != block.strip() or (
        "revision" in payload and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 1)
    ):
        raise V2OperationError("validation", "invalid knowledge reference")
    return {"type": "knowledge_ref", "value": dict(payload)}


def validate_revision_metadata(value: Any) -> dict[str, Any]:
    from src.openclank.resource_metadata import validate_metadata
    try:
        return validate_metadata(value)
    except (TypeError, ValueError) as exc:
        raise V2OperationError("validation", "invalid revision metadata") from exc


def _authorized_scope(authenticated_owner: str, owner: str, workspace_id: str | None, project_id: str | None) -> tuple[str, str, str]:
    owner, workspace, project = _scope_keys(owner, workspace_id, project_id)
    if not isinstance(authenticated_owner, str) or not authenticated_owner.strip() or authenticated_owner.strip() != owner:
        raise V2OperationError("not_found", "knowledge reference unavailable")
    return owner, workspace, project


def resolve_knowledge_reference(conn: sqlite3.Connection, *, authenticated_owner: str, owner: str,
                                workspace_id: str | None, project_id: str | None, value: Any) -> dict[str, Any]:
    """Resolve under the existing V2 visibility rule, without re-targeting a revision."""
    typed = validate_v3_value(value)
    if typed["type"] != "knowledge_ref":
        raise V2OperationError("validation", "knowledge reference required")
    owner, workspace, project = _authorized_scope(authenticated_owner, owner, workspace_id, project_id)
    payload = typed["value"]
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN")
    try:
        row = conn.execute("SELECT workspace_key,project_key,current_revision FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?", (owner, payload["block_id"])).fetchone()
        if row is None or not _scope_is_visible(row[0], row[1], workspace, project):
            raise V2OperationError("not_found", "knowledge reference unavailable")
        revision = payload["revision"] if "revision" in payload else row[2]
        if (conn.execute("SELECT 1 FROM fm_v2_knowledge_revisions WHERE owner_id=? AND block_id=? AND revision=?", (owner, payload["block_id"], revision)).fetchone() is None
                or _provider_dossier_evidence_blocked(conn, owner, payload["block_id"], revision)):
            raise V2OperationError("not_found", "knowledge reference unavailable")
        return {"block_id": payload["block_id"], "revision": revision}
    finally:
        if started:
            conn.rollback()


@dataclass(frozen=True)
class RevisionMetadataContext:
    """Trusted repository-only context; never construct from model arguments."""
    connection: sqlite3.Connection
    owner: str
    workspace_id: str | None
    project_id: str | None
    block_id: str
    revision: int
    producer_actor_type: str
    producer_actor_id: str
    change_set_id: str
    savepoint: str


def prepare_revision_metadata(conn: sqlite3.Connection, *, authenticated_owner: str, owner: str,
                              workspace_id: str | None, project_id: str | None, block_id: str,
                              revision: int, producer_actor_type: str, producer_actor_id: str,
                              change_set_id: str) -> RevisionMetadataContext:
    """Prepare before inserting a *new* change set and revision in the caller's transaction.

    The future trusted repository must create the change set, revision and its outbox
    in this transaction. The savepoint makes commit/restart or replay invalidate this context.
    """
    if not conn.in_transaction:
        raise V2OperationError("validation", "caller transaction required")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise V2OperationError("validation", "SQLite foreign keys are unavailable")
    if any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in (block_id, producer_actor_type, producer_actor_id, change_set_id)) or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise V2OperationError("validation", "invalid metadata producer context")
    owner, workspace, project = _authorized_scope(authenticated_owner, owner, workspace_id, project_id)
    source = conn.execute("SELECT workspace_key,project_key FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?", (owner, block_id)).fetchone()
    if source is None or not _scope_is_visible(source[0], source[1], workspace, project):
        raise V2OperationError("not_found", "knowledge reference unavailable")
    if conn.execute("SELECT 1 FROM fm_v2_knowledge_revisions WHERE owner_id=? AND block_id=? AND revision=?", (owner, block_id, revision)).fetchone():
        raise V2OperationError("validation", "metadata requires a new revision")
    if conn.execute("SELECT 1 FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=?", (owner, change_set_id)).fetchone():
        raise V2OperationError("validation", "metadata requires a new change set")
    savepoint = "fm_meta_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {savepoint}")
    return RevisionMetadataContext(conn, owner, source[0] or None, source[1] or None, block_id, revision,
                                   producer_actor_type, producer_actor_id, change_set_id, savepoint)


def append_revision_metadata(conn: sqlite3.Connection, *, context: RevisionMetadataContext,
                             metadata: Any, created_at: str) -> dict[str, Any]:
    """Append only for the prepared new revision and producer in the same transaction."""
    if not isinstance(context, RevisionMetadataContext) or context.connection is not conn or not conn.in_transaction:
        raise V2OperationError("validation", "prepared metadata transaction required")
    if not isinstance(created_at, str) or not created_at.strip():
        raise V2OperationError("validation", "invalid metadata timestamp")
    data = validate_revision_metadata(metadata)
    try:
        conn.execute(f"RELEASE SAVEPOINT {context.savepoint}")
    except sqlite3.Error as exc:
        raise V2OperationError("validation", "metadata transaction changed or context reused") from exc
    change = conn.execute("SELECT actor_type,actor_id FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=?", (context.owner, context.change_set_id)).fetchone()
    if change is None or (change[0], change[1]) != (context.producer_actor_type, context.producer_actor_id):
        raise V2OperationError("validation", "metadata producer change set mismatch")
    row = conn.execute("SELECT actor_type,actor_id,change_set_id,source_event_id,evidence_json FROM fm_v2_knowledge_revisions WHERE owner_id=? AND block_id=? AND revision=?", (context.owner, context.block_id, context.revision)).fetchone()
    if row is None or (row[0], row[1], row[2]) != (context.producer_actor_type, context.producer_actor_id, context.change_set_id):
        raise V2OperationError("validation", "new revision producer mismatch")
    for ref in data.get("domain_refs", []):
        resolve_knowledge_reference(conn, authenticated_owner=context.owner, owner=context.owner,
            workspace_id=context.workspace_id, project_id=context.project_id,
            value={"type": "knowledge_ref", "value": ref})
    digest = _hash(data)
    conn.execute("INSERT INTO fm_v2_revision_metadata(owner_id,block_id,revision,metadata_schema,metadata_json,metadata_hash,actor_type,actor_id,change_set_id,source_event_id,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (context.owner, context.block_id, context.revision, METADATA_SCHEMA, _json(data), digest, context.producer_actor_type,
         context.producer_actor_id, context.change_set_id, row[3], row[4], created_at))
    return {"block_id": context.block_id, "revision": context.revision, "metadata_schema": METADATA_SCHEMA, "metadata_hash": digest}

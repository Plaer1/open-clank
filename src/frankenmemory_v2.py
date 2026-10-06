"""Small, transactional bridge from the legacy provider to the v2 ontology.

The Rust provider remains the compatibility read/write surface for now.  This
module makes every successful user-authored mutation leave the same durable,
typed, tenant-scoped v2 trail instead of leaving the new schema as an empty
sidecar.  It is deliberately best-effort at the call boundary: legacy
compatibility must not turn a healthy memory write into a 503 while the v2
cutover is still being rolled out.  The transaction itself is strict and
never publishes a partial entity/block/source revision.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from src.constants import FM_DB_PATH

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "frankenmemory.v2"

KNOWLEDGE_STATUSES = {"open", "active", "superseded", "retracted"}
CARDINALITIES = {"single", "set", "ordered_many", "time_scoped"}
VALUE_TYPES = {
    "null", "string", "number", "boolean", "time", "duration", "uri",
    "entity_ref", "object", "list",
}

TRUST_SUBJECT_KINDS = {
    "knowledge_revision",
    "candidate_revision",
    "source_revision",
    "document_revision",
    "code_claim_revision",
    "derived_artifact_revision",
}
TRUST_STATES = {"unreviewed", "assigned", "revoked"}
TRUST_REASON_CODES = {
    "owner_review",
    "owner_pin",
    "owner_correction",
    "owner_retraction",
    "import_review",
    "migration_review",
}


class V2OperationError(RuntimeError):
    """Stable repository/transport error without tenant-existence leakage."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        current: Optional[Mapping[str, Any]] = None,
        proposal: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.current = dict(current) if current is not None else None
        self.proposal = dict(proposal) if proposal is not None else None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }
        if self.current is not None:
            result["current"] = self.current
        if self.proposal is not None:
            result["proposal"] = self.proposal
        return result


class RevisionConflict(V2OperationError):
    def __init__(self, current: Mapping[str, Any], proposal: Mapping[str, Any]) -> None:
        super().__init__(
            "revision_conflict",
            "The record changed; review the current head and proposal.",
            current=current,
            proposal=proposal,
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _clamp(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _tables_ready(conn: sqlite3.Connection) -> bool:
    required = {
        "fm_v2_change_sets",
        "fm_v2_entities",
        "fm_v2_entity_revisions",
        "fm_v2_knowledge_blocks",
        "fm_v2_knowledge_revisions",
        "fm_v2_sources",
        "fm_v2_evidence",
        "fm_v2_outbox",
    }
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'fm_v2_%'"
    ).fetchall()
    return required.issubset({str(row[0]) for row in rows})


def _job_tables_ready(conn: sqlite3.Connection) -> bool:
    required = {"fm_v2_jobs", "fm_v2_attempts"}
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'fm_v2_%'"
    ).fetchall()
    return required.issubset({str(row[0]) for row in rows})


def _require_trust_schema(conn: sqlite3.Connection) -> None:
    """Admit the current native schema without creating missing sidecars."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(fm_v2_trust_assignments)")}
    if not set(['owner_id', 'assignment_id', 'subject_kind', 'subject_id', 'subject_revision', 'workspace_key', 'project_key', 'assignment_revision', 'state', 'trust', 'actor_type', 'actor_id', 'reason_code', 'rationale', 'evidence_json', 'supersedes_assignment_id', 'content_hash', 'created_at']).issubset(present):
        raise V2OperationError("unsupported_contract", "Legacy FM sidecars require .clanker/tools/migrations/python/secondary.py fm-v2-sidecars")



def _require_manifest_schema(conn: sqlite3.Connection) -> None:
    """Admit the current native schema without creating missing sidecars."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(fm_v2_job_manifests)")}
    if not set(['owner_id', 'manifest_id', 'job_id', 'phase', 'selected_json', 'completed_json', 'reused_json', 'failed_json', 'waived_json', 'config_hash', 'model_hash', 'tool_hash', 'input_hash', 'parent_manifest_id', 'content_hash', 'sealed_at']).issubset(present):
        raise V2OperationError("unsupported_contract", "Legacy FM sidecars require .clanker/tools/migrations/python/secondary.py fm-v2-sidecars")



def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise V2OperationError("validation", f"{field} is required")
    return text


def _scope_keys(
    owner: Any,
    workspace_id: Any = None,
    project_id: Any = None,
) -> tuple[str, str, str]:
    owner_key = _required_text(owner, "owner_id")
    workspace = "" if workspace_id is None else str(workspace_id).strip()
    project = "" if project_id is None else str(project_id).strip()
    if workspace_id is not None and not workspace:
        raise V2OperationError("validation", "workspace_id cannot be blank")
    if project_id is not None and not project:
        raise V2OperationError("validation", "project_id cannot be blank")
    if project and not workspace:
        raise V2OperationError("validation", "project_id requires workspace_id")
    return owner_key, workspace, project


def _wire_scope(owner: str, workspace: str, project: str) -> dict[str, Any]:
    return {
        "owner_id": owner,
        "workspace_id": workspace or None,
        "project_id": project or None,
        "session_id": None,
    }


def _scope_is_visible(
    row_workspace: str,
    row_project: str,
    requested_workspace: str,
    requested_project: str,
) -> bool:
    if not row_workspace and not row_project:
        return True
    if not requested_workspace:
        return False
    if row_workspace != requested_workspace:
        return False
    if not row_project:
        return True
    return bool(requested_project and row_project == requested_project)


def _validate_typed_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise V2OperationError("validation", "value must be a typed object")
    typed = dict(value)
    value_type = str(typed.get("type") or "")
    if value_type not in VALUE_TYPES:
        raise V2OperationError("validation", "value.type is unsupported")
    allowed = {"type"} if value_type == "null" else {"type", "value"}
    if set(typed) != allowed:
        raise V2OperationError("validation", "typed value fields do not match its type")
    if value_type == "null":
        return {"type": "null"}
    payload = typed.get("value")
    valid = (
        (value_type == "string" and isinstance(payload, str))
        or (value_type == "number" and isinstance(payload, (int, float)) and not isinstance(payload, bool))
        or (value_type == "boolean" and isinstance(payload, bool))
        or (value_type in {"time", "duration", "uri"} and isinstance(payload, str))
        or (value_type == "object" and isinstance(payload, Mapping))
        or (value_type == "list" and isinstance(payload, list))
        or (
            value_type == "entity_ref"
            and isinstance(payload, Mapping)
            and set(payload) == {"entity_id"}
            and bool(str(payload.get("entity_id") or "").strip())
        )
    )
    if not valid:
        raise V2OperationError("validation", f"value does not match type {value_type}")
    if value_type in {"object", "entity_ref"}:
        payload = dict(payload)
    return {"type": value_type, "value": payload}


def _json_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise V2OperationError("validation", f"{field} must be a list of non-blank strings")
    return list(dict.fromkeys(item.strip() for item in value))


class _AttachmentConnection(sqlite3.Connection):
    def commit(self):
        super().commit()
        from src.openclank.attachment_admission import settle_references
        settle_references(getattr(self, "attachment_claims", []))
        self.attachment_claims = []

    def rollback(self):
        super().rollback()
        from src.openclank.attachment_admission import settle_references
        settle_references(getattr(self, "attachment_claims", []), committed=False)
        self.attachment_claims = []


class V2Repository:
    """Tenant-scoped append-only repository for the canonical v2 tables."""

    def __init__(self, db_path: str = FM_DB_PATH) -> None:
        self.db_path = str(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, factory=_AttachmentConnection)
        conn.attachment_claims = []
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            conn.close()
            raise V2OperationError("validation", "SQLite foreign keys are unavailable")
        if not _tables_ready(conn):
            conn.close()
            raise V2OperationError("unsupported_contract", "Frankenmemory v2 schema is unavailable")
        _require_trust_schema(conn)
        _require_manifest_schema(conn)
        return conn

    @staticmethod
    def _change_set(
        conn: sqlite3.Connection,
        *,
        owner: str,
        actor_type: str,
        actor_id: str,
        reason: str,
        source_event_id: Optional[str],
        payload: Mapping[str, Any],
        now: str,
    ) -> str:
        from src.openclank.attachment_admission import admit_references
        admitted = admit_references(owner, "memery", payload)
        if isinstance(conn, _AttachmentConnection):
            conn.attachment_claims.extend(admitted)
        change_set_id = "cs_v2_" + uuid.uuid4().hex
        conn.execute(
            "INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                owner,
                change_set_id,
                _required_text(actor_type, "actor_type"),
                _required_text(actor_id, "actor_id"),
                _required_text(reason, "reason"),
                source_event_id,
                CONTRACT_VERSION,
                _hash(payload),
                now,
            ),
        )
        return change_set_id

    @staticmethod
    def _outbox(
        conn: sqlite3.Connection,
        *,
        owner: str,
        change_set_id: str,
        kind: str,
        payload: Mapping[str, Any],
        revision: int,
        now: str,
    ) -> None:
        conn.execute(
            "INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                owner,
                "outbox_" + change_set_id,
                change_set_id,
                kind,
                _json(payload),
                revision,
                now,
            ),
        )

    @staticmethod
    def _entity_view(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload"])
        return {
            "entity_id": row["entity_id"],
            "scope": _wire_scope(row["owner_id"], row["workspace_key"], row["project_key"]),
            **payload,
            "revision": row["revision"],
            "content_hash": row["content_hash"],
            "actor_type": row["actor_type"],
            "actor_id": row["actor_id"],
            "reason": row["reason"],
            "change_set_id": row["change_set_id"],
            "previous_revision": row["previous_revision"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _knowledge_view(row: sqlite3.Row) -> dict[str, Any]:
        rationale: Any = row["activation_rationale"]
        try:
            rationale = json.loads(rationale)
        except (TypeError, json.JSONDecodeError):
            pass
        return {
            "block_id": row["block_id"],
            "scope": _wire_scope(row["owner_id"], row["workspace_key"], row["project_key"]),
            "subject_entity_id": row["subject_entity_id"],
            "predicate": row["predicate"],
            "claim_slot": row["claim_slot"],
            "cardinality": row["cardinality"],
            "value": json.loads(row["value_json"]),
            "expected_value_type": row["expected_value_type"],
            "kind": row["kind"],
            "tags": json.loads(row["tags_json"] or "[]"),
            "status": row["status"],
            "confidence": row["confidence"],
            "trust": row["trust"],
            "activation": row["activation"],
            "activation_rationale": rationale,
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "evidence_ids": json.loads(row["evidence_json"] or "[]"),
            "revision": row["revision"],
            "content_hash": row["content_hash"],
            "actor_type": row["actor_type"],
            "actor_id": row["actor_id"],
            "reason": row["reason"],
            "source_event_id": row["source_event_id"],
            "change_set_id": row["change_set_id"],
            "previous_revision": row["previous_revision"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _entity_projection(
        conn: sqlite3.Connection,
        *,
        owner: str,
        entity_id: str,
        revision: int,
        workspace: str,
        project: str,
        payload: Mapping[str, Any],
    ) -> None:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        projection_tables = {
            "fm_v2_entity_aliases",
            "fm_v2_entity_roles",
            "fm_v2_entity_tags",
        }
        if not projection_tables.issubset(tables):
            return
        conn.execute(
            "UPDATE fm_v2_entity_roles SET is_current=0 WHERE owner_id=? AND entity_id=? AND is_current=1",
            (owner, entity_id),
        )
        conn.executemany(
            "INSERT INTO fm_v2_entity_aliases(owner_id,entity_id,revision,alias) VALUES (?,?,?,?)",
            [(owner, entity_id, revision, alias) for alias in payload["aliases"]],
        )
        conn.executemany(
            "INSERT INTO fm_v2_entity_roles(owner_id,entity_id,revision,workspace_key,project_key,role,is_current) VALUES (?,?,?,?,?,?,1)",
            [(owner, entity_id, revision, workspace, project, role) for role in payload["roles"]],
        )
        conn.executemany(
            "INSERT INTO fm_v2_entity_tags(owner_id,entity_id,revision,tag) VALUES (?,?,?,?)",
            [(owner, entity_id, revision, tag) for tag in payload["tags"]],
        )

    def assign_trust(
        self,
        *,
        owner: str,
        subject_kind: str,
        subject_id: str,
        subject_revision: int,
        trust: Optional[float] = None,
        state: str = "assigned",
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        actor_type: str = "owner",
        actor_id: Optional[str] = None,
        reason_code: str = "owner_review",
        rationale: str = "",
        evidence_ids: Optional[list[str]] = None,
        expected_assignment_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Append an owner-relative Trust assignment without rewriting content.

        Technical producer measures (legacy ``confidence`` and parser
        strength) are intentionally not accepted here.  Prompt authority is
        a separate policy decision and cannot be raised by this numeric value.
        """
        owner, workspace, project = _scope_keys(owner, workspace_id, project_id)
        subject_kind = _required_text(subject_kind, "subject_kind")
        subject_id = _required_text(subject_id, "subject_id")
        try:
            subject_revision = int(subject_revision)
        except (TypeError, ValueError) as exc:
            raise V2OperationError("validation", "subject_revision must be positive") from exc
        if subject_kind not in TRUST_SUBJECT_KINDS or subject_revision < 1:
            raise V2OperationError("validation", "unsupported Trust subject")
        state = str(state or "").strip().lower()
        if state not in TRUST_STATES:
            raise V2OperationError("validation", "unsupported Trust state")
        actor_type = str(actor_type or "").strip().lower()
        actor_id = _required_text(actor_id or owner, "actor_id")
        if actor_type != "owner" or actor_id != owner:
            raise V2OperationError("forbidden", "Trust assignments require the authenticated owner")
        reason_code = str(reason_code or "").strip().lower()
        if reason_code not in TRUST_REASON_CODES:
            raise V2OperationError("validation", "unsupported Trust reason_code")
        if state == "assigned":
            try:
                trust = float(trust)
            except (TypeError, ValueError) as exc:
                raise V2OperationError("validation", "assigned Trust requires a finite value") from exc
            if not math.isfinite(trust) or not 0.0 <= trust <= 1.0:
                raise V2OperationError("validation", "Trust must be finite and between 0 and 1")
        else:
            if trust is not None:
                raise V2OperationError("validation", "unreviewed/revoked Trust cannot carry a value")
            trust = None
        evidence = _json_list(evidence_ids, "evidence_ids")
        now = _now()
        payload = {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "subject_revision": subject_revision,
            "workspace_key": workspace,
            "project_key": project,
            "state": state,
            "trust": trust,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "reason_code": reason_code,
            "rationale": str(rationale or ""),
            "evidence_ids": evidence,
        }
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(
                "SELECT * FROM fm_v2_trust_assignments WHERE owner_id=? AND subject_kind=? AND subject_id=? AND subject_revision=? ORDER BY assignment_revision DESC LIMIT 1",
                (owner, subject_kind, subject_id, subject_revision),
            ).fetchone()
            actual_previous = str(latest["assignment_id"]) if latest else None
            if expected_assignment_id is not None and str(expected_assignment_id or "") != (actual_previous or ""):
                current = dict(latest) if latest else {"state": "unreviewed", "trust": None}
                raise RevisionConflict(current, {**payload, "expected_assignment_id": expected_assignment_id})
            assignment_revision = int(latest["assignment_revision"]) + 1 if latest else 1
            content_hash = _hash({**payload, "assignment_revision": assignment_revision, "supersedes_assignment_id": actual_previous})
            assignment_id = "trust_" + content_hash[:32]
            change_set = self._change_set(
                conn,
                owner=owner,
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason_code,
                source_event_id=None,
                payload={"trust_assignment": payload, "assignment_revision": assignment_revision},
                now=now,
            )
            conn.execute(
                "INSERT INTO fm_v2_trust_assignments(owner_id,assignment_id,subject_kind,subject_id,subject_revision,workspace_key,project_key,assignment_revision,state,trust,actor_type,actor_id,reason_code,rationale,evidence_json,supersedes_assignment_id,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner, assignment_id, subject_kind, subject_id, subject_revision, workspace, project, assignment_revision, state, trust, actor_type, actor_id, reason_code, str(rationale or ""), _json(evidence), actual_previous, content_hash, now),
            )
            self._outbox(
                conn,
                owner=owner,
                change_set_id=change_set,
                kind="trust_assignment",
                payload={"assignment_id": assignment_id, "subject_kind": subject_kind, "subject_id": subject_id, "subject_revision": subject_revision},
                revision=assignment_revision,
                now=now,
            )
            conn.commit()
            return {
                "owner_id": owner,
                "assignment_id": assignment_id,
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "subject_revision": subject_revision,
                "scope": _wire_scope(owner, workspace, project),
                "assignment_revision": assignment_revision,
                "state": state,
                "trust": trust,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "reason_code": reason_code,
                "rationale": str(rationale or ""),
                "evidence_ids": evidence,
                "supersedes_assignment_id": actual_previous,
                "content_hash": content_hash,
                "change_set_id": change_set,
                "created_at": now,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_trust(
        self,
        *,
        owner: str,
        subject_kind: str,
        subject_id: str,
        subject_revision: int,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        owner, workspace, project = _scope_keys(owner, workspace_id, project_id)
        if subject_kind not in TRUST_SUBJECT_KINDS:
            raise V2OperationError("validation", "unsupported Trust subject")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM fm_v2_trust_assignments WHERE owner_id=? AND subject_kind=? AND subject_id=? AND subject_revision=? ORDER BY assignment_revision DESC LIMIT 1",
                (owner, subject_kind, str(subject_id), int(subject_revision)),
            ).fetchone()
            if not row or not _scope_is_visible(row["workspace_key"], row["project_key"], workspace, project):
                return {
                    "owner_id": owner,
                    "subject_kind": subject_kind,
                    "subject_id": str(subject_id),
                    "subject_revision": int(subject_revision),
                    "scope": _wire_scope(owner, workspace, project),
                    "assignment_id": None,
                    "state": "unreviewed",
                    "trust": None,
                }
            return {
                "owner_id": owner,
                "assignment_id": row["assignment_id"],
                "subject_kind": row["subject_kind"],
                "subject_id": row["subject_id"],
                "subject_revision": row["subject_revision"],
                "scope": _wire_scope(owner, row["workspace_key"], row["project_key"]),
                "assignment_revision": row["assignment_revision"],
                "state": row["state"],
                "trust": row["trust"],
                "actor_type": row["actor_type"],
                "actor_id": row["actor_id"],
                "reason_code": row["reason_code"],
                "rationale": row["rationale"],
                "evidence_ids": json.loads(row["evidence_json"] or "[]"),
                "supersedes_assignment_id": row["supersedes_assignment_id"],
                "content_hash": row["content_hash"],
                "created_at": row["created_at"],
            }
        finally:
            conn.close()

    def create_entity(
        self,
        *,
        owner: str,
        entity_id: str,
        entity_type: str,
        canonical_label: str,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        aliases: Optional[list[str]] = None,
        roles: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
        status: str = "active",
        actor_type: str = "user",
        actor_id: Optional[str] = None,
        reason: str = "create entity",
        source_event_id: Optional[str] = None,
    ) -> dict[str, Any]:
        owner, workspace, project = _scope_keys(owner, workspace_id, project_id)
        entity_id = _required_text(entity_id, "entity_id")
        payload = {
            "entity_type": _required_text(entity_type, "entity_type"),
            "canonical_label": str(canonical_label),
            "aliases": _json_list(aliases, "aliases"),
            "roles": _json_list(roles, "roles"),
            "tags": _json_list(tags, "tags"),
            "status": _required_text(status, "status"),
        }
        now = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM fm_v2_entities WHERE owner_id=? AND entity_id=?",
                (owner, entity_id),
            ).fetchone():
                raise V2OperationError("validation", "entity_id already exists")
            change_set = self._change_set(
                conn,
                owner=owner,
                actor_type=actor_type,
                actor_id=actor_id or owner,
                reason=reason,
                source_event_id=source_event_id,
                payload=payload,
                now=now,
            )
            conn.execute(
                "INSERT INTO fm_v2_entities(owner_id,entity_id,workspace_key,project_key,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,0,?)",
                (owner, entity_id, workspace, project, change_set, now),
            )
            conn.execute(
                "INSERT INTO fm_v2_entity_revisions(owner_id,entity_id,revision,payload,content_hash,actor_type,actor_id,reason,change_set_id,previous_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (owner, entity_id, 1, _json(payload), _hash(payload), actor_type, actor_id or owner, reason, change_set, None, now),
            )
            self._entity_projection(
                conn,
                owner=owner,
                entity_id=entity_id,
                revision=1,
                workspace=workspace,
                project=project,
                payload=payload,
            )
            conn.execute(
                "UPDATE fm_v2_entities SET current_revision=1 WHERE owner_id=? AND entity_id=?",
                (owner, entity_id),
            )
            self._outbox(
                conn,
                owner=owner,
                change_set_id=change_set,
                kind="entity_revision",
                payload={"entity_id": entity_id, "revision": 1},
                revision=1,
                now=now,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_entity(owner=owner, entity_id=entity_id, workspace_id=workspace or None, project_id=project or None)

    def get_entity(
        self,
        *,
        owner: str,
        entity_id: str,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        revision: Optional[int] = None,
    ) -> dict[str, Any]:
        owner, workspace, project = _scope_keys(owner, workspace_id, project_id)
        entity_id = _required_text(entity_id, "entity_id")
        conn = self._connect()
        try:
            identity = conn.execute(
                "SELECT * FROM fm_v2_entities WHERE owner_id=? AND entity_id=?",
                (owner, entity_id),
            ).fetchone()
            if not identity or not _scope_is_visible(identity["workspace_key"], identity["project_key"], workspace, project):
                raise V2OperationError("not_found", "entity not found")
            wanted = int(revision or identity["current_revision"])
            row = conn.execute(
                "SELECT e.owner_id,e.entity_id,e.workspace_key,e.project_key,r.* FROM fm_v2_entities e JOIN fm_v2_entity_revisions r ON r.owner_id=e.owner_id AND r.entity_id=e.entity_id WHERE e.owner_id=? AND e.entity_id=? AND r.revision=?",
                (owner, entity_id, wanted),
            ).fetchone()
            if not row:
                raise V2OperationError("not_found", "entity revision not found")
            return self._entity_view(row)
        finally:
            conn.close()

    def update_entity(
        self,
        *,
        owner: str,
        entity_id: str,
        expected_revision: int,
        payload: Mapping[str, Any],
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        actor_type: str = "user",
        actor_id: Optional[str] = None,
        reason: str = "update entity",
    ) -> dict[str, Any]:
        current = self.get_entity(
            owner=owner,
            entity_id=entity_id,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        merged = {
            key: payload.get(key, current[key])
            for key in ("entity_type", "canonical_label", "aliases", "roles", "tags", "status")
        }
        owner, workspace, project = _scope_keys(owner, workspace_id, project_id)
        entity_id = _required_text(entity_id, "entity_id")
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError) as exc:
            raise V2OperationError("validation", "expected_revision is required") from exc
        merged = {
            "entity_type": _required_text(merged["entity_type"], "entity_type"),
            "canonical_label": str(merged["canonical_label"]),
            "aliases": _json_list(merged["aliases"], "aliases"),
            "roles": _json_list(merged["roles"], "roles"),
            "tags": _json_list(merged["tags"], "tags"),
            "status": _required_text(merged["status"], "status"),
        }
        now = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            identity = conn.execute(
                "SELECT * FROM fm_v2_entities WHERE owner_id=? AND entity_id=?",
                (owner, entity_id),
            ).fetchone()
            if not identity or (identity["workspace_key"], identity["project_key"]) != (workspace, project):
                raise V2OperationError("not_found", "entity not found")
            if int(identity["current_revision"]) != expected_revision:
                actual = self._entity_at_connection(conn, owner, entity_id, int(identity["current_revision"]))
                raise RevisionConflict(actual, {"expected_revision": expected_revision, **merged})
            revision = expected_revision + 1
            change_set = self._change_set(
                conn,
                owner=owner,
                actor_type=actor_type,
                actor_id=actor_id or owner,
                reason=reason,
                source_event_id=None,
                payload=merged,
                now=now,
            )
            conn.execute(
                "INSERT INTO fm_v2_entity_revisions(owner_id,entity_id,revision,payload,content_hash,actor_type,actor_id,reason,change_set_id,previous_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (owner, entity_id, revision, _json(merged), _hash(merged), actor_type, actor_id or owner, reason, change_set, expected_revision, now),
            )
            self._entity_projection(
                conn,
                owner=owner,
                entity_id=entity_id,
                revision=revision,
                workspace=workspace,
                project=project,
                payload=merged,
            )
            changed = conn.execute(
                "UPDATE fm_v2_entities SET current_revision=? WHERE owner_id=? AND entity_id=? AND current_revision=?",
                (revision, owner, entity_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise RevisionConflict(current, {"expected_revision": expected_revision, **merged})
            self._outbox(
                conn,
                owner=owner,
                change_set_id=change_set,
                kind="entity_revision",
                payload={"entity_id": entity_id, "revision": revision},
                revision=revision,
                now=now,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_entity(owner=owner, entity_id=entity_id, workspace_id=workspace or None, project_id=project or None)

    @classmethod
    def _entity_at_connection(cls, conn: sqlite3.Connection, owner: str, entity_id: str, revision: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT e.owner_id,e.entity_id,e.workspace_key,e.project_key,r.* FROM fm_v2_entities e JOIN fm_v2_entity_revisions r ON r.owner_id=e.owner_id AND r.entity_id=e.entity_id WHERE e.owner_id=? AND e.entity_id=? AND r.revision=?",
            (owner, entity_id, revision),
        ).fetchone()
        if not row:
            raise V2OperationError("not_found", "entity revision not found")
        return cls._entity_view(row)

    def entity_history(
        self,
        *,
        owner: str,
        entity_id: str,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        current = self.get_entity(owner=owner, entity_id=entity_id, workspace_id=workspace_id, project_id=project_id)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT e.owner_id,e.entity_id,e.workspace_key,e.project_key,r.* FROM fm_v2_entities e JOIN fm_v2_entity_revisions r ON r.owner_id=e.owner_id AND r.entity_id=e.entity_id WHERE e.owner_id=? AND e.entity_id=? ORDER BY r.revision",
                (current["scope"]["owner_id"], entity_id),
            ).fetchall()
            return [self._entity_view(row) for row in rows]
        finally:
            conn.close()

    @staticmethod
    def _prepare_knowledge_payload(
        payload: Mapping[str, Any],
        current: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        def selected(name: str, default: Any = None) -> Any:
            if name in payload:
                return payload[name]
            if current is not None and name in current:
                return current[name]
            return default

        value = _validate_typed_value(selected("value"))
        status = _required_text(selected("status", "active"), "status")
        if status not in KNOWLEDGE_STATUSES:
            raise V2OperationError("validation", "knowledge status is unsupported")
        expected_value_type = selected("expected_value_type")
        if expected_value_type is not None:
            expected_value_type = _required_text(expected_value_type, "expected_value_type")
            if expected_value_type not in VALUE_TYPES:
                raise V2OperationError("validation", "expected_value_type is unsupported")
        if status == "open" and (value["type"] != "null" or not expected_value_type):
            raise V2OperationError("validation", "open knowledge requires null value and expected_value_type")
        valid_from = selected("valid_from")
        valid_to = selected("valid_to")
        if valid_from is not None:
            valid_from = _required_text(valid_from, "valid_from")
        if valid_to is not None:
            valid_to = _required_text(valid_to, "valid_to")
        if valid_from and valid_to and valid_to <= valid_from:
            raise V2OperationError("validation", "valid_to must be after valid_from")
        rationale = selected("activation_rationale", "")
        if not isinstance(rationale, (str, Mapping)):
            raise V2OperationError("validation", "activation_rationale must be text or an object")
        evidence_ids = _json_list(selected("evidence_ids", []), "evidence_ids")
        return {
            "value": value,
            "expected_value_type": expected_value_type,
            "kind": _required_text(selected("kind", "fact"), "kind"),
            "tags": _json_list(selected("tags", []), "tags"),
            "status": status,
            "confidence": _clamp(selected("confidence"), 0.5),
            "trust": _clamp(selected("trust"), 0.5),
            "activation": _required_text(selected("activation", "manual"), "activation"),
            "activation_rationale": dict(rationale) if isinstance(rationale, Mapping) else rationale,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "evidence_ids": evidence_ids,
        }

    @staticmethod
    def _validate_knowledge_relations(
        conn: sqlite3.Connection,
        *,
        owner: str,
        workspace: str,
        project: str,
        subject_entity_id: str,
        predicate: str,
        cardinality: str,
        payload: Mapping[str, Any],
    ) -> None:
        entity = conn.execute(
            "SELECT workspace_key,project_key FROM fm_v2_entities WHERE owner_id=? AND entity_id=?",
            (owner, subject_entity_id),
        ).fetchone()
        if not entity or (entity["workspace_key"], entity["project_key"]) != (workspace, project):
            raise V2OperationError("validation", "subject entity must exist in the exact write scope")
        definition = conn.execute(
            "SELECT value_type,cardinality,allow_unknown FROM fm_v2_predicates WHERE predicate=?",
            (predicate,),
        ).fetchone()
        if definition:
            actual_type = payload["value"]["type"]
            expected_type = payload["expected_value_type"] if actual_type == "null" else actual_type
            if expected_type != definition["value_type"] or cardinality != definition["cardinality"]:
                raise V2OperationError("validation", "predicate type or cardinality mismatch")
        for evidence_id in payload["evidence_ids"]:
            if not conn.execute(
                "SELECT 1 FROM fm_v2_evidence WHERE owner_id=? AND evidence_id=?",
                (owner, evidence_id),
            ).fetchone():
                raise V2OperationError("validation", "evidence does not exist in the write scope")

    @staticmethod
    def _insert_knowledge_revision(
        conn: sqlite3.Connection,
        *,
        identity: sqlite3.Row,
        revision: int,
        payload: Mapping[str, Any],
        change_set: str,
        actor_type: str,
        actor_id: str,
        reason: str,
        source_event_id: Optional[str],
        now: str,
    ) -> None:
        canonical = {
            "subject_entity_id": identity["subject_entity_id"],
            "predicate": identity["predicate"],
            "claim_slot": identity["claim_slot"],
            "cardinality": identity["cardinality"],
            **payload,
        }
        conn.execute(
            "INSERT INTO fm_v2_knowledge_revisions(owner_id,block_id,revision,value_type,value_json,expected_value_type,kind,tags_json,status,confidence,trust,activation,activation_rationale,valid_from,valid_to,evidence_json,content_hash,actor_type,actor_id,reason,source_event_id,change_set_id,previous_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity["owner_id"],
                identity["block_id"],
                revision,
                payload["value"]["type"],
                _json(payload["value"]),
                payload["expected_value_type"],
                payload["kind"],
                _json(payload["tags"]),
                payload["status"],
                payload["confidence"],
                payload["trust"],
                payload["activation"],
                _json(payload["activation_rationale"]) if isinstance(payload["activation_rationale"], Mapping) else payload["activation_rationale"],
                payload["valid_from"],
                payload["valid_to"],
                _json(payload["evidence_ids"]),
                _hash(canonical),
                actor_type,
                actor_id,
                reason,
                source_event_id,
                change_set,
                revision - 1 or None,
                now,
            ),
        )
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "fm_v2_revision_evidence" in tables:
            conn.executemany(
                "INSERT INTO fm_v2_revision_evidence(owner_id,block_id,revision,evidence_id) VALUES (?,?,?,?)",
                [
                    (identity["owner_id"], identity["block_id"], revision, evidence_id)
                    for evidence_id in payload["evidence_ids"]
                ],
            )

    @classmethod
    def _knowledge_at_connection(
        cls,
        conn: sqlite3.Connection,
        owner: str,
        block_id: str,
        revision: int,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT b.owner_id,b.block_id,b.workspace_key,b.project_key,b.subject_entity_id,b.predicate,b.claim_slot,b.cardinality,r.* FROM fm_v2_knowledge_blocks b JOIN fm_v2_knowledge_revisions r ON r.owner_id=b.owner_id AND r.block_id=b.block_id WHERE b.owner_id=? AND b.block_id=? AND r.revision=?",
            (owner, block_id, revision),
        ).fetchone()
        if not row:
            raise V2OperationError("not_found", "knowledge revision not found")
        return cls._knowledge_view(row)

    def create_knowledge(
        self,
        *,
        owner: str,
        block_id: str,
        subject_entity_id: str,
        predicate: str,
        claim_slot: str,
        payload: Mapping[str, Any],
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        cardinality: str = "single",
        actor_type: str = "user",
        actor_id: Optional[str] = None,
        reason: str = "create knowledge",
        source_event_id: Optional[str] = None,
    ) -> dict[str, Any]:
        owner, workspace, project = _scope_keys(owner, workspace_id, project_id)
        block_id = _required_text(block_id, "block_id")
        subject_entity_id = _required_text(subject_entity_id, "subject_entity_id")
        predicate = _required_text(predicate, "predicate")
        claim_slot = _required_text(claim_slot, "claim_slot")
        cardinality = _required_text(cardinality, "cardinality")
        if cardinality not in CARDINALITIES:
            raise V2OperationError("validation", "cardinality is unsupported")
        prepared = self._prepare_knowledge_payload(payload)
        now = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?",
                (owner, block_id),
            ).fetchone():
                raise V2OperationError("validation", "block_id already exists")
            self._validate_knowledge_relations(
                conn,
                owner=owner,
                workspace=workspace,
                project=project,
                subject_entity_id=subject_entity_id,
                predicate=predicate,
                cardinality=cardinality,
                payload=prepared,
            )
            canonical = {
                "block_id": block_id,
                "subject_entity_id": subject_entity_id,
                "predicate": predicate,
                "claim_slot": claim_slot,
                "cardinality": cardinality,
                **prepared,
            }
            change_set = self._change_set(
                conn,
                owner=owner,
                actor_type=actor_type,
                actor_id=actor_id or owner,
                reason=reason,
                source_event_id=source_event_id,
                payload=canonical,
                now=now,
            )
            conn.execute(
                "INSERT INTO fm_v2_knowledge_blocks(owner_id,block_id,workspace_key,project_key,subject_entity_id,predicate,claim_slot,cardinality,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
                (owner, block_id, workspace, project, subject_entity_id, predicate, claim_slot, cardinality, change_set, now),
            )
            identity = conn.execute(
                "SELECT * FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?",
                (owner, block_id),
            ).fetchone()
            self._insert_knowledge_revision(
                conn,
                identity=identity,
                revision=1,
                payload=prepared,
                change_set=change_set,
                actor_type=actor_type,
                actor_id=actor_id or owner,
                reason=reason,
                source_event_id=source_event_id,
                now=now,
            )
            conn.execute(
                "UPDATE fm_v2_knowledge_blocks SET current_revision=1 WHERE owner_id=? AND block_id=?",
                (owner, block_id),
            )
            self._outbox(
                conn,
                owner=owner,
                change_set_id=change_set,
                kind="knowledge_revision",
                payload={"block_id": block_id, "revision": 1},
                revision=1,
                now=now,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_knowledge(owner=owner, block_id=block_id, workspace_id=workspace or None, project_id=project or None)

    def get_knowledge(
        self,
        *,
        owner: str,
        block_id: str,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        revision: Optional[int] = None,
    ) -> dict[str, Any]:
        owner, workspace, project = _scope_keys(owner, workspace_id, project_id)
        block_id = _required_text(block_id, "block_id")
        conn = self._connect()
        try:
            identity = conn.execute(
                "SELECT * FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?",
                (owner, block_id),
            ).fetchone()
            if not identity or not _scope_is_visible(identity["workspace_key"], identity["project_key"], workspace, project):
                raise V2OperationError("not_found", "knowledge block not found")
            wanted = int(revision or identity["current_revision"])
            return self._knowledge_at_connection(conn, owner, block_id, wanted)
        finally:
            conn.close()

    def update_knowledge(
        self,
        *,
        owner: str,
        block_id: str,
        expected_revision: int,
        payload: Mapping[str, Any],
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        actor_type: str = "user",
        actor_id: Optional[str] = None,
        reason: str = "update knowledge",
        source_event_id: Optional[str] = None,
    ) -> dict[str, Any]:
        current = self.get_knowledge(
            owner=owner,
            block_id=block_id,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        prepared = self._prepare_knowledge_payload(payload, current)
        owner, workspace, project = _scope_keys(owner, workspace_id, project_id)
        block_id = _required_text(block_id, "block_id")
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError) as exc:
            raise V2OperationError("validation", "expected_revision is required") from exc
        now = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            identity = conn.execute(
                "SELECT * FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?",
                (owner, block_id),
            ).fetchone()
            if not identity or (identity["workspace_key"], identity["project_key"]) != (workspace, project):
                raise V2OperationError("not_found", "knowledge block not found")
            if int(identity["current_revision"]) != expected_revision:
                actual = self._knowledge_at_connection(conn, owner, block_id, int(identity["current_revision"]))
                raise RevisionConflict(actual, {"expected_revision": expected_revision, **prepared})
            self._validate_knowledge_relations(
                conn,
                owner=owner,
                workspace=workspace,
                project=project,
                subject_entity_id=identity["subject_entity_id"],
                predicate=identity["predicate"],
                cardinality=identity["cardinality"],
                payload=prepared,
            )
            revision = expected_revision + 1
            if source_event_id is None:
                # A transition revises the claim, not its origin: carry the
                # source lineage forward so read paths keyed on the source
                # event (legacy-presence gate, memory-id derivation) keep
                # resolving the block after an edit/resolve/reopen.
                carried = conn.execute(
                    "SELECT source_event_id FROM fm_v2_knowledge_revisions "
                    "WHERE owner_id=? AND block_id=? AND revision=?",
                    (owner, block_id, expected_revision),
                ).fetchone()
                source_event_id = str(carried[0]) if carried and carried[0] else None
            change_set = self._change_set(
                conn,
                owner=owner,
                actor_type=actor_type,
                actor_id=actor_id or owner,
                reason=reason,
                source_event_id=source_event_id,
                payload=prepared,
                now=now,
            )
            self._insert_knowledge_revision(
                conn,
                identity=identity,
                revision=revision,
                payload=prepared,
                change_set=change_set,
                actor_type=actor_type,
                actor_id=actor_id or owner,
                reason=reason,
                source_event_id=source_event_id,
                now=now,
            )
            changed = conn.execute(
                "UPDATE fm_v2_knowledge_blocks SET current_revision=? WHERE owner_id=? AND block_id=? AND current_revision=?",
                (revision, owner, block_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise RevisionConflict(current, {"expected_revision": expected_revision, **prepared})
            self._outbox(
                conn,
                owner=owner,
                change_set_id=change_set,
                kind="knowledge_revision",
                payload={"block_id": block_id, "revision": revision},
                revision=revision,
                now=now,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_knowledge(owner=owner, block_id=block_id, workspace_id=workspace or None, project_id=project or None)

    def knowledge_history(
        self,
        *,
        owner: str,
        block_id: str,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        current = self.get_knowledge(owner=owner, block_id=block_id, workspace_id=workspace_id, project_id=project_id)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT b.owner_id,b.block_id,b.workspace_key,b.project_key,b.subject_entity_id,b.predicate,b.claim_slot,b.cardinality,r.* FROM fm_v2_knowledge_blocks b JOIN fm_v2_knowledge_revisions r ON r.owner_id=b.owner_id AND r.block_id=b.block_id WHERE b.owner_id=? AND b.block_id=? ORDER BY r.revision",
                (current["scope"]["owner_id"], block_id),
            ).fetchall()
            return [self._knowledge_view(row) for row in rows]
        finally:
            conn.close()

    def list_questions(
        self,
        *,
        owner: str,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        owner, workspace, project = _scope_keys(owner, workspace_id, project_id)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT b.owner_id,b.block_id,b.workspace_key,b.project_key,b.subject_entity_id,b.predicate,b.claim_slot,b.cardinality,r.* FROM fm_v2_knowledge_blocks b JOIN fm_v2_knowledge_revisions r ON r.owner_id=b.owner_id AND r.block_id=b.block_id AND r.revision=b.current_revision WHERE b.owner_id=? AND r.status='open' ORDER BY r.created_at,b.block_id",
                (owner,),
            ).fetchall()
            return [
                self._knowledge_view(row)
                for row in rows
                if _scope_is_visible(row["workspace_key"], row["project_key"], workspace, project)
            ]
        finally:
            conn.close()

    def knowledge_diff(
        self,
        *,
        owner: str,
        block_id: str,
        from_revision: int,
        to_revision: int,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        before = self.get_knowledge(owner=owner, block_id=block_id, workspace_id=workspace_id, project_id=project_id, revision=from_revision)
        after = self.get_knowledge(owner=owner, block_id=block_id, workspace_id=workspace_id, project_id=project_id, revision=to_revision)
        ignored = {"revision", "content_hash", "actor_type", "actor_id", "reason", "change_set_id", "previous_revision", "created_at"}
        changes = {
            key: {"before": before.get(key), "after": after.get(key)}
            for key in sorted((set(before) | set(after)) - ignored)
            if before.get(key) != after.get(key)
        }
        return {"block_id": block_id, "from_revision": int(from_revision), "to_revision": int(to_revision), "changes": changes}

    def knowledge_blame(
        self,
        *,
        owner: str,
        block_id: str,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        history = self.knowledge_history(owner=owner, block_id=block_id, workspace_id=workspace_id, project_id=project_id)
        ignored = {"revision", "content_hash", "actor_type", "actor_id", "reason", "change_set_id", "previous_revision", "created_at"}
        blamed: dict[str, Any] = {}
        previous: dict[str, Any] = {}
        for revision in history:
            for field in sorted(set(revision) - ignored):
                if field not in previous or previous[field] != revision[field]:
                    blamed[field] = {
                        "revision": revision["revision"],
                        "actor_type": revision["actor_type"],
                        "actor_id": revision["actor_id"],
                        "reason": revision["reason"],
                        "created_at": revision["created_at"],
                    }
                previous[field] = revision[field]
        return {"block_id": block_id, "fields": blamed}

    def revert_knowledge(
        self,
        *,
        owner: str,
        block_id: str,
        expected_revision: int,
        target_revision: int,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        actor_type: str = "user",
        actor_id: Optional[str] = None,
        reason: str = "revert knowledge",
    ) -> dict[str, Any]:
        target = self.get_knowledge(owner=owner, block_id=block_id, workspace_id=workspace_id, project_id=project_id, revision=target_revision)
        payload = {
            key: target[key]
            for key in (
                "value", "expected_value_type", "kind", "tags", "status", "confidence",
                "trust", "activation", "activation_rationale", "valid_from", "valid_to", "evidence_ids",
            )
        }
        return self.update_knowledge(
            owner=owner,
            block_id=block_id,
            expected_revision=expected_revision,
            payload=payload,
            workspace_id=workspace_id,
            project_id=project_id,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=f"{reason} to revision {int(target_revision)}",
        )

    def record_conflict(
        self,
        *,
        owner: str,
        block_id: str,
        expected_revision: int,
        proposal: Mapping[str, Any],
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        actor_type: str = "user",
        actor_id: Optional[str] = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        current = self.get_knowledge(owner=owner, block_id=block_id, workspace_id=workspace_id, project_id=project_id)
        prepared = self._prepare_knowledge_payload(proposal, current)
        owner, workspace, project = _scope_keys(owner, workspace_id, project_id)
        now = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            identity = conn.execute(
                "SELECT * FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?",
                (owner, block_id),
            ).fetchone()
            if not identity or (identity["workspace_key"], identity["project_key"]) != (workspace, project):
                raise V2OperationError("not_found", "knowledge block not found")
            conflict_id = "conflict_" + uuid.uuid4().hex
            conflict_payload = {"expected_revision": int(expected_revision), **prepared}
            change_set = self._change_set(
                conn,
                owner=owner,
                actor_type=actor_type,
                actor_id=actor_id or owner,
                reason="record conflict",
                source_event_id=None,
                payload=conflict_payload,
                now=now,
            )
            conn.execute(
                "INSERT INTO fm_v2_conflicts(owner_id,conflict_id,block_id,current_revision,competing_payload,state,rationale,created_change_set_id,created_at) VALUES (?,?,?,?,?,'open',?,?,?)",
                (owner, conflict_id, block_id, identity["current_revision"], _json(conflict_payload), rationale, change_set, now),
            )
            self._outbox(
                conn,
                owner=owner,
                change_set_id=change_set,
                kind="knowledge_conflict",
                payload={"block_id": block_id, "conflict_id": conflict_id, "current_revision": identity["current_revision"]},
                revision=int(identity["current_revision"]),
                now=now,
            )
            conn.commit()
            return {
                "conflict_id": conflict_id,
                "block_id": block_id,
                "state": "open",
                "current": current,
                "proposal": conflict_payload,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def resolve_conflict(
        self,
        *,
        owner: str,
        conflict_id: str,
        expected_revision: int,
        payload: Mapping[str, Any],
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        actor_type: str = "user",
        actor_id: Optional[str] = None,
        rationale: str = "resolve conflict",
    ) -> dict[str, Any]:
        owner, workspace, project = _scope_keys(owner, workspace_id, project_id)
        conflict_id = _required_text(conflict_id, "conflict_id")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conflict = conn.execute(
                "SELECT c.*,b.workspace_key,b.project_key,b.subject_entity_id,b.predicate,b.claim_slot,b.cardinality,b.current_revision AS block_revision FROM fm_v2_conflicts c JOIN fm_v2_knowledge_blocks b ON b.owner_id=c.owner_id AND b.block_id=c.block_id WHERE c.owner_id=? AND c.conflict_id=? AND c.state='open'",
                (owner, conflict_id),
            ).fetchone()
            if not conflict or (conflict["workspace_key"], conflict["project_key"]) != (workspace, project):
                raise V2OperationError("not_found", "conflict not found")
            current = self._knowledge_at_connection(conn, owner, conflict["block_id"], int(conflict["block_revision"]))
            prepared = self._prepare_knowledge_payload(payload, current)
            if int(conflict["block_revision"]) != int(expected_revision):
                raise RevisionConflict(current, {"expected_revision": int(expected_revision), **prepared})
            identity = conn.execute(
                "SELECT * FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?",
                (owner, conflict["block_id"]),
            ).fetchone()
            self._validate_knowledge_relations(
                conn,
                owner=owner,
                workspace=workspace,
                project=project,
                subject_entity_id=identity["subject_entity_id"],
                predicate=identity["predicate"],
                cardinality=identity["cardinality"],
                payload=prepared,
            )
            revision = int(expected_revision) + 1
            now = _now()
            change_set = self._change_set(
                conn,
                owner=owner,
                actor_type=actor_type,
                actor_id=actor_id or owner,
                reason=rationale,
                source_event_id=None,
                payload={"resolved_conflict_ids": [conflict_id], **prepared},
                now=now,
            )
            self._insert_knowledge_revision(
                conn,
                identity=identity,
                revision=revision,
                payload=prepared,
                change_set=change_set,
                actor_type=actor_type,
                actor_id=actor_id or owner,
                reason=rationale,
                source_event_id=None,
                now=now,
            )
            if conn.execute(
                "UPDATE fm_v2_knowledge_blocks SET current_revision=? WHERE owner_id=? AND block_id=? AND current_revision=?",
                (revision, owner, conflict["block_id"], expected_revision),
            ).rowcount != 1:
                raise RevisionConflict(current, {"expected_revision": int(expected_revision), **prepared})
            conn.execute(
                "UPDATE fm_v2_conflicts SET state='resolved',rationale=?,resolved_change_set_id=?,resolved_at=? WHERE owner_id=? AND conflict_id=? AND state='open'",
                (rationale, change_set, now, owner, conflict_id),
            )
            self._outbox(
                conn,
                owner=owner,
                change_set_id=change_set,
                kind="knowledge_revision",
                payload={"block_id": conflict["block_id"], "revision": revision, "resolved_conflict_ids": [conflict_id]},
                revision=revision,
                now=now,
            )
            conn.commit()
            return self.get_knowledge(owner=owner, block_id=conflict["block_id"], workspace_id=workspace or None, project_id=project or None)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_knowledge(
        self,
        *,
        owner: str,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        statuses: Optional[list[str]] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        owner, workspace, project = _scope_keys(owner, workspace_id, project_id)
        selected_statuses = statuses or ["open", "active"]
        if not selected_statuses or any(status not in KNOWLEDGE_STATUSES for status in selected_statuses):
            raise V2OperationError("validation", "statuses contain an unsupported value")
        try:
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError) as exc:
            raise V2OperationError("validation", "limit must be an integer") from exc
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in selected_statuses)
            rows = conn.execute(
                f"SELECT b.owner_id,b.block_id,b.workspace_key,b.project_key,b.subject_entity_id,b.predicate,b.claim_slot,b.cardinality,r.* FROM fm_v2_knowledge_blocks b JOIN fm_v2_knowledge_revisions r ON r.owner_id=b.owner_id AND r.block_id=b.block_id AND r.revision=b.current_revision WHERE b.owner_id=? AND r.status IN ({placeholders}) ORDER BY r.created_at,b.block_id",
                [owner, *selected_statuses],
            ).fetchall()
            return [
                self._knowledge_view(row)
                for row in rows
                if _scope_is_visible(row["workspace_key"], row["project_key"], workspace, project)
            ][:limit]
        finally:
            conn.close()


def execute_v2_operation(
    request: Mapping[str, Any],
    *,
    authenticated_owner: str,
    enabled: bool = False,
    db_path: str = FM_DB_PATH,
) -> dict[str, Any]:
    """Execute one disabled-by-default v2 wire operation.

    The authenticated owner is transport authority. A payload owner mismatch
    is rejected before repository lookup, so it cannot reveal whether another
    tenant's identifier exists.
    """
    operation = str(request.get("operation") or "") if isinstance(request, Mapping) else ""
    response: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "operation": operation or "invalid",
        "request_id": request.get("request_id") if isinstance(request, Mapping) else None,
    }
    try:
        if not enabled:
            raise V2OperationError("unsupported_contract", "Frankenmemory v2 transport is disabled")
        if not isinstance(request, Mapping) or request.get("contract_version") != CONTRACT_VERSION:
            raise V2OperationError("unsupported_contract", "unsupported Frankenmemory contract version")
        scope = request.get("scope")
        if not isinstance(scope, Mapping):
            raise V2OperationError("validation", "scope is required")
        authenticated_owner = _required_text(authenticated_owner, "authenticated_owner")
        requested_owner = _required_text(scope.get("owner_id"), "scope.owner_id")
        if requested_owner != authenticated_owner:
            raise V2OperationError("scope_denied", "scope is not available")
        owner, workspace, project = _scope_keys(
            authenticated_owner,
            scope.get("workspace_id"),
            scope.get("project_id"),
        )
        response["scope"] = _wire_scope(owner, workspace, project)
        payload = request.get("payload") or {}
        if not isinstance(payload, Mapping):
            raise V2OperationError("validation", "payload must be an object")
        args = dict(payload)
        common = {
            "owner": owner,
            "workspace_id": workspace or None,
            "project_id": project or None,
        }
        repository = V2Repository(db_path)
        exact_scopes = [_wire_scope(owner, "", "")]
        if workspace:
            exact_scopes.append(_wire_scope(owner, workspace, ""))
        if project:
            exact_scopes.append(_wire_scope(owner, workspace, project))
        operations = {
            "scope.resolve": lambda: {"exact_scopes": exact_scopes},
            "entity.create": lambda: repository.create_entity(**common, **args),
            "entity.get": lambda: repository.get_entity(**common, **args),
            "entity.update": lambda: repository.update_entity(**common, **args),
            "entity.history": lambda: repository.entity_history(**common, **args),
            "knowledge.create": lambda: repository.create_knowledge(**common, **args),
            "knowledge.get": lambda: repository.get_knowledge(**common, **args),
            "knowledge.list": lambda: repository.list_knowledge(**common, **args),
            "knowledge.questions": lambda: repository.list_questions(**common, **args),
            "knowledge.update": lambda: repository.update_knowledge(**common, **args),
            "knowledge.history": lambda: repository.knowledge_history(**common, **args),
            "knowledge.diff": lambda: repository.knowledge_diff(**common, **args),
            "knowledge.blame": lambda: repository.knowledge_blame(**common, **args),
            "knowledge.revert": lambda: repository.revert_knowledge(**common, **args),
            "conflict.record": lambda: repository.record_conflict(**common, **args),
            "conflict.resolve": lambda: repository.resolve_conflict(**common, **args),
            "trust.assign": lambda: repository.assign_trust(**common, **args),
            "trust.get": lambda: repository.get_trust(**common, **args),
        }
        handler = operations.get(operation)
        if handler is None:
            raise V2OperationError("validation", "operation is unsupported")
        response["payload"] = handler()
    except V2OperationError as exc:
        response.setdefault(
            "scope",
            {"owner_id": str(authenticated_owner or ""), "workspace_id": None, "project_id": None, "session_id": None},
        )
        response["payload"] = {"error": exc.as_dict()}
    except TypeError as exc:
        response["payload"] = {
            "error": V2OperationError("validation", f"invalid operation payload: {exc}").as_dict()
        }
    if response.get("request_id") is None:
        response.pop("request_id", None)
    return response


def claim_job(
    *,
    owner: str,
    job_id: str,
    worker_id: str,
    lease_seconds: int = 120,
    db_path: str = FM_DB_PATH,
) -> Optional[dict[str, Any]]:
    """Atomically claim one queued v2 job with a fencing lease.

    A stale worker cannot complete a newer attempt: every completion must
    present the returned attempt ID, worker, and monotonically increasing epoch.
    """
    owner = str(owner or "").strip()
    job_id = str(job_id or "").strip()
    worker_id = str(worker_id or "").strip()
    if not owner or not job_id or not worker_id or lease_seconds < 1:
        return None
    now = datetime.now(timezone.utc)
    expires = now.timestamp() + int(lease_seconds)
    now_text = now.isoformat()
    expires_text = datetime.fromtimestamp(expires, timezone.utc).isoformat()
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if not _job_tables_ready(conn):
                return None
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT * FROM fm_v2_jobs WHERE owner_id=? AND job_id=?",
                (owner, job_id),
            ).fetchone()
            if not job:
                conn.rollback()
                return None
            if job["state"] == "active" and job["current_attempt_id"]:
                current = conn.execute(
                    "SELECT state,lease_expires_at FROM fm_v2_attempts WHERE owner_id=? AND job_id=? AND attempt_id=?",
                    (owner, job_id, job["current_attempt_id"]),
                ).fetchone()
                if (
                    current
                    and current["state"] in {"leased", "running"}
                    and str(current["lease_expires_at"] or "") <= now_text
                ):
                    conn.execute(
                        "UPDATE fm_v2_attempts SET state='failed_retryable',updated_at=? WHERE owner_id=? AND attempt_id=?",
                        (now_text, owner, job["current_attempt_id"]),
                    )
                    conn.execute(
                        "UPDATE fm_v2_jobs SET state='retry_wait',updated_at=? WHERE owner_id=? AND job_id=?",
                        (now_text, owner, job_id),
                    )
                    job = conn.execute(
                        "SELECT * FROM fm_v2_jobs WHERE owner_id=? AND job_id=?",
                        (owner, job_id),
                    ).fetchone()
            if job["state"] not in {"queued", "retry_wait"}:
                conn.rollback()
                return None
            attempt_number = int(job["attempt_count"] or 0) + 1
            attempt_id = "attempt_" + uuid.uuid4().hex
            epoch_row = conn.execute(
                "SELECT COALESCE(MAX(lease_epoch),0) FROM fm_v2_attempts WHERE owner_id=? AND job_id=?",
                (owner, job_id),
            ).fetchone()
            lease_epoch = int(epoch_row[0] or 0) + 1
            conn.execute(
                "INSERT INTO fm_v2_attempts(owner_id,attempt_id,job_id,attempt_number,state,lease_epoch,lease_owner,lease_expires_at,heartbeat_at,progress_watermark,error_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner, attempt_id, job_id, attempt_number, "leased", lease_epoch, worker_id, expires_text, now_text, "", None, now_text, now_text),
            )
            conn.execute(
                "UPDATE fm_v2_jobs SET state='active',current_attempt_id=?,attempt_count=?,updated_at=? WHERE owner_id=? AND job_id=?",
                (attempt_id, attempt_number, now_text, owner, job_id),
            )
            conn.commit()
            return {"owner": owner, "job_id": job_id, "attempt_id": attempt_id, "lease_epoch": lease_epoch, "lease_expires_at": expires_text}
    except (OSError, sqlite3.Error, TypeError, ValueError):
        logger.exception("v2 job claim failed for %s", job_id)
        return None


def complete_job(
    *,
    owner: str,
    job_id: str,
    attempt_id: str,
    worker_id: str,
    lease_epoch: int,
    state: str = "succeeded",
    result: Optional[Mapping[str, Any]] = None,
    db_path: str = FM_DB_PATH,
) -> bool:
    """Complete only the currently fenced attempt; stale workers are rejected."""
    owner = str(owner or "").strip()
    job_id = str(job_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    worker_id = str(worker_id or "").strip()
    if not owner or not job_id or not attempt_id or not worker_id or state not in {"succeeded", "failed_terminal", "retry_wait", "cancelled"}:
        return False
    now = _now()
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            if not _job_tables_ready(conn):
                return False
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT a.lease_owner,a.lease_epoch,a.state,j.current_attempt_id,a.lease_expires_at FROM fm_v2_attempts a JOIN fm_v2_jobs j ON j.owner_id=a.owner_id AND j.job_id=a.job_id WHERE a.owner_id=? AND a.job_id=? AND a.attempt_id=?",
                (owner, job_id, attempt_id),
            ).fetchone()
            if not row or row[0] != worker_id or int(row[1]) != int(lease_epoch) or row[3] != attempt_id or row[2] not in {"leased", "running"} or str(row[4] or "") <= now:
                conn.rollback()
                return False
            attempt_state = "succeeded" if state == "succeeded" else ("failed_retryable" if state == "retry_wait" else state)
            conn.execute(
                "UPDATE fm_v2_attempts SET state=?,updated_at=? WHERE owner_id=? AND attempt_id=? AND lease_epoch=?",
                (attempt_state, now, owner, attempt_id, lease_epoch),
            )
            conn.execute(
                "UPDATE fm_v2_jobs SET state=?,result_json=?,updated_at=? WHERE owner_id=? AND job_id=? AND current_attempt_id=?",
                (state, json.dumps(result or {}, sort_keys=True), now, owner, job_id, attempt_id),
            )
            conn.commit()
            return True
    except (OSError, sqlite3.Error, TypeError, ValueError):
        logger.exception("v2 job completion failed for %s", job_id)
        return False


def seal_job_manifest(
    *,
    owner: str,
    job_id: str,
    phase: str,
    selected_ids: list[str],
    config_hash: str,
    model_hash: str,
    tool_hash: str,
    input_hash: str,
    completed_ids: Optional[list[str]] = None,
    reused_ids: Optional[list[str]] = None,
    failed_ids: Optional[list[str]] = None,
    waived_ids: Optional[list[str]] = None,
    parent_manifest_id: Optional[str] = None,
    db_path: str = FM_DB_PATH,
) -> dict[str, Any]:
    """Seal the two-stage derived-job denominator/outcome manifest.

    Only identifiers and hashes are persisted; prompts, source text, keys and
    tool payloads do not belong in this audit ledger.  A retry with the same
    manifest is idempotent, while a conflicting retry is rejected.
    """
    owner = _required_text(owner, "owner_id")
    job_id = _required_text(job_id, "job_id")
    phase = str(phase or "").strip().lower()
    if phase not in {"selection", "outcome"}:
        raise V2OperationError("validation", "manifest phase must be selection or outcome")

    def ids(values: Optional[list[str]], field: str) -> list[str]:
        result = _json_list(values or [], field)
        return sorted(result)

    selected = ids(selected_ids, "selected_ids")
    completed = ids(completed_ids, "completed_ids")
    reused = ids(reused_ids, "reused_ids")
    failed = ids(failed_ids, "failed_ids")
    waived = ids(waived_ids, "waived_ids")
    if len(set(selected)) != len(selected):
        raise V2OperationError("validation", "selected_ids must be unique")
    if phase == "selection" and any((completed, reused, failed, waived)):
        raise V2OperationError("validation", "selection manifests cannot include outcomes")
    if phase == "outcome":
        groups = [completed, reused, failed, waived]
        flat = [item for group in groups for item in group]
        if len(set(flat)) != len(flat) or set(flat) != set(selected):
            raise V2OperationError("validation", "outcome sets must be disjoint and cover selected_ids")
    hashes = {
        key: _required_text(value, key)
        for key, value in {
            "config_hash": config_hash,
            "model_hash": model_hash,
            "tool_hash": tool_hash,
            "input_hash": input_hash,
        }.items()
    }
    payload = {
        "owner_id": owner,
        "job_id": job_id,
        "phase": phase,
        "selected_ids": selected,
        "completed_ids": completed,
        "reused_ids": reused,
        "failed_ids": failed,
        "waived_ids": waived,
        **hashes,
        "parent_manifest_id": parent_manifest_id,
    }
    content_hash = _hash(payload)
    manifest_id = "manifest_" + content_hash[:32]
    now = _now()
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if not _tables_ready(conn):
                raise V2OperationError("unsupported_contract", "Frankenmemory v2 schema is unavailable")
            _require_manifest_schema(conn)
            # Sealing is a read-then-insert: a deferred transaction lets a
            # concurrent sealer pass the same existence check and hit the
            # UNIQUE(owner_id, job_id, phase) constraint instead of the
            # idempotent return.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM fm_v2_job_manifests WHERE owner_id=? AND job_id=? AND phase=?",
                (owner, job_id, phase),
            ).fetchone()
            if existing:
                if existing["content_hash"] != content_hash:
                    raise V2OperationError("manifest_conflict", "sealed job manifest differs from the existing seal")
                return dict(existing)
            conn.execute(
                "INSERT INTO fm_v2_job_manifests(owner_id,manifest_id,job_id,phase,selected_json,completed_json,reused_json,failed_json,waived_json,config_hash,model_hash,tool_hash,input_hash,parent_manifest_id,content_hash,sealed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner, manifest_id, job_id, phase, _json(selected), _json(completed), _json(reused), _json(failed), _json(waived), hashes["config_hash"], hashes["model_hash"], hashes["tool_hash"], hashes["input_hash"], parent_manifest_id, content_hash, now),
            )
            conn.commit()
            return {
                **payload,
                "manifest_id": manifest_id,
                "content_hash": content_hash,
                "sealed_at": now,
            }
    except V2OperationError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise V2OperationError("manifest_store", "could not persist derived-job manifest", retryable=True) from exc


def list_current_records(
    *,
    owner: str,
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: Optional[int] = 1000,
    require_legacy_presence: bool = False,
    identity: Optional[str] = None,
    db_path: str = FM_DB_PATH,
) -> list[dict[str, Any]]:
    """Read the current typed projection without crossing tenant scopes.

    This is the v2-native read adapter used by parity checks and future
    consumer cutover. Legacy provider records remain untouched; this function
    only reads active/open current revisions from the canonical projection.
    ``identity`` prefilters in SQL for a single memory id (block id or source
    event, with or without the ``memory:`` prefix) so keyed lookups do not
    scan the whole projection.
    """
    owner = str(owner or "").strip()
    workspace = str(workspace_id or "").strip()
    project = str(project_id or "").strip()
    identity_key = str(identity or "").strip()
    if not owner or (project and not workspace):
        return []
    if limit is not None:
        try:
            limit = max(1, min(int(limit), 5000))
        except (TypeError, ValueError):
            limit = 1000
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"fm_v2_knowledge_blocks", "fm_v2_knowledge_revisions"}.issubset(tables):
                return []
            _require_trust_schema(conn)
            scopes = [("", "")]
            if workspace:
                scopes.append((workspace, ""))
                if project:
                    scopes.append((workspace, project))
            predicates = " OR ".join("(b.workspace_key=? AND b.project_key=?)" for _ in scopes)
            legacy_clause = ""
            if require_legacy_presence:
                # Transitional cutover safety: until every legacy writer is
                # retired, a v2 row may be read as current only if its source
                # event still has a live curated record. This prevents stale
                # browser/test rows from becoming visible merely because they
                # survived in the shadow projection.
                if "curated" not in tables:
                    return []
                legacy_ref = "CASE WHEN r.source_event_id LIKE 'memory:%' THEN substr(r.source_event_id,8) ELSE r.source_event_id END"
                legacy_scope = "c.owner=b.owner_id AND COALESCE(c.workspace_id,'global')=CASE WHEN b.workspace_key='' THEN 'global' ELSE b.workspace_key END"
                legacy_clause = (
                    f" AND (EXISTS (SELECT 1 FROM curated c WHERE c.id={legacy_ref} AND {legacy_scope} AND COALESCE(c.archived,0)=0)"
                    # A resolved question archives its compatibility row by
                    # design (kind stays open_question, status goes active).
                    # Requiring a live row here would hide the answer through
                    # every read API, so the resolved arm only requires the
                    # legacy lineage row to exist, archived or not.
                    f" OR (r.kind='open_question' AND r.status='active' AND EXISTS (SELECT 1 FROM curated c WHERE c.id={legacy_ref} AND {legacy_scope})))"
                )
            params: list[Any] = [owner]
            for scope in scopes:
                params.extend(scope)
            identity_clause = ""
            if identity_key:
                identity_clause = (
                    " AND (b.block_id=? OR r.source_event_id=?"
                    " OR r.source_event_id='memory:'||?)"
                )
                params.extend([identity_key, identity_key, identity_key])
            params.append(-1 if limit is None else limit)
            rows = conn.execute(
                f"""
                WITH current_rows AS (
                    SELECT b.owner_id,b.block_id,b.workspace_key,b.project_key,
                           b.subject_entity_id,b.predicate,b.claim_slot,b.current_revision,
                           r.value_type,r.value_json,r.expected_value_type,r.kind,r.tags_json,
                           r.status,r.confidence,r.trust,r.activation,r.activation_rationale,
                           r.evidence_json,r.source_event_id,r.created_at,
                           (SELECT ta.state FROM fm_v2_trust_assignments ta
                             WHERE ta.owner_id=b.owner_id
                               AND ta.subject_kind='knowledge_revision'
                               AND ta.subject_id=b.block_id
                               AND ta.subject_revision=r.revision
                             ORDER BY ta.assignment_revision DESC LIMIT 1) AS trust_state,
                           (SELECT ta.trust FROM fm_v2_trust_assignments ta
                             WHERE ta.owner_id=b.owner_id
                               AND ta.subject_kind='knowledge_revision'
                               AND ta.subject_id=b.block_id
                               AND ta.subject_revision=r.revision
                             ORDER BY ta.assignment_revision DESC LIMIT 1) AS assigned_trust,
                           ROW_NUMBER() OVER (
                               PARTITION BY CASE
                                   WHEN r.source_event_id LIKE 'memory:%'
                                       THEN substr(r.source_event_id,8)
                                   WHEN COALESCE(r.source_event_id,'') <> ''
                                       THEN r.source_event_id
                                   ELSE b.block_id
                               END
                               ORDER BY r.created_at DESC,b.block_id
                           ) AS memory_rank
                      FROM fm_v2_knowledge_blocks b
                      JOIN fm_v2_knowledge_revisions r
                        ON r.owner_id=b.owner_id AND r.block_id=b.block_id
                       AND r.revision=b.current_revision
                     WHERE b.owner_id=? AND ({predicates})
                       AND r.status IN ('active','open')
                       {legacy_clause}
                       {identity_clause}
                )
                SELECT owner_id,block_id,workspace_key,project_key,
                       subject_entity_id,predicate,claim_slot,current_revision,
                       value_type,value_json,expected_value_type,kind,tags_json,
                       status,confidence,trust,activation,activation_rationale,
                       evidence_json,source_event_id,created_at,trust_state,assigned_trust
                  FROM current_rows
                 WHERE memory_rank=1
                 ORDER BY created_at DESC,block_id
                 LIMIT ?
                """,
                params,
            ).fetchall()
            records: list[dict[str, Any]] = []
            for row in rows:
                try:
                    typed = json.loads(row["value_json"] or "{}")
                except json.JSONDecodeError:
                    typed = {}
                try:
                    tags = json.loads(row["tags_json"] or "[]")
                except json.JSONDecodeError:
                    tags = []
                try:
                    evidence_ids = json.loads(row["evidence_json"] or "[]")
                except json.JSONDecodeError:
                    evidence_ids = []
                quote = ""
                if evidence_ids and "fm_v2_evidence" in tables:
                    placeholders = ",".join("?" for _ in evidence_ids)
                    evidence = conn.execute(
                        f"SELECT quote FROM fm_v2_evidence WHERE owner_id=? AND evidence_id IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
                        [owner, *[str(item) for item in evidence_ids]],
                    ).fetchone()
                    quote = str(evidence[0] or "") if evidence else ""
                typed_value = typed.get("value") if isinstance(typed, dict) else None
                if row["value_type"] == "string":
                    text_value = str(typed_value or "")
                elif isinstance(typed_value, dict) and typed_value.get("data"):
                    text_value = str(typed_value["data"])
                else:
                    text_value = quote
                memory_id = str(row["source_event_id"] or "")
                if memory_id.startswith("memory:"):
                    memory_id = memory_id.split(":", 1)[1]
                records.append({
                    "id": memory_id or row["block_id"],
                    "text": text_value,
                    "owner": row["owner_id"],
                    "workspace_id": row["workspace_key"] or None,
                    "project_id": row["project_key"] or None,
                    "kind": row["kind"],
                    "category": row["kind"],
                    "status": row["status"],
                    "metadata": {
                        "predicate": row["predicate"],
                        "claim_slot": row["claim_slot"],
                        "block_id": row["block_id"],
                        "revision": row["current_revision"],
                        "expected_value_type": row["expected_value_type"],
                        "activation": row["activation"],
                        "activation_rationale": row["activation_rationale"],
                        "typed_value": typed,
                    },
                    "tags": [str(tag) for tag in tags if isinstance(tag, (str, int))],
                    "confidence_score": row["confidence"],
                    "trust_score": row["trust"],
                    "trust": {
                        "state": row["trust_state"] or "unreviewed",
                        "value": row["assigned_trust"],
                        "subject_kind": "knowledge_revision",
                        "subject_id": row["block_id"],
                        "subject_revision": row["current_revision"],
                    },
                    "source_revision": max(
                        [int(item.rsplit("_", 1)[-1]) for item in evidence_ids if str(item).rsplit("_", 1)[-1].isdigit()] or [None],
                    ),
                    "source_message_ids": [str(row["source_event_id"])] if row["source_event_id"] else [],
                })
            # A compatibility-era mirror could have created one block per
            # category because the predicate was part of its deterministic
            # key.  A memory ID is the stable claim slot, so collapse those
            # historical duplicates here and keep the newest source event.
            unique: dict[str, dict[str, Any]] = {}
            for record in records:
                key = str(record.get("id") or record.get("metadata", {}).get("block_id") or "")
                if not key or key not in unique:
                    unique[key] = record
            return list(unique.values())
    except (OSError, sqlite3.Error, TypeError, ValueError):
        logger.exception("v2 current-record read failed for %s", owner)
        return []


def search_current_records(
    query: str,
    *,
    owner: str,
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    top_k: int = 5,
    require_legacy_presence: bool = False,
    db_path: str = FM_DB_PATH,
) -> list[dict[str, Any]]:
    """Search the current typed projection without leaving its tenant scope.

    This is the deterministic v2 fallback/read path while the Rust vector
    index remains a derived accelerator. It scores token coverage over the
    current value, predicate, kind, and tags; callers can still fall back to
    the legacy provider during the compatibility window.
    """
    tokens = [token for token in re.findall(r"[\w-]+", str(query or "").casefold()) if len(token) > 1]
    if not tokens:
        return []
    try:
        top_k = max(1, min(int(top_k), 100))
    except (TypeError, ValueError):
        top_k = 5
    rows = list_current_records(
        owner=owner,
        workspace_id=workspace_id,
        project_id=project_id,
        limit=None,
        require_legacy_presence=require_legacy_presence,
        db_path=db_path,
    )
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        haystack = " ".join(
            str(part or "")
            for part in (
                row.get("text"),
                row.get("kind"),
                row.get("category"),
                metadata.get("predicate"),
                " ".join(str(tag) for tag in row.get("tags") or []),
            )
        ).casefold()
        present = sum(1 for token in tokens if token in haystack)
        if not present:
            continue
        score = present / len(tokens)
        if " ".join(tokens) in haystack:
            score += 0.25
        scored.append((score, {**row, "recall_score": round(score, 6)}))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("id", ""))))
    return [row for _, row in scored[:top_k]]


def get_current_record(
    memory_id: str,
    *,
    owner: str,
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    require_legacy_presence: bool = False,
    db_path: str = FM_DB_PATH,
) -> Optional[dict[str, Any]]:
    """Return one exact current v2 record inside the authenticated scope."""
    memory_id = str(memory_id or "").strip()
    if not memory_id:
        return None
    for row in list_current_records(
        owner=owner,
        workspace_id=workspace_id,
        project_id=project_id,
        limit=5,
        require_legacy_presence=require_legacy_presence,
        identity=memory_id,
        db_path=db_path,
    ):
        if str(row.get("id") or "") == memory_id:
            return row
    return None


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _mirror_provenance(
    record: Any,
    metadata: Mapping[str, Any],
    *,
    owner: str,
    action: str,
) -> tuple[str, str, str, str, dict[str, Any]]:
    source = str(_record_value(record, "source", "") or "").strip().lower()
    declared_source_type = str(
        _record_value(record, "source_type", "") or ""
    ).strip().lower()
    reviewed = action == "review_candidate" or bool(metadata.get("manually_reviewed"))
    source_class = (
        "human" if source in {"user", "user_created"}
        else "ai" if source in {"ai_agent", "agent_explicit"}
        else "auto_extracted" if source in {"odysseus", "open_clank", "auto_extracted"}
        else None
    )
    # Candidate approval in the legacy engine rewrites source_type to human;
    # its retained source label is the only original-author signal. Outside
    # that known bridge conversion, an explicit valid source_type is primary.
    if reviewed:
        source_type = source_class or "auto_extracted"
    elif declared_source_type in {"human", "ai", "auto_extracted", "procedural"}:
        source_type = declared_source_type
    elif source_class:
        source_type = source_class
    else:
        source_type = "auto_extracted"

    if source_type == "human":
        actor_type, actor_id = "user", owner
    elif source_type == "ai":
        actor_type, actor_id = "ai", source if source_class == "ai" else "ai"
    elif source_type == "procedural":
        actor_type, actor_id = "system", source or "procedural"
    else:
        actor_type, actor_id = "automation", (
            source if source_class in {None, "auto_extracted"} and source
            else "auto_extracted"
        )

    activation = "review_approved" if reviewed else (
        "manual" if source_type == "human" else "candidate"
    )
    rationale = {
        "action": action,
        "source": source or None,
        "source_type": source_type,
        "declared_source_type": declared_source_type or None,
        "reviewed_by": owner if reviewed else None,
    }
    return source_type, actor_type, actor_id, activation, rationale


def mirror_record(
    record: Any,
    *,
    owner: str,
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    action: str = "upsert",
    db_path: str = FM_DB_PATH,
) -> bool:
    """Mirror one provider record into v2 and return whether it was written.

    ``record.id`` is the stable claim slot, so edits append a revision to the
    same block.  Open questions are represented as a normal knowledge block
    with ``kind=open_question``, ``status=open``, and a typed null value.  Its
    ``about`` / predicate metadata stays on the revision, so no prose parser
    or fake answer object is needed.
    """
    owner = str(owner or "").strip()
    if not owner:
        return False
    workspace = str(workspace_id or "").strip()
    project = str(project_id or "").strip()
    if project and not workspace:
        return False
    record_id = str(_record_value(record, "id", "")).strip()
    content = str(_record_value(record, "text", _record_value(record, "content", "")) or "")
    if not record_id or not content.strip():
        return False
    metadata = _record_value(record, "metadata", {})
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    category = str(_record_value(record, "category", _record_value(record, "kind", "fact")) or "fact").strip().lower()
    is_question = category in {"unknown", "question", "open_question"} or action == "resolve_question"
    kind = "open_question" if is_question else str(_record_value(record, "kind", category) or category)
    status = "open" if is_question and action not in {"delete", "resolve_question"} else ("retracted" if action in {"delete", "resolve_question"} else "active")
    about = metadata.get("about") or metadata.get("subject") or ("self" if category == "identity" else "memory")
    question_context = None
    if is_question and metadata.get("question_context") is not None:
        try:
            from services.memory.question_context import normalize_question_context
            question_context = normalize_question_context(
                metadata.get("question_context"),
                owner=owner,
                workspace_id=workspace,
                project_id=project,
            )
        except Exception:
            # Invalid associations remain compatibility-only; never let an
            # untrusted question claim a reserved identity or cross-scope ref.
            question_context = None
    if question_context:
        target = question_context.get("target") or {}
        if target.get("kind") == "entity":
            about = {"id": target.get("id"), "kind": "entity"}
        if question_context.get("predicate"):
            metadata["predicate"] = question_context["predicate"]
        if question_context.get("claim_slot"):
            metadata["claim_slot"] = question_context["claim_slot"]
    if isinstance(about, Mapping):
        about_label = str(about.get("label") or about.get("id") or "memory")
    else:
        about_label = str(about).strip() or "memory"
    predicate = str(metadata.get("predicate") or ("question" if is_question else category)).strip() or "fact"
    claim_slot = str(metadata.get("claim_slot") or record_id)
    subject_attribution = metadata.get("subject_attribution")
    reviewed_subject_role = ""
    if isinstance(subject_attribution, Mapping):
        import_review = metadata.get("import_review")
        if (
            subject_attribution.get("contract")
            == "openclank.memory-subject-attribution/v1"
            and subject_attribution.get("state") == "proposed"
            and subject_attribution.get("requires_review") is True
            and isinstance(import_review, Mapping)
            and str(import_review.get("reviewed_by") or "").strip() == owner
        ):
            reviewed_subject_role = str(
                subject_attribution.get("role") or ""
            ).strip()
    tags = metadata.get("tags", _record_value(record, "tags", []))
    tags = sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()})
    if (
        category == "identity"
        and reviewed_subject_role != "handler"
        and "self" not in tags
    ):
        tags.append("self")
    entity_id = "entity_" + _hash([owner, workspace, project, about_label])[:32]
    target_entity_bound = False
    if question_context and (question_context.get("target") or {}).get("kind") == "entity":
        requested_entity_id = str((question_context.get("target") or {}).get("id") or "").strip()
        existing = None
        try:
            with sqlite3.connect(db_path, timeout=30) as probe:
                existing = probe.execute(
                    "SELECT 1 FROM fm_v2_entities WHERE owner_id=? AND entity_id=? AND workspace_key=? AND project_key=?",
                    (owner, requested_entity_id, workspace, project),
                ).fetchone()
        except sqlite3.Error:
            existing = None
        if existing:
            entity_id = requested_entity_id
            target_entity_bound = True
    elif reviewed_subject_role in {"handler", "assistant_self"}:
        requested_entity_id = str(
            (subject_attribution or {}).get("entity_id") or ""
        ).strip()
        role_clause = (
            "r.role='handler'"
            if reviewed_subject_role == "handler"
            else "r.role LIKE 'assistant_self:%'"
        )
        existing = None
        try:
            with sqlite3.connect(db_path, timeout=30) as probe:
                existing = probe.execute(
                    "SELECT 1 FROM fm_v2_entities e "
                    "JOIN fm_v2_entity_roles r "
                    "ON r.owner_id=e.owner_id AND r.entity_id=e.entity_id "
                    "AND r.revision=e.current_revision AND r.is_current=1 "
                    "WHERE e.owner_id=? AND e.entity_id=? "
                    "AND e.workspace_key=? AND e.project_key=? AND " + role_clause,
                    (owner, requested_entity_id, workspace, project),
                ).fetchone()
        except sqlite3.Error:
            existing = None
        if existing:
            entity_id = requested_entity_id
            about = {"id": requested_entity_id, "kind": "entity"}
            about_label = requested_entity_id
            target_entity_bound = True
            if reviewed_subject_role == "handler" and category == "identity":
                predicate = "preferred_name"
                claim_slot = "identity.preferred_name"
    # claim_slot is the stable identity of the assertion.  Predicate/category
    # can be corrected later; including it here would strand the prior block
    # and make an edit look like a second memory.
    block_id = "block_" + _hash([owner, workspace, project, entity_id, claim_slot])[:32]
    source_id = "source_memory_" + _hash([owner, record_id])[:32]
    source_event_id = f"memory:{record_id}"
    now = _now()
    # An open question has no value yet.  Its text is evidence; about and
    # predicate identify the answer slot without encoding an unanswered
    # question as a truthy object.
    about_payload = (
        dict(about) if isinstance(about, Mapping)
        else {"label": about_label}
    )
    if is_question:
        value = {"type": "null"}
        expected_value_type = str(metadata.get("expected_value_type") or "string")
    else:
        value = {"type": "string", "value": content}
        expected_value_type = "string"
    source_type, actor_type, actor_id, activation, activation_provenance = (
        _mirror_provenance(record, metadata, owner=owner, action=action)
    )
    entity_payload = {
        "entity_type": "person" if category == "identity" else "concept",
        "canonical_label": about_label,
        "aliases": [],
        "roles": ["self"] if "self" in tags else [],
        "tags": tags,
    }
    revision_payload = {
        "value": value,
        "kind": kind,
        "category": category,
        "predicate": predicate,
        "claim_slot": claim_slot,
        "status": status,
        "tags": tags,
        "about": about_payload,
        "record_id": record_id,
        "provenance": activation_provenance,
        "question_context": question_context,
    }
    revision_hash = _hash(revision_payload)
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if not _tables_ready(conn):
                return False
            # Revision assignment and publication must share one reserved
            # writer transaction. A deferred transaction lets concurrent
            # mirrors read the same head before either owns the write lock.
            conn.execute("BEGIN IMMEDIATE")
            # A retry with the same source event and payload is a no-op.  A
            # changed payload still appends a new revision to the same block.
            previous_hash = conn.execute(
                "SELECT content_hash FROM fm_v2_knowledge_revisions WHERE owner_id=? AND block_id=? ORDER BY revision DESC LIMIT 1",
                (owner, block_id),
            ).fetchone()
            if previous_hash and previous_hash[0] == revision_hash:
                return True
            block = conn.execute(
                "SELECT current_revision FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?",
                (owner, block_id),
            ).fetchone()
            revision = int(block[0]) + 1 if block else 1
            change_set = "cs_v2_" + _hash(
                [owner, block_id, revision, revision_hash, action]
            )[:32]
            source_row = conn.execute(
                "SELECT source_revision,content_hash,source_type FROM fm_v2_sources WHERE owner_id=? AND source_id=? ORDER BY source_revision DESC LIMIT 1",
                (owner, source_id),
            ).fetchone()
            source_revision = 1
            if source_row:
                source_revision = int(source_row[0])
                if source_row[1] != _hash(content) or source_row[2] != source_type:
                    source_revision += 1
            conn.execute(
                "INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (owner, change_set, actor_type, actor_id, action, source_event_id, CONTRACT_VERSION, _hash(revision_payload), now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO fm_v2_sources(owner_id,source_id,workspace_key,project_key,source_uri,source_revision,content_hash,source_type,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (owner, source_id, workspace, project, f"memory://{record_id}", source_revision, _hash(content), source_type, now),
            )
            evidence_id = f"evidence_{record_id}_{source_revision}"
            conn.execute(
                "INSERT OR IGNORE INTO fm_v2_evidence(owner_id,evidence_id,source_id,source_revision,locator_type,locator_json,quote,extraction_contract,parser_version,raw_value_json,normalized_value_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner, evidence_id, source_id, source_revision, "memory_record", json.dumps({"record_id": record_id, "source_revision": source_revision}, sort_keys=True), content, "provider-record-v1", "open-clank", json.dumps(content), json.dumps(value, sort_keys=True), now),
            )
            entity = conn.execute(
                "SELECT current_revision FROM fm_v2_entities WHERE owner_id=? AND entity_id=?",
                (owner, entity_id),
            ).fetchone()
            if target_entity_bound and not entity:
                raise ValueError("question target entity disappeared from scope")
            if target_entity_bound and entity:
                # A question may point at a reserved Handler/self entity, but
                # the question bridge must never rewrite that entity's label,
                # aliases, roles, or history with untrusted question prose.
                entity_revision = int(entity[0])
            else:
                entity_revision = int(entity[0]) + 1 if entity else 1
                conn.execute(
                    "INSERT OR IGNORE INTO fm_v2_entities(owner_id,entity_id,workspace_key,project_key,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,?,?)",
                    (owner, entity_id, workspace, project, change_set, entity_revision, now),
                )
                conn.execute(
                    "INSERT INTO fm_v2_entity_revisions(owner_id,entity_id,revision,payload,content_hash,actor_type,actor_id,reason,change_set_id,previous_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (owner, entity_id, entity_revision, json.dumps(entity_payload, sort_keys=True), _hash(entity_payload), actor_type, actor_id, action, change_set, entity_revision - 1 or None, now),
                )
                conn.execute(
                    "UPDATE fm_v2_entities SET current_revision=? WHERE owner_id=? AND entity_id=?",
                    (entity_revision, owner, entity_id),
                )
            conn.execute(
                "INSERT OR IGNORE INTO fm_v2_knowledge_blocks(owner_id,block_id,workspace_key,project_key,subject_entity_id,predicate,claim_slot,cardinality,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (owner, block_id, workspace, project, entity_id, predicate, claim_slot, "single", change_set, revision, now),
            )
            conn.execute(
                "INSERT INTO fm_v2_knowledge_revisions(owner_id,block_id,revision,value_type,value_json,expected_value_type,kind,tags_json,status,confidence,trust,activation,activation_rationale,evidence_json,content_hash,actor_type,actor_id,reason,source_event_id,change_set_id,previous_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner, block_id, revision, "null" if is_question else "string", json.dumps(value, sort_keys=True), expected_value_type, kind, json.dumps(tags), status, _clamp(_record_value(record, "confidence_score"), 0.5), _clamp(_record_value(record, "trust_score"), 0.5), activation, json.dumps({"reason": "provider bridge", "about": about_payload, "question_context": question_context, "provenance": activation_provenance}, sort_keys=True), json.dumps([evidence_id]), revision_hash, actor_type, actor_id, action, source_event_id, change_set, revision - 1 or None, now),
            )
            conn.execute(
                "INSERT INTO fm_v2_revision_evidence(owner_id,block_id,revision,evidence_id) VALUES (?,?,?,?)",
                (owner, block_id, revision, evidence_id),
            )
            conn.execute(
                "UPDATE fm_v2_knowledge_blocks SET predicate=?,current_revision=? WHERE owner_id=? AND block_id=?",
                (predicate, revision, owner, block_id),
            )
            conn.execute(
                "INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES (?,?,?,?,?,?,?)",
                (owner, f"outbox_{change_set}", change_set, "knowledge_revision", json.dumps({"block_id": block_id, "revision": revision}, sort_keys=True), source_revision, now),
            )
        return True
    except (OSError, sqlite3.Error, TypeError, ValueError):
        logger.exception("v2 memory mirror failed for %s", record_id)
        return False


def mirror_capture_job(
    *,
    owner: str,
    session_id: Optional[str],
    user_text: str,
    assistant_text: str,
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    db_path: str = FM_DB_PATH,
) -> bool:
    """Persist automatic capture as a durable, idempotent v2 job envelope."""
    owner = str(owner or "").strip()
    if not owner:
        return False
    workspace = str(workspace_id or "").strip()
    project = str(project_id or "").strip()
    if project and not workspace:
        return False
    key = "capture:" + _hash([owner, session_id or "", user_text or "", assistant_text or ""])
    now = _now()
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            if not _tables_ready(conn):
                return False
            conn.execute(
                "INSERT OR IGNORE INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,attempt_count,input_hash,result_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner, "job_" + _hash(key)[:32], "conversation_capture", workspace, project, key, "succeeded", 1, _hash([user_text or "", assistant_text or ""]), json.dumps({"session_id": session_id, "captured": True}, sort_keys=True), now, now),
            )
        return True
    except (OSError, sqlite3.Error, TypeError, ValueError):
        logger.exception("v2 capture job mirror failed")
        return False


def mirror_import_job(
    *,
    owner: str,
    filename: str,
    content: str,
    session_id: Optional[str] = None,
    result: Optional[list[dict[str, Any]]] = None,
    state: str = "succeeded",
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    db_path: str = FM_DB_PATH,
) -> bool:
    """Record a file-import attempt with a stable content/idempotency key."""
    owner = str(owner or "").strip()
    if not owner:
        return False
    workspace = str(workspace_id or "").strip()
    project = str(project_id or "").strip()
    if project and not workspace:
        return False
    key = "import:" + _hash([owner, filename, content])
    now = _now()
    if state not in {"succeeded", "failed_terminal", "cancelled"}:
        state = "failed_terminal"
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            if not _tables_ready(conn):
                return False
            _require_manifest_schema(conn)
            payload = json.dumps({"filename": filename, "session_id": session_id, "suggestion_count": len(result or []), "suggestions": result or []}, sort_keys=True)
            job_id = "job_" + _hash(key)[:32]
            existing = conn.execute(
                "SELECT state FROM fm_v2_jobs WHERE owner_id=? AND idempotency_key=?",
                (owner, key),
            ).fetchone()
            if existing is None:
                # The state trigger deliberately requires lifecycle-respecting
                # transitions, so a terminal import is queued then activated
                # before it is sealed as succeeded/failed/cancelled.
                conn.execute(
                    "INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,attempt_count,input_hash,result_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (owner, job_id, "memory_file_import", workspace, project, key, "queued", 0, _hash(content), payload, now, now),
                )
                conn.execute(
                    "UPDATE fm_v2_jobs SET state='active',attempt_count=1,updated_at=? WHERE owner_id=? AND job_id=?",
                    (now, owner, job_id),
                )
                conn.execute(
                    "UPDATE fm_v2_jobs SET state=?,result_json=?,updated_at=? WHERE owner_id=? AND job_id=?",
                    (state, payload, now, owner, job_id),
                )
            else:
                current = str(existing[0] or "")
                # A retry must never regress a vouched terminal outcome.  Keep
                # the original state while refreshing only diagnostic output.
                if current in {"succeeded", "failed_terminal", "cancelled"}:
                    conn.execute(
                        "UPDATE fm_v2_jobs SET result_json=?,updated_at=? WHERE owner_id=? AND idempotency_key=?",
                        (payload, now, owner, key),
                    )
                else:
                    if current == "retry_wait":
                        conn.execute(
                            "UPDATE fm_v2_jobs SET state='queued',updated_at=? WHERE owner_id=? AND idempotency_key=?",
                            (now, owner, key),
                        )
                        current = "queued"
                    if current == "queued":
                        conn.execute(
                            "UPDATE fm_v2_jobs SET state='active',attempt_count=attempt_count+1,updated_at=? WHERE owner_id=? AND idempotency_key=?",
                            (now, owner, key),
                        )
                    conn.execute(
                        "UPDATE fm_v2_jobs SET state=?,result_json=?,updated_at=? WHERE owner_id=? AND idempotency_key=?",
                        (state, payload, now, owner, key),
                    )
            conn.commit()
        return True
    except (OSError, sqlite3.Error, TypeError, ValueError):
        logger.exception("v2 import job mirror failed")
        return False


def conversation_source_workspaces(*, owner: str, session_id: str, db_path: str) -> list[str]:
    """Discover actual exact-chat capture bindings at the pinned authority."""
    if not owner or not session_id:
        raise V2OperationError("invalid_source", "owner and exact conversation id required")
    with sqlite3.connect("file:" + __import__("urllib.parse", fromlist=["quote"]).quote(str(db_path), safe="/") + "?mode=ro", uri=True) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"curated", "raw", "candidates", "fm_v2_jobs"}.issubset(tables):
            raise V2OperationError("source_detach_unavailable", "source authority schema is unavailable")
        workspaces = {str(row[0] or "") for table in ("curated", "raw", "candidates") for row in conn.execute(f"SELECT DISTINCT workspace_id FROM {table} WHERE owner=? AND session_id=?", (owner, session_id))}
        # Completed durable receipts preserve binding discovery on a retry
        # after navigation links were already detached in another workspace.
        for workspace, encoded in conn.execute("SELECT workspace_key,result_json FROM fm_v2_jobs WHERE owner_id=? AND kind IN ('conversation_capture','conversation_source_removed') AND state='succeeded'", (owner,)):
            result = json.loads(encoded or "{}")
            if isinstance(result, dict) and result.get("session_id") == session_id:
                workspaces.add(str(workspace or ""))
    return sorted(workspaces)


def mark_conversation_source_removed(*, owner: str, session_id: str,
                                     workspace_id: str, db_path: str) -> dict[str, Any]:
    """Atomically detach exact chat navigation links; retain admitted content.

    Memory event/evidence IDs remain valid independent lineage. A durable job
    receipt makes a retry return the original committed counts.
    """
    if not str(owner or "").strip() or not str(session_id or "").strip():
        raise V2OperationError("invalid_source", "owner and exact conversation id required")
    key = "conversation-source-removed:" + _hash([owner, workspace_id, session_id])
    now = _now()
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"curated", "raw", "candidates", "fm_v2_jobs"}.issubset(tables):
            raise V2OperationError("source_detach_unavailable", "source authority schema is unavailable")
        previous = conn.execute("SELECT result_json FROM fm_v2_jobs WHERE owner_id=? AND idempotency_key=? AND state='succeeded'", (owner, key)).fetchone()
        if previous:
            return {**json.loads(previous[0]), "already_applied": True}
        counts = {}
        for table in ("curated", "raw"):
            rows = conn.execute(f"SELECT id,metadata FROM {table} WHERE owner=? AND session_id=? AND workspace_id=?", (owner, session_id, workspace_id)).fetchall()
            for record_id, encoded in rows:
                metadata = json.loads(encoded or "null")
                if not isinstance(metadata, dict):
                    metadata = {"previous_metadata": metadata} if metadata is not None else {}
                metadata["conversation_source_removed"] = {"session_id": session_id, "removed_at": now}
                conn.execute(f"UPDATE {table} SET session_id='',session_key='',metadata=? WHERE owner=? AND id=?", (json.dumps(metadata, sort_keys=True), owner, record_id))
            counts[table] = len(rows)
        counts["candidates"] = conn.execute(
            "UPDATE candidates SET session_id='',reason=reason||? WHERE owner=? AND session_id=? AND workspace_id=?",
            (" [conversation_source_removed:" + session_id + "]", owner, session_id, workspace_id),
        ).rowcount
        receipt = {"complete": True, "owner": owner, "session_id": session_id,
                   "workspace_id": workspace_id, "detached": counts,
                   "independent_content_retained": True, "already_applied": False}
        job_id = "job_" + _hash(key)[:32]
        conn.execute("INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,attempt_count,input_hash,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (owner, job_id, "conversation_source_removed", workspace_id, "", key, "queued", 0, _hash([owner, session_id]), now, now))
        conn.execute("UPDATE fm_v2_jobs SET state='active',attempt_count=1 WHERE owner_id=? AND job_id=?", (owner, job_id))
        conn.execute("UPDATE fm_v2_jobs SET state='succeeded',result_json=? WHERE owner_id=? AND job_id=?", (json.dumps(receipt, sort_keys=True), owner, job_id))
        return receipt

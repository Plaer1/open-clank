"""Application-facing history and lifecycle over canonical Frankenmemory v2.

Published knowledge lives in the append-only ``fm_v2_knowledge_*`` tables.
Unreviewed material does not: Rust ``candidates`` is the one proposal queue and
is reached through :class:`FrankenmemoryProvider`.  Keeping that boundary here
prevents a second, subtly different candidate system from growing in Python.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Iterable, Mapping, Optional

from src.constants import FM_DB_PATH
from src.frankenmemory_v2 import (
    RevisionConflict,
    V2OperationError,
    V2Repository,
    _scope_is_visible,
)


class VersionedMemoryError(RuntimeError):
    """A typed knowledge request is invalid in its current lifecycle state."""


class VersionedMemoryNotFound(VersionedMemoryError):
    pass


class VersionedMemoryConflict(VersionedMemoryError):
    """Optimistic revision check failed; ``current`` is safe to show in UI."""

    def __init__(self, expected_revision: int, current: Mapping[str, Any]):
        self.expected_revision = int(expected_revision)
        self.current = dict(current)
        current_revision = self.current.get("revision", self.current.get("latest_revision"))
        super().__init__(
            f"revision {self.expected_revision} is stale; current revision is {current_revision}"
        )


@dataclass(frozen=True)
class KnowledgeScope:
    owner: str
    workspace_id: str = "global"
    project_id: str = ""

    def __post_init__(self) -> None:
        if not str(self.owner).strip():
            raise VersionedMemoryError("owner is required")
        if not str(self.workspace_id).strip():
            raise VersionedMemoryError("workspace_id is required")
        if self.project_id and not self.workspace_id:
            raise VersionedMemoryError("project scope requires workspace scope")

    @property
    def workspace_key(self) -> str:
        return str(self.workspace_id).strip()

    @property
    def project_key(self) -> str:
        return str(self.project_id).strip()


def _loads(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _typed(value: Any, value_type: Optional[str] = None) -> dict[str, Any]:
    if isinstance(value, Mapping) and set(value) >= {"type"}:
        return dict(value)
    inferred = value_type or (
        "object"
        if isinstance(value, Mapping)
        else "list"
        if isinstance(value, list)
        else "boolean"
        if isinstance(value, bool)
        else "number"
        if isinstance(value, (int, float))
        else "null"
        if value is None
        else "string"
    )
    return {"type": "null"} if inferred == "null" else {"type": inferred, "value": value}


def _text_value(value: Mapping[str, Any], fallback: str = "") -> str:
    payload = value.get("value")
    if isinstance(payload, str):
        return payload
    if payload is None:
        return fallback
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if isinstance(data, str):
            return data
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _scope(scope: KnowledgeScope) -> tuple[str, str, str]:
    return scope.owner.strip(), scope.workspace_key, scope.project_key


def _resolve_block_id(
    conn: sqlite3.Connection,
    scope: KnowledgeScope,
    identifier: str,
) -> str:
    owner, workspace, project = _scope(scope)
    identifier = str(identifier or "").strip()
    if not identifier:
        raise VersionedMemoryNotFound("knowledge identifier is required")
    row = conn.execute(
        """
        SELECT block_id FROM fm_v2_knowledge_blocks
         WHERE owner_id=? AND workspace_key=? AND project_key=? AND block_id=?
        """,
        (owner, workspace, project, identifier),
    ).fetchone()
    if row:
        return str(row[0])
    row = conn.execute(
        """
        SELECT b.block_id
          FROM fm_v2_knowledge_blocks b
          JOIN fm_v2_knowledge_revisions r
            ON r.owner_id=b.owner_id AND r.block_id=b.block_id
         WHERE b.owner_id=? AND b.workspace_key=? AND b.project_key=?
           AND r.source_event_id IN (?,?)
         ORDER BY r.revision DESC LIMIT 1
        """,
        (owner, workspace, project, identifier, f"memory:{identifier}"),
    ).fetchone()
    if not row:
        raise VersionedMemoryNotFound("knowledge was not found in this scope")
    return str(row[0])


def _memory_id(history: list[dict[str, Any]], block_id: str) -> str:
    for revision in reversed(history):
        source_event = str(revision.get("source_event_id") or "")
        if source_event.startswith("memory:"):
            return source_event.split(":", 1)[1]
    return block_id


def _detail(
    repository: V2Repository,
    scope: KnowledgeScope,
    block_id: str,
) -> dict[str, Any]:
    owner, workspace, project = _scope(scope)
    try:
        current = repository.get_knowledge(
            owner=owner,
            workspace_id=workspace,
            project_id=project or None,
            block_id=block_id,
        )
        history_ascending = repository.knowledge_history(
            owner=owner,
            workspace_id=workspace,
            project_id=project or None,
            block_id=block_id,
        )
    except V2OperationError as exc:
        if exc.code == "not_found":
            raise VersionedMemoryNotFound("knowledge was not found in this scope") from exc
        raise VersionedMemoryError(str(exc)) from exc

    evidence_ids = sorted(
        {
            str(evidence_id)
            for revision in history_ascending
            for evidence_id in revision.get("evidence_ids") or []
        }
    )
    evidence: list[dict[str, Any]] = []
    entity: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []
    with sqlite3.connect(repository.db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        if evidence_ids:
            marks = ",".join("?" for _ in evidence_ids)
            rows = conn.execute(
                f"""
                SELECT e.*,s.source_uri,s.source_type,s.content_hash AS source_content_hash,
                       s.forget_state
                  FROM fm_v2_evidence e
                  JOIN fm_v2_sources s
                    ON s.owner_id=e.owner_id AND s.source_id=e.source_id
                   AND s.source_revision=e.source_revision
                 WHERE e.owner_id=? AND e.evidence_id IN ({marks})
                 ORDER BY e.created_at DESC
                """,
                [owner, *evidence_ids],
            ).fetchall()
            evidence = [
                {
                    "id": row["evidence_id"],
                    "source_id": row["source_id"],
                    "source_revision": row["source_revision"],
                    "source_uri": row["source_uri"],
                    "source_type": row["source_type"],
                    "source_content_hash": row["source_content_hash"],
                    "forget_state": row["forget_state"],
                    "locator_type": row["locator_type"],
                    "locator": _loads(row["locator_json"], {}),
                    "quote": row["quote"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        try:
            entity = repository.get_entity(
                owner=owner,
                workspace_id=workspace,
                project_id=project or None,
                entity_id=current["subject_entity_id"],
            )
        except V2OperationError:
            entity = {}
        if "fm_v2_conflicts" in {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }:
            conflicts = [
                {
                    **dict(row),
                    "competing_payload": _loads(row["competing_payload"], {}),
                }
                for row in conn.execute(
                    "SELECT * FROM fm_v2_conflicts WHERE owner_id=? AND block_id=? ORDER BY created_at DESC",
                    (owner, block_id),
                ).fetchall()
            ]

    fallback = evidence[0].get("quote", "") if evidence else ""
    history = [
        {**revision, "text": _text_value(revision["value"], fallback)}
        for revision in reversed(history_ascending)
    ]
    current_with_text = {**current, "text": _text_value(current["value"], fallback)}
    return {
        "id": _memory_id(history_ascending, block_id),
        "block_id": block_id,
        "owner": owner,
        "workspace_id": workspace,
        "project_id": project or None,
        "subject_entity_id": current["subject_entity_id"],
        "entity": entity,
        "predicate": current["predicate"],
        "claim_slot": current["claim_slot"],
        "current_revision": int(current["revision"]),
        "latest_revision": int(current["revision"]),
        "current": current_with_text,
        "latest": current_with_text,
        "history": history,
        "evidence": evidence,
        "conflicts": conflicts,
        "explanation": {
            "stable_identity": current["claim_slot"],
            "published_revision": int(current["revision"]),
            "activation": current["activation"],
            "activation_rationale": current["activation_rationale"],
            "evidence_count": len(evidence),
        },
    }


def _details(
    repository: V2Repository,
    scope: KnowledgeScope,
    block_ids: list[str],
) -> list[dict[str, Any]]:
    """Batch counterpart of ``_detail``: one connection, set-based queries.

    ``_detail`` pays three-plus connections per block (current revision,
    history, evidence/conflicts, entity), so listing hundreds of blocks made
    ``list_knowledge``/``canonical_graph`` O(n) on connection setup. The
    assembled dicts are identical: same view builders, same per-block
    ordering, same evidence/conflict/entity fallbacks.
    """
    owner, workspace, project = _scope(scope)
    if not block_ids:
        return []
    marks = ",".join("?" for _ in block_ids)
    joined_columns = (
        "b.owner_id,b.block_id,b.workspace_key,b.project_key,"
        "b.subject_entity_id,b.predicate,b.claim_slot,b.cardinality,r.*"
    )
    with sqlite3.connect(repository.db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        current_by_block = {
            str(row["block_id"]): row
            for row in conn.execute(
                f"SELECT {joined_columns} FROM fm_v2_knowledge_blocks b"
                f" JOIN fm_v2_knowledge_revisions r"
                f"  ON r.owner_id=b.owner_id AND r.block_id=b.block_id"
                f" AND r.revision=b.current_revision"
                f" WHERE b.owner_id=? AND b.block_id IN ({marks})",
                [owner, *block_ids],
            ).fetchall()
        }
        if any(block_id not in current_by_block for block_id in block_ids):
            raise VersionedMemoryNotFound("knowledge was not found in this scope")
        history_by_block: dict[str, list[dict[str, Any]]] = {
            block_id: [] for block_id in block_ids
        }
        for row in conn.execute(
            f"SELECT {joined_columns} FROM fm_v2_knowledge_blocks b"
            f" JOIN fm_v2_knowledge_revisions r"
            f"  ON r.owner_id=b.owner_id AND r.block_id=b.block_id"
            f" WHERE b.owner_id=? AND b.block_id IN ({marks})"
            f" ORDER BY r.revision",
            [owner, *block_ids],
        ).fetchall():
            history_by_block[str(row["block_id"])].append(
                V2Repository._knowledge_view(row)
            )
        evidence_ids = sorted(
            {
                str(evidence_id)
                for history in history_by_block.values()
                for revision in history
                for evidence_id in revision.get("evidence_ids") or []
            }
        )
        evidence_ordered: list[dict[str, Any]] = []
        if evidence_ids:
            evidence_marks = ",".join("?" for _ in evidence_ids)
            evidence_ordered = [
                {
                    "id": row["evidence_id"],
                    "source_id": row["source_id"],
                    "source_revision": row["source_revision"],
                    "source_uri": row["source_uri"],
                    "source_type": row["source_type"],
                    "source_content_hash": row["source_content_hash"],
                    "forget_state": row["forget_state"],
                    "locator_type": row["locator_type"],
                    "locator": _loads(row["locator_json"], {}),
                    "quote": row["quote"],
                    "created_at": row["created_at"],
                }
                for row in conn.execute(
                    f"SELECT e.*,s.source_uri,s.source_type,"
                    f"s.content_hash AS source_content_hash,s.forget_state"
                    f"  FROM fm_v2_evidence e"
                    f" JOIN fm_v2_sources s"
                    f"   ON s.owner_id=e.owner_id AND s.source_id=e.source_id"
                    f"  AND s.source_revision=e.source_revision"
                    f" WHERE e.owner_id=? AND e.evidence_id IN ({evidence_marks})"
                    f" ORDER BY e.created_at DESC",
                    [owner, *evidence_ids],
                ).fetchall()
            ]
        entity_ids = sorted(
            {
                str(V2Repository._knowledge_view(row)["subject_entity_id"])
                for row in current_by_block.values()
            }
        )
        entity_views: dict[str, dict[str, Any]] = {}
        if entity_ids:
            entity_marks = ",".join("?" for _ in entity_ids)
            for row in conn.execute(
                f"SELECT e.owner_id,e.entity_id,e.workspace_key,e.project_key,r.*"
                f"  FROM fm_v2_entities e"
                f" JOIN fm_v2_entity_revisions r"
                f"   ON r.owner_id=e.owner_id AND r.entity_id=e.entity_id"
                f"  AND r.revision=e.current_revision"
                f" WHERE e.owner_id=? AND e.entity_id IN ({entity_marks})",
                [owner, *entity_ids],
            ).fetchall():
                if _scope_is_visible(
                    row["workspace_key"], row["project_key"], workspace, project
                ):
                    entity_views[str(row["entity_id"])] = V2Repository._entity_view(row)
        conflicts_by_block: dict[str, list[dict[str, Any]]] = {
            block_id: [] for block_id in block_ids
        }
        if "fm_v2_conflicts" in {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }:
            for row in conn.execute(
                f"SELECT * FROM fm_v2_conflicts WHERE owner_id=? AND block_id IN ({marks})"
                f" ORDER BY created_at DESC",
                [owner, *block_ids],
            ).fetchall():
                conflicts_by_block[str(row["block_id"])].append(
                    {**dict(row), "competing_payload": _loads(row["competing_payload"], {})}
                )

    details = []
    for block_id in block_ids:
        current = V2Repository._knowledge_view(current_by_block[block_id])
        history_ascending = history_by_block[block_id]
        block_evidence_ids = {
            str(evidence_id)
            for revision in history_ascending
            for evidence_id in revision.get("evidence_ids") or []
        }
        evidence = [
            dict(item) for item in evidence_ordered if item["id"] in block_evidence_ids
        ]
        entity = entity_views.get(str(current["subject_entity_id"]), {})
        conflicts = conflicts_by_block[block_id]
        fallback = evidence[0].get("quote", "") if evidence else ""
        history = [
            {**revision, "text": _text_value(revision["value"], fallback)}
            for revision in reversed(history_ascending)
        ]
        current_with_text = {
            **current,
            "text": _text_value(current["value"], fallback),
        }
        details.append(
            {
                "id": _memory_id(history_ascending, block_id),
                "block_id": block_id,
                "owner": owner,
                "workspace_id": workspace,
                "project_id": project or None,
                "subject_entity_id": current["subject_entity_id"],
                "entity": entity,
                "predicate": current["predicate"],
                "claim_slot": current["claim_slot"],
                "current_revision": int(current["revision"]),
                "latest_revision": int(current["revision"]),
                "current": current_with_text,
                "latest": current_with_text,
                "history": history,
                "evidence": evidence,
                "conflicts": conflicts,
                "explanation": {
                    "stable_identity": current["claim_slot"],
                    "published_revision": int(current["revision"]),
                    "activation": current["activation"],
                    "activation_rationale": current["activation_rationale"],
                    "evidence_count": len(evidence),
                },
            }
        )
    return details


def get_knowledge(
    identifier: str,
    *,
    owner: str,
    workspace_id: str = "global",
    project_id: str = "",
    db_path: str = FM_DB_PATH,
) -> dict[str, Any]:
    scope = KnowledgeScope(owner, workspace_id, project_id)
    with sqlite3.connect(db_path, timeout=30) as conn:
        block_id = _resolve_block_id(conn, scope, identifier)
    return _detail(V2Repository(db_path), scope, block_id)


def list_knowledge(
    *,
    owner: str,
    workspace_id: str = "global",
    project_id: str = "",
    statuses: Optional[Iterable[str]] = None,
    kinds: Optional[Iterable[str]] = None,
    limit: int = 500,
    db_path: str = FM_DB_PATH,
) -> list[dict[str, Any]]:
    scope = KnowledgeScope(owner, workspace_id, project_id)
    wanted_status = {str(value) for value in statuses or [] if str(value)}
    wanted_kinds = {str(value) for value in kinds or [] if str(value)}
    limit = max(1, min(int(limit), 5000))
    owner_key, workspace, project = _scope(scope)
    with sqlite3.connect(db_path, timeout=30) as conn:
        rows = conn.execute(
            """
            SELECT block_id FROM fm_v2_knowledge_blocks
             WHERE owner_id=? AND workspace_key=? AND project_key=?
             ORDER BY created_at DESC LIMIT ?
            """,
            (owner_key, workspace, project, limit),
        ).fetchall()
    repository = V2Repository(db_path)
    details = _details(repository, scope, [str(row[0]) for row in rows])
    if wanted_status:
        details = [item for item in details if item["current"]["status"] in wanted_status]
    if wanted_kinds:
        details = [item for item in details if item["current"]["kind"] in wanted_kinds]
    return details


def propose_knowledge(**_: Any) -> dict[str, Any]:
    """Fail closed: proposals belong in the canonical Rust candidate queue."""
    raise VersionedMemoryError(
        "knowledge proposals must use Frankenmemory's scoped Rust candidate queue"
    )


def transition_knowledge(
    identifier: str,
    action: str,
    *,
    owner: str,
    workspace_id: str = "global",
    project_id: str = "",
    expected_revision: int,
    value: Any = None,
    value_type: Optional[str] = None,
    expected_value_type: Optional[str] = None,
    kind: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
    target_revision: Optional[int] = None,
    actor_type: str = "user",
    actor_id: Optional[str] = None,
    reason: Optional[str] = None,
    db_path: str = FM_DB_PATH,
) -> dict[str, Any]:
    """Append one published lifecycle revision with optimistic concurrency.

    Proposal/edit/review/activation is intentionally absent here.  That flow is
    Rust ``candidates`` -> review -> curated -> v2 mirror.  This function owns
    only published corrections and open-question lifecycle transitions.
    """
    action = str(action or "").strip().lower()
    if action == "activate":
        raise VersionedMemoryError("activation must review a scoped Rust candidate")
    if action not in {"edit", "correct", "resolve", "reopen", "retract", "revert", "supersede"}:
        raise VersionedMemoryError(f"unsupported knowledge transition {action!r}")
    scope = KnowledgeScope(owner, workspace_id, project_id)
    with sqlite3.connect(db_path, timeout=30) as conn:
        block_id = _resolve_block_id(conn, scope, identifier)
    repository = V2Repository(db_path)
    try:
        current = repository.get_knowledge(
            owner=scope.owner,
            workspace_id=scope.workspace_key,
            project_id=scope.project_key or None,
            block_id=block_id,
        )
        if int(expected_revision) != int(current["revision"]):
            proposal: dict[str, Any] = {
                "activation_rationale": {
                    "action": action,
                    "reason": str(reason or action),
                }
            }
            if value is not None:
                proposal["value"] = _typed(value, value_type)
            if kind is not None:
                proposal["kind"] = str(kind)
            if tags is not None:
                proposal["tags"] = [str(tag) for tag in tags]
            try:
                repository.record_conflict(
                    owner=scope.owner,
                    workspace_id=scope.workspace_key,
                    project_id=scope.project_key or None,
                    block_id=block_id,
                    expected_revision=int(expected_revision),
                    proposal=proposal,
                    actor_type=actor_type,
                    actor_id=actor_id or scope.owner,
                    rationale="optimistic revision mismatch",
                )
            except V2OperationError:
                pass
            raise VersionedMemoryConflict(int(expected_revision), current)
        if action == "revert":
            if target_revision is None:
                raise VersionedMemoryError("revert requires target_revision")
            result = repository.revert_knowledge(
                owner=scope.owner,
                workspace_id=scope.workspace_key,
                project_id=scope.project_key or None,
                block_id=block_id,
                expected_revision=int(expected_revision),
                target_revision=int(target_revision),
                actor_type=actor_type,
                actor_id=actor_id or scope.owner,
                reason=reason or "revert knowledge",
            )
            return _detail(repository, scope, result["block_id"])

        payload: dict[str, Any] = {
            "activation": "manual",
            "activation_rationale": {
                "action": action,
                "reason": str(reason or action),
                "actor": str(actor_id or scope.owner),
            },
        }
        if kind is not None:
            payload["kind"] = str(kind)
        if tags is not None:
            payload["tags"] = sorted(
                {str(tag).strip() for tag in tags if str(tag).strip()}
            )
        if expected_value_type is not None:
            payload["expected_value_type"] = str(expected_value_type)

        if action == "resolve":
            if current["kind"] != "open_question" or current["status"] != "open":
                raise VersionedMemoryError("resolve is only valid for an open question")
            if value is None or (isinstance(value, str) and not value.strip()):
                raise VersionedMemoryError("resolve requires the answer value")
            payload.update({"value": _typed(value, value_type), "status": "active"})
        elif action == "reopen":
            if current["kind"] != "open_question":
                raise VersionedMemoryError("reopen is only valid for an open question")
            payload.update(
                {
                    "value": {"type": "null"},
                    "expected_value_type": expected_value_type
                    or current.get("expected_value_type")
                    or "string",
                    "status": "open",
                }
            )
        elif action == "retract":
            payload["status"] = "retracted"
        elif action == "supersede":
            payload["status"] = "superseded"
        else:
            if value is not None:
                payload["value"] = _typed(value, value_type)
            next_value = payload.get("value", current["value"])
            payload["status"] = "open" if next_value.get("type") == "null" else "active"

        result = repository.update_knowledge(
            owner=scope.owner,
            workspace_id=scope.workspace_key,
            project_id=scope.project_key or None,
            block_id=block_id,
            expected_revision=int(expected_revision),
            payload=payload,
            actor_type=actor_type,
            actor_id=actor_id or scope.owner,
            reason=reason or action,
        )
    except RevisionConflict as exc:
        raise VersionedMemoryConflict(int(expected_revision), exc.current or {}) from exc
    except V2OperationError as exc:
        if exc.code == "not_found":
            raise VersionedMemoryNotFound("knowledge was not found in this scope") from exc
        raise VersionedMemoryError(str(exc)) from exc
    return _detail(repository, scope, result["block_id"])


def canonical_graph(
    op: str,
    *,
    owner: str,
    workspace_id: str = "global",
    project_id: str = "",
    query: Optional[str] = None,
    node_id: Optional[str] = None,
    to_node_id: Optional[str] = None,
    tag: Optional[str] = None,
    direction: Optional[str] = None,
    limit: int = 100,
    db_path: str = FM_DB_PATH,
) -> dict[str, Any]:
    """Read the current entity/block/evidence/source graph from canonical rows."""
    details = list_knowledge(
        owner=owner,
        workspace_id=workspace_id,
        project_id=project_id,
        statuses={"open", "active"},
        limit=5000,
        db_path=db_path,
    )
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    def edge_id(src: str, dst: str, edge_tag: str) -> str:
        import hashlib

        return "edge_" + hashlib.sha256(
            f"{src}\0{dst}\0{edge_tag}".encode("utf-8")
        ).hexdigest()[:24]

    for item in details:
        current = item["current"]
        entity = item["entity"]
        entity_id = item["subject_entity_id"]
        block_id = item["block_id"]
        entity_label = entity.get("canonical_label") or entity_id
        nodes[entity_id] = {
            "id": entity_id,
            "label": entity_label,
            "name": entity_label,
            "kind": entity.get("entity_type") or "concept",
            "trust": current.get("trust", 0.5),
            "last_seen": current.get("created_at"),
        }
        block_label = current.get("text") or item["predicate"]
        nodes[block_id] = {
            "id": block_id,
            "label": block_label,
            "name": block_label,
            "kind": current.get("kind") or "knowledge",
            "trust": current.get("trust", 0.5),
            "last_seen": current.get("created_at"),
        }
        eid = edge_id(entity_id, block_id, item["predicate"])
        edges[eid] = {
            "id": eid,
            "src_id": entity_id,
            "dst_id": block_id,
            "tag": item["predicate"],
            "fact": block_label,
        }
        for evidence in item["evidence"]:
            evidence_id = evidence["id"]
            source_id = evidence["source_id"]
            nodes[evidence_id] = {
                "id": evidence_id,
                "label": evidence.get("quote") or "evidence",
                "name": evidence.get("quote") or "evidence",
                "kind": "evidence",
                "trust": current.get("trust", 0.5),
                "last_seen": evidence.get("created_at"),
            }
            nodes[source_id] = {
                "id": source_id,
                "label": evidence.get("source_uri") or source_id,
                "name": evidence.get("source_uri") or source_id,
                "kind": "source",
                "trust": current.get("trust", 0.5),
                "last_seen": evidence.get("created_at"),
            }
            for src, dst, edge_tag in (
                (block_id, evidence_id, "evidence"),
                (evidence_id, source_id, "source"),
            ):
                eid = edge_id(src, dst, edge_tag)
                edges[eid] = {
                    "id": eid,
                    "src_id": src,
                    "dst_id": dst,
                    "tag": edge_tag,
                    "fact": evidence.get("quote"),
                }

    all_nodes = list(nodes.values())
    all_edges = list(edges.values())
    if tag:
        all_edges = [edge for edge in all_edges if edge["tag"] == tag]
    limit = max(1, min(int(limit), 500))
    if op in {"cues", "rank"}:
        needle = str(query or "").casefold()
        hits = [
            {
                "node": node,
                "score": 1.0
                if needle and needle in str(node.get("label") or "").casefold()
                else 0.5,
            }
            for node in all_nodes
            if not needle or needle in str(node.get("label") or "").casefold()
        ][:limit]
        return {"op": op, "hits": hits, "canonical": True}
    if op == "fetch":
        return {"op": op, "node": nodes.get(str(node_id or "")), "canonical": True}
    if op == "expand":
        target = str(node_id or "")
        hits = []
        for edge in all_edges:
            if edge["src_id"] != target and edge["dst_id"] != target:
                continue
            if direction == "out" and edge["src_id"] != target:
                continue
            if direction == "in" and edge["dst_id"] != target:
                continue
            other_id = edge["dst_id"] if edge["src_id"] == target else edge["src_id"]
            hits.append({"edge": edge, "other": nodes.get(other_id)})
        return {"op": op, "hits": hits[:limit], "canonical": True}
    if op == "tags":
        counts: dict[str, int] = {}
        for edge in all_edges:
            counts[edge["tag"]] = counts.get(edge["tag"], 0) + 1
        tags = [
            {"tag": key, "count": value}
            for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]
        return {"op": op, "tags": tags[:limit], "canonical": True}
    if op == "trace":
        start, goal = str(node_id or ""), str(to_node_id or "")
        adjacency: dict[str, list[tuple[str, str]]] = {}
        for edge in all_edges:
            adjacency.setdefault(edge["src_id"], []).append((edge["dst_id"], edge["tag"]))
            adjacency.setdefault(edge["dst_id"], []).append((edge["src_id"], edge["tag"]))
        queue = deque([(start, [start], [])])
        seen = {start}
        found: list[dict[str, Any]] = []
        while queue and len(found) < limit:
            current, path, labels = queue.popleft()
            if current == goal:
                # Keep the canonical trace shape identical to the Rust graph
                # contract and the Brain canvas reader.  The browser consumes
                # ``node_ids`` (not the ambiguous legacy ``nodes`` key), so a
                # canonical-only trace must remain clickable/renderable.
                found.append({"node_ids": path, "tags": labels})
                continue
            for neighbor, edge_tag in adjacency.get(current, []):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                queue.append((neighbor, [*path, neighbor], [*labels, edge_tag]))
        return {"op": op, "paths": found, "canonical": True}
    visible_nodes = all_nodes[:limit]
    visible_node_ids = {str(node["id"]) for node in visible_nodes}
    visible_edges = [
        edge
        for edge in all_edges
        if str(edge["src_id"]) in visible_node_ids
        and str(edge["dst_id"]) in visible_node_ids
    ][: max(limit * 3, limit)]
    return {
        "op": "overview",
        "nodes": visible_nodes,
        "edges": visible_edges,
        "node_total": len(all_nodes),
        "edge_total": len(all_edges),
        "canonical": True,
    }

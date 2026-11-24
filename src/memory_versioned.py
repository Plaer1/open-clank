"""Application-facing history and lifecycle over canonical Memery v2.

Published knowledge lives in the append-only ``fm_v2_knowledge_*`` tables.
Unreviewed material does not: Rust ``candidates`` is the one proposal queue and
is reached through :class:`MemeryProvider`.  Keeping that boundary here
prevents a second, subtly different candidate system from growing in Python.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import hashlib
import os
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Optional

from src.constants import FM_DB_PATH
from src.memery_v2 import (
    RevisionConflict,
    V2OperationError,
    V2Repository,
    _scope_is_visible,
    _provider_dossier_evidence_blocked,
    _provider_dossier_source_blocked,
    _tables_ready, _require_trust_schema, _require_manifest_schema,
    guarded_update_operation,
    _sealed_json,
    _hash,
)


@dataclass(frozen=True)
class PublishedReviewRequest:
    """Internal authenticated route context; never deserialize model arguments into it."""
    owner: str
    workspace_id: str
    request_key: str
    approved_paths: tuple[str, ...]
    account_id: Optional[str] = None
    before_issue: Optional[Callable[[], bool]] = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ConfirmedQuestionReview:
    """Host-created exact identity and explicit answer for one reviewed question."""
    review: PublishedReviewRequest
    block_id: str
    subject_entity_id: str
    predicate: str
    claim_slot: str
    expected_revision: int
    answer: str


def question_review_operation_key(account_id: str, original_key: str) -> str:
    """Bind one opaque Human review key to its independently authenticated account."""
    import hashlib
    values=(account_id, original_key)
    if any(type(v) is not str or not v or v != v.strip() or len(v.encode("utf-8")) > 512 for v in values):
        raise VersionedMemoryError("question review account unavailable")
    digest=hashlib.sha256(b"s09.question-review-key.v1\0")
    for value in values:
        raw=value.encode("utf-8");digest.update(len(raw).to_bytes(8,"big"));digest.update(raw)
    return "s09q_"+digest.hexdigest()


def prepare_published_lifecycle(*, db_path: str, owner: str, workspace_id: str, block_id: str,
                                action: str, expected_revision: int, target_revision: Optional[int],
                                request_key: str, approved_paths: Optional[tuple[str, ...]] = None,
                                account_id: Optional[str] = None) -> dict[str, Any]:
    """Build an exact reviewed operation without writing or retargeting a head."""
    from src.memery_field_authority import proposal_fields
    import hashlib
    question_actions={'question_defer':'deferred','question_dismiss':'dismissed','question_reopen':'eligible'}
    if (action not in {'retract','revert',*question_actions} or type(expected_revision) is not int or expected_revision<1
        or type(request_key) is not str or not request_key or request_key!=request_key.strip() or len(request_key)>512
        or action in {'retract',*question_actions} and target_revision is not None
        or action=='revert' and (type(target_revision) is not int or target_revision<1)):
        raise VersionedMemoryError('published review unavailable')
    repo=V2Repository(db_path,guarded_v17=True)
    current=repo.get_knowledge(owner=owner,workspace_id=workspace_id,block_id=block_id,revision=expected_revision)
    scope={'owner_id':owner,'workspace_id':workspace_id,'project_id':None,'session_id':None}
    if current.get('block_id')!=block_id or current.get('scope')!=scope:
        raise VersionedMemoryError('published review scope changed')
    before=repo._prepare_knowledge_payload({},current)
    if action=='retract':
        if current.get('status') not in ('active','open'): raise VersionedMemoryError('memory is not retractable')
        payload={'status':'retracted'}; reason='retracted by user'
        changes=[{'path':'/status','before':{'present':True,'value':current['status']},'after':{'present':True,'value':'retracted'}}]
    elif action in question_actions:
        from copy import deepcopy
        from services.memory.question_context import normalize_question_review
        rationale=current.get('activation_rationale')
        if (current.get('kind')!='open_question' or current.get('status')!='open'
            or current.get('value')!={'type':'null'} or type(rationale) is not dict
            or 'question_context' not in rationale
            or (rationale['question_context'] is not None and type(rationale['question_context']) is not dict)):
            raise VersionedMemoryError('question review unavailable')
        normalize_question_review(rationale.get('question_review'))
        updated=deepcopy(rationale)
        updated['question_review']={'version':1,'disposition':question_actions[action]}
        payload={'activation_rationale':updated};reason=action.replace('_',' ')
        changes=proposal_fields(before,{**before,**payload})
    else:
        target=repo.get_knowledge(owner=owner,workspace_id=workspace_id,block_id=block_id,revision=target_revision)
        if (target.get('scope')!=scope or current.get('source_event_id') is not None or target.get('source_event_id') is not None
            or current.get('evidence_ids') or target.get('evidence_ids') or current.get('kind')=='open_question' or target.get('kind')=='open_question'):
            raise VersionedMemoryError('reversion source unavailable')
        payload=repo._prepare_knowledge_payload({},target);reason=f'revert revision {target_revision}'
        changes=proposal_fields(before,payload)
    paths=tuple(change['path'] for change in changes)
    if not paths or len(paths)>64 or (approved_paths is not None and approved_paths!=paths):
        raise VersionedMemoryError('published review fields changed')
    operation_key=question_review_operation_key(account_id,request_key) if action in question_actions else request_key
    operation=guarded_update_operation(owner=owner,workspace=workspace_id,project='',block_id=block_id,
        expected_revision=expected_revision,payload=payload,request_key=operation_key,actor_type='user',actor_id=owner,
        reason=reason,source_event_id=None,approved_paths=paths)
    encoded=_sealed_json(operation)
    if len(encoded.encode('utf-8')) > 65536:
        raise VersionedMemoryError('published review is too large')
    return {'version':'s03.lifecycle-preview.v1','owner':owner,'workspace_id':workspace_id,'block_id':block_id,'action':action,
            'expected_revision':expected_revision,'target_revision':target_revision,'request_key':request_key,'reason':reason,
            'selected_paths':list(paths),'changes':changes,'operation_json':encoded,'operation_sha256':hashlib.sha256(encoded.encode()).hexdigest()}


class VersionedMemoryError(RuntimeError):
    """A typed knowledge request is invalid in its current lifecycle state."""


class VersionedMemoryNotFound(VersionedMemoryError):
    pass


def read_source_reference(*, db_path: str, owner: str, workspace_id: str,
                          reference: Mapping[str, Any]) -> dict[str, Any] | None:
    """Bounded read-only S09 projection. Never initialize/migrate a database.

    These are persisted producer revisions, not proofs of current disk bytes.
    SQL bounds each returned text value before it crosses into Python.
    """
    from urllib.parse import quote
    from contextlib import closing
    kind = reference["kind"]
    with closing(sqlite3.connect("file:" + quote(str(db_path), safe="/") + "?mode=ro", uri=True, timeout=1)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        # A corrupt/unindexed database cannot cause an unbounded bridge scan.
        remaining = 1000
        def progress():
            nonlocal remaining
            remaining -= 1
            return int(remaining <= 0)
        conn.set_progress_handler(progress, 1000)
        common = """CASE WHEN length(CAST(s.source_id AS BLOB))<=256 THEN s.source_id END AS source_id,s.source_revision,s.forget_state,
            CASE WHEN length(CAST(s.content_hash AS BLOB))<=128 THEN s.content_hash END AS source_hash,
            CASE WHEN length(CAST(s.source_type AS BLOB))<=128 THEN s.source_type END AS source_type,
            (SELECT max(n.source_revision) FROM fm_v2_sources n WHERE n.owner_id=s.owner_id
              AND n.source_id=s.source_id AND n.workspace_key=s.workspace_key AND n.project_key=s.project_key) AS latest_revision"""
        if kind == "memory_evidence":
            source = conn.execute("""SELECT s.source_id,s.source_revision FROM fm_v2_evidence e JOIN fm_v2_sources s
                ON s.owner_id=e.owner_id AND s.source_id=e.source_id AND s.source_revision=e.source_revision
                WHERE e.owner_id=? AND e.evidence_id=? AND s.source_revision=? AND s.workspace_key=? AND s.project_key='' LIMIT 1""",
                (owner,reference["evidence_id"],reference["source_revision"],workspace_id)).fetchone()
        else:
            source = conn.execute("""SELECT s.source_id,s.source_revision FROM fm_v2_chunks c JOIN fm_v2_documents d
                ON d.owner_id=c.owner_id AND d.document_id=c.document_id JOIN fm_v2_sources s
                ON s.owner_id=d.owner_id AND s.source_id=d.source_id AND s.source_revision=d.source_revision
                WHERE c.owner_id=? AND d.document_id=? AND c.chunk_id=? AND s.source_revision=? AND s.workspace_key=? AND s.project_key='' LIMIT 1""",
                (owner,reference["document_id"],reference["chunk_id"],reference["source_revision"],workspace_id)).fetchone()
        if source is None or _provider_dossier_source_blocked(conn,owner,source["source_id"],source["source_revision"]):
            return None
        if kind == "memory_evidence":
            row = conn.execute("SELECT " + common + """,e.evidence_id,
                CASE WHEN length(CAST(e.locator_type AS BLOB))<=128 THEN e.locator_type END AS locator_type,
                CASE WHEN length(CAST(e.locator_json AS BLOB))<=8192 THEN e.locator_json END AS locator_json,
                CASE WHEN length(CAST(e.quote AS BLOB))<=8192 THEN e.quote END AS excerpt,
                length(CAST(e.quote AS BLOB))>8192 AS excerpt_oversize,
                CASE WHEN length(CAST(e.extraction_contract AS BLOB))<=128 THEN e.extraction_contract END AS extraction_contract,
                CASE WHEN length(CAST(e.parser_version AS BLOB))<=128 THEN e.parser_version END AS parser_version
                FROM fm_v2_evidence e JOIN fm_v2_sources s ON s.owner_id=e.owner_id
                  AND s.source_id=e.source_id AND s.source_revision=e.source_revision
                WHERE e.owner_id=? AND e.evidence_id=? AND s.source_revision=?
                  AND s.workspace_key=? AND s.project_key='' LIMIT 1""",
                (owner, reference["evidence_id"], reference["source_revision"], workspace_id)).fetchone()
        else:
            row = conn.execute("SELECT " + common + """,c.chunk_id,d.document_id,
                'text' AS locator_type,
                CASE WHEN length(CAST(c.locator_json AS BLOB))<=8192 THEN c.locator_json END AS locator_json,
                CASE WHEN length(CAST(c.text AS BLOB))<=8192 THEN c.text END AS excerpt,
                length(CAST(c.text AS BLOB))>8192 AS excerpt_oversize,
                CASE WHEN length(CAST(c.content_hash AS BLOB))<=128 THEN c.content_hash END AS chunk_hash,
                CASE WHEN length(CAST(d.parser_version AS BLOB))<=128 THEN d.parser_version END AS parser_version
                FROM fm_v2_chunks c JOIN fm_v2_documents d ON d.owner_id=c.owner_id AND d.document_id=c.document_id
                JOIN fm_v2_sources s ON s.owner_id=d.owner_id AND s.source_id=d.source_id AND s.source_revision=d.source_revision
                WHERE c.owner_id=? AND d.document_id=? AND c.chunk_id=? AND s.source_revision=?
                  AND s.workspace_key=? AND s.project_key='' LIMIT 1""",
                (owner, reference["document_id"], reference["chunk_id"], reference["source_revision"], workspace_id)).fetchone()
        if row is None:
            return None
        result = dict(row)
        if (kind == "memory_evidence" and result["locator_type"] == "memory_record"
                and conn.execute("PRAGMA user_version").fetchone()[0] >= 17):
            # A guarded mirror edit can retain an original evidence ID even
            # when its original quotation no longer supports the edited value.
            locator = json.loads(result["locator_json"])
            record = locator.get("record_id")
            if not isinstance(record, str) or not record:
                return None
            try:
                block = _resolve_block_id(conn, KnowledgeScope(owner, workspace_id), record)
            except VersionedMemoryNotFound:
                return None
            if not history_block_visible(conn, owner, block):
                return None
            head = conn.execute("""SELECT r.value_json,r.status FROM fm_v2_knowledge_blocks b
                JOIN fm_v2_knowledge_revisions r ON r.owner_id=b.owner_id AND r.block_id=b.block_id
                  AND r.revision=b.current_revision WHERE b.owner_id=? AND b.block_id=?""", (owner, block)).fetchone()
            if head is None or head["status"] != "active":
                return None
            value = json.loads(head["value_json"])
            if isinstance(value, dict) and value.get("type") == "string":
                result["superseded_quote"] = value.get("value") != result["excerpt"]
        return result


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
        if payload.get("version") == "s08.policy-summary.v1" and isinstance(payload.get("summary"), str):
            return payload["summary"]
        if payload.get("version") == "s08.policy-journal.v1" and isinstance(payload.get("content"), str):
            return payload["content"]
        data = payload.get("data")
        if isinstance(data, str):
            return data
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _scope(scope: KnowledgeScope) -> tuple[str, str, str]:
    return scope.owner.strip(), scope.workspace_key, scope.project_key


def history_block_visible(conn, owner, block_id):
    """Guard retained native rows whose legacy curated record has been forgotten."""
    if conn.execute("PRAGMA user_version").fetchone()[0] < 17:
        return True
    row = conn.execute("""SELECT b.workspace_key,b.claim_slot,b.predicate,r.kind,r.status,
        (SELECT substr(h.source_event_id,8) FROM fm_v2_knowledge_revisions h
         WHERE h.owner_id=b.owner_id AND h.block_id=b.block_id AND h.source_event_id LIKE 'memory:%'
         ORDER BY h.revision DESC LIMIT 1) AS curated_id
        FROM fm_v2_knowledge_blocks b JOIN fm_v2_knowledge_revisions r
          ON r.owner_id=b.owner_id AND r.block_id=b.block_id AND r.revision=b.current_revision
        WHERE b.owner_id=? AND b.block_id=?""", (owner, block_id)).fetchone()
    if row is None:
        return False
    workspace, slot, predicate, kind, status, curated = row
    if predicate == 's08.composite':
        return conn.execute("""SELECT 1 FROM candidates k JOIN curated c
            ON c.owner=k.owner AND c.id=k.accepted_curated_id WHERE k.owner=? AND k.id=?
            AND k.status='accepted' AND COALESCE(c.workspace_id,'global')=? AND COALESCE(c.archived,0)=0""",
            (owner, slot, workspace or 'global')).fetchone() is not None
    if not curated:
        return True
    return conn.execute("""SELECT 1 FROM curated WHERE owner=? AND id=? AND COALESCE(workspace_id,'global')=?
        AND (COALESCE(archived,0)=0 OR ?)""",
        (owner, curated, workspace or 'global', kind == 'open_question' and status == 'active')).fetchone() is not None


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


def _detail(repository: V2Repository, scope: KnowledgeScope, block_id: str) -> dict[str, Any]:
    # Use the same guarded snapshot and evidence materializer as bulk reads.
    try:
        details = _details(repository, scope, [block_id], require_repository_preflight=True)
    except V2OperationError as exc:
        if exc.code == "not_found": raise VersionedMemoryNotFound("knowledge was not found in this scope") from exc
        raise VersionedMemoryError(str(exc)) from exc
    if not details:
        raise VersionedMemoryNotFound("knowledge was not found in this scope")
    return details[0]


def _details(
    repository: V2Repository,
    scope: KnowledgeScope,
    block_ids: Optional[list[str]],
    *, require_repository_preflight: bool = False, graph_preparation=None, graph_profile=None,
) -> list[dict[str, Any]]:
    """Batch counterpart of ``_detail``: one connection, set-based queries.

    ``_detail`` pays three-plus connections per block (current revision,
    history, evidence/conflicts, entity), so listing hundreds of blocks made
    ``list_knowledge``/``canonical_graph`` O(n) on connection setup. The
    assembled dicts are identical: same view builders, same per-block
    ordering, same evidence/conflict/entity fallbacks.
    """
    owner, workspace, project = _scope(scope)
    if block_ids is not None and not block_ids:
        return []
    joined_columns = (
        "b.owner_id,b.block_id,b.workspace_key,b.project_key,"
        "b.subject_entity_id,b.predicate,b.claim_slot,b.cardinality,r.*"
    )
    with sqlite3.connect(repository.db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        if require_repository_preflight:
            conn.execute("PRAGMA foreign_keys=ON")
            if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise V2OperationError("validation", "SQLite foreign keys are unavailable")
        conn.execute("BEGIN")
        if graph_preparation is not None:
            marker=conn.execute("SELECT value FROM fm_meta WHERE key='openclank.s01.knowledge-ref.v1'").fetchone()
            database=conn.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
            cookie=conn.execute("PRAGMA schema_version").fetchone()[0]
            if not database or database[0]!=graph_preparation.database_id or cookie!=graph_preparation.schema_cookie:
                raise V2OperationError("unsupported_contract", "guarded graph preparation changed")
            graph_profile["knowledge_ref"] = bool(marker and marker[0]=='1')
        if block_ids is None:
            block_ids=[str(row[0]) for row in conn.execute("SELECT block_id FROM fm_v2_knowledge_blocks WHERE owner_id=? AND workspace_key=? AND project_key=? ORDER BY created_at DESC LIMIT 5000",(owner,workspace,project)).fetchall()]
            if not block_ids: return []
        marks = ",".join("?" for _ in block_ids)
        if require_repository_preflight:
            if not _tables_ready(conn): raise V2OperationError("unsupported_contract", "Memery v2 schema is unavailable")
            _require_trust_schema(conn)
            _require_manifest_schema(conn)
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
        block_ids = [block_id for block_id in block_ids
                     if history_block_visible(conn, owner, block_id) and _scope_is_visible(current_by_block[block_id]["workspace_key"], current_by_block[block_id]["project_key"], workspace, project)
                     and not _provider_dossier_evidence_blocked(conn, owner, block_id, current_by_block[block_id]["revision"])]
        current_by_block = {block_id: current_by_block[block_id] for block_id in block_ids}
        if not block_ids:
            return []
        marks = ",".join("?" for _ in block_ids)
        connection_metadata = {}
        if graph_profile is not None and graph_profile.get("knowledge_ref"):
            from src.openclank.resource_metadata import validate_metadata, _unique
            for row in conn.execute(
                f"SELECT m.*,r.change_set_id AS revision_change,r.actor_type AS revision_actor_type,r.actor_id AS revision_actor_id,r.source_event_id AS revision_source_event,r.evidence_json AS revision_evidence FROM fm_v2_revision_metadata m JOIN fm_v2_knowledge_blocks b ON b.owner_id=m.owner_id AND b.block_id=m.block_id AND b.current_revision=m.revision JOIN fm_v2_knowledge_revisions r ON r.owner_id=m.owner_id AND r.block_id=m.block_id AND r.revision=m.revision WHERE m.owner_id=? AND m.block_id IN ({marks})", [owner,*block_ids]):
                raw=row["metadata_json"]
                if type(raw) is not str or len(raw.encode())>65536 or hashlib.sha256(raw.encode()).hexdigest()!=row["metadata_hash"]:
                    raise V2OperationError("unsupported_contract", "connection metadata unavailable")
                try:
                    metadata=validate_metadata(json.loads(raw,object_pairs_hook=_unique),allow_policy_overrides=True,allow_connection=True)
                except (TypeError,ValueError):
                    raise V2OperationError("unsupported_contract", "connection metadata unavailable")
                if "connection" in metadata:
                    if (row["metadata_schema"]!="openclank.memory-revision-metadata.v1" or row["change_set_id"]!=row["revision_change"] or row["actor_type"]!="user" or row["revision_actor_type"]!="user" or row["actor_id"]!=row["revision_actor_id"] or row["source_event_id"]!=row["revision_source_event"] or row["evidence_json"]!=row["revision_evidence"]):
                        raise V2OperationError("unsupported_contract", "connection metadata unavailable")
                    authority=conn.execute("SELECT origin,producer_type,producer_id,approved_by,source_event_id,evidence_json,change_set_id FROM fm_v2_revision_field_authority WHERE owner_id=? AND block_id=? AND revision=? AND field_path='/metadata/connection'",(owner,row["block_id"],row["revision"])).fetchone()
                    if (authority is None or authority["origin"]!="manual" or authority["producer_type"]!="user" or authority["producer_id"]!=row["actor_id"] or authority["approved_by"]!=row["actor_id"] or authority["source_event_id"]!=row["source_event_id"] or authority["evidence_json"]!=row["evidence_json"] or authority["change_set_id"]!=row["change_set_id"]):
                        raise V2OperationError("unsupported_contract", "connection metadata authority unavailable")
                    connection_metadata[str(row["block_id"])]=metadata["connection"]
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
            if not _provider_dossier_evidence_blocked(conn, owner, row["block_id"], row["revision"]):
                history_by_block[str(row["block_id"])].append(V2Repository._knowledge_view(row))
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
        def with_revision_text(revision):
            revision_evidence = set(revision.get("evidence_ids") or ())
            quote = next((item["quote"] for item in evidence if item["id"] in revision_evidence), "")
            return {**revision, "text": _text_value(revision["value"], quote)}

        history = [with_revision_text(revision) for revision in reversed(history_ascending)]
        current_with_text = with_revision_text(current)
        details.append(
            {
                "id": _memory_id(history_ascending, block_id),
                "block_id": block_id,
                "connection_metadata": connection_metadata.get(block_id),
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
        "knowledge proposals must use Memery's scoped Rust candidate queue"
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
    guarded_v17: bool = False,
    published_review: Optional[PublishedReviewRequest] = None,
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
    if guarded_v17 and (not isinstance(published_review, PublishedReviewRequest) or published_review.owner != scope.owner or published_review.workspace_id != scope.workspace_key):
        raise VersionedMemoryError("authenticated scoped review required")
    if not guarded_v17 and published_review is not None:
        raise VersionedMemoryError("guarded review is not enabled")
    repository = V2Repository(db_path, guarded_v17=guarded_v17)
    try:
        current = repository.get_knowledge(
            owner=scope.owner,
            workspace_id=scope.workspace_key,
            project_id=scope.project_key or None,
            block_id=block_id,
            revision=int(expected_revision) if guarded_v17 else None,
        )
        if not guarded_v17 and int(expected_revision) != int(current["revision"]):
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
        if action == "revert" and not guarded_v17:
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

        if action == "revert":
            if target_revision is None:
                raise VersionedMemoryError("revert requires target_revision")
            target = repository.get_knowledge(owner=scope.owner, workspace_id=scope.workspace_key,
                project_id=scope.project_key or None, block_id=block_id, revision=int(target_revision))
            payload = repository._prepare_knowledge_payload({}, target)
        elif action == "resolve":
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

        if guarded_v17 and current.get("kind") == "open_question":
            # Ordinary reviewed transitions retain scoped question identity and
            # an earlier disposition; only the dedicated Human owner changes it.
            from copy import deepcopy
            from services.memory.question_context import normalize_question_review
            existing=current.get("activation_rationale")
            if isinstance(existing, Mapping):
                rationale=payload.get("activation_rationale")
                rationale=dict(rationale) if isinstance(rationale, Mapping) else {"prior_rationale":rationale}
                for key in ("question_context", "question_text", "question_review"):
                    if key in existing:
                        if key == "question_review": normalize_question_review(existing[key])
                        rationale[key]=deepcopy(existing[key])
                payload["activation_rationale"]=rationale

        review = None
        if guarded_v17:
            from src.memery_field_authority import OwnerReviewContext
            operation = guarded_update_operation(owner=scope.owner, workspace=scope.workspace_key,
                project=scope.project_key, block_id=block_id, expected_revision=int(expected_revision),
                payload=payload, request_key=published_review.request_key, actor_type="user", actor_id=scope.owner,
                reason=reason or action, source_event_id=None, approved_paths=published_review.approved_paths)
            sealed_operation_json = _sealed_json(operation)
            review = OwnerReviewContext(scope.owner, scope.owner, int(expected_revision),
                published_review.request_key, _hash(operation), published_review.approved_paths)
        result = repository.update_knowledge(
            owner=scope.owner,
            workspace_id=scope.workspace_key,
            project_id=scope.project_key or None,
            block_id=block_id,
            expected_revision=int(expected_revision),
            payload=payload,
            actor_type="user" if guarded_v17 else actor_type,
            actor_id=scope.owner if guarded_v17 else actor_id or scope.owner,
            reason=reason or action,
            **({"trusted_review": review, "request_key": published_review.request_key, "sealed_operation_json": sealed_operation_json} if guarded_v17 else {}),
        )
    except RevisionConflict as exc:
        raise VersionedMemoryConflict(int(expected_revision), exc.current or {}) from exc
    except V2OperationError as exc:
        if exc.code == "not_found":
            raise VersionedMemoryNotFound("knowledge was not found in this scope") from exc
        raise VersionedMemoryError(str(exc)) from exc
    detail = _detail(repository, scope, result["block_id"])
    if guarded_v17:
        detail["applied_revision"] = result["revision"]
        detail["applied"] = {**result, "text": _text_value(result["value"], "")}
        detail["field_decision"] = result["field_decision"]
    return detail


def _with_live_curated_lineage(details: list[dict[str, Any]], *, owner: str, db_path: str) -> list[dict[str, Any]]:
    """The panel list's lineage rule for the graph (src/memery_v2.py list_current_records, require_legacy_presence=True).

    A block that has a curated lineage (``_memory_id`` found a ``memory:`` source event, so ``item["id"] != block_id``) is
    served only while that curated record is live (same owner and workspace, not archived; a resolved open question accepts
    an archived row). Blocks without a curated lineage (principal/handler entities, seeds, composites) are kept. Read-only."""
    lineage = {item["id"] for item in details if item["id"] != item["block_id"]}
    s08 = [item for item in details if item.get("predicate") == "s08.composite"]
    if not lineage and not s08:
        return details
    found: dict[str, tuple[str, int]] = {}
    with sqlite3.connect(db_path, timeout=30) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='curated'").fetchone():
            ids = sorted(lineage)
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                for row in conn.execute(
                    f"SELECT id,COALESCE(workspace_id,'global'),COALESCE(archived,0) FROM curated WHERE owner=? AND id IN ({','.join('?' for _ in chunk)})",
                    [owner, *chunk],
                ):
                    found[str(row[0])] = (str(row[1]), int(row[2]))

    # An accepted S08 consolidation block (summary or journal synthesis) is current only while the curated record its accepted candidate
    # points at is live (same owner and workspace, not archived): the block's claim slot is that candidate's id.
    s08_live: set[str] = set()
    if s08:
        with sqlite3.connect(db_path, timeout=30) as conn:
            if {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {"curated", "candidates"}:
                for item in s08:
                    row = conn.execute(
                        "SELECT 1 FROM candidates k JOIN curated c ON c.id=k.accepted_curated_id AND c.owner=k.owner WHERE k.id=? AND k.owner=? AND k.status='accepted'"
                        " AND COALESCE(c.workspace_id,'global')=? AND COALESCE(c.archived,0)=0",
                        (item["claim_slot"], owner, item["workspace_id"] or "global")).fetchone()
                    if row:
                        s08_live.add(item["block_id"])

    def keep(item: dict[str, Any]) -> bool:
        if item.get("predicate") == "s08.composite":
            return item["block_id"] in s08_live
        if item["id"] == item["block_id"]:
            return True
        live = found.get(item["id"])
        if live is None or live[0] != (item["current"]["scope"].get("workspace_id") or "global"):
            return False
        return live[1] == 0 or (item["current"].get("kind") == "open_question" and item["current"].get("status") == "active")

    return [item for item in details if keep(item)]


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
    guarded_preparation=None,
) -> dict[str, Any]:
    """Read the current entity/block/evidence/source graph from canonical rows."""
    graph_profile={"knowledge_ref":False}
    if guarded_preparation is not None:
        from src.memery_database import GuardedStorePreparation
        if type(guarded_preparation) is not GuardedStorePreparation or os.path.realpath(db_path)!=guarded_preparation.database_path:
            raise VersionedMemoryError("guarded graph preparation unavailable")
        guarded_preparation.assert_valid()
        details=_details(V2Repository(db_path),KnowledgeScope(owner,workspace_id,project_id),None,graph_preparation=guarded_preparation,graph_profile=graph_profile)
        details=[item for item in details if item["current"]["status"] in {"open","active"}]
        guarded_preparation.assert_valid()
    else:
        details = list_knowledge(
        owner=owner,
        workspace_id=workspace_id,
        project_id=project_id,
        statuses={"open", "active"},
        limit=5000,
        db_path=db_path,
    )
    details = _with_live_curated_lineage(details, owner=owner, db_path=db_path)
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

    # A knowledge reference is a scoped revision fact, not a code/runtime edge.
    # Only project a target already independently visible in this bounded read.
    by_block = {item["block_id"]: item for item in details}
    for item in details:
        value = item["current"].get("value")
        if not isinstance(value, dict) or value.get("type") != "knowledge_ref":
            continue
        ref = value.get("value")
        if (type(ref) is not dict or set(ref) != {"block_id", "revision"}
                or type(ref.get("block_id")) is not str or type(ref.get("revision")) is not int or ref["revision"] < 1):
            continue
        target = by_block.get(ref["block_id"])
        current = bool(graph_profile["knowledge_ref"] and target and target["current"].get("revision") == ref["revision"])
        if current:
            destination = ref["block_id"]
            state = "current"
        else:
            # Do not disclose an inaccessible target ID or infer why it is absent.
            destination = edge_id(item["block_id"], "unavailable", "knowledge_reference")
            nodes[destination] = {"id": destination, "label": "Referenced revision unavailable", "name": "Referenced revision unavailable", "kind": "knowledge_reference_unavailable"}
            state = "unavailable_or_historical"
        connection=item.get("connection_metadata")
        source_id=item["block_id"]
        if connection is not None:
            subject=connection["subject"]; subject_item=by_block.get(subject["block_id"])
            source_current=bool(graph_profile["knowledge_ref"] and subject_item and subject_item["current"].get("revision")==subject["revision"])
            if source_current:
                source_id=subject["block_id"]
            else:
                source_id=edge_id(item["block_id"],"unavailable_subject","knowledge_reference")
                nodes[source_id]={"id":source_id,"label":"Referenced revision unavailable","name":"Referenced revision unavailable","kind":"knowledge_reference_unavailable"}
                state="unavailable_or_historical"
        eid = edge_id(source_id, destination, "knowledge_reference:" + item["block_id"] if connection is not None else "knowledge_reference")
        edges[eid] = {"id": eid, "src_id": source_id, "dst_id": destination,
                      "tag": "knowledge_reference", "reference_state": state,
                      "target_revision": ref["revision"] if current else None,
                      "relation_block_id": item["block_id"] if connection is not None else None,
                      "source_revision": connection["subject"]["revision"] if connection is not None and source_current else None}


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

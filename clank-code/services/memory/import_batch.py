"""Durable owner-scoped memory import batches.

The legacy file importer predates the v2 job substrate and records one best-
effort ``memory_file_import`` row after the work has already happened.  This
module gives the UI one parent job and deterministic child identities while
keeping the existing v2 job tables as the only durable job authority.  Source
bytes are staged outside SQLite with owner-derived paths and are never placed
in the job ledger.
"""

from __future__ import annotations

from src.memery_writer_fence import connect_writer

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from src.constants import DATA_DIR, FM_DB_PATH
from src.memery_v2 import _job_tables_ready, seal_job_manifest


BATCH_CONTRACT = "openclank.memory-import-batch/v1"
REVIEW_CONTRACT = "openclank.memory-import-review/v1"
ENTITY_MATCH_CANDIDATE_CONTRACT = "openclank.memory-entity-match-candidate/v1"
MAX_BATCH_FILES = 20
MAX_BATCH_BYTES = 50 * 1024 * 1024
STAGED_SOURCE_TTL_SECONDS = 24 * 60 * 60
_REVIEW_ACTIONS = frozenset({"accept", "reject", "defer"})
_REVIEW_OUTCOMES = frozenset({"accepted", "reused", "rejected", "deferred"})
_TERMINAL_REVIEW_OUTCOMES = frozenset({"accepted", "reused", "rejected"})
_MEMORY_CATEGORIES = frozenset({
    "fact", "contact", "task", "preference", "identity", "project", "goal", "unknown",
})
_PUBLICATION_KINDS = frozenset({"memory", "candidate", "existing"})
_MAX_REVIEW_HISTORY = 20
_MAX_ENTITY_MATCH_CANDIDATES = 8
_MAX_MATCHED_ALIAS_LENGTH = 120
_ASSISTANT_ENTITY_ID_RE = re.compile(r"\Aprincipal_assistant_[0-9a-f]{32}\Z")
_HANDLER_ENTITY_ID_RE = re.compile(r"\Aprincipal_handler_[0-9a-f]{32}\Z")
_SOURCE_EVIDENCE_HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_HANDLER_TEXT_RE = re.compile(r"\A%USER%(?:['’]s\b|\s|—|:|\Z)")
_ASSISTANT_SELF_TEXT_RE = re.compile(
    r"\A(?:I\b|I['’](?:m|ve|d|ll)\b|My\b|Me\b|Mine\b|Myself\b)",
    re.IGNORECASE,
)
_DOCUMENT_ROLES = frozenset({
    "assistant_identity", "handler_profile", "assistant_instructions",
    "mixed_memory", "generic_document",
})
_SUBJECT_ROLES = frozenset({
    "assistant_self", "handler", "named_external", "joint", "instruction", "unknown",
})
_SUBJECT_ATTRIBUTION_METHODS = frozenset({
    "document_profile", "document_policy", "markdown_context",
    "grounded_model", "persona_alias", "unresolved",
})
_ENTITY_MATCH_CANDIDATE_KEYS = frozenset({
    "contract",
    "entity_id",
    "role",
    "matched_alias",
    "match_method",
    "state",
    "requires_review",
})
_SUBJECT_ATTRIBUTION_KEYS = frozenset({
    "contract", "role", "entity_id", "document_role", "method", "state",
    "requires_review", "section", "source_evidence_hash",
})
_STAGING_LOCK = threading.RLock()


class ImportBatchError(RuntimeError):
    """A typed, safe-to-display batch failure."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def item_identity(content: bytes) -> str:
    """Return a filename-independent, content-addressed item identity."""

    return "item_" + hashlib.sha256(content).hexdigest()[:32]


def _owner_path(owner: str) -> str:
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]


def _payload(value: Any, *, code: str = "job_store") -> dict[str, Any]:
    """Decode one store-owned JSON object without accepting arbitrary shapes."""

    try:
        decoded = json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise ImportBatchError(code, "The import job record is unavailable.", retryable=True) from exc
    if not isinstance(decoded, Mapping):
        raise ImportBatchError(code, "The import job record is unavailable.", retryable=True)
    return dict(decoded)


def _json_value(value: Any) -> Any:
    """Return a JSON-only value so review payloads cannot carry live objects."""

    try:
        return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ImportBatchError("invalid_review", "The review payload is invalid.") from exc


def _validated_entity_match_candidates(value: Any) -> list[dict[str, Any]]:
    """Return only bounded, exact server candidate contract values.

    Candidate construction happens after model output is stripped at the route
    boundary.  This second validation keeps malformed or expanded JSON out of
    durable extraction/review records and only admits reserved assistant IDs.
    """

    if not isinstance(value, list) or len(value) > _MAX_ENTITY_MATCH_CANDIDATES:
        return []
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != _ENTITY_MATCH_CANDIDATE_KEYS:
            continue
        entity_id = raw.get("entity_id")
        alias = raw.get("matched_alias")
        if (
            raw.get("contract") != ENTITY_MATCH_CANDIDATE_CONTRACT
            or raw.get("role") != "assistant_self"
            or raw.get("match_method") != "persona_setting_exact"
            or raw.get("state") != "proposed"
            or raw.get("requires_review") is not True
            or not isinstance(entity_id, str)
            or _ASSISTANT_ENTITY_ID_RE.fullmatch(entity_id) is None
            or not isinstance(alias, str)
            or not alias.strip()
            or len(alias) > _MAX_MATCHED_ALIAS_LENGTH
            or any(ord(character) < 32 or ord(character) == 127 for character in alias)
        ):
            continue
        key = (entity_id, alias)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "contract": ENTITY_MATCH_CANDIDATE_CONTRACT,
            "entity_id": entity_id,
            "role": "assistant_self",
            "matched_alias": alias,
            "match_method": "persona_setting_exact",
            "state": "proposed",
            "requires_review": True,
        })
    # Round-trip the fixed schema once so only ordinary bounded JSON reaches
    # the durable proposal fingerprint and review ledger.
    return _json_value(candidates)


def _validated_subject_attribution(value: Any) -> Optional[dict[str, Any]]:
    """Admit one fixed server-owned reserved-principal proposal."""

    if not isinstance(value, Mapping) or set(value) != _SUBJECT_ATTRIBUTION_KEYS:
        return None
    role = str(value.get("role") or "")
    entity_id = str(value.get("entity_id") or "")
    document_role = str(value.get("document_role") or "")
    method = str(value.get("method") or "")
    section = str(value.get("section") or "")
    source_hash = str(value.get("source_evidence_hash") or "")
    valid_id = (
        _ASSISTANT_ENTITY_ID_RE.fullmatch(entity_id) is not None
        if role == "assistant_self"
        else _HANDLER_ENTITY_ID_RE.fullmatch(entity_id) is not None
    )
    if (
        value.get("contract") != "openclank.memory-subject-attribution/v1"
        or role not in {"assistant_self", "handler"}
        or not valid_id
        or document_role not in _DOCUMENT_ROLES
        or method not in _SUBJECT_ATTRIBUTION_METHODS
        or value.get("state") != "proposed"
        or value.get("requires_review") is not True
        or len(section) > 120
        or any(ord(character) < 32 or ord(character) == 127 for character in section)
        or _SOURCE_EVIDENCE_HASH_RE.fullmatch(source_hash) is None
    ):
        return None
    return _json_value({key: value[key] for key in sorted(_SUBJECT_ATTRIBUTION_KEYS)})


def _canonical_question_context(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the scope-free canonical association used for fingerprints.

    The review route re-normalizes a model-supplied context before
    publication, adding the contract, a default mode, and the owner scope.
    Fingerprinting the canonical form keeps that normalization alone from
    reading as an owner edit.  An invalid context stays raw here; the review
    boundary rejects it with typed 422 guidance instead.
    """
    from services.memory.question_context import (
        QuestionContextError,
        normalize_question_context,
    )

    try:
        normalized = normalize_question_context(value)
    except QuestionContextError:
        normalized = None
    if normalized is None:
        return _json_value(dict(value))
    return _json_value(normalized)


_SNAPSHOT_IMPORT_SEAL = object()
_KNOWLEDGE_SNAPSHOT_SEAL = object()
_KNOWLEDGE_SNAPSHOT_PROFILE = "s10.curated-transfer.v1"


def _validated_imported_snapshot(value: Any) -> dict[str, Any]:
    from src.openclank.analysis_snapshot_contract import validate_reference
    if type(value) is not dict or set(value) != {"capture_origin", "reference", "attachment"} or value["capture_origin"] != "imported_unverified":
        raise ImportBatchError("invalid_manifest", "Snapshot review is unavailable.")
    try:
        ref = validate_reference(value["reference"])
    except (TypeError, ValueError) as exc:
        raise ImportBatchError("invalid_manifest", "Snapshot review is unavailable.") from exc
    if value["attachment"] != {"type": "attachment_ref", "attachment_id": ref["upload_id"]}:
        raise ImportBatchError("invalid_manifest", "Snapshot review is unavailable.")
    return _json_value(value)


_MARKDOWN_BUNDLE_FIELDS = frozenset({
    "state", "bundle_sha256", "origin_sha256", "foreign", "omitted_items", "omissions_at_least",
})
_MARKDOWN_ITEM_FIELDS = frozenset({
    "item_sha256", "declared_state", "source_sha256", "source_revision", "source_uri_sha256",
    "source_messages_sha256", "revision_omitted",
})


def _markdown_hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _markdown_count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= (1 << 53) - 1


def _markdown_bundle_valid(value: Mapping[str, Any]) -> bool:
    return (
        value.get("state") == "unverified"
        and _markdown_hash(value.get("bundle_sha256"))
        and _markdown_hash(value.get("origin_sha256"))
        and type(value.get("foreign")) is bool
        and _markdown_count(value.get("omitted_items"))
        and type(value.get("omissions_at_least")) is bool
    )


def _markdown_claim(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != _MARKDOWN_BUNDLE_FIELDS | _MARKDOWN_ITEM_FIELDS:
        return None
    if (
        not _markdown_bundle_valid(value)
        or not _markdown_hash(value.get("item_sha256"))
        or value.get("declared_state") not in ("resolved", "unresolved")
        or any(value[key] is not None and not _markdown_hash(value[key])
               for key in ("source_sha256", "source_uri_sha256", "source_messages_sha256"))
        or (value["source_revision"] is not None and not _markdown_count(value["source_revision"]))
        or type(value["revision_omitted"]) is not bool
        or (value["revision_omitted"] and value["source_revision"] is not None)
    ):
        return None
    return dict(value)


def _initial_proposal(suggestion: Any) -> dict[str, Any]:
    """Return the immutable extraction proposal used to identify one suggestion."""

    if isinstance(suggestion, Mapping):
        text = str(suggestion.get("text") or "").strip()
        category = str(suggestion.get("category") or "fact").strip().lower() or "fact"
        context = suggestion.get("question_context")
        entity_match_candidates = _validated_entity_match_candidates(
            suggestion.get("entity_match_candidates")
        )
        subject_role = str(suggestion.get("subject_role") or "").strip().lower()
        document_role = str(suggestion.get("document_role") or "").strip().lower()
        subject_attribution = _validated_subject_attribution(
            suggestion.get("subject_attribution")
        )
    else:
        text = str(suggestion or "").strip()
        category = "fact"
        context = None
        entity_match_candidates = []
        subject_role = ""
        document_role = ""
        subject_attribution = None
    proposal: dict[str, Any] = {"text": text, "category": category}
    claim = _markdown_claim(suggestion.get("markdown_source")) if isinstance(suggestion, Mapping) else None
    if claim is not None:
        proposal["markdown_source"] = claim
    if isinstance(context, Mapping):
        proposal["question_context"] = _canonical_question_context(context)
    if entity_match_candidates:
        proposal["entity_match_candidates"] = entity_match_candidates
    if subject_role in _SUBJECT_ROLES:
        proposal["subject_role"] = subject_role
    if document_role in _DOCUMENT_ROLES:
        proposal["document_role"] = document_role
    if subject_attribution:
        proposal["subject_attribution"] = subject_attribution
    return proposal


def _normalize_suggestions(
    *,
    batch_id: str,
    item_id: str,
    result: Mapping[str, Any],
    markdown_bundle: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Attach server-derived immutable identities to extracted suggestions.

    The browser can edit proposal fields, but it never chooses a suggestion ID.
    Duplicate model output receives an occurrence discriminator; identical cards
    remain individually auditable without tying identity to browser order.
    """

    normalized = dict(result)
    suggestions = result.get("suggestions")
    if not isinstance(suggestions, list):
        return normalized
    # The 100-fact ceiling is a server admission bound, not a prompt hope: a
    # broken or hostile model cannot stuff unbounded review cards into the
    # durable job ledger.
    suggestions = suggestions[:100]
    occurrences: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    for raw in suggestions:
        item = dict(raw) if isinstance(raw, Mapping) else {"text": str(raw or ""), "category": "fact"}
        # These are store-owned fields. Never accept a model/browser supplied
        # ID, review decision, or proposal hash on an extraction update.
        item.pop("suggestion_id", None)
        item.pop("proposal_fingerprint", None)
        item.pop("review", None)
        item.pop("review_history", None)
        item.pop("snapshot_import", None)
        claim = _markdown_claim(item.pop("markdown_source", None))
        if markdown_bundle is not None:
            text = item.get("text")
            if (
                claim is None
                or any(claim[key] != markdown_bundle[key] for key in _MARKDOWN_BUNDLE_FIELDS)
                or not isinstance(text, str)
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != claim["item_sha256"]
            ):
                raise ImportBatchError("invalid_manifest", "The Markdown import provenance changed.")
            item["markdown_source"] = claim
        attribution = _validated_subject_attribution(
            item.pop("subject_attribution", None)
        )
        # Model/browser IDs never survive. Only the fixed server attribution
        # created at the route boundary is reattached after validation.
        item.pop("subject_entity_id", None)
        item.pop("handler_entity_id", None)
        item.pop("assistant_entity_id", None)
        if attribution:
            item["subject_attribution"] = attribution
        candidates = _validated_entity_match_candidates(
            item.pop("entity_match_candidates", None)
        )
        if candidates:
            item["entity_match_candidates"] = candidates
        proposal = _initial_proposal(item)
        proposal_fingerprint = _digest(proposal)
        occurrence = occurrences.get(proposal_fingerprint, 0)
        occurrences[proposal_fingerprint] = occurrence + 1
        item["suggestion_id"] = "suggestion_" + _digest({
            "contract": REVIEW_CONTRACT,
            "batch_id": batch_id,
            "item_id": item_id,
            "proposal": proposal,
            "occurrence": occurrence,
        })[:32]
        item["proposal_fingerprint"] = proposal_fingerprint
        items.append(item)
    normalized["suggestions"] = items
    return normalized


def _review_proposal(
    proposal: Optional[Mapping[str, Any]],
    *,
    extracted: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the owner-reviewed proposal that will be published elsewhere."""

    source = _initial_proposal(extracted)
    raw = source if proposal is None else proposal
    if not isinstance(raw, Mapping):
        raise ImportBatchError("invalid_review", "The reviewed memory proposal is invalid.")
    text = str(raw.get("text") or "").strip()
    if not text or len(text) > 5000:
        raise ImportBatchError("invalid_review", "A reviewed memory must contain 1 to 5000 characters.")
    category = str(raw.get("category") or "fact").strip().lower() or "fact"
    if category not in _MEMORY_CATEGORIES:
        raise ImportBatchError("invalid_review", "The reviewed memory category is invalid.")
    value: dict[str, Any] = {"text": text, "category": category}
    if "markdown_source" in source:
        value["markdown_source"] = source["markdown_source"]
    if "question_context" in raw and raw.get("question_context") is not None:
        if not isinstance(raw.get("question_context"), Mapping):
            raise ImportBatchError("invalid_review", "The reviewed question association is invalid.")
        value["question_context"] = _json_value(dict(raw["question_context"]))
    # Entity IDs are server-owned.  A browser may edit the prose/category, but
    # it may neither add, remove, nor replace the candidate preserved on the
    # immutable extracted suggestion.
    candidates = _validated_entity_match_candidates(
        source.get("entity_match_candidates")
    )
    if candidates:
        value["entity_match_candidates"] = candidates
    attribution = _validated_subject_attribution(
        source.get("subject_attribution")
    )
    if attribution:
        if attribution["role"] == "handler" and _HANDLER_TEXT_RE.match(text) is None:
            raise ImportBatchError(
                "subject_attribution_conflict",
                "A Handler-associated memory must keep the %USER% subject token.",
            )
        if (
            attribution["role"] == "assistant_self"
            and _ASSISTANT_SELF_TEXT_RE.match(text) is None
        ):
            raise ImportBatchError(
                "subject_attribution_conflict",
                "An assistant-self memory must keep explicit first-person wording.",
            )
        value["subject_attribution"] = attribution
        value["subject_role"] = attribution["role"]
        value["document_role"] = attribution["document_role"]
    else:
        subject_role = str(source.get("subject_role") or "")
        document_role = str(source.get("document_role") or "")
        if subject_role in _SUBJECT_ROLES:
            value["subject_role"] = subject_role
        if document_role in _DOCUMENT_ROLES:
            value["document_role"] = document_role
    return value


def _review_key_hash(value: Any) -> str:
    key = str(value or "").strip()
    if not key or len(key) > 512 or any(ord(char) < 32 for char in key):
        raise ImportBatchError("invalid_review_key", "The review idempotency key is invalid.")
    # Review keys can be client-generated secrets. Retain only a digest.
    return _digest({"review_key": key})


def _publication(value: Optional[str], kind: Optional[str]) -> Optional[dict[str, str]]:
    if value is None and kind is None:
        return None
    publication_id = str(value or "").strip()
    publication_kind = str(kind or "").strip().lower()
    if (
        not publication_id
        or len(publication_id) > 255
        or any(ord(char) < 32 for char in publication_id)
        or publication_kind not in _PUBLICATION_KINDS
    ):
        raise ImportBatchError("invalid_publication", "The published Memory reference is invalid.")
    return {"id": publication_id, "kind": publication_kind}


def _review_problem(error: Mapping[str, Any]) -> dict[str, Any]:
    code = str(error.get("code") or "MEMORY_REVIEW_PUBLISH_FAILED").strip()[:120]
    message = str(error.get("message") or "The reviewed memory could not be saved.").strip()[:500]
    if not code or not message or any(ord(char) < 32 for char in code):
        raise ImportBatchError("invalid_review", "The review failure is invalid.")
    return {"code": code, "message": message, "retryable": bool(error.get("retryable", True))}


class MemoryImportBatchStore:
    """Small repository adapter for parent/child import jobs."""

    def __init__(self, db_path: str = FM_DB_PATH, data_dir: str = DATA_DIR) -> None:
        self.db_path = str(db_path)
        self.data_dir = Path(data_dir)
        self._original_account_scope = None
        self._account_lifecycle = None
        self._staging_root_identity = None

    def scoped_for_original(self, *, account_id, authority, current, lifecycle=None):
        """Create an independent view bound to a real current target and guard."""
        from src.openclank.memory_job_admission import TargetAuthority
        if type(authority) is not TargetAuthority or not callable(current) or current() is not True:
            raise ImportBatchError('import_unavailable','The import account is unavailable.')
        try:
            binding=authority.resolve(account_id)
            if binding.source_account_id != account_id or current() is not True:
                raise ValueError('import account changed')
        except Exception as exc:
            raise ImportBatchError('import_unavailable','The import account is unavailable.') from exc
        view=type(self)(db_path=self.db_path,data_dir=self.data_dir)
        view._original_account_scope=(account_id,authority,binding,current)
        view._account_lifecycle=lifecycle
        return view

    def scoped_for_human(self, *, context):
        """Bind a separate factual read view to the existing named-human guard."""
        from src.memory_scope import memory_owner
        if type(context) is not dict or not callable(context.get('current')):
            raise ImportBatchError('import_unavailable','The import account is unavailable.')
        state=context.get('state'); user=context.get('user'); owner=context.get('owner')
        account=context.get('account'); current=context['current']; auth=getattr(state,'auth_manager',None)
        try:
            if (auth is None or not user or not account or owner != memory_owner(user)
                or auth.account_id(user)!=account or auth.username_for_account_id(account)!=user or current() is not True):
                raise ValueError('import account changed')
        except Exception as exc:
            raise ImportBatchError('import_unavailable','The import account is unavailable.') from exc
        view=type(self)(db_path=self.db_path,data_dir=self.data_dir)
        view._original_account_scope=(account,None,owner,current)
        view._account_lifecycle=getattr(state,'account_owner_lifecycle',None)
        return view

    def _connect(self) -> sqlite3.Connection:
        conn = connect_writer(self.db_path, timeout=30)
        # SQLite resolves this protected trigger function even for unrelated
        # job kinds. NULL is deny under its exact IS NOT capability predicate.
        try:
            conn.create_function("fm_policy_projection_capability", 0, lambda: None, deterministic=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except BaseException:
            conn.close()
            raise

    @staticmethod
    def _children_locked(
        conn: sqlite3.Connection, *, owner: str, batch_id: str
    ) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT job_id,state,result_json,created_at,updated_at,idempotency_key FROM fm_v2_jobs "
            "WHERE owner_id=? AND kind='memory_import_item' "
            "AND json_extract(result_json,'$.batch_id')=? ORDER BY created_at,job_id",
            (owner, batch_id),
        ).fetchall()

    @staticmethod
    def _latest_children(rows: Sequence[sqlite3.Row]) -> list[tuple[sqlite3.Row, dict[str, Any]]]:
        """Keep only the current child for each source item in parent accounting."""

        latest: dict[str, tuple[tuple[int, str, str], sqlite3.Row, dict[str, Any]]] = {}
        for row in rows:
            item = _payload(row["result_json"])
            root_item_id = str(item.get("root_item_id") or item.get("source_id") or row["job_id"])
            try:
                retry_number = int(item.get("retry_number") or 0)
            except (TypeError, ValueError):
                retry_number = 0
            rank = (retry_number, str(row["updated_at"] or ""), str(row["job_id"]))
            existing = latest.get(root_item_id)
            if existing is None or rank > existing[0]:
                latest[root_item_id] = (rank, row, item)
        return [(entry[1], entry[2]) for entry in latest.values()]

    @staticmethod
    def _review_counts(latest: Sequence[tuple[sqlite3.Row, Mapping[str, Any]]]) -> dict[str, int]:
        counts = {
            "total": 0,
            "awaiting_review": 0,
            "publishing": 0,
            "accepted": 0,
            "reused": 0,
            "rejected": 0,
            "deferred": 0,
            "edited": 0,
            "pending": 0,
        }
        for _, item in latest:
            result = item.get("result")
            suggestions = result.get("suggestions") if isinstance(result, Mapping) else None
            if not isinstance(suggestions, list):
                continue
            for suggestion in suggestions:
                if not isinstance(suggestion, Mapping):
                    continue
                counts["total"] += 1
                review = suggestion.get("review")
                state = str(review.get("state") or "awaiting_review") if isinstance(review, Mapping) else "awaiting_review"
                if state not in {"awaiting_review", "publishing", *_REVIEW_OUTCOMES}:
                    state = "awaiting_review"
                counts[state] += 1
                if isinstance(review, Mapping) and review.get("edited"):
                    counts["edited"] += 1
        counts["pending"] = (
            counts["awaiting_review"] + counts["publishing"] + counts["deferred"]
        )
        return counts

    @staticmethod
    def _parent_counts(latest: Sequence[tuple[sqlite3.Row, Mapping[str, Any]]]) -> dict[str, int]:
        completed = [entry for entry in latest if entry[0]["state"] in {"succeeded", "awaiting_review"}]
        failed = [entry for entry in latest if entry[0]["state"] in {"failed_terminal", "cancelled"}]
        pending = [entry for entry in latest if entry[0]["state"] in {"queued", "active", "retry_wait"}]
        return {
            "selected": len(latest),
            "completed": len(completed),
            "reused": sum(
                1
                for _, item in completed
                if isinstance(item.get("result"), Mapping)
                and item["result"].get("outcome") == "reused"
            ),
            "failed": len(failed),
            "pending": len(pending),
        }

    @staticmethod
    def _desired_parent_state(
        latest: Sequence[tuple[sqlite3.Row, Mapping[str, Any]]],
        review_counts: Mapping[str, int],
    ) -> str:
        states = [str(row["state"]) for row, _ in latest]
        if any(state in {"queued", "active", "retry_wait"} for state in states):
            return "active"
        if int(review_counts.get("pending", 0)):
            return "awaiting_review"
        if any(state in {"failed_terminal", "cancelled"} for state in states):
            # Keep even an all-failed batch actionable.  A child failure is
            # immutable under the v2 trigger, but the user must still be able
            # to create a replacement child from its staged source bytes.
            # Marking the parent failed_terminal would make that legitimate
            # retry impossible without inventing a second parent authority.
            return "awaiting_review"
        return "succeeded"

    def _refresh_parent_locked(
        self,
        conn: sqlite3.Connection,
        *,
        owner: str,
        batch_id: str,
        now: str,
        clear_dismissed: bool = False,
    ) -> tuple[dict[str, Any], list[sqlite3.Row], list[tuple[sqlite3.Row, dict[str, Any]]]]:
        parent = conn.execute(
            "SELECT state,result_json FROM fm_v2_jobs WHERE owner_id=? AND job_id=? "
            "AND kind='memory_import_batch'",
            (owner, batch_id),
        ).fetchone()
        if not parent:
            raise ImportBatchError("batch_not_found", "The import batch is not available.")
        children = self._children_locked(conn, owner=owner, batch_id=batch_id)
        latest = self._latest_children(children)
        review_counts = self._review_counts(latest)
        desired_state = self._desired_parent_state(latest, review_counts)
        current_state = str(parent["state"])
        # The v2 trigger deliberately forbids terminal-job resurrection. A
        # caller must use begin_item_retry(), which moves an actionable parent
        # through awaiting_review -> queued -> active before arriving here.
        if current_state != desired_state and not (
            (current_state == "queued" and desired_state in {"active", "cancelled"})
            or
            (current_state == "active" and desired_state in {"awaiting_review", "succeeded", "failed_terminal", "cancelled"})
            or (current_state == "awaiting_review" and desired_state in {"awaiting_review", "succeeded", "cancelled"})
        ):
            raise ImportBatchError("invalid_batch_state", "The import batch cannot accept that update.")
        payload = _payload(parent["result_json"])
        if clear_dismissed:
            # A newly fenced review decision is owner re-engagement, so it
            # un-dismisses the batch; replayed decisions return before this
            # refresh and keep the marker.
            payload.pop("dismissed_at", None)
        payload["state"] = desired_state
        payload["counts"] = self._parent_counts(latest)
        payload["review_counts"] = review_counts
        payload["updated_at"] = now
        conn.execute(
            "UPDATE fm_v2_jobs SET state=?,result_json=?,updated_at=? WHERE owner_id=? AND job_id=?",
            (desired_state, json.dumps(payload, sort_keys=True), now, owner, batch_id),
        )
        return payload, children, latest

    def _stage_root(self, owner: str, batch_id: str) -> Path:
        return self.data_dir / "memory_import_staging" / _owner_path(owner) / batch_id

    def _assert_import_not_erased(self, conn, owner: str, *, job_id=None, idempotency_key=None) -> None:
        """Use the existing owner's transaction; marker presence only denies."""
        from src.openclank.memory_job_admission import _deny_import_erasure_marker
        try:
            version=conn.execute('PRAGMA user_version').fetchone()[0]
            identity=conn.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
            if version not in (16,17,18) or identity is None or type(identity[0]) is not str or not identity[0]:
                raise ValueError('import database unavailable')
            # Missing outbox in a partial or unknown store is unavailable.
            conn.execute('SELECT owner_id,event_id FROM fm_v2_outbox LIMIT 0')
            captured_owners = None
            scope=self._original_account_scope
            if version in (17,18) and scope is None:  # 18 is the guarded successor of 17: same account-scope rule
                raise ValueError('import account authority unavailable')
            if version in (17,18) and scope is not None:
                account,authority,binding,current=scope
                if (not conn.in_transaction or current() is not True
                    or (authority is None and binding != owner)
                    or (authority is not None and (binding.memory_owner != owner or authority.resolve(account) != binding))):
                    raise ValueError('import account changed')
                from src.openclank.owner_custody_facts import verified_owner_custody
                captured_owners,_has_head=verified_owner_custody(conn,database_id=identity[0],
                    account_id=account,current_owner=owner)
                if current() is not True or (authority is not None and authority.resolve(account) != binding):
                    raise ValueError('import account changed')
            _deny_import_erasure_marker(conn,owner=owner,job_id=job_id,idempotency_key=idempotency_key,
                captured_owners=captured_owners)
            if job_id is not None:
                stored=conn.execute('SELECT idempotency_key FROM fm_v2_jobs WHERE owner_id=? AND job_id=?',
                                    (owner,job_id)).fetchone()
                if stored is not None and stored[0] is not None:
                    _deny_import_erasure_marker(conn,owner=owner,idempotency_key=stored[0],captured_owners=captured_owners)
        except (sqlite3.Error, ValueError, TypeError, IndexError) as exc:
            raise ImportBatchError('import_unavailable', 'The import source is unavailable.') from exc

    @staticmethod
    def _validate_staged_identity(
        owner: str, batch_id: str, item_id: Optional[str] = None
    ) -> None:
        # Fullmatch, not prefix: staging identities are server-derived digests
        # and must never carry path separators or traversal segments.
        if (
            not owner
            or not batch_id.startswith("batch_")
            or re.fullmatch(r"[0-9a-f_]{1,128}", batch_id[6:]) is None
            or (
                item_id is not None
                and (
                    not item_id.startswith("item_")
                    or re.fullmatch(r"[0-9a-f_]{1,128}", item_id[5:]) is None
                )
            )
        ):
            raise ImportBatchError("invalid_identity", "The import source identity is invalid.")

    def stage_bytes(self, owner: str, batch_id: str, item_id: str, content: bytes) -> Path:
        self._validate_staged_identity(owner, batch_id, item_id)
        with _STAGING_LOCK, self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._assert_import_not_erased(conn,owner,job_id=batch_id)
            self._assert_import_not_erased(conn,owner,job_id=item_id)
            if self._original_account_scope is not None:
                path=self._write_scoped_staged(conn,owner=owner,batch_id=batch_id,item_id=item_id,content=content)
                conn.commit()
                return path
            root = self._stage_root(owner, batch_id)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            try: os.chmod(root, 0o700)
            except OSError: pass
            path = root / f"{item_id}.bin"
            path.write_bytes(content)
            try: os.chmod(path, 0o600)
            except OSError: pass
            conn.commit()
        return path

    def _write_scoped_staged(self, conn, *, owner, batch_id, item_id, content):
        if (os.name!='posix' or os.open not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd
            or any(not hasattr(os,name) for name in ('O_DIRECTORY','O_NOFOLLOW','O_NONBLOCK'))):
            raise ImportBatchError('staging_unavailable','The import staging platform is unavailable.')
        try:
            self.data_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
            current=self._scoped_staging_binding(conn,owner=owner,batch_id=batch_id)
            row=conn.execute("SELECT result_json FROM fm_v2_jobs WHERE owner_id=? AND job_id=? AND kind='memory_import_item' AND json_extract(result_json,'$.batch_id')=?",(owner,item_id,batch_id)).fetchone()
            payload=_payload(row['result_json']) if row else {}
            if (type(content) is not bytes or payload.get('staged_item_id')!=item_id
                or payload.get('byte_size')!=len(content) or payload.get('content_hash')!=hashlib.sha256(content).hexdigest()
                or current() is not True): raise ValueError('staged file identity changed')
            flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
            def child(parent,name):
                try: os.mkdir(name,0o700,dir_fd=parent)
                except FileExistsError: pass
                return os.open(name,flags,dir_fd=parent)
            data_fd=os.open(self._staging_root_identity[0],flags)
            try:
                root_stat=os.fstat(data_fd)
                if (root_stat.st_dev,root_stat.st_ino)!=self._staging_root_identity[1:]: raise ValueError('staging root changed')
                stage_fd=child(data_fd,'memory_import_staging')
                try:
                    owner_fd=child(stage_fd,_owner_path(owner))
                    try:
                        batch_fd=child(owner_fd,batch_id)
                        try:
                            name=item_id+'.bin'
                            try:
                                fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=batch_fd)
                            except FileExistsError:
                                if self._read_scoped_staged(conn,owner=owner,batch_id=batch_id,item_id=item_id,payload=payload)!=content:
                                    raise ValueError('staged file changed')
                            else:
                                try:
                                    remaining=memoryview(content)
                                    while remaining:
                                        written=os.write(fd,remaining)
                                        if written<=0: raise ValueError('staged file changed')
                                        remaining=remaining[written:]
                                    held=os.fstat(fd);named=os.stat(name,dir_fd=batch_fd,follow_symlinks=False)
                                    if (not stat.S_ISREG(held.st_mode) or (held.st_dev,held.st_ino,held.st_size)!=(named.st_dev,named.st_ino,named.st_size)
                                        or held.st_size!=len(content) or current() is not True): raise ValueError('staged file changed')
                                finally: os.close(fd)
                        finally: os.close(batch_fd)
                    finally: os.close(owner_fd)
                finally: os.close(stage_fd)
            finally: os.close(data_fd)
            if current() is not True: raise ValueError('staging authority changed')
            return self._stage_root(owner,batch_id)/(item_id+'.bin')
        except (OSError,ValueError,TypeError,KeyError,sqlite3.Error) as exc:
            raise ImportBatchError('staging_unavailable','The import staging source is unavailable.') from exc

    def _scoped_staging_binding(self, conn, *, owner, batch_id):
        from src.openclank.import_staging_custody import verified_staging_relocations
        account,authority,binding,current=self._original_account_scope
        def still_current():
            try:
                return (current() is True and ((authority is None and binding==owner)
                    or (authority is not None and binding.memory_owner==owner and authority.resolve(account)==binding)))
            except Exception:
                return False
        database=conn.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
        root=os.path.realpath(os.path.abspath(os.fspath(self.data_dir)))
        observed=os.stat(root,follow_symlinks=False)
        if database is None or not stat.S_ISDIR(observed.st_mode):
            raise ValueError('staging root unavailable')
        identity=(root,observed.st_dev,observed.st_ino)
        if self._staging_root_identity is None:
            self._staging_root_identity=identity
        elif self._staging_root_identity != identity:
            raise ValueError('staging root changed')
        owners,_moves=verified_staging_relocations(conn,database_id=database[0],account_id=account,
            current_owner=owner,lifecycle=self._account_lifecycle,current=still_current)
        parent=conn.execute("SELECT input_hash,result_json,workspace_key,project_key FROM fm_v2_jobs WHERE owner_id=? AND job_id=? AND kind='memory_import_batch'",(owner,batch_id)).fetchone()
        if parent is None or parent['project_key'] != '': raise ValueError('original batch unavailable')
        record=_payload(parent['result_json']); items=record.get('items')
        if type(items) is not list or not items or type(record.get('workspace_id')) is not str or record['workspace_id'] != parent['workspace_key']:
            raise ValueError('original batch unavailable')
        inputs=[]
        for item in items:
            if type(item) is not dict or not all(k in item for k in ('source_id','byte_size','content_hash','extension','document_role')):
                raise ValueError('original batch unavailable')
            inputs.append({'item_id':item['source_id'],'byte_size':item['byte_size'],
                'content_hash':item['content_hash'],'extension':item['extension'],'document_role':item['document_role']})
        matches=[]
        for captured in owners:
            descriptor={'contract':BATCH_CONTRACT,'owner':captured,'workspace_id':record['workspace_id'],
                'session_id':record.get('session_id') or '', 'items':inputs}
            if record.get('knowledge_snapshot_profile'):
                if record['knowledge_snapshot_profile'] != _KNOWLEDGE_SNAPSHOT_PROFILE: raise ValueError('original transfer unavailable')
                descriptor['knowledge_snapshot_profile'] = _KNOWLEDGE_SNAPSHOT_PROFILE
                if type(record.get('knowledge_snapshot')) is not dict: raise ValueError('original transfer unavailable')
                descriptor['knowledge_snapshot'] = record['knowledge_snapshot']
            if record.get('identity_context_hash'): descriptor['identity_context_hash']=record['identity_context_hash']
            if record.get('action_hash') is not None: descriptor['action_hash']=record['action_hash']
            digest=_digest(descriptor)
            if digest==parent['input_hash'] and batch_id=='batch_'+digest[:32]: matches.append(captured)
        if len(matches)!=1: raise ValueError('original batch owner unavailable')
        return still_current

    def _read_scoped_staged(self, conn, *, owner, batch_id, item_id, payload):
        """Verify account lineage, original batch and exact nofollow file."""
        if not all(hasattr(os,name) for name in ('O_DIRECTORY','O_NOFOLLOW','O_NONBLOCK')):
            raise ImportBatchError('staged_source_missing','Staged import source is unavailable.')
        try:
            still_current=self._scoped_staging_binding(conn,owner=owner,batch_id=batch_id)
            staged_id=payload.get('staged_item_id') or item_id
            self._validate_staged_identity(owner,batch_id,staged_id)
            expected_hash=payload.get('content_hash'); expected_size=payload.get('byte_size')
            if (type(expected_hash) is not str or re.fullmatch(r'[0-9a-f]{64}',expected_hash) is None
                or type(expected_size) is not int or not 0<=expected_size<=MAX_BATCH_BYTES):
                raise ValueError('staged file identity unavailable')
            directory_flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
            data_fd=os.open(self._staging_root_identity[0],directory_flags)
            try:
                root_stat=os.fstat(data_fd)
                if (root_stat.st_dev,root_stat.st_ino)!=self._staging_root_identity[1:]: raise ValueError('staging root changed')
                stage_fd=os.open('memory_import_staging',directory_flags,dir_fd=data_fd)
                try:
                    owner_fd=os.open(_owner_path(owner),directory_flags,dir_fd=stage_fd)
                    try:
                        batch_fd=os.open(batch_id,directory_flags,dir_fd=owner_fd)
                        try:
                            name=staged_id+'.bin'
                            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=batch_fd)
                            try:
                                before=os.fstat(fd); named=os.stat(name,dir_fd=batch_fd,follow_symlinks=False)
                                signature=lambda v:(v.st_dev,v.st_ino,v.st_size,v.st_mtime_ns,v.st_ctime_ns)
                                if (not stat.S_ISREG(before.st_mode) or signature(before)!=signature(named)
                                    or before.st_size!=expected_size or time.time()-before.st_mtime>STAGED_SOURCE_TTL_SECONDS):
                                    raise ValueError('staged file changed')
                                chunks=[]; remaining=expected_size
                                while remaining:
                                    chunk=os.read(fd,min(remaining,1024*1024))
                                    if not chunk: raise ValueError('staged file changed')
                                    chunks.append(chunk); remaining-=len(chunk)
                                if os.read(fd,1): raise ValueError('staged file changed')
                                after=os.fstat(fd); named_after=os.stat(name,dir_fd=batch_fd,follow_symlinks=False)
                                content=b''.join(chunks)
                                if (signature(before)!=signature(after) or signature(after)!=signature(named_after)
                                    or hashlib.sha256(content).hexdigest()!=expected_hash or still_current() is not True):
                                    raise ValueError('staged file changed')
                                return content
                            finally: os.close(fd)
                        finally: os.close(batch_fd)
                    finally: os.close(owner_fd)
                finally: os.close(stage_fd)
            finally: os.close(data_fd)
        except (OSError,ValueError,TypeError,KeyError,sqlite3.Error) as exc:
            raise ImportBatchError('staged_source_missing','Staged import source is unavailable.') from exc

    def read_staged(self, owner: str, batch_id: str, item_id: str, *, _knowledge_seal: Any = None) -> bytes:
        self._validate_staged_identity(owner, batch_id, item_id)
        with _STAGING_LOCK, self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._assert_import_not_erased(conn,owner,job_id=batch_id)
            self._assert_import_not_erased(conn,owner,job_id=item_id)
            row = conn.execute("SELECT result_json FROM fm_v2_jobs WHERE owner_id=? AND job_id=? "
                "AND kind='memory_import_item' AND json_extract(result_json,'$.batch_id')=?",
                (owner,item_id,batch_id)).fetchone()
            payload = _payload(row['result_json']) if row else {}
            if payload.get('knowledge_snapshot_profile') and _knowledge_seal is not _KNOWLEDGE_SNAPSHOT_SEAL:
                raise ImportBatchError('staged_source_unavailable','Transfer intake is private.')
            if _knowledge_seal is not None and (_knowledge_seal is not _KNOWLEDGE_SNAPSHOT_SEAL or payload.get('knowledge_snapshot_profile') != _KNOWLEDGE_SNAPSHOT_PROFILE):
                raise ImportBatchError('staged_source_unavailable','Transfer intake unavailable.')
            for key in ('root_item_id','staged_item_id'):
                linked=payload.get(key)
                if linked:
                    self._validate_staged_identity(owner,batch_id,linked)
                    self._assert_import_not_erased(conn,owner,job_id=linked)
            if self._original_account_scope is not None:
                content=self._read_scoped_staged(conn,owner=owner,batch_id=batch_id,item_id=item_id,payload=payload)
                conn.commit()
                return content
            path = self._stage_root(owner,batch_id) / f"{item_id}.bin"
            if not path.exists() and payload.get('staged_item_id'):
                path = self._stage_root(owner,batch_id) / f"{payload['staged_item_id']}.bin"
            try: file_stat = path.stat()
            except FileNotFoundError as exc:
                raise ImportBatchError('staged_source_missing','This import item is no longer available; upload it again.',retryable=False) from exc
            if time.time() - file_stat.st_mtime > STAGED_SOURCE_TTL_SECONDS:
                path.unlink(missing_ok=True)
                raise ImportBatchError('staged_source_expired','This staged import item expired; upload it again.',retryable=False)
            content=path.read_bytes()
            conn.commit()
            return content

    def _owner_staging_snapshot_unlocked(self, owner: str) -> dict[str, Any]:
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise ImportBatchError(
                "scope_required", "An authenticated Memory scope is required."
            )
        parent = (self.data_dir / "memory_import_staging").resolve()
        root = parent / _owner_path(owner_key)
        if root.is_symlink():
            raise ImportBatchError(
                "staging_path_invalid", "The import staging path is invalid."
            )
        if not root.exists():
            entries: list[dict[str, Any]] = []
        else:
            entries = []
            for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
                if path.is_symlink():
                    raise ImportBatchError(
                        "staging_path_invalid", "The import staging path is invalid."
                    )
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise ImportBatchError(
                        "staging_path_invalid", "The import staging path is invalid."
                    )
                data = path.read_bytes()
                entries.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
        return {
            "count": len(entries),
            "bytes": sum(int(row["size"]) for row in entries),
            "fingerprint": "sha256:" + _digest(entries),
        }

    def _selective_staging_snapshot_unlocked(self, owner: str, *, retained: list, erasable: list) -> dict[str, Any]:
        """Bounded physical observation only; the caller must establish reset authority."""
        if type(owner) is not str or not owner or owner != owner.strip():
            raise ImportBatchError('staging_unavailable', 'The import staging inventory is unavailable.')
        keys = {'workspace_id','batch_id','staged_item_id','source_id_sha256','content_sha256','byte_size','job_selectors'}
        seen = set(); groups = []; total = 0
        for disposition, values in (('retained',retained),('erasable',erasable)):
            if type(values) is not list or len(values) > 128:
                raise ImportBatchError('staging_unavailable', 'The import staging inventory is unavailable.')
            ordered = []
            for value in values:
                if type(value) is not dict or set(value) != keys:
                    raise ImportBatchError('staging_unavailable', 'The import staging inventory is unavailable.')
                workspace,batch,item = (value[k] for k in ('workspace_id','batch_id','staged_item_id'))
                if any(type(v) is not str or not v or v != v.strip() or len(v.encode('utf-8')) > 512 for v in (workspace,batch,item)):
                    raise ImportBatchError('staging_unavailable', 'The import staging inventory is unavailable.')
                self._validate_staged_identity(owner,batch,item)
                jobs=value['job_selectors']; size=value['byte_size']; digest=value['content_sha256']; source=value['source_id_sha256']
                if (type(jobs) is not list or not jobs or len(jobs)>512 or jobs != sorted(set(jobs))
                    or any(type(j) is not str or re.fullmatch(r'[0-9a-f]{64}',j) is None for j in jobs)
                    or type(source) is not str or re.fullmatch(r'[0-9a-f]{64}',source) is None
                    or type(size) is not int or not 0 <= size <= MAX_BATCH_BYTES
                    or type(digest) is not str or re.fullmatch(r'[0-9a-f]{64}',digest) is None):
                    raise ImportBatchError('staging_unavailable', 'The import staging inventory is unavailable.')
                key=(workspace,batch,item); physical=(batch,item)
                if physical in seen:
                    raise ImportBatchError('staging_unavailable', 'The import staging inventory is ambiguous.')
                seen.add(physical); ordered.append(key); total += size
                groups.append((disposition,value))
            if ordered != sorted(ordered):
                raise ImportBatchError('staging_unavailable', 'The import staging inventory is unavailable.')
        if len(groups)>128 or total>128*1024*1024:
            raise ImportBatchError('staging_unavailable', 'The import staging inventory is too large.')
        # The classifier promises whole-batch retention. Refuse a mixed batch
        # even if a malformed classification gives distinct item IDs.
        retained_batches={v['batch_id'] for v in retained}
        if any(v['batch_id'] in retained_batches for v in erasable):
            raise ImportBatchError('staging_unavailable', 'The import staging inventory is ambiguous.')
        if (os.name != 'posix' or os.open not in os.supports_dir_fd
            or any(not hasattr(os, name) for name in ('O_DIRECTORY','O_NOFOLLOW','O_NONBLOCK'))):
            raise ImportBatchError('staging_unavailable', 'The import staging platform is unavailable.')
        directory_flags=os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        # A FIFO must not block before fstat can reject it as nonregular.
        file_flags=os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        def open_dir(name, parent):
            return os.open(name,directory_flags,dir_fd=parent)
        rows=[]; observed_files=set()
        try:
            data_fd=os.open(os.fspath(self.data_dir),directory_flags)
            try:
                try:
                    staging_fd=open_dir('memory_import_staging',data_fd)
                except FileNotFoundError:
                    staging_fd=None
                try:
                    try:
                        owner_fd=open_dir(_owner_path(owner),staging_fd) if staging_fd is not None else None
                    except FileNotFoundError:
                        owner_fd=None
                    try:
                        for disposition,value in groups:
                            row={'disposition':disposition,'workspace_id':value['workspace_id'],
                                 'batch_id':value['batch_id'],'staged_item_id':value['staged_item_id'],
                                 'state':'absent'}
                            try:
                                batch_fd=open_dir(value['batch_id'],owner_fd) if owner_fd is not None else None
                            except FileNotFoundError:
                                batch_fd=None
                            try:
                                try:
                                    fd=os.open(value['staged_item_id']+'.bin',file_flags,dir_fd=batch_fd) if batch_fd is not None else None
                                except FileNotFoundError:
                                    fd=None
                                if fd is not None:
                                    try:
                                        before=os.fstat(fd)
                                        if not stat.S_ISREG(before.st_mode) or before.st_size != value['byte_size']:
                                            raise ImportBatchError('staging_unavailable','The staged import changed.')
                                        file_identity=(before.st_dev,before.st_ino)
                                        if file_identity in observed_files:
                                            raise ImportBatchError('staging_unavailable','The import staging inventory is ambiguous.')
                                        observed_files.add(file_identity)
                                        hasher=hashlib.sha256(); remaining=before.st_size
                                        while remaining:
                                            block=os.read(fd,min(1024*1024,remaining))
                                            if not block: raise ImportBatchError('staging_unavailable','The staged import changed.')
                                            hasher.update(block); remaining-=len(block)
                                        after=os.fstat(fd)
                                        if ((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
                                            != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)
                                            or hasher.hexdigest()!=value['content_sha256']):
                                            raise ImportBatchError('staging_unavailable','The staged import changed.')
                                        row.update({'state':'present','device':before.st_dev,'inode':before.st_ino,
                                                    'size':before.st_size,'mtime_ns':before.st_mtime_ns,
                                                    'ctime_ns':before.st_ctime_ns,'sha256':hasher.hexdigest()})
                                    finally: os.close(fd)
                            finally:
                                if batch_fd is not None: os.close(batch_fd)
                            rows.append(row)
                    finally:
                        if owner_fd is not None: os.close(owner_fd)
                finally:
                    if staging_fd is not None: os.close(staging_fd)
            finally: os.close(data_fd)
        except OSError as exc:
            raise ImportBatchError('staging_unavailable','The import staging inventory is unavailable.') from exc
        root=os.path.realpath(os.path.join(os.fspath(self.data_dir),'memory_import_staging',_owner_path(owner)))
        return {'version':1,'root_sha256':hashlib.sha256(os.fsencode(root)).hexdigest(),'entries':rows}

    def _cleanup_selective_staging(self, *, claim, provider, barrier, witness) -> dict[str, Any]:
        """Reconcile exact selected staging only after original Native receipts."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from services.memory.skill_lifecycle import locked
        from src.openclank.account_request_barrier import AccountRequestBarrier, _ResetDrainWitness
        from src.openclank.fm_reset_receipt import read_committed_reset_receipt
        if (type(claim) is not MemoryResetClaim or type(barrier) is not AccountRequestBarrier
            or type(witness) is not _ResetDrainWitness or witness.owner != claim.owner
            or barrier is not getattr(claim._coordinator._reset_state,'account_request_barrier',None)):
            raise ImportBatchError('staging_unavailable','The import staging authority is unavailable.')
        coordinator=claim._coordinator
        def load_original():
            with locked(os.fspath(coordinator.lock_target)):
                row=coordinator._load_journal().get(claim.operation_id)
                if type(row) is not dict: raise ValueError('reset unavailable')
                manifest=row.get('reset_selective_manifest'); snapshot=row.get('reset_selective_staging')
                frozen=row.get('reset_native_frozen')
                if (type(manifest) is not dict or type(snapshot) is not dict or type(frozen) is not dict
                    or row.get('reset_native_started') is not True or row.get('reset_native_request') != frozen.get('request')
                    or row.get('reset_selective_manifest_sha256') != _digest(manifest)
                    or row.get('reset_selective_staging_sha256') != _digest(snapshot)
                    or manifest.get('account_id') != claim.account_id or manifest.get('operation_id') != claim.operation_id
                    or frozen.get('request',{}).get('selective_manifest_sha256') != _digest(manifest)):
                    raise ValueError('reset unavailable')
                return json.loads(json.dumps([manifest,snapshot,frozen],sort_keys=True,ensure_ascii=False,allow_nan=False))
        original=load_original(); manifest,snapshot,frozen=original
        def current():
            try:
                return (barrier.reset_witness_current(witness) is True
                    and claim.assert_current_post_dispatch(provider=provider,frozen=frozen) is True
                    and load_original()==original)
            except Exception:
                return False
        if current() is not True:
            raise ImportBatchError('staging_unavailable','The import staging authority changed.')
        try:
            receipt=read_committed_reset_receipt(provider._guarded_preparation,frozen,current=current)
            if receipt.get('state') != 'committed' or receipt.get('sql_result',{}).get('complete') is not True:
                raise ValueError('receipt unavailable')
            partition=manifest['partition']; imports=partition['imports']; request=frozen['request']
            if (type(imports) is not dict or imports.get('version') != 2
                or imports.get('owner') != request['owner'] or imports.get('account_id') != claim.account_id
                or type(imports.get('digest')) is not str or re.fullmatch(r'[0-9a-f]{64}',imports['digest']) is None
                or type(imports.get('job_bindings')) is not list or type(imports.get('erasable_job_selectors')) is not list):
                raise ValueError('partition unavailable')
            bindings={b['job_selector']:b for b in imports['job_bindings'] if type(b) is dict and type(b.get('job_selector')) is str}
            selected=imports['erasable_job_selectors']
            if (len(bindings)!=len(imports['job_bindings']) or selected != sorted(set(selected))
                or any(type(x) is not str or re.fullmatch(r'[0-9a-f]{64}',x) is None or x not in bindings for x in selected)):
                raise ValueError('partition unavailable')
            overall=request['expected_counts']['ingest']['selective']['partition_sha256']
            database=frozen['database']
            if (partition.get('binding_sha256') != overall or type(database) is not dict
                or os.path.realpath(self.db_path) != database.get('path')
                or os.path.abspath(self.db_path) != database.get('path')
                or os.path.realpath(os.fspath(self.data_dir)) != os.path.dirname(database.get('path') or '')):
                raise ValueError('reset database changed')
            def database_current():
                observed=os.stat(self.db_path,follow_symlinks=False)
                return (stat.S_ISREG(observed.st_mode)
                    and (observed.st_dev,observed.st_ino)==(database.get('device'),database.get('inode')))
            if not database_current(): raise ValueError('reset database changed')
            reset_id='cs_reset_'+_digest([claim.account_id,claim.operation_id])
            def unique(pairs):
                result={}
                for key,value in pairs:
                    if key in result: raise ValueError('duplicate marker')
                    result[key]=value
                return result
            with _STAGING_LOCK, self._connect() as conn:
                conn.execute('BEGIN')
                db_id=conn.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
                db_version=conn.execute('PRAGMA user_version').fetchone()
                if (current() is not True or not database_current() or db_id is None
                    or db_id[0] != database.get('database_id') or db_version is None
                    or db_version[0] != database.get('schema_version')):
                    raise ValueError('reset database changed')
                from src.openclank.import_staging_custody import verified_staging_relocations
                captured_owners,_moves=verified_staging_relocations(conn,database_id=db_id[0],account_id=claim.account_id,
                    current_owner=claim.owner,lifecycle=getattr(coordinator._reset_state,'account_owner_lifecycle',None),
                    current=current)
                # The frozen reset actor and both event identities stay captured;
                # only the SQL owner namespace moved under the verified chain.
                captured=request['owner']
                if captured not in captured_owners:
                    raise ValueError('reset custody unavailable')
                reset_row=conn.execute("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=? AND reason='s06.guarded-reset.v1' AND length(CAST(operation_metadata_json AS BLOB))<=262144",(claim.owner,reset_id)).fetchone()
                if (reset_row is None or reset_row['actor_type']!='user' or reset_row['actor_id']!=captured
                    or reset_row['contract_version']!='frankenmemory.v2' or reset_row['source_event_id'] is not None
                    or hashlib.sha256(reset_row['operation_metadata_json'].encode('utf-8')).hexdigest()!=reset_row['content_hash']):
                    raise ValueError('reset receipt unavailable')
                reset=json.loads(reset_row['operation_metadata_json'],object_pairs_hook=unique,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid reset receipt')))
                if (type(reset) is not dict or set(reset)!={'version','request_sha256','request','result'}
                    or reset['version']!='s06.guarded-reset.v1' or reset['request']!=request
                    or reset['request_sha256']!=_digest(request) or reset['result']!=receipt['sql_result']
                    or reset['result'].get('complete') is not True
                    or reset['result']['categories']['ingest']['state']!='complete'
                    or reset['result']['categories']['ingest']['selective']['partition_sha256']!=overall):
                    raise ValueError('reset receipt unavailable')
                reset_event=conn.execute("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind='s06_guarded_reset'",(claim.owner,'outbox_'+reset_id+'_s06_guarded_reset',reset_id)).fetchone()
                if reset_event is None or reset_event['source_revision']!=0 or reset_event['payload_json']!=reset_row['operation_metadata_json']:
                    raise ValueError('reset receipt unavailable')
                # Both independently keyed Native denial events must be the
                # exact commitments for every original selected import job.
                for selector in selected:
                    binding=bindings[selector]
                    expected={'version':'s04.import-erased.v2','owner':captured,
                        'workspace_id':binding['workspace_id'],'reset_workspace_id':request['workspace_id'],
                        'account_id':claim.account_id,'job_selector':selector,'operation_id':claim.operation_id,
                        'partition_sha256':overall,'import_partition_sha256':imports['digest'],
                        'reset_receipt_id':reset_id,'idempotency_sha256':binding['idempotency_sha256'],
                        'job_binding_sha256':_digest(binding)}
                    for event,kind in (('outbox_s04_import_erased_'+_digest([captured,selector]),'s04_import_erased'),
                                       ('outbox_s04_import_key_erased_'+binding['idempotency_sha256'],'s04_import_key_erased')):
                        row=conn.execute('SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind=?',
                            (claim.owner,event,reset_id,kind)).fetchone()
                        if (row is None or row['source_revision'] != 0
                            or json.loads(row['payload_json'],object_pairs_hook=unique,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid terminal marker'))) != expected):
                            raise ValueError('terminal marker unavailable')
                retained=imports['retained_staging']; erasable=imports['erasable_staging']
                if any(any(job not in selected for job in group['job_selectors']) for group in erasable):
                    raise ValueError('staging selection unavailable')
                observed=self._selective_staging_snapshot_unlocked(claim.owner,retained=retained,erasable=erasable)
                initial=snapshot.get('entries'); now=observed['entries']
                groups=[('retained',value) for value in retained]+[('erasable',value) for value in erasable]
                if (set(snapshot) != {'version','root_sha256','entries'} or type(snapshot.get('version')) is not int
                    or snapshot['version'] != 1 or snapshot.get('root_sha256') != observed.get('root_sha256')
                    or type(initial) is not list or len(initial)!=len(now)
                    or len(initial)!=len(groups)):
                    raise ValueError('staging changed')
                for entry,(disposition,group) in zip(initial,groups):
                    base={'disposition','workspace_id','batch_id','staged_item_id','state'}
                    present=base|{'device','inode','size','mtime_ns','ctime_ns','sha256'}
                    if (type(entry) is not dict or entry.get('disposition') != disposition
                        or any(entry.get(key)!=group.get(key) for key in ('workspace_id','batch_id','staged_item_id'))
                        or (entry.get('state')=='absent' and set(entry)!=base)
                        or (entry.get('state')=='present' and (set(entry)!=present
                            or any(type(entry.get(k)) is not int or entry[k]<0 for k in ('device','inode','size','mtime_ns','ctime_ns'))
                            or entry.get('size')!=group.get('byte_size') or entry.get('sha256')!=group.get('content_sha256')))
                        or entry.get('state') not in ('absent','present')):
                        raise ValueError('staging changed')
                removed=0; count_unavailable=False
                for before,after in zip(initial,now):
                    if before == after: continue
                    base=('disposition','workspace_id','batch_id','staged_item_id')
                    if (before.get('disposition') != 'erasable' or after.get('state') != 'absent'
                        or any(before.get(k)!=after.get(k) for k in base)):
                        raise ValueError('staging changed')
                    count_unavailable=True
                if current() is not True: raise ValueError('authority changed')
                count_unavailable = count_unavailable or any(
                    row['disposition']=='erasable' and row['state']=='absent' for row in now)
                if any(row['disposition']=='erasable' and row['state']=='present' for row in now):
                    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
                    data_fd=os.open(os.fspath(self.data_dir),flags)
                    try:
                        stage_fd=os.open('memory_import_staging',flags,dir_fd=data_fd)
                        try:
                            owner_fd=os.open(_owner_path(claim.owner),flags,dir_fd=stage_fd)
                            try:
                                for before,after in zip(initial,now):
                                    if after['disposition'] != 'erasable' or after['state'] != 'present':
                                        if after['disposition']=='erasable': count_unavailable=True
                                        continue
                                    batch_fd=os.open(after['batch_id'],flags,dir_fd=owner_fd)
                                    try:
                                        name=after['staged_item_id']+'.bin'
                                        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=batch_fd)
                                        try:
                                            held=os.fstat(fd); named=os.stat(name,dir_fd=batch_fd,follow_symlinks=False)
                                            wanted=(after['device'],after['inode'],after['size'],after['mtime_ns'],after['ctime_ns'])
                                            actual=lambda st:(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
                                            if not stat.S_ISREG(held.st_mode) or actual(held)!=wanted or actual(named)!=wanted:
                                                raise ValueError('staging changed')
                                            os.unlink(name,dir_fd=batch_fd); removed+=1
                                        finally: os.close(fd)
                                    finally: os.close(batch_fd)
                            finally: os.close(owner_fd)
                        finally: os.close(stage_fd)
                    finally: os.close(data_fd)
                final=self._selective_staging_snapshot_unlocked(claim.owner,retained=retained,erasable=erasable)
                if (final.get('root_sha256') != snapshot.get('root_sha256')
                    or any(a['disposition']=='retained' and a!=b for a,b in zip(initial,final['entries']))
                    or any(a['disposition']=='erasable' and b['state']!='absent' for a,b in zip(initial,final['entries']))
                    or current() is not True or not database_current()):
                    raise ValueError('staging postcondition unavailable')
                conn.commit()
                return {'complete':True,'count':removed,**({'count_unavailable':True} if count_unavailable else {})}
        except (ValueError,KeyError,TypeError,sqlite3.Error,OSError,RecursionError) as exc:
            raise ImportBatchError('staging_unavailable','The selective staging cleanup is unavailable.') from exc

    def _guarded_staging_inventory_fd(self, root_fd):
        """Hash a held tree without following links; return content and inode evidence."""
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
        entries=[]; identities=[]; seen=set()
        def signature(value):
            return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)
        def visit(directory, prefix):
            before=os.fstat(directory)
            for name in sorted(os.listdir(directory)):
                if not name or name in ('.','..') or '/' in name or '\x00' in name:
                    raise ValueError('invalid staging entry')
                named=os.stat(name,dir_fd=directory,follow_symlinks=False)
                path=prefix+name
                if stat.S_ISDIR(named.st_mode):
                    child=os.open(name,flags,dir_fd=directory)
                    try:
                        if signature(os.fstat(child))!=signature(named): raise ValueError('staging directory changed')
                        identities.append((path,signature(named)))
                        visit(child,path+'/')
                    finally: os.close(child)
                elif stat.S_ISREG(named.st_mode):
                    fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
                    try:
                        opened=os.fstat(fd)
                        if signature(opened)!=signature(named) or (opened.st_dev,opened.st_ino) in seen:
                            raise ValueError('staging file changed')
                        seen.add((opened.st_dev,opened.st_ino)); digest=hashlib.sha256(); remaining=opened.st_size
                        while remaining:
                            data=os.read(fd,min(remaining,1024*1024))
                            if not data: raise ValueError('staging file changed')
                            digest.update(data);remaining-=len(data)
                        if os.read(fd,1) or signature(os.fstat(fd))!=signature(opened): raise ValueError('staging file changed')
                        if signature(os.stat(name,dir_fd=directory,follow_symlinks=False))!=signature(opened): raise ValueError('staging file changed')
                        entries.append({'path':path,'size':opened.st_size,'sha256':digest.hexdigest()})
                        identities.append((path,signature(opened)))
                    finally: os.close(fd)
                else:
                    raise ValueError('unsupported staging entry')
            if signature(os.fstat(directory))!=signature(before): raise ValueError('staging directory changed')
        visit(root_fd,'')
        entries.sort(key=lambda item:item['path']);identities.sort(key=lambda item:item[0])
        return {'count':len(entries),'bytes':sum(item['size'] for item in entries),'fingerprint':'sha256:'+_digest(entries)},tuple(identities)

    def _guarded_owner_staging_snapshot_unlocked(self, owner):
        if os.name!='posix' or os.open not in os.supports_dir_fd or not all(hasattr(os,k) for k in ('O_DIRECTORY','O_NOFOLLOW','O_NONBLOCK')):
            raise ImportBatchError('staging_unavailable','The import staging platform is unavailable.')
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
        try:
            data_fd=os.open(os.path.realpath(os.fspath(self.data_dir)),flags)
            try:
                try: parent_fd=os.open('memory_import_staging',flags,dir_fd=data_fd)
                except FileNotFoundError: return {'count':0,'bytes':0,'fingerprint':'sha256:'+_digest([])}
                try:
                    try: root_fd=os.open(_owner_path(owner),flags,dir_fd=parent_fd)
                    except FileNotFoundError: return {'count':0,'bytes':0,'fingerprint':'sha256:'+_digest([])}
                    try: return self._guarded_staging_inventory_fd(root_fd)[0]
                    finally: os.close(root_fd)
                finally: os.close(parent_fd)
            finally: os.close(data_fd)
        except (OSError,ValueError,TypeError) as exc:
            raise ImportBatchError('staging_unavailable','The import staging inventory is unavailable.') from exc

    def _guarded_rename_owner_staging(self, old_owner, new_owner, *, expected_source, expected_target):
        from core.atomic_io import rename_directory_noreplace
        if (os.name!='posix' or os.open not in os.supports_dir_fd or os.rmdir not in os.supports_dir_fd
            or not all(hasattr(os,k) for k in ('O_DIRECTORY','O_NOFOLLOW','O_NONBLOCK'))):
            raise ImportBatchError('staging_unavailable','The import staging platform is unavailable.')
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
        empty={'count':0,'bytes':0,'fingerprint':'sha256:'+_digest([])}
        if type(expected_source) is not dict or type(expected_target) is not dict:
            raise ImportBatchError('staging_unavailable','The original staging inventory is unavailable.')
        try:
            root_path=os.path.realpath(os.fspath(self.data_dir))
            data_fd=os.open(root_path,flags)
            try:
                data_identity=os.fstat(data_fd)
                def roots_current(parent, expected):
                    named_data=os.stat(root_path,follow_symlinks=False)
                    named_parent=os.stat('memory_import_staging',dir_fd=data_fd,follow_symlinks=False)
                    held_parent=os.fstat(parent)
                    return ((named_data.st_dev,named_data.st_ino)==(data_identity.st_dev,data_identity.st_ino)
                        and (named_parent.st_dev,named_parent.st_ino)==(expected.st_dev,expected.st_ino)
                        and (held_parent.st_dev,held_parent.st_ino)==(expected.st_dev,expected.st_ino))
                try: parent=os.open('memory_import_staging',flags,dir_fd=data_fd)
                except FileNotFoundError:
                    if expected_source==empty and expected_target==empty:return {**empty,'complete':True,'already_applied':True}
                    raise ValueError('staging parent unavailable')
                try:
                    before_parent=os.fstat(parent)
                    src_name=_owner_path(old_owner);dst_name=_owner_path(new_owner)
                    def opened(name):
                        try:return os.open(name,flags,dir_fd=parent)
                        except FileNotFoundError:return None
                    src=opened(src_name);dst=opened(dst_name)
                    try:
                        source,source_ids=self._guarded_staging_inventory_fd(src) if src is not None else (empty,())
                        target,_target_ids=self._guarded_staging_inventory_fd(dst) if dst is not None else (empty,())
                        if not source['count']:
                            if target==expected_source and expected_target==empty:
                                return {**target,'complete':True,'already_applied':True}
                            if source==expected_source and target==expected_target and source==empty and target==empty:
                                return {**target,'complete':True,'already_applied':True}
                            raise ValueError('original staging move unavailable')
                        if source!=expected_source or target!=expected_target or target!=empty:
                            raise ValueError('original staging inventory changed')
                        if dst is not None:
                            named=os.stat(dst_name,dir_fd=parent,follow_symlinks=False);held=os.fstat(dst)
                            if (named.st_dev,named.st_ino)!=(held.st_dev,held.st_ino):raise ValueError('staging target changed')
                            os.rmdir(dst_name,dir_fd=parent);os.close(dst);dst=None
                        named=os.stat(src_name,dir_fd=parent,follow_symlinks=False);held=os.fstat(src)
                        if (named.st_dev,named.st_ino)!=(held.st_dev,held.st_ino) or not stat.S_ISDIR(named.st_mode):
                            raise ValueError('staging source changed')
                        if not roots_current(parent,before_parent): raise ValueError('staging parent changed')
                        rename_directory_noreplace(parent,src_name,parent,dst_name)
                        moved=os.open(dst_name,flags,dir_fd=parent)
                        try:
                            after=os.fstat(moved)
                            if (after.st_dev,after.st_ino)!=(held.st_dev,held.st_ino): raise ValueError('staging move changed')
                            observed,observed_ids=self._guarded_staging_inventory_fd(moved)
                            if observed!=source or observed_ids!=source_ids: raise ValueError('staging move changed')
                            end=os.fstat(parent)
                            if ((end.st_dev,end.st_ino)!=(before_parent.st_dev,before_parent.st_ino)
                                or not roots_current(parent,before_parent)): raise ValueError('staging parent changed')
                            return {**observed,'complete':True,'already_applied':False}
                        finally:os.close(moved)
                    finally:
                        if src is not None:os.close(src)
                        if dst is not None:os.close(dst)
                finally:os.close(parent)
            finally:os.close(data_fd)
        except (OSError,ValueError,TypeError) as exc:
            raise ImportBatchError('staging_unavailable','The import staging move is unavailable.') from exc

    def preview_owner_staging(self, owner: str, *, guarded: bool = False) -> dict[str, Any]:
        """Fingerprint exact owner-scoped staged source bytes for nuke CAS."""
        with _STAGING_LOCK:
            return (self._guarded_owner_staging_snapshot_unlocked(owner) if guarded
                else self._owner_staging_snapshot_unlocked(owner))

    def rename_owner_staging(
        self,
        old_owner: str,
        new_owner: str,
        *,
        expected_source: Optional[Mapping[str, Any]] = None,
        expected_target: Optional[Mapping[str, Any]] = None,
        guarded: bool = False,
    ) -> dict[str, Any]:
        """Atomically move owner-staged imports with crash-safe reconciliation."""

        old_owner = str(old_owner or "").strip().lower()
        new_owner = str(new_owner or "").strip().lower()
        if not old_owner or not new_owner or "\x00" in old_owner or "\x00" in new_owner:
            raise ImportBatchError(
                "scope_required", "An authenticated Memory scope is required."
            )
        with _STAGING_LOCK:
            if guarded:
                return self._guarded_rename_owner_staging(old_owner,new_owner,
                    expected_source=expected_source,expected_target=expected_target)
            source = self._owner_staging_snapshot_unlocked(old_owner)
            target = self._owner_staging_snapshot_unlocked(new_owner)
            if old_owner == new_owner:
                return {**source, "complete": True, "already_applied": True}
            if int(source["count"]) and int(target["count"]):
                raise ImportBatchError(
                    "staging_owner_conflict",
                    "Both import owners contain staged source bytes.",
                    retryable=True,
                )
            if expected_source is not None and int(source["count"]):
                if dict(expected_source) != source:
                    raise ImportBatchError(
                        "stale_staging_preview",
                        "The import staging migration preview is stale.",
                        retryable=True,
                    )
            if expected_target is not None and int(target["count"]):
                target_matches_source = (
                    expected_source is not None and dict(expected_source) == target
                )
                if dict(expected_target) != target and not target_matches_source:
                    raise ImportBatchError(
                        "stale_staging_preview",
                        "The target import staging preview is stale.",
                        retryable=True,
                    )
            if not int(source["count"]):
                if int(target["count"]):
                    return {**target, "complete": True, "already_applied": True}
                if expected_source is not None and int(expected_source.get("count") or 0):
                    raise ImportBatchError(
                        "staging_move_incomplete",
                        "The staged import sources disappeared during owner migration.",
                        retryable=True,
                    )
                return {**target, "complete": True, "already_applied": True}

            parent = self.data_dir / "memory_import_staging"
            source_root = parent / _owner_path(old_owner)
            target_root = parent / _owner_path(new_owner)
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target_root.exists():
                if target_root.is_symlink() or not target_root.is_dir():
                    raise ImportBatchError(
                        "staging_path_invalid", "The import staging path is invalid."
                    )
                if any(target_root.iterdir()):
                    raise ImportBatchError(
                        "staging_owner_conflict",
                        "The target import owner already has staged source bytes.",
                        retryable=True,
                    )
                target_root.rmdir()
            try:
                os.replace(source_root, target_root)
            except OSError as exc:
                raise ImportBatchError(
                    "staging_owner_move_failed",
                    "The staged import sources could not be moved.",
                    retryable=True,
                ) from exc
            after_source = self._owner_staging_snapshot_unlocked(old_owner)
            after_target = self._owner_staging_snapshot_unlocked(new_owner)
            if int(after_source["count"]) or after_target != source:
                raise ImportBatchError(
                    "staging_move_incomplete",
                    "The staged import owner migration did not converge.",
                    retryable=True,
                )
            return {**after_target, "complete": True, "already_applied": False}

    def _guarded_purge_owner_staging(self, owner, *, expected):
        """Delete only the witnessed tree through held nofollow directory FDs."""
        if (os.name!='posix' or os.open not in os.supports_dir_fd or os.unlink not in os.supports_dir_fd
            or os.rmdir not in os.supports_dir_fd or not all(hasattr(os,k) for k in ('O_DIRECTORY','O_NOFOLLOW','O_NONBLOCK'))):
            raise ImportBatchError('staging_unavailable','The import staging platform is unavailable.')
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
        empty={'count':0,'bytes':0,'fingerprint':'sha256:'+_digest([])}
        if type(expected) is not dict or set(expected)!={'count','bytes','fingerprint'}:
            raise ImportBatchError('staging_unavailable','The original staging inventory is unavailable.')
        try:
            root_path=os.path.realpath(os.fspath(self.data_dir));data_fd=os.open(root_path,flags)
            try:
                data_stat=os.fstat(data_fd)
                try: parent=os.open('memory_import_staging',flags,dir_fd=data_fd)
                except FileNotFoundError:
                    return {'complete':True,'count':0,**({'count_unavailable':True} if expected!=empty else {})}
                try:
                    parent_stat=os.fstat(parent);name=_owner_path(owner)
                    try: root=os.open(name,flags,dir_fd=parent)
                    except FileNotFoundError:
                        return {'complete':True,'count':0,**({'count_unavailable':True} if expected!=empty else {})}
                    try:
                        root_stat=os.fstat(root);observed,identities=self._guarded_staging_inventory_fd(root)
                        if observed!=expected:
                            if observed==empty:return {'complete':True,'count':0,'count_unavailable':True}
                            raise ValueError('original staging inventory changed')
                        expected_ids=dict(identities)
                        def sig(value): return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)
                        def remove(directory,prefix):
                            for entry in sorted(os.listdir(directory)):
                                path=prefix+entry; prior=expected_ids.get(path)
                                if prior is None:raise ValueError('staging inventory changed')
                                named=os.stat(entry,dir_fd=directory,follow_symlinks=False)
                                if stat.S_ISDIR(named.st_mode):
                                    child=os.open(entry,flags,dir_fd=directory)
                                    try:
                                        held=os.fstat(child)
                                        if sig(held)!=prior or sig(named)!=prior:raise ValueError('staging directory changed')
                                        remove(child,path+'/')
                                        named_after=os.stat(entry,dir_fd=directory,follow_symlinks=False)
                                        if (named_after.st_dev,named_after.st_ino)!=(held.st_dev,held.st_ino):raise ValueError('staging directory changed')
                                    finally:os.close(child)
                                    os.rmdir(entry,dir_fd=directory)
                                elif stat.S_ISREG(named.st_mode):
                                    if sig(named)!=prior:raise ValueError('staging file changed')
                                    os.unlink(entry,dir_fd=directory)
                                else:raise ValueError('staging entry changed')
                        remove(root,'')
                        named=os.stat(name,dir_fd=parent,follow_symlinks=False)
                        if (named.st_dev,named.st_ino)!=(root_stat.st_dev,root_stat.st_ino):raise ValueError('staging root changed')
                    finally:os.close(root)
                    os.rmdir(name,dir_fd=parent)
                    named_parent=os.stat('memory_import_staging',dir_fd=data_fd,follow_symlinks=False)
                    named_data=os.stat(root_path,follow_symlinks=False)
                    if ((named_parent.st_dev,named_parent.st_ino)!=(parent_stat.st_dev,parent_stat.st_ino)
                        or (named_data.st_dev,named_data.st_ino)!=(data_stat.st_dev,data_stat.st_ino)):
                        raise ValueError('staging parent changed')
                    return {'complete':True,'count':observed['count']}
                finally:os.close(parent)
            finally:os.close(data_fd)
        except (OSError,ValueError,TypeError) as exc:
            raise ImportBatchError('staging_unavailable','The import staging purge is unavailable.') from exc

    def purge_owner_staging(
        self, owner: str, *, expected: Mapping[str, Any], guarded: bool = False
    ) -> dict[str, Any]:
        """Delete only the previewed owner's staged import bytes."""
        with _STAGING_LOCK:
            if guarded:
                return self._guarded_purge_owner_staging(owner,expected=expected)
            current = self._owner_staging_snapshot_unlocked(owner)
            if dict(expected or {}) != current and int(current["count"]) != 0:
                raise ImportBatchError(
                    "stale_staging_preview",
                    "The import staging reset preview is stale.",
                    retryable=True,
                )
            if int(current["count"]) == 0:
                return {"complete": True, "count": 0}
            root = self.data_dir / "memory_import_staging" / _owner_path(owner)
            if root.exists():
                paths = sorted(
                    root.rglob("*"),
                    key=lambda value: len(value.parts),
                    reverse=True,
                )
                for path in paths:
                    if path.is_symlink():
                        raise ImportBatchError(
                            "staging_path_invalid",
                            "The import staging path is invalid.",
                        )
                    if path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                    else:
                        raise ImportBatchError(
                            "staging_path_invalid",
                            "The import staging path is invalid.",
                        )
                root.rmdir()
            after = self._owner_staging_snapshot_unlocked(owner)
            if int(after["count"]) != 0:
                raise ImportBatchError(
                    "staging_purge_incomplete",
                    "Some staged import sources remain after reset.",
                    retryable=True,
                )
            return {"complete": True, "count": int(current["count"])}

    def create(
        self,
        *,
        owner: str,
        workspace_id: str,
        session_id: Optional[str],
        items: Sequence[Mapping[str, Any]],
        identity_context_hash: Optional[str] = None,
        action_key: Optional[str] = None,
        _knowledge_seal: Any = None,
        _knowledge_descriptor: Optional[Mapping[str, Any]] = None,
        _lookup_only: bool = False,
    ) -> Optional[dict[str, Any]]:
        if (_knowledge_seal is None) != (_knowledge_descriptor is None):
            raise ImportBatchError("invalid_manifest", "Transfer import unavailable.")
        if _knowledge_seal is not None and _knowledge_seal is not _KNOWLEDGE_SNAPSHOT_SEAL:
            raise ImportBatchError("invalid_manifest", "Transfer import unavailable.")
        owner = str(owner or "").strip()
        workspace_id = str(workspace_id or "").strip()
        if not owner or not workspace_id:
            raise ImportBatchError("scope_required", "An authenticated Memory scope is required.")
        if not items or len(items) > MAX_BATCH_FILES:
            raise ImportBatchError(
                "batch_file_limit",
                f"Choose between 1 and {MAX_BATCH_FILES} files per import.",
            )
        normalized: list[dict[str, Any]] = []
        total = 0
        seen: set[str] = set()
        for raw in items:
            item_id = str(raw.get("item_id") or "").strip()
            content_hash = str(raw.get("content_hash") or "").strip()
            filename = str(raw.get("filename") or "upload").strip()[:255]
            document_role = str(raw.get("document_role") or "generic_document").strip().lower()
            try:
                size = int(raw.get("byte_size") or 0)
            except (TypeError, ValueError) as exc:
                raise ImportBatchError("invalid_manifest", "The import manifest is invalid.") from exc
            if (
                not item_id
                or not content_hash
                or size < 0
                or document_role not in _DOCUMENT_ROLES
            ):
                raise ImportBatchError("invalid_manifest", "The import manifest is invalid.")
            if item_id in seen:
                continue
            seen.add(item_id)
            total += size
            normalized.append(
                {
                    "item_id": item_id,
                    "filename": filename or "upload",
                    "byte_size": size,
                    "content_hash": content_hash,
                    "extension": str(raw.get("extension") or "").lower(),
                    "document_role": document_role,
                }
            )
        if total > MAX_BATCH_BYTES:
            raise ImportBatchError(
                "batch_byte_limit",
                f"The selected files exceed the {MAX_BATCH_BYTES // (1024 * 1024)} MB batch limit.",
            )
        identity_context_hash = str(identity_context_hash or "").strip().lower()
        if identity_context_hash and re.fullmatch(r"[0-9a-f]{64}", identity_context_hash) is None:
            raise ImportBatchError(
                "invalid_manifest",
                "The import identity context is invalid.",
            )
        input_descriptor = {
            "contract": BATCH_CONTRACT,
            "owner": owner,
            "workspace_id": workspace_id,
            "session_id": session_id or "",
            # Display filenames are provenance only. Their *validated semantic
            # document role* is extractor input, however: identical bytes used
            # as USER.md and notes.md must not replay different subject rules.
            # Renames within one role remain content-idempotent.
            "items": [
                {
                    "item_id": item["item_id"],
                    "byte_size": item["byte_size"],
                    "content_hash": item["content_hash"],
                    "extension": item["extension"],
                    "document_role": item["document_role"],
                }
                for item in normalized
            ],
        }
        if _knowledge_seal is _KNOWLEDGE_SNAPSHOT_SEAL:
            if self._original_account_scope is None or len(normalized) != 1 or not action_key or not identity_context_hash:
                raise ImportBatchError("invalid_manifest", "Transfer import unavailable.")
            input_descriptor["knowledge_snapshot_profile"] = _KNOWLEDGE_SNAPSHOT_PROFILE
            input_descriptor["knowledge_snapshot"] = _json_value(_knowledge_descriptor)
        if identity_context_hash:
            input_descriptor["identity_context_hash"] = identity_context_hash
        action_hash = None
        if action_key is not None:
            try:
                if type(action_key) is not str or str(uuid.UUID(action_key)) != action_key:
                    raise ValueError('noncanonical action')
            except (ValueError, AttributeError, TypeError):
                raise ImportBatchError('invalid_action', 'The import action identity is invalid.') from None
            action_hash = _digest(['s04.import-action.v1',owner,action_key])
            input_descriptor['action_hash'] = action_hash
        input_hash = _digest(input_descriptor)
        batch_id = "batch_" + input_hash[:32]
        idempotency_key = ('memory-import-action:' + action_hash if action_hash is not None
                           else 'memory-import-batch:' + input_hash)
        # Job IDs are globally unique within an owner, so scope the stable
        # content identity by the batch while keeping it independent of the
        # client filename. Repeated content in one batch is one reusable item.
        for item in normalized:
            item["source_id"] = item["item_id"]
            item["item_id"] = (
                "item_" + input_hash[:16] + "_" + item["content_hash"][:16]
                + "_" + _digest(item["source_id"])[:8]
            )
            item["root_item_id"] = item["item_id"]
            item["retry_number"] = 0
            item["staged_item_id"] = item["item_id"]
        now = _now()
        try:
            with self._connect() as conn:
                if not _job_tables_ready(conn):
                    raise ImportBatchError(
                        "job_substrate_unavailable",
                        "The Memory import job substrate is unavailable; try again shortly.",
                        retryable=True,
                    )
                conn.execute("BEGIN IMMEDIATE")
                self._assert_import_not_erased(conn,owner,job_id=batch_id,idempotency_key=idempotency_key)
                for item in normalized:
                    child_key = f"{idempotency_key}:{item['source_id']}:{item['item_id']}"
                    self._assert_import_not_erased(conn,owner,job_id=item['item_id'],idempotency_key=child_key)
                candidates=[]
                owners=(owner,)
                if self._original_account_scope is not None:
                    account,authority,binding,current=self._original_account_scope
                    if (current() is not True or (authority is None and binding!=owner)
                        or (authority is not None and (binding.memory_owner!=owner or authority.resolve(account)!=binding))):
                        raise ValueError('import account changed')
                    from src.openclank.owner_custody_facts import verified_owner_custody
                    database=conn.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
                    if database is None: raise ValueError('import database unavailable')
                    owners,_has_head=verified_owner_custody(conn,database_id=database[0],account_id=account,current_owner=owner)
                from src.openclank.memory_job_admission import _deny_import_erasure_marker
                for captured in owners:
                    original_descriptor=dict(input_descriptor); original_descriptor['owner']=captured
                    original_action=_digest(['s04.import-action.v1',captured,action_key]) if action_key is not None else None
                    if original_action is not None: original_descriptor['action_hash']=original_action
                    original_hash=_digest(original_descriptor)
                    original_id='batch_'+original_hash[:32]
                    original_key=('memory-import-action:'+original_action if original_action is not None else 'memory-import-batch:'+original_hash)
                    _deny_import_erasure_marker(conn,owner=owner,job_id=original_id,idempotency_key=original_key,
                        captured_owners=(captured,))
                    row=conn.execute("SELECT job_id,result_json,state,input_hash,kind,workspace_key,project_key FROM fm_v2_jobs WHERE owner_id=? AND idempotency_key=?",(owner,original_key)).fetchone()
                    if row is not None:
                        if (row[0]!=original_id or row[3]!=original_hash or row[4]!='memory_import_batch'
                            or row[5]!=workspace_id or row[6]!=''):
                            raise ImportBatchError('batch_conflict','This import action has different content.')
                        candidates.append(row)
                if len(candidates)>1: raise ImportBatchError('batch_conflict','The original import identity is ambiguous.')
                existing=candidates[0] if candidates else None
                if self._original_account_scope is not None and (current() is not True or
                    (authority is not None and authority.resolve(account)!=binding)):
                    raise ValueError('import account changed')
                if existing:
                    self._assert_import_not_erased(conn,owner=owner,job_id=existing[0])
                    conn.commit()
                    payload = json.loads(existing[1] or "{}")
                    payload.setdefault("batch_id", existing[0])
                    payload.setdefault("state", existing[2])
                    payload["replayed"] = True
                    return payload
                if _lookup_only:
                    conn.rollback()
                    return None
                parent_payload = {
                    "contract": BATCH_CONTRACT,
                    **({"knowledge_snapshot_profile": _KNOWLEDGE_SNAPSHOT_PROFILE, "knowledge_snapshot": _json_value(_knowledge_descriptor)} if _knowledge_seal is _KNOWLEDGE_SNAPSHOT_SEAL else {}),
                    "batch_id": batch_id,
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "identity_context_hash": identity_context_hash or None,
                    **({'action_hash': action_hash} if action_hash is not None else {}),
                    "items": normalized,
                    "counts": {
                        "selected": len(normalized),
                        "completed": 0,
                        "reused": 0,
                        "failed": 0,
                        "pending": len(normalized),
                    },
                    "review_counts": {
                        "total": 0,
                        "awaiting_review": 0,
                        "publishing": 0,
                        "accepted": 0,
                        "reused": 0,
                        "rejected": 0,
                        "deferred": 0,
                        "edited": 0,
                        "pending": 0,
                    },
                }
                if _knowledge_seal is _KNOWLEDGE_SNAPSHOT_SEAL and len(json.dumps(parent_payload,sort_keys=True).encode("utf-8")) > 768*1024:
                    raise ImportBatchError("invalid_manifest", "Transfer intake unavailable.")
                conn.execute(
                    "INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,current_attempt_id,attempt_count,input_hash,result_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,NULL,0,?,?,?,?)",
                    (
                        owner,
                        batch_id,
                        "memory_import_batch",
                        workspace_id,
                        "",
                        idempotency_key,
                        "queued",
                        input_hash,
                        json.dumps(parent_payload, sort_keys=True),
                        now,
                        now,
                    ),
                )
                for item in normalized:
                    child_id = item["item_id"]
                    # The same bytes may be submitted from two distinct source
                    # files; their child jobs remain separate even when the
                    # extraction result can be reused by content hash.
                    child_key = f"{idempotency_key}:{item['source_id']}:{child_id}"
                    child_payload = {
                        "contract": BATCH_CONTRACT,
                        **({"knowledge_snapshot_profile": _KNOWLEDGE_SNAPSHOT_PROFILE, "knowledge_snapshot": _json_value(_knowledge_descriptor)} if _knowledge_seal is _KNOWLEDGE_SNAPSHOT_SEAL else {}),
                        "batch_id": batch_id,
                        **item,
                        "state": "queued",
                    }
                    if _knowledge_seal is _KNOWLEDGE_SNAPSHOT_SEAL and len(json.dumps(child_payload,sort_keys=True).encode("utf-8")) > 768*1024:
                        raise ImportBatchError("invalid_manifest", "Transfer intake unavailable.")
                    conn.execute(
                        "INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,current_attempt_id,attempt_count,input_hash,result_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,NULL,0,?,?,?,?)",
                        (
                            owner,
                            child_id,
                            "memory_import_item",
                            workspace_id,
                            "",
                            child_key,
                            "queued",
                            item["content_hash"],
                            json.dumps(child_payload, sort_keys=True),
                            now,
                            now,
                        ),
                    )
                conn.execute(
                    "UPDATE fm_v2_jobs SET state='active',updated_at=? WHERE owner_id=? AND job_id=?",
                    (now, owner, batch_id),
                )
                conn.commit()
        except ImportBatchError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ImportBatchError(
                "batch_conflict",
                "This import batch changed while it was being created; retry it.",
                retryable=True,
            ) from exc
        except sqlite3.Error as exc:
            raise ImportBatchError(
                "job_store",
                "The Memory import job could not be recorded.",
                retryable=True,
            ) from exc
        seal_job_manifest(
            owner=owner,
            job_id=batch_id,
            phase="selection",
            selected_ids=[item["item_id"] for item in normalized],
            config_hash=_digest({"contract": BATCH_CONTRACT, "limits": [MAX_BATCH_FILES, MAX_BATCH_BYTES]}),
            model_hash="pending",
            tool_hash="memory-import",
            input_hash=input_hash,
            db_path=self.db_path,
            _original_account_scope=self._original_account_scope,
        )
        parent_payload["batch_id"] = batch_id
        parent_payload["state"] = "active"
        return parent_payload

    def update_item(
        self,
        *,
        owner: str,
        batch_id: str,
        item_id: str,
        state: str,
        result: Optional[Mapping[str, Any]] = None,
        error: Optional[Mapping[str, Any]] = None,
        _snapshot_descriptor: Optional[Mapping[str, Any]] = None,
        _snapshot_claim: Optional[Mapping[str, Any]] = None,
        _snapshot_seal: Any = None,
        _knowledge_descriptor: Optional[Mapping[str, Any]] = None,
        _knowledge_seal: Any = None,
    ) -> dict[str, Any]:
        if _knowledge_descriptor is not None and (_knowledge_seal is not _KNOWLEDGE_SNAPSHOT_SEAL or state != "awaiting_review"):
            raise ImportBatchError("invalid_item_state", "Transfer intake is private.")
        if _snapshot_descriptor is not None and (_snapshot_seal is not _SNAPSHOT_IMPORT_SEAL or state != "awaiting_review"):
            raise ImportBatchError("invalid_item_state", "Snapshot staging is unavailable.")
        if state not in {"succeeded", "failed_terminal", "cancelled", "awaiting_review"}:
            raise ImportBatchError("invalid_item_state", "The import item state is invalid.")
        now = _now()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._assert_import_not_erased(conn,owner,job_id=batch_id)
                self._assert_import_not_erased(conn,owner,job_id=item_id)
                parent = conn.execute(
                    "SELECT job_id FROM fm_v2_jobs WHERE owner_id=? AND job_id=? "
                    "AND kind='memory_import_batch'",
                    (owner, batch_id),
                ).fetchone()
                if not parent:
                    raise ImportBatchError("batch_not_found", "The import batch is not available.")
                row = conn.execute(
                    "SELECT result_json,state,input_hash FROM fm_v2_jobs WHERE owner_id=? AND job_id=? AND kind='memory_import_item'",
                    (owner, item_id),
                ).fetchone()
                if not row:
                    raise ImportBatchError("item_not_found", "The import item is not available.")
                payload = _payload(row[0])
                if payload.get("batch_id") != batch_id:
                    raise ImportBatchError("item_not_found", "The import item is not available.")
                if payload.get("knowledge_snapshot_profile") and (_knowledge_seal is not _KNOWLEDGE_SNAPSHOT_SEAL or _knowledge_descriptor is None):
                    raise ImportBatchError("invalid_item_state", "Transfer intake is private.")
                if _knowledge_descriptor is not None and payload.get("knowledge_snapshot_profile") != _KNOWLEDGE_SNAPSHOT_PROFILE:
                    raise ImportBatchError("invalid_item_state", "Transfer intake is private.")
                current_state = str(row[1])
                if current_state == "queued":
                    conn.execute(
                        "UPDATE fm_v2_jobs SET state='active',updated_at=? WHERE owner_id=? AND job_id=?",
                        (now, owner, item_id),
                    )
                    current_state = "active"
                if current_state not in {"active", "awaiting_review"}:
                    raise ImportBatchError(
                        "item_finalized",
                        "This import item is already final. Retry it to create a new attempt.",
                    )
                if current_state == "awaiting_review" and state == "awaiting_review":
                    raise ImportBatchError(
                        "item_awaiting_review",
                        "This import item is already waiting for review.",
                    )
                if current_state == "awaiting_review" and state not in {"succeeded", "cancelled"}:
                    raise ImportBatchError("invalid_item_state", "The import item state is invalid.")
                stored_result = dict(result or {})
                markdown_bundle = stored_result.get("markdown_bundle")
                if "markdown_bundle" in stored_result and (
                    not isinstance(markdown_bundle, Mapping)
                    or set(markdown_bundle) != _MARKDOWN_BUNDLE_FIELDS
                    or not _markdown_bundle_valid(markdown_bundle)
                    or payload.get("extension") != ".md"
                    or markdown_bundle["bundle_sha256"] != payload.get("content_hash")
                    or markdown_bundle["bundle_sha256"] != row["input_hash"]
                ):
                    raise ImportBatchError("invalid_manifest", "The Markdown import provenance changed.")
                if state == "awaiting_review":
                    stored_result = _normalize_suggestions(
                        batch_id=batch_id,
                        item_id=item_id,
                        result=stored_result,
                        markdown_bundle=markdown_bundle,
                    )
                if _knowledge_descriptor is not None:
                    from services.memory.knowledge_snapshot_format import recipient_descriptor, parse_package
                    descriptor = dict(_knowledge_descriptor)
                    account = self._knowledge_scope(owner)
                    parent_row = conn.execute("SELECT result_json,workspace_key FROM fm_v2_jobs WHERE owner_id=? AND job_id=? AND kind='memory_import_batch'", (owner,batch_id)).fetchone()
                    parent_payload = _payload(parent_row['result_json']) if parent_row else {}
                    raw = self._read_scoped_staged(conn,owner=owner,batch_id=batch_id,item_id=item_id,payload=payload)
                    actual = recipient_descriptor(raw,account_id=account,workspace_id=parent_row['workspace_key'],action_id=descriptor.get('action_id'))
                    package = parse_package(raw)
                    if (actual != descriptor or parent_payload.get('knowledge_snapshot_profile') != _KNOWLEDGE_SNAPSHOT_PROFILE
                        or parent_payload.get('knowledge_snapshot') != actual or payload.get('knowledge_snapshot') != actual
                        or parent_payload.get('identity_context_hash') != actual['review_sha256']
                        or payload.get('content_hash') != actual['raw_sha256'] or row['input_hash'] != actual['raw_sha256']
                        or payload.get('byte_size') != len(raw)):
                        raise ImportBatchError('invalid_manifest','Transfer intake changed.')
                    suggestion = stored_result.get('suggestions')
                    if type(suggestion) is not list or len(suggestion) != 1 or type(suggestion[0]) is not dict:
                        raise ImportBatchError('invalid_manifest','Transfer intake unavailable.')
                    suggestion = suggestion[0]
                    proposal = {'text':package['knowledge']['text'],'category':actual['category'],'knowledge_snapshot':actual}
                    if _initial_proposal(suggestion) != {'text':proposal['text'],'category':proposal['category']}:
                        raise ImportBatchError('invalid_manifest','Transfer text changed.')
                    suggestion['knowledge_snapshot'] = actual
                    suggestion['proposal_fingerprint'] = _digest(proposal)
                    suggestion['suggestion_id'] = 'suggestion_' + _digest({'contract':REVIEW_CONTRACT,'batch_id':batch_id,'item_id':item_id,'proposal':proposal,'occurrence':0})[:32]
                    if len(json.dumps(proposal,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')) > 320*1024:
                        raise ImportBatchError('invalid_manifest','Transfer intake unavailable.')
                    payload['knowledge_snapshot_fingerprint'] = _digest(actual)
                if _snapshot_descriptor is not None:
                    descriptor = _validated_imported_snapshot(_snapshot_descriptor)
                    suggestions = stored_result.get("suggestions")
                    if not isinstance(suggestions, list) or len(suggestions) != 1:
                        raise ImportBatchError("invalid_manifest", "Snapshot review is unavailable.")
                    suggestion = suggestions[0]
                    suggestion["snapshot_import"] = descriptor
                    proposal = {**_initial_proposal(suggestion), "snapshot_import": descriptor}
                    suggestion["proposal_fingerprint"] = _digest(proposal)
                    suggestion["suggestion_id"] = "suggestion_" + _digest({
                        "contract": REVIEW_CONTRACT, "batch_id": batch_id, "item_id": item_id,
                        "proposal": proposal, "occurrence": 0,
                    })[:32]
                    if (type(_snapshot_claim) is not dict or set(_snapshot_claim) != {"account", "upload_id", "scope"}
                        or _snapshot_claim["upload_id"] != descriptor["reference"]["upload_id"]
                        or not isinstance(_snapshot_claim["account"], str) or not _snapshot_claim["account"]
                        or not isinstance(_snapshot_claim["scope"], str)
                        or re.fullmatch(r"pending:ma4import:[0-9a-f]{64}", _snapshot_claim["scope"]) is None):
                        raise ImportBatchError("invalid_manifest", "Snapshot staging claim unavailable.")
                    payload["snapshot_import_fingerprint"] = _digest(descriptor)
                    payload["snapshot_import_claim"] = _json_value(_snapshot_claim)
                elif payload.get("snapshot_import_fingerprint"):
                    raise ImportBatchError("invalid_item_state", "Snapshot review is private.")
                payload.update({"state": state, "result": stored_result})
                if error:
                    payload["error"] = dict(error)
                elif state != "failed_terminal":
                    payload.pop("error", None)
                if _knowledge_descriptor is not None and len(json.dumps(payload,sort_keys=True).encode("utf-8")) > 768*1024:
                    raise ImportBatchError("invalid_manifest", "Transfer intake unavailable.")
                conn.execute(
                    "UPDATE fm_v2_jobs SET state=?,result_json=?,updated_at=? WHERE owner_id=? AND job_id=?",
                    (state, json.dumps(payload, sort_keys=True), now, owner, item_id),
                )
                parent_payload, children, latest = self._refresh_parent_locked(
                    conn, owner=owner, batch_id=batch_id, now=now,
                )
                conn.commit()
                pending = [row for row, _ in latest if row["state"] in {"queued", "active", "retry_wait"}]
                # The existing v2 manifest supports one item-level outcome
                # seal. Preserve that compatibility seal for first attempts;
                # review evidence itself lives on immutable suggestion IDs in
                # the durable batch payload until the v2 manifest grows a
                # revisioned per-suggestion ledger.
                if not pending and not any(
                    int(item.get("retry_number") or 0) > 0 for _, item in self._latest_children(children)
                ):
                    completed = [row for row in children if row["state"] in {"succeeded", "awaiting_review"}]
                    failed = [row for row in children if row["state"] in {"failed_terminal", "cancelled"}]
                    seal_job_manifest(
                        owner=owner,
                        job_id=batch_id,
                        phase="outcome",
                        selected_ids=[str(row["job_id"]) for row in children],
                        completed_ids=[
                            str(row["job_id"])
                            for row in completed
                            if _payload(row["result_json"]).get("result", {}).get("outcome") != "reused"
                        ],
                        reused_ids=[
                            str(row["job_id"])
                            for row in completed
                            if _payload(row["result_json"]).get("result", {}).get("outcome") == "reused"
                        ],
                        failed_ids=[str(row["job_id"]) for row in failed],
                        waived_ids=[],
                        config_hash=_digest({"contract": BATCH_CONTRACT, "limits": [MAX_BATCH_FILES, MAX_BATCH_BYTES]}),
                        model_hash="runtime",
                        tool_hash="memory-import",
                        input_hash=_digest(parent_payload.get("items", [])),
                        db_path=self.db_path,
                        _original_account_scope=self._original_account_scope,
                    )
                return parent_payload
        except ImportBatchError:
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise ImportBatchError("job_store", "The import result could not be recorded.", retryable=True) from exc

    def begin_item_retry(
        self,
        *,
        owner: str,
        batch_id: str,
        item_id: str,
    ) -> dict[str, Any]:
        """Create a replacement child for a failed source without reviving it.

        `fm_v2_jobs` treats failed-terminal children as immutable.  A retry is
        therefore a new queued child with durable `retry_of` and shared source
        lineage, not an invalid failed_terminal -> active state transition.
        The caller must process the returned `item` and pass its new item ID to
        update_item().
        """

        now = _now()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._assert_import_not_erased(conn,owner,job_id=batch_id)
                self._assert_import_not_erased(conn,owner,job_id=item_id)
                parent = conn.execute(
                    "SELECT state,result_json,workspace_key FROM fm_v2_jobs WHERE owner_id=? "
                    "AND job_id=? AND kind='memory_import_batch'",
                    (owner, batch_id),
                ).fetchone()
                if not parent:
                    raise ImportBatchError("batch_not_found", "The import batch is not available.")
                if str(parent["state"]) not in {"active", "awaiting_review"}:
                    raise ImportBatchError(
                        "batch_retry_unavailable",
                        "This finalized import batch cannot retry that item; upload it again.",
                    )
                row = conn.execute(
                    "SELECT state,result_json,idempotency_key,input_hash FROM fm_v2_jobs "
                    "WHERE owner_id=? AND job_id=? AND kind='memory_import_item'",
                    (owner, item_id),
                ).fetchone()
                if not row:
                    raise ImportBatchError("item_not_found", "The import item is not available.")
                source = _payload(row["result_json"])
                if source.get("knowledge_snapshot_profile"):
                    raise ImportBatchError("item_retry_unavailable", "Transfer intake is private.")
                if source.get("batch_id") != batch_id:
                    raise ImportBatchError("item_not_found", "The import item is not available.")
                if str(row["state"]) != "failed_terminal":
                    raise ImportBatchError(
                        "item_retry_unavailable",
                        "Only a failed import item can be retried.",
                    )
                error = source.get("error")
                # Older rows may have no retry decision. Preserve their existing
                # explicit retry path and honor a stored denial on the original.
                if isinstance(error, Mapping) and error.get("retryable") is False:
                    raise ImportBatchError(
                        "item_retry_unavailable",
                        "This import cannot be retried. Check the original import status.",
                    )
                if source.get("superseded_by"):
                    raise ImportBatchError(
                        "item_retry_superseded",
                        "A newer retry already exists for this file. Refresh its status before retrying again.",
                        retryable=True,
                    )
                root_item_id = str(source.get("root_item_id") or item_id)
                self._assert_import_not_erased(conn,owner,job_id=root_item_id)
                if source.get('staged_item_id'):
                    self._assert_import_not_erased(conn,owner,job_id=source['staged_item_id'])
                descendants = self._children_locked(conn, owner=owner, batch_id=batch_id)
                retry_number = 0
                for descendant in descendants:
                    payload = _payload(descendant["result_json"])
                    if str(payload.get("root_item_id") or descendant["job_id"]) != root_item_id:
                        continue
                    try:
                        retry_number = max(retry_number, int(payload.get("retry_number") or 0))
                    except (TypeError, ValueError):
                        continue
                retry_number += 1
                retry_item_id = "item_" + _digest({
                    "contract": BATCH_CONTRACT,
                    "batch_id": batch_id,
                    "root_item_id": root_item_id,
                    "retry_number": retry_number,
                })[:48]
                self._assert_import_not_erased(conn,owner,job_id=retry_item_id,
                    idempotency_key=f"{row['idempotency_key']}:retry:{retry_number}")
                retry_payload = {
                    "contract": BATCH_CONTRACT,
                    "batch_id": batch_id,
                    "item_id": retry_item_id,
                    "source_id": source.get("source_id") or root_item_id,
                    "root_item_id": root_item_id,
                    "retry_of": item_id,
                    "retry_number": retry_number,
                    "staged_item_id": source.get("staged_item_id") or item_id,
                    "filename": source.get("filename") or "upload",
                    "byte_size": source.get("byte_size") or 0,
                    "content_hash": source.get("content_hash") or "",
                    "extension": source.get("extension") or "",
                    "document_role": source.get("document_role") or "generic_document",
                    "state": "queued",
                }
                source["superseded_by"] = retry_item_id
                source["retry_requeued_at"] = now
                conn.execute(
                    "UPDATE fm_v2_jobs SET result_json=?,updated_at=? WHERE owner_id=? AND job_id=?",
                    (json.dumps(source, sort_keys=True), now, owner, item_id),
                )
                conn.execute(
                    "INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,"
                    "idempotency_key,state,current_attempt_id,attempt_count,input_hash,result_json,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,NULL,0,?,?,?,?)",
                    (
                        owner,
                        retry_item_id,
                        "memory_import_item",
                        parent["workspace_key"],
                        "",
                        f"{row['idempotency_key']}:retry:{retry_number}",
                        "queued",
                        row["input_hash"],
                        json.dumps(retry_payload, sort_keys=True),
                        now,
                        now,
                    ),
                )
                # awaiting_review -> active is intentionally not legal. Move
                # through queued so the v2 lifecycle trigger remains the guard.
                if str(parent["state"]) == "awaiting_review":
                    conn.execute(
                        "UPDATE fm_v2_jobs SET state='queued',updated_at=? WHERE owner_id=? AND job_id=?",
                        (now, owner, batch_id),
                    )
                parent_payload, _, _ = self._refresh_parent_locked(
                    conn, owner=owner, batch_id=batch_id, now=now,
                )
                conn.commit()
                return {
                    "batch_id": batch_id,
                    "state": parent_payload["state"],
                    "item": retry_payload,
                    "replayed": False,
                }
        except ImportBatchError:
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise ImportBatchError("job_store", "The import retry could not be recorded.", retryable=True) from exc

    def _knowledge_verified(self, conn, *, owner, batch_id, item_id, suggestion, context):
        from services.memory.knowledge_snapshot_format import recipient_descriptor, parse_package
        if type(context) is not tuple or len(context) != 3:
            raise ImportBatchError('invalid_review','Transfer intake unavailable.')
        account,workspace,action=context
        if self._knowledge_scope(owner) != account:
            raise ImportBatchError('import_unavailable','Transfer account unavailable.')
        parent=conn.execute("SELECT result_json,workspace_key FROM fm_v2_jobs WHERE owner_id=? AND job_id=? AND kind='memory_import_batch'",(owner,batch_id)).fetchone()
        child=conn.execute("SELECT result_json,input_hash FROM fm_v2_jobs WHERE owner_id=? AND job_id=? AND kind='memory_import_item'",(owner,item_id)).fetchone()
        pp=_payload(parent['result_json']) if parent else {}; item=_payload(child['result_json']) if child else {}
        children=self._children_locked(conn,owner=owner,batch_id=batch_id)
        selected=pp.get('items')
        suggestions=(item.get('result') or {}).get('suggestions')
        if (len(children)!=1 or str(children[0]['job_id'])!=item_id or type(selected) is not list or len(selected)!=1
            or type(selected[0]) is not dict or selected[0].get('item_id')!=item_id
            or type(suggestions) is not list or len(suggestions)!=1
            or not parent or parent['workspace_key']!=workspace or pp.get('knowledge_snapshot_profile')!=_KNOWLEDGE_SNAPSHOT_PROFILE
            or item.get('knowledge_snapshot_profile')!=_KNOWLEDGE_SNAPSHOT_PROFILE or item.get('batch_id')!=batch_id):
            raise ImportBatchError('invalid_review','Transfer intake unavailable.')
        raw=self._read_scoped_staged(conn,owner=owner,batch_id=batch_id,item_id=item_id,payload=item)
        try:
            desc=recipient_descriptor(raw,account_id=account,workspace_id=workspace,action_id=action)
            knowledge=parse_package(raw)['knowledge']
        except (ValueError,TypeError,UnicodeError) as exc:
            raise ImportBatchError('invalid_review','Transfer intake unavailable.') from exc
        proposal={'text':knowledge['text'],'category':desc['category'],'knowledge_snapshot':desc}
        if (pp.get('knowledge_snapshot')!=desc or item.get('knowledge_snapshot')!=desc
            or pp.get('identity_context_hash')!=desc['review_sha256'] or item.get('byte_size')!=len(raw)
            or item.get('content_hash')!=desc['raw_sha256'] or child['input_hash']!=desc['raw_sha256']
            or item.get('knowledge_snapshot_fingerprint')!=_digest(desc) or type(suggestion) is not dict
            or suggestion.get('knowledge_snapshot')!=desc or suggestion.get('text')!=proposal['text']
            or suggestion.get('category')!=proposal['category'] or suggestion.get('proposal_fingerprint')!=_digest(proposal)
            or suggestion.get('suggestion_id')!='suggestion_'+_digest({'contract':REVIEW_CONTRACT,'batch_id':batch_id,'item_id':item_id,'proposal':proposal,'occurrence':0})[:32]):
            raise ImportBatchError('invalid_review','Transfer intake changed.')
        return proposal

    def _knowledge_scope(self, owner: str) -> str:
        scope = self._original_account_scope
        if scope is None:
            raise ImportBatchError('import_unavailable','Transfer account unavailable.')
        account, authority, binding, current = scope
        if authority is not None or binding != owner or current() is not True:
            raise ImportBatchError('import_unavailable','Transfer account unavailable.')
        return account

    def create_knowledge_snapshot(self, *, owner: str, workspace_id: str, account_id: str,
                                  action_id: str, raw: bytes, expected_review_sha256: str) -> dict[str, Any]:
        from services.memory.knowledge_snapshot_format import recipient_descriptor
        if self._knowledge_scope(owner) != account_id:
            raise ImportBatchError('import_unavailable','Transfer account unavailable.')
        try:
            descriptor = recipient_descriptor(raw,account_id=account_id,workspace_id=workspace_id,action_id=action_id)
        except (ValueError,TypeError,UnicodeError) as exc:
            raise ImportBatchError('invalid_manifest','Transfer package unavailable.') from exc
        if descriptor['review_sha256'] != expected_review_sha256:
            raise ImportBatchError('invalid_manifest','Transfer review changed.')
        parent = self.create(owner=owner,workspace_id=workspace_id,session_id=None,
            items=[{'item_id':item_identity(raw),'filename':'knowledge-snapshot.json','byte_size':len(raw),
                    'content_hash':descriptor['raw_sha256'],'extension':'.json','document_role':'generic_document'}],
            identity_context_hash=descriptor['review_sha256'],action_key=action_id,_knowledge_seal=_KNOWLEDGE_SNAPSHOT_SEAL,
            _knowledge_descriptor=descriptor)
        if parent.get('knowledge_snapshot_profile') != _KNOWLEDGE_SNAPSHOT_PROFILE or parent.get('knowledge_snapshot') != descriptor or parent.get('identity_context_hash') != descriptor['review_sha256']:
            raise ImportBatchError('batch_conflict','Transfer original changed.')
        item=parent.get('items')
        if type(item) is not list or len(item)!=1 or type(item[0]) is not dict:
            raise ImportBatchError('invalid_manifest','Transfer original unavailable.')
        self.stage_bytes(owner,parent['batch_id'],item[0]['item_id'],raw)
        return self.get(owner=owner,batch_id=parent['batch_id'])

    def find_knowledge_snapshot(self, *, owner: str, workspace_id: str, account_id: str,
                                action_id: str, raw: bytes, expected_review_sha256: str) -> Optional[dict[str, Any]]:
        from services.memory.knowledge_snapshot_format import recipient_descriptor
        if self._knowledge_scope(owner) != account_id:
            raise ImportBatchError('import_unavailable','Transfer account unavailable.')
        try:
            descriptor = recipient_descriptor(raw,account_id=account_id,workspace_id=workspace_id,action_id=action_id)
        except (ValueError,TypeError,UnicodeError) as exc:
            raise ImportBatchError('invalid_manifest','Transfer package unavailable.') from exc
        if descriptor['review_sha256'] != expected_review_sha256:
            raise ImportBatchError('invalid_manifest','Transfer review changed.')
        parent = self.create(owner=owner,workspace_id=workspace_id,session_id=None,
            items=[{'item_id':item_identity(raw),'filename':'knowledge-snapshot.json','byte_size':len(raw),
                    'content_hash':descriptor['raw_sha256'],'extension':'.json','document_role':'generic_document'}],
            identity_context_hash=descriptor['review_sha256'],action_key=action_id,_knowledge_seal=_KNOWLEDGE_SNAPSHOT_SEAL,
            _knowledge_descriptor=descriptor,_lookup_only=True)
        if parent is None:
            return None
        if parent.get('knowledge_snapshot_profile') != _KNOWLEDGE_SNAPSHOT_PROFILE or parent.get('knowledge_snapshot') != descriptor or parent.get('identity_context_hash') != descriptor['review_sha256']:
            raise ImportBatchError('batch_conflict','Transfer original changed.')
        return self.get(owner=owner,batch_id=parent['batch_id'])

    def stage_knowledge_snapshot(self, *, owner: str, batch_id: str, item_id: str,
                                 account_id: str, workspace_id: str, action_id: str) -> dict[str, Any]:
        from services.memory.knowledge_snapshot_format import recipient_descriptor, parse_package
        if self._knowledge_scope(owner) != account_id:
            raise ImportBatchError('import_unavailable','Transfer account unavailable.')
        raw=self.read_staged(owner,batch_id,item_id,_knowledge_seal=_KNOWLEDGE_SNAPSHOT_SEAL)
        try:
            descriptor=recipient_descriptor(raw,account_id=account_id,workspace_id=workspace_id,action_id=action_id)
            knowledge=parse_package(raw)['knowledge']
        except (ValueError,TypeError,UnicodeError) as exc:
            raise ImportBatchError('invalid_manifest','Transfer package unavailable.') from exc
        with self._connect() as conn:
            conn.execute('BEGIN')
            row=conn.execute("SELECT result_json,state FROM fm_v2_jobs WHERE owner_id=? AND job_id=? AND kind='memory_import_item'",(owner,item_id)).fetchone()
            item=_payload(row['result_json']) if row else {}
            if row and row['state'] in {'awaiting_review','succeeded'}:
                suggestions=(item.get('result') or {}).get('suggestions')
                if type(suggestions) is not list or len(suggestions)!=1:
                    raise ImportBatchError('invalid_manifest','Transfer intake unavailable.')
                self._knowledge_verified(conn,owner=owner,batch_id=batch_id,item_id=item_id,suggestion=suggestions[0],context=(account_id,workspace_id,action_id))
                conn.commit()
                return self.get(owner=owner,batch_id=batch_id)
            conn.commit()
        self.update_item(owner=owner,batch_id=batch_id,item_id=item_id,state='awaiting_review',
            result={'suggestions':[{'text':knowledge['text'],'category':knowledge['category']}]},
            _knowledge_descriptor=descriptor,_knowledge_seal=_KNOWLEDGE_SNAPSHOT_SEAL)
        return self.get(owner=owner,batch_id=batch_id)

    def prepare_knowledge_snapshot_review(self, *, owner, batch_id, item_id, suggestion_id, review_key, account_id, workspace_id, action_id):
        if review_key != 's10-knowledge-snapshot:' + action_id:
            raise ImportBatchError('invalid_review_key','Transfer review identity unavailable.')
        result=self.prepare_review_decision(owner=owner,batch_id=batch_id,suggestion_id=suggestion_id,action='accept',review_key=review_key,actor_id=owner,
            _knowledge_seal=_KNOWLEDGE_SNAPSHOT_SEAL,_knowledge_context=(account_id,workspace_id,action_id),_knowledge_item_id=item_id)
        if result.get('item_id')!=item_id:
            raise ImportBatchError('review_unavailable','Transfer intake changed.')
        return result

    def read_original_knowledge_snapshot_review(self, *, owner, batch_id, item_id, suggestion_id, review_key_hash, proposal_fingerprint, account_id, workspace_id, action_id, allow_settled=False):
        if review_key_hash != _review_key_hash('s10-knowledge-snapshot:' + action_id):
            raise ImportBatchError('invalid_review_key','Transfer review identity unavailable.')
        return self.read_original_publishing_review(owner=owner,batch_id=batch_id,item_id=item_id,suggestion_id=suggestion_id,
            review_key_hash=review_key_hash,proposal_fingerprint=proposal_fingerprint,allow_settled=allow_settled,
            _knowledge_seal=_KNOWLEDGE_SNAPSHOT_SEAL,_knowledge_context=(account_id,workspace_id,action_id))

    def finalize_knowledge_snapshot_review(self, *, owner, batch_id, item_id, suggestion_id, review_key, publication_id, publication_kind, account_id, workspace_id, action_id):
        if review_key != 's10-knowledge-snapshot:' + action_id or publication_kind != 'candidate':
            raise ImportBatchError('invalid_review','Transfer outcome unavailable.')
        material=['s03.import-review-id.v1',account_id,workspace_id,batch_id,item_id,suggestion_id,_review_key_hash(review_key)]
        expected='importreview_'+hashlib.sha256(json.dumps(material,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()
        if publication_id != expected:
            raise ImportBatchError('invalid_review','Transfer outcome changed.')
        result=self.finalize_review_decision(owner=owner,batch_id=batch_id,suggestion_id=suggestion_id,review_key=review_key,
            outcome='accepted',publication_id=publication_id,publication_kind=publication_kind,
            _knowledge_seal=_KNOWLEDGE_SNAPSHOT_SEAL,_knowledge_context=(account_id,workspace_id,action_id),_knowledge_item_id=item_id)
        if result.get('item_id')!=item_id:
            raise ImportBatchError('review_unavailable','Transfer intake changed.')
        return result

    def stage_imported_snapshot(self, *, owner: str, batch_id: str, item_id: str,
                                description: str, descriptor: Mapping[str, Any],
                                stage_claim: Mapping[str, Any]) -> dict[str, Any]:
        descriptor = _validated_imported_snapshot(descriptor)
        if not isinstance(description, str) or not 1 <= len(description) <= 5000:
            raise ImportBatchError("invalid_manifest", "Snapshot review is unavailable.")
        return self.update_item(owner=owner, batch_id=batch_id, item_id=item_id,
            state="awaiting_review", result={"suggestions": [{"text": description, "category": "fact"}]},
            _snapshot_descriptor=descriptor, _snapshot_claim=stage_claim, _snapshot_seal=_SNAPSHOT_IMPORT_SEAL)

    def preview_snapshot_import_claims(self, *, owner: str, account: str) -> list[dict[str, str]]:
        """Freeze only marked item claims; no claim is itself authority."""
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fm_v2_jobs'").fetchone():
                return []
            rows = conn.execute("SELECT job_id,result_json FROM fm_v2_jobs WHERE owner_id=? AND kind='memory_import_item'", (owner,)).fetchall()
        claims = []
        for row in rows:
            item = _payload(row["result_json"])
            if not item.get("snapshot_import_fingerprint"):
                continue
            claim = item.get("snapshot_import_claim")
            suggestions = (item.get("result") or {}).get("suggestions") if isinstance(item.get("result"), dict) else None
            if type(suggestions) is not list or len(suggestions) != 1 or type(suggestions[0]) is not dict:
                raise ImportBatchError("invalid_manifest", "Snapshot staging inventory unavailable.")
            suggestion = suggestions[0]
            descriptor = _validated_imported_snapshot(suggestion.get("snapshot_import"))
            if (item.get("snapshot_import_fingerprint") != _digest(descriptor)
                or suggestion.get("proposal_fingerprint") != _digest({**_initial_proposal(suggestion), "snapshot_import": descriptor})):
                raise ImportBatchError("invalid_manifest", "Snapshot staging inventory unavailable.")
            if (type(claim) is not dict or set(claim) != {"account", "upload_id", "scope"}
                or claim.get("account") != account or claim.get("upload_id") != descriptor["reference"]["upload_id"]
                or not isinstance(claim.get("scope"), str)
                or re.fullmatch(r"pending:ma4import:[0-9a-f]{64}", claim["scope"]) is None):
                raise ImportBatchError("invalid_manifest", "Snapshot staging claim unavailable.")
            claims.append({"item_id": str(row["job_id"]), **claim})
            if len(claims) > 4096:
                raise ImportBatchError("invalid_manifest", "Snapshot staging inventory exceeds its bound.")
        return sorted(claims, key=lambda value: value["item_id"])

    def assert_snapshot_import_rows_removed(self, *, owner: str, claims: list[dict[str, str]]) -> None:
        with self._connect() as conn:
            for claim in claims:
                if conn.execute("SELECT 1 FROM fm_v2_jobs WHERE owner_id=? AND job_id=?", (owner, claim["item_id"])).fetchone():
                    raise ImportBatchError("stale_staging_preview", "Snapshot staging rows remain.")

    def read_imported_snapshot(self, *, owner: str, batch_id: str, item_id: str,
                               suggestion_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row, item, _, _, suggestion = self._find_suggestion_locked(
                conn, owner=owner, batch_id=batch_id, suggestion_id=suggestion_id)
            if str(row["job_id"]) != item_id or str(row["state"]) not in {"awaiting_review", "succeeded"}:
                raise ImportBatchError("item_not_reviewable", "Snapshot review is unavailable.")
            descriptor = _validated_imported_snapshot(suggestion.get("snapshot_import"))
            proposal = {**_initial_proposal(suggestion), "snapshot_import": descriptor}
            if (item.get("snapshot_import_fingerprint") != _digest(descriptor)
                or suggestion.get("proposal_fingerprint") != _digest(proposal)):
                raise ImportBatchError("invalid_manifest", "Snapshot review is unavailable.")
            return {"descriptor": descriptor, "description": proposal["text"],
                    "proposal_fingerprint": suggestion["proposal_fingerprint"]}

    def _find_suggestion_locked(
        self,
        conn: sqlite3.Connection,
        *,
        owner: str,
        batch_id: str,
        suggestion_id: str,
    ) -> tuple[sqlite3.Row, dict[str, Any], list[dict[str, Any]], int, dict[str, Any]]:
        parent = conn.execute(
            "SELECT job_id,idempotency_key FROM fm_v2_jobs WHERE owner_id=? AND job_id=? AND kind='memory_import_batch'",
            (owner, batch_id),
        ).fetchone()
        if not parent:
            raise ImportBatchError("batch_not_found", "The import batch is not available.")
        self._assert_import_not_erased(conn, owner, job_id=batch_id, idempotency_key=parent["idempotency_key"])
        for row in self._children_locked(conn, owner=owner, batch_id=batch_id):
            item = _payload(row["result_json"])
            result = item.get("result")
            suggestions = result.get("suggestions") if isinstance(result, Mapping) else None
            if not isinstance(suggestions, list):
                continue
            copied = [dict(value) if isinstance(value, Mapping) else {} for value in suggestions]
            for index, suggestion in enumerate(copied):
                if str(suggestion.get("suggestion_id") or "") == suggestion_id:
                    self._assert_import_not_erased(conn, owner, job_id=row["job_id"], idempotency_key=row["idempotency_key"])
                    return row, item, copied, index, suggestion
        raise ImportBatchError("suggestion_not_found", "The import suggestion is not available.")

    @staticmethod
    def _review_response(
        *,
        batch_id: str,
        item_id: str,
        suggestion_id: str,
        review: Mapping[str, Any],
        replayed: bool,
    ) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "item_id": item_id,
            "suggestion_id": suggestion_id,
            "review": dict(review),
            "replayed": replayed,
        }

    @staticmethod
    def _closed_no_effect(review, *, owner, item_id):
        if type(review) is not dict:
            return False
        expected={'contract','state','decision','review_key_hash','actor_id','prepared_at','proposal',
                  'proposal_fingerprint','original_proposal_fingerprint','edited','last_failure','failed_at','no_effect_original'}
        key=review.get('review_key_hash'); fingerprint=review.get('proposal_fingerprint')
        return (set(review)==expected and review.get('contract')==REVIEW_CONTRACT
            and review.get('state')=='awaiting_review' and review.get('decision')=='accept'
            and review.get('actor_id')==owner and type(key) is str and re.fullmatch(r'[0-9a-f]{64}',key) is not None
            and type(fingerprint) is str and re.fullmatch(r'[0-9a-f]{64}',fingerprint) is not None
            and type(review.get('proposal')) is dict and _digest(review['proposal'])==fingerprint
            and type(review.get('failed_at')) is str
            and review.get('last_failure')=={'code':'IMPORT_REVIEW_NOT_APPLIED','message':'The original review was not applied.','retryable':True}
            and review.get('no_effect_original')=={'version':1,'item_id':item_id,'review_key_hash':key,'proposal_fingerprint':fingerprint})

    def find_original_import_review(self, *, owner: str, batch_id: str, suggestion_id: str,
                                    review_key: str, proposal: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
        """Read a matching existing original ticket without preparing a new one."""
        key_hash=_review_key_hash(review_key)
        try:
            with self._connect() as conn:
                conn.execute('BEGIN')
                self._assert_import_not_erased(conn,owner,job_id=batch_id)
                row,item,_suggestions,_index,suggestion=self._find_suggestion_locked(
                    conn,owner=owner,batch_id=batch_id,suggestion_id=suggestion_id)
                self._assert_import_not_erased(conn,owner,job_id=str(row['job_id']))
                if item.get('knowledge_snapshot_profile'):
                    raise ImportBatchError('review_unavailable','Transfer intake is private.')
                review=suggestion.get('review') if type(suggestion) is dict else None
                fingerprint=_digest(_review_proposal(proposal,extracted=suggestion))
                history=suggestion.get('review_history') if type(suggestion) is dict else None
                prior=([review] if type(review) is dict else []) + (history if type(history) is list else [])
                for old in prior:
                    if type(old) is dict and old.get('review_key_hash') == key_hash and old.get('state') == 'awaiting_review':
                        if (not self._closed_no_effect(old,owner=owner,item_id=str(row['job_id']))
                            or old.get('proposal_fingerprint') != fingerprint):
                            raise ImportBatchError('review_key_conflict','The review decision changed; use a new action.')
                        conn.commit()
                        return self._review_response(batch_id=batch_id,item_id=str(row['job_id']),
                            suggestion_id=suggestion_id,review=old,replayed=True)
                if type(review) is not dict or review.get('state') not in {'publishing','accepted','reused','rejected','deferred'}:
                    conn.commit();return None
                if item.get('snapshot_import_fingerprint'):
                    raise ImportBatchError('review_unavailable','The original import review is unavailable.')
                fingerprint=_digest(_review_proposal(proposal,extracted=suggestion))
                if (review.get('contract') != REVIEW_CONTRACT or review.get('decision') != 'accept'
                    or review.get('actor_id') != owner or review.get('review_key_hash') != key_hash
                    or review.get('proposal_fingerprint') != fingerprint):
                    raise ImportBatchError('review_unavailable','The original import review changed.')
                result=self._review_response(batch_id=batch_id,item_id=str(row['job_id']),
                    suggestion_id=suggestion_id,review=review,replayed=True)
                conn.commit();return result
        except ImportBatchError:
            raise
        except (sqlite3.Error,ValueError,TypeError,KeyError) as exc:
            raise ImportBatchError('review_unavailable','The original import review is unavailable.') from exc

    def find_publishing_import_review(self, *, owner: str, batch_id: str,
                                      suggestion_id: str) -> dict[str, Any]:
        """Read the stored original identity for status, never prepare a review."""
        try:
            with self._connect() as conn:
                conn.execute('BEGIN')
                self._assert_import_not_erased(conn, owner, job_id=batch_id)
                row, _item, _suggestions, _index, suggestion = self._find_suggestion_locked(
                    conn, owner=owner, batch_id=batch_id, suggestion_id=suggestion_id)
                item_id = str(row['job_id'])
                self._assert_import_not_erased(conn, owner, job_id=item_id)
                review = suggestion.get('review') if type(suggestion) is dict else None
                if type(review) is not dict or review.get('state') not in {'publishing','accepted','reused','awaiting_review'}:
                    raise ImportBatchError('review_unavailable', 'The original import review is unavailable.')
                identity = (review.get('review_key_hash'), review.get('proposal_fingerprint'))
                conn.commit()
            return self.read_original_publishing_review(owner=owner, batch_id=batch_id,
                item_id=item_id, suggestion_id=suggestion_id, review_key_hash=identity[0],
                proposal_fingerprint=identity[1], allow_settled=True)
        except ImportBatchError:
            raise
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise ImportBatchError('review_unavailable', 'The original import review is unavailable.') from exc

    def read_original_publishing_review(self, *, owner: str, batch_id: str, item_id: str,
                                        suggestion_id: str, review_key_hash: str,
                                        proposal_fingerprint: str, allow_settled: bool = False,
                                        _knowledge_seal: Any = None, _knowledge_context: Any = None) -> dict[str, Any]:
        """Read one exact original review; this grants no publication."""
        if (any(type(value) is not str or not value or value != value.strip()
                for value in (owner,batch_id,item_id,suggestion_id))
            or any(type(value) is not str or re.fullmatch(r'[0-9a-f]{64}',value) is None
                for value in (review_key_hash,proposal_fingerprint))):
            raise ImportBatchError('review_unavailable','The original import review is unavailable.')
        try:
            with self._connect() as conn:
                conn.execute('BEGIN')
                self._assert_import_not_erased(conn,owner,job_id=batch_id)
                self._assert_import_not_erased(conn,owner,job_id=item_id)
                row,item,_suggestions,_index,suggestion=self._find_suggestion_locked(
                    conn,owner=owner,batch_id=batch_id,suggestion_id=suggestion_id)
                knowledge_proposal=None
                if item.get('knowledge_snapshot_profile'):
                    if _knowledge_seal is not _KNOWLEDGE_SNAPSHOT_SEAL:
                        raise ImportBatchError('review_unavailable','Transfer intake is private.')
                    knowledge_proposal=self._knowledge_verified(conn,owner=owner,batch_id=batch_id,item_id=item_id,suggestion=suggestion,context=_knowledge_context)
                elif _knowledge_seal is not None:
                    raise ImportBatchError('review_unavailable','Transfer intake unavailable.')
                review=suggestion.get('review') if type(suggestion) is dict else None
                expected={'contract','state','decision','review_key_hash','actor_id','prepared_at',
                          'proposal','proposal_fingerprint','original_proposal_fingerprint','edited'}
                settled = type(review) is dict and review.get('state') in {'accepted','reused'}
                released = type(review) is dict and review.get('state') == 'awaiting_review'
                if settled and allow_settled is True:
                    expected |= {'completed_at','publication'}
                if released and allow_settled is True:
                    expected |= {'last_failure','failed_at','no_effect_original'}
                no_effect = {'version':1,'item_id':item_id,'review_key_hash':review_key_hash,
                             'proposal_fingerprint':proposal_fingerprint}
                if (str(row['job_id']) != item_id or item.get('snapshot_import_fingerprint')
                    or type(review) is not dict or set(review) != expected
                    or review.get('contract') != REVIEW_CONTRACT
                    or (review.get('state') != 'publishing' and not (allow_settled is True and (settled or released)))
                    or (released and (review.get('no_effect_original') != no_effect
                        or review.get('last_failure') != {'code':'IMPORT_REVIEW_NOT_APPLIED','message':'The original review was not applied.','retryable':True}
                        or type(review.get('failed_at')) is not str))
                    or (settled and (type(review.get('completed_at')) is not str
                        or type(review.get('publication')) is not dict
                        or _publication(review['publication'].get('id'), review['publication'].get('kind')) != review['publication']))
                    or review.get('decision') != 'accept' or review.get('actor_id') != owner
                    or review.get('review_key_hash') != review_key_hash
                    or review.get('proposal_fingerprint') != proposal_fingerprint
                    or type(review.get('proposal')) is not dict
                    or _digest(review['proposal']) != proposal_fingerprint
                    or (review['proposal'] != knowledge_proposal if knowledge_proposal is not None else _review_proposal(review['proposal'],extracted=suggestion) != review['proposal'])
                    or type(review.get('original_proposal_fingerprint')) is not str
                    or re.fullmatch(r'[0-9a-f]{64}',review['original_proposal_fingerprint']) is None
                    or type(review.get('edited')) is not bool):
                    raise ImportBatchError('review_unavailable','The original import review changed.')
                result=self._review_response(batch_id=batch_id,item_id=item_id,suggestion_id=suggestion_id,
                    review=review,replayed=True)
                conn.commit()
                return result
        except ImportBatchError:
            raise
        except (sqlite3.Error,ValueError,TypeError,KeyError) as exc:
            raise ImportBatchError('review_unavailable','The original import review is unavailable.') from exc

    def prepare_review_decision(
        self,
        *,
        owner: str,
        batch_id: str,
        suggestion_id: str,
        action: str,
        review_key: str,
        proposal: Optional[Mapping[str, Any]] = None,
        actor_id: Optional[str] = None,
        _snapshot_seal: Any = None,
        _snapshot_item_id: Optional[str] = None,
        _knowledge_seal: Any = None,
        _knowledge_context: Any = None, _knowledge_item_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Fence one review decision before its external memory publication.

        Call this before remember().  If the process fails after the external
        write but before finalization, the durable `publishing` state forces an
        explicit reconciliation instead of silently issuing another save.
        """

        action = str(action or "").strip().lower()
        if action not in _REVIEW_ACTIONS:
            raise ImportBatchError("invalid_review", "The review decision is invalid.")
        key_hash = _review_key_hash(review_key)
        actor = str(actor_id or owner).strip()
        if not actor or actor != owner:
            raise ImportBatchError("invalid_review_actor", "The review actor is invalid.")
        now = _now()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row, item, suggestions, index, suggestion = self._find_suggestion_locked(
                    conn, owner=owner, batch_id=batch_id, suggestion_id=suggestion_id,
                )
                knowledge_proposal = None
                if item.get("knowledge_snapshot_profile"):
                    if _knowledge_seal is not _KNOWLEDGE_SNAPSHOT_SEAL or _knowledge_item_id != str(row['job_id']) or action != 'accept' or proposal is not None:
                        raise ImportBatchError("invalid_review", "Transfer intake is private.")
                    knowledge_proposal=self._knowledge_verified(conn,owner=owner,batch_id=batch_id,item_id=str(row['job_id']),suggestion=suggestion,context=_knowledge_context)
                elif _knowledge_seal is not None:
                    raise ImportBatchError("invalid_review", "Transfer intake unavailable.")
                snapshot = None
                if item.get("snapshot_import_fingerprint"):
                    if (_snapshot_seal is not _SNAPSHOT_IMPORT_SEAL or _snapshot_item_id != str(row["job_id"])
                        or action != "accept" or proposal is not None):
                        raise ImportBatchError("snapshot_review_private", "Use the snapshot review action.")
                    snapshot = _validated_imported_snapshot(suggestion.get("snapshot_import"))
                    original = {**_initial_proposal(suggestion), "snapshot_import": snapshot}
                    if (item.get("snapshot_import_fingerprint") != _digest(snapshot)
                        or suggestion.get("proposal_fingerprint") != _digest(original)):
                        raise ImportBatchError("invalid_manifest", "Snapshot review is unavailable.")
                elif _snapshot_seal is not None:
                    raise ImportBatchError("snapshot_review_private", "Snapshot review is unavailable.")
                review = suggestion.get("review") if isinstance(suggestion.get("review"), Mapping) else None
                if action in {"reject", "defer"}:
                    # Reject/defer publish nothing, so the publish gates do not
                    # apply: fence the immutable extraction proposal for audit.
                    # It is deterministic, keeping replay and fencing exact.
                    reviewed = _initial_proposal(suggestion)
                else:
                    reviewed = _review_proposal(proposal, extracted=suggestion)
                if snapshot is not None:
                    reviewed["snapshot_import"] = snapshot
                if knowledge_proposal is not None:
                    if reviewed != {"text":knowledge_proposal["text"],"category":knowledge_proposal["category"]}:
                        raise ImportBatchError("invalid_review", "Transfer text changed.")
                    reviewed=knowledge_proposal
                fingerprint = _digest(reviewed)
                history = suggestion.get('review_history')
                prior = ([review] if type(review) is dict else []) + (history if type(history) is list else [])
                for old in prior:
                    if type(old) is dict and old.get('review_key_hash') == key_hash and old.get('state') == 'awaiting_review':
                        if (not self._closed_no_effect(old,owner=owner,item_id=str(row['job_id']))
                            or old.get('decision') != action or old.get('proposal_fingerprint') != fingerprint):
                            raise ImportBatchError('review_key_conflict','The review decision changed; use a new action.')
                        conn.commit()
                        return self._review_response(batch_id=batch_id,item_id=str(row['job_id']),
                            suggestion_id=suggestion_id,review=old,replayed=True)
                if review and str(review.get("state") or "") in _TERMINAL_REVIEW_OUTCOMES:
                    if (
                        review.get("review_key_hash") == key_hash
                        and review.get("decision") == action
                        and review.get("proposal_fingerprint") == fingerprint
                    ):
                        conn.commit()
                        return self._review_response(
                            batch_id=batch_id, item_id=str(row["job_id"]), suggestion_id=suggestion_id,
                            review=review, replayed=True,
                        )
                    raise ImportBatchError("review_finalized", "This import suggestion was already reviewed.")
                if review and str(review.get("state") or "") == "publishing":
                    if (
                        review.get("review_key_hash") == key_hash
                        and review.get("decision") == action
                        and review.get("proposal_fingerprint") == fingerprint
                    ):
                        conn.commit()
                        return self._review_response(
                            batch_id=batch_id, item_id=str(row["job_id"]), suggestion_id=suggestion_id,
                            review=review, replayed=True,
                        )
                    raise ImportBatchError("review_in_progress", "This import suggestion is already being saved.", retryable=True)
                if (review and review.get('state') == 'awaiting_review'
                    and review.get('review_key_hash') == key_hash
                    and (review.get('decision') != action or review.get('proposal_fingerprint') != fingerprint)):
                    raise ImportBatchError('review_key_conflict', 'The review decision changed; use a new action.')
                if str(row["state"]) != "awaiting_review":
                    raise ImportBatchError("item_not_reviewable", "This import item is not waiting for review.")
                if review:
                    history = suggestion.get("review_history")
                    history = list(history) if isinstance(history, list) else []
                    history.append(dict(review))
                    if len(history) > _MAX_REVIEW_HISTORY and any(
                        self._closed_no_effect(old,owner=owner,item_id=str(row['job_id']))
                        for old in history[:-_MAX_REVIEW_HISTORY]
                    ):
                        raise ImportBatchError('review_history_full','The original review history is full.')
                    suggestion["review_history"] = history[-_MAX_REVIEW_HISTORY:]
                original_fingerprint = (
                    suggestion.get("proposal_fingerprint")
                    or _digest(_initial_proposal(suggestion))
                )
                review = {
                    "contract": REVIEW_CONTRACT,
                    "state": "publishing",
                    "decision": action,
                    "review_key_hash": key_hash,
                    "actor_id": actor,
                    "prepared_at": now,
                    "proposal": reviewed,
                    "proposal_fingerprint": fingerprint,
                    "original_proposal_fingerprint": original_fingerprint,
                    # Compare canonical proposals: review re-normalizes a model
                    # question_context (contract, default mode, owner scope)
                    # before publication, and that normalization alone must
                    # not read as an owner edit.
                    "edited": (fingerprint if knowledge_proposal is not None else (_digest({**_initial_proposal(reviewed), "snapshot_import": snapshot}) if snapshot is not None
                               else _digest(_initial_proposal(reviewed)))) != original_fingerprint,
                }
                suggestion["review"] = review
                suggestions[index] = suggestion
                result = dict(item.get("result") or {})
                result["suggestions"] = suggestions
                item["result"] = result
                if knowledge_proposal is not None and len(json.dumps(item,sort_keys=True).encode("utf-8")) > 768*1024:
                    raise ImportBatchError("invalid_review", "Transfer intake unavailable.")
                conn.execute(
                    "UPDATE fm_v2_jobs SET result_json=?,updated_at=? WHERE owner_id=? AND job_id=?",
                    (json.dumps(item, sort_keys=True), now, owner, row["job_id"]),
                )
                self._refresh_parent_locked(
                    conn, owner=owner, batch_id=batch_id, now=now, clear_dismissed=True,
                )
                conn.commit()
                return self._review_response(
                    batch_id=batch_id, item_id=str(row["job_id"]), suggestion_id=suggestion_id,
                    review=review, replayed=False,
                )
        except ImportBatchError:
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise ImportBatchError("job_store", "The review decision could not be recorded.", retryable=True) from exc

    def prepare_imported_snapshot_review(self, *, owner: str, batch_id: str, item_id: str,
                                         suggestion_id: str, review_key: str) -> dict[str, Any]:
        return self.prepare_review_decision(owner=owner, batch_id=batch_id, suggestion_id=suggestion_id,
            action="accept", review_key=review_key, actor_id=owner,
            _snapshot_seal=_SNAPSHOT_IMPORT_SEAL, _snapshot_item_id=item_id)

    def finalize_review_decision(
        self,
        *,
        owner: str,
        batch_id: str,
        suggestion_id: str,
        review_key: Optional[str],
        outcome: str,
        publication_id: Optional[str] = None,
        publication_kind: Optional[str] = None,
        _snapshot_seal: Any = None,
        _snapshot_item_id: Optional[str] = None,
        _original_identity: Optional[tuple[str, str, str]] = None,
        _knowledge_seal: Any = None, _knowledge_context: Any = None, _knowledge_item_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record the canonical Memory write that completed a prepared review."""

        outcome = str(outcome or "").strip().lower()
        if outcome not in _REVIEW_OUTCOMES:
            raise ImportBatchError("invalid_review", "The review outcome is invalid.")
        if _original_identity is None:
            key_hash = _review_key_hash(review_key)
        else:
            if (review_key is not None or type(_original_identity) is not tuple or len(_original_identity) != 3
                or type(_original_identity[0]) is not str or not _original_identity[0]
                or any(type(v) is not str or re.fullmatch(r'[0-9a-f]{64}', v) is None for v in _original_identity[1:])):
                raise ImportBatchError('review_unavailable', 'The original import review is unavailable.')
            key_hash = _original_identity[1]
        publication = _publication(publication_id, publication_kind)
        now = _now()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row, item, suggestions, index, suggestion = self._find_suggestion_locked(
                    conn, owner=owner, batch_id=batch_id, suggestion_id=suggestion_id,
                )
                if item.get("knowledge_snapshot_profile"):
                    if _knowledge_seal is not _KNOWLEDGE_SNAPSHOT_SEAL or _knowledge_item_id != str(row["job_id"]):
                        raise ImportBatchError("invalid_review", "Transfer intake is private.")
                    knowledge_proposal=self._knowledge_verified(conn,owner=owner,batch_id=batch_id,item_id=str(row['job_id']),suggestion=suggestion,context=_knowledge_context)
                    if type(suggestion.get('review')) is not dict or suggestion['review'].get('proposal') != knowledge_proposal:
                        raise ImportBatchError("invalid_review", "Transfer intake changed.")
                elif _knowledge_seal is not None:
                    raise ImportBatchError("invalid_review", "Transfer intake unavailable.")
                if item.get("snapshot_import_fingerprint"):
                    if _snapshot_seal is not _SNAPSHOT_IMPORT_SEAL or _snapshot_item_id != str(row["job_id"]):
                        raise ImportBatchError("snapshot_review_private", "Use the snapshot review action.")
                    descriptor = _validated_imported_snapshot(suggestion.get("snapshot_import"))
                    if (item.get("snapshot_import_fingerprint") != _digest(descriptor)
                        or suggestion.get("proposal_fingerprint") != _digest({**_initial_proposal(suggestion), "snapshot_import": descriptor})):
                        raise ImportBatchError("invalid_manifest", "Snapshot review is unavailable.")
                elif _snapshot_seal is not None:
                    raise ImportBatchError("snapshot_review_private", "Snapshot review is unavailable.")
                review = suggestion.get("review") if isinstance(suggestion.get("review"), Mapping) else None
                if not review:
                    raise ImportBatchError("review_not_prepared", "Prepare this import suggestion before saving it.")
                if (_original_identity is not None and
                    (str(row['job_id']) != _original_identity[0] or item.get('snapshot_import_fingerprint')
                     or review.get('contract') != REVIEW_CONTRACT or review.get('state') not in {'publishing','accepted','reused'}
                     or review.get('decision') != 'accept' or review.get('actor_id') != owner
                     or review.get('proposal_fingerprint') != _original_identity[2]
                     or type(review.get('proposal')) is not dict
                     or _digest(review['proposal']) != _original_identity[2])):
                    raise ImportBatchError('review_unavailable', 'The original import review changed.')
                if review.get("review_key_hash") != key_hash:
                    raise ImportBatchError("review_key_conflict", "The review decision changed; refresh before saving it.")
                if str(review.get("state") or "") in _TERMINAL_REVIEW_OUTCOMES:
                    if review.get("state") == outcome and review.get("publication") == publication:
                        conn.commit()
                        return self._review_response(
                            batch_id=batch_id, item_id=str(row["job_id"]), suggestion_id=suggestion_id,
                            review=review, replayed=True,
                        )
                    raise ImportBatchError("review_finalized", "This import suggestion was already reviewed.")
                if str(review.get("state") or "") != "publishing":
                    raise ImportBatchError("review_not_prepared", "Prepare this import suggestion before saving it.")
                action = str(review.get("decision") or "")
                valid = {
                    "accept": {"accepted", "reused"},
                    "reject": {"rejected"},
                    "defer": {"deferred"},
                }
                if outcome not in valid.get(action, set()):
                    raise ImportBatchError("review_outcome_conflict", "The review outcome does not match its decision.")
                if outcome in {"accepted", "reused"} and not publication:
                    raise ImportBatchError("publication_required", "A saved Memory reference is required for this review.")
                if outcome not in {"accepted", "reused"} and publication:
                    raise ImportBatchError("invalid_publication", "Only accepted Memory reviews may include a saved reference.")
                review["state"] = outcome
                review["completed_at"] = now
                review["publication"] = publication
                suggestion["review"] = review
                suggestions[index] = suggestion
                result = dict(item.get("result") or {})
                result["suggestions"] = suggestions
                item["result"] = result
                all_terminal = bool(suggestions) and all(
                    isinstance(candidate.get("review"), Mapping)
                    and str(candidate["review"].get("state") or "") in _TERMINAL_REVIEW_OUTCOMES
                    for candidate in suggestions
                )
                item_state = "succeeded" if all_terminal else str(row["state"])
                item["state"] = item_state
                conn.execute(
                    "UPDATE fm_v2_jobs SET state=?,result_json=?,updated_at=? WHERE owner_id=? AND job_id=?",
                    (item_state, json.dumps(item, sort_keys=True), now, owner, row["job_id"]),
                )
                self._refresh_parent_locked(conn, owner=owner, batch_id=batch_id, now=now)
                conn.commit()
                return self._review_response(
                    batch_id=batch_id, item_id=str(row["job_id"]), suggestion_id=suggestion_id,
                    review=review, replayed=False,
                )
        except ImportBatchError:
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise ImportBatchError("job_store", "The review result could not be recorded.", retryable=True) from exc

    def finalize_imported_snapshot_review(self, *, owner: str, batch_id: str, item_id: str,
                                          suggestion_id: str, review_key: str,
                                          publication_id: str, publication_kind: str) -> dict[str, Any]:
        return self.finalize_review_decision(owner=owner, batch_id=batch_id, suggestion_id=suggestion_id,
            review_key=review_key, outcome="accepted", publication_id=publication_id,
            publication_kind=publication_kind, _snapshot_seal=_SNAPSHOT_IMPORT_SEAL,
            _snapshot_item_id=item_id)

    def fail_review_decision(
        self,
        *,
        owner: str,
        batch_id: str,
        suggestion_id: str,
        review_key: Optional[str],
        error: Mapping[str, Any],
        _original_identity: Optional[tuple[str, str, str]] = None,
    ) -> dict[str, Any]:
        """Release a prepared decision after a failed external publication."""

        if _original_identity is None:
            key_hash = _review_key_hash(review_key)
        else:
            if (review_key is not None or type(_original_identity) is not tuple or len(_original_identity) != 3
                or type(_original_identity[0]) is not str or not _original_identity[0]
                or any(type(v) is not str or re.fullmatch(r'[0-9a-f]{64}',v) is None for v in _original_identity[1:])):
                raise ImportBatchError('review_unavailable','The original import review is unavailable.')
            key_hash = _original_identity[1]
        problem = _review_problem(error)
        now = _now()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row, item, suggestions, index, suggestion = self._find_suggestion_locked(
                    conn, owner=owner, batch_id=batch_id, suggestion_id=suggestion_id,
                )
                if item.get("knowledge_snapshot_profile"):
                    raise ImportBatchError("invalid_review", "Transfer intake is private.")
                if item.get("snapshot_import_fingerprint"):
                    raise ImportBatchError("snapshot_review_private", "Snapshot review remains pending.")
                review = suggestion.get("review") if isinstance(suggestion.get("review"), Mapping) else None
                if (_original_identity is not None and
                    (str(row['job_id']) != _original_identity[0] or type(review) is not dict
                     or review.get('contract') != REVIEW_CONTRACT or review.get('decision') != 'accept'
                     or review.get('actor_id') != owner or review.get('proposal_fingerprint') != _original_identity[2]
                     or type(review.get('proposal')) is not dict or _digest(review['proposal']) != _original_identity[2])):
                    raise ImportBatchError('review_unavailable','The original import review changed.')
                if not review or review.get("review_key_hash") != key_hash:
                    raise ImportBatchError("review_key_conflict", "The review decision changed; refresh before retrying it.")
                no_effect = {'version':1,'item_id':str(row['job_id']),'review_key_hash':key_hash,
                             'proposal_fingerprint':_original_identity[2]} if _original_identity is not None else None
                expected_problem = {'code':'IMPORT_REVIEW_NOT_APPLIED','message':'The original review was not applied.','retryable':True}
                if _original_identity is not None and problem != expected_problem:
                    raise ImportBatchError('review_unavailable','The original import review is unavailable.')
                if (_original_identity is not None and review.get('state') == 'awaiting_review'
                    and review.get('no_effect_original') == no_effect and review.get('last_failure') == expected_problem
                    and type(review.get('failed_at')) is str):
                    conn.commit()
                    return self._review_response(batch_id=batch_id,item_id=str(row['job_id']),
                        suggestion_id=suggestion_id,review=review,replayed=True)
                if str(review.get("state") or "") != "publishing":
                    raise ImportBatchError("review_not_prepared", "This import suggestion is not being saved.")
                review["state"] = "awaiting_review"
                if no_effect is not None:
                    review['no_effect_original'] = no_effect
                review["last_failure"] = problem
                review["failed_at"] = now
                suggestion["review"] = review
                suggestions[index] = suggestion
                result = dict(item.get("result") or {})
                result["suggestions"] = suggestions
                item["result"] = result
                conn.execute(
                    "UPDATE fm_v2_jobs SET result_json=?,updated_at=? WHERE owner_id=? AND job_id=?",
                    (json.dumps(item, sort_keys=True), now, owner, row["job_id"]),
                )
                self._refresh_parent_locked(conn, owner=owner, batch_id=batch_id, now=now)
                conn.commit()
                return self._review_response(
                    batch_id=batch_id, item_id=str(row["job_id"]), suggestion_id=suggestion_id,
                    review=review, replayed=False,
                )
        except ImportBatchError:
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise ImportBatchError("job_store", "The review failure could not be recorded.", retryable=True) from exc

    def list_pending(
        self,
        *,
        owner: str,
        states: Sequence[str] = ("queued", "active", "awaiting_review"),
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Summarize an owner's unresolved batches, most recently updated first.

        A browser that lost the long-running import response (for example to a
        proxy read timeout) uses this to recover the durable batch and resume
        review instead of stranded server-side suggestions.
        """
        allowed = [str(state) for state in states if str(state) in {
            "queued", "active", "awaiting_review", "retry_wait",
        }]
        if not allowed:
            return []
        limit = max(1, min(int(limit or 10), 50))
        placeholders = ",".join("?" for _ in allowed)
        try:
            with self._connect() as conn:
                conn.execute('BEGIN')
                self._assert_import_not_erased(conn,owner)
                rows = conn.execute(
                    "SELECT job_id,state,result_json,created_at,updated_at FROM fm_v2_jobs "
                    f"WHERE owner_id=? AND kind='memory_import_batch' AND state IN ({placeholders}) "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (owner, *allowed, limit),
                ).fetchall()
                for row in rows:
                    self._assert_import_not_erased(conn,owner,job_id=row['job_id'])
                conn.commit()
            batches = []
            for row in rows:
                payload = _payload(row["result_json"])
                if payload.get("dismissed_at"):
                    # A dismissed batch stays fetchable by id but no longer
                    # surfaces as pending review work.
                    continue
                items = payload.get("items")
                filenames = [
                    str(item.get("filename") or "upload")
                    for item in items
                    if isinstance(item, Mapping)
                ] if isinstance(items, list) else []
                batches.append({
                    "contract": BATCH_CONTRACT,
                    "batch_id": row["job_id"],
                    "state": row["state"],
                    "counts": payload.get("counts") or {},
                    "review_counts": payload.get("review_counts") or {},
                    "filenames": filenames,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                })
            return batches
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise ImportBatchError(
                "job_store", "The import batch list is unavailable.", retryable=True
            ) from exc

    def dismiss_batch(self, *, owner: str, batch_id: str) -> dict[str, Any]:
        """Mark an awaiting-review batch dismissed without touching its job state.

        The v2 state trigger pins awaiting_review -> queued|succeeded|cancelled,
        so dismissal is a payload marker honored by list_pending, not a job
        state: get() and later review activity keep working, and a newly fenced
        review decision clears the marker again.  Re-dismissal is idempotent.
        """

        now = _now()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._assert_import_not_erased(conn,owner,job_id=batch_id)
                parent = conn.execute(
                    "SELECT state,result_json FROM fm_v2_jobs WHERE owner_id=? AND job_id=? "
                    "AND kind='memory_import_batch'",
                    (owner, batch_id),
                ).fetchone()
                if not parent:
                    raise ImportBatchError("batch_not_found", "The import batch is not available.")
                if str(parent["state"]) != "awaiting_review":
                    raise ImportBatchError(
                        "invalid_batch_state",
                        "Only an import batch waiting for review can be dismissed.",
                    )
                payload = _payload(parent["result_json"])
                if not payload.get("dismissed_at"):
                    payload["dismissed_at"] = now
                    payload["updated_at"] = now
                    # Only the payload moves; the state column is untouched so
                    # the v2 transition trigger never sees a dismissal.
                    conn.execute(
                        "UPDATE fm_v2_jobs SET result_json=?,updated_at=? WHERE owner_id=? AND job_id=?",
                        (json.dumps(payload, sort_keys=True), now, owner, batch_id),
                    )
                conn.commit()
                return payload
        except ImportBatchError:
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise ImportBatchError(
                "job_store", "The import batch could not be dismissed.", retryable=True
            ) from exc

    def get(self, *, owner: str, batch_id: str) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                conn.execute('BEGIN')
                self._assert_import_not_erased(conn,owner,job_id=batch_id)
                parent = conn.execute(
                    "SELECT state,result_json,updated_at FROM fm_v2_jobs WHERE owner_id=? AND job_id=? AND kind='memory_import_batch'",
                    (owner, batch_id),
                ).fetchone()
                if not parent:
                    raise ImportBatchError("batch_not_found", "The import batch is not available.")
                payload = json.loads(parent[1] or "{}")
                knowledge_root = payload.get("knowledge_snapshot")
                knowledge_profile = payload.get("knowledge_snapshot_profile")
                if knowledge_profile is not None or knowledge_root is not None:
                    if knowledge_profile != _KNOWLEDGE_SNAPSHOT_PROFILE or type(knowledge_root) is not dict:
                        raise ImportBatchError('invalid_manifest','Transfer original unavailable.')
                    payload.pop("knowledge_snapshot",None)
                payload["batch_id"] = batch_id
                payload["state"] = parent[0]
                payload["updated_at"] = parent[2]
                children = conn.execute(
                    "SELECT result_json,state,job_id FROM fm_v2_jobs WHERE owner_id=? AND kind='memory_import_item' AND json_extract(result_json,'$.batch_id')=? ORDER BY job_id",
                    (owner, batch_id),
                ).fetchall()
                if knowledge_profile == _KNOWLEDGE_SNAPSHOT_PROFILE and len(children) != 1:
                    raise ImportBatchError('invalid_manifest','Transfer original unavailable.')
                payload["items"] = []
                for child in children:
                    self._assert_import_not_erased(conn,owner,job_id=child[2])
                    item = json.loads(child[0] or "{}")
                    item.pop("snapshot_import_claim", None)
                    item_profile=item.get('knowledge_snapshot_profile')
                    item_root=item.get('knowledge_snapshot')
                    if knowledge_profile is not None or item_profile is not None or item_root is not None:
                        if (knowledge_profile != _KNOWLEDGE_SNAPSHOT_PROFILE or item_profile != knowledge_profile
                            or type(item_root) is not dict or item_root != knowledge_root):
                            raise ImportBatchError('invalid_manifest','Transfer original unavailable.')
                        item.pop('knowledge_snapshot',None)
                        for suggestion in ((item.get('result') or {}).get('suggestions') or []):
                            if type(suggestion) is dict:
                                suggestion.pop('knowledge_snapshot',None)
                                for review in [suggestion.get('review'),*(suggestion.get('review_history') or [])]:
                                    if type(review) is dict:
                                        review.pop('proposal',None)
                    item["state"] = child[1]
                    payload["items"].append(item)
                conn.commit()
                return payload
        except ImportBatchError:
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise ImportBatchError("job_store", "The import status is unavailable.", retryable=True) from exc

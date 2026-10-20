"""Durable owner-scoped memory import batches.

The legacy file importer predates the v2 job substrate and records one best-
effort ``memory_file_import`` row after the work has already happened.  This
module gives the UI one parent job and deterministic child identities while
keeping the existing v2 job tables as the only durable job authority.  Source
bytes are staged outside SQLite with owner-derived paths and are never placed
in the job ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from src.constants import DATA_DIR, FM_DB_PATH
from src.frankenmemory_v2 import _job_tables_ready, seal_job_manifest


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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _children_locked(
        conn: sqlite3.Connection, *, owner: str, batch_id: str
    ) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT job_id,state,result_json,created_at,updated_at FROM fm_v2_jobs "
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
        with _STAGING_LOCK:
            root = self._stage_root(owner, batch_id)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(root, 0o700)
            except OSError:
                pass
            path = root / f"{item_id}.bin"
            path.write_bytes(content)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return path

    def read_staged(self, owner: str, batch_id: str, item_id: str) -> bytes:
        self._validate_staged_identity(owner, batch_id, item_id)
        with _STAGING_LOCK:
            path = self._stage_root(owner, batch_id) / f"{item_id}.bin"
            if not path.exists():
                # Retry children intentionally reference the original staged bytes;
                # a retry must not require a second upload merely because terminal
                # v2 jobs cannot be resurrected.
                try:
                    with self._connect() as conn:
                        row = conn.execute(
                            "SELECT result_json FROM fm_v2_jobs WHERE owner_id=? AND job_id=? "
                            "AND kind='memory_import_item' AND json_extract(result_json,'$.batch_id')=?",
                            (owner, item_id, batch_id),
                        ).fetchone()
                    staged_item_id = str(_payload(row["result_json"]).get("staged_item_id") or "") if row else ""
                    if staged_item_id:
                        self._validate_staged_identity(owner, batch_id, staged_item_id)
                        path = self._stage_root(owner, batch_id) / f"{staged_item_id}.bin"
                except (ImportBatchError, sqlite3.Error):
                    # The public error below remains deliberately non-leaking.
                    pass
            try:
                stat = path.stat()
            except FileNotFoundError as exc:
                raise ImportBatchError(
                    "staged_source_missing",
                    "This import item is no longer available; upload it again.",
                    retryable=False,
                ) from exc
            if time.time() - stat.st_mtime > STAGED_SOURCE_TTL_SECONDS:
                path.unlink(missing_ok=True)
                raise ImportBatchError(
                    "staged_source_expired",
                    "This staged import item expired; upload it again.",
                    retryable=False,
                )
            return path.read_bytes()

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

    def preview_owner_staging(self, owner: str) -> dict[str, Any]:
        """Fingerprint exact owner-scoped staged source bytes for nuke CAS."""
        with _STAGING_LOCK:
            return self._owner_staging_snapshot_unlocked(owner)

    def rename_owner_staging(
        self,
        old_owner: str,
        new_owner: str,
        *,
        expected_source: Optional[Mapping[str, Any]] = None,
        expected_target: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Atomically move owner-staged imports with crash-safe reconciliation."""

        old_owner = str(old_owner or "").strip().lower()
        new_owner = str(new_owner or "").strip().lower()
        if not old_owner or not new_owner or "\x00" in old_owner or "\x00" in new_owner:
            raise ImportBatchError(
                "scope_required", "An authenticated Memory scope is required."
            )
        with _STAGING_LOCK:
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

    def purge_owner_staging(
        self, owner: str, *, expected: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Delete only the previewed owner's staged import bytes."""
        with _STAGING_LOCK:
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
    ) -> dict[str, Any]:
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
        if identity_context_hash:
            input_descriptor["identity_context_hash"] = identity_context_hash
        input_hash = _digest(input_descriptor)
        batch_id = "batch_" + input_hash[:32]
        idempotency_key = "memory-import-batch:" + input_hash
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
                existing = conn.execute(
                    "SELECT job_id,result_json,state FROM fm_v2_jobs WHERE owner_id=? AND idempotency_key=?",
                    (owner, idempotency_key),
                ).fetchone()
                if existing:
                    conn.commit()
                    payload = json.loads(existing[1] or "{}")
                    payload.setdefault("batch_id", existing[0])
                    payload.setdefault("state", existing[2])
                    payload["replayed"] = True
                    return payload
                parent_payload = {
                    "contract": BATCH_CONTRACT,
                    "batch_id": batch_id,
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "identity_context_hash": identity_context_hash or None,
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
                        "batch_id": batch_id,
                        **item,
                        "state": "queued",
                    }
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
    ) -> dict[str, Any]:
        if state not in {"succeeded", "failed_terminal", "cancelled", "awaiting_review"}:
            raise ImportBatchError("invalid_item_state", "The import item state is invalid.")
        now = _now()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                parent = conn.execute(
                    "SELECT job_id FROM fm_v2_jobs WHERE owner_id=? AND job_id=? "
                    "AND kind='memory_import_batch'",
                    (owner, batch_id),
                ).fetchone()
                if not parent:
                    raise ImportBatchError("batch_not_found", "The import batch is not available.")
                row = conn.execute(
                    "SELECT result_json,state FROM fm_v2_jobs WHERE owner_id=? AND job_id=? AND kind='memory_import_item'",
                    (owner, item_id),
                ).fetchone()
                if not row:
                    raise ImportBatchError("item_not_found", "The import item is not available.")
                payload = _payload(row[0])
                if payload.get("batch_id") != batch_id:
                    raise ImportBatchError("item_not_found", "The import item is not available.")
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
                if state == "awaiting_review":
                    stored_result = _normalize_suggestions(
                        batch_id=batch_id,
                        item_id=item_id,
                        result=stored_result,
                    )
                payload.update({"state": state, "result": stored_result})
                if error:
                    payload["error"] = dict(error)
                elif state != "failed_terminal":
                    payload.pop("error", None)
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
                if source.get("batch_id") != batch_id:
                    raise ImportBatchError("item_not_found", "The import item is not available.")
                if str(row["state"]) != "failed_terminal":
                    raise ImportBatchError(
                        "item_retry_unavailable",
                        "Only a failed import item can be retried.",
                    )
                if source.get("superseded_by"):
                    raise ImportBatchError(
                        "item_retry_superseded",
                        "A newer retry already exists for this file. Refresh its status before retrying again.",
                        retryable=True,
                    )
                root_item_id = str(source.get("root_item_id") or item_id)
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

    def _find_suggestion_locked(
        self,
        conn: sqlite3.Connection,
        *,
        owner: str,
        batch_id: str,
        suggestion_id: str,
    ) -> tuple[sqlite3.Row, dict[str, Any], list[dict[str, Any]], int, dict[str, Any]]:
        parent = conn.execute(
            "SELECT job_id FROM fm_v2_jobs WHERE owner_id=? AND job_id=? AND kind='memory_import_batch'",
            (owner, batch_id),
        ).fetchone()
        if not parent:
            raise ImportBatchError("batch_not_found", "The import batch is not available.")
        for row in self._children_locked(conn, owner=owner, batch_id=batch_id):
            item = _payload(row["result_json"])
            result = item.get("result")
            suggestions = result.get("suggestions") if isinstance(result, Mapping) else None
            if not isinstance(suggestions, list):
                continue
            copied = [dict(value) if isinstance(value, Mapping) else {} for value in suggestions]
            for index, suggestion in enumerate(copied):
                if str(suggestion.get("suggestion_id") or "") == suggestion_id:
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
                review = suggestion.get("review") if isinstance(suggestion.get("review"), Mapping) else None
                if action in {"reject", "defer"}:
                    # Reject/defer publish nothing, so the publish gates do not
                    # apply: fence the immutable extraction proposal for audit.
                    # It is deterministic, keeping replay and fencing exact.
                    reviewed = _initial_proposal(suggestion)
                else:
                    reviewed = _review_proposal(proposal, extracted=suggestion)
                fingerprint = _digest(reviewed)
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
                if str(row["state"]) != "awaiting_review":
                    raise ImportBatchError("item_not_reviewable", "This import item is not waiting for review.")
                if review:
                    history = suggestion.get("review_history")
                    history = list(history) if isinstance(history, list) else []
                    history.append(dict(review))
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
                    "edited": _digest(_initial_proposal(reviewed)) != original_fingerprint,
                }
                suggestion["review"] = review
                suggestions[index] = suggestion
                result = dict(item.get("result") or {})
                result["suggestions"] = suggestions
                item["result"] = result
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

    def finalize_review_decision(
        self,
        *,
        owner: str,
        batch_id: str,
        suggestion_id: str,
        review_key: str,
        outcome: str,
        publication_id: Optional[str] = None,
        publication_kind: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record the canonical Memory write that completed a prepared review."""

        outcome = str(outcome or "").strip().lower()
        if outcome not in _REVIEW_OUTCOMES:
            raise ImportBatchError("invalid_review", "The review outcome is invalid.")
        key_hash = _review_key_hash(review_key)
        publication = _publication(publication_id, publication_kind)
        now = _now()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row, item, suggestions, index, suggestion = self._find_suggestion_locked(
                    conn, owner=owner, batch_id=batch_id, suggestion_id=suggestion_id,
                )
                review = suggestion.get("review") if isinstance(suggestion.get("review"), Mapping) else None
                if not review:
                    raise ImportBatchError("review_not_prepared", "Prepare this import suggestion before saving it.")
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

    def fail_review_decision(
        self,
        *,
        owner: str,
        batch_id: str,
        suggestion_id: str,
        review_key: str,
        error: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Release a prepared decision after a failed external publication."""

        key_hash = _review_key_hash(review_key)
        problem = _review_problem(error)
        now = _now()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row, item, suggestions, index, suggestion = self._find_suggestion_locked(
                    conn, owner=owner, batch_id=batch_id, suggestion_id=suggestion_id,
                )
                review = suggestion.get("review") if isinstance(suggestion.get("review"), Mapping) else None
                if not review or review.get("review_key_hash") != key_hash:
                    raise ImportBatchError("review_key_conflict", "The review decision changed; refresh before retrying it.")
                if str(review.get("state") or "") != "publishing":
                    raise ImportBatchError("review_not_prepared", "This import suggestion is not being saved.")
                review["state"] = "awaiting_review"
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
        states: Sequence[str] = ("active", "awaiting_review"),
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
                rows = conn.execute(
                    "SELECT job_id,state,result_json,created_at,updated_at FROM fm_v2_jobs "
                    f"WHERE owner_id=? AND kind='memory_import_batch' AND state IN ({placeholders}) "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (owner, *allowed, limit),
                ).fetchall()
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
                parent = conn.execute(
                    "SELECT state,result_json,updated_at FROM fm_v2_jobs WHERE owner_id=? AND job_id=? AND kind='memory_import_batch'",
                    (owner, batch_id),
                ).fetchone()
                if not parent:
                    raise ImportBatchError("batch_not_found", "The import batch is not available.")
                payload = json.loads(parent[1] or "{}")
                payload["batch_id"] = batch_id
                payload["state"] = parent[0]
                payload["updated_at"] = parent[2]
                children = conn.execute(
                    "SELECT result_json,state FROM fm_v2_jobs WHERE owner_id=? AND kind='memory_import_item' AND json_extract(result_json,'$.batch_id')=? ORDER BY job_id",
                    (owner, batch_id),
                ).fetchall()
                payload["items"] = []
                for child in children:
                    item = json.loads(child[0] or "{}")
                    item["state"] = child[1]
                    payload["items"].append(item)
                return payload
        except ImportBatchError:
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise ImportBatchError("job_store", "The import status is unavailable.", retryable=True) from exc

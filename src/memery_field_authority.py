"""Inactive pure field-authority decisions for future trusted V2 writers.

Persisted provenance and OwnerReviewContext are supplied by the authenticated
repository, never by a model or source document. No storage or review queue is
created here. The caller must apply only returned paths in its CAS transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

_ORIGINS = {"manual", "inferred", "inherited", "legacy_unknown"}
_PROTECTED = {"manual", "legacy_unknown"}
_MISSING = object()


@dataclass(frozen=True)
class TrustedProducerContext:
    """Internal repository-supplied producer classification and validated evidence paths."""
    kind: str  # automatic, inherited, or legacy_import
    validated_inherited_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class OwnerReviewContext:
    authenticated_owner: str
    reviewer_id: str
    expected_revision: int
    proposal_key: str
    payload_hash: str
    approved_paths: tuple[str, ...]


def _equal(left: Any, right: Any) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return type(left) is type(right) and left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _changes(before: Any, after: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    if _equal(before, after):
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        return [change for key in sorted(before.keys() | after.keys())
                for change in _changes(before.get(key, _MISSING), after.get(key, _MISSING), path + "/" + _escape(key))]
    return [(path, before, after)]


def _overlaps(left: str, right: str) -> bool:
    return left == right or left == "" or right == "" or left.startswith(right + "/") or right.startswith(left + "/")


def proposal_fields(current: Mapping[str, Any], proposed: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Adapt actual changes to a candidate/conflict payload without persisting it."""
    def slot(value: Any) -> dict[str, Any]:
        return {"present": False} if value is _MISSING else {"present": True, "value": value}
    return [{"path": path, "before": slot(before), "after": slot(after)}
            for path, before, after in _changes(dict(current), dict(proposed))]


def replay_disposition(existing_hash: str | None, payload_hash: str) -> str:
    if not isinstance(payload_hash, str) or not payload_hash:
        raise ValueError("payload hash required")
    if existing_hash is None:
        return "new"
    return "replay" if existing_hash == payload_hash else "payload_conflict"


def decide_fields(*, owner: str, current: Mapping[str, Any], proposed: Mapping[str, Any],
                  current_revision: int, expected_revision: int, provenance: Mapping[str, str],
                  proposal_key: str, payload_hash: str, existing_payload_hash: str | None = None,
                  review: OwnerReviewContext | None = None, classification: str | None = None,
                  producer: TrustedProducerContext | None = None) -> dict[str, Any]:
    """Return a decision; trusted provenance/review must come from repository authority."""
    if not isinstance(owner, str) or not owner or not isinstance(current, dict) or not isinstance(proposed, dict):
        raise ValueError("owner and object values required")
    if any(not isinstance(path, str) or (path and not path.startswith("/")) or origin not in _ORIGINS for path, origin in provenance.items()):
        raise ValueError("invalid persisted field provenance")
    if classification not in (None, "change", "correction", "unresolved"):
        raise ValueError("invalid proposal classification")
    if not proposal_key or isinstance(current_revision, bool) or isinstance(expected_revision, bool) or not isinstance(current_revision, int) or not isinstance(expected_revision, int) or min(current_revision, expected_revision) < 1:
        raise ValueError("proposal identity and revisions required")
    if producer is not None and (producer.kind not in {"automatic", "inherited", "legacy_import"} or (producer.kind != "inherited" and producer.validated_inherited_paths)):
        raise ValueError("invalid trusted producer context")
    base = {"classification": classification or "unresolved", "allowed_paths": [], "review_paths": [], "origins": {}}
    if review is not None:
        if review.authenticated_owner != owner or review.reviewer_id != owner:
            return {**base, "outcome": "review_denied"}
        if review.expected_revision != current_revision:
            return {**base, "outcome": "stale_approval"}
        if review.proposal_key != proposal_key or review.payload_hash != payload_hash:
            return {**base, "outcome": "payload_conflict"}
    disposition = replay_disposition(existing_payload_hash, payload_hash)
    if disposition == "payload_conflict":
        return {**base, "outcome": "payload_conflict"}
    if disposition == "replay":
        return {**base, "outcome": "replay"}
    if expected_revision != current_revision:
        return {**base, "outcome": "stale_revision"}
    changes = _changes(dict(current), dict(proposed))
    if review is not None:
        if len(set(review.approved_paths)) != len(review.approved_paths) or any(path not in {c[0] for c in changes} for path in review.approved_paths):
            return {**base, "outcome": "invalid_review_subset"}
    approved = set(review.approved_paths) if review else set()
    allowed, pending, origins = [], [], {}
    for path, before, _ in changes:
        related = [(p, origin) for p, origin in provenance.items() if _overlaps(path, p)]
        ancestor = [(p, origin) for p, origin in related if p == "" or path == p or path.startswith(p + "/")]
        protected = any(origin in _PROTECTED for _, origin in related)
        if before is not _MISSING and not ancestor:
            protected = True  # Existing field without proven authority remains legacy_unknown.
        if (review is not None and path not in approved) or (protected and path not in approved):
            pending.append(path)
            continue
        allowed.append(path)
        if path in approved:
            origins[path] = "manual"
        elif before is _MISSING:
            origins[path] = ("inferred" if producer and producer.kind == "automatic" else
                             "inherited" if producer and producer.kind == "inherited" and path in producer.validated_inherited_paths else
                             "legacy_unknown")
        else:
            origins[path] = max(ancestor, key=lambda item: len(item[0]))[1]
    outcome = "unchanged" if not changes else "partial" if allowed and pending else "review_required" if pending else "allowed"
    return {**base, "outcome": outcome, "allowed_paths": allowed, "review_paths": pending, "origins": origins}


def candidate_disposition(state: str) -> str:
    """Do not mislabel automatic activation as authenticated owner approval."""
    mapping = {"proposed": "pending", "validating": "pending", "awaiting_review": "pending", "eligible_auto": "pending",
               "rejected": "rejected", "cancelled": "cancelled", "activated": "activated_not_owner_approval",
               "duplicate": "unresolved", "corroborating": "unresolved", "conflicted": "unresolved", "quarantined": "unresolved"}
    if state not in mapping:
        raise ValueError("unsupported candidate state")
    return mapping[state]


def conflict_disposition(state: str) -> str:
    mapping = {"open": "unresolved", "resolved": "resolved_requires_audit", "dismissed": "dismissed_requires_audit"}
    if state not in mapping:
        raise ValueError("unsupported conflict state")
    return mapping[state]

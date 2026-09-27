"""Content-free Stats capture and deterministic admission."""

from __future__ import annotations

import hashlib
import math
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy.exc import IntegrityError

from core.stats_models import StatsEvent

PRODUCER_REVISION = "s01-host-v1"
TOKEN_FIELDS = {
    "input_tokens": ("input_tokens", "inputTokens"),
    "output_tokens": ("output_tokens", "outputTokens"),
    "cache_read_tokens": ("cache_read_tokens", "cacheReadTokens"),
    "cache_write_tokens": ("cache_write_tokens", "cacheWriteTokens"),
    "reasoning_tokens": ("reasoning_tokens", "reasoningTokens"),
}
ALLOWED_KINDS = {"request", "provider_attempt", "partial", "response", "failure", "cancellation"}


def _check_projection_interrupt(cancel_event=None, deadline=None):
    if cancel_event is not None and cancel_event.is_set():
        raise TimeoutError("Stats projection cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Stats projection deadline exceeded")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _nonnegative(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, float) or not math.isfinite(value) or value < 0 or value != int(value):
        return None
    if abs(value) > 2**53:
        return None
    return int(value)


def _metric(metadata: Mapping[str, Any], key: str) -> Any:
    metrics = metadata.get("metrics")
    if not isinstance(metrics, Mapping):
        metrics = metadata
    candidates = TOKEN_FIELDS[key] + (("thinking_tokens",) if key == "reasoning_tokens" else ())
    for candidate in candidates:
        if candidate in metrics:
            return metrics[candidate]
    return None


def _safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"event_kind", "status", "terminal", "billable", "observation_kind", "sequence",
               "supersedes_replay_key", "actor_kind", "source", "producer_revision", "attempt_id",
               "operation_id", "parent_id", "root_operation_id", "provider_id", "account_id", "route_id",
               "requested_model", "actual_model", "model_fingerprint", "model_identity_source", "normalization_profile", "duration_ms", "billing_lane",
               "estimated", "covers_attempts",
               # Typed, content-free S09 quality signals.  Presence is
               # meaningful even when the value is explicitly False.
               "prompt_unverified", "prompt_missing_verification", "context_pressure",
               "compacted", "mid_task_compaction", "abandoned", "edit_churn",
               "tool_failure", "retry", "streak_failure"}
    result = {}
    for key in allowed:
        value = metadata.get(key)
        if isinstance(value, (str, bool, int, float)) and not (isinstance(value, float) and not math.isfinite(value)):
            result[key] = value
    return result


def _replay_key(prefix: str, identity: str, revision: str) -> str:
    return f"{prefix}:{hashlib.sha256(f'{identity}\0{revision}'.encode()).hexdigest()}"


def _message_replay_key(message_id: str) -> str:
    return _replay_key("message", str(message_id), "canonical-message-v1")


def _event_values(*, owner: str, event_kind: str, event_time, replay_key: str, session_id=None,
                  message_id=None, root_operation_id=None, operation_id=None, attempt_id=None,
                  parent_id=None, workspace_id=None, provider_id=None, account_id=None, route_id=None,
        requested_model=None, actual_model=None, model_fingerprint=None, metadata=None,
        source="host", incognito=False, observation_scope="message"):
    metadata = dict(metadata or {})
    token_values = {name: _nonnegative(_metric(metadata, name)) for name in TOKEN_FIELDS}
    reported = any(value is not None for value in token_values.values())
    state = "estimated" if metadata.get("estimated") else "reported"
    token_states = {f"{name}_state": (state if value is not None else "unavailable") for name, value in token_values.items()}
    observation_kind = str(metadata.get("observation_kind") or "final_snapshot")
    if observation_kind not in {"delta", "cumulative_snapshot", "final_snapshot"}:
        observation_kind = "final_snapshot"
    kind = event_kind if event_kind in ALLOWED_KINDS else "response"
    return dict(id=str(uuid.uuid4()), replay_key=replay_key, owner=owner, session_id=session_id,
        message_id=message_id, root_operation_id=root_operation_id, operation_id=operation_id,
        attempt_id=attempt_id, parent_id=parent_id, event_kind=kind, event_time=event_time or _now(),
        actor_kind=str(metadata.get("actor_kind") or "foreground"), workspace_id=workspace_id,
        provider_id=provider_id, account_id=account_id, route_id=route_id,
        requested_model=requested_model, actual_model=actual_model,
        model_fingerprint=model_fingerprint, **token_values,
        token_state=(state if reported else "unavailable"), **token_states,
        observation_kind=observation_kind, sequence=_nonnegative(metadata.get("sequence")),
        supersedes_replay_key=metadata.get("supersedes_replay_key"),
        status=str(metadata.get("status") or ("complete" if kind == "response" else kind)),
        terminal=bool(metadata.get("terminal", kind in {"response", "failure", "cancellation"})),
        billable=metadata.get("billable") if isinstance(metadata.get("billable"), bool) else None,
        duration_ms=_nonnegative(metadata.get("duration_ms")), source=source,
        producer_revision=str(metadata.get("producer_revision") or PRODUCER_REVISION),
        event_metadata=_safe_metadata(metadata),
        attempt_coverage=("covered" if metadata.get("covers_attempts") is True else "unavailable"),
        observation_scope=observation_scope,
        incognito=incognito)


def _insert_once(db, values):
    if values["incognito"]:
        return None
    # The lookup and insert must be isolated.  A concurrent writer may win the
    # unique replay key between them; rolling back only this savepoint keeps
    # the caller's transaction usable.
    nested = db.begin_nested()
    try:
        existing = db.query(StatsEvent).filter(
            StatsEvent.replay_key == values["replay_key"]
        ).first()
        if existing is not None:
            nested.rollback()
            return existing
        event = StatsEvent(**values)
        db.add(event)
        db.flush()
        nested.commit()
        return event
    except IntegrityError:
        nested.rollback()
        existing = db.query(StatsEvent).filter(
            StatsEvent.replay_key == values["replay_key"]
        ).first()
        if existing is None:
            raise
        return existing


def erase_owner_events(db, owner: str) -> int:
    """Erase linked and unlinked Stats facts for an authoritative owner."""
    owner = str(owner or "").strip()
    if not owner:
        return 0
    count = db.query(StatsEvent).filter(StatsEvent.owner == owner).delete(synchronize_session=False)
    return int(count or 0)


def capture_message_event(db, db_session, db_message, message, *, producer_revision=PRODUCER_REVISION):
    metadata = dict(getattr(message, "metadata", None) or {})
    if metadata.get("incognito") or metadata.get("stats_disabled"):
        return None
    owner = str(getattr(db_session, "owner", None) or "").strip()
    if not owner:
        # Shared/legacy sessions have no authoritative owner scope.
        return None
    if getattr(message, "role", None) != "assistant" and not isinstance(metadata.get("metrics"), Mapping):
        return None
    root = str(metadata.get("root_operation_id") or metadata.get("root_turn_id") or "").strip() or None
    # Canonical message IDs are immutable and database scoped.  Do not let a
    # mutable metadata hint or owner rename create a second observation.
    # The canonical database message ID is the identity.  The producer
    # revision only separates schema/normalization generations; metadata may
    # never select a second replay identity.
    replay = _message_replay_key(str(db_message.id))
    values = _event_values(owner=owner,
        event_kind=str(metadata.get("event_kind") or "response"), event_time=db_message.timestamp,
        replay_key=replay, session_id=db_session.id, message_id=db_message.id, root_operation_id=root,
        operation_id=metadata.get("operation_id"), attempt_id=metadata.get("attempt_id"),
        parent_id=metadata.get("parent_id"), workspace_id=getattr(db_session, "workspace_id", None),
        provider_id=metadata.get("provider_id"), account_id=metadata.get("account_id"),
        route_id=metadata.get("route_id") or metadata.get("model_route_id"),
        requested_model=metadata.get("requested_model"),
        actual_model=metadata.get("actual_model") or metadata.get("model"),
        model_fingerprint=metadata.get("model_fingerprint"),
        metadata={**metadata, "producer_revision": producer_revision}, source="session_message",
        observation_scope=("root" if root else "message"))
    return _insert_once(db, values)


def capture_operation_result(db, request, result, *, selected_route=None):
    event_kind = {"complete": "response", "failed": "failure", "cancelled": "cancellation"}.get(result.state, "response")
    metadata = {"event_kind": event_kind,
        "status": result.state, "terminal": True, "observation_kind": "final_snapshot",
        "billable": None, "operation_id": result.operation_id,
        "root_operation_id": result.root_operation_id, "route_id": result.model_route_id,
        "account_id": result.selected_account_id,
        "billing_lane": getattr(selected_route, "billing_lane", result.billing_lane),
        "provider_id": getattr(selected_route, "provider_id", None),
        "actual_model": getattr(selected_route, "model_id", None),
        "model_identity_source": "selected_route",
        # Native ACP/SDK producers declare the profile that describes whether
        # cache and reasoning counts are already separated.  Preserve that
        # provenance; only the legacy managed result path uses the inclusive
        # fallback.
        "normalization_profile": (
            result.normalization_profile or "managed-sdk-inclusive-v1"
        ),
        "model_fingerprint": result.model_fingerprint, "source": "operation_router"}
    for key, value in result.usage.items():
        metadata[key] = value
    values = _event_values(owner=request.owner, event_kind=event_kind, event_time=_now(),
        replay_key=_replay_key("operation", f"{request.owner}:{result.operation_id}", PRODUCER_REVISION),
        root_operation_id=result.root_operation_id, operation_id=result.operation_id,
        attempt_id=None, route_id=result.model_route_id, provider_id=getattr(selected_route, "provider_id", None),
        actual_model=getattr(selected_route, "model_id", None),
        account_id=result.selected_account_id, metadata=metadata, source="operation_router",
        observation_scope="operation")
    return _insert_once(db, values)


@dataclass(frozen=True)
class StatsProjection:
    """Admitted facts plus explicit overlap and coverage evidence."""

    events: tuple[StatsEvent, ...]
    excluded: tuple[StatsEvent, ...]
    coverage: str

    def __iter__(self):
        return iter(self.events)

    def __len__(self):
        return len(self.events)


def project_admitted_events(events: Iterable[StatsEvent], *, cancel_event=None, deadline=None) -> StatsProjection:
    """Project a hierarchy without double-counting root/operation/attempt facts."""
    grouped = defaultdict(list)
    aggregates = defaultdict(list)
    aggregate_scope = {}
    attempts = []
    operations = []
    operations_by_root = defaultdict(list)
    attempts_by_root = defaultdict(list)
    attempts_by_operation = defaultdict(list)
    for event in events:
        _check_projection_interrupt(cancel_event, deadline)
        attempt_id = getattr(event, "attempt_id", None)
        operation_id = getattr(event, "operation_id", None)
        root_id = getattr(event, "root_operation_id", None)
        explicit_scope = getattr(event, "observation_scope", None)
        if attempt_id is not None or explicit_scope == "attempt":
            scope = (event.owner, "attempt", attempt_id or event.replay_key)
            attempts.append(event)
            attempts_by_root[(event.owner, root_id)].append(event)
            attempts_by_operation[(event.owner, root_id, operation_id)].append(event)
        elif explicit_scope == "operation":
            scope = (event.owner, "operation", operation_id or event.replay_key)
            key = (event.owner, root_id, "operation", operation_id or event.replay_key)
            aggregates[key].append(event)
            aggregate_scope[scope] = key
            operations.append(event)
            operations_by_root[(event.owner, root_id)].append(event)
        elif explicit_scope == "root" or (explicit_scope is None and root_id is not None):
            scope = (event.owner, "root", root_id or event.replay_key)
            key = (event.owner, root_id, "root", None)
            aggregates[key].append(event)
            aggregate_scope[scope] = key
        elif operation_id is not None:
            scope = (event.owner, "operation", operation_id)
            operations.append(event)
        else:
            scope = (event.owner, "message", event.replay_key)
        grouped[scope].append(event)

    excluded = []
    covered = set()
    ambiguous = set()
    for key, aggregate_rows in aggregates.items():
        _check_projection_interrupt(cancel_event, deadline)
        owner, root_id, kind, operation_id = key
        descendants = []
        if kind == "root":
            descendants.extend(operations_by_root[(owner, root_id)])
            descendants.extend(attempts_by_root[(owner, root_id)])
        else:
            if root_id is None:
                descendants.extend(event for (event_owner, _root, event_operation), values in attempts_by_operation.items()
                                   if event_owner == owner and event_operation == operation_id for event in values)
            else:
                descendants.extend(attempts_by_operation[(owner, root_id, operation_id)])
        if not descendants:
            continue
        current = max(aggregate_rows, key=lambda row: (row.terminal, row.sequence if row.sequence is not None else -1, row.ingested_at))
        is_covered = (getattr(current, "attempt_coverage", None) == "covered"
                      or (getattr(current, "event_metadata", None) or {}).get("covers_attempts") is True)
        if is_covered:
            covered.add(key)
        else:
            ambiguous.add(key)
            excluded.extend(aggregate_rows)

    covered_root_keys = {(key[0], key[1]) for key in covered if key[2] == "root"}
    covered_operation_keys = {(key[0], key[1], key[3]) for key in covered if key[2] == "operation"}

    selected = []
    for scope, rows in grouped.items():
        _check_projection_interrupt(cancel_event, deadline)
        owner, namespace, identifier = scope
        aggregate_key = aggregate_scope.get(scope)
        if aggregate_key in ambiguous:
            continue
        if namespace == "operation":
            if any((owner, getattr(row, "root_operation_id", None)) in covered_root_keys for row in rows):
                excluded.extend(rows)
                continue
        elif namespace == "attempt":
            if any((owner, getattr(row, "root_operation_id", None)) in covered_root_keys
                   or (owner, None, getattr(row, "operation_id", None)) in covered_operation_keys
                   or (owner, getattr(row, "root_operation_id", None), getattr(row, "operation_id", None)) in covered_operation_keys
                   for row in rows):
                excluded.extend(rows)
                continue
        snapshots = [row for row in rows if row.observation_kind != "delta"]
        if snapshots:
            latest = max(
                snapshots,
                key=lambda row: (
                    row.terminal,
                    row.sequence if row.sequence is not None else -1,
                    row.ingested_at,
                ),
            )
            selected.append(latest)
            latest_seq = latest.sequence
            if latest_seq is not None:
                selected.extend(
                    row for row in rows
                    if row.observation_kind == "delta"
                    and row.sequence is not None
                    and row.sequence > latest_seq
                )
        else:
            selected.extend(row for row in rows if row.observation_kind == "delta")
    coverage = "partial_ambiguous" if ambiguous else "complete"
    return StatsProjection(
        tuple(sorted(selected, key=lambda row: row.event_time)),
        tuple(excluded),
        coverage,
    )


def select_admitted_events(events: Iterable[StatsEvent]) -> list[StatsEvent]:
    """Compatibility wrapper returning only projected admitted events."""
    return list(project_admitted_events(events).events)

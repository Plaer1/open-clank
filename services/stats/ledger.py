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
from types import SimpleNamespace

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
EXTRA_METRICS = {"totalTokens", "audioInputTokens", "audioOutputTokens", "imageInputTokens", "imageOutputTokens", "searchRequests"}
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
        return value if 0 <= value <= 2**53 - 1 else None
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
               "estimated", "covers_attempts", "conversation_deleted",
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
    for key in ("metric_coverage", "covered_dispatch_ids", "loss_reasons", "extra_metrics", "raw_metrics"):
        value = metadata.get(key)
        if isinstance(value, (dict, list)):
            result[key] = value
    for key in ("instance_id", "identity_coverage", "dispatch_index", "retry_of_dispatch_id"):
        value = metadata.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
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
    for name in TOKEN_FIELDS:
        if ((metadata.get("metric_coverage") or {}).get(name) or {}).get("state") in {"unavailable", "not_applicable"}:
            token_values[name] = None
    reported = any(value is not None for value in token_values.values())
    coverage = metadata.get("metric_coverage") or {}
    token_states = {f"{name}_state": ((coverage.get(name) or {}).get("state", state) if value is not None else "unavailable") for name, value in token_values.items()}
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
        if db.get_bind().dialect.name == "sqlite":
            # Acquire SQLite's write slot before reading. SELECT inside a
            # SAVEPOINT pins a WAL snapshot which a concurrent writer can
            # invalidate; upgrading it then raises SQLITE_BUSY_SNAPSHOT.
            # Exact-key conflict handling preserves the original immutable
            # observation and does not swallow other constraint failures.
            from sqlalchemy.dialects.sqlite import insert
            columns = {StatsEvent.__mapper__.attrs[key].columns[0]: value for key, value in values.items()}
            db.execute(insert(StatsEvent.__table__).values(columns).on_conflict_do_nothing(index_elements=[StatsEvent.replay_key]))
            event = db.query(StatsEvent).filter(StatsEvent.replay_key == values["replay_key"]).one()
            nested.commit()
            return event
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


def detach_session_events(db, *, owner, session_id):
    """Retain content-free numbers while erasing conversation links atomically.

    The canonical conversation deletion owns this transaction and commits it.
    Clear both cascade foreign keys before deleting messages or their session.
    """
    from sqlalchemy import or_
    from core.database import ChatMessage
    owner = str(owner or "").strip()
    if not owner or not session_id:
        raise ValueError("authoritative owner and session required")
    message_ids = db.query(ChatMessage.id).filter(ChatMessage.session_id == session_id)
    rows = db.query(StatsEvent).filter(StatsEvent.owner == owner,
                                     or_(StatsEvent.session_id == session_id, StatsEvent.message_id.in_(message_ids)))
    count = 0
    for row in rows.yield_per(500):
        row.session_id = None
        row.message_id = None
        row.event_metadata = {**_safe_metadata(row.event_metadata or {}), "conversation_deleted": True}
        count += 1
    return count


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
    metric_payload = metadata.get("metrics")
    metadata["extra_metrics"] = {key: value for key,value in (metric_payload if isinstance(metric_payload,Mapping) else metadata).items() if key in EXTRA_METRICS and _nonnegative(value) is not None}
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
            result.normalization_profile
        ),
        "model_fingerprint": result.model_fingerprint, "source": "operation_router"}
    metadata.update(metric_coverage=getattr(result, "metric_coverage", {}),
                    covered_dispatch_ids=getattr(result, "covered_dispatch_ids", []),
                    loss_reasons=getattr(result, "loss_reasons", []),
                    identity_coverage=getattr(result, "identity_coverage", "partial"))
    metadata["extra_metrics"] = {key: value for key, value in result.usage.items()
                                  if key in EXTRA_METRICS and _nonnegative(value) is not None}
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


def capture_chat_metrics(db, *, owner, context, metrics, outcome="completed"):
    """Content-free final engine usage, independent of optional capture admission.

    Caller proves the active owner/root/journal. This is an operation snapshot,
    never invented per-dispatch facts; existing projection folds it with exact
    attempt observations and any later canonical message/root snapshot.
    """
    if context.get("owner") != owner or not context.get("root_operation_id") or not context.get("operation_id") or not context.get("instance_id"):
        raise ValueError("trusted chat usage context required")
    raw = {key: value for key in TOKEN_FIELDS if (value := _nonnegative(_metric(metrics, key))) is not None}
    metadata = {
        "metrics": raw, "raw_metrics": raw, "terminal": True, "observation_kind": "final_snapshot",
        "sequence": 0, "normalization_profile": metrics.get("normalization_profile"),
        "metric_coverage": metrics.get("metric_coverage") or {},
        "covered_dispatch_ids": metrics.get("covered_dispatch_ids") or [],
        "loss_reasons": metrics.get("loss_reasons") or [],
        "identity_coverage": metrics.get("identity_coverage", "partial"),
        "instance_id": context["instance_id"], "actor_kind": context.get("actor_kind", "foreground"),
        "billing_lane": context.get("billing_lane"), "billable": None,
        "duration_ms": _measured_duration(metrics.get("response_time") * 1000) if isinstance(metrics.get("response_time"), (int, float)) and not isinstance(metrics.get("response_time"), bool) else None,
        "status": "complete" if outcome == "completed" else outcome,
    }
    identity = f"{owner}:{context['instance_id']}:{context['root_operation_id']}:{context['operation_id']}"
    values = _event_values(owner=owner, event_kind="response" if outcome == "completed" else "cancellation" if outcome in {"cancelled", "interrupted"} else "failure",
        event_time=_now(), replay_key=_replay_key("chat_usage", identity, "l01-chat-usage-v1"),
        root_operation_id=context["root_operation_id"], operation_id=context["operation_id"],
        session_id=context.get("session_id"), workspace_id=context.get("workspace_id"),
        provider_id=context.get("provider_id"), account_id=context.get("account_id"), route_id=context.get("route_id"),
        requested_model=context.get("model_id"), actual_model=metrics.get("actual_model"),
        metadata=metadata, source="acp_final_usage", observation_scope="operation")
    return _insert_once(db, values)


def _measured_duration(value):
    import math
    return round(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def capture_attempt_event(db, *, owner, context, attempt_id, payload):
    """One managed transport attempt; wire/SDK snapshots share this identity."""
    if not str(owner or "").strip() or not attempt_id or not context.get("root_operation_id") or not context.get("instance_id"):
        raise ValueError("trusted attempt context required")
    metrics = payload.get("metrics") or {}
    terminal = bool(payload.get("terminal"))
    outcome = payload.get("outcome")
    kind = "response" if outcome == "completed" else "cancellation" if outcome in {"cancelled", "disconnected", "interrupted"} else "failure" if terminal and outcome == "upstream_error" else "provider_attempt"
    metadata = {"metrics": metrics, "status": "complete" if outcome == "completed" else "failed" if terminal and outcome == "upstream_error" else outcome or "dispatch",
                "terminal": terminal, "observation_kind": "final_snapshot" if terminal else "cumulative_snapshot",
                "sequence": payload.get("sequence", 0), "normalization_profile": payload.get("normalization_profile"),
                "billing_lane": context.get("billing_lane"), "actor_kind": context.get("actor_kind", "foreground"),
                "duration_ms": _measured_duration((payload.get("timing") or {}).get("terminal_ms")),
                "retry": bool(payload.get("retry_of_dispatch_id")) or context.get("retry", False), "billable": payload.get("billable")}
    source = str(payload.get("source") or "wire")
    metadata.update({key: payload.get(key) for key in ("metric_coverage", "covered_dispatch_ids", "loss_reasons", "dispatch_index", "retry_of_dispatch_id")})
    metadata.update(instance_id=context["instance_id"], identity_coverage=payload.get("identity_coverage") or context.get("identity_coverage", "partial"),
                    observation_kind=payload.get("observation_kind") or metadata["observation_kind"])
    metadata["extra_metrics"] = {key: value for key, value in metrics.items() if key in EXTRA_METRICS and _nonnegative(value) is not None}
    identity = f"{owner}:{context['instance_id']}:{context['root_operation_id']}:{context.get('operation_id')}:{attempt_id}:{source}:{payload.get('sequence',0)}"
    values = _event_values(owner=owner, event_kind=kind, event_time=_now(),
                          replay_key=_replay_key("attempt", identity, "l02-managed-attempt-v1"),
                          root_operation_id=context.get("root_operation_id"), operation_id=context.get("operation_id"), attempt_id=attempt_id,
                          session_id=context.get("session_id"), workspace_id=context.get("workspace_id"), parent_id=context.get("parent_id"),
                          provider_id=context.get("provider_id"), account_id=context.get("account_id"), route_id=context.get("route_id"),
                          requested_model=context.get("requested_model"), actual_model=payload.get("actual_model") or context.get("actual_model"),
                          metadata=metadata, source=source, observation_scope="attempt")
    return _insert_once(db, values)


@dataclass(frozen=True)
class StatsProjection:
    """Admitted facts plus explicit overlap and coverage evidence."""

    events: tuple[SimpleNamespace, ...]
    excluded: tuple[StatsEvent, ...]
    coverage: str

    def __iter__(self):
        return iter(self.events)

    def __len__(self):
        return len(self.events)


def _order(row):
    return (bool(row.terminal), row.sequence if row.sequence is not None else -1, row.ingested_at or row.event_time)


def _coverage(row, metric):
    metadata = row.event_metadata or {}
    explicit = (metadata.get("metric_coverage") or {}).get(metric)
    if isinstance(explicit, Mapping):
        return dict(explicit)
    return {"state": getattr(row, metric + "_state", "reported"),
            "coverage": "unknown", "source": row.source, "reason": "coverage_not_declared"}


def project_admitted_events(events: Iterable[StatsEvent], *, cancel_event=None, deadline=None) -> StatsProjection:
    """Reconcile each metric independently; raw ORM observations are never mutated.

    A snapshot missing a category cannot erase earlier usable evidence. Sources
    for one dispatch are alternatives. Aggregate overlap is suppressed only
    for the metric and dispatches it actually covers; unknown overlap selects
    one authority and records ambiguity instead of adding incompatible totals.
    """
    grouped = defaultdict(list)
    for row in events:
        _check_projection_interrupt(cancel_event, deadline)
        metadata = row.event_metadata or {}
        namespace = "attempt" if row.attempt_id else "operation" if row.observation_scope == "operation" else "root" if row.root_operation_id and row.observation_scope == "root" else "message"
        identifier = row.attempt_id if namespace == "attempt" else row.operation_id if namespace == "operation" else row.root_operation_id if namespace == "root" else row.replay_key
        grouped[(row.owner, metadata.get("instance_id"), row.root_operation_id, row.operation_id, namespace, identifier)].append(row)
    selected = []
    excluded = []
    ambiguous = False
    partial = False
    for key, rows in grouped.items():
        _check_projection_interrupt(cancel_event, deadline)
        representative = max(rows, key=lambda row: (row.source != "host", _order(row)))
        view = SimpleNamespace(**{column.key: getattr(representative, column.key) for column in StatsEvent.__mapper__.column_attrs})
        durations = [row for row in rows if row.duration_ms is not None]
        if durations:
            view.duration_ms = max(durations, key=_order).duration_ms
        if view.billable is None:
            billable = [row for row in rows if row.billable is not None]
            if billable: view.billable = max(billable, key=_order).billable
        view.event_metadata = dict(representative.event_metadata or {})
        view.event_metadata["metric_coverage"] = {}
        view.event_metadata["metric_provenance"] = {}
        view.event_metadata["loss_reasons"] = sorted({reason for row in rows for reason in (row.event_metadata or {}).get("loss_reasons", [])})
        extra_keys = {name for row in rows for name in (row.event_metadata or {}).get("extra_metrics", {})}
        view.event_metadata["extra_metrics"] = {}
        for metric in list(TOKEN_FIELDS) + sorted(extra_keys):
            candidates = [row for row in rows if (getattr(row, metric, None) if metric in TOKEN_FIELDS else (row.event_metadata or {}).get("extra_metrics", {}).get(metric)) is not None]
            if not candidates:
                if metric in TOKEN_FIELDS:
                    setattr(view, metric, None); setattr(view, metric + "_state", "unavailable")
                continue
            # Compare independent source snapshots only after folding each source.
            authorities = []
            for source in {row.source for row in candidates}:
                source_rows = [row for row in candidates if row.source == source]
                snapshots = [row for row in source_rows if row.observation_kind != "delta"]
                latest = max(snapshots, key=_order) if snapshots else None
                increments = [row for row in source_rows if row.observation_kind == "delta" and (latest is None or row.sequence is not None and latest.sequence is not None and row.sequence > latest.sequence)]
                facts = ([latest] if latest else []) + increments
                authority = max(facts, key=_order)
                value = sum((getattr(row, metric) if metric in TOKEN_FIELDS else row.event_metadata["extra_metrics"][metric]) for row in facts)
                cov = _coverage(authority, metric)
                authorities.append((cov.get("state") == "reported", cov.get("coverage") == "complete", _order(authority), authority, value, cov, facts))
            _, _, _, authority, value, cov, facts = max(authorities, key=lambda item: item[:3])
            if metric in TOKEN_FIELDS:
                setattr(view, metric, value); setattr(view, metric + "_state", cov.get("state", "reported"))
            else:
                view.event_metadata["extra_metrics"][metric] = value
            view.event_metadata["metric_coverage"][metric] = cov
            view.event_metadata["metric_provenance"][metric] = {"replay_keys": [row.replay_key for row in facts], "source": authority.source,
                "normalization_profile": (authority.event_metadata or {}).get("normalization_profile"),
                "covered_dispatch_ids": [authority.attempt_id] if authority.attempt_id else sorted((authority.event_metadata or {}).get("covered_dispatch_ids") or [])}
            partial |= cov.get("coverage") != "complete"
        excluded.extend(row for row in rows if row is not representative)
        selected.append(view)
    for row in selected:
        for metric in EXTRA_METRICS:
            setattr(row,metric,row.event_metadata["extra_metrics"].get(metric))
            setattr(row,metric + "_state", (row.event_metadata["metric_coverage"].get(metric) or {}).get("state", "unavailable"))
    # Operations reconcile their attempts first; roots reconcile the remaining
    # contributions second. No root-as-operation inference is performed.
    for aggregate in sorted(selected, key=lambda row: row.observation_scope == "root"):
        if aggregate.attempt_id or aggregate.observation_scope not in {"operation", "root"}:
            continue
        descendants = [row for row in selected if row is not aggregate and row.owner == aggregate.owner
                       and aggregate.root_operation_id and row.root_operation_id == aggregate.root_operation_id
                       and (row.attempt_id or aggregate.observation_scope == "root" and row.observation_scope == "operation")
                       and (aggregate.observation_scope == "root" or row.operation_id == aggregate.operation_id)]
        covers = set(aggregate.event_metadata.get("covered_dispatch_ids") or [])
        legacy_cover = aggregate.attempt_coverage == "covered" or aggregate.event_metadata.get("covers_attempts") is True
        for metric in list(TOKEN_FIELDS) + sorted(EXTRA_METRICS):
            if getattr(aggregate, metric) is None:
                if any(getattr(row, metric) is not None for row in descendants):
                    setattr(aggregate, metric + "_state", "not_applicable")
                continue
            present = [row for row in descendants if getattr(row, metric) is not None]
            def covered_ids(row):
                return {row.attempt_id} if row.attempt_id else set(row.event_metadata.get("covered_dispatch_ids") or [])
            if covers and any(covers.issubset(covered_ids(row)) and covered_ids(row) != covers for row in present):
                # A broader descendant aggregate already includes this exact
                # subset. Keep its usable total, without inventing subtraction.
                setattr(aggregate, metric, None); setattr(aggregate, metric + "_state", "not_applicable")
                aggregate.event_metadata["metric_coverage"].pop(metric, None)
                aggregate.event_metadata["metric_provenance"].pop(metric, None)
                continue
            known = [row for row in descendants if legacy_cover or covers and covered_ids(row) and covered_ids(row).issubset(covers)]
            unknown = [row for row in present if row not in known and (not covers and not legacy_cover or covers.intersection(covered_ids(row)) or not covered_ids(row))]
            if unknown:
                ambiguous = True
                aggregate.event_metadata["metric_coverage"][metric]["coverage"] = "unknown"
                aggregate.event_metadata["metric_coverage"][metric]["reason"] = "unproven_aggregate_overlap"
            for row in known + unknown:
                setattr(row, metric, None); setattr(row, metric + "_state", "not_applicable")
                row.event_metadata["metric_coverage"].pop(metric, None)
                row.event_metadata["metric_provenance"].pop(metric, None)
    partial = False
    for row in selected:
        partial |= any(entry.get("coverage") != "complete" for entry in row.event_metadata["metric_coverage"].values())
        row.event_metadata["extra_metrics"] = {key:getattr(row,key) for key in EXTRA_METRICS if getattr(row,key) is not None}
        profiles = {entry.get("normalization_profile") for key,entry in row.event_metadata["metric_provenance"].items() if key in TOKEN_FIELDS}
        row.event_metadata["normalization_profile"] = next(iter(profiles)) if len(profiles) == 1 else None
        row.token_state = "estimated" if any(getattr(row, metric + "_state") == "estimated" for metric in TOKEN_FIELDS) else "reported" if any(getattr(row, metric) is not None for metric in TOKEN_FIELDS) else "unavailable"
        partial |= bool(row.attempt_id and row.token_state == "unavailable" and not row.event_metadata.get("extra_metrics") and any(getattr(row,key + "_state") != "not_applicable" for key in ("input_tokens","output_tokens"))) or bool(row.event_metadata["loss_reasons"]) or row.event_metadata.get("identity_coverage") == "partial" or not row.terminal
    return StatsProjection(tuple(sorted(selected, key=lambda row: row.event_time)), tuple(excluded),
                           "partial_ambiguous" if ambiguous else "partial" if partial else "complete")


def read_attempt_measurements(db, *, owner, instance_id, root_operation_id, operation_id, attempt_id):
    rows = db.query(StatsEvent).filter(StatsEvent.owner == owner, StatsEvent.root_operation_id == root_operation_id,
                                     StatsEvent.operation_id == operation_id, StatsEvent.attempt_id == attempt_id).all()
    rows = [row for row in rows if (row.event_metadata or {}).get("instance_id") == instance_id]
    # A validated aggregate can fill a dispatch only when it explicitly
    # covers exactly that dispatch. Never divide multi-request SDK totals.
    if rows:
        aggregates = db.query(StatsEvent).filter(StatsEvent.owner == owner, StatsEvent.root_operation_id == root_operation_id,
                                                StatsEvent.operation_id == operation_id, StatsEvent.attempt_id.is_(None)).all()
        rows.extend(row for row in aggregates if (row.event_metadata or {}).get("covered_dispatch_ids") == [attempt_id])
    projection = project_admitted_events(rows)
    if not projection.events:
        return {"metrics": {}, "metric_coverage": {}, "normalization_profile": None,
                "coverage": "missing", "loss_reasons": [], "provenance": {}}
    metrics, metric_coverage, provenance = {}, {}, {}
    losses = set()
    for row in projection.events:
        for key in list(TOKEN_FIELDS) + sorted(EXTRA_METRICS):
            value = getattr(row,key,None)
            if value is not None:
                metrics[key] = value
                metric_coverage[key] = row.event_metadata["metric_coverage"][key]
                provenance[key] = row.event_metadata["metric_provenance"][key]
        losses.update(row.event_metadata["loss_reasons"])
    profiles = {entry.get("normalization_profile") for key,entry in provenance.items() if key in TOKEN_FIELDS}
    return {"metrics": metrics, "metric_coverage": metric_coverage,
            "normalization_profile": next(iter(profiles)) if len(profiles) == 1 else None,
            "coverage": projection.coverage, "loss_reasons": sorted(losses), "provenance": provenance}


def select_admitted_events(events: Iterable[StatsEvent]) -> list[SimpleNamespace]:
    """Return reconciled facts for readers that do not need the coverage envelope."""
    return list(project_admitted_events(events).events)

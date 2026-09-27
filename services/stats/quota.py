"""Passive, normalized quota observations; never stores provider headers or credentials."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
import re
import hmac
from sqlalchemy import and_, exists, not_
from sqlalchemy.orm import aliased
from uuid import uuid4

_PROVIDER_LABELS = {"openai": "OpenAI", "anthropic": "Anthropic", "google": "Google", "openrouter": "OpenRouter"}

def _provider_label(value):
    return _PROVIDER_LABELS.get(str(value).lower(), "Provider")
from core.stats_models import StatsQuotaObservation, StatsQuotaCycle

class QuotaError(ValueError): pass

_MAX_TEXT = 128
_MAX_LABEL = 256
_MAX_INT = 2**63 - 1
_SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]+$")
_SAFE_ERROR = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
# This is a public domain-separation label, not a credential. Deployments may
# inject a server-local authority through replay_key_authority when they need
# keyed replay identities; deterministic tests use this explicit nonsecret key.
_QUOTA_REPLAY_DOMAIN_KEY = b"openclank.quota.replay.v2"

def _text(value, name, *, limit=_MAX_TEXT):
    if not isinstance(value, str) or not value or len(value) > limit or not _SAFE_TEXT.fullmatch(value):
        raise QuotaError(f"invalid quota {name}")
    return value

def _utc(value):
    if not isinstance(value, datetime): raise QuotaError("observation time must be datetime")
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value

def admit_quota_observation(db, *, owner, account_id, provider_id, window_id, window_kind,
                            observed_at, source_kind, adapter_revision, utilization_numerator=None,
                            utilization_denominator=None, limit_value=None, remaining_value=None,
                            reset_at=None, label=None, operation_id=None, error_class=None,
                            replay_key_authority=_QUOTA_REPLAY_DOMAIN_KEY,
                            binding_operation_id=None):
    owner = _text(owner, "owner")
    account_id = _text(account_id, "account")
    provider_id = _text(provider_id, "provider")
    window_id = _text(window_id, "window")
    window_kind = _text(window_kind, "window kind")
    source_kind = _text(source_kind, "source")
    adapter_revision = _text(adapter_revision, "adapter revision")
    if source_kind not in {"official", "local", "error"}:
        raise QuotaError("unsupported quota source")
    if label is not None:
        label = _text(label, "label", limit=_MAX_LABEL)
    if operation_id is not None:
        operation_id = _text(operation_id, "operation")
    if error_class is not None:
        if not isinstance(error_class, str) or not _SAFE_ERROR.fullmatch(error_class):
            raise QuotaError("invalid quota error class")
    # Quota observations are accepted only for a live canonical account.  The
    # provider metadata uses the same engine/session as Stats, but has its own
    # declarative Base, so keep the import local and avoid coupling startup.
    try:
        from core.provider_models import ProviderAccount, ProviderConnection, ProviderOperationBinding
        account = db.query(ProviderAccount).filter(
            ProviderAccount.id == account_id,
            ProviderAccount.owner == owner,
            ProviderAccount.enabled.is_(True),
            ProviderAccount.deleted_at.is_(None),
        ).first()
        if account is None:
            raise QuotaError("quota account is not an owner-scoped live account")
        connection = db.query(ProviderConnection).filter(
            ProviderConnection.id == account.connection_id,
            ProviderConnection.owner == owner,
            ProviderConnection.enabled.is_(True),
            ProviderConnection.deleted_at.is_(None),
        ).first()
        if connection is None or connection.family_id != provider_id:
            raise QuotaError("quota provider does not match the canonical account")
        if operation_id is not None:
            binding = db.query(ProviderOperationBinding).filter(
                ProviderOperationBinding.owner == owner,
                ProviderOperationBinding.credential_owner == owner,
                ProviderOperationBinding.root_operation_id == (binding_operation_id or operation_id),
                ProviderOperationBinding.connection_id == account.connection_id,
                ProviderOperationBinding.selected_account_id == account_id,
                ProviderOperationBinding.provider_id == provider_id,
                ProviderOperationBinding.state == "committed",
                ProviderOperationBinding.committed_at.isnot(None),
            ).first()
            if binding is None:
                raise QuotaError("quota operation binding is not canonical")
    except QuotaError:
        raise
    except Exception as exc:
        # A missing provider metadata table is a configuration error, not an
        # opportunity to accept an unbound historical observation.
        raise QuotaError("canonical provider metadata is unavailable") from exc
    if window_kind not in {"continuous", "discrete", "credits", "unavailable"}:
        raise QuotaError("unsupported quota window kind")
    for value in (utilization_numerator, utilization_denominator, limit_value, remaining_value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _MAX_INT): raise QuotaError("quota values must be bounded nonnegative integers")
    if utilization_denominator == 0 or (utilization_numerator is not None and utilization_denominator is None): raise QuotaError("invalid utilization fraction")
    if remaining_value is not None and limit_value is not None and remaining_value > limit_value:
        raise QuotaError("remaining exceeds limit")
    observed_at = _utc(observed_at); reset_at = _utc(reset_at) if reset_at is not None else None
    if reset_at is not None and window_kind == "discrete" and reset_at < observed_at:
        raise QuotaError("discrete reset precedes observation")
    normalized = {
        "owner": owner, "account_id": account_id, "provider_id": provider_id,
        "operation_id": operation_id, "window_id": window_id, "window_kind": window_kind,
        "observed_at": observed_at.isoformat(), "source_kind": source_kind,
        "adapter_revision": adapter_revision, "utilization_numerator": utilization_numerator,
        "utilization_denominator": utilization_denominator, "limit_value": limit_value,
        "remaining_value": remaining_value, "reset_at": reset_at.isoformat() if reset_at else None,
        "label": label, "error_class": error_class,
    }
    if not isinstance(replay_key_authority, (bytes, bytearray)) or not replay_key_authority or len(replay_key_authority) > 64:
        raise QuotaError("invalid replay key authority")
    payload = ("openclank.quota.v2\0" + json.dumps(normalized, sort_keys=True, separators=(",", ":"))).encode()
    replay_key = hmac.new(bytes(replay_key_authority), payload, "sha256").hexdigest()
    if db.query(StatsQuotaObservation.id).filter_by(replay_key=replay_key).first(): return None
    state = "official" if source_kind == "official" else ("error" if source_kind == "error" else "local")
    row = StatsQuotaObservation(id=str(uuid4()), replay_key=replay_key, owner=owner, account_id=account_id, provider_id=provider_id, window_id=window_id, window_kind=window_kind, label=label, operation_id=operation_id, utilization_numerator=utilization_numerator, utilization_denominator=utilization_denominator, limit_value=limit_value, remaining_value=remaining_value, reset_at=reset_at, observed_at=observed_at, source_kind=source_kind, adapter_revision=adapter_revision, state=state, error_class=error_class)
    db.add(row)
    reconcile_quota_cycle(db, row)
    db.flush()
    enforce_quota_retention(db, owner=owner)
    return row

def reconcile_quota_cycle(db, observation):
    """Record one idempotent discrete reset cycle; continuous windows never cycle."""
    if observation.window_kind != "discrete" or observation.reset_at is None:
        return None
    # A late/out-of-order sample cannot close a cycle: a newer observation is
    # already the authority for that window and must not be retroactively
    # interpreted as a reset transition.
    if db.query(StatsQuotaObservation.id).filter(
        StatsQuotaObservation.owner == observation.owner,
        StatsQuotaObservation.account_id == observation.account_id,
        StatsQuotaObservation.provider_id == observation.provider_id,
        StatsQuotaObservation.window_id == observation.window_id,
        StatsQuotaObservation.observed_at > observation.observed_at,
    ).first() is not None:
        return None
    prior = db.query(StatsQuotaObservation).filter(
        StatsQuotaObservation.owner == observation.owner,
        StatsQuotaObservation.account_id == observation.account_id,
        StatsQuotaObservation.provider_id == observation.provider_id,
        StatsQuotaObservation.window_id == observation.window_id,
        StatsQuotaObservation.window_kind == "discrete",
        StatsQuotaObservation.observed_at < observation.observed_at,
    ).order_by(StatsQuotaObservation.observed_at.desc()).first()
    if prior is None or prior.reset_at is None or prior.reset_at >= observation.reset_at:
        return None
    # A reset timestamp observed before the previous reset instant is a clock
    # correction; only a later-window sample can complete the prior cycle.
    if observation.observed_at < prior.reset_at:
        return None
    cycle_key = sha256(f"cycle\0{prior.owner}\0{prior.account_id}\0{prior.provider_id}\0{prior.window_id}\0{prior.reset_at.isoformat()}".encode()).hexdigest()
    existing = db.query(StatsQuotaCycle).filter_by(cycle_key=cycle_key).first()
    if existing: return existing
    cycle = StatsQuotaCycle(id=str(uuid4()), owner=prior.owner, account_id=prior.account_id, provider_id=prior.provider_id, window_id=prior.window_id, cycle_key=cycle_key, started_at=prior.observed_at, completed_at=observation.observed_at, source_kind=observation.source_kind, adapter_revision=observation.adapter_revision)
    db.add(cycle); return cycle


def erase_owner_quota(db, owner):
    cycles = db.query(StatsQuotaCycle).filter_by(owner=owner).delete(synchronize_session=False)
    observations = db.query(StatsQuotaObservation).filter_by(owner=owner).delete(synchronize_session=False)
    return {"cycles": cycles, "observations": observations}


def erase_account_quota(db, account_id, *, owner=None):
    query = db.query(StatsQuotaObservation).filter_by(account_id=account_id)
    cycle_query = db.query(StatsQuotaCycle).filter_by(account_id=account_id)
    if owner is not None:
        query = query.filter_by(owner=owner); cycle_query = cycle_query.filter_by(owner=owner)
    observations = query.delete(synchronize_session=False); cycles = cycle_query.delete(synchronize_session=False)
    return {"cycles": cycles, "observations": observations}


def enforce_quota_retention(db, *, owner, max_observations_per_key=1000,
                            max_cycles=1000):
    """Bound passive history while retaining the latest official headline."""
    if not isinstance(max_observations_per_key, int) or max_observations_per_key < 1:
        raise QuotaError("invalid quota observation retention cap")
    if not isinstance(max_cycles, int) or max_cycles < 1:
        raise QuotaError("invalid quota cycle retention cap")
    keys = db.query(StatsQuotaObservation.account_id,
                    StatsQuotaObservation.provider_id,
                    StatsQuotaObservation.window_id).filter_by(owner=owner).distinct().order_by(
                        StatsQuotaObservation.account_id.asc(), StatsQuotaObservation.provider_id.asc(),
                        StatsQuotaObservation.window_id.asc()).limit(10001).all()
    keys_truncated = len(keys) > 10000
    keys = keys[:10000]
    removed = 0
    for account_id, provider_id, window_id in keys:
        base = db.query(StatsQuotaObservation).filter_by(
            owner=owner, account_id=account_id, provider_id=provider_id,
            window_id=window_id)
        count = base.count()
        excess = count - max_observations_per_key
        if excess <= 0:
            continue
        protected = base.filter(StatsQuotaObservation.state == "official").order_by(
            StatsQuotaObservation.observed_at.desc()).first()
        candidate_query = base.filter(StatsQuotaObservation.state != "official").order_by(
            StatsQuotaObservation.observed_at.asc()).with_entities(StatsQuotaObservation.id)
        candidate_ids = [row[0] for row in candidate_query.limit(min(excess, 1000)).all()]
        if len(candidate_ids) < min(excess, 1000):
            official_query = base.filter(StatsQuotaObservation.state == "official",
                                         StatsQuotaObservation.id != (protected.id if protected else "")) \
                .order_by(StatsQuotaObservation.observed_at.asc()).with_entities(StatsQuotaObservation.id)
            candidate_ids.extend(row[0] for row in official_query.limit(min(excess - len(candidate_ids), 1000)).all())
        if candidate_ids:
            db.query(StatsQuotaObservation).filter(StatsQuotaObservation.id.in_(candidate_ids)).delete(
                synchronize_session=False)
            removed += len(candidate_ids)
    cycle_keys = db.query(StatsQuotaCycle.account_id, StatsQuotaCycle.provider_id,
                          StatsQuotaCycle.window_id).filter_by(owner=owner).distinct().order_by(
                              StatsQuotaCycle.account_id.asc(), StatsQuotaCycle.provider_id.asc(),
                              StatsQuotaCycle.window_id.asc()).limit(10001).all()
    cycle_truncated = len(cycle_keys) > 10000
    cycle_removed = 0
    for account_id, provider_id, window_id in cycle_keys[:10000]:
        cycle_query = db.query(StatsQuotaCycle).filter_by(
            owner=owner, account_id=account_id, provider_id=provider_id,
            window_id=window_id)
        excess = max(0, cycle_query.count() - max_cycles)
        cycle_ids = [row[0] for row in cycle_query.order_by(
            StatsQuotaCycle.completed_at.asc()).with_entities(StatsQuotaCycle.id).limit(
                min(excess, 1000)).all()]
        if cycle_ids:
            db.query(StatsQuotaCycle).filter(StatsQuotaCycle.id.in_(cycle_ids)).delete(
                synchronize_session=False)
            cycle_removed += len(cycle_ids)
    return {"observations": removed, "cycles": cycle_removed,
            "truncated": keys_truncated or cycle_truncated}


def quota_snapshot(db, *, owner, account_id=None, session_id=None, period="30d", timezone_name="UTC", now=None, stale_after_seconds=3600):
    from sqlalchemy.exc import OperationalError
    try:
        from core.provider_models import ProviderAccount, ProviderConnection
        configured_query = db.query(ProviderAccount, ProviderConnection.family_id).join(
            ProviderConnection, ProviderConnection.id == ProviderAccount.connection_id
        ).filter(ProviderAccount.owner == owner, ProviderAccount.enabled.is_(True),
                 ProviderAccount.deleted_at.is_(None), ProviderConnection.enabled.is_(True),
                 ProviderConnection.deleted_at.is_(None))
        if account_id is not None:
            configured_query = configured_query.filter(ProviderAccount.id == account_id)
        configured = configured_query.all()
    except OperationalError as exc:
        raise QuotaError("canonical provider metadata is unavailable") from exc
    except Exception as exc:
        raise QuotaError("canonical provider metadata is unavailable") from exc
    query = db.query(StatsQuotaObservation).filter_by(owner=owner)
    snapshot_now = _utc(now or datetime.now(timezone.utc))
    grouped = {}
    truncated_accounts = []
    # Query keys per configured account, with a per-account bound. This keeps
    # sibling accounts visible even when one account has >1000 windows.
    for account, family_id in configured:
        account_query = query.filter(StatsQuotaObservation.account_id == account.id)
        key_count = account_query.with_entities(StatsQuotaObservation.provider_id,
                                                StatsQuotaObservation.window_id).distinct().count()
        if key_count > 1000:
            truncated_accounts.append(account.id)
        key_columns = (StatsQuotaObservation.account_id,
                       StatsQuotaObservation.provider_id,
                       StatsQuotaObservation.window_id)
        official_keys = account_query.filter(StatsQuotaObservation.state == "official").with_entities(
            *key_columns).distinct().order_by(StatsQuotaObservation.provider_id.asc(),
                                              StatsQuotaObservation.window_id.asc()).limit(1000).all()
        remaining = max(0, 1000 - len(official_keys))
        official_row = aliased(StatsQuotaObservation)
        official_key_exists = exists().where(and_(
            official_row.owner == owner,
            official_row.account_id == StatsQuotaObservation.account_id,
            official_row.provider_id == StatsQuotaObservation.provider_id,
            official_row.window_id == StatsQuotaObservation.window_id,
            official_row.state == "official",
        ))
        diagnostic_keys = account_query.filter(StatsQuotaObservation.state != "official").filter(
            not_(official_key_exists)).with_entities(
            *key_columns).distinct().order_by(StatsQuotaObservation.provider_id.asc(),
                                              StatsQuotaObservation.window_id.asc()).limit(remaining).all()
        keys = list(dict.fromkeys(official_keys + diagnostic_keys))
        for key in keys:
            key_query = account_query.filter(StatsQuotaObservation.provider_id == key[1],
                                             StatsQuotaObservation.window_id == key[2])
            official_rows = key_query.filter(StatsQuotaObservation.state == "official").order_by(
                StatsQuotaObservation.observed_at.desc()).limit(1).all()
            diagnostic_rows = key_query.filter(StatsQuotaObservation.state != "official").order_by(
                StatsQuotaObservation.observed_at.desc()).limit(20).all()
            grouped[key] = (official_rows + diagnostic_rows,
                            key_query.filter(StatsQuotaObservation.state != "official").count() > 20)
    now = snapshot_now
    observations = []
    windows_per_account = {}
    for account_key, _provider_key, _window_key in grouped:
        windows_per_account[account_key] = windows_per_account.get(account_key, 0) + 1
    for (account_key, provider_key, window_key), grouped_value in grouped.items():
        rows, diagnostics_truncated = grouped_value
        official = next((row for row in rows if row.state == "official"), None)
        headline = official or next((row for row in rows if row.state != "error"), None) or rows[0]
        diagnostics = [row for row in rows if row.id != headline.id]
        diagnostics = diagnostics[:20]
        age = (now - headline.observed_at).total_seconds()
        item = {"account_id": headline.account_id, "account_label": f"Account {sorted({key[0] for key in grouped}) .index(headline.account_id) + 1}", "provider_id": headline.provider_id, "provider_label": _provider_label(headline.provider_id),
                "window_id": headline.window_id, "window_kind": headline.window_kind,
                "label": headline.label, "limit_value": headline.limit_value,
                "remaining_value": headline.remaining_value,
                # Adapters may designate a comparable headline with the
                # stable primary/headline/5h window id. A lone window is also
                # comparable; unlike windows are never silently maximized.
                "is_headline": windows_per_account.get(account_key) == 1 or str(headline.window_id).lower() in {"primary", "headline", "5h"},
                "state": ("stale" if age > stale_after_seconds else headline.state),
                "utilization_numerator": headline.utilization_numerator,
                "utilization_denominator": headline.utilization_denominator,
                "reset_at": headline.reset_at.isoformat()+"Z" if headline.reset_at else None,
                "observed_at": headline.observed_at.isoformat()+"Z",
                "adapter_revision": headline.adapter_revision,
                "error_class": headline.error_class,
                "diagnostics": [{"state": row.state, "source_kind": row.source_kind,
                                 "observed_at": row.observed_at.isoformat()+"Z",
                                 "utilization_numerator": row.utilization_numerator,
                                 "utilization_denominator": row.utilization_denominator,
                                 "error_class": row.error_class} for row in diagnostics],
                "diagnostics_truncated": diagnostics_truncated}
        comparable = [row for row in diagnostics if row.state == "local"
                      and row.utilization_numerator is not None
                      and row.utilization_denominator == headline.utilization_denominator
                      and row.utilization_denominator is not None]
        if comparable and headline.utilization_numerator is not None:
            row = comparable[0]
            item["signed_delta"] = {"numerator": row.utilization_numerator - headline.utilization_numerator,
                                     "denominator": headline.utilization_denominator}
        observations.append(item)
    seen_accounts = {item["account_id"] for item in observations}
    for account, family_id in configured:
        if account.id not in seen_accounts:
            observations.append({"account_id": account.id, "account_label": "Configured account", "provider_id": family_id, "provider_label": _provider_label(family_id),
                                 "window_id": None, "window_kind": "unavailable",
                                 "state": "unavailable", "utilization_numerator": None,
                                 "utilization_denominator": None, "reset_at": None,
                                 "observed_at": None, "adapter_revision": None,
                                 "label": None, "limit_value": None, "remaining_value": None,
                                 "diagnostics": [], "diagnostics_truncated": False})
    requested_missing = account_id is not None and not configured
    account_order = {account.id: index for index, (account, _family_id) in enumerate(sorted(configured, key=lambda item: item[0].id))}
    timeline = []
    timeline_truncated = False
    for account, _family_id in configured:
        rows = query.filter(StatsQuotaObservation.account_id == account.id,
                            StatsQuotaObservation.observed_at >= now - timedelta(hours=24)) \
            .order_by(StatsQuotaObservation.observed_at.asc()).limit(1001).all()
        if len(rows) > 1000:
            timeline_truncated = True
            rows = rows[:1000]
        for row in rows:
            if row.utilization_numerator is None or row.utilization_denominator in (None, 0):
                continue
            timeline.append({"account_index": account_order[account.id],
                             "provider_id": row.provider_id, "window_id": row.window_id,
                             "observed_at": row.observed_at.isoformat() + "Z",
                             "numerator": row.utilization_numerator,
                             "denominator": row.utilization_denominator,
                             "reset_at": row.reset_at.isoformat() + "Z" if row.reset_at else None})
    capability_providers = sorted({item["provider_id"] for item in observations if item.get("provider_id")})
    capability_windows = sorted({item["window_id"] for item in observations if item.get("window_id")})
    token_volume = []
    token_volume_truncated = False
    try:
        from core.stats_models import StatsEvent
        event_rows = db.query(StatsEvent).filter(
            StatsEvent.owner == owner,
            StatsEvent.event_time >= now - timedelta(hours=24),
        ).order_by(StatsEvent.event_time.desc(), StatsEvent.id.desc()).limit(1001).all()
        token_volume_truncated = len(event_rows) > 1000
        event_rows.reverse()
        for event in event_rows[:1000]:
            if event.input_tokens is None and event.output_tokens is None:
                continue
            token_volume.append({"observed_at": event.event_time.isoformat() + "Z",
                                 "tokens": (event.input_tokens or 0) + (event.output_tokens or 0),
                                 "attributed": bool(event.session_id or event.operation_id)})
    except OperationalError as exc:
        raise QuotaError("stats event timeline is unavailable") from exc
    current_session = {"attributed": False, "tokens": None, "cost": {"state": "unpriced"}}
    if session_id:
        from core.stats_models import StatsEvent
        from services.stats.ledger import select_admitted_events
        session_query = db.query(StatsEvent).filter(StatsEvent.owner == owner, StatsEvent.session_id == session_id).order_by(StatsEvent.event_time.desc(), StatsEvent.id.desc())
        session_rows = session_query.limit(10001).all()
        session_rows.reverse()
        session_has_more = len(session_rows) > 10000
        if session_has_more:
            session_rows = session_rows[:10000]
        projected = select_admitted_events(session_rows)
        token_rows = [row for row in projected if row.token_state in {"reported", "estimated"} and (row.input_tokens is not None or row.output_tokens is not None)]
        if token_rows:
            total = sum((row.input_tokens or 0) + (row.output_tokens or 0) for row in token_rows)
            states = {row.token_state for row in token_rows}
            current_session = {"attributed": True, "tokens": total, "token_state": "estimated" if "estimated" in states else "reported", "cost": {"state": "unpriced"}, "event_count": len(token_rows)}
            if session_has_more:
                current_session["has_more"] = True
                current_session["truncated"] = True
        elif session_has_more:
            current_session["has_more"] = True
            current_session["truncated"] = True
    return {"schema":"open-clank.stats.v1", "scope": {"period": period, "timezone": timezone_name}, "observations":observations,
            "owner_scope": sha256(owner.encode()).hexdigest()[:16],
            "capabilities": {"providers": capability_providers, "windows": capability_windows, "models": []},
            "current_session": current_session,
            "headline_policy": "single-window-per-account; provider-designated headline required for multi-window accounts",
            "account_status": ("unavailable" if requested_missing else "available"),
            "truncation": {"windows": bool(truncated_accounts),
                           "accounts": truncated_accounts,
                           "timeline": timeline_truncated,
                           "token_volume": token_volume_truncated},
            "timeline": timeline, "timeline_truncated": timeline_truncated,
            "token_volume": token_volume, "token_volume_truncated": token_volume_truncated}

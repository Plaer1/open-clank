"""Bounded owner-first S04 Stats query primitives."""

from __future__ import annotations

import csv
import io
import time
import threading
import json
import math
import hashlib
from decimal import Decimal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy.exc import OperationalError

from core.stats_models import StatsEvent
from services.stats.ledger import project_admitted_events
from services.stats.privacy import IdentityCatalog, identity_handle, owner_scope, safe_scope

MAX_FACTS = 100_000
MAX_BUCKETS = 1_000
MAX_PAGE = 100
MAX_TOP = 20
MAX_FILTER_VALUES = 100
TOP_FIELDS = {"workspace_id", "provider_id", "account_id", "route_id", "requested_model", "actual_model", "actor_kind", "source", "status", "observation_scope"}
TOKEN_VALUE_FIELDS = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"}
FORMULA_REVISION = "s04-base-v1"

def _owner_scope(owner: str) -> str:
    return owner_scope(owner)


_PUBLIC_ID_FIELDS = {
    "account_id": "account_id", "provider_id": "provider_id", "workspace_id": "workspace_id",
    "route_id": "route_id", "requested_model": "requested_model", "actual_model": "actual_model",
    "session_id": "session_id",
}


def _identity_catalogs(owner: str, rows: list[dict]) -> dict[str, IdentityCatalog]:
    return {
        field: IdentityCatalog(owner, field, [row.get(field) for row in rows])
        for field in _PUBLIC_ID_FIELDS
        if any(row.get(field) not in (None, "") for row in rows)
    }


def _public_report(report: dict, *, owner: str | None = None, db=None) -> dict:
    """Remove raw Stats identities at the public boundary.

    Query execution retains canonical values locally for filtering.  Responses
    carry owner-bound handles and result-local labels, which routes can resolve
    back to canonical values only after reapplying the authenticated owner.
    """
    source = json.loads(json.dumps(report, ensure_ascii=False, default=str))
    effective_owner = owner or str(source.pop("_owner", ""))
    if not effective_owner and source.get("owner_scope"):
        # Already projected reports are safe to export and must remain stable;
        # an owner hash is not an identity secret from which to re-project.
        return source
    if not effective_owner:
        effective_owner = "public"
    rows = source.get("rows") if isinstance(source.get("rows"), list) else []
    catalog_values = source.pop("_identity_values", {})
    from services.stats.presentation import canonical_labels
    labels = canonical_labels(db, effective_owner) if db is not None else {}
    catalogs = _identity_catalogs(effective_owner, [row for row in rows if isinstance(row, dict)])
    for field, values in catalog_values.items() if isinstance(catalog_values, dict) else ():
        if field in _PUBLIC_ID_FIELDS:
            catalogs[field] = IdentityCatalog(effective_owner, field, values, labels=labels.get(field, {}))
    if db is not None:
        scope_filters = (source.get('scope') or {}).get('filters') or {}
        for field in scope_filters:
            if field in _PUBLIC_ID_FIELDS and hasattr(StatsEvent, field):
                candidates = [r[0] for r in db.query(getattr(StatsEvent, field)).filter(StatsEvent.owner == effective_owner).distinct().limit(MAX_FACTS + 1).all()]
                catalogs[field] = IdentityCatalog(effective_owner, field, candidates, labels=labels.get(field, {}))
    def project(value, key: str | None = None):
        if isinstance(value, dict):
            result = {}
            for child_key, child in value.items():
                if child_key == "owner":
                    continue
                if child_key == "id" and child not in (None, ""):
                    result["handle"] = identity_handle(effective_owner, "session_id", str(child))
                    continue
                if child_key in _PUBLIC_ID_FIELDS and child not in (None, ""):
                    catalog = catalogs.get(child_key)
                    result[child_key.removesuffix("_id") + "_identity"] = (
                        catalog.project(str(child)) if catalog else {
                            "handle": identity_handle(effective_owner, child_key, str(child)),
                            "label": child_key.replace("_", " ").title(),
                        }
                    )
                elif child_key.endswith("_id") and child_key not in {"event_id", "id"} and child not in (None, ""):
                    # Unknown identity columns are still opaque and cannot be
                    # used as a public authority.
                    result[child_key.removesuffix("_id") + "_handle"] = identity_handle(
                        effective_owner, "session_id", str(child))
                else:
                    result[child_key] = project(child, child_key)
            return result
        if isinstance(value, list):
            return [project(item, key) for item in value]
        return value
    projected = project(source)
    if isinstance(source.get("scope"), dict):
        projected["scope"] = safe_scope(effective_owner, source["scope"], catalogs=catalogs)
    projected["owner_scope"] = owner_scope(effective_owner)
    return projected


def _safe_export_report(report: dict) -> dict:
    """Defensively redact caller-supplied reports before JSON/CSV export."""
    scope = report.get("scope") if isinstance(report.get("scope"), dict) else {}
    if report.get("owner_scope") and not scope.get("owner"):
        return report
    return _public_report(report, owner=str(scope.get("owner") or ""))


class StatsQueryError(ValueError):
    pass


@dataclass(frozen=True)
class StatsScope:
    owner: str
    timezone: str
    start_utc: datetime | None
    end_utc: datetime | None
    resolution: str
    period: str = "custom"


def _parse_dt(value: str, tz: ZoneInfo) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise StatsQueryError("invalid ISO datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def parse_scope(*, owner: str, period: str = "7d", timezone_name: str = "UTC",
                start: str | None = None, end: str | None = None,
                resolution: str = "day", now: datetime | None = None) -> StatsScope:
    owner = str(owner or "").strip().lower()
    if not owner:
        raise StatsQueryError("owner scope is required")
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError):
        raise StatsQueryError("timezone must be a valid IANA zone") from None
    if resolution not in {"hour", "day", "week", "month"}:
        raise StatsQueryError("unsupported resolution")
    current = (now or datetime.now(timezone.utc)).astimezone(tz)
    if start is not None or end is not None or period == "custom":
        if not start or not end:
            raise StatsQueryError("custom period requires start and end")
        start_utc, end_utc = _parse_dt(start, tz), _parse_dt(end, tz)
    elif period == "all":
        start_utc = end_utc = None
    elif period in {"today", "3d", "7d", "30d", "90d"}:
        if period == "today":
            local_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
            local_end = local_start + timedelta(days=1)
        else:
            days = int(period[:-1])
            local_end = current
            local_start = current - timedelta(days=days)
        start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None)
        end_utc = local_end.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        raise StatsQueryError("unsupported period")
    if start_utc is not None and end_utc is not None:
        if end_utc <= start_utc:
            raise StatsQueryError("end must be after start")
        if end_utc - start_utc > timedelta(days=366):
            raise StatsQueryError("range exceeds 366 days")
    return StatsScope(owner, timezone_name, start_utc, end_utc, resolution, period)


def _typed(value, *, unit: str = "count", state: str | None = None):
    result = {"value": value, "unit": unit, "state": state or ("reported" if value is not None else "unavailable")}
    if isinstance(value, int) and abs(value) > (2**53 - 1):
        result["value"] = None
        result["exact"] = str(value)
    return result


def _event_state(event, field):
    value = getattr(event, field, None)
    return getattr(event, field + "_state", "reported" if value is not None else "unavailable")


def _aggregate(records, *, empty_known=False):
    """Aggregate values without treating missing or unsupported measurements as zero."""
    records = [(value,state) for value,state in records if state != "not_applicable"]
    total = len(records)
    known = [value for value, state in records if value is not None and state in {"reported", "estimated", "partial"}]
    states = [state for value, state in records if value is not None and state in {"reported", "estimated", "partial"}]
    if not records and empty_known:
        return {"value": 0, "state": "reported", "supported": 0, "total": 0}
    if not known:
        state = "unsupported" if records and all(state == "unsupported" for _value, state in records) else "unavailable"
        return {"value": None, "state": state, "supported": 0, "total": total}
    if len(known) < total or "partial" in states:
        state = "partial"
    elif "estimated" in states:
        state = "estimated"
    else:
        state = "reported"
    return {"value": sum(known), "state": state, "supported": len(known), "total": total}


def temporal_buckets(events, scope: StatsScope, *, field: str = "output_tokens", zero_fill: bool = True,
                     coverage_known: bool = False, cancel_event=None, deadline=None):
    if field not in TOKEN_VALUE_FIELDS:
        raise StatsQueryError("unsupported bucket field: " + field)
    """Group admitted events into timezone-aware calendar buckets."""
    tz = ZoneInfo(scope.timezone)
    values = {}
    for event in events:
        if cancel_event is not None and cancel_event.is_set():
            raise TimeoutError("bucket projection cancelled")
        utc_event = event.event_time.replace(tzinfo=timezone.utc)
        if scope.start_utc is not None and not (scope.start_utc <= utc_event.replace(tzinfo=None) < scope.end_utc):
            continue
        local = utc_event.astimezone(tz)
        if scope.resolution == "hour":
            key = utc_event.replace(minute=0, second=0, microsecond=0).replace(tzinfo=None)
        elif scope.resolution == "day":
            key_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        elif scope.resolution == "week":
            key_local = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            key_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if scope.resolution != "hour":
            key = key_local.astimezone(timezone.utc).replace(tzinfo=None)
        values.setdefault(key, []).append((getattr(event, field, None), _event_state(event, field)))
    if zero_fill and scope.start_utc is not None and scope.end_utc is not None:
        local = scope.start_utc.replace(tzinfo=timezone.utc).astimezone(tz)
        utc_cursor = scope.start_utc.replace(tzinfo=timezone.utc).replace(minute=0, second=0, microsecond=0)
        if scope.resolution == "hour": local = utc_cursor.astimezone(tz)
        elif scope.resolution == "day": local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        elif scope.resolution == "week": local = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        else: local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        count = 0
        while (utc_cursor if scope.resolution == "hour" else local.astimezone(timezone.utc)).replace(tzinfo=None) < scope.end_utc:
            if count >= MAX_BUCKETS:
                raise StatsQueryError("bucket cap exceeded")
            key = (utc_cursor if scope.resolution == "hour" else local.astimezone(timezone.utc)).replace(tzinfo=None)
            values.setdefault(key, [])
            if scope.resolution == "hour":
                utc_cursor += timedelta(hours=1)
                local = utc_cursor.astimezone(tz)
            else:
                local += {"day": timedelta(days=1), "week": timedelta(weeks=1), "month": timedelta(days=31)}[scope.resolution]
            if scope.resolution == "month" and local.day != 1:
                local = local.replace(day=1)
            count += 1
    if len(values) > MAX_BUCKETS:
        raise StatsQueryError("bucket cap exceeded")
    result = []
    for key, vals in sorted(values.items()):
        aggregate = _aggregate(vals, empty_known=coverage_known)
        result.append({"start": key.isoformat() + "Z", "local_start": key.replace(tzinfo=timezone.utc).astimezone(tz).isoformat(), "value": _typed(aggregate["value"], unit="tokens", state=aggregate["state"]), "supported": aggregate["supported"], "total": aggregate["total"], "count": len(vals)})
    return result


def top_n(events, field: str, *, value_field: str = "output_tokens", limit: int = 10):
    if limit < 1 or limit > MAX_TOP:
        raise StatsQueryError("top-n limit must be 1..20")
    if field not in TOP_FIELDS:
        raise StatsQueryError("unsupported grouping field: " + field)
    if value_field not in TOKEN_VALUE_FIELDS:
        raise StatsQueryError("unsupported value field: " + value_field)
    groups = {}
    for event in events:
        raw_key = getattr(event, field, None)
        key = ("Unknown", "unknown") if raw_key is None else (str(raw_key), "value")
        groups.setdefault(key, []).append((getattr(event, value_field, None), _event_state(event, value_field)))
    ranked = sorted(((key, _aggregate(values)["value"], len(values), values) for key, values in groups.items()), key=lambda row: (-(row[1] or 0), str(row[0])))
    result = [{"key": key[0], "kind": key[1], "value": _typed((aggregate := _aggregate(values))["value"], unit="tokens", state=aggregate["state"]), "supported": aggregate["supported"], "total": aggregate["total"], "count": count} for key, _total, count, values in ranked[:limit]]
    if len(ranked) > limit:
        rest = ranked[limit:]
        aggregate = _aggregate([record for row in rest for record in row[3]])
        result.append({"key": "Other", "kind": "other", "value": _typed(aggregate["value"], unit="tokens", state=aggregate["state"]), "supported": aggregate["supported"], "total": aggregate["total"], "count": sum(row[2] for row in rest)})
    return result


def discover_filters(db, scope: StatsScope, field: str, *, filters=None, limit: int = MAX_FILTER_VALUES, cancel_event=None, deadline=None):
    allowed = {"workspace_id", "provider_id", "account_id", "route_id", "requested_model", "actual_model", "actor_kind", "source", "status", "observation_scope"}
    if field not in allowed:
        raise StatsQueryError("unsupported filter: " + field)
    if limit < 1 or limit > MAX_FILTER_VALUES:
        raise StatsQueryError("filter value limit exceeded")
    effective_filters = {key: value for key, value in (filters or {}).items() if key != field}
    projection, fact_truncated = admitted_events(db, scope, filters=effective_filters, cancel_event=cancel_event, deadline=deadline)
    all_values = sorted({getattr(event, field) for event in projection.events if getattr(event, field, None)})
    rows = all_values[:limit + 1]
    values = rows[:limit]
    from services.stats.presentation import canonical_labels
    catalog = IdentityCatalog(scope.owner, field, values, labels=canonical_labels(db, scope.owner).get(field, {})) if field in _PUBLIC_ID_FIELDS else None
    return _public_report({"schema": "open-clank.stats.v1", "field": field,
            "choices": catalog.choices() if catalog else [{"handle": str(value), "label": str(value)} for value in values],
            "truncated": len(rows) > limit,
            "scope": {"owner": scope.owner, "timezone": scope.timezone, "start": scope.start_utc.isoformat() + "Z" if scope.start_utc else None, "end": scope.end_utc.isoformat() + "Z" if scope.end_utc else None, "resolution": scope.resolution, "filters": effective_filters, "excluded_facet": field}, "formula_revision": FORMULA_REVISION, "provenance": {"source": "stats_events", "projection": "s01-ledger"}, "coverage": {"state": "partial_truncated" if fact_truncated else projection.coverage, "truncated": len(rows) > limit or fact_truncated}, "warnings": ["filter_value_cap_reached"] if len(rows) > limit else []}, owner=scope.owner, db=db)


def percentile(values, percentile_value: float = 0.5):
    if isinstance(percentile_value, bool) or not isinstance(percentile_value, (int, float)) or not math.isfinite(float(percentile_value)) or not 0 <= percentile_value <= 1:
        raise StatsQueryError("percentile must be between 0 and 1")
    clean = []
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            decimal_value = Decimal(str(value))
        except Exception as exc:
            raise StatsQueryError("percentile values must be numeric") from exc
        if not decimal_value.is_finite() or decimal_value < 0:
            raise StatsQueryError("percentile values must be finite and nonnegative")
        clean.append(decimal_value)
    clean.sort()
    if not clean:
        return {"value": None, "unit": "tokens", "state": "unavailable", "population": 0, "method": "linear"}
    position = Decimal(len(clean) - 1) * Decimal(str(percentile_value))
    lower, upper = math.floor(position), math.ceil(position)
    value = clean[lower] if lower == upper else clean[lower] + (clean[upper] - clean[lower]) * (position - lower)
    if abs(value) > Decimal(2**53 - 1):
        result = {"value": None, "exact": format(value, "f").removesuffix(".0"), "unit": "tokens", "state": "reported"}
    else:
        result = _typed(int(value) if value == value.to_integral_value() else float(value), unit="tokens")
    result.update({"population": len(clean), "method": "linear", "percentile": percentile_value})
    return result


def prior_period(scope: StatsScope) -> StatsScope:
    if scope.start_utc is None or scope.end_utc is None:
        raise StatsQueryError("prior period requires a bounded range")
    start = scope.start_utc
    end = scope.end_utc
    if scope.period in {"today", "3d", "7d", "30d", "90d"}:
        days = {"today": 1, "3d": 3, "7d": 7, "30d": 30}[scope.period]
        prior_start = (start.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(scope.timezone)) - timedelta(days=days)).astimezone(timezone.utc).replace(tzinfo=None)
        return StatsScope(scope.owner, scope.timezone, prior_start, scope.start_utc, scope.resolution, "custom")
    else:
        duration = scope.end_utc - scope.start_utc
        return StatsScope(scope.owner, scope.timezone, scope.start_utc - duration, scope.start_utc, scope.resolution, "custom")


def session_drilldown(db, scope: StatsScope, *, session_id: str, filters=None, page: int = 1, page_size: int = 100, cancel_event=None, deadline=None):
    if page < 1 or page_size < 1 or page_size > MAX_PAGE:
        raise StatsQueryError("invalid page")
    from dataclasses import replace
    projection, truncated = admitted_events(db, scope, filters=filters, cancel_event=cancel_event, deadline=deadline)
    projection = replace(projection, events=tuple(e for e in projection.events if e.session_id == session_id))
    selected = list(projection.events)[(page - 1) * page_size: page * page_size]
    return _public_report({"schema": "open-clank.stats.v1", "session_id": session_id, "scope": {"owner": scope.owner, "timezone": scope.timezone, "start": scope.start_utc.isoformat() + "Z" if scope.start_utc else None, "end": scope.end_utc.isoformat() + "Z" if scope.end_utc else None, "resolution": scope.resolution, "filters": filters or {}}, "formula_revision": FORMULA_REVISION, "_identity_values": {"session_id": [session_id], "actual_model": [row.actual_model for row in projection.events]}, "rows": [{"id": row.id, "event_time": row.event_time.isoformat() + "Z", "scope": row.observation_scope, "status": row.status, "actual_model": row.actual_model, "session_id": session_id, "output_tokens": _typed(row.output_tokens, unit="tokens", state=_event_state(row, "output_tokens"))} for row in selected], "coverage": {"state": "partial_truncated" if truncated else projection.coverage, "excluded": len(projection.excluded), "truncated": truncated}, "warnings": ["fact_cap_reached"] if truncated else [], "pagination": {"page": page, "page_size": page_size, "pages": max(1, (len(projection.events) + page_size - 1) // page_size)}}, owner=scope.owner, db=db)


def export_json(report: dict, *, max_bytes: int = 5 * 1024 * 1024) -> bytes:
    data = json.dumps(_safe_export_report(report), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > max_bytes:
        raise StatsQueryError("export exceeds 5 MiB")
    return data


def _install_progress_handler(db, cancel_event, deadline):
    raw = db.connection().connection
    if not hasattr(raw, "set_progress_handler"):
        return None
    def interrupted():
        return int((cancel_event is not None and cancel_event.is_set())
                   or (deadline is not None and time.monotonic() >= deadline))
    raw.set_progress_handler(interrupted, 1000)
    return raw


def _event_query(db, scope: StatsScope, filters: dict[str, str] | None = None):
    # Owner is the first predicate before time, dimensions, or aggregation.
    query = db.query(StatsEvent).filter(StatsEvent.owner == scope.owner)
    if scope.start_utc is not None:
        query = query.filter(StatsEvent.event_time >= scope.start_utc, StatsEvent.event_time < scope.end_utc)
    filters = _resolve_filter_handles(db, scope.owner, filters or {})
    allowed = {"workspace_id", "provider_id", "account_id", "route_id", "requested_model",
               "actual_model", "workspace_id", "actor_kind", "source", "status", "observation_scope"}
    unknown = set(filters) - allowed
    if unknown:
        raise StatsQueryError("unsupported filter: " + sorted(unknown)[0])
    for key, value in filters.items():
        query = query.filter(getattr(StatsEvent, key) == value)
    return query.order_by(StatsEvent.event_time, StatsEvent.id)


def _resolve_filter_handles(db, owner: str, filters: dict[str, str]) -> dict[str, str]:
    """Resolve public identity handles only after reapplying owner scope."""
    resolved = dict(filters)
    for field, value in list(resolved.items()):
        if field not in {"account_id", "provider_id", "workspace_id", "route_id",
                         "requested_model", "actual_model"}:
            continue
        if not isinstance(value, str) or not value.startswith(field.removesuffix("_id").replace("_", "-") + "_"):
            continue
        column = getattr(StatsEvent, field)
        candidates = [row[0] for row in db.query(column).filter(
            StatsEvent.owner == owner, column.isnot(None)).distinct().limit(MAX_FILTER_VALUES * 100 + 1).all()]
        from services.stats.privacy import StatsIdentityError
        try:
            resolved[field] = IdentityCatalog(owner, field, candidates).resolve(value)
        except StatsIdentityError as exc:
            raise StatsQueryError("unknown or out-of-scope identity filter") from exc
    return resolved


def query_summary(db, scope: StatsScope, *, filters: dict[str, str] | None = None,
                  page: int = 1, page_size: int = 100, all_rows: bool = False,
                  cancel_event: threading.Event | None = None, deadline: float | None = None):
    if page < 1 or page_size < 1 or page_size > MAX_PAGE:
        raise StatsQueryError("page must be >=1 and page_size must be 1..100")
    projection, truncated = admitted_events(db, scope, filters=filters, cancel_event=cancel_event, deadline=deadline)
    events = list(projection.events)
    total_pages = max(1, (len(events) + page_size - 1) // page_size)
    selected = list(events) if all_rows else ([] if page > total_pages else events[(page - 1) * page_size: page * page_size])
    totals = {}
    for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
        aggregate = _aggregate([(getattr(event, field, None), _event_state(event, field)) for event in events])
        totals[field] = _typed(aggregate["value"], unit="tokens", state=aggregate["state"])
    dispatches = {(event.owner,event.attempt_id): event for event in events if event.attempt_id}
    for event in events:
        for dispatch_id in event.event_metadata.get("covered_dispatch_ids") or []:
            dispatches.setdefault((event.owner,dispatch_id),None)
    billable_known = all(event is not None and event.billable is not None for event in dispatches.values())
    other_metrics = {}
    for key in sorted({key for event in events for key in event.event_metadata.get("extra_metrics", {})}):
        aggregate = _aggregate([(getattr(event,key,None),getattr(event,key + "_state","unavailable")) for event in events])
        other_metrics[key] = _typed(aggregate["value"], unit="requests" if key == "searchRequests" else "tokens", state=aggregate["state"])
    return _public_report({
        "schema": "open-clank.stats.v1",
        "owner_scope": _owner_scope(scope.owner),
        "scope": {"owner": scope.owner, "timezone": scope.timezone,
                   "start": scope.start_utc.isoformat() + "Z" if scope.start_utc else None,
                   "end": scope.end_utc.isoformat() + "Z" if scope.end_utc else None,
                   "resolution": scope.resolution, "filters": filters or {}},
        "formula_revision": FORMULA_REVISION,
        "provenance": {"source": "stats_events", "projection": "s01-ledger", "producer_revisions": sorted({event.producer_revision for event in events})},
        "freshness": {"state": "observed", "ingested_through": max((event.ingested_at for event in events), default=None).isoformat() + "Z" if events and max(event.ingested_at for event in events) else None},
        "units": {field: "tokens" for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")},
        "totals": totals,
        "other_metrics": other_metrics,
        "fact_count": _typed(len(events)),
        "operation_counts": {"logical_operations": len({(event.owner, event.operation_id) for event in events if event.operation_id}),
                             "dispatch_attempts": len(dispatches),
                             "billable_attempts": {"value": sum(event.billable is True for event in dispatches.values()) if billable_known else None,
                                                   "state": "reported" if billable_known else "unavailable"},
                             "method": "distinct_observed_and_explicitly_covered_dispatch_ids"},
        "_identity_values": {field: sorted({getattr(event, field, None) for event in events if getattr(event, field, None)}) for field in _PUBLIC_ID_FIELDS},
        "rows": [{"id": event.id, "event_time": event.event_time.isoformat() + "Z",
                  "session_id": event.session_id, "scope": event.observation_scope,
                  "conversation_state": "deleted" if event.event_metadata.get("conversation_deleted") else "active",
                  "actual_model": event.actual_model, "status": event.status,
                  "output_tokens": _typed(event.output_tokens, unit="tokens",
                                           state=event.output_tokens_state)} for event in selected],
        "pagination": {"page": page, "page_size": len(selected) if all_rows else page_size, "pages": 1 if all_rows else total_pages},
        "coverage": {"state": ("partial_truncated" if truncated else projection.coverage), "excluded": len(projection.excluded),
                      "truncated": truncated},
        "warnings": (["fact_cap_reached"] if truncated else [])
                    + (["overlapping_coverage"] if projection.coverage != "complete" else []),
    }, owner=scope.owner, db=db)


def admitted_events(db, scope: StatsScope, *, filters=None, cancel_event=None, deadline=None):
    """Load the bounded owner-filtered projection used by all base panels."""
    raw = _install_progress_handler(db, cancel_event, deadline)
    try:
        resolved = _resolve_filter_handles(db, scope.owner, filters or {})
        # Validate dimensions, but admit the complete owner/date population
        # before selecting dimensions: filtering raw overlaps changes authority.
        _event_query(db, scope, filters)
        rows = _event_query(db, scope).limit(MAX_FACTS + 1).all()
    except OperationalError as exc:
        if (cancel_event is not None and cancel_event.is_set()) or (deadline is not None and time.monotonic() >= deadline):
            raise TimeoutError("Stats admitted-event query cancelled or deadline exceeded") from exc
        raise
    finally:
        if raw is not None:
            raw.set_progress_handler(None, 0)
    truncated = len(rows) > MAX_FACTS
    projection = project_admitted_events(rows[:MAX_FACTS], cancel_event=cancel_event, deadline=deadline)
    if resolved:
        from dataclasses import replace
        projection = replace(projection, events=tuple(event for event in projection.events
            if all(getattr(event, field) == value for field, value in resolved.items())))
    return projection, truncated


def export_csv(report: dict, *, max_bytes: int = 5 * 1024 * 1024) -> bytes:
    report = _safe_export_report(report)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(["schema", "formula_revision", "owner_scope", "scope_timezone", "scope_start", "scope_end", "scope_resolution", "scope_filters", "event_time", "session_identity", "scope", "model_identity", "status", "output_tokens", "output_tokens_state", "output_tokens_unit"])
    scope = report.get("scope", {})
    scope_filters = json.dumps(scope.get("filters", {}), sort_keys=True, separators=(",", ":"))
    for row in report.get("rows", []):
        token = row.get("output_tokens", {})
        token_value = token.get("value") if token.get("value") is not None else token.get("exact")
        session_identity = row.get("session_identity") or row.get("session_handle")
        model_identity = row.get("actual_model_identity") or row.get("actual_model_handle")
        values = [report.get("schema"), report.get("formula_revision"), report.get("owner_scope"), scope.get("timezone"), scope.get("start"), scope.get("end"), scope.get("resolution"), scope_filters, row.get("event_time"), session_identity, row.get("scope"), model_identity, row.get("status"), token_value, token.get("state"), token.get("unit")]
        safe = ["'" + value if isinstance(value, str) and value[:1] in "=+-@" else value for value in values]
        writer.writerow(safe)
    data = output.getvalue().encode("utf-8")
    if len(data) > max_bytes:
        raise StatsQueryError("export exceeds 5 MiB")
    return data

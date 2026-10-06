"""Exact, owner-independent S03 price authority and cost projection."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from fractions import Fraction
from datetime import datetime, timezone
from collections.abc import Iterable, Mapping

from core.stats_models import StatsEvent, StatsPriceSchedule

PRICE_CATEGORIES = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "web_search_credits",
)
USAGE_UNITS = {category: ("credits" if category == "web_search_credits" else "tokens") for category in PRICE_CATEGORIES}


class PricingError(ValueError):
    pass


def _check_pricing_interrupt(cancel_event=None, deadline=None):
    if cancel_event is not None and cancel_event.is_set():
        raise TimeoutError("pricing projection cancelled")
    if deadline is not None and __import__("time").monotonic() >= deadline:
        raise TimeoutError("pricing projection deadline exceeded")


@dataclass(frozen=True)
class InclusionProfile:
    """Versioned adapter declaration preventing overlapping token categories."""
    version: str
    included_categories: frozenset[str]
    input_includes_cache_read: bool = False
    input_includes_cache_write: bool = False
    output_includes_reasoning: bool = False
    reasoning_billing: str = "unsupported"
    billable_categories: frozenset[str] = frozenset(PRICE_CATEGORIES)
    metric_profiles: Mapping[str, "InclusionProfile"] | None = None

    def __post_init__(self):
        if (not self.version or not self.included_categories.issubset(PRICE_CATEGORIES)
                or not self.billable_categories.issubset(PRICE_CATEGORIES)
                or self.reasoning_billing not in {"included_in_output", "separate", "unsupported"}):
            raise PricingError("invalid inclusion profile")


def resolve_profile(event: StatsEvent, profiles: Mapping[str, InclusionProfile]) -> InclusionProfile | None:
    """Resolve normalization only from persisted producer/adapter evidence."""
    metadata = event.event_metadata if isinstance(event.event_metadata, Mapping) else {}
    provenance = metadata.get("metric_provenance") or {}
    versions = {key: entry.get("normalization_profile") for key, entry in provenance.items() if key in PRICE_CATEGORIES}
    if len(set(versions.values())) > 1:
        selections = {key: resolve_profile(SimpleNamespace(event_metadata={"normalization_profile": version}), profiles)
                      for key, version in versions.items()}
        return InclusionProfile("reconciled-per-metric-v1", frozenset(PRICE_CATEGORIES),
                                metric_profiles={key: value for key, value in selections.items() if value is not None})
    version = metadata.get("normalization_profile")
    if not isinstance(version, str) or not version:
        return None
    profile = profiles.get(version)
    if profile is None:
        categories = frozenset(PRICE_CATEGORIES)
        known = {
            "sdk-embedding-input-v1": InclusionProfile("sdk-embedding-input-v1", frozenset({"input_tokens"})),
            "acp-inclusive-input-separated-output-v1": InclusionProfile("acp-inclusive-input-separated-output-v1", categories, True, True, False, "separate"),
            "acp-separated-v1": InclusionProfile("acp-separated-v1", categories, reasoning_billing="separate"),
            "managed-sdk-inclusive-v1": InclusionProfile("managed-sdk-inclusive-v1", categories, True, True, True, "separate"),
            "openai-wire-inclusive-v1": InclusionProfile("openai-wire-inclusive-v1", frozenset({"input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens"}), True, False, True, "included_in_output"),
            "anthropic-wire-separated-v1": InclusionProfile("anthropic-wire-separated-v1", frozenset({"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"})),
        }
        profile = known.get(version)
    return profile if isinstance(profile, InclusionProfile) else None


@dataclass(frozen=True)
class PriceComponent:
    category: str
    currency: str | None
    state: str
    amount: Fraction | None
    schedule_id: str | None = None
    revision: str | None = None
    reason: str | None = None

    def serialized(self) -> dict:
        payload = {"category": self.category, "currency": self.currency, "currency_unit": "currency_major", "usage_unit": USAGE_UNITS[self.category], "state": self.state,
                   "schedule_id": self.schedule_id, "revision": self.revision}
        if self.amount is None:
            payload["amount"] = None
        else:
            payload["amount"] = str(self.amount.numerator) if self.amount.denominator == 1 else f"{self.amount.numerator}/{self.amount.denominator}"
        if self.reason:
            payload["reason"] = self.reason
        return payload


def admit_schedule(db, *, schedule_id: str, provider_id: str, billing_lane: str,
                   model_identity: str | None, route_id: str | None, token_category: str,
                   currency: str, rate_numerator: int, rate_denominator: int,
                   rate_unit: str = "currency_major_per_token",
                   effective_start: datetime, effective_end: datetime | None,
                   source_url: str, source_hash: str, admission_revision: str,
                   retrieved_at: datetime | None = None) -> StatsPriceSchedule:
    """Persist one immutable schedule row; admission never fetches a source."""
    if not schedule_id or not provider_id or not billing_lane or token_category not in PRICE_CATEGORIES:
        raise PricingError("schedule identity/category is invalid")
    if not isinstance(effective_start, datetime) or (effective_end is not None and not isinstance(effective_end, datetime)):
        raise PricingError("effective dates must be datetime values")
    if effective_end is not None and (effective_start.tzinfo is not None) != (effective_end.tzinfo is not None):
        raise PricingError("effective dates must use the same timezone mode")
    if not currency or not source_url or not source_hash or not admission_revision:
        raise PricingError("schedule provenance is required")
    if model_identity is None and route_id is None:
        raise PricingError("schedule requires an exact model or route selector")
    expected_unit = "currency_major_per_credit" if token_category == "web_search_credits" else "currency_major_per_token"
    if rate_unit != expected_unit:
        raise PricingError("unsupported rate unit")
    if not isinstance(rate_numerator, int) or isinstance(rate_numerator, bool) or rate_numerator < 0:
        raise PricingError("rate numerator must be a nonnegative integer")
    if not isinstance(rate_denominator, int) or isinstance(rate_denominator, bool) or rate_denominator <= 0:
        raise PricingError("rate denominator must be a positive integer")
    if effective_end is not None and effective_end <= effective_start:
        raise PricingError("effective interval must be half-open and nonempty")
    if effective_start.tzinfo is not None:
        effective_start = effective_start.astimezone(timezone.utc).replace(tzinfo=None)
        if effective_end is not None:
            effective_end = effective_end.astimezone(timezone.utc).replace(tzinfo=None)
    if retrieved_at is not None and not isinstance(retrieved_at, datetime):
        raise PricingError("retrieval date must be datetime")
    if retrieved_at is not None and retrieved_at.tzinfo is not None:
        retrieved_at = retrieved_at.astimezone(timezone.utc).replace(tzinfo=None)
    if db.query(StatsPriceSchedule.id).filter(StatsPriceSchedule.id == schedule_id).first() is not None:
        raise PricingError("price schedules are immutable")
    existing = db.query(StatsPriceSchedule).filter(
        StatsPriceSchedule.active.is_(True), StatsPriceSchedule.provider_id == provider_id,
        StatsPriceSchedule.billing_lane == billing_lane, StatsPriceSchedule.token_category == token_category,
        StatsPriceSchedule.currency == currency,
    ).all()
    for prior in existing:
        model_compatible = model_identity is None or prior.model_identity is None or model_identity == prior.model_identity
        route_compatible = route_id is None or prior.route_id is None or route_id == prior.route_id
        if not (model_compatible and route_compatible):
            continue
        overlaps_end = effective_end is None or prior.effective_start < effective_end
        overlaps_start = prior.effective_end is None or effective_start < prior.effective_end
        if overlaps_end and overlaps_start:
            raise PricingError("active price schedules overlap")
    row = StatsPriceSchedule(id=schedule_id, provider_id=provider_id, billing_lane=billing_lane,
        model_identity=model_identity, route_id=route_id, token_category=token_category,
        currency=currency, rate_numerator=rate_numerator, rate_denominator=rate_denominator,
        rate_unit=rate_unit,
        effective_start=effective_start, effective_end=effective_end, source_url=source_url,
        source_hash=source_hash, admission_revision=admission_revision,
        retrieved_at=retrieved_at, active=True)
    db.add(row)
    return row


def _schedule_index(schedules, cancel_event=None, deadline=None):
    index = {}
    for schedule in schedules:
        _check_pricing_interrupt(cancel_event, deadline)
        index.setdefault((schedule.provider_id, schedule.billing_lane, schedule.token_category, schedule.model_identity, schedule.route_id), []).append(schedule)
    return index


def _schedule_for(event: StatsEvent, schedules: Iterable[StatsPriceSchedule] | Mapping, category: str, cancel_event=None, deadline=None):
    metadata = event.event_metadata if isinstance(event.event_metadata, Mapping) else {}
    lane = metadata.get("billing_lane")
    if not isinstance(lane, str) or not lane:
        return None
    if isinstance(schedules, Mapping):
        keys = ((event.provider_id, lane, category, event.actual_model, event.route_id),
                (event.provider_id, lane, category, event.actual_model, None),
                (event.provider_id, lane, category, None, event.route_id))
        candidates = []
        for key in dict.fromkeys(keys):
            _check_pricing_interrupt(cancel_event, deadline)
            candidates.extend(schedules.get(key, ()))
    else:
        candidates = schedules
    matches = []
    for schedule in candidates:
        _check_pricing_interrupt(cancel_event, deadline)
        if not schedule.active or schedule.provider_id != event.provider_id or schedule.billing_lane != lane:
            continue
        if schedule.token_category != category:
            continue
        if schedule.route_id is not None:
            if schedule.route_id != event.route_id:
                continue
            if schedule.model_identity is not None and schedule.model_identity != event.actual_model:
                continue
        elif schedule.model_identity != event.actual_model:
            continue
        try:
            if event.event_time < schedule.effective_start or (schedule.effective_end is not None and event.event_time >= schedule.effective_end):
                continue
        except TypeError as exc:
            raise PricingError("event and schedule dates must use the same timezone mode") from exc
        matches.append(schedule)
    if len(matches) > 1:
        raise PricingError("multiple active schedules match one event")
    return matches[0] if matches else None


def _billed(event: StatsEvent, category: str) -> PriceComponent | None:
    metadata = event.event_metadata if isinstance(event.event_metadata, Mapping) else {}
    billed = metadata.get("billed_charge")
    if not isinstance(billed, Mapping):
        return None
    item = billed.get(category)
    if not isinstance(item, Mapping) or not isinstance(item.get("amount_minor"), int) or isinstance(item.get("amount_minor"), bool):
        return None
    currency = item.get("currency")
    if not isinstance(currency, str) or not currency:
        return None
    exponent = item.get("currency_exponent")
    if not isinstance(exponent, int) or isinstance(exponent, bool) or exponent < 0 or exponent > 9:
        return None
    return PriceComponent(category, currency, "billed", Fraction(item["amount_minor"], 10 ** exponent), reason="provider_reported")


def _normalized_counts(event: StatsEvent, profile: InclusionProfile | None) -> dict[str, tuple[int | None, str, str | None]]:
    """Normalize overlapping usage categories once for pricing and cache formulas."""
    result = {}
    for category in PRICE_CATEGORIES:
        value = getattr(event, category, None)
        state = getattr(event, category + "_state", "unavailable")
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**53 - 1):
            result[category] = (None, "unpriced", "invalid_count")
            continue
        result[category] = (value, state, None)
    if profile is None:
        return result
    if profile.metric_profiles is not None:
        for category in PRICE_CATEGORIES:
            if result[category][0] is not None and category not in profile.metric_profiles:
                result[category] = (None, "unpriced", "missing_inclusion_profile")
    def subtract(base, parts, category):
        if base is None or result[category][1] not in {"reported", "estimated"} or any(result[item][0] is None or result[item][1] not in {"reported", "estimated"} for item in parts):
            return (None, "unpriced", "missing_required_subset")
        remainder = base - sum(result[item][0] for item in parts)
        if remainder < 0:
            return (None, "unpriced", "negative_normalized_count")
        state = "estimated" if result[category][1] == "estimated" or any(result[item][1] == "estimated" for item in parts) else "reported"
        return (remainder, state, None)
    input_profile = profile.metric_profiles.get("input_tokens") if profile.metric_profiles is not None else profile
    output_profile = profile.metric_profiles.get("output_tokens") if profile.metric_profiles is not None else profile
    reasoning_profile = profile.metric_profiles.get("reasoning_tokens") if profile.metric_profiles is not None else profile
    provenance = (event.event_metadata or {}).get("metric_provenance") or {}
    def compatible(parts, category):
        coverage = provenance.get(category, {}).get("covered_dispatch_ids")
        return coverage is None or all(provenance.get(part, {}).get("covered_dispatch_ids") == coverage for part in parts)
    if input_profile and (input_profile.input_includes_cache_read or input_profile.input_includes_cache_write):
        parts = []
        if input_profile.input_includes_cache_read: parts.append("cache_read_tokens")
        if input_profile.input_includes_cache_write: parts.append("cache_write_tokens")
        result["input_tokens"] = subtract(result["input_tokens"][0], parts, "input_tokens") if compatible(parts,"input_tokens") else (None,"unpriced","incomplete_subset_dispatch_coverage")
    if output_profile and output_profile.output_includes_reasoning and output_profile.reasoning_billing == "separate":
        result["output_tokens"] = subtract(result["output_tokens"][0], ["reasoning_tokens"], "output_tokens") if compatible(["reasoning_tokens"],"output_tokens") else (None,"unpriced","incomplete_subset_dispatch_coverage")
    if reasoning_profile and reasoning_profile.reasoning_billing == "included_in_output":
        result["reasoning_tokens"] = (None, "not_applicable", "included_in_output")
    return result


def price_event(event: StatsEvent, schedules: Iterable[StatsPriceSchedule] | Mapping, *, inclusion_profile: InclusionProfile | None = None, cancel_event=None, deadline=None) -> tuple[PriceComponent, ...]:
    present = {category for category in PRICE_CATEGORIES if getattr(event, category, None) is not None}
    components = []
    for category in PRICE_CATEGORIES:
        billed = _billed(event, category)
        if billed is not None:
            components.append(billed)
            continue
    if not present and not components:
        return ()
    if present and inclusion_profile is None:
        return tuple(components) + tuple(PriceComponent(category, None, "unpriced", None, reason="missing_inclusion_profile") for category in present if category not in {component.category for component in components})
    normalized = _normalized_counts(event, inclusion_profile)
    blocked = set()
    missing_profiles = set()
    for category in present:
        selected_profile = inclusion_profile.metric_profiles.get(category) if inclusion_profile and inclusion_profile.metric_profiles is not None else inclusion_profile
        if selected_profile is None:
            missing_profiles.add(category)
        elif category not in selected_profile.included_categories or category not in selected_profile.billable_categories or category == "reasoning_tokens" and selected_profile.reasoning_billing in {"included_in_output", "unsupported"}:
            blocked.add(category)
    for category in PRICE_CATEGORIES:
        if category in {component.category for component in components}:
            continue
        value, state, normalization_reason = normalized[category]
        if getattr(event, category + "_state", None) == "not_applicable":
            continue
        if category in missing_profiles:
            components.append(PriceComponent(category, None, "unpriced", None, reason="missing_inclusion_profile"))
            continue
        if normalization_reason:
            components.append(PriceComponent(category, None, state, None, reason=normalization_reason))
            continue
        if category in blocked:
            components.append(PriceComponent(category, None, "not_applicable", None, reason="category_included_or_nonbillable"))
            continue
        if value is None or state not in {"reported", "estimated"}:
            components.append(PriceComponent(category, None, "unpriced", None, reason="unsupported_or_missing_category"))
            continue
        schedule = _schedule_for(event, schedules, category, cancel_event, deadline)
        if schedule is None:
            components.append(PriceComponent(category, None, "unpriced", None, reason="no_exact_effective_schedule"))
        else:
            amount = Fraction(value * schedule.rate_numerator, schedule.rate_denominator)
            components.append(PriceComponent(category, schedule.currency, "estimated", amount,
                                              schedule.id, schedule.admission_revision))
    return tuple(components)


def project_cost(events: Iterable[StatsEvent], schedules: Iterable[StatsPriceSchedule], *, profiles: Mapping[str, InclusionProfile] | None = None, cancel_event=None, deadline=None) -> dict:
    """Return separate billed/estimated currency totals and explicit unpriced coverage."""
    schedules = tuple(schedules)
    schedule_index = _schedule_index(schedules, cancel_event, deadline)
    totals: dict[str, dict[str, Fraction]] = {"billed": {}, "estimated": {}}
    components = []
    for event in events:
        _check_pricing_interrupt(cancel_event, deadline)
        event_components = price_event(event, schedule_index, inclusion_profile=resolve_profile(event, profiles or {}), cancel_event=cancel_event, deadline=deadline)
        components.extend(event_components)
        for component in event_components:
            if component.amount is not None:
                totals[component.state].setdefault(component.currency, Fraction(0))
                totals[component.state][component.currency] += component.amount
    return {"formula_revision": "s03-exact-v1", "totals": {
                state: {currency: (str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}")
                        for currency, value in values.items()} for state, values in totals.items()},
            "components": [component.serialized() for component in components],
            "coverage": {"total_components": len(components),
                         "billed": sum(component.state == "billed" for component in components),
                         "estimated": sum(component.state == "estimated" for component in components),
                         "unpriced": sum(component.state == "unpriced" for component in components)}}


def cost_envelope(events: Iterable[StatsEvent], schedules: Iterable[StatsPriceSchedule], *, inclusion_profile: InclusionProfile | None = None, profiles: Mapping[str, InclusionProfile] | None = None, scope: Mapping | None = None, cancel_event=None, deadline=None) -> dict:
    """Common versioned cost payload consumed by later Stats resources."""
    events = tuple(events)
    report = project_cost(events, schedules, profiles=profiles, cancel_event=cancel_event, deadline=deadline) if profiles is not None else project_cost(events, schedules, cancel_event=cancel_event, deadline=deadline) if inclusion_profile is None else _project_with_profile(events, schedules, inclusion_profile)
    measurements_partial = any((event.event_metadata or {}).get("loss_reasons") or
                               any(value.get("coverage") != "complete" for value in (event.event_metadata or {}).get("metric_coverage", {}).values())
                               for event in events)
    report["coverage"]["measurement"] = "partial" if measurements_partial else "complete"
    report["coverage"]["state"] = "partial_unpriced" if report["coverage"]["unpriced"] else "partial_measurement" if measurements_partial else "complete"
    report.update({"schema": "open-clank.stats.v1", "scope": dict(scope or {}),
                   "formula_revision": "s03-exact-v1", "provenance": {"source": "stats_events", "price_authority": "stats_price_schedules"},
                   "warnings": ["unpriced_categories"] if report["coverage"]["unpriced"] else []})
    return report


def _project_with_profile(events, schedules, profile):
    components = [component for event in events for component in price_event(event, schedules, inclusion_profile=profile)]
    totals = {"billed": {}, "estimated": {}}
    for component in components:
        if component.amount is not None:
            totals[component.state].setdefault(component.currency, Fraction(0))
            totals[component.state][component.currency] += component.amount
    return {"totals": {state: {currency: str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}" for currency, value in values.items()} for state, values in totals.items()}, "components": [component.serialized() for component in components], "coverage": {"total_components": len(components), "billed": sum(c.state == "billed" for c in components), "estimated": sum(c.state == "estimated" for c in components), "unpriced": sum(c.state == "unpriced" for c in components)}}


def cache_rate(events: Iterable[StatsEvent], *, inclusion_profile: InclusionProfile | None = None, profiles: Mapping[str, InclusionProfile] | None = None, cancel_event=None, deadline=None) -> dict:
    """Frozen observed formula: cache reads / (cache reads + uncached input)."""
    events = tuple(event for event in events if not (getattr(event,"input_tokens_state",None) == "not_applicable" and getattr(event,"output_tokens_state",None) == "not_applicable"))
    if profiles is not None:
        resolved = []
        for event in events:
            profile = resolve_profile(event, profiles)
            if profile is None:
                return {"value": None, "state": "unavailable", "formula_revision": "s03-cache-v1", "reason": "unknown_inclusion_profile"}
            resolved.append((event, profile))
    elif inclusion_profile is None:
        return {"value": None, "state": "unavailable", "formula_revision": "s03-cache-v1", "reason": "missing_inclusion_profile"}
    values = []
    for item in (resolved if profiles is not None else ((event, inclusion_profile) for event in events)):
        _check_pricing_interrupt(cancel_event, deadline)
        event, event_profile = item
        normalized = _normalized_counts(event, event_profile)
        read, read_state, _ = normalized["cache_read_tokens"]
        _raw_input, _raw_state, _ = normalized["input_tokens"]
        uncached, input_state, _ = normalized["input_tokens"]
        if read is None or uncached is None or read_state not in {"reported", "estimated"} or input_state not in {"reported", "estimated"}:
            return {"value": None, "state": "unavailable", "formula_revision": "s03-cache-v1", "reason": "missing_typed_measurement"}
        values.append((read, uncached, read_state == "estimated" or input_state == "estimated"))
    if any(read is None or uncached is None for read, uncached, _estimated in values):
        return {"value": None, "state": "unavailable", "formula_revision": "s03-cache-v1"}
    denominator = sum(read + uncached for read, uncached, _estimated in values)
    ratio = Fraction(sum(read for read, _uncached, _estimated in values), denominator) if denominator else None
    estimated = any(_estimated for _read, _uncached, _estimated in values)
    return {"value": None if ratio is None else (str(ratio.numerator) if ratio.denominator == 1 else f"{ratio.numerator}/{ratio.denominator}"), "state": "estimated" if estimated and denominator else "reported" if denominator else "unavailable", "formula_revision": "s03-cache-v1"}


def cache_counterfactual(events: Iterable[StatsEvent], schedules: Iterable[StatsPriceSchedule], *, inclusion_profile: InclusionProfile | None = None, profiles: Mapping[str, InclusionProfile] | None = None, cancel_event=None, deadline=None) -> dict:
    """Compare observed cache pricing with uncached input at the same revision."""
    events = tuple(event for event in events if not (getattr(event,"input_tokens_state",None) == "not_applicable" and getattr(event,"output_tokens_state",None) == "not_applicable"))
    if profiles is not None:
        events = tuple(events)
    elif inclusion_profile is None:
        return {"state": "unavailable", "reason": "missing_inclusion_profile", "formula_revision": "s03-cache-v1"}
    schedules = tuple(schedules)
    schedule_index = _schedule_index(schedules, cancel_event, deadline)
    observed: dict[str, Fraction] = {}
    uncached: dict[str, Fraction] = {}
    used_schedules = set()
    used_profiles = set()
    for event in events:
        _check_pricing_interrupt(cancel_event, deadline)
        event_profile = resolve_profile(event, profiles) if profiles is not None else inclusion_profile
        if event_profile is None:
            return {"state": "unavailable", "reason": "unknown_inclusion_profile", "formula_revision": "s03-cache-v1"}
        normalized = _normalized_counts(event, event_profile)
        used_profiles.add(event_profile.version)
        input_value, input_state, reason = normalized["input_tokens"]
        read_value, read_state, _ = normalized["cache_read_tokens"]
        write_value, write_state, _ = normalized["cache_write_tokens"]
        if input_value is None or read_value is None or write_value is None or input_state not in {"reported", "estimated"} or read_state not in {"reported", "estimated"} or write_state not in {"reported", "estimated"}:
            return {"state": "unavailable", "reason": "missing_typed_measurement", "formula_revision": "s03-cache-v1"}
        input_schedule = _schedule_for(event, schedule_index, "input_tokens", cancel_event, deadline)
        read_schedule = _schedule_for(event, schedule_index, "cache_read_tokens", cancel_event, deadline)
        write_schedule = _schedule_for(event, schedule_index, "cache_write_tokens", cancel_event, deadline)
        if input_schedule is None or read_schedule is None or write_schedule is None:
            return {"state": "unavailable", "reason": "unpriced_cache_category", "formula_revision": "s03-cache-v1"}
        used_schedules.update(((input_schedule.id, input_schedule.admission_revision), (read_schedule.id, read_schedule.admission_revision), (write_schedule.id, write_schedule.admission_revision)))
        if len({input_schedule.currency, read_schedule.currency, write_schedule.currency}) != 1:
            return {"state": "unavailable", "reason": "incomparable_cache_currencies", "formula_revision": "s03-cache-v1"}
        currency = input_schedule.currency
        uncached[currency] = uncached.get(currency, Fraction(0)) + Fraction((input_value + read_value + write_value) * input_schedule.rate_numerator, input_schedule.rate_denominator)
        observed[currency] = observed.get(currency, Fraction(0)) + Fraction(input_value * input_schedule.rate_numerator, input_schedule.rate_denominator)
        observed[read_schedule.currency] = observed.get(read_schedule.currency, Fraction(0)) + Fraction(read_value * read_schedule.rate_numerator, read_schedule.rate_denominator)
        observed[write_schedule.currency] = observed.get(write_schedule.currency, Fraction(0)) + Fraction(write_value * write_schedule.rate_numerator, write_schedule.rate_denominator)
    def exact(value):
        return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"
    return {"state": "estimated", "currencies": {currency: {"observed": exact(observed.get(currency, Fraction(0))), "uncached": exact(uncached.get(currency, Fraction(0))), "savings": exact(uncached.get(currency, Fraction(0)) - observed.get(currency, Fraction(0)))} for currency in sorted(set(observed) | set(uncached))}, "formula_revision": "s03-cache-v1", "provenance": {"schedules": [{"id": sid, "revision": revision} for sid, revision in sorted(used_schedules)], "profiles": sorted(used_profiles)}}

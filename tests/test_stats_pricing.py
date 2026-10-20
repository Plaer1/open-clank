from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from core.stats_models import StatsEvent, StatsPriceSchedule
from services.stats.pricing import InclusionProfile, PricingError, admit_schedule, cache_counterfactual, cache_rate, price_event, project_cost


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pricing.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _event(**kwargs):
    values = dict(id="e", replay_key="e", owner="alice", event_kind="response",
                  observation_scope="message", event_time=datetime(2026, 1, 1, 12),
                  source="test", producer_revision="test", status="complete",
                  event_metadata={"billing_lane": "api"}, provider_id="p",
                  actual_model="model-a", route_id="route-a", input_tokens=3,
                  input_tokens_state="reported")
    values.update(kwargs)
    return StatsEvent(**values)


def _cache_fixture(db, *, model="model-a", currency="USD"):
    categories = frozenset({"input_tokens", "cache_read_tokens", "cache_write_tokens"})
    for category, rate in zip(sorted(categories), (2, 3, 1)):
        admit_schedule(db, schedule_id=f"{model}-{currency}-{category}", provider_id="p",
                       billing_lane="api", model_identity=model, route_id=None,
                       token_category=category, currency=currency, rate_numerator=rate,
                       rate_denominator=1, effective_start=datetime(2026, 1, 1),
                       effective_end=None, source_url="https://example.invalid/fictional-rates",
                       source_hash="fixture-only", admission_revision="fixture-r1")
    db.commit()
    profile = InclusionProfile("fixture-inclusive-v1", categories, input_includes_cache_read=True,
                               input_includes_cache_write=True, billable_categories=categories)
    event = _event(actual_model=model, input_tokens=100, cache_read_tokens=40, cache_write_tokens=10,
                   cache_read_tokens_state="reported", cache_write_tokens_state="reported")
    return profile, event


@pytest.mark.parametrize("bad", [-1, True, 0.5, 2**53, None, 101])
def test_invalid_cache_subset_never_becomes_a_charge_or_ratio(tmp_path, bad):
    db = _db(tmp_path)
    profile, event = _cache_fixture(db)
    event.cache_read_tokens = bad
    parts = price_event(event, db.query(StatsPriceSchedule).all(), inclusion_profile=profile)
    ordinary = next(part for part in parts if part.category == "input_tokens")
    assert ordinary.state == "unpriced" and ordinary.amount is None
    assert cache_rate([event], inclusion_profile=profile)["value"] is None
    assert cache_counterfactual([event], db.query(StatsPriceSchedule).all(), inclusion_profile=profile)["state"] == "unavailable"


def test_estimated_subsets_preserve_estimated_rate_and_missing_state_is_unknown(tmp_path):
    db = _db(tmp_path)
    profile, event = _cache_fixture(db)
    event.cache_read_tokens_state = "estimated"
    rate = cache_rate([event], inclusion_profile=profile)
    assert rate["value"] == "4/9" and rate["state"] == "estimated"
    event.cache_read_tokens_state = "unavailable"
    assert cache_rate([event], inclusion_profile=profile)["value"] is None


def test_cache_counterfactual_keeps_currency_baskets_distinct(tmp_path):
    db = _db(tmp_path)
    profile, usd = _cache_fixture(db)
    _, eur = _cache_fixture(db, model="euro-model", currency="EUR")
    schedules = db.query(StatsPriceSchedule).all()
    result = cache_counterfactual([usd, eur], iter(schedules), inclusion_profile=profile)
    assert set(result["currencies"]) == {"USD", "EUR"}
    for basket in result["currencies"].values():
        assert basket == {"observed": "160", "uncached": "100", "savings": "-60"}
    # A currency mismatch inside one event cannot be compared without FX evidence.
    next(row for row in schedules if row.model_identity == "model-a" and row.token_category == "cache_read_tokens").currency = "EUR"
    incompatible = cache_counterfactual([usd], schedules, inclusion_profile=profile)
    assert incompatible["state"] == "unavailable"


def test_pricing_stops_when_cancelled_during_event_iteration(tmp_path):
    import threading
    from services.stats.pricing import cost_envelope
    db = _db(tmp_path)
    profile, event = _cache_fixture(db)
    event.event_metadata = {"billing_lane": "api", "normalization_profile": profile.version}
    cancelled = threading.Event()
    def events():
        yield event
        cancelled.set()
        yield event
    with pytest.raises(TimeoutError):
        cost_envelope(events(), db.query(StatsPriceSchedule).all(),
                      profiles={profile.version: profile}, cancel_event=cancelled)


def test_pricing_stops_while_scanning_one_large_schedule_bucket():
    class CancelDuringScan:
        def __init__(self):
            self.checks = 0

        def is_set(self):
            self.checks += 1
            return self.checks >= 8

    event = _event()
    key = ("p", "api", "input_tokens", "model-a", "route-a")
    candidates = [
        StatsPriceSchedule(
            id=f"inactive-{index}", provider_id="p", billing_lane="api",
            model_identity="model-a", route_id="route-a", token_category="input_tokens",
            currency="USD", rate_numerator=1, rate_denominator=1,
            rate_unit="currency_major_per_token", effective_start=datetime(2026, 1, 1),
            effective_end=None, source_url="https://example.invalid/fixture",
            source_hash="fixture", admission_revision="fixture", active=False,
        )
        for index in range(50)
    ]
    with pytest.raises(TimeoutError):
        price_event(
            event,
            {key: candidates},
            inclusion_profile=InclusionProfile("single-v1", frozenset({"input_tokens"})),
            cancel_event=CancelDuringScan(),
        )


def test_exact_effective_boundary_and_rational_totals(tmp_path):
    db = _db(tmp_path)
    start = datetime(2026, 1, 1)
    admit_schedule(db, schedule_id="old", provider_id="p", billing_lane="api", model_identity="model-a", route_id="route-a", token_category="input_tokens", currency="USD", rate_numerator=1, rate_denominator=3, effective_start=start, effective_end=start + timedelta(days=1), source_url="https://example.invalid/a", source_hash="a", admission_revision="r1")
    admit_schedule(db, schedule_id="new", provider_id="p", billing_lane="api", model_identity="model-a", route_id="route-a", token_category="input_tokens", currency="USD", rate_numerator=2, rate_denominator=3, effective_start=start + timedelta(days=1), effective_end=None, source_url="https://example.invalid/b", source_hash="b", admission_revision="r2")
    db.commit()
    schedules = db.query(StatsPriceSchedule).all()
    profile = InclusionProfile("single-v1", frozenset({"input_tokens"}))
    first = price_event(_event(), schedules, inclusion_profile=profile)
    second = price_event(_event(id="e2", replay_key="e2", event_time=start + timedelta(days=1)), schedules, inclusion_profile=profile)
    assert first[0].amount == 1 and second[0].amount == 2 and first[0].revision == "r1"


def test_billed_precedes_estimated_and_unpriced_is_explicit(tmp_path):
    db = _db(tmp_path)
    row = _event(event_metadata={"billing_lane": "api", "billed_charge": {"input_tokens": {"amount_minor": 7, "currency": "USD", "currency_exponent": 2}}})
    result = project_cost([row, _event(id="unknown", replay_key="unknown", actual_model="missing")], [])
    assert result["totals"]["billed"]["USD"] == "7/100"
    assert result["coverage"]["billed"] == 1 and result["coverage"]["unpriced"] >= 1


def test_schedule_rejects_invalid_interval_or_rate(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(PricingError):
        admit_schedule(db, schedule_id="bad", provider_id="p", billing_lane="api", model_identity="m", route_id="r", token_category="input_tokens", currency="USD", rate_numerator=1, rate_denominator=0, effective_start=datetime(2026, 1, 1), effective_end=None, source_url="x", source_hash="x", admission_revision="r")


def test_inclusion_profile_blocks_implicit_double_counting(tmp_path):
    db = _db(tmp_path)
    schedule_args = dict(provider_id="p", billing_lane="api", model_identity="model-a", route_id="route-a", currency="USD", rate_numerator=1, rate_denominator=1, effective_start=datetime(2026, 1, 1), effective_end=None, source_url="x", source_hash="x", admission_revision="r")
    admit_schedule(db, schedule_id="in", token_category="input_tokens", **schedule_args)
    admit_schedule(db, schedule_id="out", token_category="output_tokens", **schedule_args)
    db.commit(); event = _event(output_tokens=2, output_tokens_state="reported")
    assert all(part.state == "unpriced" for part in price_event(event, db.query(StatsPriceSchedule).all()))
    profile = InclusionProfile("adapter-v1", frozenset({"input_tokens", "output_tokens"}))
    assert {part.state for part in price_event(event, db.query(StatsPriceSchedule).all(), inclusion_profile=profile)} >= {"estimated", "unpriced"}


def test_overlapping_model_and_route_selectors_are_rejected(tmp_path):
    db = _db(tmp_path)
    args = dict(provider_id="p", billing_lane="api", token_category="input_tokens", currency="USD", rate_numerator=1, rate_denominator=1, effective_start=datetime(2026, 1, 1), effective_end=None, source_url="x", source_hash="x", admission_revision="r")
    admit_schedule(db, schedule_id="model", model_identity="model-a", route_id=None, **args)
    with pytest.raises(PricingError):
        admit_schedule(db, schedule_id="route", model_identity=None, route_id="route-a", **args)


def test_normalized_input_subtracts_cache_subsets_before_pricing(tmp_path):
    db = _db(tmp_path)
    base = dict(provider_id="p", billing_lane="api", model_identity="model-a", route_id="route-a", currency="USD", rate_denominator=1, effective_start=datetime(2026, 1, 1), effective_end=None, source_url="x", source_hash="x")
    for name, category, rate in (("i", "input_tokens", 1), ("r", "cache_read_tokens", 2), ("w", "cache_write_tokens", 3)):
        admit_schedule(db, schedule_id=name, token_category=category, rate_numerator=rate, admission_revision=name, **base)
    db.commit()
    event = _event(input_tokens=100, cache_read_tokens=40, cache_write_tokens=10,
                   cache_read_tokens_state="reported", cache_write_tokens_state="reported")
    profile = InclusionProfile("adapter-v2", frozenset({"input_tokens", "cache_read_tokens", "cache_write_tokens"}), input_includes_cache_read=True, input_includes_cache_write=True)
    parts = price_event(event, db.query(StatsPriceSchedule).all(), inclusion_profile=profile)
    amounts = {part.category: part.amount for part in parts if part.amount is not None}
    assert amounts["input_tokens"] == 50 and amounts["cache_read_tokens"] == 80 and amounts["cache_write_tokens"] == 30


def test_cache_counterfactual_uses_same_normalized_basket_and_negative_savings(tmp_path):
    db = _db(tmp_path)
    base = dict(provider_id="p", billing_lane="api", model_identity="model-a", route_id="route-a", currency="USD", rate_denominator=1, effective_start=datetime(2026, 1, 1), effective_end=None, source_url="x", source_hash="x")
    for name, category, rate in (("i", "input_tokens", 1), ("r", "cache_read_tokens", 2), ("w", "cache_write_tokens", 3)):
        admit_schedule(db, schedule_id=name, token_category=category, rate_numerator=rate, admission_revision=name, **base)
    db.commit()
    event = _event(input_tokens=100, cache_read_tokens=40, cache_write_tokens=10, cache_read_tokens_state="reported", cache_write_tokens_state="reported")
    profile = InclusionProfile("adapter-v2", frozenset({"input_tokens", "cache_read_tokens", "cache_write_tokens"}), input_includes_cache_read=True, input_includes_cache_write=True)
    result = cache_counterfactual([event], db.query(StatsPriceSchedule).all(), inclusion_profile=profile)
    assert result["currencies"]["USD"] == {"observed": "160", "uncached": "100", "savings": "-60"}
    assert cache_rate([event], inclusion_profile=profile)["value"] == "4/9"


def test_reasoning_profiles_do_not_double_charge_or_drop_reasoning(tmp_path):
    db = _db(tmp_path)
    base = dict(provider_id="p", billing_lane="api", model_identity="model-a", route_id="route-a", currency="USD", rate_numerator=1, rate_denominator=1, effective_start=datetime(2026, 1, 1), effective_end=None, source_url="x", source_hash="x")
    for name, category in (("o", "output_tokens"), ("r", "reasoning_tokens")):
        admit_schedule(db, schedule_id=name, token_category=category, admission_revision=name, **base)
    db.commit(); event = _event(input_tokens=None, input_tokens_state="unavailable", output_tokens=30, output_tokens_state="reported", reasoning_tokens=10, reasoning_tokens_state="reported")
    separate = InclusionProfile("reason-v1", frozenset({"output_tokens", "reasoning_tokens"}), output_includes_reasoning=True, reasoning_billing="separate")
    amounts = {p.category: p.amount for p in price_event(event, db.query(StatsPriceSchedule).all(), inclusion_profile=separate) if p.amount is not None}
    assert amounts["output_tokens"] == 20 and amounts["reasoning_tokens"] == 10
    included = InclusionProfile("reason-v2", frozenset({"output_tokens", "reasoning_tokens"}), output_includes_reasoning=True, reasoning_billing="included_in_output")
    assert not any(p.category == "reasoning_tokens" and p.state == "estimated" for p in price_event(event, db.query(StatsPriceSchedule).all(), inclusion_profile=included))

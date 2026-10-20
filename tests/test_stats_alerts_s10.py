from datetime import datetime, timedelta, timezone

import pytest

from services.stats.alerts import (
    StatsAlertError,
    acknowledge_alerts,
    evaluate_quota_alerts,
    normalize_preferences,
)


NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


def _row(percent=81, *, reset="2026-09-28T00:00:00Z", state="official", owner_bits="secret"):
    return {
        "account_id": f"account-{owner_bits}", "provider_id": "openai", "window_id": "five-hour",
        "window_kind": "discrete", "state": state, "utilization_numerator": percent,
        "utilization_denominator": 100, "reset_at": reset, "observed_at": NOW.isoformat(),
    }


def test_preferences_bound_refresh_and_validate_typed_controls():
    assert normalize_preferences({"refresh_seconds": 1})["refresh_seconds"] == 30
    assert normalize_preferences({"refresh_seconds": 999999})["refresh_seconds"] == 86400
    assert normalize_preferences({"quota_thresholds": [100, 80, 90, 80]})["quota_thresholds"] == [80, 90, 100]
    with pytest.raises(StatsAlertError):
        normalize_preferences({"quota_alerts": "yes"})


def test_crossing_ack_dedupe_and_new_reset_cycle_without_raw_identity():
    prefs = {"quota_alerts": True, "quota_thresholds": [80, 90, 100]}
    candidates, state = evaluate_quota_alerts(owner="alice", observations=[_row(81)], preferences=prefs, now=NOW)
    assert [item["threshold"] for item in candidates] == [80]
    assert "secret" not in str(candidates) and "account-" not in str(candidates)
    state = acknowledge_alerts(state, candidates, [candidates[0]["token"]], owner="alice")
    repeated, state = evaluate_quota_alerts(owner="alice", observations=[_row(85)], preferences=prefs, state=state, now=NOW)
    assert repeated == []
    ninety, state = evaluate_quota_alerts(owner="alice", observations=[_row(91)], preferences=prefs, state=state, now=NOW)
    assert [item["threshold"] for item in ninety] == [90]
    next_cycle, _ = evaluate_quota_alerts(owner="alice", observations=[_row(81, reset="2026-10-01T00:00:00Z")], preferences=prefs, state=state, now=NOW)
    assert [item["threshold"] for item in next_cycle] == [80]


def test_unacknowledged_crossing_remains_pending_until_delivery_permission_allows_ack():
    prefs = {"quota_alerts": True}
    first, state = evaluate_quota_alerts(owner="alice", observations=[_row(81)], preferences=prefs, now=NOW)
    repeated, state = evaluate_quota_alerts(owner="alice", observations=[_row(81)], preferences=prefs, state=state, now=NOW)
    assert repeated == first
    state = acknowledge_alerts(state, repeated, [repeated[0]["token"]], owner="alice")
    assert evaluate_quota_alerts(owner="alice", observations=[_row(81)], preferences=prefs, state=state, now=NOW)[0] == []


def test_off_stale_local_future_and_invalid_fractions_never_claim_crossings():
    enabled = {"quota_alerts": True}
    stale = _row(100, state="stale")
    local = _row(100, state="local")
    future = _row(100); future["observed_at"] = (NOW + timedelta(days=1)).isoformat()
    invalid = _row(100); invalid["utilization_denominator"] = 0
    for row in (stale, local, future, invalid):
        assert evaluate_quota_alerts(owner="alice", observations=[row], preferences=enabled, now=NOW)[0] == []
    assert evaluate_quota_alerts(owner="alice", observations=[_row(100)], preferences={"quota_alerts": False}, now=NOW)[0] == []


def test_owner_binding_prevents_shared_dedupe_tokens():
    prefs = {"quota_alerts": True}
    alice, _ = evaluate_quota_alerts(owner="alice", observations=[_row()], preferences=prefs, now=NOW)
    bob, _ = evaluate_quota_alerts(owner="bob", observations=[_row()], preferences=prefs, now=NOW)
    assert alice[0]["token"] != bob[0]["token"]
    assert alice[0]["owner_scope"] != bob[0]["owner_scope"]


def test_acknowledgement_marks_only_the_matching_opaque_window():
    prefs = {"quota_alerts": True}
    first = _row(); second = _row(owner_bits="other"); second["window_id"] = "weekly"
    candidates, state = evaluate_quota_alerts(owner="alice", observations=[first, second], preferences=prefs, now=NOW)
    state = acknowledge_alerts(state, candidates, [candidates[0]["token"]], owner="alice")
    delivered = [row.get("delivered", []) for row in state["windows"].values()]
    assert sorted(map(len, delivered)) == [0, 1]


def test_acknowledgement_owner_binding_rejects_foreign_candidate():
    prefs = {"quota_alerts": True}
    candidates, state = evaluate_quota_alerts(owner="alice", observations=[_row()], preferences=prefs, now=NOW)
    unchanged = acknowledge_alerts(state, candidates, [candidates[0]["token"]], owner="bob")
    assert unchanged["windows"] == state["windows"]
    accepted = acknowledge_alerts(state, candidates, [candidates[0]["token"]], owner="alice")
    assert accepted["windows"] != state["windows"]

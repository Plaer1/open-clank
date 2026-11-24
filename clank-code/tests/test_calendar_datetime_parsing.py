"""Calendar date parsing for UTC offsets and natural-language dates."""
import pytest

from routes.calendar_routes import _parse_dt, _parse_dt_pair

# Inputs datetime.fromisoformat() rejects (so they hit the dateutil fallback)
# but that carry a numeric UTC offset dateutil resolves to tz-aware.
_OFFSET_NONISO = [
    "Mon, 05 Jan 2026 14:00:00 +0900",
    "January 5, 2026 14:00 +0900",
]


@pytest.mark.parametrize("s", _OFFSET_NONISO)
def test_parse_dt_dateutil_fallback_returns_naive(s):
    d = _parse_dt(s)
    assert d.tzinfo is None, f"{s!r} leaked tz-aware: {d!r}"
    # +0900 14:00 -> 05:00 UTC, naive.
    assert (d.hour, d.minute) == (5, 0)


@pytest.mark.parametrize("s", _OFFSET_NONISO)
def test_parse_dt_pair_fallback_returns_naive(s):
    dt, _is_utc = _parse_dt_pair(s)
    assert dt.tzinfo is None, f"{s!r} leaked tz-aware via _parse_dt_pair: {dt!r}"


def test_parse_dt_naive_input_unchanged():
    d = _parse_dt("January 5, 2026 14:00")  # no offset -> stays as parsed
    assert d.tzinfo is None
    assert (d.hour, d.minute) == (14, 0)


def test_time_first_today_equals_day_first():
    assert _parse_dt("3pm today") == _parse_dt("today at 3pm")


def test_time_first_tomorrow_equals_day_first():
    assert _parse_dt("9am tomorrow") == _parse_dt("tomorrow at 9am")


def test_time_first_with_minutes_equals_day_first():
    assert _parse_dt("2:30pm tomorrow") == _parse_dt("tomorrow at 2:30pm")


def test_time_first_tonight_maps_to_today():
    assert _parse_dt("11pm tonight") == _parse_dt("today at 11pm")


def test_time_first_yesterday_equals_day_first():
    assert _parse_dt("8am yesterday") == _parse_dt("yesterday at 8am")


def test_tonight_with_time_parses_to_today_evening():
    got = _parse_dt("tonight at 9pm")
    ref = _parse_dt("today at 9pm")
    assert got.hour == 21 and got.minute == 0
    assert got.date() == ref.date()


def test_bare_tonight_is_today():
    assert _parse_dt("tonight").date() == _parse_dt("today").date()


def test_tonight_matches_today_time_exactly():
    assert _parse_dt("tonight at 7:30pm") == _parse_dt("today at 7:30pm")

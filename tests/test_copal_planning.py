import json
from pathlib import Path

import pytest

from src.openclank.copal_planning import (
    EVENT_KIND,
    EVENT_SCHEMA,
    MAX_RECURRENCE_EXPANSION,
    PlanningValidationError,
    event_document_name,
    event_from_document,
    expand_recurrence,
    legacy_inventory,
    merge_event,
    planning_projection,
    serialize_event,
    serialize_track_registry,
    validate_event,
)


TRACKS = [
    {"id": "home", "name": "Home", "color": "#14b8a6", "icon": "🏠", "enabled": True},
    {"id": "car", "name": "Car", "color": "#0ea5e9", "icon": "car", "enabled": True},
]


def event_doc(event, *, document_id="EVENT1", head="head-1"):
    return {
        "id": document_id,
        "head": head,
        "kind": EVENT_KIND,
        "name": ".events/Move.md",
        "text": serialize_event(event, tracks=TRACKS),
    }


def test_canonical_events_use_a_typed_hidden_namespace():
    event = {"legacyId": "stable-7", "title": "Move the car"}
    name = event_document_name(event)

    assert EVENT_KIND == "copal-event"
    assert name.startswith(".events/move-the-car--")
    assert name.endswith(".md")


def test_rich_event_note_round_trip_preserves_all_semantics_and_unknown_fields():
    source = {
        "legacyId": "old-7",
        "title": "Move the car",
        "description": "A **real** Markdown body.\n",
        "startDate": "FUZZY",
        "dueDate": None,
        "status": "in-progress",
        "priority": "high",
        "trackId": "home",
        "sharedTrackIds": ["car", "car", "home"],
        "tags": ["move", "car"],
        "linkId": "shared-move",
        "fuzzy": {"anchorStart": "2026-07-10", "anchorEnd": "2026-07-15", "fadeIn": True},
        "fadeDays": 4,
        "titleStart": "pack",
        "titleEnd": "arrive",
        "stages": [{"id": "s1", "title": "Pack", "done": True, "date": "2026-07-11", "extra": 9}],
        "copal_extra": {"oldFlag": "keep-me"},
        "_frontmatterExtra": {"aliases": ["Move"]},
    }

    restored = event_from_document(event_doc(source))

    assert restored["id"] == "EVENT1"
    assert restored["description"] == source["description"]
    assert restored["sharedTrackIds"] == ["car"]
    assert restored["fuzzy"] == source["fuzzy"]
    assert restored["stages"] == source["stages"]
    assert restored["copal_extra"] == {"oldFlag": "keep-me"}
    assert restored["_frontmatterExtra"] == {"aliases": ["Move"]}


def test_event_patch_is_field_scoped_revision_payload_and_validates_dates():
    current = event_from_document(event_doc({
        "title": "Before", "description": "body", "startDate": "2026-07-10", "dueDate": "2026-07-12",
        "status": "pending", "priority": "medium", "trackId": "home", "sharedTrackIds": [], "tags": [],
        "copal_extra": {"future": {"shape": True}},
    }))
    changed = merge_event(current, {"title": "After", "dueDate": "2026-07-13"}, TRACKS)
    assert changed["title"] == "After"
    assert changed["startDate"] == "2026-07-10"
    assert changed["copal_extra"] == {"future": {"shape": True}}
    with pytest.raises(PlanningValidationError, match="precede"):
        merge_event(current, {"dueDate": "2026-07-01"}, TRACKS)
    with pytest.raises(PlanningValidationError, match="Unsupported event field"):
        merge_event(current, {"madeUp": True}, TRACKS)


def test_legacy_inventory_maps_tracks_shared_fuzzy_stages_and_unknowns():
    planning = {
        "id": "LEGACY",
        "head": "legacy-head",
        "kind": "planning",
        "text": json.dumps({
            "title": "Move",
            "globalStart": "2026-07-01",
            "tracks": [{
                **TRACKS[0],
                "tasks": [{
                    "id": "task-1", "title": "Pack", "description": "boxes", "startDate": "FUZZY", "dueDate": None,
                    "status": "pending", "priority": "high", "sharedTrackIds": ["car"], "tags": ["move"],
                    "fuzzy": {"anchorStart": "2026-07-10", "fadeIn": True}, "stages": [{"title": "Books", "done": False}],
                    "unknownOriginal": 42,
                }],
            }, {**TRACKS[1], "tasks": []}],
            "floatingTodos": [{"id": "todo-1", "text": "Call mover", "done": False}],
        }),
    }

    inventory = legacy_inventory(planning)

    assert len(inventory["tracks"]) == 2
    assert len(inventory["events"]) == 2
    assert inventory["events"][0]["sharedTrackIds"] == ["car"]
    assert inventory["events"][0]["copal_extra"]["unknownOriginal"] == 42
    assert inventory["events"][0]["stages"][0]["id"].startswith("stage-")
    assert inventory["events"][1]["floating"] is True
    assert inventory["metadata"] == {"title": "Move", "globalStart": "2026-07-01"}


def test_planning_projection_is_query_over_canonical_event_documents():
    registry = {
        "id": "TRACKS",
        "head": "track-head",
        "kind": "copal-tracks",
        "name": ".copal/tracks.json",
        "text": serialize_track_registry(TRACKS, {"title": "Canonical"}),
    }
    event = event_doc({
        "title": "Shared", "description": "body", "startDate": "2026-07-10", "dueDate": "2026-07-12",
        "status": "pending", "priority": "medium", "trackId": "home", "sharedTrackIds": ["car"], "tags": [],
    })

    projection = planning_projection([registry, event])

    assert projection["canonical"] is True
    assert projection["trackRegistry"] == {"id": "TRACKS", "head": "track-head"}
    assert projection["tracks"][0]["tasks"][0]["id"] == "EVENT1"
    assert projection["tracks"][0]["tasks"][0]["head"] == "head-1"
    assert projection["tracks"][1]["tasks"] == []


def test_fuzzy_and_track_validation_do_not_silently_coerce_semantics():
    with pytest.raises(PlanningValidationError, match="fuzzy anchor"):
        validate_event({"title": "bad", "startDate": "FUZZY", "trackId": "home"}, TRACKS)
    with pytest.raises(PlanningValidationError, match="Unknown main track"):
        validate_event({"title": "bad", "startDate": "2026-07-10", "trackId": "missing"}, TRACKS)


# ── Schema v2 + Recurrence ─────────────────────────────────────────────


def test_schema_version_is_2():
    assert EVENT_SCHEMA == 2


def test_recurrence_validates_frequency_interval_count_or_until():
    base = {"title": "Standup", "startDate": "2026-07-10"}
    valid = validate_event({**base, "recurrence": {"frequency": "weekly", "interval": 1, "count": 5}})
    assert valid["recurrence"]["frequency"] == "weekly"
    assert valid["recurrence"]["interval"] == 1
    assert valid["recurrence"]["count"] == 5
    assert valid["recurrence"]["until"] is None

    with pytest.raises(PlanningValidationError, match="frequency"):
        validate_event({**base, "recurrence": {"frequency": "hourly", "count": 3}})
    with pytest.raises(PlanningValidationError, match="requires either count or until"):
        validate_event({**base, "recurrence": {"frequency": "daily"}})
    with pytest.raises(PlanningValidationError, match="not both"):
        validate_event({**base, "recurrence": {"frequency": "daily", "count": 5, "until": "2026-12-31"}})


def test_recurrence_validates_interval_and_exceptions():
    base = {"title": "Gym", "startDate": "2026-07-10"}
    valid = validate_event({**base, "recurrence": {"frequency": "daily", "interval": 2, "until": "2026-07-31", "exceptionDates": ["2026-07-15", "2026-07-20"]}})
    assert valid["recurrence"]["interval"] == 2
    assert valid["recurrence"]["exceptionDates"] == ["2026-07-15", "2026-07-20"]

    with pytest.raises(PlanningValidationError, match="interval"):
        validate_event({**base, "recurrence": {"frequency": "daily", "interval": 0, "count": 3}})
    with pytest.raises(PlanningValidationError, match="exceptionDates"):
        validate_event({**base, "recurrence": {"frequency": "daily", "count": 3, "exceptionDates": "not-a-list"}})


def test_recurrence_round_trips_through_serialization():
    event = {
        "title": "Weekly sync",
        "startDate": "2026-07-10",
        "dueDate": "2026-07-10",
        "recurrence": {"frequency": "weekly", "interval": 1, "count": 4, "exceptionDates": ["2026-07-24"], "allDay": False},
    }
    serialized = serialize_event(event, tracks=TRACKS)
    assert "recurrence" in serialized
    doc = event_doc(event)
    restored = event_from_document(doc)
    assert restored["recurrence"]["frequency"] == "weekly"
    assert restored["recurrence"]["count"] == 4
    assert restored["recurrence"]["exceptionDates"] == ["2026-07-24"]


def test_all_day_field_round_trips():
    event = {"title": "Holiday", "startDate": "2026-12-25", "dueDate": "2026-12-25", "allDay": True}
    serialized = serialize_event(event, tracks=TRACKS)
    assert "allDay" in serialized
    doc = event_doc(event)
    restored = event_from_document(doc)
    assert restored["allDay"] is True


# ── Recurrence Expansion Engine ────────────────────────────────────────


def test_expand_weekly_recurrence_count_bounded():
    event = {"id": "E1", "title": "Standup", "startDate": "2026-07-07", "dueDate": "2026-07-07",
             "recurrence": {"frequency": "weekly", "interval": 1, "count": 4}}
    occs = expand_recurrence(event, "2026-07-01", "2026-08-15")
    assert len(occs) == 4
    assert occs[0]["occurrenceKey"] == "2026-07-07"
    assert occs[3]["occurrenceKey"] == "2026-07-28"
    for occ in occs:
        assert occ["eventId"] == "E1"


def test_expand_daily_recurrence_until_bounded():
    event = {"id": "E2", "title": "Meditate", "startDate": "2026-07-10",
             "recurrence": {"frequency": "daily", "interval": 1, "until": "2026-07-13"}}
    occs = expand_recurrence(event, "2026-07-01", "2026-07-31")
    dates = [o["occurrenceKey"] for o in occs]
    assert dates == ["2026-07-10", "2026-07-11", "2026-07-12", "2026-07-13"]


def test_expand_monthly_recurrence_preserves_day():
    event = {"id": "E3", "title": "Rent", "startDate": "2026-01-15",
             "recurrence": {"frequency": "monthly", "interval": 1, "count": 3}}
    occs = expand_recurrence(event, "2026-01-01", "2026-12-31")
    dates = [o["occurrenceKey"] for o in occs]
    assert dates == ["2026-01-15", "2026-02-15", "2026-03-15"]


def test_expand_monthly_end_of_month_clamps():
    event = {"id": "E4", "title": "Report", "startDate": "2026-01-31",
             "recurrence": {"frequency": "monthly", "interval": 1, "count": 3}}
    occs = expand_recurrence(event, "2026-01-01", "2026-12-31")
    dates = [o["occurrenceKey"] for o in occs]
    # Jan 31 → Feb 28 (not leap) → Mar 31
    assert dates == ["2026-01-31", "2026-02-28", "2026-03-31"]


def test_expand_skips_exception_dates():
    event = {"id": "E5", "title": "Yoga", "startDate": "2026-07-07",
             "recurrence": {"frequency": "weekly", "interval": 1, "count": 4, "exceptionDates": ["2026-07-14", "2026-07-28"]}}
    occs = expand_recurrence(event, "2026-07-01", "2026-08-15")
    dates = [o["occurrenceKey"] for o in occs]
    assert "2026-07-14" not in dates
    assert "2026-07-28" not in dates
    assert len(occs) == 2


def test_expand_hard_cap_at_500():
    event = {"id": "E6", "title": "Freq", "startDate": "2020-01-01",
             "recurrence": {"frequency": "daily", "interval": 1, "count": 1000}}
    occs = expand_recurrence(event, "2020-01-01", "2025-12-31")
    assert len(occs) == MAX_RECURRENCE_EXPANSION


def test_expand_no_recurrence_returns_empty():
    event = {"id": "E7", "title": "One-shot", "startDate": "2026-07-10"}
    assert expand_recurrence(event, "2026-07-01", "2026-07-31") == []


def test_expand_window_clip():
    event = {"id": "E8", "title": "Weekly", "startDate": "2026-07-01",
             "recurrence": {"frequency": "weekly", "interval": 1, "count": 20}}
    occs = expand_recurrence(event, "2026-08-01", "2026-08-31")
    # Only August occurrences
    for occ in occs:
        assert occ["occurrenceKey"].startswith("2026-08")


def test_expand_preserves_duration():
    event = {"id": "E9", "title": "Trip", "startDate": "2026-07-10", "dueDate": "2026-07-12",
             "recurrence": {"frequency": "monthly", "interval": 1, "count": 2}}
    occs = expand_recurrence(event, "2026-07-01", "2026-12-31")
    assert occs[0]["startDate"] == "2026-07-10"
    assert occs[0]["dueDate"] == "2026-07-12"
    assert occs[1]["startDate"] == "2026-08-10"
    assert occs[1]["dueDate"] == "2026-08-12"


def test_expand_all_day_flag_set():
    event = {"id": "E10", "title": "Holiday", "startDate": "2026-12-25",
             "recurrence": {"frequency": "yearly", "interval": 1, "count": 1, "allDay": True}}
    # yearly not supported yet, but allDay flag should propagate
    event["recurrence"]["frequency"] = "monthly"
    occs = expand_recurrence(event, "2026-12-01", "2026-12-31")
    assert occs[0]["allDay"] is True


def test_expand_exception_dates_as_strings():
    event = {"id": "E11", "title": "Skip", "startDate": "2026-07-07",
             "recurrence": {"frequency": "daily", "interval": 1, "count": 5, "exceptionDates": ["2026-07-09"]}}
    occs = expand_recurrence(event, "2026-07-07", "2026-07-11")
    assert len(occs) == 4
    assert all(o["occurrenceKey"] != "2026-07-09" for o in occs)


# ── Calendar Projection — Rename Stability ─────────────────────────────


def test_calendar_uid_stable_across_rename(monkeypatch):
    """Calendar UID is derived from logical_id, not event name."""
    from src.openclank.copal_calendar_projection import build_projection

    tasks = [{"id": "t1", "title": "Old Name", "startDate": "2026-08-01", "dueDate": "2026-08-01"}]
    planning = {"tracks": [{"id": "a", "name": "Track", "tasks": tasks}]}
    events1, _ = build_projection(planning, owner="o", workspace="w", planning_document_id="P")

    # Rename
    tasks[0]["title"] = "New Name"
    events2, _ = build_projection(planning, owner="o", workspace="w", planning_document_id="P")

    assert events1[0].uid == events2[0].uid
    assert events2[0].summary == "New Name"


def test_recurring_event_calendar_uids_are_per_occurrence(monkeypatch):
    """Each occurrence of a recurring event gets a unique UID."""
    from src.openclank.copal_calendar_projection import build_projection

    task = {
        "id": "rec-1",
        "title": "Weekly sync",
        "startDate": "2026-07-07",
        "dueDate": "2026-07-07",
        "recurrence": {"frequency": "weekly", "interval": 1, "count": 4},
    }
    planning = {"tracks": [{"id": "a", "name": "Track", "tasks": [task]}]}
    events, diag = build_projection(
        planning, owner="o", workspace="w", planning_document_id="P",
        window_start="2026-07-01", window_end="2026-08-15",
    )
    assert len(events) == 4
    uids = {e.uid for e in events}
    assert len(uids) == 4  # all unique
    for e in events:
        assert "2026-07" in e.description or "2026-08" in e.description  # occurrence in desc


def test_timeline_chrome_uses_active_theme_tokens():
    root = Path(__file__).resolve().parents[1]
    planning = (root / "static/js/copal/planning.js").read_text(encoding="utf-8")
    style = (root / "static/style.css").read_text(encoding="utf-8")
    renderer = planning.split("function renderTimeline", 1)[1].split("function renderTodo", 1)[0]
    timeline_css = style.split(":where(.copal-timeline-toolbar", 1)[1].split("/* Rich event", 1)[0]

    assert "THEME_TRACK_COLOR" in renderer
    assert "eventTrack.color || '#14b8a6'" not in renderer
    for token in (
        "--copal-timeline-accent:", "--copal-timeline-canvas:",
        "--copal-timeline-band:", "--copal-timeline-label:",
        "--copal-timeline-grid:", "--copal-timeline-shadow:",
    ):
        assert token in timeline_css
    for fixed_chrome in ("68%, white", "rgba(0, 0, 0, .45)", "var(--accent, #22d3ee)"):
        assert fixed_chrome not in timeline_css
    assert "var(--font-mono" in timeline_css

"""Calendar agent actions against isolated, file-backed SQLite stores."""
import json
import shutil
import sys
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb
from core.database import CalendarEvent, Note


@pytest.fixture(scope="module")
def calendar_schema(tmp_path_factory):
    path = tmp_path_factory.mktemp("calendar-template") / "schema.db"
    engine = create_engine(f"sqlite:///{path}")
    try:
        cdb.Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    return path


@pytest.fixture(autouse=True)
def calendar_db(calendar_schema, tmp_path, monkeypatch):
    import routes.calendar_routes as routes

    path = tmp_path / "calendar.db"
    shutil.copyfile(calendar_schema, path)
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool, connect_args={"check_same_thread": False})
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    monkeypatch.setattr(sys.modules["core"], "database", cdb)
    monkeypatch.setattr(cdb, "SessionLocal", sessions)
    monkeypatch.setattr(routes, "SessionLocal", sessions, raising=False)
    try:
        yield sessions
    finally:
        engine.dispose()


async def test_batch_events_with_datetime_objects(calendar_db):
    """Model emits {"events": [{"summary": ..., "start": {"dateTime": ...}, "end": {"dateTime": ...}}]}."""
    from src.tool_implementations import do_manage_calendar

    owner = "tester-" + uuid.uuid4().hex[:6]
    payload = {
        "events": [
            {
                "summary": "Morning Gym",
                "start": {"dateTime": "2026-06-09T06:00:00+05:30"},
                "end": {"dateTime": "2026-06-09T07:00:00+05:30"},
            },
            {
                "summary": "Morning Gym",
                "start": {"dateTime": "2026-06-10T06:00:00+05:30"},
                "end": {"dateTime": "2026-06-10T07:00:00+05:30"},
            },
        ]
    }
    res = await do_manage_calendar(json.dumps(payload), owner=owner)
    assert res.get("exit_code") == 0, res
    assert "Created 2 event(s)" in res.get("response", "")

    # Verify events exist in DB
    db = calendar_db()
    events = db.query(CalendarEvent).filter(CalendarEvent.summary == "Morning Gym").all()
    assert len(events) == 2
    db.close()


async def test_batch_events_with_flat_strings():
    """Model emits {"events": [{"summary": ..., "start": "ISO", "end": "ISO"}]}."""
    from src.tool_implementations import do_manage_calendar

    owner = "tester-" + uuid.uuid4().hex[:6]
    payload = {
        "events": [
            {
                "summary": "Standup",
                "start": "2026-06-09T09:00:00",
                "end": "2026-06-09T09:30:00",
            },
        ]
    }
    res = await do_manage_calendar(json.dumps(payload), owner=owner)
    assert res.get("exit_code") == 0, res
    assert "Created 1 event(s)" in res.get("response", "")


async def test_batch_events_partial_failure(calendar_db):
    """Batch with some valid and some invalid events — should surface both counts and first error."""
    from src.tool_implementations import do_manage_calendar

    owner = "tester-" + uuid.uuid4().hex[:6]
    payload = {
        "events": [
            {
                "summary": "Valid Event 1",
                "start": "2026-06-09T10:00:00",
                "end": "2026-06-09T11:00:00",
            },
            {
                "summary": "Invalid Event",
                # Missing required dtstart — will fail
            },
            {
                "summary": "Valid Event 2",
                "start": "2026-06-09T14:00:00",
                "end": "2026-06-09T15:00:00",
            },
        ]
    }
    res = await do_manage_calendar(json.dumps(payload), owner=owner)

    # Partial failure = non-zero exit code
    assert res.get("exit_code") != 0, "Partial failure should return non-zero exit code"

    # Response should mention both created and failed counts
    response = res.get("response", "")
    assert "Created 2 event(s)" in response, f"Should report 2 created: {response}"
    assert "Failed to create 1 event(s)" in response, f"Should report 1 failed: {response}"
    assert "error" in response.lower() or "required" in response.lower(), "Should include error details"

    # Metadata fields
    assert res.get("created_count") == 2
    assert res.get("failed_count") == 1

    # Verify only valid events were created
    db = calendar_db()
    events = db.query(CalendarEvent).filter(
        CalendarEvent.summary.in_(["Valid Event 1", "Valid Event 2"])
    ).all()
    assert len(events) == 2
    db.close()


async def test_create_event_with_rrule_persists_recurrence(calendar_db):
    from src.tool_implementations import do_manage_calendar

    owner = "tester-" + uuid.uuid4().hex[:6]
    rrule = "FREQ=WEEKLY;BYDAY=MO"
    res = await do_manage_calendar(json.dumps({
        "action": "create_event",
        "summary": "Standup",
        "dtstart": "2026-06-08T09:00:00Z",
        "rrule": rrule,
    }), owner=owner)
    assert res.get("exit_code", 0) == 0, res
    uid = res.get("uid")
    assert uid, res

    db = calendar_db()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == uid).first()
        assert ev is not None
        assert ev.rrule == rrule  # ONE event carrying the recurrence rule
        assert ev.summary == "Standup"
    finally:
        db.close()


async def test_create_event_without_rrule_is_single(calendar_db):
    from src.tool_implementations import do_manage_calendar

    owner = "tester-" + uuid.uuid4().hex[:6]
    res = await do_manage_calendar(json.dumps({
        "action": "create_event",
        "summary": "One-off",
        "dtstart": "2026-06-09T10:00:00Z",
    }), owner=owner)
    assert res.get("exit_code", 0) == 0, res
    db = calendar_db()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == res["uid"]).first()
        assert ev is not None and (ev.rrule or "") == ""
    finally:
        db.close()


async def test_update_event_can_clear_rrule(calendar_db):
    from src.tool_implementations import do_manage_calendar

    owner = "tester-" + uuid.uuid4().hex[:6]
    created = await do_manage_calendar(json.dumps({
        "action": "create_event",
        "summary": "Repeating standup",
        "dtstart": "2026-07-01T14:00:00Z",
        "rrule": "FREQ=WEEKLY;BYDAY=WE",
    }), owner=owner)
    assert created.get("exit_code", 0) == 0, created

    updated = await do_manage_calendar(json.dumps({
        "action": "update_event",
        "uid": created["uid"],
        "rrule": "",
    }), owner=owner)
    assert updated.get("exit_code", 0) == 0, updated

    db = calendar_db()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == created["uid"]).first()
        assert ev is not None
        assert (ev.rrule or "") == ""
    finally:
        db.close()


async def test_update_event_can_clear_rrule_with_repeat_none_alias(calendar_db):
    from src.tool_implementations import do_manage_calendar

    owner = "tester-" + uuid.uuid4().hex[:6]
    created = await do_manage_calendar(json.dumps({
        "action": "create_event",
        "summary": "Repeating review",
        "dtstart": "2026-07-01T15:00:00Z",
        "rrule": "FREQ=WEEKLY;BYDAY=WE",
    }), owner=owner)
    assert created.get("exit_code", 0) == 0, created

    updated = await do_manage_calendar(json.dumps({
        "action": "update_event",
        "uid": created["uid"],
        "repeat": "none",
    }), owner=owner)
    assert updated.get("exit_code", 0) == 0, updated

    db = calendar_db()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == created["uid"]).first()
        assert ev is not None
        assert (ev.rrule or "") == ""
    finally:
        db.close()


async def test_list_events_exposes_rrule_for_repeating_events():
    from src.tool_implementations import do_manage_calendar

    owner = "tester-" + uuid.uuid4().hex[:6]
    rrule = "FREQ=WEEKLY;BYDAY=WE"
    created = await do_manage_calendar(json.dumps({
        "action": "create_event",
        "summary": "Weekly sync",
        "dtstart": "2026-07-01T14:00:00Z",
        "rrule": rrule,
    }), owner=owner)
    assert created.get("exit_code", 0) == 0, created

    listed = await do_manage_calendar(json.dumps({
        "action": "list_events",
        "start": "2026-07-01T00:00:00Z",
        "end": "2026-07-02T00:00:00Z",
    }), owner=owner)
    assert listed.get("exit_code", 0) == 0, listed
    matches = [ev for ev in listed["events"] if ev["uid"] == created["uid"]]
    assert matches
    assert matches[0]["rrule"] == rrule
    assert f"repeats({rrule})" in listed["response"]


@pytest.fixture
def tokyo_offset():
    from routes.calendar_routes import set_user_tz_offset
    set_user_tz_offset(540)  # Tokyo, UTC+9
    try:
        yield
    finally:
        set_user_tz_offset(None)


async def test_update_event_dtstart_anchored_to_user_tz(tokyo_offset, calendar_db):
    from src.tool_implementations import do_manage_calendar

    owner = "tz-" + uuid.uuid4().hex[:6]
    naive = "2026-06-10T14:00:00"  # 14:00 Tokyo == 05:00 UTC

    created = await do_manage_calendar(json.dumps({
        "action": "create_event",
        "summary": "Standup",
        "dtstart": naive,
    }), owner=owner)
    assert created.get("exit_code", 0) == 0, created
    uid = created["uid"]

    db = calendar_db()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == uid).first()
        created_dtstart, created_is_utc = ev.dtstart, ev.is_utc
    finally:
        db.close()

    # Update the same event to the SAME naive wall-clock value.
    updated = await do_manage_calendar(json.dumps({
        "action": "update_event",
        "uid": uid,
        "dtstart": naive,
    }), owner=owner)
    assert updated.get("exit_code", 0) == 0, updated

    db = calendar_db()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == uid).first()
        # Same input -> same stored moment and same is_utc flag as create.
        assert ev.dtstart == created_dtstart
        assert bool(ev.is_utc) == bool(created_is_utc)
        # And concretely: 14:00 Tokyo is 05:00 UTC, stored naive-UTC.
        assert ev.dtstart.hour == 5
        assert bool(ev.is_utc) is True
    finally:
        db.close()


async def test_list_events_accepts_start_time_end_time_aliases():
    from src.tool_implementations import do_manage_calendar

    owner = "list-" + uuid.uuid4().hex[:6]
    created = await do_manage_calendar(json.dumps({
        "action": "create_event",
        "summary": "July planning",
        "dtstart": "2026-07-15T12:00:00Z",
        "dtend": "2026-07-15T13:00:00Z",
    }), owner=owner)
    assert created.get("exit_code", 0) == 0, created

    listed = await do_manage_calendar(json.dumps({
        "action": "list_events",
        "start_time": "2026-07-01T00:00:00Z",
        "end_time": "2026-08-01T00:00:00Z",
    }), owner=owner)
    assert listed.get("exit_code", 0) == 0, listed
    assert "between 2026-07-01 and 2026-08-01" in listed["response"]
    assert [event["summary"] for event in listed["events"]] == ["July planning"]


async def test_list_events_query_without_range_does_not_default_to_two_weeks():
    from src.tool_implementations import do_manage_calendar

    listed = await do_manage_calendar(json.dumps({
        "action": "list_events",
        "query": "July",
    }), owner="list-" + uuid.uuid4().hex[:6])
    assert listed.get("exit_code") == 1, listed
    assert "explicit start/end" in listed["error"]


@pytest.mark.parametrize(
    ("start_key", "end_key"),
    [
        ("start_date", "end_date"),
        ("from", "to"),
        ("range_start", "range_end"),
    ],
)
async def test_list_events_honors_range_aliases(start_key, end_key):
    from src.tool_implementations import do_manage_calendar

    owner = "calendar-alias-" + uuid.uuid4().hex[:8]

    inside = await do_manage_calendar(json.dumps({
        "action": "create_event",
        "summary": "Late June planning",
        "dtstart": "2126-06-25T10:00:00Z",
    }), owner=owner)
    assert inside.get("exit_code", 0) == 0, inside

    outside = await do_manage_calendar(json.dumps({
        "action": "create_event",
        "summary": "Outside July planning",
        "dtstart": "2126-07-10T10:00:00Z",
    }), owner=owner)
    assert outside.get("exit_code", 0) == 0, outside

    res = await do_manage_calendar(json.dumps({
        "action": "list_events",
        start_key: "2126-06-01T00:00:00Z",
        end_key: "2126-07-01T00:00:00Z",
    }), owner=owner)

    assert res.get("exit_code", 0) == 0, res
    summaries = [event["summary"] for event in res["events"]]
    assert summaries == ["Late June planning"]
    assert "between 2126-06-01 and 2126-07-01" in res["response"]


async def test_list_events_rejects_partial_loose_range():
    from src.tool_implementations import do_manage_calendar

    owner = "calendar-partial-" + uuid.uuid4().hex[:8]

    # Partial: has query and start, but no end
    res = await do_manage_calendar(json.dumps({
        "action": "list_events",
        "query": "July",
        "start_time": "2126-07-01T00:00:00Z",
    }), owner=owner)

    assert res.get("exit_code", 1) == 1, res
    assert "list_events needs explicit start/end" in res.get("error", "")

    # Partial: has query and end, but no start
    res2 = await do_manage_calendar(json.dumps({
        "action": "list_events",
        "query": "July",
        "end_time": "2126-07-31T23:59:59Z",
    }), owner=owner)

    assert res2.get("exit_code", 1) == 1, res2
    assert "list_events needs explicit start/end" in res2.get("error", "")


async def _create_with_reminder(reminder, owner):
    from src.tool_implementations import do_manage_calendar

    payload = {
        "action": "create_event",
        "summary": "Dentist",
        # Far-future so the reminder is never "already passed".
        "dtstart": "2030-01-01T10:00:00",
        "reminder_minutes": reminder,
    }
    return await do_manage_calendar(json.dumps(payload), owner=owner)


@pytest.mark.parametrize("reminder,expected", [
    ("5 mins", 5),
    ("10 mins", 10),
    ("2 hrs", 120),
    ("1 hr", 60),
    ("15 minutes", 15),   # regression: long form still works
    ("30m", 30),          # regression: bare unit still works
])
async def test_reminder_minutes_accepts_abbreviations(reminder, expected, calendar_db):
    owner = "tester-" + uuid.uuid4().hex[:6]
    res = await _create_with_reminder(reminder, owner)
    assert res.get("exit_code") == 0, res
    assert f"reminder {expected} min before" in res.get("response", ""), res

    db = calendar_db()
    try:
        note = (
            db.query(Note)
            .filter(Note.owner == owner, Note.title == "Reminder: Dentist")
            .first()
        )
        assert note is not None, "reminder note should have been created"
    finally:
        db.close()


async def test_no_reminder_when_offset_absent():
    owner = "tester-" + uuid.uuid4().hex[:6]
    from src.tool_implementations import do_manage_calendar

    payload = {
        "action": "create_event",
        "summary": "No Reminder Event",
        "dtstart": "2030-02-01T10:00:00",
    }
    res = await do_manage_calendar(json.dumps(payload), owner=owner)
    assert res.get("exit_code") == 0, res
    assert "reminder set" not in res.get("response", ""), res

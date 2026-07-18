"""CalDAV sync: a VEVENT uid held by another local calendar is skipped.

CalendarEvent.uid is the global primary key, but CalDAV only guarantees a uid
is unique per collection. Two failure modes bracket the design:

- The original unscoped lookup returned whatever row held the uid — including
  another owner's — and the sync reassigned its calendar_id, stealing the
  event across calendars/owners.
- Scoping the lookup to the synced calendar (the first fix) stopped the theft
  but made the sync INSERT a duplicate uid whenever the same event legitimately
  exists elsewhere locally — e.g. a Copal event mirrored to Google coming back
  on the pull. The uid PK rejected the batch and rolled back EVERY event of
  that calendar, every sync (live failure 2026-07-17).

Now _find_existing_event returns _UID_ELSEWHERE for a uid living under any
other calendar and the sync skips that VEVENT: no theft, no crash, the local
row stays authoritative.
"""
import tempfile
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import CalendarEvent, CalendarCal
from src.caldav_sync import _UID_ELSEWHERE, _find_existing_event

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(f"sqlite:///{_TMPDB.name}", connect_args={"check_same_thread": False}, poolclass=NullPool)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


def _setup():
    db = _TS()
    try:
        db.query(CalendarEvent).delete(); db.query(CalendarCal).delete()
        db.add(CalendarCal(id="calA", owner="alice", name="A"))
        db.add(CalendarCal(id="calB", owner="bob", name="B"))
        # dtstart/dtend are NOT NULL in the schema, so seed valid values.
        db.add(CalendarEvent(
            uid="shared@svc", calendar_id="calA", summary="Alice event",
            dtstart=datetime(2026, 6, 4, 9, 0), dtend=datetime(2026, 6, 4, 10, 0),
        ))
        db.commit()
    finally:
        db.close()


def test_uid_in_another_calendar_returns_skip_sentinel():
    _setup()
    db = _TS()
    try:
        # Bob's calendar syncing the same uid must neither resolve Alice's row
        # (theft) nor None (insert → uid PK violation → whole batch rolled
        # back). The sentinel tells the sync to skip the VEVENT.
        assert _find_existing_event(db, {}, "shared@svc", "calB") is _UID_ELSEWHERE
        # Same calendar still resolves its own event (normal update path).
        own = _find_existing_event(db, {}, "shared@svc", "calA")
        assert own is not None and own.calendar_id == "calA"
        # Unknown uid is a genuinely new event.
        assert _find_existing_event(db, {}, "new@svc", "calB") is None
    finally:
        db.close()


def test_alice_event_is_not_moved():
    _setup()
    db = _TS()
    try:
        assert _find_existing_event(db, {}, "shared@svc", "calB") is _UID_ELSEWHERE
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == "shared@svc").first()
        assert ev.calendar_id == "calA"  # unchanged — not hijacked
    finally:
        db.close()


def test_pending_takes_precedence():
    _setup()
    db = _TS()
    try:
        sentinel = object()
        assert _find_existing_event(db, {"shared@svc": sentinel}, "shared@svc", "calB") is sentinel
    finally:
        db.close()

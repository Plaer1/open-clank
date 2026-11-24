"""CalDAV pull survives its own write-backs coming home (uid PK round-trip).

Live failure 2026-07-17: Copal events mirrored to Google carry the same uid as
their local rows (which live under the Copal calendar, not the CalDAV one).
The pull-sync's calendar-scoped lookup missed those rows, INSERTed duplicates,
and the uid primary key rejected the batch — rolling back EVERY event of the
primary calendar on every sync ("UNIQUE constraint failed:
calendar_events.uid"). The sync must skip a VEVENT whose uid already lives
under another local calendar while still admitting the rest of the batch.
"""
import sys
import tempfile
import types
from datetime import datetime, timedelta

import core.database as cdb
from core.database import CalendarCal, CalendarEvent
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from src import caldav_sync

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)

_URL = "https://cal.example.test/dav/primary/"
_TOMORROW = (datetime.utcnow() + timedelta(days=1)).strftime("%Y%m%dT100000Z")


def _vevent(uid: str, summary: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nSUMMARY:{summary}\r\n"
        f"DTSTART:{_TOMORROW}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


class _FakeObj:
    def __init__(self, data):
        self.data = data


class _FakeCalendar:
    url = _URL
    name = "Primary"

    def date_search(self, start, end, expand=False):
        # The server echoes back a mirrored local event AND a genuinely new
        # one, in one batch — pre-fix the duplicate poisoned the whole commit.
        return [
            _FakeObj(_vevent("mirrored@local", "Exterminate ants (server copy)")),
            _FakeObj(_vevent("fresh@server", "Dentist")),
        ]


class _FakeClient:
    def __init__(self, url=None, username=None, password=None):
        self.url = url
        self.session = types.SimpleNamespace(max_redirects=30)

    def principal(self):
        raise RuntimeError("no discovery on this server")

    def calendar(self, url=None):
        return _FakeCalendar()

    def close(self):
        self.closed = True


def _install_fake_caldav(monkeypatch):
    fake = types.ModuleType("caldav")
    fake.DAVClient = _FakeClient
    err = types.ModuleType("caldav.lib.error")

    class AuthorizationError(Exception):
        pass

    class NotFoundError(Exception):
        pass

    err.AuthorizationError = AuthorizationError
    err.NotFoundError = NotFoundError
    lib = types.ModuleType("caldav.lib")
    lib.error = err
    fake.lib = lib
    monkeypatch.setitem(sys.modules, "caldav", fake)
    monkeypatch.setitem(sys.modules, "caldav.lib", lib)
    monkeypatch.setitem(sys.modules, "caldav.lib.error", err)
    monkeypatch.setattr(caldav_sync, "SessionLocal", _TS, raising=False)
    monkeypatch.setattr(cdb, "SessionLocal", _TS, raising=False)


def test_pull_skips_mirrored_uid_and_admits_the_rest(monkeypatch):
    _install_fake_caldav(monkeypatch)
    db = _TS()
    try:
        db.query(CalendarEvent).delete()
        db.query(CalendarCal).delete()
        db.add(CalendarCal(id="copal-cal", owner="alice", name="Copal", source="local"))
        db.add(CalendarEvent(
            uid="mirrored@local", calendar_id="copal-cal", summary="Exterminate ants.",
            origin="copal",
            dtstart=datetime.utcnow() + timedelta(days=1),
            dtend=datetime.utcnow() + timedelta(days=1, hours=1),
        ))
        db.commit()
    finally:
        db.close()

    result = caldav_sync._sync_blocking("alice", _URL, "user", "pw")

    assert result["errors"] == [], result
    assert result["events"] == 1, "only the genuinely new event counts"

    db = _TS()
    try:
        mirrored = db.query(CalendarEvent).filter(CalendarEvent.uid == "mirrored@local").one()
        assert mirrored.calendar_id == "copal-cal", "local row not stolen"
        assert mirrored.summary == "Exterminate ants.", "local row not overwritten"
        fresh = db.query(CalendarEvent).filter(CalendarEvent.uid == "fresh@server").one()
        assert fresh.calendar_id != "copal-cal"
        assert fresh.summary == "Dentist"
        assert fresh.origin == "caldav"
    finally:
        db.close()

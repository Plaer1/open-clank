from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, CalendarCal, CalendarEvent
from src.openclank.copal_calendar_projection import (
    ORIGIN,
    build_projection,
    calendar_id_for,
    reconcile_projection,
)


def _sessions():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[CalendarCal.__table__, CalendarEvent.__table__])
    return sessionmaker(bind=engine)


def _planning(tasks):
    return {
        "tracks": [
            {"id": "alpha", "name": "Alpha", "color": "#123456", "tasks": tasks},
        ]
    }


def test_mapping_uses_inclusive_dates_anchors_auto_and_shared_deduplication():
    events, diagnostics = build_projection(
        {
            "tracks": [
                {
                    "id": "a",
                    "name": "Track A",
                    "tasks": [
                        {"id": "exact", "title": "Exact", "startDate": "2026-07-10", "dueDate": "2026-07-12"},
                        {"id": "fuzzy", "title": "Fuzzy", "startDate": "FUZZY", "fuzzy": {"anchorStart": "2026-07-20", "anchorEnd": "2026-07-22"}},
                        {"id": "shared-a", "linkId": "same", "title": "Shared", "startDate": "2026-07-15", "dueDate": "2026-07-15"},
                        {"id": "auto", "title": "Auto", "startDate": "AUTO"},
                        {"id": "unknown", "title": "Unknown", "startDate": "FUZZY"},
                    ],
                },
                {
                    "id": "b",
                    "name": "Track B",
                    "tasks": [
                        {"id": "shared-b", "linkId": "same", "title": "Shared copy", "startDate": "2026-07-15", "dueDate": "2026-07-16"},
                    ],
                },
            ]
        },
        owner="owner@example.test",
        workspace="default",
        planning_document_id="PLAN",
    )

    by_id = {event.logical_id: event for event in events}
    assert set(by_id) == {"exact", "fuzzy", "same", "auto"}
    assert by_id["exact"].end_exclusive.isoformat() == "2026-07-13"
    assert by_id["fuzzy"].start.isoformat() == "2026-07-20"
    assert by_id["same"].end_exclusive.isoformat() == "2026-07-17"
    assert by_id["auto"].start.isoformat() == "2026-07-23"
    assert any(item["taskId"] == "unknown" and "unresolved" in item["reason"] for item in diagnostics)
    assert any("deduplicated" in item["reason"] for item in diagnostics)


def test_reconcile_is_idempotent_updates_and_deletes_only_owned_mirrors(monkeypatch):
    monkeypatch.setenv("COPAL_CALENDAR_PROJECTION_ENABLED", "1")
    sessions = _sessions()
    owner = "owner@example.test"
    workspace = "personal"
    planning = _planning([
        {"id": "one", "title": "One", "startDate": "2026-08-01", "dueDate": "2026-08-02", "priority": "high"},
        {"id": "two", "title": "Two", "startDate": "2026-08-03", "dueDate": None},
    ])

    first = reconcile_projection(
        planning,
        owner=owner,
        workspace=workspace,
        planning_document_id="PLAN",
        source_revision="head-1",
        session_factory=sessions,
    )
    second = reconcile_projection(
        planning,
        owner=owner,
        workspace=workspace,
        planning_document_id="PLAN",
        source_revision="head-1",
        session_factory=sessions,
    )
    assert (first["created"], first["updated"], first["deleted"]) == (2, 0, 0)
    assert (second["created"], second["updated"], second["deleted"], second["unchanged"]) == (0, 0, 0, 2)
    assert first["sourceHash"] == second["sourceHash"]

    db = sessions()
    calendar_id = calendar_id_for(owner, workspace)
    calendar = db.query(CalendarCal).filter_by(id=calendar_id).one()
    assert (calendar.name, calendar.source, calendar.owner) == ("Copal · personal", "local", owner)
    db.add(CalendarEvent(
        uid="native-user-event",
        calendar_id=calendar_id,
        summary="Do not touch",
        dtstart=datetime(2026, 8, 1),
        dtend=datetime(2026, 8, 2),
        all_day=True,
        origin="local",
    ))
    db.commit()
    db.close()

    changed = _planning([
        {"id": "one", "title": "One changed", "startDate": "2026-08-04", "dueDate": "2026-08-04"},
    ])
    result = reconcile_projection(
        changed,
        owner=owner,
        workspace=workspace,
        planning_document_id="PLAN",
        source_revision="head-2",
        session_factory=sessions,
    )
    assert (result["created"], result["updated"], result["deleted"]) == (0, 1, 1)

    db = sessions()
    rows = db.query(CalendarEvent).filter_by(calendar_id=calendar_id).all()
    assert {row.uid for row in rows if row.origin != ORIGIN} == {"native-user-event"}
    projected = next(row for row in rows if row.origin == ORIGIN)
    assert projected.summary == "One changed"
    assert projected.dtend == datetime(2026, 8, 5)
    db.close()


def test_disabled_projection_has_no_calendar_side_effect(monkeypatch):
    monkeypatch.setenv("COPAL_CALENDAR_PROJECTION_ENABLED", "false")
    sessions = _sessions()
    result = reconcile_projection(
        _planning([{"id": "one", "title": "One", "startDate": "2026-08-01"}]),
        owner="owner@example.test",
        workspace="default",
        planning_document_id="PLAN",
        session_factory=sessions,
    )
    assert result["enabled"] is False
    db = sessions()
    assert db.query(CalendarCal).count() == 0
    db.close()

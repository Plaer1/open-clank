import json

import pytest

from src.openclank.copal_planning import (
    PlanningValidationError,
    event_from_document,
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
        "kind": "markdown",
        "name": "Events/Move.md",
        "text": serialize_event(event, tracks=TRACKS),
    }


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

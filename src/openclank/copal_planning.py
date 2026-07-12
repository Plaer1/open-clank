"""Canonical Copal event-note model and legacy planning migration helpers."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import date
from pathlib import PurePosixPath
from typing import Any, Iterable


EVENT_KIND = "markdown"
TRACKS_KIND = "copal-tracks"
MIGRATION_KIND = "copal-migration"
TRACKS_NAME = ".copal/tracks.json"
MIGRATION_NAME = ".copal/planning-migration.json"
EVENT_SCHEMA = 1
TRACK_SCHEMA = 1

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_EVENT_FIELDS = (
    "title",
    "startDate",
    "dueDate",
    "status",
    "priority",
    "trackId",
    "sharedTrackIds",
    "tags",
    "linkId",
    "fuzzy",
    "fadeDays",
    "titleStart",
    "titleEnd",
    "stages",
    "floating",
)
_RESERVED_FRONTMATTER = {"copal_type", "copal_schema", "copal_legacy_id", "copal_extra", *_EVENT_FIELDS}
_STATUS = {"pending", "in-progress", "done", "ongoing"}
_PRIORITY = {"low", "medium", "high", "normal", "critical"}


class PlanningValidationError(ValueError):
    pass


def _json_value(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value.strip("'\"")


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse Copal's deliberately flat JSON-valued frontmatter."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        raise PlanningValidationError("Unterminated frontmatter block")
    values: dict[str, Any] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        if key:
            values[key] = _json_value(raw)
    body_start = end + 4
    if text[body_start:body_start + 1] == "\n":
        body_start += 1
    return values, text[body_start:]


def _render_frontmatter(values: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for key, value in values.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}")
    lines.extend(["---", body.rstrip("\n")])
    return "\n".join(lines).rstrip() + "\n"


def _iso_day(value: Any, field: str, *, nullable: bool = True) -> str | None:
    if value in (None, "") and nullable:
        return None
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise PlanningValidationError(f"{field} must be an ISO date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise PlanningValidationError(f"{field} is not a real calendar day") from exc
    return value


def _strings(value: Any, field: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise PlanningValidationError(f"{field} must be a list")
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def validate_event(event: dict[str, Any], tracks: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    value = deepcopy(event)
    title = str(value.get("title") or "").strip()
    value["title"] = title or "Untitled event"
    value["description"] = str(value.get("description") or "")

    start = value.get("startDate")
    if start not in (None, "", "AUTO", "FUZZY"):
        start = _iso_day(start, "startDate", nullable=False)
    value["startDate"] = start or None
    value["dueDate"] = _iso_day(value.get("dueDate"), "dueDate")

    fuzzy = value.get("fuzzy")
    if fuzzy in (None, ""):
        fuzzy = None
    elif not isinstance(fuzzy, dict):
        raise PlanningValidationError("fuzzy must be an object")
    else:
        fuzzy = deepcopy(fuzzy)
        for key in ("anchorStart", "anchorEnd", "whiskerStart"):
            if key in fuzzy:
                fuzzy[key] = _iso_day(fuzzy.get(key), f"fuzzy.{key}")
        if "fadeIn" in fuzzy:
            fuzzy["fadeIn"] = bool(fuzzy["fadeIn"])
    value["fuzzy"] = fuzzy

    if value["startDate"] == "FUZZY" and not fuzzy:
        raise PlanningValidationError("FUZZY events need at least one fuzzy anchor")
    if isinstance(value["startDate"], str) and _DATE_RE.fullmatch(value["startDate"]) and value["dueDate"]:
        if date.fromisoformat(value["dueDate"]) < date.fromisoformat(value["startDate"]):
            raise PlanningValidationError("dueDate cannot precede startDate")

    status = str(value.get("status") or "pending")
    priority = str(value.get("priority") or "medium")
    if status not in _STATUS:
        raise PlanningValidationError(f"Unsupported status: {status}")
    if priority not in _PRIORITY:
        raise PlanningValidationError(f"Unsupported priority: {priority}")
    value["status"] = status
    value["priority"] = priority

    track_ids = {str(track.get("id")) for track in tracks if track.get("id")}
    primary = str(value.get("trackId") or "").strip() or None
    if primary and track_ids and primary not in track_ids:
        raise PlanningValidationError(f"Unknown main track: {primary}")
    shared = [item for item in _strings(value.get("sharedTrackIds"), "sharedTrackIds") if item != primary]
    if track_ids:
        missing = [item for item in shared if item not in track_ids]
        if missing:
            raise PlanningValidationError(f"Unknown shared track: {missing[0]}")
    value["trackId"] = primary
    value["sharedTrackIds"] = shared
    value["tags"] = _strings(value.get("tags"), "tags")

    fade_days = value.get("fadeDays")
    if fade_days in (None, ""):
        fade_days = 0
    if isinstance(fade_days, bool) or not isinstance(fade_days, (int, float)) or fade_days < 0:
        raise PlanningValidationError("fadeDays must be a non-negative number")
    value["fadeDays"] = int(fade_days)
    for field in ("linkId", "titleStart", "titleEnd"):
        value[field] = str(value.get(field) or "").strip() or None

    stages = value.get("stages") or []
    if not isinstance(stages, list):
        raise PlanningValidationError("stages must be a list")
    normalized_stages: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise PlanningValidationError("Each stage must be an object")
        current = deepcopy(stage)
        current["title"] = str(current.get("title") or current.get("text") or "").strip() or f"Stage {index + 1}"
        current["done"] = bool(current.get("done"))
        current["date"] = _iso_day(current.get("date"), f"stages[{index}].date")
        if not current.get("id"):
            digest = hashlib.sha1(f"{index}\n{current['title']}".encode()).hexdigest()[:10]
            current["id"] = f"stage-{digest}"
        normalized_stages.append(current)
    value["stages"] = normalized_stages
    value["floating"] = bool(value.get("floating") or not primary)
    if not isinstance(value.get("copal_extra", {}), dict):
        raise PlanningValidationError("copal_extra must be an object")
    return value


def event_from_document(doc: dict[str, Any]) -> dict[str, Any] | None:
    try:
        properties, body = split_frontmatter(str(doc.get("text") or ""))
    except PlanningValidationError:
        return None
    if properties.get("copal_type") != "event":
        return None
    event = {field: deepcopy(properties.get(field)) for field in _EVENT_FIELDS}
    event.update({
        "id": str(doc.get("id") or ""),
        "documentId": str(doc.get("id") or ""),
        "head": doc.get("head"),
        "name": doc.get("name"),
        "description": body,
        "legacyId": properties.get("copal_legacy_id"),
        "copal_extra": deepcopy(properties.get("copal_extra") or {}),
        "_frontmatterExtra": {
            key: deepcopy(value)
            for key, value in properties.items()
            if key not in _RESERVED_FRONTMATTER
        },
    })
    if not event.get("title"):
        event["title"] = PurePosixPath(str(doc.get("name") or "Untitled")).stem
    try:
        return validate_event(event)
    except PlanningValidationError:
        event["invalid"] = True
        return event


def serialize_event(event: dict[str, Any], *, tracks: Iterable[dict[str, Any]] = ()) -> str:
    value = validate_event(event, tracks)
    properties = deepcopy(value.get("_frontmatterExtra") or {})
    properties.update({
        "copal_type": "event",
        "copal_schema": EVENT_SCHEMA,
    })
    if value.get("legacyId"):
        properties["copal_legacy_id"] = value["legacyId"]
    for field in _EVENT_FIELDS:
        properties[field] = deepcopy(value.get(field))
    if value.get("copal_extra"):
        properties["copal_extra"] = deepcopy(value["copal_extra"])
    return _render_frontmatter(properties, value.get("description") or "")


def merge_event(current: dict[str, Any], patch: dict[str, Any], tracks: Iterable[dict[str, Any]]) -> dict[str, Any]:
    allowed = {*_EVENT_FIELDS, "description"}
    unexpected = sorted(set(patch) - allowed)
    if unexpected:
        raise PlanningValidationError(f"Unsupported event field: {unexpected[0]}")
    merged = deepcopy(current)
    for key, value in patch.items():
        merged[key] = deepcopy(value)
    return validate_event(merged, tracks)


def validate_tracks(tracks: Any) -> list[dict[str, Any]]:
    if not isinstance(tracks, list):
        raise PlanningValidationError("tracks must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, track in enumerate(tracks):
        if not isinstance(track, dict):
            raise PlanningValidationError("Each track must be an object")
        current = {key: deepcopy(value) for key, value in track.items() if key != "tasks"}
        track_id = str(current.get("id") or "").strip()
        if not track_id or track_id in seen:
            raise PlanningValidationError(f"Track {index + 1} needs a unique id")
        seen.add(track_id)
        current["id"] = track_id
        current["name"] = str(current.get("name") or "").strip()
        if not current["name"]:
            raise PlanningValidationError(f"Track {track_id} needs a name")
        color = str(current.get("color") or "#14b8a6")
        if not _COLOR_RE.fullmatch(color):
            raise PlanningValidationError(f"Track {track_id} has an invalid color")
        current["color"] = color.lower()
        current["icon"] = str(current.get("icon") or "•")[:32]
        current["enabled"] = current.get("enabled") is not False
        result.append(current)
    return result


def serialize_track_registry(tracks: list[dict[str, Any]], metadata: dict[str, Any] | None = None) -> str:
    payload = deepcopy(metadata or {})
    payload.update({"schemaVersion": TRACK_SCHEMA, "tracks": validate_tracks(tracks)})
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def track_registry_from_document(doc: dict[str, Any] | None) -> dict[str, Any]:
    if not doc:
        return {"schemaVersion": TRACK_SCHEMA, "tracks": []}
    try:
        payload = json.loads(str(doc.get("text") or "{}"))
    except json.JSONDecodeError as exc:
        raise PlanningValidationError("Track registry is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PlanningValidationError("Track registry root must be an object")
    payload = deepcopy(payload)
    payload["tracks"] = validate_tracks(payload.get("tracks") or [])
    return payload


def canonical_documents(docs: Iterable[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    docs = list(docs)
    registry = next((doc for doc in docs if doc.get("kind") == TRACKS_KIND), None)
    marker = next((doc for doc in docs if doc.get("kind") == MIGRATION_KIND), None)
    events = [event for doc in docs if (event := event_from_document(doc)) is not None]
    return registry, marker, events


def planning_projection(docs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    docs = list(docs)
    registry_doc, marker_doc, events = canonical_documents(docs)
    if not registry_doc and not events:
        legacy = next((doc for doc in docs if doc.get("kind") == "planning"), None)
        if legacy:
            try:
                payload = json.loads(str(legacy.get("text") or "{}"))
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                return {
                    **payload,
                    "canonical": False,
                    "migrationRequired": True,
                    "legacyDocument": {"id": legacy.get("id"), "head": legacy.get("head")},
                }

    registry = track_registry_from_document(registry_doc)
    tracks = [dict(track, tasks=[]) for track in registry.get("tracks") or []]
    by_id = {track["id"]: track for track in tracks}
    floating: list[dict[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    for event in events:
        clean = {key: deepcopy(value) for key, value in event.items() if not key.startswith("_")}
        track = by_id.get(str(event.get("trackId") or ""))
        if track:
            track["tasks"].append(clean)
        else:
            floating.append(clean)
            if event.get("trackId"):
                diagnostics.append({"eventId": event["id"], "reason": f"unknown track {event['trackId']}"})

    marker: dict[str, Any] | None = None
    if marker_doc:
        try:
            marker = json.loads(str(marker_doc.get("text") or "{}"))
        except json.JSONDecodeError:
            marker = {"state": "invalid"}
    metadata = {key: deepcopy(value) for key, value in registry.items() if key not in {"schemaVersion", "tracks"}}
    return {
        **metadata,
        "schemaVersion": TRACK_SCHEMA,
        "canonical": True,
        "migrationRequired": False,
        "tracks": tracks,
        "floatingTodos": floating,
        "trackRegistry": {"id": registry_doc.get("id"), "head": registry_doc.get("head")} if registry_doc else None,
        "migration": marker,
        "diagnostics": diagnostics,
    }


def legacy_inventory(planning_doc: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(str(planning_doc.get("text") or "{}"))
    except json.JSONDecodeError as exc:
        raise PlanningValidationError("Legacy planning document is not valid JSON") from exc
    if not isinstance(data, dict):
        raise PlanningValidationError("Legacy planning root must be an object")
    tracks = validate_tracks(data.get("tracks") or [])
    events: list[dict[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    seen: set[str] = set()
    for source_track in data.get("tracks") or []:
        track_id = str(source_track.get("id") or "")
        for index, task in enumerate(source_track.get("tasks") or []):
            if not isinstance(task, dict):
                diagnostics.append({"reason": f"non-object task on track {track_id}"})
                continue
            legacy_id = str(task.get("id") or f"{track_id}-{index}")
            if legacy_id in seen:
                diagnostics.append({"legacyId": legacy_id, "reason": "duplicate task id"})
                legacy_id = f"{legacy_id}-{index}"
            seen.add(legacy_id)
            known = {key: deepcopy(task.get(key)) for key in _EVENT_FIELDS if key in task}
            extra = {key: deepcopy(value) for key, value in task.items() if key not in {*_EVENT_FIELDS, "id", "description"}}
            known.update({
                "legacyId": legacy_id,
                "title": task.get("title") or task.get("text") or "Untitled event",
                "description": task.get("description") or "",
                "trackId": track_id,
                "copal_extra": extra,
            })
            events.append(validate_event(known, tracks))
    for index, task in enumerate(data.get("floatingTodos") or []):
        if not isinstance(task, dict):
            continue
        legacy_id = str(task.get("id") or f"floating-{index}")
        extra = {key: deepcopy(value) for key, value in task.items() if key not in {*_EVENT_FIELDS, "id", "description", "text"}}
        event = {key: deepcopy(task.get(key)) for key in _EVENT_FIELDS if key in task}
        event.update({
            "legacyId": legacy_id,
            "title": task.get("title") or task.get("text") or "Untitled task",
            "description": task.get("description") or "",
            "status": task.get("status") or ("done" if task.get("done") else "pending"),
            "trackId": None,
            "floating": True,
            "copal_extra": extra,
        })
        events.append(validate_event(event, tracks))
    metadata = {key: deepcopy(value) for key, value in data.items() if key not in {"tracks", "floatingTodos"}}
    return {"tracks": tracks, "events": events, "metadata": metadata, "diagnostics": diagnostics}


def event_document_name(event: dict[str, Any]) -> str:
    legacy = str(event.get("legacyId") or event.get("id") or "event")
    title = _SLUG_RE.sub("-", str(event.get("title") or "event").lower()).strip("-")[:48] or "event"
    suffix = hashlib.sha1(legacy.encode()).hexdigest()[:10]
    return f"Events/{title}--{suffix}.md"


def revision_fingerprint(docs: Iterable[dict[str, Any]]) -> str:
    heads = sorted(f"{doc.get('id')}:{doc.get('head')}" for doc in docs)
    return hashlib.sha256("\n".join(heads).encode()).hexdigest()

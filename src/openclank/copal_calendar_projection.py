"""One-way Copal planning projection into Odysseus's native calendar store.

Copal/Redb remains canonical.  This module only owns rows in one deterministic
local calendar per Copal owner/workspace and never participates in native
calendar reads.  Reconciliation is deliberately idempotent so a failed
second-store write can be retried without rolling back a committed Copal edit.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable

from core.database import CalendarCal, CalendarEvent, SessionLocal


CALENDAR_NAMESPACE = uuid.UUID("d1ea9683-d50d-5f8e-8ca7-f0eaf76125f4")
EVENT_NAMESPACE = uuid.UUID("7ee55d82-55bf-534d-a339-f6680bc13a67")
ORIGIN = "copal"


@dataclass(frozen=True)
class ProjectedEvent:
    task_id: str
    logical_id: str
    uid: str
    summary: str
    description: str
    start: date
    end_exclusive: date
    color: str | None
    importance: str
    status: str


def projection_enabled() -> bool:
    return os.environ.get("COPAL_CALENDAR_PROJECTION_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _parse_day(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed


def _all_tasks(planning: dict[str, Any]) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    for track in planning.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        for task in track.get("tasks") or []:
            if isinstance(task, dict):
                yield track, task


def _event_type(tags: list[str]) -> str | None:
    lowered = {tag.lower() for tag in tags}
    for name in ("travel", "health", "meal", "social", "personal", "work", "admin"):
        if name in lowered:
            return name
    return None


def build_projection(
    planning: dict[str, Any],
    *,
    owner: str,
    workspace: str,
    planning_document_id: str,
    window_start: str | date | None = None,
    window_end: str | date | None = None,
) -> tuple[list[ProjectedEvent], list[dict[str, str]]]:
    """Map planning JSON to deterministic all-day native events.

    Exact ranges are inclusive in Copal and therefore gain one day for native
    ``dtend``.  FUZZY tasks use only explicit anchors.  AUTO is derived only
    when another visible task supplies a defensible latest end; no date is
    invented for unresolved records.

    Recurring tasks are expanded into per-occurrence projections with UIDs
    derived from seriesId + occurrenceKey. Non-recurring tasks keep their
    existing UID logic.
    """
    from src.openclank.copal_planning import expand_recurrence, _DATE_RE

    rows = list(_all_tasks(planning))
    known_ends: list[date] = []
    for _, task in rows:
        fuzzy = task.get("fuzzy") if isinstance(task.get("fuzzy"), dict) else {}
        end = _parse_day(task.get("dueDate")) or _parse_day(fuzzy.get("anchorEnd"))
        start = _parse_day(task.get("startDate")) or _parse_day(fuzzy.get("anchorStart"))
        if end or start:
            known_ends.append(end or start)  # type: ignore[arg-type]
    auto_start = max(known_ends) + timedelta(days=1) if known_ends else None

    # Default expansion window: 1 year from today if not specified
    today = date.today()
    if window_start is None:
        window_start = today - timedelta(days=30)
    elif isinstance(window_start, str):
        window_start = date.fromisoformat(window_start)
    if window_end is None:
        window_end = today + timedelta(days=365)
    elif isinstance(window_end, str):
        window_end = date.fromisoformat(window_end)

    selected: dict[str, ProjectedEvent] = {}
    diagnostics: list[dict[str, str]] = []
    for track, task in rows:
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            diagnostics.append({"taskId": "", "reason": "missing stable task ID"})
            continue
        logical_id = str(task.get("linkId") or task_id).strip()

        # Recurring tasks: expand into per-occurrence projections
        recurrence = task.get("recurrence") if isinstance(task.get("recurrence"), dict) else None
        if recurrence:
            occurrences = expand_recurrence(task, window_start, window_end)
            track_name = str(track.get("name") or track.get("id") or "Planning")
            tags = [str(tag) for tag in task.get("tags") or [] if str(tag).strip()]
            status = str(task.get("status") or "pending")
            priority = str(task.get("priority") or "normal").lower()
            importance = priority if priority in {"low", "normal", "high", "critical"} else "normal"
            base_desc_parts = [str(task.get("description") or "").strip()]
            base_desc_parts.extend([
                f"Copal track: {track_name}",
                f"Status: {status}",
                f"Source: /copal/timeline?doc={planning_document_id}",
            ])
            if tags:
                base_desc_parts.append(f"Tags: {', '.join(tags)}")
            summary = str(task.get("title") or task.get("text") or "Untitled Copal task")[:512]
            for occ in occurrences:
                occ_key = occ["occurrenceKey"]
                occ_uid = str(uuid.uuid5(EVENT_NAMESPACE, f"{owner}\n{workspace}\n{planning_document_id}\n{logical_id}\n{occ_key}"))
                occ_start = _parse_day(occ["startDate"])
                occ_end = _parse_day(occ.get("dueDate")) or occ_start
                if not occ_start or not occ_end:
                    continue
                occ_desc = base_desc_parts + [f"Occurrence: {occ_key}"]
                selected[occ_uid] = ProjectedEvent(
                    task_id=task_id,
                    logical_id=logical_id,
                    uid=occ_uid,
                    summary=summary,
                    description="\n".join(part for part in occ_desc if part),
                    start=occ_start,
                    end_exclusive=occ_end + timedelta(days=1),
                    color=str(track.get("color") or "").strip() or None,
                    importance=importance,
                    status=status,
                )
            continue

        fuzzy = task.get("fuzzy") if isinstance(task.get("fuzzy"), dict) else {}
        start = _parse_day(task.get("startDate")) or _parse_day(fuzzy.get("anchorStart"))
        end = _parse_day(task.get("dueDate")) or _parse_day(fuzzy.get("anchorEnd"))
        if task.get("startDate") == "AUTO":
            start = auto_start
        if not start and end:
            start = end
        if start and not end:
            end = start
        if not start or not end:
            diagnostics.append({"taskId": task_id, "reason": "unresolved date or fuzzy anchors"})
            continue
        if end < start:
            diagnostics.append({"taskId": task_id, "reason": "end precedes start"})
            continue

        track_name = str(track.get("name") or track.get("id") or "Planning")
        tags = [str(tag) for tag in task.get("tags") or [] if str(tag).strip()]
        status = str(task.get("status") or "pending")
        description_parts = [str(task.get("description") or "").strip()]
        description_parts.extend([
            f"Copal track: {track_name}",
            f"Status: {status}",
            f"Source: /copal/timeline?doc={planning_document_id}",
        ])
        if tags:
            description_parts.append(f"Tags: {', '.join(tags)}")
        priority = str(task.get("priority") or "normal").lower()
        importance = priority if priority in {"low", "normal", "high", "critical"} else "normal"
        uid = str(uuid.uuid5(EVENT_NAMESPACE, f"{owner}\n{workspace}\n{planning_document_id}\n{logical_id}"))
        event = ProjectedEvent(
            task_id=task_id,
            logical_id=logical_id,
            uid=uid,
            summary=str(task.get("title") or task.get("text") or "Untitled Copal task")[:512],
            description="\n".join(part for part in description_parts if part),
            start=start,
            end_exclusive=end + timedelta(days=1),
            color=str(track.get("color") or "").strip() or None,
            importance=importance,
            status=status,
        )
        existing = selected.get(logical_id)
        if existing is None:
            selected[logical_id] = event
        else:
            # A linkId can intentionally repeat one logical event on multiple
            # tracks.  Prefer the broadest explicit range and record that it
            # was folded, rather than creating duplicate native events.
            selected[logical_id] = ProjectedEvent(
                task_id=existing.task_id,
                logical_id=logical_id,
                uid=existing.uid,
                summary=existing.summary,
                description=f"{existing.description}\nAlso linked from: {track_name}",
                start=min(existing.start, event.start),
                end_exclusive=max(existing.end_exclusive, event.end_exclusive),
                color=existing.color,
                importance=existing.importance,
                status=existing.status,
            )
            diagnostics.append({"taskId": task_id, "reason": f"deduplicated shared linkId {logical_id}"})

    return sorted(selected.values(), key=lambda item: (item.start, item.uid)), diagnostics


def calendar_id_for(owner: str, workspace: str) -> str:
    return str(uuid.uuid5(CALENDAR_NAMESPACE, f"{owner}\n{workspace}"))


def _fingerprint(events: list[ProjectedEvent]) -> str:
    payload = [
        {
            **asdict(event),
            "start": event.start.isoformat(),
            "end_exclusive": event.end_exclusive.isoformat(),
        }
        for event in events
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def reconcile_projection(
    planning: dict[str, Any],
    *,
    owner: str,
    workspace: str,
    planning_document_id: str,
    source_revision: str | None = None,
    session_factory: Callable[[], Any] = SessionLocal,
) -> dict[str, Any]:
    """Create/update/delete only Copal-owned mirrors in the dedicated calendar."""

    if not projection_enabled():
        return {"enabled": False, "created": 0, "updated": 0, "deleted": 0, "diagnostics": []}

    desired, diagnostics = build_projection(
        planning,
        owner=owner,
        workspace=workspace,
        planning_document_id=planning_document_id,
    )
    calendar_id = calendar_id_for(owner, workspace)
    desired_by_uid = {event.uid: event for event in desired}
    db = session_factory()
    created = updated = deleted = 0
    try:
        calendar = db.query(CalendarCal).filter(CalendarCal.id == calendar_id).first()
        if calendar and calendar.owner != owner:
            raise RuntimeError("deterministic Copal calendar ID is owned by another user")
        if not calendar:
            calendar = CalendarCal(
                id=calendar_id,
                owner=owner,
                name=f"Copal · {workspace}",
                color="#14b8a6",
                source="local",
            )
            db.add(calendar)
            db.flush()
        else:
            calendar.name = f"Copal · {workspace}"
            calendar.source = "local"

        existing = {
            event.uid: event
            for event in db.query(CalendarEvent).filter(
                CalendarEvent.calendar_id == calendar_id,
                CalendarEvent.origin == ORIGIN,
            ).all()
        }
        for uid, wanted in desired_by_uid.items():
            event = existing.get(uid)
            values = {
                "calendar_id": calendar_id,
                "summary": wanted.summary,
                "description": wanted.description,
                "location": "Copal",
                "dtstart": datetime.combine(wanted.start, datetime.min.time()),
                "dtend": datetime.combine(wanted.end_exclusive, datetime.min.time()),
                "all_day": True,
                "is_utc": False,
                "rrule": "",
                "color": wanted.color,
                "status": "confirmed",
                "importance": wanted.importance,
                "event_type": _event_type([]),
                "origin": ORIGIN,
                "remote_href": None,
                "remote_etag": None,
                "caldav_sync_pending": None,
            }
            if event is None:
                db.add(CalendarEvent(uid=uid, **values))
                created += 1
            else:
                changed = any(getattr(event, key) != value for key, value in values.items())
                for key, value in values.items():
                    setattr(event, key, value)
                updated += int(changed)
        for uid, event in existing.items():
            if uid not in desired_by_uid:
                db.delete(event)
                deleted += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return {
        "enabled": True,
        "calendarId": calendar_id,
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "unchanged": len(desired) - created - updated,
        "sourceRevision": source_revision,
        "sourceHash": _fingerprint(desired),
        "events": [
            {"taskId": event.task_id, "logicalId": event.logical_id, "uid": event.uid}
            for event in desired
        ],
        "diagnostics": diagnostics,
    }

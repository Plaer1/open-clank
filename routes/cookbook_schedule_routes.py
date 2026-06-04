"""Cookbook schedule routes — turns the Cookbook \"Schedule\" modal into
calendar events on the designated schedule calendar, and exposes a
diagnostic /upcoming endpoint for the UI.

All routes live under /api/cookbook/schedule/* so the whole file can be
removed by deleting one router-registration line in app.py. The setup
function is a no-op when `cookbook_scheduler_enabled` is False.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)


_DAYS = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def setup_cookbook_schedule_routes() -> APIRouter:
    router = APIRouter(prefix="/api/cookbook/schedule", tags=["cookbook-schedule"])

    @router.get("/upcoming")
    async def upcoming(request: Request, hours: int = 24):
        """Next N hours of scheduled events with reconciler status.

        Drives the "what's running, what's queued" badges in the
        Cookbook UI. Cheap read — no SSH, just calendar + state file.
        """
        require_admin(request)
        from src.settings import get_setting
        if not get_setting("cookbook_scheduler_enabled", False):
            return {"enabled": False, "events": []}
        calendar_href = get_setting("cookbook_schedule_calendar_href", "") or ""
        if not calendar_href:
            return {"enabled": True, "calendar_href": "", "events": []}

        hours = max(1, min(int(hours or 24), 24 * 14))
        now = datetime.now(timezone.utc)
        end = now + timedelta(hours=hours)
        from src.cookbook_scheduler import _fetch_calendar_events, _read_state
        events = await _fetch_calendar_events(calendar_href, now - timedelta(minutes=5), end)
        state = _read_state()
        tracked = (state.get("scheduler") or {}).get("events") or {}
        out: List[Dict[str, Any]] = []
        for ev in events:
            uid = ev.get("uid") or ev.get("id") or ""
            if not uid:
                continue
            t = tracked.get(uid) or {}
            out.append({
                "uid": uid,
                "title": ev.get("summary") or "",
                "start": ev.get("dtstart") or ev.get("start"),
                "end": ev.get("dtend") or ev.get("end"),
                "status": t.get("status") or "scheduled",
                "reason": t.get("reason") or "",
                "session_id": t.get("session_id") or "",
            })
        return {"enabled": True, "calendar_href": calendar_href, "events": out}

    @router.post("/from-cookbook")
    async def schedule_from_cookbook(request: Request, body: Dict[str, Any] = Body(default_factory=dict)):
        """Create one or more calendar events from the Cookbook Schedule modal.

        Body shape:
          {
            "model": "Qwen3.5-397B-A17B-AWQ",     # display title
            "preset": "Qwen3.5-397B-A17B-AWQ",    # optional, matched to saved preset
            "repo_id": "...",                     # optional, for non-preset launches
            "cmd": "vllm serve ...",              # optional
            "host": "pewds@192.168.1.12",         # optional
            "port": 8003,                         # optional
            "slots": [
              {"start": "09:00", "end": "17:00"}, # one or more time windows per day
              {"start": "21:00", "end": "23:30"}
            ],
            "days": ["MO","TU","WE","TH","FR"],   # weekdays this repeats
            "until": "2026-12-31",                # optional end date, else forever
            "start_date": "2026-06-05"            # optional first day, else today
          }

        Creates one calendar event per slot (so split-shift schedules
        are visible as separate blocks). All events share the same
        RRULE so they can be edited together by changing one.
        """
        require_admin(request)
        from src.settings import get_setting
        if not get_setting("cookbook_scheduler_enabled", False):
            raise HTTPException(400, "Cookbook scheduler is not enabled in Settings.")
        calendar_href = get_setting("cookbook_schedule_calendar_href", "") or ""
        if not calendar_href:
            raise HTTPException(400, "No Cookbook schedule calendar is configured in Settings.")

        title = (body.get("model") or body.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "model (title) is required")
        slots = body.get("slots") or []
        if not isinstance(slots, list) or not slots:
            raise HTTPException(400, "at least one time slot is required")
        for s in slots:
            if not isinstance(s, dict):
                raise HTTPException(400, "slot must be an object")
            if not _HHMM_RE.match(str(s.get("start") or "")):
                raise HTTPException(400, f"slot.start must be HH:MM, got {s.get('start')!r}")
            if not _HHMM_RE.match(str(s.get("end") or "")):
                raise HTTPException(400, f"slot.end must be HH:MM, got {s.get('end')!r}")
        days = [d for d in (body.get("days") or []) if d in _DAYS]
        if not days:
            # Default to every day if the user didn't pick.
            days = list(_DAYS)

        # Compose the cookbook: YAML block dropped into event DESCRIPTION
        # so the reconciler knows how to launch.
        yaml_lines = ["cookbook:"]
        for k in ("preset", "repo_id", "cmd", "host", "port"):
            v = body.get(k)
            if v:
                yaml_lines.append(f"  {k}: {v}")
        if len(yaml_lines) == 1:
            # Fall back: the title alone is the preset name. Reconciler
            # will preset-match against saved presets at launch time.
            yaml_lines.append(f"  preset: {title}")
        description = "\n".join(yaml_lines)

        # First-occurrence date defaults to today (UTC) so the schedule
        # applies starting now. RRULE-BYDAY handles day filtering.
        start_date = body.get("start_date")
        if start_date:
            try:
                d0 = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(400, "start_date must be YYYY-MM-DD")
        else:
            d0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        until = (body.get("until") or "").strip()
        until_clause = ""
        if until:
            try:
                u = datetime.strptime(until, "%Y-%m-%d")
                until_clause = f";UNTIL={u.strftime('%Y%m%dT235959Z')}"
            except ValueError:
                raise HTTPException(400, "until must be YYYY-MM-DD")

        rrule = f"FREQ=WEEKLY;BYDAY={','.join(days)}{until_clause}"

        # Create one event per slot. Call /api/calendar/events directly
        # so we don't reinvent CalDAV plumbing.
        import httpx
        from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
        headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
        created: List[str] = []
        for slot in slots:
            sh, sm = [int(x) for x in str(slot["start"]).split(":")]
            eh, em = [int(x) for x in str(slot["end"]).split(":")]
            dtstart = d0.replace(hour=sh, minute=sm)
            dtend = d0.replace(hour=eh, minute=em)
            if dtend <= dtstart:
                # Overnight: schedule the end on the next day.
                dtend = dtend + timedelta(days=1)
            ev_body = {
                "summary": title,
                "dtstart": dtstart.isoformat(),
                "dtend": dtend.isoformat(),
                "all_day": False,
                "description": description,
                "calendar_href": calendar_href,
                "rrule": rrule,
                "color": "#3b82f6",
            }
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.post(
                        "http://localhost:7000/api/calendar/events",
                        json=ev_body, headers=headers,
                    )
                    if r.status_code >= 400:
                        logger.warning(f"schedule: calendar event create failed: {r.status_code} {r.text[:200]}")
                        continue
                    data = r.json()
            except Exception as e:
                logger.warning(f"schedule: calendar event create errored: {e}")
                continue
            uid = data.get("uid") or data.get("id") or ""
            if uid:
                created.append(uid)
        if not created:
            raise HTTPException(500, "Failed to create any calendar events for this schedule")
        return {"ok": True, "created": created, "slots": len(slots), "rrule": rrule}

    @router.post("/reconcile-now")
    async def reconcile_now(request: Request):
        """Manual kick of the reconciler. Useful for testing + the
        \"Run now\" button in the Cookbook UI."""
        require_admin(request)
        from src.cookbook_scheduler import _reconcile_once
        return await _reconcile_once()

    return router

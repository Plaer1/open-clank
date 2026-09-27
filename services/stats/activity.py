"""Bounded, content-free S08 activity projection."""
from __future__ import annotations

import hashlib
import math
import time
from contextlib import contextmanager
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import and_, or_

from core.database import AgentTurn, ChatMessage, Session as DbSession, TurnActor
from core.stats_models import StatsEvent
from services.stats.query import _install_progress_handler
from services.stats.ledger import select_admitted_events
from services.stats.privacy import identity_handle, owner_scope

FORMULA_REVISION = "s08-activity-v1"
MAX_ROWS = 100_000
MAX_BUCKETS = 366
MAX_PAGE_SIZE = 100


class ActivityError(ValueError):
    pass


def _check(cancel_event, deadline):
    if cancel_event is not None and cancel_event.is_set():
        raise ActivityError("cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise ActivityError("deadline_exceeded")


def _handle(owner: str, kind: str, value: str) -> str:
    digest = hashlib.sha256(f"s08\0{owner}\0{kind}\0{value}".encode()).hexdigest()[:24]
    return f"{kind}_{digest}"


def _dt(value, zone: ZoneInfo) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    current = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return current.astimezone(zone)


def _bucket(value, resolution: str, zone: ZoneInfo) -> str | None:
    current = _dt(value, zone)
    if current is None:
        return None
    if resolution == "day":
        return current.date().isoformat()
    if resolution == "week":
        return (current.date()).fromordinal(current.date().toordinal() - current.weekday()).isoformat()
    if resolution == "month":
        return current.strftime("%Y-%m")
    raise ActivityError("resolution must be day, week, or month")


def _percentile(values: list[float], fraction: float) -> dict:
    values = sorted(value for value in values if math.isfinite(value) and value >= 0)
    if not values:
        return {"state": "unavailable", "value": None, "population": 0, "formula_revision": FORMULA_REVISION}
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    value = values[lower] if lower == upper else values[lower] + (values[upper] - values[lower]) * (position - lower)
    return {"state": "reported", "value": round(value, 3), "population": len(values), "formula_revision": FORMULA_REVISION}


def _typed_sum(values):
    values = list(values)
    numeric = []
    states = []
    for value, state in values:
        if value is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric.append(value)
            states.append(state if state in {"reported", "estimated"} else "unavailable")
    if not numeric:
        return None, "unavailable"
    total = sum(numeric)
    if isinstance(total, float) and not math.isfinite(total):
        return None, "unavailable"
    state = "unavailable" if "unavailable" in states else "estimated" if "estimated" in states or len(states) != len(values) else "reported"
    return (str(total) if abs(total) > 2**53 - 1 else total), state


def _breakdown(values: Iterable[str | None], owner: str, kind: str) -> list[dict]:
    counts = Counter(value for value in values if value)
    ordered = counts.most_common(16)
    result = [{"label": f"{kind} {index + 1}", "handle": _handle(owner, kind, value), "count": count}
              for index, (value, count) in enumerate(ordered[:15])]
    other = sum(count for _, count in ordered[15:]) + sum(count for value, count in counts.items() if value not in dict(ordered))
    if other:
        result.append({"label": "Other", "handle": _handle(owner, kind, "other"), "count": other})
    return result


def _workspace_breakdown(sessions, messages, actors, owner):
    by_session = {row["session_id"]: row.get("workspace") for row in sessions}
    counts = defaultdict(lambda: {"messages": 0, "sessions": 0, "actors": 0})
    for row in sessions:
        if row.get("workspace"):
            counts[row["workspace"]]["sessions"] += 1
    for row in messages:
        workspace = by_session.get(row.get("session_id"))
        if workspace:
            counts[workspace]["messages"] += 1
    for row in actors:
        workspace = by_session.get(row.get("session_id"))
        if workspace:
            counts[workspace]["actors"] += 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1]["messages"], item[0]))
    visible = ordered[:15]
    result = [{"label": f"workspace {index + 1}", "handle": _handle(owner, "workspace", value), **stats}
              for index, (value, stats) in enumerate(visible)]
    if len(ordered) > 15:
        result.append({"label": "Other", "handle": _handle(owner, "workspace", "other"),
                       "messages": sum(stats["messages"] for _, stats in ordered[15:]),
                       "sessions": sum(stats["sessions"] for _, stats in ordered[15:]),
                       "actors": sum(stats["actors"] for _, stats in ordered[15:])})
    return result


def _active_seconds(times: list[datetime]) -> float | None:
    if len(times) < 2:
        return None
    ordered = sorted(times)
    return sum(min(1800.0, max(0.0, (right - left).total_seconds()))
               for left, right in zip(ordered, ordered[1:]))


def project_activity(*, owner: str, messages: list[dict], sessions: list[dict], events: list[dict], actors: list[dict],
                     timezone_name: str = "UTC", resolution: str = "day", page: int = 1,
                     page_size: int = 50, sort: str = "messages", direction: str = "desc",
                     cancel_event=None, deadline=None, truncated: bool = False, scope: dict | None = None,
                     display_end=None) -> dict:
    if not str(owner or "").strip():
        raise ActivityError("owner is required")
    _check(cancel_event, deadline)
    if page < 1 or page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ActivityError("invalid page")
    try:
        zone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ActivityError("invalid timezone") from exc
    if resolution not in {"day", "week", "month"}:
        raise ActivityError("resolution must be day, week, or month")
    if sort not in {"messages", "active", "elapsed", "output_tokens", "automation"} or direction not in {"asc", "desc"}:
        raise ActivityError("invalid session sort")
    for row in messages + sessions + events + actors:
        _check(cancel_event, deadline)

    timeline = defaultdict(lambda: {"messages": 0, "user_messages": 0, "assistant_messages": 0, "sessions": set()})
    heatmap = Counter()
    for row in messages:
        bucket = _bucket(row.get("timestamp"), resolution, zone)
        if bucket:
            timeline[bucket]["messages"] += 1
            timeline[bucket]["user_messages"] += row.get("role") == "user"
            timeline[bucket]["assistant_messages"] += row.get("role") == "assistant"
            if row.get("session_id"):
                timeline[bucket]["sessions"].add(row["session_id"])
        current = _dt(row.get("timestamp"), zone)
        if current:
            heatmap[(current.weekday(), current.hour)] += 1
    timeline_rows = [{"bucket": bucket, "messages": values["messages"],
                      "user_messages": values["user_messages"], "assistant_messages": values["assistant_messages"],
                      "sessions": len(values["sessions"])}
                     for bucket, values in sorted(timeline.items())[-MAX_BUCKETS:]]
    heatmap_rows = [{"weekday": weekday, "hour": hour, "messages": heatmap[(weekday, hour)]}
                    for weekday in range(7) for hour in range(24)]

    messages_by_session = defaultdict(list)
    for row in messages:
        if row.get("session_id"):
            messages_by_session[row["session_id"]].append(row)
    actors_by_session = defaultdict(list)
    for row in actors:
        if row.get("session_id"):
            actors_by_session[row["session_id"]].append(row)
    session_counts = Counter(messages_by_session.keys())
    session_counts.update({session_id: len(rows) - 1 for session_id, rows in messages_by_session.items()})
    token_values_by_session = defaultdict(list)
    for event in events:
        session_id = event.get("session_id")
        if not session_id:
            continue
        if event.get("output_tokens") is not None:
            token_values_by_session[session_id].append((event["output_tokens"], event.get("output_tokens_state", "reported")))
    session_rows = []
    for session in sessions:
        _check(cancel_event, deadline)
        session_id = session["session_id"]
        session_messages = messages_by_session.get(session_id, [])
        times = [_dt(row["timestamp"], zone) for row in session_messages if _dt(row.get("timestamp"), zone)]
        elapsed = (max(times) - min(times)).total_seconds() if len(times) > 1 else None
        active_seconds = _active_seconds(times)
        session_rows.append({"open_handle": identity_handle(owner, "session_id", session_id),
                             "message_count": len(session_messages),
                             "output_tokens": _typed_sum(token_values_by_session.get(session_id, []))[0],
                             "output_tokens_state": _typed_sum(token_values_by_session.get(session_id, []))[1],
                             "elapsed_seconds": elapsed, "active_seconds": active_seconds,
                             "automation": session.get("mode") in {"agent", "research"},
                             "actor_shape": ("subagent" if any(row.get("nested") for row in actors_by_session.get(session_id, []))
                                             else "automation" if actors_by_session.get(session_id) or session.get("mode") in {"agent", "research"}
                                             else "human"),
                             "_start": min(times) if times else None, "_end": max(times) if times else None})
    def sort_value(row):
        value = {"messages": row["message_count"], "active": row["active_seconds"],
                 "elapsed": row["elapsed_seconds"], "output_tokens": row["output_tokens"],
                 "automation": int(row["automation"])}[sort]
        return (value is None, value if value is not None else 0, row["open_handle"])
    populated = [row for row in session_rows if {"active": row["active_seconds"], "elapsed": row["elapsed_seconds"],
                                                  "output_tokens": row["output_tokens"]}.get(sort, row["message_count"]) is not None]
    empty = [row for row in session_rows if row not in populated]
    populated.sort(key=sort_value, reverse=direction == "desc")
    empty.sort(key=lambda row: row["open_handle"])
    session_rows = populated + empty
    start = (page - 1) * page_size
    page_rows = [{key: value for key, value in row.items() if not key.startswith("_")}
                 for row in session_rows[start:start + page_size]]
    durations = [float(event["duration_ms"]) for event in events if event.get("duration_ms") is not None]
    output_total, output_state = _typed_sum((row.get("output_tokens"), row.get("output_tokens_state", "unavailable")) for row in events)
    intervals = [(row["_start"], row["_end"]) for row in session_rows if row["_start"] and row["_end"]]
    boundaries = sorted({point for interval in intervals for point in interval})
    peak = 0
    for point in boundaries:
        peak = max(peak, sum(start <= point <= end for start, end in intervals))
    active = sum(1 for row in session_rows if row["_start"] and row["_end"])
    observed_days = [value.date() for row in messages for value in [_dt(row.get("timestamp"), zone)] if value]
    observed_days += [value.date() for row in sessions for value in [_dt(row.get("created_at"), zone)] if value]
    observed_days += [value.date() for row in events for value in [_dt(row.get("event_time"), zone)] if value]
    requested_end = display_end
    if isinstance(requested_end, datetime):
        requested_end = _dt(requested_end, zone).date()
    latest_day = requested_end or (max(observed_days) if observed_days else None)
    window_start = latest_day - timedelta(days=364) if latest_day else None
    latest_year = latest_day.year if latest_day else None
    latest_messages = [row for row in messages if (_dt(row.get("timestamp"), zone) and window_start <= _dt(row.get("timestamp"), zone).date() <= latest_day)] if latest_day else []
    contribution_year = latest_year
    contribution_points = defaultdict(lambda: {"messages": 0, "sessions": 0, "output_values": []})
    for row in latest_messages:
        day = _bucket(row.get("timestamp"), "day", zone)
        if day:
            contribution_points[day]["messages"] += 1
    for row in sessions:
        created = _dt(row.get("created_at"), zone)
        if created and window_start <= created.date() <= latest_day:
            contribution_points[created.date().isoformat()]["sessions"] += 1
    for row in events:
        event_time = _dt(row.get("event_time"), zone)
        if event_time and window_start <= event_time.date() <= latest_day and row.get("output_tokens") is not None:
            point = contribution_points[event_time.date().isoformat()]
            point["output_values"].append((row["output_tokens"], row.get("output_tokens_state", "reported")))
    if latest_day:
        cursor = window_start
        while cursor <= latest_day:
            contribution_points.setdefault(cursor.isoformat(), {"messages": 0, "sessions": 0, "output_values": []})
            cursor += timedelta(days=1)
    contribution_rows = [{"day": day, "messages": values["messages"], "sessions": values["sessions"],
                          "output_tokens": _typed_sum(values["output_values"])[0],
                          "output_tokens_state": _typed_sum(values["output_values"])[1]}
                         for day, values in sorted(contribution_points.items())]
    actor_counts = Counter(row.get("mode") or "unknown" for row in actors)
    actor_comparison = []
    for index, (mode, count) in enumerate(sorted(actor_counts.items(), key=lambda item: (-item[1], item[0])), 1):
        actor_sessions = {row.get("session_id") for row in actors if (row.get("mode") or "unknown") == mode}
        actor_messages = sum(row.get("session_id") in actor_sessions for row in messages)
        actor_comparison.append({"label": f"actor {index}", "handle": _handle(owner, "actor", mode),
                                 "count": count, "sessions": len(actor_sessions), "messages": actor_messages,
                                 "timing": {"state": "unavailable"}})

    active_dates = {current.date() for row in messages + events for current in [_dt(row.get("timestamp", row.get("event_time")), zone)] if current}
    return {
        "schema": "open-clank.stats.activity.s08",
        "formula_revision": FORMULA_REVISION,
        "owner_scope": owner_scope(owner),
        "scope": scope or {"period": "all", "timezone": timezone_name},
        "summary": {"messages": len(messages), "user_messages": sum(row.get("role") == "user" for row in messages),
                     "sessions": len(sessions), "assistant_outputs": sum(row.get("role") == "assistant" for row in messages),
                     "output_tokens": output_total,
                     "output_tokens_state": output_state,
                     "active_workspaces": len({row.get("workspace") for row in sessions if row.get("workspace")}),
                     "active_days": len(active_dates),
                     "most_active_workspace": _workspace_breakdown(sessions, messages, actors, owner)[0] if _workspace_breakdown(sessions, messages, actors, owner) else None,
                     "session_message_mean": round(len(messages) / len(sessions), 3) if sessions else None,
                     "session_message_median": _percentile([len(messages_by_session.get(row["session_id"], [])) for row in sessions], .5),
                     "session_message_p90": _percentile([len(messages_by_session.get(row["session_id"], [])) for row in sessions], .9),
                     "workspace_concentration": (max((item["messages"] for item in _workspace_breakdown(sessions, messages, actors, owner)), default=0) / len(messages)) if messages else None},
        "contributions": {"year": contribution_year, "points": contribution_rows},
        "timeline": {"resolution": resolution, "timezone": timezone_name, "points": timeline_rows},
        "heatmap": {"timezone": timezone_name, "points": heatmap_rows},
        "breakdowns": {"workspace": _workspace_breakdown(sessions, messages, actors, owner),
                       "model": _breakdown((row.get("model") for row in sessions), owner, "model"),
                       "actor": _breakdown((row.get("mode") for row in actors), owner, "actor")},
        "sessions": {"rows": page_rows, "page": page, "page_size": page_size,
                     "total": len(session_rows), "truncated": truncated},
        "automation": {"state": "reported", "shapes": Counter(row["actor_shape"] for row in session_rows),
                       "human": sum(row["actor_shape"] == "human" for row in session_rows),
                       "automation": sum(row["actor_shape"] == "automation" for row in session_rows),
                       "subagent": sum(row["actor_shape"] == "subagent" for row in session_rows)},
        "session_shapes": {"archetypes": Counter("quick" if row["message_count"] <= 5 else "standard" if row["message_count"] <= 20 else "deep" if row["message_count"] <= 100 else "marathon" for row in session_rows),
                            "active_seconds": _percentile([row["active_seconds"] for row in session_rows if row["active_seconds"] is not None], .5),
                            "elapsed_seconds": _percentile([row["elapsed_seconds"] for row in session_rows if row["elapsed_seconds"] is not None], .5),
                            "peak_context": {"state": "unavailable"}, "tools": {"state": "unavailable"}},
        "velocity": {"p50_ms": _percentile(durations, .5), "p90_ms": _percentile(durations, .9),
                      "mean_ms": round(sum(durations) / len(durations), 3) if durations else None,
                      "by_actor": {"state": "unavailable", "reason": "timing_evidence_has_no_actor_join"},
                      "by_size": {"state": "unavailable"}},
        "actor_comparison": actor_comparison,
        "concurrency": {"state": "estimated", "active_sessions": active, "peak": peak,
                         "idle_sessions": max(0, len(session_rows) - active),
                         "coverage": {"state": "partial" if truncated else "complete"}},
        "capabilities": {name: {"state": "unavailable", "reason": "canonical_evidence_unavailable"}
                         for name in ("tools", "skills", "machine", "git_branch", "peak_context", "cost")},
        "coverage": {"state": "partial" if truncated else "complete", "truncated": truncated},
        "warnings": ["row_cap_reached"] if truncated else [],
    }


@contextmanager
def _progress(db, cancel_event, deadline):
    raw = _install_progress_handler(db, cancel_event, deadline)
    try:
        yield
    finally:
        if raw is not None:
            raw.set_progress_handler(None, 0)


def _load_activity(db, *, owner: str, timezone_name="UTC", resolution="day", page=1, page_size=50,
                  sort="messages", direction="desc",
                  cancel_event=None, deadline=None, start_utc=None, end_utc=None, scope=None,
                  display_end=None) -> dict:
    _check(cancel_event, deadline)
    message_query = (db.query(ChatMessage.session_id, ChatMessage.role, ChatMessage.timestamp)
                .join(DbSession, DbSession.id == ChatMessage.session_id)
                .filter(DbSession.owner == owner))
    if start_utc is not None:
        message_query = message_query.filter(ChatMessage.timestamp >= start_utc)
    if end_utc is not None:
        message_query = message_query.filter(ChatMessage.timestamp < end_utc)
    rows = message_query.order_by(ChatMessage.timestamp.desc(), ChatMessage.id.desc()).limit(MAX_ROWS + 1).all()
    messages = [{"session_id": row[0], "role": row[1], "timestamp": row[2]} for row in reversed(rows[:MAX_ROWS])]
    event_query = db.query(StatsEvent).filter(StatsEvent.owner == owner)
    if start_utc is not None:
        event_query = event_query.filter(StatsEvent.event_time >= start_utc)
    if end_utc is not None:
        event_query = event_query.filter(StatsEvent.event_time < end_utc)
    event_rows = event_query.order_by(StatsEvent.event_time.desc(), StatsEvent.id.desc()).limit(MAX_ROWS + 1).all()
    admitted_events = select_admitted_events(list(reversed(event_rows[:MAX_ROWS])))
    events = [{"duration_ms": row.duration_ms, "output_tokens": row.output_tokens,
               "output_tokens_state": row.output_tokens_state, "event_time": row.event_time,
               "session_id": row.session_id} for row in admitted_events]
    referenced_sessions = {row[0] for row in rows} | {row.session_id for row in admitted_events if row.session_id}
    session_query = db.query(DbSession.id, DbSession.workspace_id, DbSession.model, DbSession.mode, DbSession.created_at).filter(DbSession.owner == owner)
    if start_utc is not None or end_utc is not None:
        created_in_scope = []
        if start_utc is not None:
            created_in_scope.append(DbSession.created_at >= start_utc)
        if end_utc is not None:
            created_in_scope.append(DbSession.created_at < end_utc)
        created_clause = and_(*created_in_scope)
        session_query = session_query.filter(or_(created_clause, DbSession.id.in_(referenced_sessions)) if referenced_sessions else created_clause)
    sessions = session_query.order_by(DbSession.created_at.desc(), DbSession.id).limit(MAX_ROWS + 1).all()
    session_values = [{"session_id": row[0], "workspace": row[1], "model": row[2], "mode": row[3],
                       "created_at": row[4], "actor_shape": "automation" if row[3] in {"agent", "research"} else "human"}
                      for row in sessions[:MAX_ROWS]]
    actor_rows = db.query(TurnActor.mode, TurnActor.root_turn_id, TurnActor.status, ChatMessage.session_id,
                          TurnActor.parent_actor_id, TurnActor.background).join(AgentTurn, AgentTurn.root_turn_id == TurnActor.root_turn_id)
    actor_rows = (actor_rows
              .join(ChatMessage, ChatMessage.id == AgentTurn.root_turn_id).join(DbSession, DbSession.id == ChatMessage.session_id)
              .filter(DbSession.owner == owner))
    if start_utc is not None:
        actor_rows = actor_rows.filter(ChatMessage.timestamp >= start_utc)
    if end_utc is not None:
        actor_rows = actor_rows.filter(ChatMessage.timestamp < end_utc)
    actor_rows = actor_rows.order_by(ChatMessage.timestamp.desc(), TurnActor.root_turn_id.desc(), TurnActor.actor_id.desc()).limit(MAX_ROWS + 1).all()
    actors = [{"mode": row[0], "root_turn_id": row[1], "status": row[2], "session_id": row[3],
               "nested": bool(row[4])} for row in reversed(actor_rows[:MAX_ROWS])]
    truncated = len(sessions) > MAX_ROWS or len(rows) > MAX_ROWS or len(event_rows) > MAX_ROWS or len(actor_rows) > MAX_ROWS
    return project_activity(owner=owner, messages=messages, sessions=session_values, events=events, actors=actors,
                            timezone_name=timezone_name, resolution=resolution, page=page, page_size=page_size,
                            sort=sort, direction=direction,
                            cancel_event=cancel_event, deadline=deadline, truncated=truncated, scope=scope,
                            display_end=display_end)


def load_activity(db, *, owner: str, timezone_name="UTC", resolution="day", page=1, page_size=50,
                  sort="messages", direction="desc",
                  cancel_event=None, deadline=None, start_utc=None, end_utc=None, scope=None) -> dict:
    with _progress(db, cancel_event, deadline):
        zone = ZoneInfo(timezone_name)
        display_end = end_utc or datetime.now(zone)
        return _load_activity(db, owner=owner, timezone_name=timezone_name, resolution=resolution,
                              page=page, page_size=page_size, sort=sort, direction=direction,
                              cancel_event=cancel_event, deadline=deadline, start_utc=start_utc, end_utc=end_utc,
                              scope=scope, display_end=display_end)

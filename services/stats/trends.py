"""Opt-in, deterministic content trend projection for Stats."""
from __future__ import annotations

import re
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

MAX_GROUPS = 32
MAX_VARIANTS = 8
MAX_TERM_LENGTH = 128
MAX_SCAN_MESSAGES = 100_000
MAX_POINTS = 366
DEFAULT_GROUPS = ("load bearing | load-bearing", "seam", "blast radius")


class TrendError(ValueError):
    pass


def _check(cancel_event, deadline: float | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TrendError("cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TrendError("deadline_exceeded")


def parse_term_groups(raw: str | Iterable[str] | None = None) -> tuple[tuple[str, ...], ...]:
    lines = DEFAULT_GROUPS if raw is None else raw.splitlines() if isinstance(raw, str) else list(raw)
    if len(lines) > MAX_GROUPS:
        raise TrendError("too many term groups")
    groups: list[tuple[str, ...]] = []
    for line in lines:
        if not isinstance(line, str):
            raise TrendError("term groups must be text")
        variants = tuple(dict.fromkeys(part.strip() for part in line.split("|") if part.strip()))
        if not variants or len(variants) > MAX_VARIANTS:
            raise TrendError("each group needs one to eight variants")
        if any(len(term) > MAX_TERM_LENGTH for term in variants):
            raise TrendError("term is too long")
        groups.append(variants)
    return tuple(groups)


def _text(message: Mapping[str, object]) -> str:
    value = message.get("text", message.get("content", ""))
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(item.get("text", "")) for item in value if isinstance(item, Mapping))
    return ""


def _bucket(message: Mapping[str, object], resolution: str) -> str:
    raw = message.get("event_time", message.get("timestamp"))
    if raw is None:
        return "unknown"
    if isinstance(raw, datetime):
        value = raw
    else:
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError as exc:
            raise TrendError("invalid message timestamp") from exc
    value = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    if resolution == "day":
        return value.date().isoformat()
    if resolution == "week":
        return value.date().fromordinal(value.date().toordinal() - value.weekday()).isoformat()
    if resolution == "month":
        return value.strftime("%Y-%m")
    raise TrendError("resolution must be day, week, or month")


def _literal_count(text: str, variants: tuple[str, ...]) -> int:
    spans: list[tuple[int, int]] = []
    for term in variants:
        pattern = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)
        spans.extend(match.span() for match in pattern.finditer(text))
    count = 0
    last_end = -1
    for start, end in sorted(set(spans)):
        if start >= last_end:
            count += 1
            last_end = end
        elif end > last_end:
            last_end = end
    return count


def project_trends(messages: Iterable[Mapping[str, object]], *, owner: str,
                   terms: str | Iterable[str] | None = None,
                   content_opt_in: bool = False, resolution: str = "day",
                   cancel_event=None, deadline: float | None = None) -> dict:
    if not str(owner or "").strip():
        raise TrendError("owner is required")
    if resolution not in {"day", "week", "month"}:
        raise TrendError("resolution must be day, week, or month")
    if not content_opt_in:
        return {"state": "unavailable", "reason": "content_opt_in_required", "groups": []}
    groups = parse_term_groups(terms)
    rows: list[Mapping[str, object]] = []
    for message in messages:
        _check(cancel_event, deadline)
        if len(rows) >= MAX_SCAN_MESSAGES:
            raise TrendError("message scan exceeds bound")
        if not isinstance(message, Mapping):
            raise TrendError("messages must be mappings")
        row_owner = message.get("owner") or message.get("owner_id")
        if not isinstance(row_owner, str) or not row_owner.strip():
            raise TrendError("message owner is required")
        if row_owner.strip().lower() != str(owner).strip().lower():
            raise TrendError("message owner does not match scope")
        rows.append(message)
    series = []
    for variants in groups:
        _check(cancel_event, deadline)
        buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for message in rows:
            _check(cancel_event, deadline)
            key = _bucket(message, resolution)
            buckets[key][0] += _literal_count(_text(message), variants)
            buckets[key][1] += 1
        if len(buckets) > MAX_POINTS:
            raise TrendError("trend point cap exceeded")
        total = sum(value[0] for value in buckets.values())
        series.append({"label": variants[0], "variants": len(variants),
                       "occurrences": total,
                       "per_1000_messages": (total * 1000.0 / len(rows)) if rows else None,
                       "points": [{"bucket": key, "occurrences": value[0],
                                   "per_1000_messages": (value[0] * 1000.0 / value[1]) if value[1] else None}
                                  for key, value in sorted(buckets.items())]})
    return {"state": "reported" if rows else "unavailable", "message_count": len(rows),
            "resolution": resolution, "groups": series}

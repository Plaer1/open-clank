"""Durable assistant transcript layout helpers."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable


TRANSCRIPT_VERSION = 1


def _utf16_length(value: str) -> int:
    """Return the offset unit used by JavaScript String.slice()."""
    return len(value.encode("utf-16-le")) // 2


def normalize_tool_events(events: Iterable[Any] | None) -> list[dict[str, Any]]:
    """Copy tool events and give every event a stable persisted call id."""
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(events or []):
        if not isinstance(raw, dict):
            continue
        event = dict(raw)
        call_id = event.get("id") or event.get("tool_call_id") or event.get("toolCallId")
        if not str(call_id or "").strip():
            round_number = max(1, int(event.get("round") or 1))
            call_id = f"tool-{round_number}-{index + 1}"
        event["id"] = str(call_id)
        normalized.append(event)
    return normalized


def build_transcript_layout(
    content: str,
    *,
    thinking: str = "",
    tool_events: Iterable[Any] | None = None,
    status: str = "complete",
    error: str | None = None,
) -> dict[str, Any]:
    """Build the immutable terminal projection stored with one assistant row."""
    content = str(content or "")
    thinking = str(thinking or "")
    events = normalize_tool_events(tool_events)
    blocks: list[dict[str, Any]] = []
    if thinking:
        blocks.append({"kind": "thinking", "start": 0, "end": _utf16_length(thinking)})
    highest_round = 0
    for event in events:
        round_number = max(1, int(event.get("round") or 1))
        highest_round = max(highest_round, round_number)
        blocks.append({"kind": "tool", "call_id": event["id"], "round": round_number})
    if content:
        blocks.append(
            {
                "kind": "answer",
                "start": 0,
                "end": _utf16_length(content),
                "round": highest_round,
            }
        )
    layout: dict[str, Any] = {
        "version": TRANSCRIPT_VERSION,
        "content_length": _utf16_length(content),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "status": str(status or "complete"),
        "blocks": blocks,
    }
    if error:
        layout["error"] = str(error)
    return layout


def validate_transcript_layout(content: str, metadata: Any) -> bool:
    """Validate references, ranges, hash, and complete answer coverage."""
    if not isinstance(metadata, dict):
        return False
    layout = metadata.get("transcript_v2")
    if not isinstance(layout, dict) or layout.get("version") != TRANSCRIPT_VERSION:
        return False
    content = str(content or "")
    content_length = _utf16_length(content)
    if layout.get("content_length") != content_length:
        return False
    if layout.get("content_sha256") != hashlib.sha256(content.encode("utf-8")).hexdigest():
        return False
    blocks = layout.get("blocks")
    if not isinstance(blocks, list):
        return False

    tool_ids = {
        str(event.get("id") or "")
        for event in metadata.get("tool_events") or []
        if isinstance(event, dict) and str(event.get("id") or "")
    }
    answer_ranges: list[tuple[int, int]] = []
    thinking_length = _utf16_length(str(metadata.get("thinking") or ""))
    for block in blocks:
        if not isinstance(block, dict):
            return False
        kind = block.get("kind")
        if kind == "tool":
            if str(block.get("call_id") or "") not in tool_ids:
                return False
            if not isinstance(block.get("round"), int) or block["round"] < 1:
                return False
        elif kind in {"answer", "thinking"}:
            start, end = block.get("start"), block.get("end")
            limit = content_length if kind == "answer" else thinking_length
            if not isinstance(start, int) or not isinstance(end, int):
                return False
            if start < 0 or end < start or end > limit:
                return False
            if kind == "answer":
                answer_ranges.append((start, end))
        else:
            return False

    answer_ranges.sort()
    covered = 0
    for start, end in answer_ranges:
        if start != covered:
            return False
        covered = end
    return covered == content_length and (content_length == 0 or bool(answer_ranges))


def strip_invalid_transcript_layout(content: str, metadata: Any) -> Any:
    """Return a metadata copy with an untrusted layout removed."""
    if not isinstance(metadata, dict) or "transcript_v2" not in metadata:
        return metadata
    if validate_transcript_layout(content, metadata):
        return metadata
    clean = dict(metadata)
    clean.pop("transcript_v2", None)
    return clean

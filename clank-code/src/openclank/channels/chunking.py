"""Split outbound text at a service's limit, preferring paragraph, line and word boundaries."""
from __future__ import annotations
from typing import Callable


def split_text(text: str, limit: int, measure: Callable[[str], int] = len) -> list[str]:
    if limit < 1:
        raise ValueError("limit must be positive")
    text = text.strip()
    if not text:
        return []
    parts: list[str] = []
    rest = text
    while measure(rest) > limit:
        window = rest
        while measure(window) > limit:          # shrink to the longest prefix that fits (bytes or chars)
            window = window[: max(1, len(window) * limit // max(1, measure(window)))]
            if measure(window) > limit:
                window = window[:-1]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        cut = cut + 1 if cut > len(window) // 3 else len(window)
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n ")
    if rest.strip():
        parts.append(rest.strip())
    return [p for p in parts if p]

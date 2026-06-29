"""Foreground activity gate for background work.

Background tasks are allowed to run only after normal UI/API traffic has
settled. This keeps scheduled jobs and email pollers from competing with the
user opening Odysseus, Cookbook, email, documents, notes, or other panels.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import time


_ACTIVE_REQUESTS = 0
_LAST_ACTIVITY = 0.0
_COND: asyncio.Condition | None = None


def _enabled() -> bool:
    return os.getenv("BACKGROUND_TASK_FOREGROUND_GATE", "true").lower() not in {"0", "false", "no", "off"}


def _quiet_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("BACKGROUND_TASK_QUIET_MS", "1500")) / 1000.0)
    except Exception:
        return 1.5


def _max_wait_seconds() -> float:
    """0 means wait indefinitely until the UI is quiet."""
    try:
        return max(0.0, float(os.getenv("BACKGROUND_TASK_MAX_WAIT_SECONDS", "0")))
    except Exception:
        return 0.0


def _condition() -> asyncio.Condition:
    global _COND
    if _COND is None:
        _COND = asyncio.Condition()
    return _COND


_PASSIVE_EXACT_PATHS = {
    "/api/tasks/notifications",
    "/api/research/active",
    "/api/email/urgency-state",
}

_PASSIVE_PREFIXES = (
    "/api/chat/stream_status",
    "/api/health",
    "/api/prefs",
)


def should_track_interactive_request(path: str, method: str = "GET") -> bool:
    if not _enabled():
        return False
    if (method or "").upper() == "OPTIONS":
        return False
    if path in _PASSIVE_EXACT_PATHS:
        return False
    if any(path.startswith(prefix) for prefix in _PASSIVE_PREFIXES):
        return False
    return True


@asynccontextmanager
async def track_interactive_request(path: str = "", method: str = ""):
    global _ACTIVE_REQUESTS, _LAST_ACTIVITY
    if not _enabled():
        yield
        return

    cond = _condition()
    async with cond:
        _ACTIVE_REQUESTS += 1
        _LAST_ACTIVITY = time.monotonic()
        cond.notify_all()
    try:
        yield
    finally:
        async with cond:
            _ACTIVE_REQUESTS = max(0, _ACTIVE_REQUESTS - 1)
            _LAST_ACTIVITY = time.monotonic()
            cond.notify_all()


async def wait_for_interactive_quiet(label: str = "") -> bool:
    """Wait until foreground requests have stopped for the configured window.

    Returns True if the caller had to wait at all. The label is intentionally
    only for future logging/debugging so callers can keep their code simple.
    """
    if not _enabled():
        return False

    quiet = _quiet_seconds()
    max_wait = _max_wait_seconds()
    deadline = time.monotonic() + max_wait if max_wait > 0 else None
    cond = _condition()
    waited = False

    while True:
        async with cond:
            now = time.monotonic()
            quiet_remaining = quiet - (now - _LAST_ACTIVITY)
            if _ACTIVE_REQUESTS <= 0 and quiet_remaining <= 0:
                return waited

            waited = True
            timeout = 0.25 if _ACTIVE_REQUESTS > 0 else min(max(quiet_remaining, 0.05), 0.5)
            if deadline is not None:
                remaining = deadline - now
                if remaining <= 0:
                    return waited
                timeout = min(timeout, remaining)
            try:
                await asyncio.wait_for(cond.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

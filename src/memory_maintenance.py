"""Small provider-native memory maintenance loop."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

GROOM_OPS = ("decay", "dedup", "edge_decay", "tag_normalize")
logger = logging.getLogger(__name__)


def groom_interval_hours(raw: Any = None) -> float:
    """Parse the maintenance interval; zero disables the background loop."""
    value = os.environ.get("FM_GROOM_INTERVAL_HOURS", "0") if raw is None else raw
    try:
        hours = float(value)
    except (TypeError, ValueError):
        hours = 0.0
    return max(0.0, hours)


async def groom_once(provider, *, owner: str | None = None) -> list[dict]:
    """Run the fixed maintenance operations once and keep going on one failure."""
    results = []
    for op in GROOM_OPS:
        try:
            result = await provider.groom(op, owner=owner)
            logger.info("Frankenmemory groom op=%s result=%s", op, result)
            results.append({"op": op, "ok": True, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Frankenmemory groom op=%s failed: %s", op, exc)
            results.append({"op": op, "ok": False, "error": str(exc)})
    return results


async def groom_loop(provider, interval_hours: float, *, owner: str | None = None) -> None:
    """Sleep between daily passes; cancellation cleanly stops the loop."""
    interval = groom_interval_hours(interval_hours)
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval * 3600.0)
        await groom_once(provider, owner=owner)

"""Small provider-native memory maintenance loop."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Iterable
from typing import Any

GROOM_OPS = ("decay", "dedup", "edge_decay", "tag_normalize")
logger = logging.getLogger(__name__)
_ACTIVE_GROOM_COUNTS: dict[str, int] = {}
_GROOM_IDLE_EVENTS: dict[str, asyncio.Event] = {}


def _owner_key(owner: object) -> str:
    return str(owner or "").strip().casefold()


def _is_owner_fenced(
    owner: str,
    owner_is_fenced: Callable[[str], bool] | None,
) -> bool:
    if owner_is_fenced is None:
        return False
    try:
        return bool(owner_is_fenced(owner))
    except Exception:
        logger.exception("Memory groom owner-fence check failed for %s", owner)
        return True


def _begin_owner_groom(owner: str) -> None:
    key = _owner_key(owner)
    event = _GROOM_IDLE_EVENTS.get(key)
    if event is None or event.is_set():
        event = asyncio.Event()
        _GROOM_IDLE_EVENTS[key] = event
    _ACTIVE_GROOM_COUNTS[key] = _ACTIVE_GROOM_COUNTS.get(key, 0) + 1


def _end_owner_groom(owner: str) -> None:
    key = _owner_key(owner)
    remaining = max(0, _ACTIVE_GROOM_COUNTS.get(key, 0) - 1)
    if remaining:
        _ACTIVE_GROOM_COUNTS[key] = remaining
        return
    _ACTIVE_GROOM_COUNTS.pop(key, None)
    event = _GROOM_IDLE_EVENTS.pop(key, None)
    if event is not None:
        event.set()


async def drain_owner_grooms(owner: str) -> dict[str, object]:
    """Wait without a timeout for every active groom pass for one owner."""

    key = _owner_key(owner)
    observed = 0
    while _ACTIVE_GROOM_COUNTS.get(key, 0):
        observed = max(observed, _ACTIVE_GROOM_COUNTS.get(key, 0))
        event = _GROOM_IDLE_EVENTS.get(key)
        if event is None:
            await asyncio.sleep(0)
            continue
        await event.wait()
    return {"owner": key, "drained": observed}


def groom_interval_hours(raw: Any = None) -> float:
    """Parse the maintenance interval; zero disables the background loop."""
    value = os.environ.get("FM_GROOM_INTERVAL_HOURS", "0") if raw is None else raw
    try:
        hours = float(value)
    except (TypeError, ValueError):
        hours = 0.0
    return max(0.0, hours)


async def groom_once(
    provider,
    *,
    owner: str,
    workspace_id: str,
    owner_is_fenced: Callable[[str], bool] | None = None,
) -> list[dict]:
    """Run the fixed maintenance operations once and keep going on one failure."""
    owner = str(owner or "").strip()
    workspace_id = str(workspace_id or "").strip()
    if not owner or not workspace_id:
        raise ValueError("authenticated owner and workspace_id are required")
    if _is_owner_fenced(owner, owner_is_fenced):
        logger.info("Frankenmemory groom skipped for lifecycle-fenced owner=%s", owner)
        return []
    _begin_owner_groom(owner)
    try:
        # Close the check/register race: a lifecycle claim may activate after
        # the first check but before this pass becomes visible to its drain.
        if _is_owner_fenced(owner, owner_is_fenced):
            return []
        results = []
        for op in GROOM_OPS:
            if _is_owner_fenced(owner, owner_is_fenced):
                break
            try:
                result = await provider.groom(
                    op,
                    owner=owner,
                    workspace_id=workspace_id,
                )
                logger.info("Frankenmemory groom op=%s result=%s", op, result)
                results.append({"owner": owner, "op": op, "ok": True, "result": result})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Frankenmemory groom owner=%s op=%s failed: %s",
                    owner,
                    op,
                    exc,
                )
                results.append({"owner": owner, "op": op, "ok": False, "error": str(exc)})
        return results
    finally:
        _end_owner_groom(owner)


async def groom_all_owners(
    provider,
    owners: Iterable[str],
    *,
    workspace_id: str,
    owner_is_fenced: Callable[[str], bool] | None = None,
) -> list[dict]:
    """Run one tenant-scoped pass for every authenticated owner."""
    normalized = sorted(
        {
            normalized_owner
            for owner in owners
            if (normalized_owner := str(owner or "").strip())
        }
    )
    if not normalized:
        logger.warning("Frankenmemory groom skipped: no authenticated owners")
        return []
    results = []
    for owner in normalized:
        results.extend(
            await groom_once(
                provider,
                owner=owner,
                workspace_id=workspace_id,
                owner_is_fenced=owner_is_fenced,
            )
        )
    return results


async def groom_loop(
    provider,
    interval_hours: float,
    *,
    owners: Callable[[], Iterable[str]],
    workspace_id: str,
    owner_is_fenced: Callable[[str], bool] | None = None,
) -> None:
    """Sleep between daily passes; cancellation cleanly stops the loop."""
    interval = groom_interval_hours(interval_hours)
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval * 3600.0)
        await groom_all_owners(
            provider,
            owners(),
            workspace_id=workspace_id,
            owner_is_fenced=owner_is_fenced,
        )

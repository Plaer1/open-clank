"""Starts one polling task per channel that is switched on AND configured; nothing else exists until then.

No adapter is constructed and no network call is made unless the global switch, the channel's switch and its secrets are
all present. The tasks are plain asyncio tasks owned by the application's startup and shutdown hooks (no scheduler, no
task store)."""
from __future__ import annotations
import asyncio
import logging
from typing import Callable

from src.openclank.channels.access import Access
from src.openclank.channels.bridge import ChannelBridge

log = logging.getLogger(__name__)
RESTART_BACKOFF_START, RESTART_BACKOFF_MAX = 1.0, 60.0


class ChannelRunner:
    def __init__(self, store, *, global_enabled: Callable[[], bool], adapter_factories: dict[str, Callable], bridge_factory: Callable[[], ChannelBridge],
                 sleep=asyncio.sleep, on_idle: Callable[[], None] | None = None):
        self.store, self._global, self._factories, self._bridge_factory, self._sleep = store, global_enabled, adapter_factories, bridge_factory, sleep
        self._on_idle = on_idle
        self._tasks: dict[str, asyncio.Task] = {}
        self._adapters: dict[str, object] = {}
        self._bridge: ChannelBridge | None = None

    def wanted(self) -> list[str]:
        if not self._global():
            return []
        return [n for n in self._factories if self.store.enabled(n) and self.store.configured(n)]

    async def reload(self) -> None:
        """Make the running set equal the wanted set (used at startup and after any settings change)."""
        wanted = set(self.wanted())
        if not wanted and self._on_idle is not None:
            try:
                self._on_idle()                 # nothing is switched on: the channel API token is revoked, not left dormant
            except Exception as exc:
                log.warning("channel token revoke failed: %s", type(exc).__name__)
        for name in list(self._tasks):
            if name not in wanted:
                await self._stop_one(name)
        for name in wanted - set(self._tasks):
            if self._bridge is None:
                self._bridge = self._bridge_factory()
            adapter = self._factories[name](self.store)
            self._adapters[name] = adapter
            self._tasks[name] = asyncio.create_task(self._supervise(name, adapter))

    async def _supervise(self, name: str, adapter) -> None:
        backoff = RESTART_BACKOFF_START
        while True:
            try:
                await adapter.run(lambda inbound: self._bridge.handle(adapter, inbound))
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:                       # class name only
                log.warning("channel %s stopped: %s", name, type(exc).__name__)
                await self._sleep(backoff)
                backoff = min(backoff * 2, RESTART_BACKOFF_MAX)

    async def _stop_one(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        adapter = self._adapters.pop(name, None)
        if adapter is not None:
            try:
                await adapter.aclose()
            except Exception:
                pass

    async def stop(self) -> None:
        for name in list(self._tasks):
            await self._stop_one(name)
        if self._bridge is not None:
            try:
                await self._bridge.client.aclose()
            except Exception:
                pass
            self._bridge = None

    def running(self) -> list[str]:
        return sorted(self._tasks)

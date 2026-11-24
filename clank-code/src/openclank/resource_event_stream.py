"""Ephemeral invalidation queues; never an authority or a replay log."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Awaitable, Callable


class _Queues:
    """One event-loop-local bounded fanout, with policy chosen by its caller."""
    def __init__(self):
        self._queues: dict[tuple[str, str], set[asyncio.Queue]] = defaultdict(set)
        self._loop = None

    def _current_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is None: self._loop = loop
        if self._loop is not loop: raise RuntimeError('invalidation loop changed')

    def subscribe(self, key: tuple[str, str], *, limit: int = 100) -> asyncio.Queue:
        self._current_loop()
        queue = asyncio.Queue(maxsize=limit)
        self._queues[key].add(queue)
        return queue

    def unsubscribe(self, key: tuple[str, str], queue: asyncio.Queue) -> None:
        group = self._queues.get(key)
        if group is None: return
        group.discard(queue)
        if not group: self._queues.pop(key, None)

    def publish(self, key: tuple[str, str], item: Any, *, gap: Any = None) -> None:
        if not self._queues.get(key): return
        self._current_loop()
        for queue in tuple(self._queues.get(key, ())):
            if queue.full():
                if gap is None:
                    try: queue.get_nowait()
                    except asyncio.QueueEmpty: pass
                else:
                    while not queue.empty():
                        try: queue.get_nowait()
                        except asyncio.QueueEmpty: break
                    queue.put_nowait(gap)
                    continue
            queue.put_nowait(item)


class CopalQueues:
    """Preserve the existing owner/workspace event and drop-oldest semantics."""
    def __init__(self): self._queues = _Queues()
    def subscribe(self, owner: str, workspace: str) -> asyncio.Queue:
        return self._queues.subscribe((owner, workspace))
    def unsubscribe(self, owner: str, workspace: str, queue: asyncio.Queue) -> None:
        self._queues.unsubscribe((owner, workspace), queue)
    def publish(self, owner: str, workspace: str, event: str, data: dict) -> None:
        self._queues.publish((owner, workspace), {'event': event, 'data': data})


class ResourceSubscription:
    def __init__(self, owner, key, queue, authorize):
        self._owner, self._key, self._queue, self._authorize = owner, key, queue, authorize
        self._closed = False

    def close(self) -> None:
        if self._closed: return
        self._closed = True
        self._owner._queues.unsubscribe(self._key, self._queue)

    async def _check(self) -> None:
        try:
            if self._closed: raise PermissionError('resource subscription unavailable')
            actual = await self._authorize()
            if self._closed or actual != self._key: raise PermissionError('resource subscription unavailable')
        except BaseException:
            self.close()
            raise

    async def next(self, *, timeout: float = 15.0) -> dict | None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 60:
            raise ValueError('invalid subscription timeout')
        await self._check()
        try:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                item = None
            await self._check()
            return item
        except BaseException:
            self.close()
            raise


class ResourceInvalidations:
    """Resource-scoped invalidations, with recipient authority checked on delivery."""
    def __init__(self): self._queues = _Queues()

    @staticmethod
    def _key(owner_account: str, resource_id: str) -> tuple[str, str]:
        if any(not isinstance(value, str) or not value or len(value.encode('utf-8')) > 512 for value in (owner_account, resource_id)):
            raise ValueError('invalid resource subscription')
        return owner_account, resource_id

    async def subscribe(self, owner_account: str, resource_id: str, authorize: Callable[[], Awaitable[tuple[str, str] | None]]) -> ResourceSubscription:
        key = self._key(owner_account, resource_id)
        if not callable(authorize): raise ValueError('resource authority unavailable')
        if await authorize() != key: raise PermissionError('resource subscription unavailable')
        if sum(len(group) for group in self._queues._queues.values()) >= 1024: raise RuntimeError('resource subscriptions full')
        queue = self._queues.subscribe(key)
        handle = ResourceSubscription(self, key, queue, authorize)
        try:
            await handle._check()
            return handle
        except BaseException:
            handle.close()
            raise

    def publish(self, owner_account: str, resource_id: str) -> None:
        self._queues.publish(self._key(owner_account, resource_id), {'version': 1, 'kind': 'invalidate'},
                             gap={'version': 1, 'kind': 'gap'})


resource_invalidations = ResourceInvalidations()

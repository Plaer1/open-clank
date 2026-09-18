"""Atomic admission and draining for owner-scoped HTTP requests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable


class AccountRequestFenced(RuntimeError):
    """The owner entered a lifecycle operation before request admission."""


@dataclass(eq=False)
class _Admission:
    owner: str
    origin_task: asyncio.Task[Any]
    complete: asyncio.Future[None]
    released: bool = False


class AccountRequestBarrier:
    """Track full owner response lifetimes and drain them without a timeout.

    Admission performs its fence check and registry insertion under one lock.
    Once a durable lifecycle operation activates the fence, a drain therefore
    sees every request that passed the earlier check, while every later request
    is rejected.  Completion follows the response body iterator so streaming
    chat/tool work remains registered after middleware receives the response.
    """

    def __init__(self, is_fenced: Callable[[str], bool]) -> None:
        self._is_fenced = is_fenced
        self._lock = asyncio.Lock()
        self._active: dict[str, set[_Admission]] = {}

    @staticmethod
    def _owner(owner: str | None) -> str:
        return str(owner or "").strip().lower()

    def _fenced(self, owner: str) -> bool:
        try:
            return bool(self._is_fenced(owner))
        except Exception:
            # An unreadable durable fence cannot safely admit new work.
            return True

    async def acquire(self, owner: str) -> _Admission:
        owner_key = self._owner(owner)
        if not owner_key:
            raise AccountRequestFenced("account request owner is required")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("account request admission requires an asyncio task")
        loop = asyncio.get_running_loop()
        admission = _Admission(
            owner=owner_key,
            origin_task=task,
            complete=loop.create_future(),
        )
        async with self._lock:
            if self._fenced(owner_key):
                raise AccountRequestFenced(
                    f"account lifecycle operation is active for {owner_key}"
                )
            self._active.setdefault(owner_key, set()).add(admission)
        return admission

    async def release(self, admission: _Admission) -> None:
        async with self._lock:
            if admission.released:
                return
            admission.released = True
            active = self._active.get(admission.owner)
            if active is not None:
                active.discard(admission)
                if not active:
                    self._active.pop(admission.owner, None)
            if not admission.complete.done():
                admission.complete.set_result(None)

    @asynccontextmanager
    async def admitted(self, owner: str) -> AsyncIterator[None]:
        admission = await self.acquire(owner)
        try:
            yield
        finally:
            await self.release(admission)

    async def track_response(
        self,
        owner: str,
        invoke: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run a handler and retain admission through its streamed body."""

        admission = await self.acquire(owner)
        try:
            response = await invoke()
        except BaseException:
            await self.release(admission)
            raise

        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is None:
            await self.release(admission)
            return response

        async def tracked_body():
            try:
                async for chunk in body_iterator:
                    yield chunk
            finally:
                await self.release(admission)

        response.body_iterator = tracked_body()
        return response

    async def drain(self, owner: str) -> dict[str, Any]:
        """Wait for admitted work except the calling request, without timeout."""

        owner_key = self._owner(owner)
        current = asyncio.current_task()
        drained: set[_Admission] = set()
        while True:
            async with self._lock:
                pending = tuple(
                    admission
                    for admission in self._active.get(owner_key, ())
                    if admission.origin_task is not current
                    and not admission.complete.done()
                )
            if not pending:
                return {"owner": owner_key, "drained": len(drained)}
            drained.update(pending)
            await asyncio.gather(
                *(asyncio.shield(admission.complete) for admission in pending),
                return_exceptions=True,
            )


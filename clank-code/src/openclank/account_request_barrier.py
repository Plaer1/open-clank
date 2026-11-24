"""Atomic admission and draining for owner-scoped HTTP requests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable


class AccountRequestFenced(RuntimeError):
    """The owner entered a lifecycle operation before request admission."""


@dataclass(eq=False)
class _Admission:
    owner: str
    origin_task: asyncio.Task[Any]
    complete: asyncio.Future[None]
    released: bool = False
    utility: Any = None
    # Set only by a route that opted in via mark_passive_stream(); drain() asks it to end.
    passive: bool = False
    stop: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class _ResetDrainWitness:
    barrier: Any
    owner: str
    caller: _Admission
    revision: int


@dataclass(eq=False)
class UtilityLifetime:
    """Host-private attachment to one existing owner admission, never a wire DTO."""
    barrier: Any
    admission: _Admission
    owner: str
    account: str
    root: str
    idempotency_key: str
    request_hash: str
    auth: Any
    supervisor: Any = None
    worker: Any = None
    bridge: Any = None
    holder: str | None = None
    generation: Any = None
    epoch: Any = None
    process: Any = None
    lease: Any = None
    foreground_runtime: Any = None
    foreground_handle: Any = None
    client: Any = None
    request_id: Any = None
    issued: bool = False
    response_seen: bool = False
    host_cleanup_unknown: bool = False
    cancel_requested: bool = False
    task: Any = None
    # Optional private router-owned exact send custody; never a wire value.
    owned_send: Any = None
    settled: bool = False
    settling: bool = False

    def account_current(self) -> bool:
        try:
            return self.auth.account_id(self.owner) == self.account and self.auth.username_for_account_id(self.account) == self.owner
        except Exception:
            return False


_current_admission: ContextVar[_Admission | None] = ContextVar(
    "account_request_admission", default=None
)


class AccountRequestBarrier:
    """Track full owner response lifetimes and drain them without a timeout.

    Admission performs its fence check and registry insertion under one lock.
    Once a durable lifecycle operation activates the fence, a drain therefore
    sees every request that passed the earlier check, while every later request
    is rejected.  Completion follows the response body iterator so streaming
    chat/tool work remains registered after middleware receives the response.
    """

    def __init__(self, is_fenced: Callable[[str], bool], *, absolute_fence: Callable[[str], bool] | None = None) -> None:
        self._is_fenced = is_fenced
        self._absolute_fence = absolute_fence
        self._lock = asyncio.Lock()
        self._active: dict[str, set[_Admission]] = {}
        self._owner_revisions: dict[str, int] = {}

    @staticmethod
    def _owner(owner: str | None) -> str:
        return str(owner or "").strip().lower()

    def _fenced(self, owner: str) -> bool:
        try:
            return bool(self._is_fenced(owner))
        except Exception:
            # An unreadable durable fence cannot safely admit new work.
            return True

    async def acquire(self, owner: str, *, allow_fenced: bool = False) -> _Admission:
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
            try:
                absolute = bool(self._absolute_fence(owner_key)) if self._absolute_fence else False
            except Exception:
                absolute = True
            if absolute or (not allow_fenced and self._fenced(owner_key)):
                raise AccountRequestFenced(
                    f"account lifecycle operation is active for {owner_key}"
                )
            self._active.setdefault(owner_key, set()).add(admission)
            self._owner_revisions[owner_key] = self._owner_revisions.get(owner_key, 0) + 1
        return admission

    def mark_passive_stream(self) -> None:
        """Let drain() end this request's streamed body; call from a handler for a
        long-lived passive stream that holds no memory state. No-op outside an admission."""
        admission = _current_admission.get()
        if type(admission) is _Admission and not admission.released:
            admission.passive = True

    async def bind_utility(self, admission: _Admission, utility: Any) -> None:
        """Attach host-only utility lifetime to an already registered admission."""
        async with self._lock:
            if (type(admission) is not _Admission or admission.released
                    or admission not in self._active.get(admission.owner, ()) or admission.utility is not None):
                raise AccountRequestFenced("utility admission unavailable")
            admission.utility = utility

    async def utility_for_worker(self, owner: str, worker: Any) -> list[Any]:
        async with self._lock:
            return [item.utility for item in self._active.get(self._owner(owner), ())
                    if not item.released and item.utility is not None and item.utility.worker is worker]

    async def utility_for_child(self, owner: str, worker: Any, process: Any) -> list[Any]:
        """Snapshot existing unresolved admissions for the exact captured child."""
        async with self._lock:
            return [item.utility for item in self._active.get(self._owner(owner), ())
                    if not item.released and item.utility is not None
                    and item.utility.worker is worker and item.utility.process is process]

    async def is_live(self, owner: str, admission: _Admission) -> bool:
        """Check a host-held admission without consulting a later lifecycle fence."""
        owner_key = self._owner(owner)
        async with self._lock:
            return (type(admission) is _Admission and admission.owner == owner_key
                    and not admission.released and admission in self._active.get(owner_key, ()))

    def assert_live_admission(self, owner: str, admission: _Admission) -> None:
        """Check an existing admission synchronously on its original event loop."""
        if not (type(admission) is _Admission and admission.complete.get_loop() is asyncio.get_running_loop()
                and admission.owner == self._owner(owner) and not admission.released
                and admission in self._active.get(admission.owner, ())):
            raise RuntimeError("account admission unavailable")

    def assert_live_utility(self, utility: UtilityLifetime) -> None:
        """Check the exact bound utility at a synchronous event-loop boundary."""
        if type(utility) is not UtilityLifetime or utility.barrier is not self:
            raise RuntimeError("utility admission unavailable")
        admission = utility.admission
        if not (type(admission) is _Admission and admission.complete.get_loop() is asyncio.get_running_loop()
                and admission.owner == self._owner(utility.owner)
                and not admission.released and admission.utility is utility
                and admission in self._active.get(admission.owner, ())):
            raise RuntimeError("utility admission unavailable")

    async def release(self, admission: _Admission) -> None:
        async with self._lock:
            if admission.released:
                return
            admission.released = True
            self._owner_revisions[admission.owner] = self._owner_revisions.get(admission.owner, 0) + 1
            active = self._active.get(admission.owner)
            if active is not None:
                active.discard(admission)
                if not active:
                    self._active.pop(admission.owner, None)
            if not admission.complete.done():
                admission.complete.set_result(None)

    async def publish(self, owner: str, claim: Callable[[], Any]) -> Any:
        """Publish a durable fence atomically with request admission.

        The callback is synchronous: it must finish its durable write before
        this lock is released, and must never await or start background work.
        """
        owner_key = self._owner(owner)
        caller = _current_admission.get()
        async with self._lock:
            try:
                absolute = bool(self._absolute_fence(owner_key)) if self._absolute_fence else False
            except Exception:
                absolute = True
            if (not owner_key or caller is None or caller.owner != owner_key
                    or caller.released or caller not in self._active.get(owner_key, ()) or absolute):
                raise AccountRequestFenced("current owner request admission is unavailable")
            result = claim()
            if asyncio.iscoroutine(result):
                result.close()
                raise TypeError("fence publication must be synchronous")
            self._owner_revisions[owner_key] = self._owner_revisions.get(owner_key, 0) + 1
            return result

    async def reset_drain_witness(self, owner: str) -> _ResetDrainWitness:
        """Stamp the exact admitted reset caller only while fenced and drained."""
        owner_key=self._owner(owner); caller=_current_admission.get()
        async with self._lock:
            if (not owner_key or type(caller) is not _Admission or caller.owner!=owner_key
                or caller.released or caller not in self._active.get(owner_key, ())
                or not self._positive_reset_fence(owner_key)
                or any(a is not caller and not a.released for a in self._active.get(owner_key, ()))):
                raise AccountRequestFenced('reset drain unavailable')
            return _ResetDrainWitness(self,owner_key,caller,self._owner_revisions.get(owner_key,0))

    def reset_witness_current(self, witness: _ResetDrainWitness) -> bool:
        """Read a previously stamped witness; never creates or refreshes it."""
        def live():
            return (type(witness) is _ResetDrainWitness and witness.barrier is self
                and self._owner_revisions.get(witness.owner,0)==witness.revision
                and not witness.caller.released and witness.caller in self._active.get(witness.owner, ()))
        return live() and self._positive_reset_fence(witness.owner) and live()

    def reset_witness_revision_current(self, witness: _ResetDrainWitness) -> bool:
        """Cheap local invalidation check; it is not a durable-fence proof."""
        return (type(witness) is _ResetDrainWitness and witness.barrier is self
            and self._owner_revisions.get(witness.owner,0)==witness.revision
            and not witness.caller.released and witness.caller in self._active.get(witness.owner, ()))

    def _positive_reset_fence(self, owner: str) -> bool:
        try:
            return self._is_fenced(owner) is True
        except Exception:
            return False

    @asynccontextmanager
    async def admitted(self, owner: str) -> AsyncIterator[None]:
        admission = await self.acquire(owner)
        token = _current_admission.set(admission)
        try:
            yield
        finally:
            _current_admission.reset(token)
            await self.release(admission)

    async def track_response(
        self,
        owner: str,
        invoke: Callable[[], Awaitable[Any]],
        *, allow_fenced: bool = False,
    ) -> Any:
        """Run a handler and retain admission through its streamed body."""

        admission = await self.acquire(owner, allow_fenced=allow_fenced)
        token = _current_admission.set(admission)
        try:
            response = await invoke()
        except BaseException:
            await self.release(admission)
            raise
        finally:
            _current_admission.reset(token)

        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is None:
            await self.release(admission)
            return response

        async def tracked_body():
            try:
                if not admission.passive:
                    async for chunk in body_iterator:
                        yield chunk
                    return
                # Passive stream: race each chunk against the drain signal and end on it.
                stopper = asyncio.ensure_future(admission.stop.wait())
                try:
                    while True:
                        nxt = asyncio.ensure_future(body_iterator.__anext__())
                        await asyncio.wait({nxt, stopper}, return_when=asyncio.FIRST_COMPLETED)
                        if not nxt.done():
                            nxt.cancel()
                            await asyncio.gather(nxt, return_exceptions=True)
                            return
                        try:
                            chunk = nxt.result()
                        except StopAsyncIteration:
                            return
                        yield chunk
                finally:
                    stopper.cancel()
                    aclose = getattr(body_iterator, "aclose", None)
                    if aclose is not None:
                        await aclose()
            finally:
                await self.release(admission)

        response.body_iterator = tracked_body()
        return response

    async def drain(self, owner: str) -> dict[str, Any]:
        """Wait for admitted work except the calling request, without timeout."""

        owner_key = self._owner(owner)
        caller = _current_admission.get()
        drained: set[_Admission] = set()
        while True:
            async with self._lock:
                pending = tuple(
                    admission
                    for admission in self._active.get(owner_key, ())
                    if admission is not caller
                    and not admission.complete.done()
                )
            if not pending:
                return {"owner": owner_key, "drained": len(drained)}
            for admission in pending:
                if admission.passive:
                    admission.stop.set()
            drained.update(pending)
            await asyncio.gather(
                *(asyncio.shield(admission.complete) for admission in pending),
                return_exceptions=True,
            )


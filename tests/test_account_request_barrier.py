from __future__ import annotations

import asyncio

import pytest

from src.openclank.account_request_barrier import (
    AccountRequestBarrier,
    AccountRequestFenced,
)


def test_drain_waits_for_full_stream_and_new_admission_is_fenced():
    async def scenario():
        fenced: set[str] = set()
        barrier = AccountRequestBarrier(lambda owner: owner in fenced)
        release_stream = asyncio.Event()

        class Response:
            async def _body(self):
                yield b"first"
                await release_stream.wait()
                yield b"last"

            def __init__(self):
                self.body_iterator = self._body()

        response = await barrier.track_response("Alice", lambda: _response(Response()))
        first_chunk = asyncio.Event()

        async def consume():
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
                first_chunk.set()
            return chunks

        consumer = asyncio.create_task(consume())
        await first_chunk.wait()
        fenced.add("alice")
        drain = asyncio.create_task(barrier.drain("ALICE"))
        await asyncio.sleep(0)
        assert not drain.done()

        with pytest.raises(AccountRequestFenced):
            await barrier.track_response("alice", lambda: _response(Response()))

        release_stream.set()
        assert await consumer == [b"first", b"last"]
        assert await drain == {"owner": "alice", "drained": 1}

    asyncio.run(scenario())


def test_drain_excludes_the_calling_owner_request():
    async def scenario():
        barrier = AccountRequestBarrier(lambda _owner: False)
        async with barrier.admitted("alice"):
            assert await barrier.drain("alice") == {"owner": "alice", "drained": 0}

    asyncio.run(scenario())


def test_checker_failure_fails_closed():
    async def scenario():
        def broken(_owner):
            raise RuntimeError("ledger unavailable")

        barrier = AccountRequestBarrier(broken)
        with pytest.raises(AccountRequestFenced):
            await barrier.acquire("alice")

    asyncio.run(scenario())


async def _response(value):
    return value



class _Endless:
    """A never-ending SSE-like body; records whether it was closed."""

    def __init__(self):
        self.closed = False
        self.body_iterator = self._body()

    async def _body(self):
        try:
            while True:
                yield b": keepalive\n\n"
                await asyncio.sleep(0.01)
        finally:
            self.closed = True


async def _open(barrier, owner, passive):
    async def invoke():
        if passive:
            barrier.mark_passive_stream()
        return _Endless()
    response = await barrier.track_response(owner, invoke)

    async def consume():
        async for _ in response.body_iterator:
            pass
    return response, asyncio.create_task(consume())


def test_drain_ends_a_passive_stream_but_still_waits_for_a_chat_like_stream():
    async def scenario():
        barrier = AccountRequestBarrier(lambda owner: False)
        passive, passive_task = await _open(barrier, "alice", passive=True)
        await asyncio.sleep(0.05)
        # Without the opt-in (chat/agent/tool streams) drain keeps waiting: the documented old behaviour.
        chat, chat_task = await _open(barrier, "alice", passive=False)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(barrier.drain("alice"), timeout=0.3)
        assert passive_task.done()
        chat_task.cancel()
        await asyncio.gather(chat_task, return_exceptions=True)
        await asyncio.wait_for(barrier.drain("alice"), timeout=1)
        assert not barrier._active
    asyncio.run(scenario())


def test_drain_completes_with_only_a_passive_stream_and_closes_it():
    async def scenario():
        barrier = AccountRequestBarrier(lambda owner: False)
        response, task = await _open(barrier, "alice", passive=True)
        await asyncio.sleep(0.05)
        result = await asyncio.wait_for(barrier.drain("alice"), timeout=1)
        assert result["drained"] == 1 and task.done()
        await asyncio.sleep(0)
        assert not barrier._active
    asyncio.run(scenario())


def test_ordinary_in_flight_request_still_blocks_drain_until_it_finishes():
    async def scenario():
        barrier = AccountRequestBarrier(lambda owner: False)
        gate = asyncio.Event()

        async def handler():
            await gate.wait()
            return object()
        work = asyncio.create_task(barrier.track_response("alice", handler))
        await asyncio.sleep(0.05)
        _, passive_task = await _open(barrier, "alice", passive=True)
        drain = asyncio.create_task(barrier.drain("alice"))
        await asyncio.sleep(0.2)
        assert not drain.done() and passive_task.done()
        gate.set()
        await work
        await asyncio.wait_for(drain, timeout=1)
    asyncio.run(scenario())

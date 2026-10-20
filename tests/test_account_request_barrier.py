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


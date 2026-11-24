"""The one bounded owner for blocking History HTTP/IPC calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi import HTTPException

HISTORY_IO_WORKERS = 4
HISTORY_IO_EXECUTOR = ThreadPoolExecutor(max_workers=HISTORY_IO_WORKERS, thread_name_prefix="openclank-history-http")
HISTORY_IO_SLOTS = asyncio.Semaphore(HISTORY_IO_WORKERS)


async def bounded_history_io(call, *, timeout: float = 13.0):
    try:
        await asyncio.wait_for(HISTORY_IO_SLOTS.acquire(), timeout=0.25)
    except asyncio.TimeoutError as exc:
        raise HTTPException(503, "History is busy; retry shortly") from exc
    future = asyncio.get_running_loop().run_in_executor(HISTORY_IO_EXECUTOR, call)
    def release_when_done(done):
        try: done.exception()
        except BaseException: pass
        HISTORY_IO_SLOTS.release()
    future.add_done_callback(release_when_done)
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, "History service request exceeded its deadline") from exc

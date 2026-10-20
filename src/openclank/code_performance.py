"""Opt-in, in-process code performance capture; never part of a wire result.

Only fixed stage names and numeric aggregates are retained. Timings overlap:
query wall time includes queue, native service, decode and observation waits.
Native service includes MCP transport, not just Rust CPU time. Captures are
task-scoped; the queue explicitly carries its collector across the owner task.
There is no environment switch, endpoint, logging, or background observer.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from time import perf_counter_ns


class CodeStage(Enum):
    QUEUE = "provider_queue_wait"
    SERVICE = "provider_service"
    DECODE = "provider_decode"
    QUERY = "query_total"
    START = "query_start_wait"
    POLL = "query_poll_wait"
    OBSERVATION = "query_observation_wait"


class CodePerformanceCapture:
    def __init__(self):
        self._stages = {}
        self._open = True

    def _record(self, stage, elapsed_ns, *, failed=False, cancelled=False):
        if not isinstance(stage, CodeStage):
            raise TypeError("Performance stage must be a fixed CodeStage")
        if not self._open:
            return
        row = self._stages.setdefault(stage.value, {
            "count": 0, "elapsed_ns": 0, "failed": 0, "cancelled": 0,
        })
        row["count"] += 1
        row["elapsed_ns"] += max(0, elapsed_ns)
        row["failed"] += int(failed)
        row["cancelled"] += int(cancelled)

    def snapshot(self):
        return {key: dict(value) for key, value in sorted(self._stages.items())}


_active: ContextVar[CodePerformanceCapture | None] = ContextVar(
    "code_performance_capture", default=None,
)


@contextmanager
def capture_code_performance():
    """Explicit local harness capture. Late cleanup cannot mutate a closed run."""
    capture = CodePerformanceCapture()
    token = _active.set(capture)
    try:
        yield capture
    finally:
        capture._open = False
        _active.reset(token)


@contextmanager
def _span(capture, stage):
    if capture is None or not capture._open:
        yield
        return
    started = perf_counter_ns()
    failed = cancelled = False
    try:
        yield
    except asyncio.CancelledError:
        cancelled = True
        raise
    except BaseException:
        failed = True
        raise
    finally:
        capture._record(stage, perf_counter_ns() - started,
                        failed=failed, cancelled=cancelled)


def code_performance_span(stage):
    if not isinstance(stage, CodeStage):
        raise TypeError("Performance stage must be a fixed CodeStage")
    return _span(_active.get(), stage)


def measured_code_query(function):
    @wraps(function)
    async def measured(*args, **kwargs):
        with code_performance_span(CodeStage.QUERY):
            return await function(*args, **kwargs)
    return measured


@dataclass(frozen=True)
class _QueuedTiming:
    capture: CodePerformanceCapture
    queued_ns: int


def mark_code_request_queued(future):
    capture = _active.get()
    if capture is not None and capture._open:
        # Preserve the transport's existing (name, arguments, future) shape.
        # No arguments or request identity are retained by the collector.
        future._code_performance_timing = _QueuedTiming(capture, perf_counter_ns())


@contextmanager
def code_request_service(future):
    timing = getattr(future, "_code_performance_timing", None)
    if not isinstance(timing, _QueuedTiming) or not timing.capture._open:
        yield
        return
    del future._code_performance_timing
    timing.capture._record(CodeStage.QUEUE, perf_counter_ns() - timing.queued_ns)
    with _span(timing.capture, CodeStage.SERVICE):
        yield

"""Pure local instrumentation checks: no native child, DB or HTTP service."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src.openclank import code_performance as metrics
from src.frankenmemory_provider import FrankenmemoryProvider


def test_disabled_capture_does_not_read_clock(monkeypatch):
    def forbidden():
        raise AssertionError("disabled measurement read the clock")
    monkeypatch.setattr(metrics, "perf_counter_ns", forbidden)
    with metrics.code_performance_span(metrics.CodeStage.QUERY):
        pass
    future = SimpleNamespace()
    metrics.mark_code_request_queued(future)
    with metrics.code_request_service(future):
        pass
    assert not vars(future)


def test_fixed_numeric_payload_nested_capture_and_frozen_close(monkeypatch):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(metrics, "perf_counter_ns", lambda: next(clock))
    with metrics.capture_code_performance() as outer:
        with metrics.code_performance_span(metrics.CodeStage.QUERY):
            with metrics.capture_code_performance() as inner:
                with metrics.code_performance_span(metrics.CodeStage.DECODE):
                    pass
        queued = SimpleNamespace()
        metrics.mark_code_request_queued(queued)
    snapshot = outer.snapshot()
    with metrics.code_request_service(queued):
        pass
    assert outer.snapshot() == snapshot
    assert set(outer.snapshot()) == {"query_total"}
    assert set(inner.snapshot()) == {"provider_decode"}
    assert all(type(value) is int for row in snapshot.values() for value in row.values())
    snapshot["query_total"]["count"] = 999
    assert outer.snapshot()["query_total"]["count"] == 1
    with pytest.raises(TypeError):
        metrics.code_performance_span("/private/source/name")


async def test_queue_explicitly_binds_origin_task_not_owner_context(monkeypatch):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(metrics, "perf_counter_ns", lambda: next(clock))
    queue = asyncio.Queue()
    async def owner():
        with metrics.capture_code_performance() as other:
            future = await queue.get()
            with metrics.code_request_service(future):
                future.set_result("unchanged")
        return other.snapshot()
    task = asyncio.create_task(owner())
    with metrics.capture_code_performance() as caller:
        future = asyncio.get_running_loop().create_future()
        metrics.mark_code_request_queued(future)
        await queue.put(future)
        assert await future == "unchanged"
        assert await task == {}
    assert caller.snapshot() == {
        "provider_queue_wait": {"count": 1, "elapsed_ns": 10, "failed": 0, "cancelled": 0},
        "provider_service": {"count": 1, "elapsed_ns": 10, "failed": 0, "cancelled": 0},
    }
    assert not hasattr(future, "_code_performance_timing")


def provider_with_call(call):
    provider = FrankenmemoryProvider.__new__(FrankenmemoryProvider)
    provider._broker_url = None
    provider._scope = lambda owner: None
    provider._call_tool = call
    return provider


async def test_query_capture_preserves_polling_and_wire_result(monkeypatch):
    polls = 0
    async def call(name, arguments):
        nonlocal polls
        if arguments["action"] == "start":
            return {"token": "private-token-not-in-metrics"}
        polls += 1
        if polls <= 9:
            return {"state": "running"}
        return {"state": "completed", "result": {"hits": [], "receipt": {"head": "same"}}}
    delays = []
    original_sleep = asyncio.sleep
    async def sleep(delay):
        delays.append(delay)
        await original_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with metrics.capture_code_performance() as capture:
        result = await provider_with_call(call).code_query(
            binding={"owner": "synthetic-private-owner"}, query={"operation": "catalog"},
        )
    assert delays == [.001, .002, .004, .008, .016, .032, .064, .100, .100]
    assert result == {"hits": [], "receipt": {"head": "same"}, "cost": {"native_rpc_calls_completed": 11}}
    stages = capture.snapshot()
    assert stages["query_total"]["count"] == 1
    assert stages["query_start_wait"]["count"] == 1
    assert stages["query_poll_wait"]["count"] == 10
    assert stages["query_observation_wait"]["count"] == 9
    assert "private" not in json.dumps(stages)
    assert all(row["elapsed_ns"] >= 0 for row in stages.values())


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_long_query_observation_has_bounded_cadence_not_total_deadline(monkeypatch, terminal):
    polls, delays = 0, []
    original_sleep = asyncio.sleep
    async def sleep(delay):
        delays.append(delay)
        await original_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    async def call(name, arguments):
        nonlocal polls
        if arguments["action"] == "start":
            return {"token": "same-long-job"}
        assert arguments["action"] == "poll"
        assert arguments["token"] == "same-long-job"
        polls += 1
        if polls <= 1000:
            return {"state": "running"}
        return {"state": terminal, "result": {"hits": ["unchanged"]},
                "error": {"code": "sentinel", "message": "unchanged terminal error"}}
    result = await provider_with_call(call).code_query(binding={"owner": "test"}, query={})
    assert len(delays) == 1000
    assert delays[:7] == [.001, .002, .004, .008, .016, .032, .064]
    assert delays[7:] == [.100] * 993
    assert sum(delays) == pytest.approx(99.427)
    assert result["cost"]["native_rpc_calls_completed"] == 1002
    if terminal == "completed":
        assert result["hits"] == ["unchanged"]
        assert "error" not in result
    else:
        assert result["error"] == {"code": "sentinel", "message": "unchanged terminal error"}


@pytest.mark.parametrize("shutdown", [False, True])
async def test_capped_observation_cancellation_and_shutdown_keep_exact_cleanup(monkeypatch, shutdown):
    entered = asyncio.Event()
    actions = []
    original_sleep = asyncio.sleep
    async def call(name, arguments):
        actions.append(arguments["action"])
        if arguments["action"] == "start":
            return {"token": "owned-capped-job"}
        assert arguments["token"] == "owned-capped-job"
        assert arguments["action"] in {"poll", "cancel"}
        return {"state": "running"} if arguments["action"] == "poll" else {}
    provider = provider_with_call(call)
    provider._owner_task = asyncio.current_task()
    async def sleep(delay):
        if delay == .100:
            entered.set()
            if shutdown:
                provider._code_query_closing = True
            else:
                await asyncio.Event().wait()  # Cancelled, never clock-expired.
        await original_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    task = asyncio.create_task(provider.code_query(binding={"owner": "test"}, query={}))
    await entered.wait()
    if shutdown:
        result = await task
        assert result["error"]["code"] == "cancelled"
        assert result["cost"]["native_rpc_calls_completed"] == 9
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert actions == ["start"] + ["poll"] * 8 + ["cancel"]
    assert not provider._code_query_pending_starts


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failure_and_cancellation_are_rethrown_unchanged(cancelled):
    error = asyncio.CancelledError() if cancelled else ValueError("private source")
    @metrics.measured_code_query
    async def fail():
        raise error
    with metrics.capture_code_performance() as capture:
        with pytest.raises(type(error)) as observed:
            await fail()
    assert observed.value is error
    row = capture.snapshot()["query_total"]
    assert row["cancelled"] == int(cancelled)
    assert row["failed"] == int(not cancelled)
    assert "private" not in json.dumps(capture.snapshot())


async def test_cancel_pending_start_still_owns_exact_job_cleanup():
    entered, release = asyncio.Event(), asyncio.Event()
    actions = []
    async def call(name, arguments):
        actions.append(arguments["action"])
        if arguments["action"] == "start":
            entered.set()
            await release.wait()
            return {"token": "exact-owned-token"}
        assert arguments["action"] == "cancel"
        assert arguments["token"] == "exact-owned-token"
        return {}
    provider = provider_with_call(call)
    # Cancellation checks that the owner exists, but does not touch this task.
    provider._owner_task = asyncio.current_task()
    with metrics.capture_code_performance() as capture:
        task = asyncio.create_task(provider.code_query(binding={"owner": "test"}, query={}))
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert actions == ["start", "cancel"]
    assert not provider._code_query_pending_starts
    assert capture.snapshot()["query_total"]["cancelled"] == 1

"""Private visual job plumbing only; no native child, app or live data."""
import asyncio
import copy

import pytest

from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_provider import MemoryRequestRejectedError, MemoryTransportError


def provider(call):
    value = FrankenmemoryProvider.__new__(FrankenmemoryProvider)
    value._broker_url = None
    value._scope = lambda owner: None
    value._call_tool = call
    value._owner_task = asyncio.current_task()
    return value


def request(mode="map"):
    return {"mode":mode, "request":{"schema":"open-clank.code-map/v1", "fixture":[1]}}


@pytest.mark.parametrize("mode", ["map", "selection"])
async def test_visual_uses_same_start_poll_owner_and_unwraps_only_matching_mode(mode):
    calls = []
    async def call(name, arguments):
        assert name == "code_query"
        calls.append(copy.deepcopy(arguments))
        if arguments["action"] == "start":
            return {"token":"owned-token"}
        return {"state":"completed", "result":{"mode":mode, "result":{"schema":"open-clank.code-map/v1", "cost":{"source_bytes_read":0}}}}
    result = await provider(call).code_visual(binding={"owner":"alice", "origin":"app"}, visual_request=request(mode))
    assert calls == [
        {"action":"start", "binding":{"owner":"alice", "origin":"app"}, "visual_request":request(mode)},
        {"action":"poll", "binding":{"owner":"alice", "origin":"app"}, "token":"owned-token"}]
    assert result == {"schema":"open-clank.code-map/v1", "cost":{"source_bytes_read":0, "native_rpc_calls_completed":2}}


@pytest.mark.parametrize("bad", [None, [], {}, {"mode":"other", "request":{}},
    {"mode":"map", "request":[],}, {"mode":"map", "request":{}, "query":{}}])
async def test_bad_modes_rejected_before_start(bad):
    async def call(*args):
        raise AssertionError("must not start")
    with pytest.raises(MemoryRequestRejectedError):
        await provider(call).code_visual(binding={"owner":"alice", "origin":"app"}, visual_request=bad)


@pytest.mark.parametrize("broker,origin", [("http://unused", "app"), (None, "agent"), (None, None)])
async def test_agent_and_broker_paths_cannot_dispatch_visual(broker, origin):
    async def call(*args):
        raise AssertionError("must not start")
    value = provider(call)
    value._broker_url = broker
    with pytest.raises(MemoryRequestRejectedError):
        await value.code_visual(binding={"owner":"alice", "origin":origin}, visual_request=request())


@pytest.mark.parametrize("patch", [{"query":{}}, {"source_claim":{}}, {"_read_fence_only":True}])
async def test_visual_is_mutually_exclusive_with_old_query_modes(patch):
    async def call(*args):
        raise AssertionError("must not start")
    with pytest.raises(MemoryRequestRejectedError):
        await provider(call).code_query(**{ "binding":{"owner":"alice", "origin":"app"},
            "query":None, "_visual_request":request(), **patch})


@pytest.mark.parametrize("reply", [{"mode":"selection", "result":{"cost":{}}},
    {"mode":"map", "result":None}, {"mode":"map", "result":{"cost":[]}},
    {"mode":"map", "result":{"cost":{}}, "extra":True}, {"hits":[], "cost":{}}])
async def test_mode_mismatch_and_old_evidence_result_are_not_visual_success(reply):
    async def call(name, arguments):
        return {"token":"owned"} if arguments["action"] == "start" else {"state":"completed", "result":reply}
    with pytest.raises(MemoryTransportError):
        await provider(call).code_visual(binding={"owner":"alice", "origin":"app"}, visual_request=request())


@pytest.mark.parametrize("state", ["failed", "cancelled"])
async def test_typed_native_failure_remains_failure_not_empty_map(state):
    async def call(name, arguments):
        return {"token":"owned"} if arguments["action"] == "start" else {"state":state, "error":{"code":"stale_cursor", "message":"changed"}}
    result = await provider(call).code_visual(binding={"owner":"alice", "origin":"app"}, visual_request=request())
    assert result == {"error":{"code":"stale_cursor", "message":"changed"}, "cost":{"native_rpc_calls_completed":2}}


@pytest.mark.parametrize("late_ack", [False, True])
async def test_visual_cancellation_owns_exact_token_even_after_late_ack(late_ack):
    entered, release = asyncio.Event(), asyncio.Event()
    calls, original = [], request()
    async def call(name, arguments):
        calls.append(copy.deepcopy(arguments))
        if arguments["action"] == "start":
            if late_ack:
                entered.set()
                await release.wait()
            return {"token":"same-visual-token"}
        if arguments["action"] == "poll":
            entered.set()
            await release.wait()
            return {"state":"running"}
        assert arguments["action"] == "cancel" and arguments["token"] == "same-visual-token"
        return {}
    value = provider(call)
    task = asyncio.create_task(value.code_visual(binding={"owner":"alice", "origin":"app"}, visual_request=original))
    await entered.wait()
    original["request"]["fixture"].append(2)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls[0]["visual_request"]["request"]["fixture"] == [1]
    assert [row["action"] for row in calls] == (["start","cancel"] if late_ack else ["start","poll","cancel"])
    assert not value._code_query_pending_starts
    await asyncio.gather(*list(value.__dict__.get("_code_query_cleanups", ())))


async def test_visual_shutdown_after_ack_cancels_without_poll_or_restart():
    calls = []
    async def call(name, arguments):
        calls.append(arguments["action"])
        if arguments["action"] == "start":
            value._code_query_closing = True
            return {"token":"owned-closing-token"}
        assert arguments["action"] == "cancel" and arguments["token"] == "owned-closing-token"
        return {}
    value = provider(call)
    result = await value.code_visual(binding={"owner":"alice", "origin":"app"}, visual_request=request())
    assert result["error"]["code"] == "cancelled"
    assert calls == ["start", "cancel"] and not value._code_query_pending_starts

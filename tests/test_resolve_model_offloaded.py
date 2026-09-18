"""Issue #4589 — model resolution performs synchronous database work, so
calling it directly from an async handler can stall the event loop. The async
call sites wrap normalized route resolution in asyncio.to_thread.

do_pipeline is used as the representative handler: normalized route resolution
is the first real work it does, and a ValueError returns early before any model
operation, so these tests drive the offload path without a live provider.
"""

import asyncio
import threading
import time

import src.ai_interaction as ai
from types import SimpleNamespace


async def test_do_pipeline_resolves_model_off_the_event_loop(monkeypatch):
    # A deliberately blocking route resolver that records how many copies run
    # at once. If it ran on the event loop, the first call would block the loop
    # and the second could not start — peak concurrency would be 1.
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    def slow_resolve(*, model_spec, owner=None):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.2)
        with lock:
            state["active"] -= 1
        raise ValueError("no such model")  # early-return path, no LLM call

    monkeypatch.setattr("src.openclank.chat_routing.resolve_chat_model_spec", slow_resolve)

    content = '[{"model": "m", "instruction": "go"}]'
    results = await asyncio.gather(
        ai.do_pipeline(content, owner="u"),
        ai.do_pipeline(content, owner="u"),
    )

    assert all("error" in r for r in results)
    assert state["peak"] == 2, "resolutions did not overlap — call still blocks the loop"


async def test_do_pipeline_uses_offloaded_resolution_result(monkeypatch):
    # The offload must also return the resolved tuple, not just propagate errors.
    monkeypatch.setattr(
        "src.openclank.chat_routing.resolve_chat_model_spec",
        lambda **kwargs: SimpleNamespace(
            model_route_id="route-1",
            provider_grant_id=None,
            provider_model_id="resolved-model",
        ),
    )

    async def fake_llm(**kwargs):
        return "output from resolved-model"

    monkeypatch.setattr("src.openclank.modality_facade.complete_text", fake_llm)

    result = await ai.do_pipeline('[{"model": "m", "instruction": "go"}]', owner="u")

    assert "error" not in result, result
    # The normalized model the offloaded resolver returned reached the result.
    assert "resolved-model" in str(result)

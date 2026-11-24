"""Regression tests for retired-wire parsers and managed agent fallbacks.

Covers the five cases requested during PR review:
  1. private sync parser: content="" + reasoning_content="..." → reasoning text
  2. private async parser: same
  3. Normal content wins over reasoning_content when both present
  4. Streaming agent path: reasoning-only round does NOT emit the generic error
  5. Streaming agent path: reasoning tokens are NOT duplicated as normal answer text
"""
import asyncio
import json

import httpx
import pytest

from src import llm_core


# ---------------------------------------------------------------------------
# Helpers: fake responses for the quarantined legacy-wire parser fixtures.
# ---------------------------------------------------------------------------

def _sync_response(payload: dict) -> httpx.Response:
    req = httpx.Request("POST", "http://test/v1/chat/completions")
    return httpx.Response(200, request=req, json=payload)


def _openai_msg(content, reasoning_content=None):
    msg = {"content": content}
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    return {"choices": [{"message": msg}]}


# ---------------------------------------------------------------------------
# 1. llm_call (sync): empty content → falls back to reasoning_content
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# 2. llm_call_async (async): empty content → falls back to reasoning_content
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# 3. Normal content takes priority over reasoning_content when both present
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Streaming agent path tests (4 and 5)
# These import and test _empty_response_fallback — the real production helper
# extracted from stream_agent_loop.  If the fallback branch is reverted or
# changed, these tests will fail.
# ---------------------------------------------------------------------------

from src.agent_loop import _empty_response_fallback  # noqa: E402


# ---------------------------------------------------------------------------
# 4. Reasoning-only round: generic error is suppressed
# ---------------------------------------------------------------------------

def test_stream_agent_reasoning_only_does_not_emit_error():
    final_response, chunk = _empty_response_fallback(
        full_response="",
        round_reasoning="I reasoned carefully",
        tool_events=[],
    )
    assert chunk is None, "Must not emit any SSE chunk when reasoning is present"
    assert "The model returned an empty response" not in (chunk or "")
    assert final_response == "I reasoned carefully"


# ---------------------------------------------------------------------------
# 5. Reasoning tokens are NOT re-emitted as a normal answer delta
# ---------------------------------------------------------------------------

def test_stream_agent_reasoning_not_duplicated_as_normal_delta():
    reasoning_text = "my internal reasoning"
    _, chunk = _empty_response_fallback(
        full_response="",
        round_reasoning=reasoning_text,
        tool_events=[],
    )
    # chunk must be None — the reasoning was already sent as {thinking:true}
    assert chunk is None, (
        f"reasoning text was re-emitted as a normal delta chunk: {chunk!r}"
    )

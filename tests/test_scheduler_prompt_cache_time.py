"""Regression tests for #4850 — scheduled-task system prompt must not embed
a minute-level timestamp that busts the Anthropic prompt cache.

Three focused tests:
1. End-to-end: system prompt is clean; message ordering is [system, datetime
   user-context, task user-prompt] through the strict Agent door.
2. Failure: an Agent error does not fall back to no-tools inference.
3. Helper: current_datetime_context_message_for_tz() renders the correct local
   time for an explicit IANA timezone, and falls back to UTC for None or invalid.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace


def _make_task(prompt="run the digest"):
    return SimpleNamespace(
        id="task-1", crew_member_id=None, endpoint_id="mimo",
        endpoint_url="mimo://acp", model="xiaomi/mimo-auto",
        session_id="s", owner="admin", prompt=prompt,
        name="job", max_steps=5, max_tool_calls=5, character_id=None,
        allowed_tools="[]", workspace=None,
    )


def _patch_scheduler_deps(monkeypatch):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: [] if key == "disabled_tools" else default,
    )
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: None)


# ---------------------------------------------------------------------------
# Test 1 — end-to-end: system is clean; agent-loop message ordering is correct
# ---------------------------------------------------------------------------

async def test_scheduler_agent_loop_path(monkeypatch):
    """Drive _execute_llm_task end-to-end (real _run_agent_loop, stubbed
    stream_agent_loop).  Asserts:
      - system message contains no 'Current time:' prefix
      - messages[1] is a user-role date/time context block
      - messages[2] is the task prompt
    """
    _patch_scheduler_deps(monkeypatch)

    captured = {}

    async def _stub_stream(target, messages, **kwargs):
        captured["messages"] = list(messages)
        yield 'data: {"delta": "done"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr("src.model_dispatch.stream_agent_target", _stub_stream)

    from src.task_scheduler import TaskScheduler
    task = _make_task()
    await TaskScheduler(session_manager=None)._run_agent_loop(
        task.endpoint_url,
        task.model,
        task,
        task.session_id,
        datetime_context_msg={"role": "user", "content": "## Current date and time\nNow"},
    )

    msgs = captured.get("messages", [])
    assert len(msgs) == 3, f"expected 3 messages, got {len(msgs)}"
    assert msgs[0]["role"] == "system"
    assert "Current time:" not in msgs[0]["content"]
    assert msgs[1]["role"] == "user"
    assert "## Current date and time" in msgs[1]["content"]
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"] == "run the digest"


# ---------------------------------------------------------------------------
# Test 2 — strict failure has no no-tools success fallback
# ---------------------------------------------------------------------------

async def test_scheduler_has_no_agent_failure_fallback(monkeypatch):
    _patch_scheduler_deps(monkeypatch)

    async def _failed_stream(*args, **kwargs):
        yield 'event: error\ndata: {"code":"SUPERVISOR_UNAVAILABLE","error":"down"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr("src.model_dispatch.stream_agent_target", _failed_stream)

    import pytest
    from src.task_scheduler import TaskScheduler
    sched = TaskScheduler(session_manager=None)
    task = _make_task(prompt="send the digest")
    with pytest.raises(RuntimeError, match="SUPERVISOR_UNAVAILABLE"):
        await sched._run_agent_loop(
            task.endpoint_url, task.model, task, task.session_id,
        )


# ---------------------------------------------------------------------------
# Test 3 — current_datetime_context_message_for_tz() timezone resolution
# ---------------------------------------------------------------------------

def test_datetime_context_message_for_tz(monkeypatch):
    """Three cases with a fixed UTC timestamp (2026-06-25 18:00 UTC):
      - explicit 'America/New_York' → 2:00 PM EDT, UTC-04:00
      - None                        → UTC fallback: 6:00 PM, UTC+00:00
      - invalid IANA name           → UTC fallback: same
    """
    from src.user_time import current_datetime_context_message_for_tz

    fixed = datetime(2026, 6, 25, 18, 0, tzinfo=timezone.utc)

    # Explicit IANA timezone
    msg = current_datetime_context_message_for_tz("America/New_York", fixed)
    assert msg["role"] == "user"
    assert "America/New_York" in msg["content"]
    assert "UTC-04:00" in msg["content"]
    assert "2:00 PM" in msg["content"]

    # None → UTC (preserves old scheduler behaviour for tasks without a crew tz)
    msg = current_datetime_context_message_for_tz(None, fixed)
    assert "UTC+00:00" in msg["content"]
    assert "6:00 PM" in msg["content"]

    # Invalid IANA name → UTC fallback, no exception raised
    msg = current_datetime_context_message_for_tz("Not/A_Real_Zone", fixed)
    assert "UTC+00:00" in msg["content"]
    assert "6:00 PM" in msg["content"]

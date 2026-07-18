"""Tool cards must pair start/finish by tool-call id across the whole stream.

Live failure 2026-07-17 (mimo agent lane): the UI tracked only ONE
`currentToolBubble`; mimo runs tool calls in parallel and interleaves text
(agent_step nulls the slot), so completions closed the wrong card — or none —
and every stranded card kept its 50ms/100ms animation intervals running until
the tab crawled and the user had to refresh (killing the stream). The bridge
now stamps every tool event with the ACP toolCallId plus an exit_code the UI
understands, and the UI resolves cards through an id-keyed map with a
stream-end sweep for stragglers.
"""
import json
from pathlib import Path

import pytest

from src.openclank.acp_bridge import ACPBridge, _TurnState

_REPO = Path(__file__).resolve().parent.parent


class _Client:
    def register_callback(self, *_args):
        pass

    def on_session_update(self, *_args):
        pass


@pytest.fixture
def bridge(tmp_path):
    return ACPBridge(_Client(), str(tmp_path), owner="alice")


def _events(sses):
    return [json.loads(s[len("data: "):]) for s in sses]


def test_parallel_calls_complete_by_id_with_exit_codes(bridge):
    state = _TurnState()
    sses = []
    sses += bridge._process_update(
        "ses", {"sessionUpdate": "tool_call", "toolCallId": "a", "title": "read", "status": "pending"}, state)
    sses += bridge._process_update(
        "ses", {"sessionUpdate": "tool_call", "toolCallId": "b", "title": "grep", "status": "pending"}, state)
    # Completions arrive out of order and WITHOUT titles — the update often
    # omits them; the card must keep the name the tool_call carried.
    sses += bridge._process_update(
        "ses", {"sessionUpdate": "tool_call_update", "toolCallId": "b", "status": "failed"}, state)
    sses += bridge._process_update(
        "ses", {"sessionUpdate": "tool_call_update", "toolCallId": "a", "status": "completed"}, state)

    events = _events(sses)
    starts = [e for e in events if e["type"] == "tool_start"]
    outputs = [e for e in events if e["type"] == "tool_output"]
    assert [(e["id"], e["tool"]) for e in starts] == [("a", "read"), ("b", "grep")]
    by_id = {e["id"]: e for e in outputs}
    assert by_id["a"]["exit_code"] == 0 and by_id["a"]["tool"] == "read"
    assert by_id["b"]["exit_code"] == 1 and by_id["b"]["tool"] == "grep"


def test_progress_events_carry_id_but_no_exit_code(bridge):
    state = _TurnState()
    bridge._process_update(
        "ses", {"sessionUpdate": "tool_call", "toolCallId": "a", "title": "bash", "status": "pending"}, state)
    sses = bridge._process_update(
        "ses", {"sessionUpdate": "tool_call_update", "toolCallId": "a", "status": "in_progress"}, state)
    (event,) = _events(sses)
    assert event["type"] == "tool_progress"
    assert event["id"] == "a"
    assert "exit_code" not in event


def test_update_without_prior_call_synthesizes_a_start(bridge):
    state = _TurnState()
    sses = bridge._process_update(
        "ses",
        {"sessionUpdate": "tool_call_update", "toolCallId": "ghost", "title": "write", "status": "completed"},
        state,
    )
    events = _events(sses)
    assert [e["type"] for e in events] == ["tool_start", "tool_output"]
    assert all(e["id"] == "ghost" for e in events)
    # And the id is now known, so a duplicate start is not emitted later.
    again = _events(bridge._process_update(
        "ses", {"sessionUpdate": "tool_call", "toolCallId": "ghost", "title": "write", "status": "pending"}, state))
    assert again == []


def test_chat_js_pairs_tool_cards_by_id():
    chat = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    # tool_start registers the card under its id.
    assert "if (json.id) toolNodesById[json.id] = node;" in chat
    # progress + output resolve THIS call's card, not the newest one.
    assert chat.count("(json.id && toolNodesById[json.id]) || currentToolBubble") == 2
    # completion releases the map entry.
    assert "if (json.id) delete toolNodesById[json.id];" in chat


def test_resume_reader_shows_live_tool_activity():
    """Re-attaching to a detached run (page refresh) must not be a blind
    spinner while the agent is mid-tool-phase — that reads as a dead turn
    even though the run is alive server-side and saves on completion."""
    chat = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    resume_at = chat.index("export async function resumeStream")
    reader = chat[resume_at:resume_at + 9000]
    assert "resume-tool-feed" in reader
    assert "data-tool-id" in reader and "CSS.escape(json.id)" in reader, (
        "feed rows pair completions by tool-call id, same as live cards"
    )
    assert "setInterval" not in reader, "the feed must not leak timers"


def test_chat_js_sweeps_stranded_running_cards_at_stream_end():
    chat = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    finally_at = chat.index("} finally {", chat.index("agent-thread-node running"))
    sweep = chat[finally_at:finally_at + 2500]
    assert ".agent-thread-node.running" in sweep, "stream end sweeps stragglers"
    assert "clearInterval(n._waveInterval)" in sweep
    assert "clearInterval(n._elapsedTicker)" in sweep
    assert "interrupted" in sweep

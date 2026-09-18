"""Regression: stream_agent_loop emits `rounds_exhausted` only when the round
cap is hit while still working, and NOT on a normal finish.

The decision is a `for/else` in the loop: the `else` runs only if no `break`
fired (break = done / budget / error). A refactor that adds a stray break or
return, or moves the done-break, could silently flip this. See PR #1999 / #1997.
"""

import asyncio
import json
from types import SimpleNamespace

import src.agent_loop as al


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _types(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_common(monkeypatch):
    # Skip RAG/tool-index, MCP, and settings lookups; keep the real loop body,
    # _resolve_tool_blocks, and parse_tool_blocks.
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(
        al,
        "resolve_chat_route",
        lambda **_kwargs: SimpleNamespace(
            provider_model_id="m",
            model_route_id="route-test",
            provider_grant_id=None,
            connection_id="connection-test",
            runtime_model="connection-test/m",
            capabilities={"tools": True},
        ),
    )

    async def _fake_exec(block, *a, **k):
        return ("bash", {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _run_loop(monkeypatch, round_text, max_rounds=2):
    async def _fake_stream(_candidates, messages, **kwargs):
        if round_text.startswith("```bash"):
            yield f'data: {json.dumps({"type": "tool_calls", "calls": [{"name": "bash", "arguments": json.dumps({"command": "echo hi"})}]})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": round_text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_agent_target", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do a long multi-step task"}],
        max_rounds=max_rounds,
        relevant_tools={"bash"},
    )
    return _types(_collect(gen))


def test_emits_rounds_exhausted_when_cap_hit_mid_task(monkeypatch):
    _patch_common(monkeypatch)
    # Every round returns a tool block -> never "done" -> loop exhausts the cap.
    events = _run_loop(monkeypatch, "```bash\necho hi\n```", max_rounds=2)
    assert any(e.get("type") == "rounds_exhausted" for e in events), events


def test_no_rounds_exhausted_on_normal_finish(monkeypatch):
    _patch_common(monkeypatch)
    # A plain answer (no tool block) -> done-break on round 1 -> no event.
    events = _run_loop(monkeypatch, "All done, here is your answer.", max_rounds=2)
    assert not any(e.get("type") == "rounds_exhausted" for e in events), events


def test_emits_intent_nudge_exhausted_when_cap_is_exhausted(monkeypatch):
    _patch_common(monkeypatch)

    events = _run_loop(monkeypatch, "Let me check the logs", max_rounds=5)

    guard = next((e for e in events if e.get("type") == "intent_nudge_exhausted"), None)
    assert guard is not None, events
    assert guard["reason"] == "intent_without_action_nudge_cap"
    assert guard["nudges"] == 2


def test_emits_loop_breaker_triggered_when_loop_breaker_trips(monkeypatch):
    _patch_common(monkeypatch)

    events = _run_loop(monkeypatch, "```bash\necho hi\n```", max_rounds=6)

    guard = next((e for e in events if e.get("type") == "loop_breaker_triggered"), None)
    assert guard is not None, events
    assert guard["reason"] == "loop_breaker_stall"


def test_verifier_uses_managed_completion_identity(monkeypatch):
    from src.openclank import modality_facade

    captured = {}

    async def complete_text(**kwargs):
        captured.update(kwargs)
        return "VERIFICATION: FAIL: missing output; ignored error"

    monkeypatch.setattr(modality_facade, "complete_text", complete_text)

    failures = asyncio.run(al._run_verifier_subagent(
        "produce the report",
        "[bash] generate-report\n-> ok",
        endpoint_url="https://legacy.invalid/v1",
        model="legacy-model",
        headers={"Authorization": "must-not-cross"},
        owner="alice",
        session_id="session-1",
        workspace="/tmp/legacy",
        provider_model_route_id="pmr-chat",
        provider_grant_id="grant-1",
        root_operation_id="root-1",
    ))

    assert failures == ["missing output", "ignored error"]
    assert captured["owner"] == "alice"
    assert captured["purpose"] == "utility"
    assert captured["model_route_id"] == "pmr-chat"
    assert captured["grant_id"] == "grant-1"
    assert captured["root_operation_id"] == "root-1"
    assert "url" not in captured
    assert "headers" not in captured

import asyncio
import json
from types import SimpleNamespace
import pytest

import src.teacher_escalation as teacher_escalation


def _route(*, grant_id=None):
    return SimpleNamespace(
        model_route_id="route-teacher",
        provider_grant_id=grant_id,
        provider_model_id="teacher-model",
        connection_id="connection-teacher",
        runtime_model="connection-teacher/teacher-model",
        capabilities={"tools": True},
    )


@pytest.mark.asyncio
async def test_evaluate_turn_llm_ok(monkeypatch):
    seen = {}

    async def fake_complete_text(**kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr("src.openclank.modality_facade.complete_text", fake_complete_text)

    status, reason = await teacher_escalation.evaluate_turn_llm(
        user_request="test request",
        tool_results=[],
        agent_reply="test reply",
        student_endpoint_url="http://student.local/v1",
        owner="alice", root_operation_id="turn-1",
    )

    assert status == "ok"
    assert reason is None
    assert seen["owner"] == "alice"
    assert seen["purpose"] == "utility"
    assert seen["root_operation_id"] == "turn-1"
    assert "endpoint.local" not in json.dumps(seen)


@pytest.mark.asyncio
async def test_evaluate_turn_llm_failure(monkeypatch):
    async def fake_complete_text(**kwargs):
        return "  \"Failure\"  "

    monkeypatch.setattr("src.openclank.modality_facade.complete_text", fake_complete_text)

    status, reason = await teacher_escalation.evaluate_turn_llm(
        user_request="test request",
        tool_results=[],
        agent_reply="test reply",
        student_endpoint_url="http://student.local/v1",
        owner="alice",
    )

    assert status == "failure"
    assert "LLM evaluation flagged failure" in reason


@pytest.mark.asyncio
async def test_evaluate_turn_llm_contains_failure_but_not_exact_match(monkeypatch):
    async def fake_complete_text(**kwargs):
        return "this agent execution is not a failure"

    monkeypatch.setattr("src.openclank.modality_facade.complete_text", fake_complete_text)

    status, reason = await teacher_escalation.evaluate_turn_llm(
        user_request="test request",
        tool_results=[],
        agent_reply="test reply",
        student_endpoint_url="http://student.local/v1",
        owner="alice",
    )

    assert status == "ok"
    assert reason is None


@pytest.mark.asyncio
async def test_evaluate_turn_llm_exception_handling(monkeypatch):
    async def fake_complete_text(**kwargs):
        raise RuntimeError("model timeout")

    monkeypatch.setattr("src.openclank.modality_facade.complete_text", fake_complete_text)

    # Should degrade gracefully to "ok"
    status, reason = await teacher_escalation.evaluate_turn_llm(
        user_request="test request",
        tool_results=[],
        agent_reply="test reply",
        student_endpoint_url="http://student.local/v1",
        owner="alice",
    )

    assert status == "ok"
    assert reason is None


@pytest.mark.asyncio
async def test_maybe_escalate_triggers_tier2_background_task(monkeypatch):
    # Enable teacher settings
    monkeypatch.setattr("src.settings.get_user_setting", lambda key, owner, default=None: {"teacher_enabled": True, "teacher_model": "teacher-model", "teacher_tier2_enabled": True}.get(key, default))

    # Regex check says OK
    monkeypatch.setattr("src.teacher_escalation.evaluate_turn_regex", lambda *args: ("ok", None))

    llm_eval_called = []
    async def fake_evaluate_turn_llm(*args, **kwargs):
        llm_eval_called.append(True)
        return "failure", "LLM flagged failure"

    monkeypatch.setattr("src.teacher_escalation.evaluate_turn_llm", fake_evaluate_turn_llm)

    escalate_called = []
    async def fake_escalate_and_learn(user_request, tool_results, agent_reply, failure_reason, owner, **kwargs):
        escalate_called.append(failure_reason)
        return "skill-slug"

    monkeypatch.setattr("src.teacher_escalation.escalate_and_learn", fake_escalate_and_learn)

    # Call maybe_escalate
    task = teacher_escalation.maybe_escalate(
        student_endpoint_url="http://student.local/v1",
        mode="agent",
        user_request="test request",
        tool_results=[],
        agent_reply="test reply",
        owner="alice",
    )

    assert task is not None
    assert task.get_name() == "teacher_escalation_tier2"

    # Await the background task execution
    await task

    assert llm_eval_called == [True]
    assert escalate_called == ["LLM flagged failure"]


@pytest.mark.asyncio
async def test_maybe_escalate_tier2_disabled_by_default(monkeypatch):
    # Enable teacher settings, but keep tier2 disabled
    monkeypatch.setattr("src.settings.get_user_setting", lambda key, owner, default=None: {"teacher_enabled": True, "teacher_model": "teacher-model", "teacher_tier2_enabled": False}.get(key, default))

    # Regex check says OK
    monkeypatch.setattr("src.teacher_escalation.evaluate_turn_regex", lambda *args: ("ok", None))

    # Call maybe_escalate
    task = teacher_escalation.maybe_escalate(
        student_endpoint_url="http://student.local/v1",
        mode="agent",
        user_request="test request",
        tool_results=[],
        agent_reply="test reply",
        owner="alice",
    )

    # Should not start any background task since Tier 2 is disabled
    assert task is None


@pytest.mark.asyncio
async def test_run_teacher_inline_triggers_tier2_escalation(monkeypatch):
    # Settings and gates
    monkeypatch.setattr("src.settings.get_user_setting", lambda key, owner, default=None: {"teacher_enabled": True, "teacher_model": "teacher-model", "teacher_tier2_enabled": True}.get(key, default))
    monkeypatch.setattr(
        "src.openclank.chat_routing.resolve_chat_model_spec",
        lambda **kwargs: _route(grant_id="grant-teacher"),
    )

    # Regex evaluation says "ok"
    monkeypatch.setattr("src.teacher_escalation.evaluate_turn_regex", lambda *args: ("ok", None))

    # LLM evaluation flags "failure"
    async def fake_evaluate_turn_llm(*args, **kwargs):
        return "failure", "LLM flagged failure"
    monkeypatch.setattr("src.teacher_escalation.evaluate_turn_llm", fake_evaluate_turn_llm)

    # Mock the strict Agent door recursively called by run_teacher_inline.
    streamed = {}

    async def fake_stream_agent_target(target, *args, **kwargs):
        streamed["target"] = target
        streamed["envelope"] = kwargs.get("turn_envelope")
        yield "data: {\"type\": \"tool_output\", \"tool\": \"bash\"}\n\n"
        yield "data: {\"type\": \"text\", \"delta\": \"Teacher reply\"}\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr("src.model_dispatch.stream_agent_target", fake_stream_agent_target)

    # Mock _call_teacher returning a skill definition
    async def fake_call_teacher(spec, prompt, owner=None, root_operation_id=None):
        return '```json\n{"action": "add", "name": "test-skill"}\n```'
    monkeypatch.setattr("src.teacher_escalation._call_teacher", fake_call_teacher)

    # Mock do_manage_skills
    saved_skills = []

    async def fake_do_manage_skills(skill_json, owner=None):
        saved_skills.append(json.loads(skill_json))
        return {"success": True}
    monkeypatch.setattr("src.tool_implementations.do_manage_skills", fake_do_manage_skills)

    events = []
    async for evt in teacher_escalation.run_teacher_inline(
        student_endpoint_url="http://student.local/v1",
        student_messages=[{"role": "user", "content": "test request"}],
        student_tool_events=[],
        student_reply="student reply",
        owner="alice",
        root_operation_id="turn-inline",
    ):
        events.append(evt)

    # Make sure teacher takeover was announced and executed
    assert any("teacher_takeover" in evt for evt in events)
    assert any("tool_output" in evt for evt in events)
    assert any("skill_saved" in evt for evt in events)
    assert streamed["target"].transport == "acp"
    assert streamed["target"].endpoint_id == "connection-teacher"
    assert streamed["target"].provider_id == "connection-teacher"
    assert streamed["target"].headers == {}
    assert streamed["envelope"]["provider_grant_id"] == "grant-teacher"
    assert streamed["envelope"]["root_operation_id"] == "turn-inline"
    assert saved_skills[0]["status"] == "draft"
    assert saved_skills[0]["source"] == "teacher-escalation"


@pytest.mark.asyncio
async def test_run_teacher_inline_tier2_disabled_by_default(monkeypatch):
    # Settings and gates (Tier 2 disabled)
    monkeypatch.setattr("src.settings.get_user_setting", lambda key, owner, default=None: {"teacher_enabled": True, "teacher_model": "teacher-model", "teacher_tier2_enabled": False}.get(key, default))

    # Regex evaluation says "ok"
    monkeypatch.setattr("src.teacher_escalation.evaluate_turn_regex", lambda *args: ("ok", None))

    events = []
    async for evt in teacher_escalation.run_teacher_inline(
        student_endpoint_url="http://student.local/v1",
        student_messages=[{"role": "user", "content": "test request"}],
        student_tool_events=[],
        student_reply="student reply",
        owner="alice",
    ):
        events.append(evt)

    # Should exit early without any events (no takeover)
    assert len(events) == 0

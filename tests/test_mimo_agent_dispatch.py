"""Strict Agent dispatch accepts only normalized managed route identities."""

import json
from pathlib import Path

import pytest

from src.endpoint_resolver import ResolvedModelTarget, resolve_model_target
from src.model_dispatch import (
    AgentRunRequest,
    mimo_agent_target,
    run_agent,
    stream_agent_target,
)
from src.openclank.mimo_supervisor import SupervisorAdmissionError


def _target(*, tools=True, lifecycle="persistent"):
    return ResolvedModelTarget(
        transport="acp",
        endpoint_url="openclank://engine",
        model_id="pcn-managed/model-a",
        endpoint_id="pcn-managed",
        provider_id="pcn-managed",
        headers={},
        capabilities={"chat": True, "tools": tools},
        lifecycle=lifecycle,
    )


class _Bridge:
    def __init__(self):
        self.turns = []

    async def run_turn(self, session_id, messages, **kwargs):
        self.turns.append((session_id, messages, kwargs))
        yield f'data: {json.dumps({"delta": "managed"})}\n\n'
        yield "data: [DONE]\n\n"


class _Worker:
    def __init__(self):
        self.bridge = _Bridge()
        self.deleted = []

    async def delete_session(self, session_id):
        self.deleted.append(session_id)


class _Lease:
    generation = 7
    fingerprint = "fingerprint-managed"
    projection_pending = False

    def __init__(self):
        self.worker = _Worker()
        self.released = []

    async def release(self, *, successful_terminal=False):
        self.released.append(successful_terminal)


class _Pool:
    def __init__(self):
        self.lease = _Lease()
        self.admissions = []

    async def admit_agent(self, owner, provider_id, model_id):
        self.admissions.append((owner, provider_id, model_id))
        return self.lease


@pytest.mark.asyncio
async def test_normalized_target_is_stable_and_raw_http_fails_closed():
    target = _target()
    assert await mimo_agent_target(target, owner="alice") is target

    raw = resolve_model_target(
        "https://provider.invalid/v1/chat/completions",
        "model-a",
    )
    with pytest.raises(SupervisorAdmissionError) as rejected:
        await mimo_agent_target(raw, owner="alice")
    assert rejected.value.code == "LEGACY_PROVIDER_ROUTE_RETIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("model_id", "unqualified"),
        ("endpoint_id", "different"),
        ("provider_id", "different"),
        ("headers", {"Authorization": "must-not-cross"}),
    ],
)
async def test_managed_target_requires_exact_secret_free_connection_identity(field, value):
    values = _target().__dict__.copy()
    values[field] = value
    malformed = ResolvedModelTarget(**values)
    with pytest.raises(SupervisorAdmissionError) as rejected:
        await mimo_agent_target(malformed, owner="alice")
    assert rejected.value.code == "INVALID_MANAGED_ROUTE"


@pytest.mark.asyncio
async def test_run_agent_admits_exact_connection_model_and_releases_terminal_lease():
    pool = _Pool()
    request = AgentRunRequest(
        target=_target(),
        messages=[{"role": "user", "content": "test"}],
        session_id="managed-session",
        owner="alice",
        supervisor=pool,
        turn_envelope={"root_operation_id": "root-1"},
    )

    events = [event async for event in run_agent(request)]

    assert pool.admissions == [("alice", "pcn-managed", "model-a")]
    assert events[-1] == "data: [DONE]\n\n"
    assert pool.lease.released == [True]
    session_id, _messages, kwargs = pool.lease.worker.bridge.turns[0]
    assert session_id == "managed-session"
    assert kwargs["model"] == "pcn-managed/model-a"
    assert kwargs["turn_envelope"]["lane"] == "agent"
    assert kwargs["turn_envelope"]["root_operation_id"] == "root-1"


@pytest.mark.asyncio
async def test_tools_false_clears_agent_tool_authority():
    pool = _Pool()
    request = AgentRunRequest(
        target=_target(tools=False),
        messages=[{"role": "user", "content": "test"}],
        session_id="tools-off",
        owner="alice",
        supervisor=pool,
        turn_envelope={"allowed_tools": ["bash"]},
    )

    events = [event async for event in run_agent(request)]

    assert events[-1] == "data: [DONE]\n\n"
    envelope = pool.lease.worker.bridge.turns[0][2]["turn_envelope"]
    assert envelope["allowed_tools"] == []


@pytest.mark.asyncio
async def test_stream_wrapper_propagates_root_and_server_file_policy(monkeypatch):
    import src.tool_security as tool_security

    monkeypatch.setattr(tool_security, "brokered_agent_file_tools", lambda owner, cwd: {"read_file"})
    monkeypatch.setattr(
        tool_security,
        "unavailable_strict_agent_tools",
        lambda owner, cwd: {"bash", "python", "write_file"},
    )
    pool = _Pool()
    events = [
        event
        async for event in stream_agent_target(
            _target(),
            [{"role": "user", "content": "test"}],
            session_id="wrapped",
            owner="alice",
            supervisor=pool,
            relevant_tools={"read_file", "bash"},
            disabled_tools={"python"},
            max_tool_calls=3,
            turn_envelope={"root_operation_id": "root-wrapped"},
        )
    ]

    assert events[-1] == "data: [DONE]\n\n"
    envelope = pool.lease.worker.bridge.turns[0][2]["turn_envelope"]
    assert envelope["root_operation_id"] == "root-wrapped"
    assert envelope["allowed_tools"] == ["read_file"]
    assert {"bash", "python", "write_file"}.issubset(envelope["disabled_tools"])
    assert envelope["brokered_file_tools"] == ["read_file"]
    assert envelope["max_tool_calls"] == 3


@pytest.mark.asyncio
async def test_missing_supervisor_returns_typed_terminal_error():
    events = [
        event
        async for event in run_agent(
            AgentRunRequest(
                target=_target(),
                messages=[{"role": "user", "content": "test"}],
                session_id="missing-supervisor",
            )
        )
    ]
    assert "SUPERVISOR_UNAVAILABLE" in events[0]
    assert events[-1] == "data: [DONE]\n\n"


def test_mounted_chat_and_agent_loop_have_no_legacy_transport_fallback():
    chat = Path("routes/chat_routes.py").read_text(encoding="utf-8")
    loop = Path("src/agent_loop.py").read_text(encoding="utf-8")

    assert "stream_agent_target(" in chat
    assert 'if requested_mode not in ("", "agent", "chat", "plan"):' in chat
    assert "stream_llm_with_fallback" not in chat
    assert "stream_llm_with_fallback" not in loop
    assert "llm_call_async" not in loop
    assert "ModelEndpoint" not in loop
    assert loop.count("stream_agent_target(") >= 2

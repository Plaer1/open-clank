import asyncio
from types import SimpleNamespace

from src import bg_monitor


def test_drain_agent_ignores_non_string_deltas(monkeypatch):
    seen = {}

    async def fake_stream_agent_target(target, *args, **kwargs):
        seen["target"] = target
        seen.update(kwargs)
        yield 'data: {"delta": null}'
        yield 'data: {"delta": ["bad"]}'
        yield 'data: {"delta": "ok"}'
        yield 'data: {"type": "agent_step", "round": 2}'
        yield 'data: {"type": "tool_output", "tool": "shell", "output": "done"}'
        yield "data: [DONE]"

    monkeypatch.setattr(
        "src.model_dispatch.stream_agent_target", fake_stream_agent_target,
    )
    monkeypatch.setattr(
        "src.openclank.chat_routing.resolve_chat_route",
        lambda **_: SimpleNamespace(
            runtime_model="conn-1/model",
            connection_id="conn-1",
            capabilities={"tools": True},
            model_route_id="pmr-1",
            provider_grant_id=None,
        ),
    )

    sess = SimpleNamespace(
        endpoint_url="http://example.test",
        model="model",
        headers=None,
        context_length=0,
        id="s1",
        endpoint_id="endpoint-1",
        provider_model_route_id="pmr-1",
        owner="alice",
    )

    full, events = asyncio.run(
        bg_monitor._drain_agent(
            sess,
            [],
            workspace="/workspace",
            root_operation_id="bg-followup:job-1",
        )
    )

    assert full == "ok"
    assert seen["owner"] == "alice"
    assert seen["cwd"] == "/workspace"
    assert seen["target"].transport == "acp"
    assert seen["target"].headers == {}
    assert seen["turn_envelope"]["root_operation_id"] == "bg-followup:job-1"
    assert seen["turn_envelope"]["provider_model_route_id"] == "pmr-1"
    assert events == [{
        "round": 2,
        "tool": "shell",
        "command": None,
        "output": "done",
        "exit_code": None,
    }]


def test_followup_rejects_owner_mismatch(monkeypatch):
    sess = SimpleNamespace(
        id="s1",
        owner="bob",
    )
    manager = SimpleNamespace(get_session=lambda _session_id: sess)
    monkeypatch.setattr(
        "src.ai_interaction.get_session_manager",
        lambda: manager,
    )

    handled = asyncio.run(bg_monitor._run_followup({
        "id": "job1",
        "session_id": "s1",
        "owner": "alice",
        "workspace": "/workspace",
    }))

    assert handled is True


def test_followup_reuses_recorded_workspace(monkeypatch):
    seen = {}

    async def fake_drain(_sess, _messages, *, workspace, root_operation_id):
        seen["workspace"] = workspace
        seen["root_operation_id"] = root_operation_id
        return "continued", []

    sess = SimpleNamespace(
        id="s1",
        owner="alice",
        model="model",
        history=[],
        get_context_messages=lambda: [],
    )

    class Manager:
        def get_session(self, _session_id):
            return sess

        def add_message(self, _session_id, _message):
            pass

        def save_sessions(self):
            pass

    monkeypatch.setattr(
        "src.ai_interaction.get_session_manager",
        lambda: Manager(),
    )
    monkeypatch.setattr("src.agent_runs.is_active", lambda _session_id: False)
    monkeypatch.setattr(bg_monitor, "_drain_agent", fake_drain)
    monkeypatch.setattr(bg_monitor.bg_jobs, "result_text", lambda _rec: "done")

    handled = asyncio.run(bg_monitor._run_followup({
        "id": "job1",
        "session_id": "s1",
        "owner": "alice",
        "workspace": "/tenant/alice/project",
    }))

    assert handled is True
    assert seen["workspace"] == "/tenant/alice/project"
    assert seen["root_operation_id"] == "bg-followup:job1"


def test_terminal_followup_failure_is_redacted_before_chat_history(monkeypatch):
    saved = []
    secret = "sk-secret-secret-secret-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    sess = SimpleNamespace(id="s1", owner="alice", history=[])

    class Manager:
        def get_session(self, _session_id):
            return sess

        def add_message(self, _session_id, message):
            saved.append(message)

        def save_sessions(self):
            pass

    monkeypatch.setattr(
        "src.ai_interaction.get_session_manager",
        lambda: Manager(),
    )
    asyncio.run(bg_monitor._persist_terminal_failure(
        {
            "id": "job1",
            "session_id": "s1",
            "owner": "alice",
            "workspace": "/tenant/alice/project",
        },
        f"provider rejected {secret}",
    ))

    assert len(saved) == 1
    assert secret not in saved[0].content
    assert "<redacted" in saved[0].content

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.openclank.acp_supervisor_adapter import (
    AcpRuntimeAdapter,
    AcpSupervisorAdapter,
)
from src.openclank.agent_supervisor import (
    AgentRuntime,
    AgentSupervisor,
    AgentSupervisorAdmissionError,
    RuntimeLease,
    select_host_provider_owner,
)
from src.openclank.agent_supervisor_factory import (
    AgentSupervisorConfigurationError,
    selected_agent_supervisor_backend,
)
from src.openclank.mimo_supervisor import SupervisorAdmissionError


class FakeBridge:
    available_models = [{"modelId": "provider/model"}]
    question_handler = object()

    def __init__(self) -> None:
        self.turn_calls = []

    async def run_turn(self, session_id, messages, **options):
        self.turn_calls.append((session_id, messages, options))
        yield "data: hello\n\n"
        yield "data: [DONE]\n\n"


class FakeRuntime:
    installed_generation = 7
    installed_fingerprint = "fingerprint-7"

    def __init__(self) -> None:
        self.bridge = FakeBridge()
        self.permission_handler = object()
        self.grant_store = object()
        self.calls = []
        self.stopped = False

    @property
    def question_handler(self):
        return self.bridge.question_handler

    @property
    def http_base_url(self):
        return "http://127.0.0.1:43111"

    def internal_http_client(self, *, timeout=20.0):
        return ("client", timeout)

    def is_alive(self, owner=None):
        return not self.stopped

    def available_models(self, owner=None):
        return list(self.bridge.available_models)

    def provider_apis(self, owner=None):
        return {"provider": "https://provider.invalid"}

    async def refresh_model_catalog(self, *, owner=None):
        return self.available_models(owner)

    async def managed_engine_call(self, method, params):
        self.calls.append(("control", method, params))
        return {"ok": True}

    async def execute_operation(self, payload):
        self.calls.append(("operation", payload))
        return {"state": "complete"}

    async def negotiate_session(self, session_id, *, owner, cwd=None):
        return {"session": session_id, "owner": owner, "cwd": cwd}

    async def set_session_config(self, session_id, config_id, value, *, owner, cwd=None):
        return {"session": session_id, "config": {config_id: value}}

    async def session_http_request(
        self,
        session_id,
        method,
        suffix,
        *,
        owner,
        payload=None,
        timeout=20.0,
    ):
        return (session_id, method, suffix, owner, payload, timeout)

    async def delete_session(self, session_id, *, owner=None, mimo_session_id=None):
        self.calls.append(("delete", session_id, owner, mimo_session_id))

    async def stop(self):
        self.stopped = True


class FakeLease:
    def __init__(self, worker):
        self.owner = "alice"
        self.worker = worker
        self.generation = worker.installed_generation
        self.fingerprint = worker.installed_fingerprint
        self.projection_pending = True
        self.releases = []

    async def release(self, *, successful_terminal=False):
        self.releases.append(successful_terminal)


class FakePool:
    def __init__(self):
        self.worker = FakeRuntime()
        self.lease = FakeLease(self.worker)
        self.calls = []

    async def start(self):
        self.calls.append("start")

    async def stop(self):
        self.calls.append("stop")

    def readiness(self):
        return {"ok": True, "event_loop": True, "owner_workers": 1}

    def is_alive(self, owner=None):
        return self.worker.is_alive(owner)

    def run_sync(self, awaitable, *, timeout=None):
        return (awaitable, timeout)

    async def for_owner(self, owner):
        assert owner == "alice"
        return self.worker

    async def admit_agent(self, owner, provider_id, model_id):
        assert (owner, provider_id, model_id) == ("alice", "provider", "model")
        return self.lease

    async def admit_provider_control(self, owner):
        assert owner == "alice"
        return self.lease

    async def admit_shared_agent(self, access, provider_id, model_id):
        raise RuntimeError((access, provider_id, model_id))

    def available_models(self, owner=None):
        return self.worker.available_models(owner)

    def provider_apis(self, owner=None):
        return self.worker.provider_apis(owner)

    async def refresh_model_catalog(self, *, owner=None):
        return await self.worker.refresh_model_catalog(owner=owner)

    async def execute_operation(self, owner, payload):
        assert owner == "alice"
        return await self.worker.execute_operation(payload)

    async def negotiate_session(self, session_id, *, owner, cwd=None):
        return await self.worker.negotiate_session(session_id, owner=owner, cwd=cwd)

    async def set_session_config(self, session_id, config_id, value, *, owner, cwd=None):
        return await self.worker.set_session_config(
            session_id, config_id, value, owner=owner, cwd=cwd,
        )

    async def session_http_request(self, session_id, method, suffix, **options):
        return await self.worker.session_http_request(session_id, method, suffix, **options)

    async def delete_session(self, session_id, *, owner=None, mimo_session_id=None):
        await self.worker.delete_session(
            session_id, owner=owner, mimo_session_id=mimo_session_id,
        )

    def mapped_sessions(self, owner=None):
        return {"canonical": "runtime"} if owner == "alice" else {}

    def permission_handler_for(self, owner, request_id=None):
        return ("permission", owner, request_id)

    def question_handler_for(self, owner, request_id=None):
        return ("question", owner, request_id)

    def grant_store_for(self, owner):
        return ("grant-store", owner)

    async def refresh_endpoint_projection(self):
        self.calls.append("refresh")

    async def invalidate_owner_projection(self, owner):
        self.calls.append(("invalidate", owner))

    async def revoke_shared_access(self, actor_owner, share_id):
        self.calls.append(("revoke", actor_owner, share_id))

    async def rename_owner(self, old_owner, new_owner):
        self.calls.append(("rename", old_owner, new_owner))

    async def purge_owner(self, owner):
        self.calls.append(("purge", owner))

    async def preview_owner_memory(self, owner):
        self.calls.append(("preview-memory", owner))
        return {"count": 2, "fingerprint": "sha256:preview"}

    async def reset_owner_memory(self, owner, *, expected):
        self.calls.append(("reset-memory", owner, expected))
        return {"complete": True, "count": int(expected["count"])}

    def worker_for_owner(self, owner):
        return self.worker if owner == "alice" else None

    @property
    def bridge(self):
        return self.worker.bridge

    @property
    def permission_handler(self):
        return self.worker.permission_handler

    @property
    def http_base_url(self):
        return self.worker.http_base_url


def test_admission_error_projection_is_stable_and_mimo_error_is_compatible():
    error = AgentSupervisorAdmissionError(
        "MODEL_NOT_PROJECTED",
        "model unavailable",
        phase="routing",
        retryable=False,
        status=409,
        actions=("refresh_catalog",),
        details={"generation": 7},
    )
    assert error.as_dict() == {
        "code": "MODEL_NOT_PROJECTED",
        "error": "model unavailable",
        "phase": "routing",
        "retryable": False,
        "status": 409,
        "actions": ["refresh_catalog"],
        "details": {"generation": 7},
    }
    assert issubclass(SupervisorAdmissionError, AgentSupervisorAdmissionError)


def test_compatibility_admission_error_can_be_constructed_with_full_contract():
    error = SupervisorAdmissionError(
        "RUNTIME_UNAVAILABLE",
        "runtime unavailable",
        phase="admission",
        retryable=False,
        status=409,
        actions=("refresh",),
        details={"generation": 3},
    )
    assert error.as_dict() == {
        "code": "RUNTIME_UNAVAILABLE",
        "error": "runtime unavailable",
        "phase": "admission",
        "retryable": False,
        "status": 409,
        "actions": ["refresh"],
        "details": {"generation": 3},
    }


def test_backend_selection_is_server_owned_and_fails_closed(monkeypatch):
    monkeypatch.delenv("OPEN_CLANK_AGENT_SUPERVISOR_BACKEND", raising=False)
    assert selected_agent_supervisor_backend() == "acp"
    monkeypatch.setenv("OPEN_CLANK_AGENT_SUPERVISOR_BACKEND", "mimo")
    assert selected_agent_supervisor_backend() == "acp"
    monkeypatch.setenv("OPEN_CLANK_AGENT_SUPERVISOR_BACKEND", "rust")
    with pytest.raises(AgentSupervisorConfigurationError, match="disabled"):
        selected_agent_supervisor_backend()


def test_host_provider_owner_selection_retains_existing_rules():
    assert select_host_provider_owner(["Alice"], "") == "alice"
    assert select_host_provider_owner(["Alice", "Bob"], "") == ""
    assert select_host_provider_owner(["Alice", "Bob"], "BOB") == "bob"
    assert select_host_provider_owner(["Alice"], "mallory") == ""


@pytest.mark.asyncio
async def test_acp_adapter_conforms_and_preserves_runtime_and_lease_shapes():
    pool = FakePool()
    supervisor = AcpSupervisorAdapter(pool)

    assert isinstance(supervisor, AgentSupervisor)
    await supervisor.start()
    runtime = await supervisor.for_owner("alice")
    assert isinstance(runtime, AgentRuntime)
    assert runtime.generation == 7
    assert runtime.fingerprint == "fingerprint-7"
    assert runtime.bridge is pool.worker.bridge
    assert runtime.permission_handler is pool.worker.permission_handler

    events = [
        event
        async for event in runtime.run_turn(
            "session-1",
            [{"role": "user", "content": "hello"}],
            model="provider/model",
            cwd="/workspace",
            owner="alice",
            turn_envelope={"lane": "agent"},
        )
    ]
    assert events == ["data: hello\n\n", "data: [DONE]\n\n"]

    lease = await supervisor.admit_agent("alice", "provider", "model")
    assert isinstance(lease, RuntimeLease)
    assert lease.runtime is runtime
    assert lease.worker is runtime
    assert lease.generation == 7
    assert lease.fingerprint == "fingerprint-7"
    assert lease.projection_pending is True
    await lease.release(successful_terminal=True)
    await lease.release(successful_terminal=False)
    assert pool.lease.releases == [True]

    assert await runtime.managed_control_call("provider.list", {"owner": "alice"}) == {"ok": True}
    assert await runtime.managed_engine_call("provider.list", {"owner": "alice"}) == {"ok": True}
    assert await supervisor.session_request(
        "session-1", "POST", "command", owner="alice", payload={"name": "compact"},
    ) == ("session-1", "POST", "command", "alice", {"name": "compact"}, 20.0)
    await supervisor.delete_session(
        "session-1", owner="alice", runtime_session_id="runtime-1",
    )
    assert pool.worker.calls[-1] == ("delete", "session-1", "alice", "runtime-1")

    assert supervisor.readiness() == {
        "ok": True,
        "event_loop": True,
        "owner_workers": 1,
    }
    assert supervisor.mapped_sessions("alice") == {"canonical": "runtime"}
    assert supervisor.permission_handler_for("alice", "p1") == (
        "permission", "alice", "p1",
    )
    assert supervisor.question_handler_for("alice", "q1") == (
        "question", "alice", "q1",
    )
    assert supervisor.grant_store_for("alice") == ("grant-store", "alice")

    await supervisor.refresh_endpoint_projection()
    await supervisor.invalidate_owner_projection("alice")
    await supervisor.revoke_shared_access("alice", "share-1")
    await supervisor.rename_owner("alice", "alice2")
    await supervisor.purge_owner("alice2")
    memory_preview = await supervisor.preview_owner_memory("alice2")
    assert memory_preview == {"count": 2, "fingerprint": "sha256:preview"}
    assert await supervisor.reset_owner_memory(
        "alice2", expected=memory_preview,
    ) == {"complete": True, "count": 2}
    await supervisor.stop()
    assert pool.calls == [
        "start",
        "refresh",
        ("invalidate", "alice"),
        ("revoke", "alice", "share-1"),
        ("rename", "alice", "alice2"),
        ("purge", "alice2"),
        ("preview-memory", "alice2"),
        ("reset-memory", "alice2", memory_preview),
        "stop",
    ]


def test_adapter_has_no_dynamic_fallthrough_to_pool_internals():
    supervisor = AcpSupervisorAdapter(FakePool())
    assert not hasattr(supervisor, "_workers")
    assert not hasattr(supervisor, "_ensure_worker")
    assert isinstance(supervisor.worker_for_owner("alice"), AcpRuntimeAdapter)

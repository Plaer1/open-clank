from __future__ import annotations

import ast
import inspect
import json
import asyncio
from types import SimpleNamespace
from pathlib import Path

import pytest

from src.openclank.acp_supervisor_adapter import AcpSupervisorAdapter
from src.openclank.agent_supervisor import (
    AgentRuntime,
    AgentSupervisor,
    AgentSupervisorAdmissionError,
    RuntimeLease,
)
from src.openclank.agent_supervisor_factory import selected_agent_supervisor_backend
from src.openclank.agent_supervisor_factory import AgentSupervisorConfigurationError


def test_private_worker_lease_is_distinct_and_stopped(monkeypatch, tmp_path):
    from core import database, models
    from src.openclank import mimo_projection
    from src.openclank.mimo_supervisor import MimoSupervisorPool

    ghost = SimpleNamespace(id="ghost", owner="alice", incognito=True)
    monkeypatch.setattr(models, "get_session_manager_instance", lambda: SimpleNamespace(sessions={"ghost": ghost}))
    class Query:
        def filter(self, *_args): return self
        def first(self): return None
    class DB:
        def query(self, *_args): return Query()
        def close(self): pass
    monkeypatch.setattr(database, "SessionLocal", DB)
    closure = object()
    snapshot = SimpleNamespace(run_closure=lambda *_args: closure, fingerprint="projection")
    monkeypatch.setattr(mimo_projection, "build_projection_snapshot", lambda _owner: snapshot)
    monkeypatch.setattr(mimo_projection, "reconcile_projection", lambda *_args, **_kwargs: {"generation": 1})
    normal = SimpleNamespace(projection_snapshot=snapshot, installed_generation=1)
    class Worker:
        _private_runtime = True
        installed_generation = 1
        installed_fingerprint = "projection"
        stopped = False
        async def start(self): pass
        async def stop(self): self.stopped = True
        def is_alive(self): return not self.stopped
        def available_models(self): return [{"modelId": "provider/model"}]
    worker = Worker()
    pool = MimoSupervisorPool(data_dir=tmp_path)
    pool._workers["alice"] = normal
    monkeypatch.setattr(pool, "_new_worker", lambda *_args, **kwargs: worker if kwargs.get("private_runtime") else None)
    async def run():
        lease = await pool.admit_private_agent("alice", "ghost", "provider", "model")
        assert lease.worker is worker and pool.worker_for_owner("alice") is normal
        await lease.release()
        assert worker.stopped and pool.worker_for_owner("alice") is normal
        class FailingWorker(Worker):
            async def start(self): raise RuntimeError("startup failed")
            async def stop(self): raise RuntimeError("cleanup failed")
            def is_alive(self): return True
        failing = FailingWorker()
        monkeypatch.setattr(pool, "_new_worker", lambda *_args, **_kwargs: failing)
        with pytest.raises(RuntimeError, match="startup failed"):
            await pool.admit_private_agent("alice", "ghost", "provider", "model")
        assert pool._owner_state("alice").in_flight[failing] == 1
        with pytest.raises(Exception, match="private worker is already in use"):
            await pool.admit_private_agent("alice", "ghost", "provider", "model")
        assert pool.worker_for_owner("alice") is normal
    asyncio.run(run())


def test_private_dispatch_releases_after_failure_and_requires_handshake(monkeypatch):
    from src import model_dispatch
    from src.openclank.managed_protocol import client_capability_offer, validate_initialize_result, ManagedProtocolError

    with pytest.raises(ManagedProtocolError, match="private runtime contract"):
        validate_initialize_result({"_meta": {"openclankManaged": client_capability_offer()}}, private_runtime=True)
    validate_initialize_result({"_meta": {"openclankManaged": client_capability_offer(private_runtime=True)}}, private_runtime=True)
    target = SimpleNamespace(model_id="provider/model", lifecycle="persistent", capabilities={"tools": True})
    async def resolve(value, **_kwargs): return value
    monkeypatch.setattr(model_dispatch, "mimo_agent_target", resolve)
    calls = []
    class Bridge:
        async def run_turn(self, *_args, **_kwargs):
            if False: yield ""
            raise RuntimeError("private body must not appear")
    class Lease:
        worker = SimpleNamespace(bridge=Bridge())
        generation, fingerprint, projection_pending = 1, "f" * 64, False
        async def release(self, **_kwargs): calls.append("release")
    normal = object()
    class Pool:
        async def admit_agent(self, *_args): pytest.fail("ordinary worker must not be selected")
        async def admit_private_agent(self, *_args): calls.append("private"); return Lease()
        def worker_for_owner(self, _owner): return normal
    async def fail_close(_self): raise RuntimeError("cleanup body must not appear")
    monkeypatch.setattr(model_dispatch.AgentSessionLease, "close", fail_close)
    pool = Pool()
    async def run():
        return [item async for item in model_dispatch.run_agent(model_dispatch.AgentRunRequest(
            target=target, messages=[], session_id="ghost", owner="alice", supervisor=pool, turn_envelope={"incognito": True}))]
    output = asyncio.run(run())
    assert calls == ["private", "release"] and pool.worker_for_owner("alice") is normal
    assert "private body" not in "".join(output) and "cleanup body" not in "".join(output)


EXPECTED_SUPERVISOR_METHODS = {
    "start",
    "stop",
    "readiness",
    "is_alive",
    "run_sync",
    "for_owner",
    "admit_agent",
    "admit_private_agent",
    "admit_provider_control",
    "available_models",
    "provider_apis",
    "refresh_model_catalog",
    "execute_operation",
    "negotiate_session",
    "set_session_config",
    "session_request",
    "delete_session",
    "mapped_sessions",
    "permission_handler_for",
    "question_handler_for",
    "grant_store_for",
    "refresh_endpoint_projection",
    "invalidate_owner_projection",
    "revoke_shared_access",
    "rename_owner",
    "purge_owner",
    "preview_owner_memory",
    "reset_owner_memory",
}

EXPECTED_RUNTIME_METHODS = {
    "run_turn",
    "is_alive",
    "available_models",
    "provider_apis",
    "refresh_model_catalog",
    "managed_control_call",
    "negotiate_session",
    "set_session_config",
    "session_request",
    "delete_session",
    "stop",
}


def _protocol_methods(protocol: type) -> set[str]:
    return {
        name
        for name, value in vars(protocol).items()
        if callable(value) and not name.startswith("_")
    }


def test_frozen_protocol_surface_is_complete():
    assert _protocol_methods(AgentSupervisor) == EXPECTED_SUPERVISOR_METHODS
    assert _protocol_methods(AgentRuntime) == EXPECTED_RUNTIME_METHODS
    assert {"release"} <= _protocol_methods(RuntimeLease)


def test_acp_adapter_exports_every_neutral_supervisor_method():
    exported = {
        name
        for name, value in inspect.getmembers(AcpSupervisorAdapter, predicate=callable)
        if not name.startswith("_")
    }
    assert EXPECTED_SUPERVISOR_METHODS <= exported


def test_neutral_contract_has_no_backend_imports():
    source = Path("src/openclank/agent_supervisor.py").read_text()
    tree = ast.parse(source)
    forbidden = ("mimo", "acp", "subprocess", "provider")
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    assert not [name for name in imports if any(part in name.lower() for part in forbidden)]


def test_measured_application_callers_do_not_import_concrete_supervisor():
    for relative in ("src/model_dispatch.py", "routes/session_routes.py"):
        source = Path(relative).read_text()
        assert "src.openclank.mimo_supervisor" not in source
        assert "src.openclank.acp_supervisor_adapter" not in source


def test_concrete_supervisor_imports_are_confined_to_compatibility_adapter():
    allowed = {
        Path("src/openclank/mimo_supervisor.py"),
        Path("src/openclank/acp_supervisor_adapter.py"),
        Path("src/openclank/agent_supervisor_factory.py"),
    }
    offenders = []
    for root in (Path("src"), Path("routes")):
        for path in root.rglob("*.py"):
            if path in allowed:
                continue
            source = path.read_text()
            if "src.openclank.mimo_supervisor" in source or "src.openclank.acp_supervisor_adapter" in source:
                offenders.append(str(path))
    assert offenders == []


def test_backend_selection_is_server_owned_and_rust_is_disabled_by_default(monkeypatch):
    assert selected_agent_supervisor_backend("acp") == "acp"
    with pytest.raises(AgentSupervisorConfigurationError, match="unsupported"):
        selected_agent_supervisor_backend("mimo")
    monkeypatch.delenv("OPEN_CLANK_AGENT_SUPERVISOR_ENABLE_RUST", raising=False)
    with pytest.raises(AgentSupervisorConfigurationError, match="disabled"):
        selected_agent_supervisor_backend("rust")
    monkeypatch.setenv("OPEN_CLANK_AGENT_SUPERVISOR_ENABLE_RUST", "1")
    assert selected_agent_supervisor_backend("rust") == "rust"


def test_admission_error_serialization_is_secret_free_and_stable():
    error = AgentSupervisorAdmissionError(
        "STALE_GENERATION",
        "runtime generation is stale",
        phase="admission",
        retryable=False,
        status=409,
        actions=("refresh_catalog",),
        details={"generation": 4},
    )
    payload = error.as_dict()
    assert payload == {
        "code": "STALE_GENERATION",
        "error": "runtime generation is stale",
        "phase": "admission",
        "retryable": False,
        "status": 409,
        "actions": ["refresh_catalog"],
        "details": {"generation": 4},
    }
    assert not any(key in payload for key in ("token", "password", "secret", "argv"))


def test_compatibility_fixture_is_secret_free_and_covers_frozen_shapes():
    fixture = json.loads(
        Path("contracts/openclank/agent-supervisor-v1/fixtures/compatibility-v1.json").read_text()
    )
    assert fixture["schema"].endswith(".v1")
    for key in (
        "readiness",
        "owner_generation",
        "lease",
        "model_catalog",
        "session",
        "sse",
        "interactions",
        "managed_operation",
        "lifecycle",
    ):
        assert key in fixture
    serialized = json.dumps(fixture).lower()
    assert "password" not in serialized
    assert "credential" not in serialized
    assert "authorization" not in serialized

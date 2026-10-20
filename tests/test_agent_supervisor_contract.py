from __future__ import annotations

import ast
import inspect
import json
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


EXPECTED_SUPERVISOR_METHODS = {
    "start",
    "stop",
    "readiness",
    "is_alive",
    "run_sync",
    "for_owner",
    "admit_agent",
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
    assert selected_agent_supervisor_backend("mimo") == "acp"
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

"""Server-owned construction for the active agent supervisor backend."""

from __future__ import annotations

import os
from typing import Any

from src.openclank.agent_supervisor import (
    AGENT_SUPERVISOR_BACKEND_ENV,
    DEFAULT_AGENT_SUPERVISOR_BACKEND,
    AgentSupervisor,
)


class AgentSupervisorConfigurationError(RuntimeError):
    pass


def selected_agent_supervisor_backend(explicit: str | None = None) -> str:
    """Return the server-selected backend; browser input is never consulted."""

    value = str(
        explicit
        if explicit is not None
        else os.environ.get(
            AGENT_SUPERVISOR_BACKEND_ENV,
            DEFAULT_AGENT_SUPERVISOR_BACKEND,
        )
    ).strip().lower()
    # ``mimo`` is accepted only as a rollback/config compatibility spelling.
    if value == "mimo":
        return "acp"
    if value == "rust":
        if os.environ.get("OPEN_CLANK_AGENT_SUPERVISOR_ENABLE_RUST") != "1":
            raise AgentSupervisorConfigurationError(
                "rust agent supervisor backend is disabled until lifecycle admission closes"
            )
        return value
    if value != "acp":
        raise AgentSupervisorConfigurationError(
            f"unsupported Open Clank agent supervisor backend: {value or '<empty>'}"
        )
    return value


def build_agent_supervisor(
    *,
    backend: str | None = None,
    **options: Any,
) -> AgentSupervisor:
    selected = selected_agent_supervisor_backend(backend)
    if selected == "acp":
        from src.openclank.acp_supervisor_adapter import build_acp_supervisor

        return build_acp_supervisor(**options)
    if selected == "rust":
        from src.openclank.rust_supervisor_adapter import RustSupervisorAdapter

        socket_path = options.pop("socket_path", None)
        session_binding = options.pop("session_binding", None)
        if not socket_path or not session_binding:
            raise AgentSupervisorConfigurationError(
                "rust supervisor requires a private socket path and session binding"
            )
        return RustSupervisorAdapter(socket_path, session_binding, **options)
    raise AssertionError(f"unhandled agent supervisor backend: {selected}")

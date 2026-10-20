"""Provider-neutral contracts for Open Clank agent runtimes.

This module is the application-side seam for the agent supervisor migration.
It deliberately has no ACP, MiMo, subprocess, or provider-driver imports.  A
backend may be the current compatibility runtime or the future owned Rust
daemon, but application code receives the same structural contracts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Mapping
from typing import Any, Protocol, runtime_checkable


DEFAULT_AGENT_SUPERVISOR_BACKEND = "acp"
AGENT_SUPERVISOR_BACKEND_ENV = "OPEN_CLANK_AGENT_SUPERVISOR_BACKEND"


class AgentSupervisorAdmissionError(RuntimeError):
    """Secret-free, transport-stable failure at a supervisor boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str = "readiness",
        retryable: bool = True,
        status: int = 503,
        actions: tuple[str, ...] = (),
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.status = status
        self.actions = tuple(actions)
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "error": str(self),
            "phase": self.phase,
            "retryable": self.retryable,
            "status": self.status,
        }
        if self.actions:
            payload["actions"] = list(self.actions)
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@runtime_checkable
class AgentRuntime(Protocol):
    """One owner- and generation-bound provider runtime.

    The named methods are semantic capabilities.  Compatibility adapters may
    expose additional legacy attributes while callers move off backend names.
    """

    @property
    def generation(self) -> int: ...

    @property
    def fingerprint(self) -> str: ...

    def is_alive(self, owner: str | None = None) -> bool: ...

    def available_models(self, owner: str | None = None) -> list: ...

    def provider_apis(self, owner: str | None = None) -> dict[str, str]: ...

    def run_turn(
        self,
        session_id: str,
        messages: list[dict],
        *,
        model: str,
        cwd: str | None,
        owner: str | None,
        turn_envelope: Mapping[str, Any],
    ) -> AsyncIterator[str]: ...

    async def refresh_model_catalog(self, *, owner: str | None = None) -> list: ...

    async def managed_control_call(
        self,
        method: str,
        params: Mapping[str, Any],
    ) -> dict: ...

    async def negotiate_session(
        self,
        session_id: str,
        *,
        owner: str,
        cwd: str | None = None,
    ) -> dict: ...

    async def set_session_config(
        self,
        session_id: str,
        config_id: str,
        value: str,
        *,
        owner: str,
        cwd: str | None = None,
    ) -> dict: ...

    async def session_request(
        self,
        session_id: str,
        method: str,
        suffix: str,
        *,
        owner: str,
        payload: dict | None = None,
        timeout: float = 20.0,
    ) -> Any: ...

    async def delete_session(
        self,
        session_id: str,
        *,
        owner: str | None = None,
        runtime_session_id: str | None = None,
    ) -> None: ...

    async def stop(self) -> None: ...


@runtime_checkable
class RuntimeLease(Protocol):
    """A generation-fenced runtime admission with idempotent release."""

    owner: str
    runtime: AgentRuntime
    generation: int
    fingerprint: str
    projection_pending: bool

    async def release(self, *, successful_terminal: bool = False) -> None: ...


@runtime_checkable
class AgentSupervisor(Protocol):
    """Application-consumed supervisor surface frozen for the migration."""

    backend_name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def readiness(self) -> dict[str, object]: ...

    def is_alive(self, owner: str | None = None) -> bool: ...

    def run_sync(self, awaitable: Awaitable[Any], *, timeout: float | None = None) -> Any: ...

    async def for_owner(self, owner: str | None) -> AgentRuntime: ...

    async def admit_agent(
        self,
        owner: str | None,
        provider_id: str,
        model_id: str,
    ) -> RuntimeLease: ...

    async def admit_provider_control(self, owner: str | None) -> RuntimeLease: ...

    def available_models(self, owner: str | None = None) -> list: ...

    def provider_apis(self, owner: str | None = None) -> dict[str, str]: ...

    async def refresh_model_catalog(self, *, owner: str | None = None) -> list: ...

    async def execute_operation(self, owner: str | None, payload: dict) -> dict: ...

    async def negotiate_session(
        self,
        session_id: str,
        *,
        owner: str,
        cwd: str | None = None,
    ) -> dict: ...

    async def set_session_config(
        self,
        session_id: str,
        config_id: str,
        value: str,
        *,
        owner: str,
        cwd: str | None = None,
    ) -> dict: ...

    async def session_request(
        self,
        session_id: str,
        method: str,
        suffix: str,
        *,
        owner: str,
        payload: dict | None = None,
        timeout: float = 20.0,
    ) -> Any: ...

    async def delete_session(
        self,
        session_id: str,
        *,
        owner: str | None = None,
        runtime_session_id: str | None = None,
    ) -> None: ...

    def mapped_sessions(self, owner: str | None = None) -> dict[str, str]: ...

    def permission_handler_for(
        self,
        owner: str | None,
        request_id: str | None = None,
    ) -> Any: ...

    def question_handler_for(
        self,
        owner: str | None,
        request_id: str | None = None,
    ) -> Any: ...

    def grant_store_for(self, owner: str | None) -> Any: ...

    async def refresh_endpoint_projection(self) -> None: ...

    async def invalidate_owner_projection(self, owner: str) -> None: ...

    async def revoke_shared_access(self, actor_owner: str, share_id: str) -> None: ...

    async def rename_owner(self, old_owner: str, new_owner: str) -> None: ...

    async def purge_owner(self, owner: str) -> None: ...

    async def preview_owner_memory(self, owner: str) -> dict[str, object]: ...

    async def reset_owner_memory(
        self,
        owner: str,
        *,
        expected: dict[str, object],
    ) -> dict[str, object]: ...


def select_host_provider_owner(
    admin_owners: list[str],
    explicit_owner: str = "",
) -> str:
    """Select the one server-owned account allowed to expose host providers."""

    admins = {
        str(owner).strip().lower()
        for owner in admin_owners
        if str(owner).strip()
    }
    explicit = explicit_owner.strip().lower()
    if explicit:
        return explicit if explicit in admins else ""
    return next(iter(admins)) if len(admins) == 1 else ""

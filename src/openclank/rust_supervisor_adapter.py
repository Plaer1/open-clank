"""Disabled-by-default health adapter for the S01 Rust supervisor."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from src.openclank.agent_supervisor import AgentSupervisorAdmissionError
from src.openclank.rust_supervisor_client import RustSupervisorClient
from src.openclank.history_capture import declared_root_baseline


class RustSupervisorAdapter:
    backend_name = "rust"

    def __init__(self, socket_path: str, session_binding: str, **options: Any) -> None:
        self.client = RustSupervisorClient(socket_path, session_binding)
        configured_roots = options.get("declared_roots") or options.get("workspace_roots") or ()
        self._declared_roots = tuple(str(root) for root in configured_roots if str(root).strip())
        self._baseline_before: dict[str, Any] | None = None
        self._health: dict[str, Any] = {
            "ok": False,
            "transport": False,
            "protocol": False,
            "containment": False,
            "runtime_actor": False,
            "driver": False,
            "tool_policy": False,
            "ready": False,
        }

    async def start(self) -> None:
        self._baseline_before = declared_root_baseline(self._declared_roots) if self._declared_roots else None
        self._health = await self.client.health()
        self._health["ok"] = True

    async def stop(self) -> None:
        if self._baseline_before is not None:
            self._health["history_capture"] = {
                "status": "partial",
                "durable": False,
                "coverage": "DeclaredRootsBaseline",
                "before": self._baseline_before,
                "after": declared_root_baseline(self._declared_roots),
            }
        await self.client.close()

    def readiness(self) -> dict[str, object]:
        return dict(self._health)

    def is_alive(self, owner: str | None = None) -> bool:
        del owner
        return bool(self._health.get("transport") and self._health.get("protocol"))

    def run_sync(self, awaitable: Awaitable[Any], *, timeout: float | None = None) -> Any:
        return asyncio.run(asyncio.wait_for(awaitable, timeout)) if timeout else asyncio.run(awaitable)

    def _disabled(self) -> AgentSupervisorAdmissionError:
        return AgentSupervisorAdmissionError(
            "RUST_TURNS_DISABLED",
            "The Rust supervisor is health-only until S01 lifecycle admission closes",
            phase="driver",
            retryable=False,
            status=503,
        )

    async def for_owner(self, owner: str | None) -> Any:
        del owner
        raise self._disabled()

    async def admit_agent(self, owner: str | None, provider_id: str, model_id: str) -> Any:
        del owner, provider_id, model_id
        raise self._disabled()

    async def admit_provider_control(self, owner: str | None) -> Any:
        del owner
        raise self._disabled()

    def available_models(self, owner: str | None = None) -> list:
        del owner
        return []

    def provider_apis(self, owner: str | None = None) -> dict[str, str]:
        del owner
        return {}

    async def refresh_model_catalog(self, *, owner: str | None = None) -> list:
        del owner
        return []

    async def execute_operation(self, owner: str | None, payload: dict) -> dict:
        del owner, payload
        raise self._disabled()

    async def negotiate_session(self, session_id: str, *, owner: str, cwd: str | None = None) -> dict:
        del session_id, owner, cwd
        raise self._disabled()

    async def set_session_config(self, session_id: str, config_id: str, value: str, *, owner: str, cwd: str | None = None) -> dict:
        del session_id, config_id, value, owner, cwd
        raise self._disabled()

    async def session_request(self, session_id: str, method: str, suffix: str, *, owner: str, payload: dict | None = None, timeout: float = 20.0) -> Any:
        del session_id, method, suffix, owner, payload, timeout
        raise self._disabled()

    async def delete_session(self, session_id: str, *, owner: str | None = None, runtime_session_id: str | None = None) -> None:
        del session_id, owner, runtime_session_id
        raise self._disabled()

    def mapped_sessions(self, owner: str | None = None) -> dict[str, str]:
        del owner
        return {}

    def permission_handler_for(self, owner: str | None, request_id: str | None = None) -> Any:
        del owner, request_id
        raise self._disabled()

    def question_handler_for(self, owner: str | None, request_id: str | None = None) -> Any:
        del owner, request_id
        raise self._disabled()

    def grant_store_for(self, owner: str | None) -> Any:
        del owner
        raise self._disabled()

    async def refresh_endpoint_projection(self) -> None:
        raise self._disabled()

    async def invalidate_owner_projection(self, owner: str) -> None:
        del owner
        raise self._disabled()

    async def revoke_shared_access(self, actor_owner: str, share_id: str) -> None:
        del actor_owner, share_id
        raise self._disabled()

    async def rename_owner(self, old_owner: str, new_owner: str) -> None:
        del old_owner, new_owner
        raise self._disabled()

    async def purge_owner(self, owner: str) -> None:
        del owner
        raise self._disabled()

    async def preview_owner_memory(self, owner: str) -> dict[str, object]:
        del owner
        raise self._disabled()

    async def reset_owner_memory(
        self,
        owner: str,
        *,
        expected: dict[str, object],
    ) -> dict[str, object]:
        del owner, expected
        raise self._disabled()

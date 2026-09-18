"""Compatibility adapter from the current ACP/MiMo runtime to neutral contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Mapping
from typing import Any


class AcpRuntimeAdapter:
    """Project one current worker as a provider-neutral runtime."""

    backend_name = "acp"

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    @property
    def generation(self) -> int:
        return int(getattr(self._runtime, "installed_generation", 0))

    @property
    def fingerprint(self) -> str:
        return str(getattr(self._runtime, "installed_fingerprint", ""))

    @property
    def bridge(self) -> Any:
        """Temporary compatibility projection for pre-facade turn callers."""

        return self._runtime.bridge

    @property
    def permission_handler(self) -> Any:
        return self._runtime.permission_handler

    @property
    def question_handler(self) -> Any:
        return self._runtime.question_handler

    @property
    def grant_store(self) -> Any:
        return self._runtime.grant_store

    @property
    def http_base_url(self) -> str:
        return self._runtime.http_base_url

    def internal_http_client(self, *, timeout: float = 20.0) -> Any:
        return self._runtime.internal_http_client(timeout=timeout)

    def is_alive(self, owner: str | None = None) -> bool:
        return self._runtime.is_alive(owner=owner)

    def available_models(self, owner: str | None = None) -> list:
        return self._runtime.available_models(owner=owner)

    def provider_apis(self, owner: str | None = None) -> dict[str, str]:
        return self._runtime.provider_apis(owner=owner)

    def run_turn(
        self,
        session_id: str,
        messages: list[dict],
        *,
        model: str,
        cwd: str | None,
        owner: str | None,
        turn_envelope: Mapping[str, Any],
    ) -> AsyncIterator[str]:
        return self._runtime.bridge.run_turn(
            session_id,
            messages,
            model=model,
            cwd=cwd,
            owner=owner,
            turn_envelope=dict(turn_envelope),
        )

    async def refresh_model_catalog(self, *, owner: str | None = None) -> list:
        return await self._runtime.refresh_model_catalog(owner=owner)

    async def managed_control_call(
        self,
        method: str,
        params: Mapping[str, Any],
    ) -> dict:
        return await self._runtime.managed_engine_call(method, dict(params))

    async def managed_engine_call(self, method: str, params: dict) -> dict:
        """Temporary compatibility name; use ``managed_control_call``."""

        return await self.managed_control_call(method, params)

    async def execute_operation(self, payload: dict) -> dict:
        return await self._runtime.execute_operation(payload)

    async def negotiate_session(
        self,
        session_id: str,
        *,
        owner: str,
        cwd: str | None = None,
    ) -> dict:
        return await self._runtime.negotiate_session(
            session_id,
            owner=owner,
            cwd=cwd,
        )

    async def set_session_config(
        self,
        session_id: str,
        config_id: str,
        value: str,
        *,
        owner: str,
        cwd: str | None = None,
    ) -> dict:
        return await self._runtime.set_session_config(
            session_id,
            config_id,
            value,
            owner=owner,
            cwd=cwd,
        )

    async def session_request(
        self,
        session_id: str,
        method: str,
        suffix: str,
        *,
        owner: str,
        payload: dict | None = None,
        timeout: float = 20.0,
    ) -> Any:
        return await self._runtime.session_http_request(
            session_id,
            method,
            suffix,
            owner=owner,
            payload=payload,
            timeout=timeout,
        )

    async def session_http_request(
        self,
        session_id: str,
        method: str,
        suffix: str,
        *,
        owner: str,
        payload: dict | None = None,
        timeout: float = 20.0,
    ) -> Any:
        """Temporary compatibility name; use ``session_request``."""

        return await self.session_request(
            session_id,
            method,
            suffix,
            owner=owner,
            payload=payload,
            timeout=timeout,
        )

    async def delete_session(
        self,
        session_id: str,
        *,
        owner: str | None = None,
        runtime_session_id: str | None = None,
        mimo_session_id: str | None = None,
    ) -> None:
        # The old keyword remains accepted only on this compatibility adapter.
        mapped_session_id = runtime_session_id or mimo_session_id
        await self._runtime.delete_session(
            session_id,
            owner=owner,
            mimo_session_id=mapped_session_id,
        )

    async def stop(self) -> None:
        await self._runtime.stop()


class AcpRuntimeLease:
    """Neutral generation lease over the current pool lease."""

    def __init__(self, lease: Any, runtime: AcpRuntimeAdapter) -> None:
        self._lease = lease
        self._released = False
        self.owner = str(lease.owner)
        self.runtime = runtime
        # Compatibility for callers not yet migrated to the neutral noun.
        self.worker = runtime
        self.generation = int(lease.generation)
        self.fingerprint = str(lease.fingerprint)
        self.projection_pending = bool(lease.projection_pending)

    async def release(self, *, successful_terminal: bool = False) -> None:
        if self._released:
            return
        self._released = True
        await self._lease.release(successful_terminal=successful_terminal)

    async def __aenter__(self) -> "AcpRuntimeLease":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release(successful_terminal=exc_type is None)


class AcpSupervisorAdapter:
    """Behavior-preserving facade over ``MimoSupervisorPool``."""

    backend_name = "acp"

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._runtime_adapters: dict[int, AcpRuntimeAdapter] = {}

    def _runtime(self, runtime: Any | None) -> AcpRuntimeAdapter | None:
        if runtime is None:
            return None
        key = id(runtime)
        adapter = self._runtime_adapters.get(key)
        if adapter is None or adapter._runtime is not runtime:
            adapter = AcpRuntimeAdapter(runtime)
            self._runtime_adapters[key] = adapter
        return adapter

    async def start(self) -> None:
        await self._pool.start()

    async def stop(self) -> None:
        try:
            await self._pool.stop()
        finally:
            self._runtime_adapters.clear()

    def readiness(self) -> dict[str, object]:
        return self._pool.readiness()

    def is_alive(self, owner: str | None = None) -> bool:
        return self._pool.is_alive(owner=owner)

    def run_sync(self, awaitable: Awaitable[Any], *, timeout: float | None = None) -> Any:
        return self._pool.run_sync(awaitable, timeout=timeout)

    async def for_owner(self, owner: str | None) -> AcpRuntimeAdapter:
        runtime = self._runtime(await self._pool.for_owner(owner))
        assert runtime is not None
        return runtime

    async def admit_agent(
        self,
        owner: str | None,
        provider_id: str,
        model_id: str,
    ) -> AcpRuntimeLease:
        lease = await self._pool.admit_agent(owner, provider_id, model_id)
        runtime = self._runtime(lease.worker)
        assert runtime is not None
        return AcpRuntimeLease(lease, runtime)

    async def admit_provider_control(self, owner: str | None) -> AcpRuntimeLease:
        lease = await self._pool.admit_provider_control(owner)
        runtime = self._runtime(lease.worker)
        assert runtime is not None
        return AcpRuntimeLease(lease, runtime)

    async def admit_shared_agent(
        self,
        access: Any,
        provider_id: str,
        model_id: str,
    ) -> AcpRuntimeLease:
        """Temporary compatibility path retained to preserve its fail-closed error."""

        lease = await self._pool.admit_shared_agent(access, provider_id, model_id)
        runtime = self._runtime(lease.worker)
        assert runtime is not None
        return AcpRuntimeLease(lease, runtime)

    def available_models(self, owner: str | None = None) -> list:
        return self._pool.available_models(owner=owner)

    def provider_apis(self, owner: str | None = None) -> dict[str, str]:
        return self._pool.provider_apis(owner=owner)

    async def refresh_model_catalog(self, *, owner: str | None = None) -> list:
        return await self._pool.refresh_model_catalog(owner=owner)

    async def execute_operation(self, owner: str | None, payload: dict) -> dict:
        return await self._pool.execute_operation(owner, payload)

    async def negotiate_session(
        self,
        session_id: str,
        *,
        owner: str,
        cwd: str | None = None,
    ) -> dict:
        return await self._pool.negotiate_session(session_id, owner=owner, cwd=cwd)

    async def set_session_config(
        self,
        session_id: str,
        config_id: str,
        value: str,
        *,
        owner: str,
        cwd: str | None = None,
    ) -> dict:
        return await self._pool.set_session_config(
            session_id,
            config_id,
            value,
            owner=owner,
            cwd=cwd,
        )

    async def session_request(
        self,
        session_id: str,
        method: str,
        suffix: str,
        *,
        owner: str,
        payload: dict | None = None,
        timeout: float = 20.0,
    ) -> Any:
        return await self._pool.session_http_request(
            session_id,
            method,
            suffix,
            owner=owner,
            payload=payload,
            timeout=timeout,
        )

    async def session_http_request(
        self,
        session_id: str,
        method: str,
        suffix: str,
        *,
        owner: str,
        payload: dict | None = None,
        timeout: float = 20.0,
    ) -> Any:
        """Temporary compatibility name; use ``session_request``."""

        return await self.session_request(
            session_id,
            method,
            suffix,
            owner=owner,
            payload=payload,
            timeout=timeout,
        )

    async def delete_session(
        self,
        session_id: str,
        *,
        owner: str | None = None,
        runtime_session_id: str | None = None,
        mimo_session_id: str | None = None,
    ) -> None:
        await self._pool.delete_session(
            session_id,
            owner=owner,
            mimo_session_id=runtime_session_id or mimo_session_id,
        )

    def mapped_sessions(self, owner: str | None = None) -> dict[str, str]:
        return self._pool.mapped_sessions(owner=owner)

    def permission_handler_for(
        self,
        owner: str | None,
        request_id: str | None = None,
    ) -> Any:
        return self._pool.permission_handler_for(owner, request_id=request_id)

    def question_handler_for(
        self,
        owner: str | None,
        request_id: str | None = None,
    ) -> Any:
        return self._pool.question_handler_for(owner, request_id=request_id)

    def grant_store_for(self, owner: str | None) -> Any:
        return self._pool.grant_store_for(owner)

    async def refresh_endpoint_projection(self) -> None:
        await self._pool.refresh_endpoint_projection()

    async def invalidate_owner_projection(self, owner: str) -> None:
        await self._pool.invalidate_owner_projection(owner)

    async def revoke_shared_access(self, actor_owner: str, share_id: str) -> None:
        await self._pool.revoke_shared_access(actor_owner, share_id)

    async def rename_owner(self, old_owner: str, new_owner: str) -> None:
        await self._pool.rename_owner(old_owner, new_owner)

    async def purge_owner(self, owner: str) -> None:
        await self._pool.purge_owner(owner)

    async def preview_owner_memory(self, owner: str) -> dict[str, object]:
        """Preview the owner's runtime-authored memory through the facade."""

        return await self._pool.preview_owner_memory(owner)

    async def reset_owner_memory(
        self,
        owner: str,
        *,
        expected: dict[str, object],
    ) -> dict[str, object]:
        """Fence the owner runtime and clear only its authored-memory cache."""

        return await self._pool.reset_owner_memory(owner, expected=expected)

    def worker_for_owner(self, owner: str | None) -> AcpRuntimeAdapter | None:
        """Temporary compatibility lookup for measured legacy callers/tests."""

        return self._runtime(self._pool.worker_for_owner(owner))

    @property
    def bridge(self) -> Any:
        return self._pool.bridge

    @property
    def permission_handler(self) -> Any:
        return self._pool.permission_handler

    @property
    def http_base_url(self) -> str:
        return self._pool.http_base_url


def build_acp_supervisor(**options: Any) -> AcpSupervisorAdapter:
    """Build the current backend without leaking its concrete pool to callers."""

    from src.openclank.mimo_supervisor import MimoSupervisorPool

    return AcpSupervisorAdapter(MimoSupervisorPool(**options))

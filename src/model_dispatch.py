"""Transport-safe model execution shared by chat and auxiliary callers."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Iterable, Optional

from fastapi import HTTPException

from src.endpoint_resolver import ResolvedModelTarget, resolve_model_target

logger = logging.getLogger(__name__)

_mimo_supervisor: Any = None


@dataclass(frozen=True)
class AgentRunRequest:
    target: ResolvedModelTarget
    messages: list[dict]
    session_id: str
    owner: Optional[str] = None
    cwd: Optional[str] = None
    supervisor: Any = None
    turn_envelope: Optional[dict] = None


@dataclass(frozen=True)
class AuxiliaryRequest:
    purpose: str
    target: ResolvedModelTarget
    messages: list[dict]
    session_id: Optional[str] = None
    owner: Optional[str] = None
    cwd: Optional[str] = None
    supervisor: Any = None
    timeout: Optional[float] = None
    options: dict[str, Any] = field(default_factory=dict)


class AgentSessionLease:
    """Delete one ephemeral MiMo session exactly once on every terminal path."""

    def __init__(self, worker, session_id: str, *, ephemeral: bool):
        self.worker = worker
        self.session_id = session_id
        self.ephemeral = ephemeral
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self.ephemeral:
            return
        try:
            await self.worker.delete_session(self.session_id)
        except Exception as exc:
            logger.debug("MiMo ephemeral session cleanup %s: %s", self.session_id, exc)


def _typed_error_sse(exc: Exception) -> tuple[str, str]:
    if hasattr(exc, "as_dict"):
        payload = exc.as_dict()
        status = int(payload.pop("status", getattr(exc, "status", 503)))
    elif isinstance(exc, HTTPException):
        payload = {
            "code": "AGENT_REQUEST_REJECTED",
            "error": str(exc.detail),
            "phase": "admission",
            "retryable": exc.status_code >= 500,
        }
        status = exc.status_code
    else:
        payload = {
            "code": "AGENT_INTERNAL_ERROR",
            "error": "Agent failed before completion.",
            "phase": "internal",
            "retryable": True,
        }
        status = 500
    return (
        f"event: error\ndata: {json.dumps({**payload, 'status': status})}\n\n",
        "data: [DONE]\n\n",
    )


def _agent_turn_envelope(
    messages: list[dict],
    cwd: Optional[str],
    supplied: Optional[dict],
    options: dict[str, Any],
) -> dict:
    """Normalize legacy caller kwargs once at the structural Agent door."""
    envelope = dict(supplied or {})
    if not envelope.get("system_prompt"):
        system_parts = [
            str(message.get("content") or "").strip()
            for message in messages
            if message.get("role") == "system" and str(message.get("content") or "").strip()
        ]
        if system_parts:
            envelope["system_prompt"] = "\n\n".join(system_parts)
    envelope.setdefault("workspace", cwd or "")
    if options.get("disabled_tools") is not None:
        envelope["disabled_tools"] = sorted({
            *map(str, envelope.get("disabled_tools") or []),
            *map(str, options.get("disabled_tools") or []),
        })
    relevant = options.get("relevant_tools")
    if relevant is not None and envelope.get("allowed_tools") is None:
        envelope["allowed_tools"] = sorted(map(str, relevant))
    if options.get("max_tool_calls") is not None:
        envelope["max_tool_calls"] = max(0, int(options.get("max_tool_calls") or 0))
    if options.get("plan_mode"):
        envelope["mode"] = "plan"
    envelope.setdefault(
        "interaction_policy",
        "fail_on_interaction" if options.get("workload") == "background" else "interactive",
    )
    return envelope


def set_mimo_supervisor(supervisor: Any) -> None:
    global _mimo_supervisor
    _mimo_supervisor = supervisor


def get_mimo_supervisor() -> Any:
    return _mimo_supervisor


async def mimo_agent_target(
    target: ResolvedModelTarget,
    *,
    owner: Optional[str] = None,
    supervisor: Any = None,
) -> ResolvedModelTarget:
    """Resolve strict Agent identity by persisted endpoint ID, never by URL."""
    if target is None:
        return None
    if target.transport == "acp":
        return target
    pool = supervisor or _mimo_supervisor
    if pool is None:
        from src.openclank.mimo_supervisor import SupervisorAdmissionError
        raise SupervisorAdmissionError("SUPERVISOR_UNAVAILABLE", "MiMo Agent runtime is unavailable")

    from core.database import ModelEndpoint, SessionLocal
    from src.auth_helpers import owner_filter
    from src.model_capabilities import endpoint_capability_states
    from src.openclank.mimo_supervisor import ENDPOINT_PROVIDER_PREFIX, SupervisorAdmissionError

    endpoint_id = (target.endpoint_id or "").strip()
    if not endpoint_id:
        raise SupervisorAdmissionError(
            "UNREGISTERED_ENDPOINT",
            "This legacy session has no unambiguous persisted endpoint identity",
            phase="routing", retryable=False,
        )
    db = SessionLocal()
    try:
        query = db.query(ModelEndpoint).filter(
            ModelEndpoint.id == endpoint_id,
            ModelEndpoint.is_enabled == True,  # noqa: E712
        )
        query = owner_filter(query, ModelEndpoint, owner or "")
        endpoint = query.first()
        if endpoint is None:
            raise SupervisorAdmissionError(
                "ENDPOINT_NOT_PROJECTABLE",
                "The selected endpoint is missing, disabled, or not visible to this owner",
                phase="routing", retryable=False,
            )
        capability = next(
            (
                item
                for item in endpoint_capability_states(db, endpoint)
                if item["model_id"] == target.model_id
            ),
            None,
        )
        tools_enabled = capability.get("tools_enabled", True) if capability else True
    finally:
        db.close()
    provider_id = f"{ENDPOINT_PROVIDER_PREFIX}{endpoint_id}"
    return resolve_model_target(
        "mimo://acp",
        f"{provider_id}/{target.model_id}",
        endpoint_id=endpoint_id,
        provider_id=provider_id,
        capabilities={"tools": tools_enabled, "auxiliary": False},
        lifecycle=target.lifecycle,
    )


async def _supervisor(explicit: Any = None, *, owner: Optional[str] = None) -> Any:
    supervisor = explicit or _mimo_supervisor
    if supervisor and hasattr(supervisor, "for_owner"):
        try:
            supervisor = await supervisor.for_owner(owner)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
    if not supervisor or not supervisor.is_alive() or not supervisor.bridge:
        raise HTTPException(503, "MiMo ACP is unavailable")
    return supervisor


def _validated_candidates(
    primary: ResolvedModelTarget,
    fallbacks: Iterable[tuple[str, str, dict]] = (),
) -> list[tuple[str, str, dict]]:
    candidates = [(primary.endpoint_url, primary.model_id, dict(primary.headers))]
    for url, model, headers in fallbacks:
        target = resolve_model_target(url, model, headers)
        candidates.append((target.endpoint_url, target.model_id, dict(target.headers)))
    return candidates


async def stream_chat_target(
    target: ResolvedModelTarget,
    messages: list[dict],
    *,
    session_id: str,
    owner: Optional[str] = None,
    cwd: Optional[str] = None,
    supervisor: Any = None,
    fallbacks: Iterable[tuple[str, str, dict]] = (),
    **kwargs: Any,
) -> AsyncGenerator[str, None]:
    if target.transport == "acp":
        try:
            sup = await _supervisor(supervisor, owner=owner)
            bridge = sup.bridge
        except HTTPException as exc:
            yield f'event: error\ndata: {json.dumps({"error": exc.detail, "status": exc.status_code})}\n\n'
            yield "data: [DONE]\n\n"
            return
        try:
            envelope = dict(kwargs.get("turn_envelope") or {})
            envelope.update({"lane": "auxiliary", "incognito": True})
            async for chunk in bridge.run_turn(
                session_id,
                messages,
                model=target.model_id,
                cwd=None,
                owner=owner,
                turn_envelope=envelope,
            ):
                yield chunk
        finally:
            try:
                await sup.delete_session(session_id)
            except Exception as exc:
                logger.debug("Failed to clean up auxiliary MiMo stream %s: %s", session_id, exc)
        return

    from src.llm_core import stream_llm_with_fallback

    # ACP-only concepts stop at the transport fork: the HTTP leg's stream_llm
    # has no turn envelope and must not receive one.
    kwargs.pop("turn_envelope", None)
    async for chunk in stream_llm_with_fallback(
        _validated_candidates(target, fallbacks),
        messages,
        owner=owner,
        cwd=cwd,
        supervisor=supervisor,
        **kwargs,
    ):
        yield chunk


async def stream_agent_target(
    target: ResolvedModelTarget,
    messages: list[dict],
    *,
    session_id: str,
    owner: Optional[str] = None,
    cwd: Optional[str] = None,
    supervisor: Any = None,
    fallbacks: Iterable[tuple[str, str, dict]] = (),
    **kwargs: Any,
) -> AsyncGenerator[str, None]:
    turn_envelope = _agent_turn_envelope(
        messages, cwd, kwargs.pop("turn_envelope", None), kwargs,
    )
    request = AgentRunRequest(
        target=target,
        messages=messages,
        session_id=session_id,
        owner=owner,
        cwd=cwd,
        supervisor=supervisor,
        turn_envelope=turn_envelope,
    )
    async for chunk in run_agent(request):
        yield chunk


async def run_agent(request: AgentRunRequest) -> AsyncGenerator[str, None]:
    """The only strict tool-bearing application door."""
    pool = request.supervisor or _mimo_supervisor
    if pool is None or not callable(getattr(pool, "admit_agent", None)):
        from src.openclank.mimo_supervisor import SupervisorAdmissionError
        exc = SupervisorAdmissionError(
            "SUPERVISOR_UNAVAILABLE", "The strict MiMo Agent coordinator is unavailable"
        )
        for event in _typed_error_sse(exc):
            yield event
        return

    try:
        target = await mimo_agent_target(
            request.target,
            owner=request.owner,
            supervisor=pool,
        )
    except Exception as exc:
        if not hasattr(exc, "as_dict") and not isinstance(exc, HTTPException):
            logger.exception("Unexpected strict Agent admission failure")
        for event in _typed_error_sse(exc):
            yield event
        return

    qualified = target.model_id.split("/", 1)
    if len(qualified) != 2:
        from src.openclank.mimo_supervisor import SupervisorAdmissionError
        exc = SupervisorAdmissionError(
            "MODEL_NOT_PROJECTED", "MiMo Agent models must retain provider identity",
            phase="routing", retryable=False,
        )
        for event in _typed_error_sse(exc):
            yield event
        return
    provider_id, model_id = qualified
    worker_lease = None
    session_lease = None
    successful_terminal = False
    try:
        worker_lease = await pool.admit_agent(request.owner, provider_id, model_id)
        worker = worker_lease.worker
        incognito = bool((request.turn_envelope or {}).get("incognito"))
        session_lease = AgentSessionLease(
            worker,
            request.session_id,
            # The bridge owns exact-id cleanup for Temporary Agent sessions.
            ephemeral=target.lifecycle == "ephemeral" and not incognito,
        )
        yield f'data: {json.dumps({"type": "projection", "data": {"generation": worker_lease.generation, "fingerprint": worker_lease.fingerprint[:12], "projection_pending": worker_lease.projection_pending}})}\n\n'
        envelope = dict(request.turn_envelope or {})
        envelope["lane"] = "agent"
        if target.capabilities.get("tools") is False:
            envelope["allowed_tools"] = []
        async for chunk in worker.bridge.run_turn(
            request.session_id,
            request.messages,
            model=target.model_id,
            cwd=request.cwd,
            owner=request.owner,
            turn_envelope=envelope,
        ):
            if chunk.strip() == "data: [DONE]":
                successful_terminal = True
            yield chunk
    except Exception as exc:
        if not hasattr(exc, "as_dict") and not isinstance(exc, HTTPException):
            logger.exception("Unexpected strict Agent runtime failure")
        for event in _typed_error_sse(exc):
            yield event
    finally:
        if session_lease is not None:
            await session_lease.close()
        if worker_lease is not None:
            await worker_lease.release(successful_terminal=successful_terminal)


async def call_model_target(
    target: ResolvedModelTarget,
    messages: list[dict],
    *,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    cwd: Optional[str] = None,
    supervisor: Any = None,
    **kwargs: Any,
) -> str:
    """Compatibility wrapper; named auxiliaries have no Agent authority."""
    purpose = str(kwargs.pop("purpose", "legacy_auxiliary"))
    return await run_auxiliary_inference(AuxiliaryRequest(
        purpose=purpose,
        target=target,
        messages=messages,
        session_id=session_id,
        owner=owner,
        cwd=cwd,
        supervisor=supervisor,
        options=kwargs,
    ))


async def run_auxiliary_inference(request: AuxiliaryRequest) -> str:
    """Named, structurally tool-free inference; it can never rescue Agent."""
    if not request.purpose.strip():
        raise ValueError("Auxiliary inference requires a named product purpose")
    target = request.target
    if target.transport == "http":
        from src.llm_core import llm_call_async

        options = dict(request.options)
        if request.timeout is not None:
            options.setdefault("timeout", request.timeout)
        return await llm_call_async(
            target.endpoint_url,
            target.model_id,
            request.messages,
            headers=dict(target.headers),
            session_id=request.session_id,
            owner=request.owner,
            cwd=None,
            _transport_checked=True,
            **options,
        )

    sup = await _supervisor(request.supervisor, owner=request.owner)
    turn_session = request.session_id or f"aux-{request.purpose}-{uuid.uuid4().hex}"
    parts: list[str] = []
    error: Optional[tuple[int, str]] = None
    lease = AgentSessionLease(sup, turn_session, ephemeral=True)
    try:
        async for chunk in sup.bridge.run_turn(
            turn_session,
            request.messages,
            model=target.model_id,
            cwd=None,
            owner=request.owner,
            turn_envelope={"lane": "auxiliary", "purpose": request.purpose, "incognito": True},
        ):
            event = "message"
            for line in chunk.splitlines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event == "error" or data.get("error"):
                    error = (
                        int(data.get("status") or 502),
                        str(data.get("error") or data.get("text") or "MiMo ACP call failed"),
                    )
                    continue
                if isinstance(data.get("delta"), str) and not data.get("thinking"):
                    parts.append(data["delta"])
        if error:
            raise HTTPException(*error)
        return "".join(parts)
    finally:
        await lease.close()

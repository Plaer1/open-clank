"""Transport-safe model execution shared by chat and auxiliary callers."""

from __future__ import annotations

import json
import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Iterable, Optional

from fastapi import HTTPException

from src.endpoint_resolver import (
    ResolvedModelTarget,
    resolve_model_target,
)
from src.openclank.agent_supervisor import (
    AgentSupervisorAdmissionError as SupervisorAdmissionError,
)

logger = logging.getLogger(__name__)

_agent_supervisor: Any = None


@dataclass(frozen=True)
class AgentRunRequest:
    target: ResolvedModelTarget
    messages: list[dict]
    session_id: str
    owner: Optional[str] = None
    cwd: Optional[str] = None
    supervisor: Any = None
    turn_envelope: Optional[dict] = None
    execution_proof_callback: Any = None
    foreground_account_context: Any = None
    direct_settlement_callback: Any = None
    direct_account_only: bool = False
    provider_policy_selection: Any = None
    provider_policy_intent: Any = None


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
    """Delete one ephemeral Open Clank agent session exactly once on every terminal path."""

    def __init__(self, worker, session_id: str, *, ephemeral: bool):
        self.worker = worker
        self.session_id = session_id
        self.ephemeral = ephemeral
        self._closed = False
        self.cleanup_failed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self.ephemeral:
            return
        try:
            await self.worker.delete_session(self.session_id)
        except Exception as exc:
            self.cleanup_failed = True
            logger.debug("Open Clank agent ephemeral session cleanup %s: %s", self.session_id, exc)


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


def set_agent_supervisor(supervisor: Any) -> None:
    """Set the server-owned neutral supervisor used by model dispatch."""

    global _agent_supervisor
    _agent_supervisor = supervisor


def get_agent_supervisor() -> Any:
    return _agent_supervisor


def set_mimo_supervisor(supervisor: Any) -> None:
    """Compatibility alias for the pre-facade application bootstrap."""

    set_agent_supervisor(supervisor)


def get_mimo_supervisor() -> Any:
    """Compatibility alias for legacy callers during the strangler phase."""

    return get_agent_supervisor()


async def mimo_agent_target(
    target: ResolvedModelTarget,
    *,
    owner: Optional[str] = None,
    supervisor: Any = None,
) -> ResolvedModelTarget:
    """Reject every route that did not enter through normalized managed ACP."""
    if target is None:
        return None
    if target.transport == "acp":
        connection_id, separator, model_id = str(target.model_id or "").partition("/")
        if (
            not separator
            or not connection_id
            or not model_id
            or target.provider_id != connection_id
            or target.endpoint_id != connection_id
            or target.headers
        ):
            raise SupervisorAdmissionError(
                "INVALID_MANAGED_ROUTE",
                "The selected model did not retain its normalized connection identity",
                phase="routing",
                retryable=False,
            )
        return target
    raise SupervisorAdmissionError(
        "LEGACY_PROVIDER_ROUTE_RETIRED",
        "Direct provider dispatch is unavailable; select a model from the provider catalogue",
        phase="routing",
        retryable=False,
    )


async def _supervisor(explicit: Any = None, *, owner: Optional[str] = None) -> Any:
    supervisor = explicit or _agent_supervisor
    if supervisor and hasattr(supervisor, "for_owner"):
        try:
            supervisor = await supervisor.for_owner(owner)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
    if not supervisor or not supervisor.is_alive() or not supervisor.bridge:
        raise HTTPException(503, "Open Clank agent ACP is unavailable")
    return supervisor


async def _acp_turn_worker(
    target: ResolvedModelTarget,
    *,
    owner: Optional[str],
    supervisor: Any,
):
    """Return the recipient-bound managed worker for one normalized ACP turn."""
    target = await mimo_agent_target(target, owner=owner, supervisor=supervisor)
    worker = await _supervisor(supervisor, owner=owner)
    return worker, target.model_id, None


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
    """Compatibility SSE wrapper over managed ``chat.complete``.

    This helper remains for lightweight product surfaces that still consume
    the historical SSE shape (currently rewrite).  It deliberately performs
    no provider HTTP streaming: the managed router owns provider execution,
    retries, account rotation, and sharing.  Tool-bearing turns belong at
    :func:`stream_agent_target` instead.
    """

    del cwd, fallbacks
    try:
        target = await mimo_agent_target(
            target,
            owner=owner,
            supervisor=supervisor,
        )
        if kwargs.get("tools"):
            raise SupervisorAdmissionError(
                "TOOLS_REQUIRE_AGENT",
                "Tool-bearing turns require the managed Agent boundary",
                phase="routing",
                retryable=False,
            )

        envelope = dict(kwargs.get("turn_envelope") or {})
        from src.openclank.chat_routing import resolve_chat_route, shared_endpoint_id
        from src.openclank.modality_facade import complete_text

        _connection_id, _separator, provider_model_id = target.model_id.partition("/")
        grant_id = str(envelope.get("provider_grant_id") or "").strip()
        route = resolve_chat_route(
            owner=owner,
            endpoint_id=(
                shared_endpoint_id(grant_id)
                if grant_id
                else target.endpoint_id
            ),
            model_id=provider_model_id,
        )
        max_tokens = kwargs.get("max_output_tokens", kwargs.get("max_tokens"))
        if max_tokens is not None and int(max_tokens) < 1:
            max_tokens = None
        text = await complete_text(
            owner=owner or "local-installation",
            purpose="chat",
            messages=messages,
            model_route_id=route.model_route_id,
            grant_id=route.provider_grant_id,
            root_operation_id=envelope.get("root_operation_id"),
            idempotency_key=f"stream-chat-{session_id}",
            temperature=kwargs.get("temperature"),
            max_output_tokens=max_tokens,
        )
        if text:
            yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"
    except Exception as exc:
        for event in _typed_error_sse(exc):
            yield event


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
    # Every strict/background origin converges here before a worker lease is
    # acquired. A fresh Rust AgentScope projection selects private file
    # lifetools; native OpenCode filesystem/search aliases stay denied,
    # so a caller cannot recover the old repo/home fallback by omitting the
    # route layer's disabled-tool list.
    from src.tool_security import (
        brokered_agent_file_tools,
        unavailable_strict_agent_tools,
    )
    supplied_envelope = kwargs.get("turn_envelope") or {}
    authority_workspace = str(supplied_envelope.get("authority_workspace_id") or "")
    brokered_file_tools = brokered_agent_file_tools(owner, cwd, workspace_id=authority_workspace, chat_id=session_id)
    unavailable_tools = unavailable_strict_agent_tools(owner, cwd, workspace_id=authority_workspace, chat_id=session_id)
    if unavailable_tools:
        kwargs["disabled_tools"] = set(kwargs.get("disabled_tools") or ()) | unavailable_tools
        relevant = kwargs.get("relevant_tools")
        if relevant is not None:
            kwargs["relevant_tools"] = set(relevant) - unavailable_tools
    proof_callback = kwargs.pop("execution_proof_callback", None)
    foreground_account_context = kwargs.pop("_foreground_account_context", None)
    direct_settlement_callback = kwargs.pop("_direct_settlement_callback", None)
    direct_account_only = kwargs.pop("_direct_account_only", False) is True
    provider_policy_selection = kwargs.pop('_provider_policy_selection', None)
    provider_policy_intent = kwargs.pop('_provider_policy_intent', None)
    turn_envelope = _agent_turn_envelope(
        messages, cwd, kwargs.pop("turn_envelope", None), kwargs,
    )
    # Caller payloads cannot mint this field. It is a fresh server projection
    # of the current owner/workspace AgentScope and is consumed only to expose
    # private Rust-backed lifetools while native OS tools stay denied.
    turn_envelope["brokered_file_tools"] = sorted(brokered_file_tools)
    request = AgentRunRequest(
        target=target,
        messages=messages,
        session_id=session_id,
        owner=owner,
        cwd=cwd,
        supervisor=supervisor,
        turn_envelope=turn_envelope,
        execution_proof_callback=proof_callback,
        foreground_account_context=foreground_account_context,
        direct_settlement_callback=direct_settlement_callback,
        direct_account_only=direct_account_only,
        provider_policy_selection=provider_policy_selection,
        provider_policy_intent=provider_policy_intent,
    )
    async for chunk in run_agent(request):
        yield chunk


async def run_agent(request: AgentRunRequest) -> AsyncGenerator[str, None]:
    """The only strict tool-bearing application door."""
    incognito = bool((request.turn_envelope or {}).get("incognito"))
    pool = request.supervisor or _agent_supervisor
    if (getattr(_agent_supervisor, '_s05_foreground_runtime', None) is not None
        and pool is not _agent_supervisor):
        exc = SupervisorAdmissionError("OCCUPANCY_UNAVAILABLE", "Foreground occupancy is unavailable", phase="admission", retryable=True)
        for event in _typed_error_sse(exc):
            yield event
        return
    if pool is None or not callable(getattr(pool, "admit_agent", None)):
        exc = SupervisorAdmissionError(
            "SUPERVISOR_UNAVAILABLE", "The strict Open Clank agent coordinator is unavailable"
        )
        for event in _typed_error_sse(exc):
            yield event
        return

    try:
        if not incognito:
            from src.openclank.transcript_projection import assert_directory_execution_active
            assert_directory_execution_active(request.session_id, request.owner)
        target = await mimo_agent_target(
            request.target,
            owner=request.owner,
            supervisor=pool,
        )
        runtime_model = target.model_id
    except Exception as exc:
        if not incognito and not hasattr(exc, "as_dict") and not isinstance(exc, HTTPException):
            logger.exception("Unexpected strict Agent admission failure")
        safe_exc = exc if not incognito or hasattr(exc, "as_dict") else SupervisorAdmissionError("PRIVATE_RUNTIME_UNAVAILABLE", "Private Agent runtime is unavailable", phase="admission", retryable=False)
        for event in _typed_error_sse(safe_exc):
            yield event
        return

    qualified = runtime_model.split("/", 1)
    if len(qualified) != 2:
        exc = SupervisorAdmissionError(
            "MODEL_NOT_PROJECTED", "Open Clank agent models must retain provider identity",
            phase="routing", retryable=False,
        )
        for event in _typed_error_sse(exc):
            yield event
        return
    provider_id, model_id = qualified
    worker_lease = None
    session_lease = None
    successful_terminal = False
    model_terminal_observed = False
    saw_terminal_error = False
    foreground_runtime = getattr(pool, "_s05_foreground_runtime", None)
    foreground_handle = None
    direct_admission = None
    direct_attachment = None
    direct_bridge = None
    issued = False
    try:
        selection = getattr(request, 'provider_policy_selection', None)
        intent = getattr(request, 'provider_policy_intent', None)
        if intent is not None:
            from src.openclank.provider_human_policy import CapturedProviderPolicyIntent
            if type(intent) is not CapturedProviderPolicyIntent or intent.selection is not selection:
                raise ValueError('captured provider policy intent unavailable')
            intent.assert_current()
            context=request.foreground_account_context
            if (not request.direct_account_only or type(context) is not tuple or len(context)!=4
                    or intent.admission.owner!=request.owner or intent.admission.account_id!=context[0]
                    or intent.admission._barrier is not context[2] or intent.admission._admission is not context[3]):
                raise ValueError('captured provider policy intent changed')
        if selection is not None:
            from src.openclank.provider_human_policy import CapturedProviderPolicy
            if type(selection) is not CapturedProviderPolicy:
                raise ValueError('captured provider policy unavailable')
            selection.assert_current()
            context = request.foreground_account_context
            if (not (foreground_runtime is not None or request.direct_account_only)
                    or type(context) is not tuple or len(context) != 4
                    or selection.owner != request.owner or selection.chat_id != request.session_id
                    or selection.account_id != context[0] or selection._admission._barrier is not context[2]
                    or selection._admission._admission is not context[3]):
                raise ValueError('captured provider policy authority changed')
        if foreground_runtime is not None or getattr(request,'direct_account_only',False) is True:
            from src.openclank.memory_job_admission import ForegroundRuntime
            if (foreground_runtime is not None and type(foreground_runtime) is not ForegroundRuntime) or not request.owner:
                raise SupervisorAdmissionError("OCCUPANCY_UNAVAILABLE", "Foreground occupancy is unavailable", phase="admission", retryable=True)
            context = request.foreground_account_context
            if type(context) is not tuple or len(context) != 4:
                raise SupervisorAdmissionError("OCCUPANCY_UNAVAILABLE", "Foreground account admission unavailable", phase="admission", retryable=True)
            account, auth, barrier, admission = context
            if (not account or auth.account_id(request.owner) != account
                    or auth.username_for_account_id(account) != request.owner
                    or not await barrier.is_live(request.owner, admission)):
                raise SupervisorAdmissionError("OCCUPANCY_UNAVAILABLE", "Foreground account admission unavailable", phase="admission", retryable=True)
            direct_admission = await barrier.acquire(request.owner)
        if incognito:
            admit = getattr(pool, "admit_private_agent", None)
            if not callable(admit) or not request.owner:
                raise SupervisorAdmissionError("PRIVATE_RUNTIME_UNAVAILABLE", "Private Agent runtime is unavailable", phase="admission", retryable=False)
            worker_lease = await admit(request.owner, request.session_id, provider_id, model_id)
        else:
            worker_lease = await pool.admit_agent(request.owner, provider_id, model_id)
        worker = worker_lease.worker
        if foreground_runtime is not None or getattr(request,'direct_account_only',False) is True:
            if (auth.account_id(request.owner) != account or auth.username_for_account_id(account) != request.owner
                    or not await barrier.is_live(request.owner, direct_admission)):
                raise SupervisorAdmissionError("OCCUPANCY_UNAVAILABLE", "Foreground account admission changed", phase="admission", retryable=True)
            from src.memory_scope import memory_owner
            if foreground_runtime is not None:
                foreground_handle = foreground_runtime.enter(owner=memory_owner(request.owner), account_id=account, connection_id=provider_id)
            direct_bridge = worker.bridge
            if foreground_runtime is None:
                direct_attachment = await worker_lease.attach_direct_foreground(barrier, direct_admission, account, None, None, account_only=True)
            else:
                direct_attachment = await worker_lease.attach_direct_foreground(barrier, direct_admission, account, foreground_runtime, foreground_handle)
            if selection is not None:
                await direct_bridge._bind_direct_provider_policy(direct_attachment, selection)
            if intent is not None:
                await direct_bridge._bind_direct_provider_policy_intent(direct_attachment, intent)
        session_lease = AgentSessionLease(
            worker,
            request.session_id,
            # The bridge owns exact-id cleanup for Temporary Agent sessions.
            ephemeral=target.lifecycle == "ephemeral" and not incognito,
        )
        is_shared = bool((request.turn_envelope or {}).get("provider_grant_id"))
        yield f'data: {json.dumps({"type": "projection", "data": {"generation": worker_lease.generation, "fingerprint": worker_lease.fingerprint[:12], "projection_pending": worker_lease.projection_pending, **({"shared": True} if is_shared else {})}})}\n\n'
        envelope = dict(request.turn_envelope or {})
        envelope["lane"] = "agent"
        if target.capabilities.get("tools") is False:
            envelope["allowed_tools"] = []
        from src.openclank.acp_supervisor_adapter import AcpRuntimeLease
        if type(worker_lease) is AcpRuntimeLease:
            private_agent_lease = worker_lease._private_agent_lease()
            if private_agent_lease is None:
                raise SupervisorAdmissionError('SUPERVISOR_UNAVAILABLE','Original Agent admission unavailable',phase='admission',retryable=True)
        else:
            private_agent_lease = worker_lease
        issued = True
        async for chunk in worker.bridge.run_turn(
            request.session_id,
            request.messages,
            model=runtime_model,
            cwd=request.cwd,
            owner=request.owner,
            turn_envelope=envelope,
            _gcf_lease=private_agent_lease,
            **({"_direct_attachment": direct_attachment} if direct_attachment is not None else {}),
            **({"execution_proof_callback": request.execution_proof_callback} if request.execution_proof_callback is not None else {}),
        ):
            if chunk.lstrip().startswith("event: error"):
                saw_terminal_error = True
            if chunk.strip() == "data: [DONE]":
                successful_terminal = not saw_terminal_error
                model_terminal_observed = successful_terminal
            yield chunk
    except Exception as exc:
        successful_terminal = False
        if not incognito and not hasattr(exc, "as_dict") and not isinstance(exc, HTTPException):
            logger.exception("Unexpected strict Agent runtime failure")
        safe_exc = exc if not incognito or hasattr(exc, "as_dict") else SupervisorAdmissionError("PRIVATE_RUNTIME_UNAVAILABLE", "Private Agent runtime is unavailable", phase="admission", retryable=False)
        for event in _typed_error_sse(safe_exc):
            yield event
    except BaseException:
        successful_terminal = False
        raise
    finally:
        # Model completion and local cleanup are different facts.  Capture the
        # former before session/worker teardown can change local state.
        model_terminal = model_terminal_observed
        direct_settled = False
        try:
            if session_lease is not None:
                try:
                    await session_lease.close()
                    if session_lease.cleanup_failed:
                        successful_terminal = False
                except Exception:
                    logger.warning("Agent session cleanup unavailable")
                    successful_terminal = False
        finally:
            try:
                if direct_attachment is not None and (model_terminal or not issued):
                    settlement = asyncio.create_task(direct_bridge._finish_direct_foreground(direct_attachment, terminal=True))
                    while not settlement.done():
                        try:
                            await asyncio.shield(settlement)
                        except asyncio.CancelledError:
                            successful_terminal = False
                    direct_settled = settlement.result() is True
                elif direct_attachment is None and direct_admission is not None:
                    if foreground_handle is not None:
                        foreground_runtime.leave(foreground_handle)
                    await barrier.release(direct_admission)
            finally:
                if worker_lease is not None:
                    cleanup = asyncio.create_task(worker_lease.release(successful_terminal=successful_terminal))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        try:
                            await cleanup
                        except Exception:
                            logger.warning("Agent worker cleanup unavailable")
                    except Exception:
                        logger.warning("Agent worker cleanup unavailable")
                callback=getattr(request,'direct_settlement_callback',None)
                if callable(callback):
                    evidence={'direct_settled':direct_settled,'model_terminal':model_terminal,
                        'session_closed':bool(session_lease is not None and getattr(session_lease,'_closed',False) and not session_lease.cleanup_failed),
                        'worker_released':bool(worker_lease is not None and getattr(worker_lease,'_released',False))}
                    try:
                        outcome=callback(evidence)
                        if inspect.isawaitable(outcome):
                            task=asyncio.create_task(outcome)
                            while not task.done():
                                try: await asyncio.shield(task)
                                except asyncio.CancelledError: continue
                            task.result()
                    except Exception:
                        logger.warning('Agent direct settlement receipt unavailable')


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
    """Compatibility wrapper over the typed managed-completion boundary.

    Provider URLs and headers are deliberately not forwarded. A surviving
    caller must already carry normalized managed connection identity; direct
    HTTP targets fail closed.
    """
    if not request.purpose.strip():
        raise ValueError("Auxiliary inference requires a named product purpose")
    target = request.target
    if target.transport != "acp" or target.headers:
        raise SupervisorAdmissionError(
            "LEGACY_PROVIDER_ROUTE_RETIRED",
            "Direct provider dispatch is unavailable; configure a managed route binding",
            phase="routing",
            retryable=False,
        )

    await mimo_agent_target(
        target,
        owner=request.owner,
        supervisor=request.supervisor,
    )

    from src.openclank.chat_routing import resolve_chat_route
    from src.openclank.modality_facade import complete_text

    route = resolve_chat_route(
        owner=request.owner,
        endpoint_id=target.endpoint_id,
        model_id=target.model_id.split("/", 1)[1],
    )
    raw_purpose = request.purpose.strip().lower()
    if raw_purpose == "memory" or raw_purpose.startswith("memory:"):
        purpose = "memory"
    elif "research" in raw_purpose:
        purpose = "research"
    elif "task" in raw_purpose:
        purpose = "tasks"
    elif raw_purpose == "chat":
        purpose = "chat"
    else:
        purpose = "utility"

    options = dict(request.options)
    max_output_tokens = options.pop(
        "max_output_tokens",
        options.pop("max_tokens", None),
    )
    call = complete_text(
        owner=request.owner or "local-installation",
        purpose=purpose,
        messages=request.messages,
        model_route_id=route.model_route_id,
        grant_id=route.provider_grant_id,
        root_operation_id=options.pop("root_operation_id", None),
        temperature=options.pop("temperature", None),
        max_output_tokens=max_output_tokens,
    )
    if request.timeout is None:
        return await call

    import asyncio

    return await asyncio.wait_for(call, timeout=request.timeout)

"""Transport-safe model execution shared by chat and auxiliary callers."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncGenerator, Iterable, Optional

from fastapi import HTTPException

from src.endpoint_resolver import ResolvedModelTarget, resolve_model_target

logger = logging.getLogger(__name__)

_mimo_supervisor: Any = None


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
) -> Optional[ResolvedModelTarget]:
    """Agent-mode ACP rewrite (mimo-drives-agent metaplan, e's ruling
    2026-07-17): when an http target's model is servable through a
    projected endpoint provider, return the ACP target so mimo's native
    tool engine runs the turn. None = not servable → the caller keeps
    today's homegrown path. The decision is made HERE, before dispatch —
    no mid-stream fallback."""
    if target is None or target.transport != "http":
        return None
    try:
        from src.settings import get_setting

        if not bool(get_setting("agent_via_mimo", True)):
            return None
    except Exception:
        pass
    pool = supervisor or _mimo_supervisor
    if pool is None:
        return None
    try:
        from src.endpoint_resolver import endpoint_id_for_chat_url
        from src.openclank.mimo_supervisor import ENDPOINT_PROVIDER_PREFIX

        ep_id = endpoint_id_for_chat_url(target.endpoint_url, owner=owner)
        if not ep_id:
            return None
        mimo_model = f"{ENDPOINT_PROVIDER_PREFIX}{ep_id}/{target.model_id}"
        worker = await _supervisor(pool, owner=owner)
        catalog = {
            item.get("modelId")
            for item in (worker.available_models() or [])
            if item.get("modelId")
        }
        if mimo_model not in catalog:
            logger.info(
                "[agent-route] %s not in mimo catalog (%d models) — homegrown loop",
                mimo_model,
                len(catalog),
            )
            return None
        logger.info("[agent-route] agent turn routed to mimo: %s", mimo_model)
        return resolve_model_target("mimo://acp", mimo_model)
    except HTTPException:
        return None
    except Exception as exc:
        logger.warning("[agent-route] mimo rewrite failed, homegrown loop: %s", exc)
        return None


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
            async for chunk in bridge.run_turn(
                session_id,
                messages,
                model=target.model_id,
                cwd=cwd,
                owner=owner,
                turn_envelope=kwargs.get("turn_envelope"),
            ):
                yield chunk
        finally:
            if target.lifecycle == "ephemeral":
                try:
                    await sup.delete_session(session_id)
                except Exception as exc:
                    logger.warning("Failed to clean up ephemeral MiMo stream %s: %s", session_id, exc)
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
    if target.transport == "acp":
        async for chunk in stream_chat_target(
            target,
            messages,
            session_id=session_id,
            owner=owner,
            cwd=cwd,
            supervisor=supervisor,
            fallbacks=fallbacks,
            **kwargs,
        ):
            yield chunk
        return

    from src.agent_loop import stream_agent_loop

    # ACP-only concepts stop at the transport fork (see stream_chat_target).
    kwargs.pop("turn_envelope", None)
    async for chunk in stream_agent_loop(
        target.endpoint_url,
        target.model_id,
        messages,
        headers=dict(target.headers),
        session_id=session_id,
        owner=owner,
        fallbacks=_validated_candidates(target, fallbacks)[1:],
        workspace=cwd,
        **kwargs,
    ):
        yield chunk


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
    """Return one response string without ever handing ACP URLs to httpx."""
    if target.transport == "http":
        from src.llm_core import llm_call_async

        return await llm_call_async(
            target.endpoint_url,
            target.model_id,
            messages,
            headers=dict(target.headers),
            session_id=session_id,
            _transport_checked=True,
            **kwargs,
        )

    sup = await _supervisor(supervisor, owner=owner)
    turn_session = session_id or f"aux-{uuid.uuid4().hex}"
    parts: list[str] = []
    error: Optional[tuple[int, str]] = None
    try:
        async for chunk in sup.bridge.run_turn(
            turn_session,
            messages,
            model=target.model_id,
            cwd=cwd,
            owner=owner,
            turn_envelope=kwargs.get("turn_envelope"),
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
        if target.lifecycle == "ephemeral":
            try:
                await sup.delete_session(turn_session)
            except Exception as exc:
                logger.warning("Failed to clean up ephemeral MiMo session %s: %s", turn_session, exc)

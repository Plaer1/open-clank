"""Always-on monitor that auto-continues the agent when a background job
(see src/bg_jobs.py) finishes.

Reliability is the whole point: completion → agent re-invocation must never
silently no-op. The monitor drains `bg_jobs.pending_followups()` every tick and
only calls `mark_followed_up()` AFTER the agent run succeeds — so a transient
failure is simply retried on the next tick. A timed-out/dead job still produces
a follow-up ("the job failed/timed out"), so the user always hears back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid

from src import bg_jobs

logger = logging.getLogger(__name__)

_monitor_task = None
POLL_INTERVAL_S = 5
# The follow-up agent run is allowed a few rounds to actually continue the task
# (e.g. after `pip install` finishes, run the transcription).
_FOLLOWUP_MAX_ROUNDS = 12
_MONITOR_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
_FOLLOWUP_TIMEOUT_S = 15 * 60


async def _drain_agent(sess, messages):
    """Run the agent loop headless against a session. Returns
    (final_prose, tool_events) — tool_events in the same shape the live chat
    saves, so the frontend rebuilds them as standard agent-thread tool cards."""
    from src.endpoint_resolver import resolve_model_target
    from src.model_dispatch import stream_agent_target

    full = ""
    tool_events = []
    round_num = 1
    target = resolve_model_target(
        sess.endpoint_url,
        sess.model,
        getattr(sess, "headers", None),
        endpoint_id=getattr(sess, "endpoint_id", None),
    )
    async for chunk in stream_agent_target(
        target,
        messages,
        context_length=getattr(sess, "context_length", 0) or 0,
        session_id=sess.id,
        max_rounds=_FOLLOWUP_MAX_ROUNDS,
        owner=getattr(sess, "owner", None),
        cwd=getattr(sess, "workspace", None),
        workload="background",
    ):
        if chunk.startswith("event: error"):
            data_line = next((line[5:].strip() for line in chunk.splitlines() if line.startswith("data:")), "")
            try:
                payload = json.loads(data_line)
            except ValueError:
                payload = {"error": data_line or "Agent follow-up failed"}
            raise RuntimeError(str(payload.get("code") or payload.get("error") or "Agent follow-up failed"))
        if not chunk.startswith("data: "):
            continue
        body = chunk[6:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            d = json.loads(body)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        if "delta" in d:
            delta = d.get("delta")
            if isinstance(delta, str):
                if d.get("thinking"):
                    continue
                full += delta
        elif d.get("type") == "agent_step":
            round_num = d.get("round", round_num)
        elif d.get("type") == "tool_output":
            # Mirror the live chat's tool_event shape (chat_routes / chatRenderer).
            tool_events.append({
                "round": round_num,
                "tool": d.get("tool"),
                "command": d.get("command"),
                "output": d.get("output"),
                "exit_code": d.get("exit_code"),
            })
    return full, tool_events


async def _run_followup(rec: dict) -> bool:
    """Re-invoke the agent in the job's session with the result. Returns True
    if the follow-up completed (or there's nothing to do) — i.e. it's safe to
    mark followed_up. Returns False to retry on the next tick."""
    from src.ai_interaction import get_session_manager
    from core.models import ChatMessage

    sm = get_session_manager()
    if not sm:
        return False  # not ready yet — retry
    sess = sm.get_session(rec["session_id"])
    if not sess:
        # Session was deleted — nothing to continue. Consider it handled so we
        # don't retry forever.
        logger.info("bg-followup: session %s gone for job %s — skipping", rec.get("session_id"), rec.get("id"))
        return True

    # Crash-safe idempotency: persistence may have succeeded immediately before
    # a process died and released its file-store claim. Never append twice.
    for message in getattr(sess, "history", ()):
        metadata = getattr(message, "metadata", None)
        if metadata is None and isinstance(message, dict):
            metadata = message.get("metadata")
        if isinstance(metadata, dict) and metadata.get("bg_job_id") == rec.get("id"):
            return True

    # Don't write into a session that's mid-stream. The followup appends to
    # history + save_sessions(); a concurrent live turn does the same, and with
    # no per-session lock the two interleave (reordered/clobbered messages).
    # Defer — return False so we retry on the next tick once the turn finishes.
    try:
        from src import agent_runs
        if agent_runs.is_active(sess.id):
            logger.info("bg-followup: session %s busy (live turn) — deferring job %s", sess.id, rec.get("id"))
            return False
    except Exception:
        pass

    inject = (
        f"[Background job {rec['id']} finished]\n\n"
        f"{bg_jobs.result_text(rec)}\n\n"
        "Continue the task using this output. Don't repeat work that's already done. "
        "If the task is now complete, give the user the final result."
    )
    context = sess.get_context_messages()
    context.append({"role": "user", "content": inject})

    full, tool_events = await _drain_agent(sess, context)
    if not full.strip() and not tool_events:
        raise RuntimeError("Agent follow-up ended without a result")

    # Persist ONLY the assistant continuation so it renders as a normal agent
    # turn — a standard chat bubble plus `tool_events` that the frontend
    # rebuilds into the usual agent-thread tool cards (chatRenderer:1494). The
    # trigger isn't saved as its own message (it'd be an out-of-place bubble);
    # the raw job output is stashed in metadata for traceability instead.
    sm.add_message(sess.id, ChatMessage(
        "assistant", full,
        metadata={
            "tool_events": tool_events,
            "model": sess.model,
            "bg_job_id": rec["id"],
            "bg_result": bg_jobs.result_text(rec)[:4000],
        },
    ))
    sm.save_sessions()
    logger.info("bg-followup: auto-continued session %s for job %s (%d chars, %d tools)",
                sess.id, rec["id"], len(full), len(tool_events))
    return True


async def _loop():
    while True:
        try:
            for rec in bg_jobs.claim_pending_followups(_MONITOR_ID):
                claim_token = rec.get("_claim_token") or ""
                try:
                    completed = await asyncio.wait_for(
                        _run_followup(rec), timeout=_FOLLOWUP_TIMEOUT_S,
                    )
                    if completed:
                        bg_jobs.finish_followup(rec["id"], claim_token)
                    else:
                        bg_jobs.fail_followup(
                            rec["id"], claim_token, "Session is busy", count_attempt=False,
                        )
                except Exception as e:
                    state = bg_jobs.fail_followup(rec["id"], claim_token, str(e))
                    logger.warning("bg-followup failed for %s (%s): %s", rec.get("id"), state, e)
                    if state == "failed":
                        await _persist_terminal_failure(rec, str(e))
        except Exception as e:
            logger.warning("bg-monitor tick error: %s", e)
        await asyncio.sleep(POLL_INTERVAL_S)


async def _persist_terminal_failure(rec: dict, error: str) -> None:
    """Surface exhausted continuation failure once without claiming success."""
    from core.models import ChatMessage
    from src.ai_interaction import get_session_manager

    sm = get_session_manager()
    sess = sm.get_session(rec.get("session_id")) if sm else None
    if not sess:
        return
    for message in getattr(sess, "history", ()):
        metadata = getattr(message, "metadata", None)
        if metadata is None and isinstance(message, dict):
            metadata = message.get("metadata")
        if isinstance(metadata, dict) and metadata.get("bg_followup_failure") == rec.get("id"):
            return
    sm.add_message(sess.id, ChatMessage(
        "assistant",
        f"Background job {rec.get('id')} finished, but its automatic Agent continuation failed: {error}",
        metadata={"bg_followup_failure": rec.get("id"), "status": "error"},
    ))
    sm.save_sessions()


def start_bg_monitor():
    """Idempotent — start the always-on background-job monitor."""
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        return _monitor_task
    _monitor_task = asyncio.create_task(_loop())
    logger.info("Background-job monitor started (poll %ds)", POLL_INTERVAL_S)
    return _monitor_task

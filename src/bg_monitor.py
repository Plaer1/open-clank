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
from src.shell_policy import redact_text

logger = logging.getLogger(__name__)

_monitor_task = None
POLL_INTERVAL_S = 5
# The follow-up agent run is allowed a few rounds to actually continue the task
# (e.g. after `pip install` finishes, run the transcription).
_FOLLOWUP_MAX_ROUNDS = 12
_MONITOR_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
_FOLLOWUP_TIMEOUT_S = 15 * 60


async def _bound_followup(candidate: dict, *, owned_run=None) -> None:
    """Run a single v2 continuation through the existing owner and run authority."""
    from dataclasses import asdict
    from core.models import get_session_manager_instance
    from src import agent_runs
    from src.endpoint_resolver import ResolvedModelTarget
    from src.model_dispatch import stream_agent_target
    from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL, resolve_chat_route
    from src.openclank.file_policy import FilePolicyRepository
    from src.openclank.workspace_policy_service import resolve_owned_workspace
    from src.openclank.memory_job_admission import TargetAuthority, ForegroundRuntime
    from routes.chat_routes import _resolve_turn_model_request_options

    manager=get_session_manager_instance(); state=getattr(manager,'lifecycle_context',None) if manager else None
    auth=getattr(state,'auth_manager',None); barrier=getattr(state,'account_request_barrier',None)
    supervisor=getattr(state,'mimo_supervisor',None)
    owner=candidate['owner']; account=candidate['account_id']; chat=candidate['session_id']; job_id=candidate['job_id']
    if not auth or not barrier or not supervisor or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:
        return
    pool=supervisor.owner_lifecycle_pool(); worker=pool.worker_for_owner(owner) if pool else None
    bridge=getattr(worker,'bridge',None); mapping=getattr(bridge,'_durable_session_map',None)
    runtime=getattr(pool,'_s05_foreground_runtime',None)
    if (not worker or not worker.is_alive() or not mapping or agent_runs._ACCOUNT_REQUEST_BARRIER is not barrier
            or (runtime is not None and type(runtime) is not ForegroundRuntime)): return
    admission=await barrier.acquire(owner)
    claim=None; started=False; run=owned_run
    try:
        async def drained_call(fn,*args,**kwargs):
            task=asyncio.create_task(asyncio.to_thread(fn,*args,**kwargs)); cancelled=False
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: cancelled=True
            result=task.result()
            if cancelled: raise asyncio.CancelledError
            return result
        authority=TargetAuthority(auth_manager=auth,supervisor=pool)
        baseline=await asyncio.to_thread(authority.resolve,account)
        snapshot=await asyncio.to_thread(bg_jobs.bound_followup_snapshot,job_id,owner=owner,account_id=account)
        identity=snapshot['identity']; session=manager.get_session(chat)
        if (identity.get('session_id')!=chat or identity.get('privacy')!='normal' or not session or session.owner!=owner
                or getattr(session,'incognito',False) or getattr(session,'archived',False)
                or getattr(session,'workspace_id',None)!=identity.get('workspace_id')): return
        workspace=await asyncio.to_thread(resolve_owned_workspace,FilePolicyRepository(),workspace_id=identity['workspace_id'],
            owner_username=owner,auth_manager=auth,purpose='agent_workspace')
        if workspace.path!=identity.get('workspace'): return
        mapped=await mapping.async_lookup(chat)
        if not mapped or mapped.get('current')!=identity.get('engine_session_id'): return

        async def current():
            if (auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                or manager.get_session(chat) is not session or session.owner!=owner
                or getattr(session,'incognito',False) or getattr(session,'archived',False)
                or getattr(session,'workspace_id',None)!=identity['workspace_id']
                or pool.worker_for_owner(owner) is not worker or not worker.is_alive()
                or getattr(worker,'bridge',None) is not bridge or not await barrier.is_live(owner,admission)):
                return False
            if await asyncio.to_thread(authority.resolve,account)!=baseline or await mapping.async_lookup(chat)!=mapped: return False
            now=await asyncio.to_thread(resolve_owned_workspace,FilePolicyRepository(),workspace_id=identity['workspace_id'],
                owner_username=owner,auth_manager=auth,purpose='agent_workspace')
            return now.path==workspace.path

        async def publish(follow):
            async with mapping.async_chat_admission_lock(chat,existing_only=True) as guard:
                if not guard or not await current(): return
                proof=await drained_call(bg_jobs.begin_bound_publication,job_id,follow['attempt_id'],follow['claim_id'])
                outcome=await manager.publish_bound_shell(proof,mapping=mapping,chat_guard=guard,target_authority=authority)
                if outcome not in ('inserted','already_present') or not await current(): outcome='unknown'
                await drained_call(bg_jobs.finish_bound_publication,proof,outcome)

        def busy():
            run=agent_runs.get_run(chat)
            if owned_run is not None and run is owned_run: return False
            return bool(run and (run.status=='running' or run.cleanup_unresolved
                or (run.task is not None and not run.task.done())))

        follow=snapshot['followup']
        if follow and follow.get('state') in ('publication_pending','dispatching'):
            await publish(follow); return
        if follow is not None or busy(): return
        async with mapping.async_chat_admission_lock(chat,blocking=False,existing_only=True) as guard:
            if not guard or not await current() or busy() or bridge._turns.get(identity['engine_session_id']): return
            claim=await drained_call(bg_jobs.claim_bound_followup,job_id,owner=owner,account_id=account)
            follow=claim['followup']
            if not getattr(session,'provider_model_route_id',None): return
            route=await asyncio.to_thread(resolve_chat_route,owner=owner,endpoint_id=getattr(session,'endpoint_id',None),
                model_id=getattr(session,'model',None),model_route_id=session.provider_model_route_id)
            options=await asyncio.to_thread(_resolve_turn_model_request_options,session,owner,None)
            frozen={'route':asdict(route),'options':options}
            if not await current() or busy() or bridge._turns.get(identity['engine_session_id']): return
            again=await asyncio.to_thread(resolve_chat_route,owner=owner,endpoint_id=getattr(session,'endpoint_id',None),
                model_id=getattr(session,'model',None),model_route_id=session.provider_model_route_id)
            if asdict(again)!=frozen['route'] or await asyncio.to_thread(_resolve_turn_model_request_options,session,owner,None)!=options: return
            messages=session.get_context_messages()
            messages.append({'role':'user','content':f"[System-generated background job result; not a human message: {job_id}]\n\n{follow['shell_output']}\n\nContinue using this output without repeating completed work."})
            target=ResolvedModelTarget(transport='acp',endpoint_url=MANAGED_ENGINE_PUBLIC_URL,model_id=route.runtime_model,
                endpoint_id=route.connection_id,provider_id=route.connection_id,headers={},capabilities=dict(route.capabilities),lifecycle='persistent')
            async def invoke():
                nonlocal started
                if not await current() or agent_runs.get_run(chat) is not run: return
                current_route=await asyncio.to_thread(resolve_chat_route,owner=owner,endpoint_id=getattr(session,'endpoint_id',None),
                    model_id=getattr(session,'model',None),model_route_id=session.provider_model_route_id)
                if (asdict(current_route)!=frozen['route'] or await asyncio.to_thread(_resolve_turn_model_request_options,session,owner,None)!=options
                    or not await current() or agent_runs.get_run(chat) is not run): return
                def issue():
                    nonlocal started
                    bg_jobs.issue_bound_followup(job_id,follow['attempt_id'],follow['claim_id'],frozen)
                    started=True
                await drained_call(issue)
                full=''; tools=[]; saw_error=False
                async def cleanup_receipt(evidence):
                    await drained_call(bg_jobs.record_bound_cleanup,job_id,follow['attempt_id'],follow['claim_id'],evidence)
                async for chunk in stream_agent_target(target,messages,session_id=chat,owner=owner,cwd=workspace.path,
                    workload='background',_foreground_account_context=(account,auth,barrier,admission),
                    _direct_settlement_callback=cleanup_receipt,
                    _direct_account_only=runtime is None,
                    turn_envelope={'root_operation_id':f"bg-followup:{job_id}:{follow['attempt_id']}",
                        'provider_model_route_id':route.model_route_id,'provider_grant_id':route.provider_grant_id,
                        'model_request_options':options,'authority_workspace_id':identity['workspace_id'],
                        'memory_read_allowed':False,'interaction_policy':'fail_on_interaction'}):
                    if chunk.startswith('event: error'): saw_error=True
                    if chunk.strip()=='data: [DONE]':
                        if not saw_error and (full.strip() or tools):
                            await drained_call(bg_jobs.record_bound_terminal,job_id,follow['attempt_id'],follow['claim_id'],full,
                                {'bg_job_id':job_id,'tool_events':tools,'origin':'generated_background_continuation'})
                        yield chunk
                        continue
                    if chunk.startswith('data: '):
                        try: data=json.loads(chunk[6:].strip())
                        except (ValueError,TypeError): data={}
                        if type(data) is dict and type(data.get('delta')) is str and not data.get('thinking'):
                            full+=data['delta']
                            if len(full.encode('utf-8'))>262144: raise ValueError('bound continuation output exceeds limit')
                        elif type(data) is dict and data.get('type')=='tool_output':
                            tools.append({k:data.get(k) for k in ('tool','output','exit_code')})
                            if len(json.dumps(tools,ensure_ascii=False).encode('utf-8'))>60000: raise ValueError('bound continuation metadata exceeds limit')
                    yield chunk
            if busy(): return
            if owned_run is None:
                run=agent_runs.start(chat,invoke(),owner=owner)
            elif agent_runs.get_run(chat) is not owned_run:
                return
        if owned_run is not None:
            async for _ in invoke(): pass
        else:
            cancelled=False
            while not run.task.done():
                try: await asyncio.shield(run.task)
                except asyncio.CancelledError:
                    if run.task.done(): break
                    cancelled=True
            if cancelled: raise asyncio.CancelledError
        latest=await asyncio.to_thread(bg_jobs.bound_followup_snapshot,job_id,owner=owner,account_id=account)
        if latest['followup'] and latest['followup'].get('state') in ('publication_pending','dispatching'):
            await publish(latest['followup'])
    finally:
        try:
            if owned_run is None and run is not None and run.task is not None:
                while not run.task.done():
                    try: await asyncio.shield(run.task)
                    except asyncio.CancelledError: continue
            if claim is not None and not started:
                await drained_call(bg_jobs.defer_bound_followup,job_id,claim['followup']['attempt_id'],claim['followup']['claim_id'])
        finally:
            task=asyncio.create_task(barrier.release(admission))
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
            task.result()


async def _drain_agent(
    sess,
    messages,
    *,
    workspace: str,
    root_operation_id: str,
):
    """Run the agent loop headless against a session. Returns
    (final_prose, tool_events) — tool_events in the same shape the live chat
    saves, so the frontend rebuilds them as standard agent-thread tool cards."""
    from src.endpoint_resolver import ResolvedModelTarget
    from src.model_dispatch import stream_agent_target
    from src.openclank.chat_routing import (
        MANAGED_ENGINE_PUBLIC_URL,
        resolve_chat_route,
    )

    full = ""
    tool_events = []
    round_num = 1
    route = resolve_chat_route(
        owner=getattr(sess, "owner", None),
        endpoint_id=getattr(sess, "endpoint_id", None),
        model_id=getattr(sess, "model", None),
        model_route_id=getattr(sess, "provider_model_route_id", None),
    )
    capabilities = dict(route.capabilities or {})
    capabilities.setdefault("chat", True)
    capabilities.setdefault("stream", True)
    capabilities.setdefault("tools", True)
    target = ResolvedModelTarget(
        transport="acp",
        endpoint_url=MANAGED_ENGINE_PUBLIC_URL,
        model_id=route.runtime_model,
        endpoint_id=route.connection_id,
        provider_id=route.connection_id,
        headers={},
        capabilities=capabilities,
        lifecycle="ephemeral",
    )
    async for chunk in stream_agent_target(
        target,
        messages,
        context_length=getattr(sess, "context_length", 0) or 0,
        session_id=sess.id,
        max_rounds=_FOLLOWUP_MAX_ROUNDS,
        owner=getattr(sess, "owner", None),
        cwd=workspace,
        workload="background",
        turn_envelope={
            "root_operation_id": root_operation_id,
            "provider_model_route_id": route.model_route_id,
            "provider_grant_id": route.provider_grant_id,
            "interaction_policy": "fail_on_interaction",
        },
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


def _session_matches_job(sess, rec: dict) -> bool:
    return (
        str(getattr(sess, "id", "") or "") == str(rec.get("session_id") or "")
        and bool(str(getattr(sess, "owner", "") or ""))
        and str(getattr(sess, "owner", "") or "") == str(rec.get("owner") or "")
        and bool(str(rec.get("workspace") or ""))
    )


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
    if not _session_matches_job(sess, rec):
        logger.warning(
            "bg-followup: scope mismatch for session %s job %s — skipping",
            rec.get("session_id"),
            rec.get("id"),
        )
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

    full, tool_events = await _drain_agent(
        sess,
        context,
        workspace=str(rec["workspace"]),
        root_operation_id=f"bg-followup:{rec['id']}",
    )
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
            for candidate in await asyncio.to_thread(bg_jobs.bound_followup_candidates):
                try:
                    from src import agent_runs
                    chat=candidate['session_id']; previous=agent_runs.get_run(chat)
                    if previous and (previous.status=='running' or previous.cleanup_unresolved
                            or (previous.task is not None and not previous.task.done())): continue
                    holder={}
                    async def owned(candidate=candidate, holder=holder):
                        await _bound_followup(candidate,owned_run=holder['run'])
                        if False: yield ''
                    holder['run']=agent_runs.start(chat,owned(),owner=candidate['owner'])
                except Exception as exc:
                    logger.warning('bound background continuation unavailable: %s',type(exc).__name__)
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
                    safe_error = redact_text(e)
                    state = bg_jobs.fail_followup(rec["id"], claim_token, safe_error)
                    logger.warning(
                        "bg-followup failed for %s (%s): %s",
                        rec.get("id"),
                        state,
                        safe_error,
                    )
                    if state == "failed":
                        await _persist_terminal_failure(rec, safe_error)
        except Exception as e:
            logger.warning("bg-monitor tick error: %s", redact_text(e))
        await asyncio.sleep(POLL_INTERVAL_S)


async def _persist_terminal_failure(rec: dict, error: str) -> None:
    """Surface exhausted continuation failure once without claiming success."""
    from core.models import ChatMessage
    from src.ai_interaction import get_session_manager

    sm = get_session_manager()
    sess = sm.get_session(rec.get("session_id")) if sm else None
    if not sess or not _session_matches_job(sess, rec):
        return
    for message in getattr(sess, "history", ()):
        metadata = getattr(message, "metadata", None)
        if metadata is None and isinstance(message, dict):
            metadata = message.get("metadata")
        if isinstance(metadata, dict) and metadata.get("bg_followup_failure") == rec.get("id"):
            return
    safe_error = redact_text(error)
    sm.add_message(sess.id, ChatMessage(
        "assistant",
        f"Background job {rec.get('id')} finished, but its automatic Agent continuation failed: {safe_error}",
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

"""Private original-utility custody for the staging-only Git worktree phase.

This module does not create an admission, a Once receipt, or a Files scope.
Only an already admitted owner can attach its original utility and phase.
"""
from __future__ import annotations
import asyncio
import hmac
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from src.openclank.account_request_barrier import UtilityLifetime
from src.openclank.git_worktree_stage_phase import WorktreeStagePhase


def _same(a,b): return type(a) is str and type(b) is str and hmac.compare_digest(a,b)


@dataclass(eq=False)
class WorktreeStageContext:
    utility: UtilityLifetime
    phase: WorktreeStagePhase
    recheck: Callable[[],Awaitable[bool]] = field(repr=False)
    deadline: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock,repr=False)


def attach_original(utility:UtilityLifetime,phase:WorktreeStagePhase,recheck:Callable[[],Awaitable[bool]],deadline:float) -> WorktreeStageContext:
    if (type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= asyncio.get_running_loop().time()
            or deadline > asyncio.get_running_loop().time() + 300
            or type(utility) is not UtilityLifetime or type(phase) is not WorktreeStagePhase or not callable(recheck)
            or utility.root!='git-worktree-stage' or utility.settled or utility.cancel_requested
            or not _same(utility.idempotency_key,phase.identity['operationID'])
            or not _same(utility.request_hash,phase.identity['requestDigest'])
            or getattr(utility,'git_worktree_stage_context',None) is not None):
        raise ValueError('worktree utility unavailable')
    context=WorktreeStageContext(utility,phase,recheck,deadline)
    utility.git_worktree_stage_context=context
    return context


async def _current(bridge:Any,utility:UtilityLifetime,context:WorktreeStageContext,*,recheck:bool=True)->bool:
    try:
        pool=utility.supervisor;owner=pool._key(utility.owner)
        pool._assert_owner_lifecycle(owner,utility.epoch)
        state=pool._states.get(owner);worker=utility.worker;identity=context.phase.identity
        return bool(type(utility) is UtilityLifetime and type(context) is WorktreeStageContext
            and asyncio.get_running_loop().time() < context.deadline
            and context.utility is utility and getattr(utility,'git_worktree_stage_context',None) is context
            and utility.root=='git-worktree-stage' and _same(utility.idempotency_key,identity['operationID'])
            and _same(utility.request_hash,identity['requestDigest']) and utility.issued and not utility.settled
            and not utility.cancel_requested and not utility.admission.released
            and await utility.barrier.is_live(owner,utility.admission) and utility.account_current()
            and utility.lease is not None and not utility.lease._released and utility.lease.worker is worker
            and utility.lease._pool is pool and utility.lease._owner_epoch==utility.epoch
            and state is not None and state.status=='ready' and state.active is worker
            and state.generation==utility.generation and worker.is_alive() and worker._proc is utility.process
            and worker._bridge is bridge is utility.bridge and bridge._owner==owner
            and bridge._managed_provider_generation==utility.holder
            and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==utility.holder
            and getattr(worker,'installed_generation',None)==utility.generation
            and (not recheck or await context.recheck() is True)
            and asyncio.get_running_loop().time() < context.deadline)
    except Exception:
        return False


async def issue_current(utility:UtilityLifetime,context:WorktreeStageContext,policy_current:Callable[[],bool])->bool:
    try: return bool(await _current(utility.bridge,utility,context,recheck=True) and policy_current() is True)
    except Exception: return False


async def dispatch(bridge:Any,request:dict)->dict:
    admitted=False;context=None
    try:
        from core.models import get_session_manager_instance
        from src.openclank.git_worktree_inverse_owner import WorktreeInverseContext, inverse_current
        from src.openclank.git_worktree_inverse_phase import dispatch_inverse
        from src.openclank.git_worktree_redo_owner import WorktreeRedoContext, redo_current
        from src.openclank.git_worktree_redo_phase import dispatch_redo
        manager=get_session_manager_instance();lifecycle=getattr(manager,'lifecycle_context',None) if manager else None
        barrier=getattr(lifecycle,'account_request_barrier',None);supervisor=getattr(lifecycle,'mimo_supervisor',None)
        pool=supervisor.owner_lifecycle_pool() if supervisor else None;worker=pool.worker_for_owner(bridge._owner) if pool else None
        process=getattr(worker,'_proc',None)
        if not barrier or not pool or not worker or not process or type(request) is not dict:return {'state':'refused'}
        matches=[]
        for utility in await barrier.utility_for_child(bridge._owner,worker,process):
            candidate=getattr(utility,'git_worktree_stage_context',None)
            if type(candidate) is WorktreeStageContext and all(_same(request.get(k),v) for k,v in candidate.phase.identity.items()): matches.append((utility,candidate))
            candidate=getattr(utility,'git_worktree_inverse_context',None)
            if type(candidate) is WorktreeInverseContext and all(_same(request.get(k),v) for k,v in candidate.identity.items()): matches.append((utility,candidate))
            candidate=getattr(utility,'git_worktree_redo_context',None)
            if type(candidate) is WorktreeRedoContext and all(_same(request.get(k),v) for k,v in candidate.identity.items()): matches.append((utility,candidate))
        if len(matches)!=1:return {'state':'refused'}
        utility,context=matches[0]
        if type(context) is WorktreeRedoContext:
            admitted=True
            if utility.barrier is not barrier or utility.supervisor is not pool or utility.bridge is not bridge or not await redo_current(utility,context):
                context.state='unknown';utility.host_cleanup_unknown=True;return {'state':'unknown'}
            return await dispatch_redo(context,request)
        if type(context) is WorktreeInverseContext:
            admitted=True
            if utility.barrier is not barrier or utility.supervisor is not pool or utility.bridge is not bridge or not await inverse_current(utility,context):
                context.state='unknown';utility.host_cleanup_unknown=True;return {'state':'unknown'}
            return await dispatch_inverse(context,request)
        async with context.lock:
            if utility.barrier is not barrier or utility.supervisor is not pool or not await _current(bridge,utility,context):
                context.phase.state='unknown';return {'state':'unknown'}
            async def verify(): return await _current(bridge,utility,context)
            context.phase.verify=verify;admitted=True
            return await context.phase.dispatch(request)
    except asyncio.CancelledError:
        if admitted and context is not None:
            if type(context) in (WorktreeInverseContext,WorktreeRedoContext):context.state='unknown';context.utility.host_cleanup_unknown=True
            else:context.phase.state='unknown'
        raise
    except Exception:
        if admitted and context is not None:
            if type(context) in (WorktreeInverseContext,WorktreeRedoContext):context.state='unknown';context.utility.host_cleanup_unknown=True
            else:context.phase.state='unknown'
        return {'state':'unknown'} if admitted else {'state':'refused'}

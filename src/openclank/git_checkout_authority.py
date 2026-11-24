"""Private original-managed-utility binding for the checkout phase owner.

Unexposed until the reviewed Once receipt and Native Git writer are joined.
"""
from __future__ import annotations

import asyncio
import math
import json
import hashlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from src.openclank.account_request_barrier import UtilityLifetime
from src.openclank.git_checkout_phase import CheckoutPhase, _same
from src.secret_storage import decrypt, encrypt


@dataclass(eq=False)
class CheckoutContext:
    utility: UtilityLifetime
    phase: CheckoutPhase
    repository: Any
    reservation_generation: int
    pending_receipt: dict = field(repr=False)
    recheck: Callable[[], Awaitable[bool]] = field(repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    current_event: dict | None = field(default=None, init=False, repr=False)
    finished: bool = field(default=False, init=False)
    completion: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


async def _current(bridge: Any, utility: UtilityLifetime, context: CheckoutContext, *, recheck: bool = True) -> bool:
    try:
        pool=utility.supervisor;owner=pool._key(utility.owner)
        pool._assert_owner_lifecycle(owner,utility.epoch)
        state=pool._states.get(owner);worker=utility.worker
        identity=context.phase.identity
        return bool(type(utility) is UtilityLifetime and type(context) is CheckoutContext
            and context.utility is utility and getattr(utility,'git_checkout_context',None) is context
            and utility.root=='git-checkout' and _same(utility.idempotency_key,identity['operationID'])
            and _same(utility.request_hash,identity['requestDigest'])
            and utility.issued and not utility.settled and not utility.cancel_requested and not utility.admission.released
            and await utility.barrier.is_live(owner,utility.admission) and utility.account_current()
            and utility.lease is not None and not utility.lease._released and utility.lease.worker is worker
            and utility.lease._pool is pool and utility.lease._owner_epoch==utility.epoch
            and state is not None and state.status=='ready' and state.active is worker
            and state.generation==utility.generation and worker.is_alive()
            and worker._proc is utility.process and worker._bridge is bridge is utility.bridge
            and bridge._owner==owner and bridge._managed_provider_generation==utility.holder
            and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==utility.holder
            and getattr(worker,'installed_generation',None)==utility.generation
            and (not recheck or await context.recheck() is True))
    except Exception:
        return False


def _settle(context: CheckoutContext, *, original_exit: bool = False) -> bool:
    """CAS only the original Once-backed pending receipt after both ACKs."""
    phase=context.phase;utility=context.utility
    if not original_exit and (phase.state!='complete' or phase.final_outcome not in ('committed','compensated') or phase.cleanup not in ('complete','pending')):
        return False
    identity=phase.identity
    with context.repository._connect() as db:
        context.repository._begin(db)
        row=db.execute('SELECT request_digest,generation,receipt_ciphertext,lifecycle_phase FROM file_operations WHERE owner_subject_id=? AND operation_id=?',
            (utility.account,identity['operationID'])).fetchone()
        if (row is None or row['request_digest']!=identity['requestDigest'] or row['generation']!=context.reservation_generation
                or row['lifecycle_phase']!='git_checkout_pending'):
            db.rollback();return False
        old=row['receipt_ciphertext']
        try: pending=json.loads(decrypt(str(old)))
        except Exception: db.rollback();return False
        if pending!=context.pending_receipt:
            db.rollback();return False
        receipt=dict(pending)
        if original_exit:
            receipt.update({'state':'unknown','settled':True,'settlement':'original_operation_retired'})
        else:
            receipt.update({'state':'applied' if phase.final_outcome=='committed' else 'rolled_back','settled':True,
                'settlement':'original_git_and_files_ack','gitCheckoutResult':{'outcome':phase.final_outcome,'cleanup':phase.cleanup}})
        encoded=encrypt(json.dumps(receipt,sort_keys=True,separators=(',',':'),ensure_ascii=False))
        changed=db.execute("UPDATE file_operations SET receipt_ciphertext=?,lifecycle_phase='git_checkout_settled' WHERE owner_subject_id=? AND operation_id=? AND request_digest=? AND generation=? AND lifecycle_phase='git_checkout_pending' AND receipt_ciphertext=?",
            (encoded,utility.account,identity['operationID'],identity['requestDigest'],context.reservation_generation,old))
        if changed.rowcount!=1: db.rollback();return False
        db.commit();return True


async def dispatch(bridge: Any, request: dict) -> dict:
    """Find one exact admitted managed child; unknown never creates an owner."""
    denied={'state':'refused'}
    admitted=False
    try:
        from core.models import get_session_manager_instance
        manager=get_session_manager_instance()
        state=getattr(manager,'lifecycle_context',None) if manager else None
        barrier=getattr(state,'account_request_barrier',None)
        supervisor=getattr(state,'mimo_supervisor',None)
        pool=supervisor.owner_lifecycle_pool() if supervisor else None
        worker=pool.worker_for_owner(bridge._owner) if pool else None
        process=getattr(worker,'_proc',None)
        if not barrier or not pool or not worker or not process or type(request) is not dict: return denied
        matches=[]
        for utility in await barrier.utility_for_child(bridge._owner,worker,process):
            context=getattr(utility,'git_checkout_context',None)
            if (type(context) is CheckoutContext and type(context.phase) is CheckoutPhase
                    and all(_same(request.get(k),v) for k,v in context.phase.identity.items())):
                matches.append((utility,context))
        if len(matches)!=1: return denied
        utility,context=matches[0]
        async with context.lock:
            context.current_event=request.get('event') if type(request.get('event')) is dict else None
            if utility.barrier is not barrier or utility.supervisor is not pool or not await _current(bridge,utility,context): return denied
            # Repeat the owner/account/policy proof immediately before Files.
            async def verify(): return await _current(bridge,utility,context)
            context.phase.verify=verify
            admitted=True
            result=await context.phase.dispatch(request)
            if result.get('state')=='complete':
                if not _settle(context):
                    context.phase.state='unknown'
                    return {'state':'unknown'}
                context.finished=True;context.completion.set()
            return result
    except Exception:
        return {'state':'unknown'} if admitted else denied


async def issue_current(utility: UtilityLifetime, context: CheckoutContext, policy_current: Callable[[], bool]) -> bool:
    """Event-loop issue check; policy_current must not perform Files I/O."""
    try:
        return bool(await _current(utility.bridge,utility,context,recheck=False) and policy_current() is True)
    except Exception:
        return False


def issue_guard(check: Callable[[], Awaitable[bool]], *, deadline: float):
    """Recheck the real owner on its event loop immediately before Files I/O.

    Construct this on the admission loop and call it only from the original
    Files worker thread, as in the existing guarded personal-source owner.
    """
    if not callable(check) or type(deadline) is not float or not math.isfinite(deadline):
        raise ValueError('checkout issue guard unavailable')
    loop=asyncio.get_running_loop()
    def current_on_loop():
        async def run(): return await check() is True
        future=asyncio.run_coroutine_threadsafe(run(),loop)
        try:
            return future.result(timeout=max(0.0,deadline-loop.time())) is True
        except Exception:
            future.cancel()
            return False
    return current_on_loop


def native_checkout_action_sha256(*, session_id, chat_id, operation_id, request_digest, git_plan_digest, files_request_digest, candidate_sha256, count, preview):
    """Bind the exact Native checkout input as a fixed cross-language tuple."""
    source=preview['source'];physical=source['physicalIndex'];config=source['config'];exe=source['executable']
    def entries(values): return [[e['path'],e['mode'],e['oid']] for e in values]
    value=['openclank.git-checkout.native-action.v1',session_id,chat_id,operation_id,request_digest,git_plan_digest,files_request_digest,candidate_sha256,count,
        [physical['version'],physical['sha256'],physical['device'],physical['inode'],physical['size'],physical['objectFormat'],physical['root']['device'],physical['root']['inode'],physical['git']['device'],physical['git']['inode'],physical['objects']['device'],physical['objects']['inode']],
        [entries(source['indexEntries']),entries(source['headEntries']),source['headOID'],source['headRef']],
        [config['device'],config['inode'],config['size'],config['sha256']],
        [exe['path'],exe['device'],exe['inode'],exe['size'],exe['sha256'],exe['version']],
        [preview['operation'],preview['targetRef'],preview['targetOID'],entries(preview['targetEntries']),preview['changed']]]
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8','strict')).hexdigest()


def reserve_checkout_once(*, repository, account, location_id, workspace_id, chat_id, operation_id, request_digest, generation, expires_unix_ms, pending):
    """Reserve and consume one exact reviewed action using the existing Files owner.

    An existing operation is status-only; a failed partial admission never
    creates a replacement operation or grants retry authority.
    """
    if (type(pending) is not dict or pending.get('operation')!='git_checkout'
            or pending.get('state')!='pending' or pending.get('intentDigest')!=request_digest):
        raise ValueError('checkout admission unavailable')
    saved=json.loads(json.dumps(pending,ensure_ascii=False,allow_nan=False))
    saved['policyTransition']={'fromGeneration':generation,'toGeneration':generation}
    existing=repository.reserve_operation(owner_subject_id=account,operation_id=operation_id,request_digest=request_digest,
        generation=generation,receipt=saved,phase='git_checkout_pending')
    if existing is not None: return {'state':'status_only','existing':existing}
    once=repository.create_git_checkout_once(subject_id=account,location_id=location_id,workspace_id=workspace_id,chat_id=chat_id,
        operation_id=operation_id,request_digest=request_digest,expected_generation=generation,expires_unix_ms=expires_unix_ms)
    if once['generation']!=generation+1: raise ValueError('checkout policy changed')
    saved['policyTransition']['toGeneration']=once['generation']
    repository.record_operation(owner_subject_id=account,operation_id=operation_id,request_digest=request_digest,
        generation=generation,receipt=saved,phase='git_checkout_pending')
    consumed=repository.consume_git_checkout_once(subject_id=account,location_id=location_id,workspace_id=workspace_id,chat_id=chat_id,
        operation_id=operation_id,request_digest=request_digest,binding_id=once['binding_id'],expected_generation=once['generation'])
    if consumed['generation']!=generation+2: raise ValueError('checkout policy changed')
    saved['policyTransition']['toGeneration']=consumed['generation']
    repository.record_operation(owner_subject_id=account,operation_id=operation_id,request_digest=request_digest,
        generation=generation,receipt=saved,phase='git_checkout_pending')
    return {'state':'admitted','binding_id':once['binding_id'],'generation':consumed['generation'],'pending':saved}

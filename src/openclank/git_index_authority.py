"""Private lifetime fence for one already-admitted Git index operation.

The context is attached to an existing UtilityLifetime by the host caller;
it is not a wire credential, registry, or independent source of authority.
"""
from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from src.openclank.account_request_barrier import UtilityLifetime
from src.secret_storage import decrypt, encrypt


@dataclass(eq=False)
class GitIndexContext:
    utility: UtilityLifetime
    repository: Any
    operation_id: str
    request_digest: str
    capability: str = field(repr=False)
    expected_action_sha256: str
    candidate_token: str | None = field(repr=False)
    reservation_generation: int
    policy_generation: int
    pending_receipt: dict = field(repr=False)
    recheck: Callable[..., Awaitable[bool]] = field(repr=False)
    profile: str = 'index'
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    completion: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    invocation: str | None = field(default=None, init=False, repr=False)
    phase: int = field(default=0, init=False)
    finished: bool = field(default=False, init=False)


def _same(a: object, b: object) -> bool:
    return isinstance(a, str) and isinstance(b, str) and hmac.compare_digest(a, b)


async def _current(bridge: Any, utility: UtilityLifetime, context: GitIndexContext) -> bool:
    try:
        pool = utility.supervisor
        owner = pool._key(utility.owner)
        pool._assert_owner_lifecycle(owner, utility.epoch)
        state = pool._states.get(owner)
        worker = utility.worker
        return bool(type(utility) is UtilityLifetime and type(context) is GitIndexContext
            and context.utility is utility and getattr(utility, 'git_index_context', None) is context
            and context.profile in ('index','commit','cleanup','branch_create','fetch','push') and utility.root == {'index':'git-index','commit':'git-commit','cleanup':'git-cleanup','branch_create':'git-branch-create','fetch':'git-fetch','push':'git-push'}[context.profile]
            and _same(utility.idempotency_key, context.operation_id)
            and _same(utility.request_hash, context.request_digest)
            and utility.issued and not utility.settled and not utility.admission.released
            and await utility.barrier.is_live(owner, utility.admission) and utility.account_current()
            and utility.lease is not None and not utility.lease._released
            and utility.lease.worker is worker and utility.lease._pool is pool
            and utility.lease._owner_epoch == utility.epoch
            and state is not None and state.status == 'ready' and state.active is worker
            and state.generation == utility.generation and worker.is_alive()
            and worker._proc is utility.process and worker._bridge is bridge is utility.bridge
            and bridge._owner == owner and bridge._managed_provider_generation == utility.holder
            and getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) == utility.holder
            and getattr(worker, 'installed_generation', None) == utility.generation)
    except Exception:
        return False


def _identity(context: GitIndexContext, request: dict) -> bool:
    return (_same(request.get('operationID'), context.operation_id)
        and _same(request.get('requestDigest'), context.request_digest)
        and _same(request.get('capability'), context.capability)
        and _same(request.get('actionSHA256'), context.expected_action_sha256))


def native_action_sha256(*, session_id: str, chat_id: str, operation_id: str,
        request_digest: str, candidate_token: str, physical: dict, action: dict, stage_bytes: bytes | None) -> str:
    """Hash the exact native parsed action; the caller supplies server-owned fields."""
    operation = action['operation']
    if operation not in ('stage', 'unstage') or (operation == 'stage') != (type(stage_bytes) is bytes):
        raise ValueError('Git action unavailable')
    if operation == 'stage' and len(stage_bytes) > 262144:
        raise ValueError('Git action unavailable')
    p = physical
    values = ['openclank.git-index.native-action.v1', session_id, chat_id, operation_id, request_digest,
        candidate_token, [p['version'],p['sha256'],p['device'],p['inode'],p['size'],p['objectFormat'],
            p['root']['device'],p['root']['inode'],p['git']['device'],p['git']['inode'],p['objects']['device'],p['objects']['inode']],
        [operation,action['path'],action['mode'],hashlib.sha256(stage_bytes).hexdigest() if operation == 'stage' else None,
            action['headOID'] if operation == 'unstage' else None]]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8','strict')).hexdigest()


def native_commit_action_sha256(*, session_id: str, chat_id: str, operation_id: str,
        request_digest: str, candidate_token: str, preview: dict, action: dict) -> str:
    """Hash the exact fixed native commit tuple, without browser authority."""
    p = preview['physicalIndex']; config = preview['config']; executable = preview['executable']
    def entries(name): return [[e['path'],e['mode'],e['oid']] for e in preview[name]]
    def identity(name):
        value = action[name]
        return [value['name'],value['email'],value['seconds'],value['timezone']]
    values = ['openclank.git-commit.native-action.v1',session_id,chat_id,operation_id,request_digest,candidate_token,
        [p['version'],p['sha256'],p['device'],p['inode'],p['size'],p['objectFormat'],p['root']['device'],p['root']['inode'],
            p['git']['device'],p['git']['inode'],p['objects']['device'],p['objects']['inode']],
        [entries('indexEntries'),entries('headEntries'),preview['headOID'],preview['headRef']],
        [config['device'],config['inode'],config['size'],config['sha256']],
        [executable['path'],executable['device'],executable['inode'],executable['size'],executable['sha256'],executable['version']],
        [action['message'],identity('author'),identity('committer')]]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8','strict')).hexdigest()

def native_cleanup_action_sha256(*, session_id: str, chat_id: str, operation_id: str,
        request_digest: str, cleanup_digest: str, witness: dict) -> str:
    values=['openclank.git-cleanup.native-action.v1',session_id,chat_id,operation_id,request_digest,cleanup_digest,witness]
    return hashlib.sha256(json.dumps(values,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8','strict')).hexdigest()


def native_branch_action_sha256(*, session_id: str, chat_id: str, operation_id: str,
        request_digest: str, preview: dict, ref: str) -> str:
    p=preview['physicalIndex']; config=preview['config']; exe=preview['executable']
    def entries(name): return [[e['path'],e['mode'],e['oid']] for e in preview[name]]
    values=['openclank.git-branch-create.native-action.v1',session_id,chat_id,operation_id,request_digest,
        [p['version'],p['sha256'],p['device'],p['inode'],p['size'],p['objectFormat'],p['root']['device'],p['root']['inode'],p['git']['device'],p['git']['inode'],p['objects']['device'],p['objects']['inode']],
        [entries('indexEntries'),entries('headEntries'),preview['headOID'],preview['headRef']],
        [config['device'],config['inode'],config['size'],config['sha256']],
        [exe['path'],exe['device'],exe['inode'],exe['size'],exe['sha256'],exe['version']],ref]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8','strict')).hexdigest()


def native_fetch_action_sha256(*, session_id: str, chat_id: str, operation_id: str, request_digest: str,
        preview: dict, remote_url: str, tracking_ref: str, old_oid: str | None, target_oid: str,
        local_remote: dict | None) -> str:
    p=preview['physicalIndex']; config=preview['config']; exe=preview['executable']
    def entries(name): return [[e['path'],e['mode'],e['oid']] for e in preview[name]]
    values=['openclank.git-fetch.native-action.v1',session_id,chat_id,operation_id,request_digest,
        [p['version'],p['sha256'],p['device'],p['inode'],p['size'],p['objectFormat'],p['root']['device'],p['root']['inode'],p['git']['device'],p['git']['inode'],p['objects']['device'],p['objects']['inode']],
        [entries('indexEntries'),entries('headEntries'),preview['headOID'],preview['headRef']],
        [config['device'],config['inode'],config['size'],config['sha256']],
        [exe['path'],exe['device'],exe['inode'],exe['size'],exe['sha256'],exe['version']],
        hashlib.sha256(remote_url.encode('utf-8','strict')).hexdigest(),tracking_ref,old_oid,target_oid,
        None if local_remote is None else [local_remote['device'],local_remote['inode']]]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8','strict')).hexdigest()


def native_push_action_sha256(*, session_id: str, chat_id: str, operation_id: str, request_digest: str,
        preview: dict, remote_url: str, destination_ref: str, observed_oid: str | None,
        local_remote: dict | None) -> str:
    p=preview['physicalIndex']; config=preview['config']; exe=preview['executable']
    def entries(name): return [[e['path'],e['mode'],e['oid']] for e in preview[name]]
    values=['openclank.git-push.native-action.v1',session_id,chat_id,operation_id,request_digest,
        [p['version'],p['sha256'],p['device'],p['inode'],p['size'],p['objectFormat'],p['root']['device'],p['root']['inode'],p['git']['device'],p['git']['inode'],p['objects']['device'],p['objects']['inode']],
        [entries('indexEntries'),entries('headEntries'),preview['headOID'],preview['headRef']],
        [config['device'],config['inode'],config['size'],config['sha256']],
        [exe['path'],exe['device'],exe['inode'],exe['size'],exe['sha256'],exe['version']],
        hashlib.sha256(remote_url.encode('utf-8','strict')).hexdigest(),destination_ref,observed_oid,
        None if local_remote is None else [local_remote['device'],local_remote['inode']]]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8','strict')).hexdigest()


def _settle(context: GitIndexContext, result: dict | None, *, host_exit: bool = False) -> bool:
    """CAS the exact original encrypted pending receipt, never create a row."""
    if (host_exit and result is not None) or (not host_exit and type(result) is not dict):
        return False
    repo = context.repository
    with repo._connect() as db:
        repo._begin(db)
        row = db.execute('SELECT request_digest,generation,receipt_ciphertext,lifecycle_phase FROM file_operations WHERE owner_subject_id=? AND operation_id=?',
            (context.utility.account, context.operation_id)).fetchone()
        if context.profile not in ('index','commit','cleanup','branch_create','fetch','push'):
            db.rollback(); return False
        phase = {'index':'git_index_pending','commit':'git_commit_pending','cleanup':'git_cleanup_pending','branch_create':'git_branch_create_pending','fetch':'git_fetch_pending','push':'git_push_pending'}[context.profile]
        settled_phase = {'index':'git_index_settled','commit':'git_commit_settled','cleanup':'git_cleanup_settled','branch_create':'git_branch_create_settled','fetch':'git_fetch_settled','push':'git_push_settled'}[context.profile]
        result_key = {'index':'gitIndexResult','commit':'gitCommitResult','cleanup':'gitCleanupResult','branch_create':'gitBranchCreateResult','fetch':'gitFetchResult','push':'gitPushResult'}[context.profile]
        if (row is None or row['request_digest'] != context.request_digest or row['generation'] != context.reservation_generation
                or row['lifecycle_phase'] != phase):
            db.rollback(); return False
        old = row['receipt_ciphertext']
        try:
            pending = json.loads(decrypt(str(old)))
        except Exception:
            db.rollback(); return False
        if pending != context.pending_receipt or (context.profile not in ('cleanup','branch_create','fetch','push') and pending.get('candidateToken') != context.candidate_token) or (context.profile in ('cleanup','branch_create','fetch','push') and ('candidateToken' in pending or context.candidate_token is not None)):
            db.rollback(); return False
        receipt = dict(pending)
        if host_exit:
            receipt.update({'state':'unknown','settled':True,'settlement':'original_child_exit'})
        else:
            receipt.update({'state': result['state'], result_key: result, 'settled': True})
        encoded = encrypt(json.dumps(receipt, sort_keys=True, separators=(',', ':'), ensure_ascii=False))
        changed = db.execute("UPDATE file_operations SET receipt_ciphertext=?,lifecycle_phase=? WHERE owner_subject_id=? AND operation_id=? AND request_digest=? AND generation=? AND lifecycle_phase=? AND receipt_ciphertext=?",
            (encoded, settled_phase, context.utility.account, context.operation_id, context.request_digest, context.reservation_generation, phase, old))
        if changed.rowcount != 1:
            db.rollback(); return False
        db.commit(); return True


def record_cleanup_host_retirement(context: GitIndexContext, state: str) -> bool:
    """CAS the original Host retirement phase; caller holds context.lock."""
    if context.profile!='cleanup' or state not in ('unknown','applied','not_issued'): return False
    previous=context.pending_receipt.get('hostRetirement','not_issued')
    if previous==state: return True
    if (previous,state) not in (('not_issued','unknown'),('unknown','applied'),('unknown','not_issued')): return False
    with context.repository._connect() as db:
        context.repository._begin(db)
        row=db.execute('SELECT request_digest,generation,receipt_ciphertext,lifecycle_phase FROM file_operations WHERE owner_subject_id=? AND operation_id=?',(context.utility.account,context.operation_id)).fetchone()
        if not row or row['request_digest']!=context.request_digest or row['generation']!=context.reservation_generation or row['lifecycle_phase']!='git_cleanup_pending': db.rollback(); return False
        old=row['receipt_ciphertext']
        try: pending=json.loads(decrypt(str(old)))
        except Exception: db.rollback(); return False
        if pending!=context.pending_receipt or 'candidateToken' in pending or context.candidate_token is not None: db.rollback(); return False
        receipt={**pending,'hostRetirement':state}
        encoded=encrypt(json.dumps(receipt,sort_keys=True,separators=(',',':'),ensure_ascii=False))
        changed=db.execute('UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=? AND request_digest=? AND generation=? AND lifecycle_phase=? AND receipt_ciphertext=?',(encoded,context.utility.account,context.operation_id,context.request_digest,context.reservation_generation,'git_cleanup_pending',old))
        if changed.rowcount!=1: db.rollback(); return False
        db.commit();context.pending_receipt=receipt;return True

def _record_cleanup_unknown(context: GitIndexContext, result: dict) -> bool:
    with context.repository._connect() as db:
        context.repository._begin(db)
        row=db.execute('SELECT request_digest,generation,receipt_ciphertext,lifecycle_phase FROM file_operations WHERE owner_subject_id=? AND operation_id=?',(context.utility.account,context.operation_id)).fetchone()
        if not row or row['request_digest']!=context.request_digest or row['generation']!=context.reservation_generation or row['lifecycle_phase']!='git_cleanup_pending': db.rollback(); return False
        old=row['receipt_ciphertext']
        try: pending=json.loads(decrypt(str(old)))
        except Exception: db.rollback(); return False
        if pending!=context.pending_receipt or 'candidateToken' in pending or context.candidate_token is not None: db.rollback(); return False
        previous=pending.get('gitCleanupUncertain')
        if previous is not None:
            db.rollback(); return previous==result
        receipt={**pending,'gitCleanupUncertain':result}
        encoded=encrypt(json.dumps(receipt,sort_keys=True,separators=(',',':'),ensure_ascii=False))
        changed=db.execute('UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=? AND request_digest=? AND generation=? AND lifecycle_phase=? AND receipt_ciphertext=?',(encoded,context.utility.account,context.operation_id,context.request_digest,context.reservation_generation,'git_cleanup_pending',old))
        if changed.rowcount!=1: db.rollback(); return False
        db.commit(); context.pending_receipt=receipt; return True

async def dispatch(bridge: Any, request: dict, *, finish: bool, profile: str = 'index') -> dict:
    """Find the exact original utility and serialize its callbacks."""
    denied = {'accepted': False} if finish else {'valid': False}
    try:
        from core.models import get_session_manager_instance
        manager = get_session_manager_instance()
        state = getattr(manager, 'lifecycle_context', None) if manager else None
        barrier = getattr(state, 'account_request_barrier', None)
        supervisor = getattr(state, 'mimo_supervisor', None)
        pool = supervisor.owner_lifecycle_pool() if supervisor else None
        worker = pool.worker_for_owner(bridge._owner) if pool else None
        process = getattr(worker, '_proc', None)
        if not barrier or not pool or not worker or not process:
            return denied
        if profile not in ('index','commit','cleanup','branch_create','fetch','push'): return denied
        matches = []
        for utility in await barrier.utility_for_child(bridge._owner, worker, process):
            context = getattr(utility, 'git_index_context', None)
            if type(context) is GitIndexContext and context.profile == profile and _identity(context, request):
                matches.append((utility, context))
        if len(matches) != 1:
            return denied
        utility, context = matches[0]
        async with context.lock:
            if (utility.barrier is not barrier or utility.supervisor is not pool
                    or utility.auth is not getattr(state, 'auth_manager', None)
                    or context.repository is not getattr(state, 'files_policy_repository', None)
                    or context.finished or not callable(context.recheck)
                    or not await _current(bridge, utility, context)):
                return denied
            invocation = request.get('invocationID')
            if type(invocation) is not str or not re.fullmatch(r'[0-9a-f]{32}', invocation):
                return denied
            if finish:
                result = request['result']
                if context.invocation is None:
                    initial = ({'state':'refused','object':'none','candidateRetained':False} if profile == 'index' else
                        {'state':'refused','tree':'none','commit':'none','ref':'not_issued','candidateRetained':False,'cleanup':'complete'} if profile == 'commit' else
                        {'state':'refused','retirement':'not_issued','removal':'not_issued','child':'not_issued','branch':'unknown'} if profile == 'cleanup' else
                        {'state':'refused','ref':'not_issued','cleanup':'complete'} if profile == 'branch_create' else
                        {'state':'refused','transfer':'not_issued','ref':'not_issued','cleanup':'complete'} if profile == 'fetch' else
                        {'state':'refused','remote':'not_issued','cleanup':'complete'})
                    if context.phase != 0 or result != initial:
                        return denied
                    context.invocation = invocation
                if not _same(context.invocation, invocation):
                    return denied
                if result['state'] == 'applied' and context.phase != (4 if profile == 'commit' else 2 if profile in ('branch_create','fetch') else 1 if profile == 'push' else 3):
                    return denied
                if profile == 'commit':
                    if ((result['state'] == 'applied') != (result['ref'] == 'applied')
                            or (result['state'] == 'unknown') != (result['ref'] == 'unknown')
                            or (result['state'] == 'applied' and (result['tree'] != 'written_or_present'
                                or result['commit'] != 'written_or_present' or not result.get('commitOID')))):
                        return denied
                if profile == 'push' and ((result['state'] == 'applied') != (result['remote'] == 'accepted') or (result['state'] == 'unknown') != (result['remote'] == 'unknown')):
                    return denied
                if profile == 'fetch' and ((result['state'] == 'applied') != (result['transfer'] == 'completed' and result['ref'] == 'applied') or (result['state'] == 'unknown') != (result['transfer'] == 'unknown' or result['ref'] == 'unknown')):
                    return denied
                if profile == 'branch_create' and ((result['state'] == 'applied') != (result['ref'] == 'applied') or (result['state'] == 'unknown') != (result['ref'] == 'unknown')):
                    return denied
                if profile == 'cleanup':
                    if 'gitCleanupUncertain' in context.pending_receipt:
                        return {'accepted':context.pending_receipt['gitCleanupUncertain']==result}
                    if (result['state']=='applied' and (result['retirement']!='applied' or result['removal']!='applied' or result['child']!='settled' or result['branch']!='present')) or (result['child']=='unknown' and result['state']!='unknown'):
                        return denied
                    if result['child']=='unknown':
                        if not await _current(bridge,utility,context) or not _record_cleanup_unknown(context,result): return denied
                        return {'accepted':True}
                candidate = result.get('candidateIdentity')
                if candidate is not None and (not result['candidateRetained'] or not _same(candidate.get('token'), context.candidate_token)):
                    return denied
                if not await _current(bridge, utility, context):
                    return denied
                if not _settle(context, result):
                    return denied
                context.finished = True
                context.completion.set()
                return {'accepted': True}
            phases = (('before_lock', 'before_object', 'before_publish') if profile == 'index' else
                ('before_lock','before_tree','before_commit','before_publish') if profile == 'commit' else ('before_lock','before_publish') if profile == 'branch_create' else ('before_transfer','before_publish') if profile == 'fetch' else ('before_push',) if profile == 'push' else ('before_close','before_retire','before_remove'))
            if utility.cancel_requested:
                return denied
            if context.phase >= len(phases) or request.get('phase') != phases[context.phase]:
                return denied
            if context.invocation is None:
                if context.phase != 0:
                    return denied
                context.invocation = invocation
            if not _same(context.invocation, invocation) or not callable(context.recheck):
                return denied
            next_phase = context.phase + 1
            # Claim before awaiting. Failure or cancellation must not allow
            # this invocation (or a duplicate HTTP call) to try the phase again.
            context.phase = len(phases) + 1
            if (await (context.recheck(request['phase']) if profile == 'cleanup' else context.recheck()) is not True or utility.cancel_requested
                    or not await _current(bridge, utility, context) or utility.cancel_requested):
                return denied
            context.phase = next_phase
            return {'valid': True}
    except Exception:
        return denied

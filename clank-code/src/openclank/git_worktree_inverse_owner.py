"""Private, once-admitted preparation of one original worktree inverse.

No route or worktree effect is exposed here. A later Source callback must keep
this exact utility and custody alive through any Git and Files mutation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Awaitable, Callable

from src.openclank.account_request_barrier import UtilityLifetime
from src.openclank.files_service_client import _WorktreeStageCustody
from src.openclank.git_checkout_authority import issue_guard
from src.openclank.git_worktree_receipts import record_cleanup
from src.openclank.git_worktree_stage_phase import _encoded, _inventory


def reviewed_inverse_digest(*, account, location_id, workspace_id, chat_id, operation_id,
                            original_operation_id, original_version_id, native_id,
                            source_path, root_path, root_object_tag, preview):
    begin = {'kind': 'begin', 'targetOID': preview['targetOID'], 'treeOID': preview['treeOID'],
             'entries': preview['entries'], 'directories': preview['directories'], 'totalBytes': preview['totalBytes']}
    plan = _inventory(begin)
    value = ['openclank.git-worktree.inverse.v1', account, location_id, workspace_id, chat_id,
             operation_id, original_operation_id, original_version_id, native_id, source_path,
             root_path, root_object_tag, plan]
    return hashlib.sha256(_encoded(value)).hexdigest(), plan


@dataclass(eq=False)
class WorktreeInverseContext:
    utility: UtilityLifetime
    custody: _WorktreeStageCustody
    identity: dict[str, str]
    binding: dict[str, str]
    original_operation_id: str
    original_version_id: str
    recheck: Callable[[], Awaitable[bool]] = field(repr=False)
    policy_current: Callable[[], bool] = field(repr=False)
    deadline: float
    repository: Any = field(repr=False)
    reservation_generation: int = field(repr=False)
    pending_receipt: dict[str, Any] = field(repr=False)
    preview: dict[str, Any] = field(repr=False)
    state: str = 'pending'
    original: dict[str, Any] | None = field(default=None, repr=False)
    invocation: str | None = None
    source_proof: dict[str, Any] | None = field(default=None, repr=False)
    source_task: asyncio.Task | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


async def inverse_current(utility: UtilityLifetime, context: WorktreeInverseContext) -> bool:
    """Revalidate the original human, lease, worker, process and policy owner."""
    try:
        pool = utility.supervisor
        owner = pool._key(utility.owner)
        pool._assert_owner_lifecycle(owner, utility.epoch)
        state, worker, bridge = pool._states.get(owner), utility.worker, utility.bridge
        same = lambda a, b: type(a) is str and type(b) is str and hmac.compare_digest(a, b)
        return bool(type(utility) is UtilityLifetime and type(context) is WorktreeInverseContext
            and context.utility is utility and getattr(utility, 'git_worktree_inverse_context', None) is context
            and utility.root == 'git-worktree-inverse' and utility.issued and not utility.settled
            and same(utility.idempotency_key, context.identity['operationID'])
            and same(utility.request_hash, context.identity['requestDigest'])
            and not utility.cancel_requested and not utility.admission.released
            and await utility.barrier.is_live(owner, utility.admission) and utility.account_current()
            and utility.lease is not None and not utility.lease._released and utility.lease.worker is worker
            and utility.lease._pool is pool and utility.lease._owner_epoch == utility.epoch
            and state is not None and state.status == 'ready' and state.active is worker
            and state.generation == utility.generation and worker.is_alive() and worker._proc is utility.process
            and worker._bridge is bridge and bridge._owner == owner
            and bridge._managed_provider_generation == utility.holder
            and getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) == utility.holder
            and getattr(worker, 'installed_generation', None) == utility.generation
            and context.policy_current() is True and await context.recheck() is True
            and asyncio.get_running_loop().time() < context.deadline)
    except Exception:
        return False


async def prepare_inverse(*, utility, repository, client, location_id, workspace_id, chat_id,
                          native_id, source_path, root_path, root_object_tag, preview,
                          original_operation_id, original_version_id, expected_digest, current, product_input_sha=None):
    """Prepare one inverse History version; callers may only inspect unknown afterward."""
    if (type(utility) is not UtilityLifetime or utility.root != 'git-worktree-inverse'
            or utility.issued or utility.settled or utility.cancel_requested or not callable(current)
            or getattr(utility, 'git_worktree_inverse_context', None) is not None):
        raise ValueError('worktree inverse unavailable')
    account, operation_id = utility.account, utility.idempotency_key
    if (any(type(v) is not str or not v or len(v.encode('utf-8', 'strict')) > 256 for v in
            (account, location_id, chat_id, native_id, original_operation_id, original_version_id))
            or original_operation_id == operation_id
            or any(type(v) is not str or not v.startswith('/') or len(v.encode('utf-8', 'strict')) > 4096
                   for v in (source_path, root_path))):
        raise ValueError('worktree inverse unavailable')
    original = repository.get_operation(owner_subject_id=account, operation_id=original_operation_id)
    prior = original.get('receipt') if type(original) is dict else None
    if (type(prior) is not dict or prior.get('operation') != 'git_worktree_stage'
            or prior.get('locationID') != location_id or prior.get('workspaceID') != workspace_id
            or prior.get('chatID') != chat_id):
        raise ValueError('worktree original unavailable')
    digest, plan = reviewed_inverse_digest(account=account, location_id=location_id, workspace_id=workspace_id,
        chat_id=chat_id, operation_id=operation_id, original_operation_id=original_operation_id,
        original_version_id=original_version_id, native_id=native_id, source_path=source_path,
        root_path=root_path, root_object_tag=root_object_tag, preview=preview)
    if (type(expected_digest) is not str or type(utility.request_hash) is not str
            or not hmac.compare_digest(digest, expected_digest) or not hmac.compare_digest(digest, utility.request_hash)):
        raise ValueError('worktree inverse review changed')
    loop = asyncio.get_running_loop(); deadline = loop.time() + 300
    generation = repository.generation(); expires = int(time.time() * 1000) + 299000
    if not utility.account_current() or await current(generation) is not True:
        raise ValueError('worktree inverse authority changed')
    pending = {'version': 1, 'state': 'pending', 'operation': 'git_worktree_inverse', 'intentDigest': digest,
               'chatID': chat_id, 'locationID': location_id, 'workspaceID': workspace_id,
               'originalOperationID': original_operation_id, 'originalVersionID': original_version_id,
               'policyTransition': {'fromGeneration': generation, 'toGeneration': generation}}
    if product_input_sha is not None:
        if type(product_input_sha) is not str or not re.fullmatch(r'[0-9a-f]{64}',product_input_sha):raise ValueError('worktree selector unavailable')
        pending['productInputSHA256']=product_input_sha
    # Product-visible selectors inherit only owner-protected review metadata.
    if all(key in prior for key in ('sourcePathSHA256','targetOID','destination')):
        pending.update({key:prior[key] for key in ('sourcePathSHA256','targetOID','destination')})
    existing = repository.reserve_operation(owner_subject_id=account, operation_id=operation_id,
        request_digest=digest, generation=generation, receipt=pending, phase='git_worktree_inverse_pending')
    if existing is not None:
        return {'state': 'status_only'}
    once = repository.create_git_worktree_inverse_once(subject_id=account, location_id=location_id,
        workspace_id=workspace_id, chat_id=chat_id, operation_id=operation_id, request_digest=digest,
        expected_generation=generation, expires_unix_ms=expires)
    if once['generation'] != generation + 1:
        raise ValueError('worktree inverse policy changed')
    pending['policyTransition']['toGeneration'] = once['generation']
    repository.record_operation(owner_subject_id=account, operation_id=operation_id, request_digest=digest,
        generation=generation, receipt=pending, phase='git_worktree_inverse_pending')
    consumed = repository.consume_git_worktree_inverse_once(subject_id=account, location_id=location_id,
        workspace_id=workspace_id, chat_id=chat_id, operation_id=operation_id, request_digest=digest,
        binding_id=once['binding_id'], expected_generation=once['generation'])
    active_generation = consumed['generation']
    if active_generation != generation + 2:
        raise ValueError('worktree inverse policy changed')
    pending['policyTransition']['toGeneration'] = active_generation
    repository.record_operation(owner_subject_id=account, operation_id=operation_id, request_digest=digest,
        generation=generation, receipt=pending, phase='git_worktree_inverse_pending')
    def policy_current():
        try:
            row = repository.get_binding(once['binding_id'])
            return bool(repository.generation() == active_generation and row.status == 'consumed'
                and row.remaining_uses == 0 and row.generation == active_generation and row.revision == 2
                and row.subject_id == account and row.location_id == location_id and row.workspace_id == workspace_id
                and row.chat_id == chat_id and row.resource_ref == 'git-worktree-inverse-action:sha256:' + digest
                and row.operation == 'git_worktree_inverse')
        except Exception:
            return False
    async def fresh():
        return await current(active_generation) is True and policy_current()
    if not await fresh():
        raise ValueError('worktree inverse authority changed')
    capability = secrets.token_hex(32)
    action = hashlib.sha256(_encoded(['openclank.git-worktree.inverse-action.v1', native_id, chat_id,
        operation_id, digest, plan, original_operation_id, original_version_id])).hexdigest()
    identity = {'operationID': operation_id, 'requestDigest': digest, 'capability': capability,
                'actionSHA256': action, 'gitPlanDigest': plan}
    binding = {'operation_id': operation_id, 'request_digest': digest, 'git_plan_digest': plan,
               'root_id': location_id, 'root_object_tag': root_object_tag}
    custody = await _WorktreeStageCustody.acquire(client(active_generation) if callable(client) else client, root_path, operation_id,
        before_issue=issue_guard(fresh, deadline=deadline), profile='inverse')
    context = WorktreeInverseContext(utility, custody, identity, binding, original_operation_id,
        original_version_id, fresh, policy_current, deadline, repository, generation,
        json.loads(_encoded(pending)), json.loads(_encoded(preview)))
    utility.git_worktree_inverse_context = context
    custody.before_issue = issue_guard(lambda: inverse_current(utility, context), deadline=deadline)
    utility.issued = True
    try:
        begin = {**binding, 'original_operation_id': original_operation_id,
                 'original_version_id': original_version_id, 'root_path': root_path}
        admitted = (await custody.exchange('worktree_inverse_begin', begin)).get('data')
        if (type(admitted) is not dict or set(admitted) != {'state', 'original'} or admitted['state'] != 'accepted'
                or type(admitted['original']) is not dict or set(admitted['original']) != {'registration', 'finalBacklink', 'targetOID', 'treeOID', 'destinationPath'}
                or admitted['original']['targetOID'] != preview['targetOID'] or admitted['original']['treeOID'] != preview['treeOID']):
            raise ValueError('worktree inverse unavailable')
        context.original = admitted['original']
        for operation in ('worktree_inverse_prepare_history', 'worktree_inverse_stage_history'):
            remaining = min(300000, max(0, int((deadline-loop.time())*1000)))
            if remaining <= 0 or not await inverse_current(utility, context):
                raise ValueError('worktree inverse authority changed')
            if (await custody.exchange(operation, {**binding, 'timeout_ms': remaining})).get('data') != {'state': 'history_prepared'}:
                raise ValueError('worktree inverse unavailable')
        if not await inverse_current(utility, context):
            raise ValueError('worktree inverse authority changed')
        context.state = 'history_prepared'
        return {'state': 'history_prepared'}
    except asyncio.CancelledError:
        context.state = 'unknown'; utility.cancel_requested = True; utility.host_cleanup_unknown = True
        raise
    except Exception:
        context.state = 'unknown'; utility.host_cleanup_unknown = True
        return {'state': 'unknown'}


async def run_inverse(*, utility, chat_id: str, native_id: str, workspace: str):
    """Issue exactly one Source child request under the original retained utility."""
    context = getattr(utility, 'git_worktree_inverse_context', None)
    if (type(context) is not WorktreeInverseContext or context.state != 'history_prepared'
            or context.source_task is not None or not await inverse_current(utility, context)):
        return {'state': 'unknown'}
    original = context.original
    if type(original) is not dict or type(original.get('destinationPath')) is not str:
        context.state = 'unknown'; utility.host_cleanup_unknown = True
        return {'state': 'unknown'}
    remaining = context.deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        context.state = 'unknown'; utility.host_cleanup_unknown = True
        return {'state': 'unknown'}
    payload = {'version': 1, 'stableChatID': chat_id, **context.identity,
               'originalOperationID': context.original_operation_id, 'originalVersionID': context.original_version_id,
               'targetOID': original['targetOID'], 'expectedPreview': context.preview,
               'destinationPath': original['destinationPath'], 'registration': original['registration'],
               'finalBacklink': original['finalBacklink'], 'timeoutMs': max(1, min(300000, int(remaining*1000)))}
    # actionSHA256 is recomputed by the retained Source child, not sent as a route field.
    payload.pop('actionSHA256')
    call = utility.worker.session_git_worktree_inverse_request(chat_id, native_id, workspace, payload,
        owner=utility.owner, timeout=remaining)
    context.source_task = asyncio.create_task(call)
    try:
        response = await asyncio.wait_for(asyncio.shield(context.source_task), timeout=remaining)
        body = response.json() if response.status_code == 200 else None
        if (type(body) is dict and body.get('state') == 'completed' and context.state == 'completed'
                and await inverse_current(utility, context)):
            try:
                released=(await context.custody.exchange('worktree_release_completed',context.binding)).get('data')
                if released=={'state':'released'} and record_cleanup(context.repository,utility,context.reservation_generation,'git_worktree_inverse','released'):return {'state':'completed','cleanup':'released'}
            except Exception:pass
            record_cleanup(context.repository,utility,context.reservation_generation,'git_worktree_inverse','unknown')
            utility.host_cleanup_unknown=True;return {'state':'completed','cleanup':'unknown'}
    except asyncio.CancelledError:
        context.state = 'unknown'; utility.cancel_requested = True; utility.host_cleanup_unknown = True
        raise
    except Exception:
        pass
    context.state = 'unknown'; utility.host_cleanup_unknown = True
    return {'state': 'unknown'}

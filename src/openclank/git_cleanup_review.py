"""Named-human review and original-operation custody for worktree cleanup.

A preview is a point-in-time observation, never an approval or a lease.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import stat
import time
import uuid

from src.openclank.files_facade import ProviderContext
from src.openclank.files_host_provider import _path
from src.openclank.resource_refs import resolve_resource_ref
from src.openclank.transcript_projection import (
    get_managed_binding, get_mimo_state, snapshot_directory_execution_scopes,
)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8', 'strict')).hexdigest()


def _directory_identity(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        held, named = os.fstat(fd), os.lstat(path)
        if (not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(named.st_mode)
                or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)
                or not 0 <= held.st_dev <= 9007199254740991
                or not 0 < held.st_ino <= 9007199254740991):
            raise ValueError('cleanup root identity unavailable')
        return {'device':held.st_dev, 'inode':held.st_ino}
    finally:
        os.close(fd)


def _map_snapshot(bridge, owner, target, host_scopes):
    """Refuse map-only, quarantined, or inconsistent current bindings."""
    inventory = bridge._durable_session_map.cleanup_inventory()
    if inventory.get('quarantine'):
        raise ValueError('quarantined session map unavailable')
    selected = {chat: binding for chat, binding, _ in host_scopes}
    for chat, entry in inventory['chats'].items():
        state = get_mimo_state(chat, owner=owner)
        marker = state.get('directory_retired_by') if isinstance(state, dict) else None
        if marker is not None:
            if type(marker) is not str or not re.fullmatch(r'[0-9a-f]{64}', marker):
                raise ValueError('session retirement unavailable')
            continue
        binding = state.get('managed_binding') if isinstance(state, dict) else None
        if (not isinstance(binding, dict) or binding.get('owner') != owner
                or binding.get('stableChatID') != chat
                or binding.get('engineSessionID') != entry['current']
                or binding.get('mappingRevision') != entry['revision']):
            raise ValueError('session map binding unavailable')
        cwd = binding.get('physicalCwd')
        if (not isinstance(cwd, str) or not os.path.isabs(cwd)
                or os.path.realpath(cwd) != cwd):
            raise ValueError('session map directory unavailable')
        if os.path.commonpath((target, cwd)) == target and selected.get(chat) != binding:
            raise ValueError('session map scope unavailable')
    return inventory


def _validate_native(value, target):
    if (type(value) is not dict or set(value) != {'version', 'state', 'gitCleanupControlV1', 'witness'}
            or type(value['version']) is not int or value['version'] != 1
            or value['state'] != 'ready' or value['gitCleanupControlV1'] is not True):
        raise ValueError('cleanup preview unavailable')
    witness = value['witness']
    keys = {'state','project','directory','branch','headOID','trackedSHA256','device','inode',
        'commonDir','commonDevice','commonInode','adminDir','adminDevice','adminInode',
        'gitfile','reciprocal','config','executable','scopes'}
    if (type(witness) is not dict or set(witness) != keys or witness['state'] != 'ready'
            or witness['directory'] != target or type(witness['branch']) is not str
            or not witness['branch'] or len(witness['branch'].encode('utf-8','strict')) > 1024
            or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', witness['headOID'] or '')
            or not re.fullmatch(r'[0-9a-f]{64}', witness['trackedSHA256'] or '')):
        raise ValueError('cleanup witness unavailable')
    scopes = witness['scopes']
    if (type(scopes) is not dict or set(scopes) != {'directory','sessions','actors'}
            or scopes['directory'] != target or type(scopes['sessions']) is not list
            or type(scopes['actors']) is not list or len(scopes['sessions']) > 512
            or len(scopes['actors']) > 512):
        raise ValueError('cleanup scopes unavailable')
    # The private native service owns the detailed strict schema and checks;
    # the exact bounded value is retained in the reviewed digest.
    encoded = json.dumps(witness, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8', 'strict')
    if len(encoded) > 1024 * 1024:
        raise ValueError('cleanup witness too large')
    return witness


def public_effect(receipt):
    host = receipt.get('hostRetirement', 'not_issued')
    if host not in ('not_issued','applied','unknown'): return {}
    result = receipt.get('gitCleanupResult') or receipt.get('gitCleanupUncertain')
    effect = {'hostRetirement':host,'nativeRetirement':'unknown','removal':'unknown','child':'unknown','branch':'unknown'}
    if isinstance(result, dict) and all(result.get(k) in choices for k, choices in (
            ('retirement',('not_issued','applied','unknown')),('removal',('not_issued','applied','unknown')),
            ('child',('not_issued','settled','unknown')),('branch',('present','unknown')))):
        effect.update(nativeRetirement=result['retirement'],removal=result['removal'],child=result['child'],branch=result['branch'])
    return {'effect':effect}


async def preview_owned(request, *, account, owner, admin, auth, barrier, admission,
                        utility, repository, provider, session_manager, confirm=False, drain=None):
    """Read an already-admitted human's exact preview; no reservation or effect."""
    from routes.orchestration_routes import _get_observation_binding, _ensure_observation_binding_current
    from src.bg_jobs import begin_background_close

    unavailable = {'version': 1, 'state': 'unavailable'}
    lease = close = background = claim = context = None
    issued = False
    try:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 32768:
                return unavailable
            raw.extend(chunk)
        body = json.loads(raw)
        if (type(body) is not dict or set(body) != ({'version','chatID','rootRef','targetRef','requestID','expectedDigest'} if confirm else {'version','chatID','rootRef','targetRef'})
                or type(body['version']) is not int or body['version'] != 1
                or any(type(body[k]) is not str or not body[k] or len(body[k].encode('utf-8','strict')) > 8192
                       for k in ('chatID','rootRef','targetRef'))):
            return unavailable
        if confirm and (type(body['requestID']) is not str or not re.fullmatch(r'[0-9a-f]{32}', body['requestID'])
                or type(body['expectedDigest']) is not str or not re.fullmatch(r'[0-9a-f]{64}', body['expectedDigest'])):
            return unavailable
        if confirm:
            utility.idempotency_key, utility.request_hash = body['requestID'], body['expectedDigest']
            utility.git_index_selector_ready = True
            prior = repository.get_operation(owner_subject_id=account, operation_id=body['requestID'])
            if prior is not None:
                return {'version':1, 'state':'status_only' if prior['digest'] == body['expectedDigest'] and prior['receipt'].get('operation') == 'git_cleanup' else 'unknown'}
        generation = repository.generation()
        active_generation = [generation]
        root = resolve_resource_ref(body['rootRef'], expected_owner_subject_id=account, current_policy_generation=generation)
        target = resolve_resource_ref(body['targetRef'], expected_owner_subject_id=account, current_policy_generation=generation)
        if any(ref.provider != 'host' or ref.kind != 'folder' for ref in (root,target)):
            return unavailable
        root_context = ProviderContext(account, owner, generation, is_admin=admin, workspace_id=root.workspace_id)
        target_context = ProviderContext(account, owner, generation, is_admin=admin, workspace_id=target.workspace_id)
        registered = await provider.authorize_readonly_root(root_context, origin_id=root.origin_id, expected_revision=None)
        root_path, target_path = registered['root'], _path(target.origin_id)
        if ((root.location_id is not None and root.location_id != registered['root_id'])
                or os.path.commonpath((root_path, target_path)) == target_path):
            return unavailable
        stat = await provider.stat(target_context, origin_id=target.origin_id)
        selected = await provider.authorize_selected_folder(target_context, origin_id=target.origin_id, expected_revision=stat.revision)
        if target.location_id is not None and target.location_id != selected['root_id']:
            return unavailable
        root_identity = _directory_identity(root_path)
        # Each selected folder and its containing Location must independently
        # have current read/write authority; the refs are selectors only.
        for context, record, path in ((root_context,registered,root_path),(target_context,selected,target_path)):
            scope = provider._scope(context)
            if not provider._path_writable(context, path, scope):
                return unavailable
            for capability in ('read','write'):
                permission = repository.resolve(subject_id=account, is_admin=admin, origin='app',
                    location_id=record['root_id'], capability=capability, workspace_id=context.workspace_id,
                    relative_resource=os.path.relpath(path, record['root']) if path != record['root'] else '',
                    chat_id=body['chatID'], group_ids=(), observation_only=True)
                if not permission.allowed or permission.policy_generation != generation:
                    return unavailable
        user, supervisor, binding, mapping = await asyncio.to_thread(
            _get_observation_binding, request, body['chatID'], None, session_manager)
        if user != owner:
            return unavailable
        original = get_managed_binding(body['chatID'], owner=owner)
        if (original.get('engineSessionID') != binding.engine_session_id
                or original.get('mappingRevision') != binding.binding_revision
                or original.get('transition') is not None or original.get('physicalCwd') != root_path):
            return unavailable
        pool = supervisor.owner_lifecycle_pool()
        lease, runtime, worker, bridge, process, holder, installed, epoch = await pool.admit_git_index_operation(owner=owner)
        utility.supervisor, utility.worker, utility.bridge, utility.process = pool, worker, bridge, process
        utility.holder, utility.generation, utility.epoch, utility.lease = holder, installed, epoch, lease
        close = pool.try_close_host_directory(target_path, lease)
        background = begin_background_close()
        if background is None:
            return unavailable
        request_id = body['requestID'] if confirm else uuid.uuid4().hex
        cleanup_digest = _hash(['openclank.git-cleanup.operation.v1', request_id, account])

        def sync_current(expected_owner, expected_account, expected_digest):
            try:
                pool._assert_owner_lifecycle(owner, epoch)
                return bool(expected_owner == owner and expected_account == account and expected_digest == cleanup_digest
                    and not utility.cancel_requested and not utility.settled and not admission.released
                    and admission.utility is utility and utility.account_current()
                    and repository.generation() == active_generation[0] and _directory_identity(root_path) == root_identity
                    and pool._states.get(owner) is runtime
                    and pool.single_normal_directory_close_current(close) and background.current()
                    and worker._proc is process and worker._bridge is bridge and worker.is_alive()
                    and getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) == holder
                    and not lease._released)
            except Exception:
                return False

        async def current():
            if not sync_current(owner, account, cleanup_digest) or not await barrier.is_live(owner, admission):
                return False
            try:
                await asyncio.to_thread(_ensure_observation_binding_current, mapping, owner, body['chatID'], binding)
                return sync_current(owner, account, cleanup_digest) and get_managed_binding(body['chatID'], owner=owner) == original
            except Exception:
                return False

        if not await current():
            return unavailable
        host = snapshot_directory_execution_scopes(target_path, owner, account, cleanup_digest, current_authority=sync_current)
        map_before = _map_snapshot(bridge, owner, target_path, host)
        response = await worker.session_git_cleanup_preview_request(body['chatID'], binding.engine_session_id,
            root_path, owner=owner, directory=target_path)
        if response.status_code != 200 or len(response.content) > 1024 * 1024 + 8192:
            return unavailable
        witness = _validate_native(response.json(), target_path)
        if not await current():
            return unavailable
        again = snapshot_directory_execution_scopes(target_path, owner, account, cleanup_digest, current_authority=sync_current)
        if again != host or _map_snapshot(bridge, owner, target_path, again) != map_before:
            return unavailable
        renewed_root = await provider.authorize_readonly_root(root_context, origin_id=root.origin_id, expected_revision=None)
        renewed_target = await provider.authorize_selected_folder(target_context, origin_id=target.origin_id, expected_revision=stat.revision)
        if renewed_root != registered or renewed_target != selected or not await current():
            return unavailable
        material = {'version':1, 'profile':'reviewed_git_cleanup_v1', 'requestID':request_id,
            'account':account, 'chatID':body['chatID'], 'policyGeneration':generation,
            'root':{'id':root.stable_id,'location':registered['root_id'],'workspace':root.workspace_id,'path':root_path,'identity':root_identity},
            'target':{'id':target.stable_id,'location':selected['root_id'],'workspace':target.workspace_id,
                'path':target_path,'revision':dict(stat.revision)},
            'rootAuthority':registered, 'targetAuthority':selected,
            'hostScopes':host, 'map':map_before, 'witness':witness, 'cleanupDigest':cleanup_digest}
        digest = _hash(material)
        if not confirm:
            return {'version':1, 'state':'ready', 'requestID':request_id, 'expectedDigest':digest,
                'review':{'targetPath':target_path, 'branch':witness['branch'], 'headOID':witness['headOID'],
                    'clean':True, 'branchRetained':True, 'historyRetained':True,
                    'hostScopes':[{'chatID':row[0]} for row in host],
                    'nativeSessions':len(witness['scopes']['sessions']), 'nativeActors':len(witness['scopes']['actors'])}}
        if digest != body['expectedDigest']:
            return {'version':1, 'state':'stale'}
        from src.openclank.git_index_authority import GitIndexContext, native_cleanup_action_sha256, record_cleanup_host_retirement, _settle
        from src.openclank.transcript_projection import retire_directory_execution_scopes
        utility.root, utility.idempotency_key, utility.request_hash = 'git-cleanup', request_id, digest
        pending = {'version':1, 'state':'pending', 'operation':'git_cleanup', 'intentDigest':digest,
            'locationID':selected['root_id'], 'workspaceID':target.workspace_id, 'chatID':body['chatID'],
            'hostRetirement':'not_issued', 'policyTransition':{'fromGeneration':generation,'toGeneration':generation}}
        existing = repository.reserve_operation(owner_subject_id=account, operation_id=request_id,
            request_digest=digest, generation=generation, receipt=pending, phase='git_cleanup_pending')
        if existing is not None:
            return {'version':1, 'state':'status_only' if existing['digest'] == digest and existing['receipt'].get('operation') == 'git_cleanup' else 'unknown'}
        try:
            once = repository.create_git_cleanup_once(subject_id=account, location_id=selected['root_id'],
                workspace_id=target.workspace_id, chat_id=body['chatID'], operation_id=request_id,
                request_digest=digest, expected_generation=generation, expires_unix_ms=int(time.time()*1000)+120000)
            if once['generation'] != generation+1: raise ValueError('cleanup policy changed')
            pending['policyTransition']['toGeneration'] = once['generation']
            repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,
                generation=generation,receipt=pending,phase='git_cleanup_pending')
            consumed = repository.consume_git_cleanup_once(subject_id=account, location_id=selected['root_id'],
                workspace_id=target.workspace_id, chat_id=body['chatID'], operation_id=request_id,
                request_digest=digest, binding_id=once['binding_id'], expected_generation=once['generation'])
            if consumed['generation'] != generation+2: raise ValueError('cleanup policy changed')
            active_generation[0] = consumed['generation']
            pending['policyTransition']['toGeneration'] = consumed['generation']
            repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,
                generation=generation,receipt=pending,phase='git_cleanup_pending')
            claim = repository.claim_git_cleanup_host_mutation(owner_subject_id=account, workspace_id=target.workspace_id,
                generation=active_generation[0], paths=[root_path, target_path])
            capability = secrets.token_hex(32)
            action_hash = native_cleanup_action_sha256(session_id=binding.engine_session_id, chat_id=body['chatID'],
                operation_id=request_id, request_digest=digest, cleanup_digest=cleanup_digest, witness=witness)
            pending['actionSHA256'] = action_hash
            repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,
                generation=generation,receipt=pending,phase='git_cleanup_pending')
            async def recheck(phase):
                if not await current(): return False
                approval = repository.get_binding(once['binding_id'])
                if (approval.status != 'consumed' or approval.remaining_uses != 0 or approval.revision != 2
                        or approval.generation != active_generation[0] or approval.subject_id != account
                        or approval.location_id != selected['root_id'] or approval.workspace_id != target.workspace_id
                        or approval.chat_id != body['chatID'] or approval.resource_ref != 'git-cleanup-action:sha256:'+digest
                        or approval.operation != 'git_worktree_cleanup'):
                    return False
                observed_claim = repository.inspect_host_mutation(owner_subject_id=account, **claim)
                if not observed_claim or observed_claim.get('paths') != sorted(set([root_path,target_path])): return False
                fresh_root = ProviderContext(account,owner,active_generation[0],is_admin=admin,workspace_id=root.workspace_id)
                fresh_target = ProviderContext(account,owner,active_generation[0],is_admin=admin,workspace_id=target.workspace_id)
                for fresh, old, path in ((fresh_root,registered,root_path),(fresh_target,selected,target_path)):
                    if not provider._path_writable(fresh,path,provider._scope(fresh)): return False
                    for cap in ('read','write'):
                        rule = repository.resolve(subject_id=account,is_admin=admin,origin='app',location_id=old['root_id'],
                            capability=cap,workspace_id=fresh.workspace_id,relative_resource=os.path.relpath(path,old['root']) if path != old['root'] else '',
                            chat_id=body['chatID'],group_ids=(),observation_only=True)
                        if not rule.allowed or rule.policy_generation != active_generation[0]: return False
                root_now = await provider.authorize_readonly_root(fresh_root,origin_id=root.origin_id,expected_revision=None)
                target_now = await provider.authorize_selected_folder(fresh_target,origin_id=target.origin_id,expected_revision=stat.revision)
                def normalized(item):
                    copied = json.loads(json.dumps(item))
                    copied['generation'] = generation
                    if isinstance(copied.get('scope'), dict) and 'generation' in copied['scope']:
                        copied['scope']['generation'] = generation
                    return copied
                if normalized(root_now) != registered or normalized(target_now) != selected: return False
                before = snapshot_directory_execution_scopes(target_path,owner,account,cleanup_digest,current_authority=sync_current)
                if phase != 'before_remove' and before != host: return False
                if phase == 'before_remove' and before != tuple((chat,binding,cleanup_digest) for chat,binding,_ in host): return False
                if _map_snapshot(bridge,owner,target_path,before) != map_before: return False
                if phase == 'before_retire':
                    if not record_cleanup_host_retirement(context,'unknown'): return False
                    retire_directory_execution_scopes(target_path,owner,account,cleanup_digest,host,current_authority=sync_current)
                    if not record_cleanup_host_retirement(context,'applied'): return False
                    after = snapshot_directory_execution_scopes(target_path,owner,account,cleanup_digest,current_authority=sync_current)
                    if after != tuple((chat,binding,cleanup_digest) for chat,binding,_ in host): return False
                    if _map_snapshot(bridge,owner,target_path,after) != map_before: return False
                return await current()
            context = GitIndexContext(utility,repository,request_id,digest,capability,action_hash,None,generation,
                active_generation[0],dict(pending),recheck,profile='cleanup')
            utility.git_index_context = context
            if not await recheck('before_close'): raise ValueError('cleanup authority changed')
            payload = {'version':1,'stableChatID':body['chatID'],'operationID':request_id,'requestDigest':digest,
                'capability':capability,'cleanupDigest':cleanup_digest,'witness':witness}
            utility.git_index_http_task = asyncio.create_task(worker.session_git_cleanup_apply_request(
                body['chatID'],binding.engine_session_id,root_path,payload,owner=owner))
            utility.git_index_drained = False
            utility.issued = issued = True
        except Exception:
            if not issued:
                if context is None:
                    context = GitIndexContext(utility,repository,request_id,digest,'','',None,generation,active_generation[0],dict(pending),lambda *_:None,profile='cleanup')
                settled = _settle(context,{'state':'refused','retirement':'not_issued','removal':'not_issued','child':'not_issued','branch':'unknown'})
                return {'version':1,'state':'refused' if settled else 'unknown','policyTransition':dict(pending['policyTransition'])}
            raise
        await drain(utility)
        receipt = repository.get_operation(owner_subject_id=account,operation_id=request_id)
        if not context.finished or not receipt or receipt['digest'] != digest:
            return {'version':1,'state':'unknown'}
        return {'version':1,'state':receipt['receipt'].get('state','unknown'), 'policyTransition':dict(pending['policyTransition']), **public_effect(receipt['receipt'])}
    except Exception:
        if issued:
            return {'version':1, 'state':'unknown', 'requestID':utility.idempotency_key, 'expectedDigest':utility.request_hash}
        return unavailable
    finally:
        if issued and not getattr(utility, 'git_index_drained', False):
            await drain(utility)
        if claim is not None:
            repository.release_host_mutation(owner_subject_id=account, **claim)
        if background is not None:
            background.release()
        if close is not None:
            pool.release_host_directory_close(close)
        if lease is not None and not lease._released:
            await lease.release()

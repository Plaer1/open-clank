"""Read-only Git inspection through the current owned managed session."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import difflib
import re
import json
import uuid
import secrets
import time
from fastapi import APIRouter, Request
from src.openclank.files_facade import ProviderContext
from src.openclank.files_host_provider import HostFilesProvider, _origin
from src.openclank.files_principal import files_principal
from src.openclank.filesystem_registry import FilesystemRootRegistry
from src.openclank.git_workbench import allowed_relative_path, index_fingerprint, derive_git_index_witness, derive_git_commit_witness
from src.openclank.resource_refs import resolve_resource_ref, stable_resource_id
from routes.orchestration_routes import _get_observation_binding, _ensure_observation_binding_current

async def _drain_git_index(utility):
    """Join the original HTTP and exact finish or positively waited child."""
    async def join():
        http = utility.git_index_http_task
        await asyncio.gather(http, return_exceptions=True)
        context = utility.git_index_context
        if context.finished:
            return
        finish = asyncio.create_task(context.completion.wait())
        child = asyncio.create_task(utility.process.wait())
        try:
            done, _ = await asyncio.wait((finish, child), return_when=asyncio.FIRST_COMPLETED)
            if context.finished and finish in done:
                return
            try:
                code = child.result() if child in done and not child.cancelled() else None
                if type(code) is int and utility.process.returncode == code:
                    from src.openclank.git_index_authority import _settle
                    async with context.lock:
                        if not context.finished and not _settle(context,None,host_exit=True):
                            raise RuntimeError('Git child-exit receipt unavailable')
                    return
            except Exception:
                pass
            # An unreadable child result is not proof of exit. Keep the actual
            # original completion signal alive and make uncertainty observable.
            utility.host_cleanup_unknown = True
            await finish
            if not context.finished:
                raise RuntimeError('Git finish unavailable')
        finally:
            for pending in (finish, child):
                if not pending.done(): pending.cancel()
            await asyncio.gather(finish, child, return_exceptions=True)

    task = asyncio.create_task(join())
    while not task.done():
        try: await asyncio.shield(task)
        except asyncio.CancelledError: utility.cancel_requested = True
    try:
        task.result()
    except BaseException:
        utility.host_cleanup_unknown = True
        raise
    utility.git_index_drained = True

def setup_git_workbench_routes(*, repository, session_manager):
    router = APIRouter(prefix='/api/git-review')
    provider = HostFilesProvider(registry=FilesystemRootRegistry(repository=repository), operation_store=repository)

    def row_profile(row):
        receipt = row.get('receipt') if type(row) is dict else None
        if type(receipt) is not dict: return None
        if receipt.get('kind') == 'git.discard.v1': return 'discard'
        if receipt.get('operation') in ('stage','unstage','stage_hunks','unstage_hunks'): return 'index'
        if receipt.get('operation') == 'git_commit': return 'commit'
        if receipt.get('operation') == 'git_cleanup': return 'cleanup'
        if receipt.get('operation') == 'git_branch_create': return 'branch_create'
        if receipt.get('operation') == 'git_fetch': return 'fetch'
        if receipt.get('operation') == 'git_push': return 'push'
        if receipt.get('operation') == 'git_checkout': return 'checkout'
        return None

    def policy_transition(account, operation_id, row):
        """Report only this operation's positively identified Once bumps."""
        receipt = row['receipt']; base = row['generation']
        result = {'fromGeneration':base,'toGeneration':base}
        stored = receipt.get('policyTransition')
        if (type(stored) is dict and set(stored) == {'fromGeneration','toGeneration'}
                and stored['fromGeneration'] == base and type(stored['toGeneration']) is int
                and base <= stored['toGeneration'] <= base + 2):
            result['toGeneration'] = stored['toGeneration']
        try:
            profile = row_profile(row)
            if profile not in ('index','commit','cleanup','branch_create','fetch','push','checkout'): return result
            identifier = {'index':'git-once-','commit':'git-commit-once-','cleanup':'git-cleanup-once-','branch_create':'git-branch-create-once-','fetch':'git-fetch-once-','push':'git-push-once-','checkout':'git-checkout-once-'}[profile] + hashlib.sha256(json.dumps([account,operation_id],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            binding = repository.get_binding(identifier)
            if (binding.subject_id != account or binding.created_by_subject_id != account
                    or binding.location_id != receipt['locationID'] or binding.workspace_id != receipt['workspaceID']
                    or binding.chat_id != receipt['chatID'] or binding.resource_ref != {'index':'git-index-action:sha256:','commit':'git-commit-action:sha256:','cleanup':'git-cleanup-action:sha256:','branch_create':'git-branch-create-action:sha256:','fetch':'git-fetch-action:sha256:','push':'git-push-action:sha256:','checkout':'git-checkout-action:sha256:'}[profile] + row['digest']
                    or binding.operation != {'index':'git_index_update','commit':'git_commit','cleanup':'git_worktree_cleanup','branch_create':'git_branch_create','fetch':'git_fetch','push':'git_push','checkout':'git_checkout'}[profile] or binding.lifetime != 'once'
                    or binding.capabilities != ('write',)):
                return result
            if binding.generation == base + 1 and binding.revision == 1 and binding.status == 'active' and binding.remaining_uses == 1:
                result['toGeneration'] = max(result['toGeneration'],base + 1)
            elif binding.generation == base + 2 and binding.revision == 2 and binding.status == 'consumed' and binding.remaining_uses == 0:
                result['toGeneration'] = max(result['toGeneration'],base + 2)
        except Exception:
            pass
        return result

    def public_effect(receipt):
        result = receipt.get('gitIndexResult') if receipt.get('settled') is True else None
        if (type(result) is not dict or result.get('object') not in ('none','written_or_present','unknown')
                or type(result.get('candidateRetained')) is not bool):
            return {}
        return {'effect':{'object':result['object'],'candidateRetained':result['candidateRetained']}}

    def public_commit_effect(receipt):
        result = receipt.get('gitCommitResult') if receipt.get('settled') is True else None
        if (type(result) is not dict or result.get('tree') not in ('none','written_or_present','unknown')
                or result.get('commit') not in ('none','written_or_present','unknown')
                or result.get('ref') not in ('not_issued','applied','unknown')
                or result.get('cleanup') not in ('complete','unknown')): return {}
        effect = {key:result[key] for key in ('tree','commit','ref','cleanup')}
        if 'commitOID' in result: effect['commitOID'] = result['commitOID']
        return {'effect':effect}

    def public_branch_effect(receipt):
        result = receipt.get('gitBranchCreateResult') if receipt.get('settled') is True else None
        if (type(result) is not dict or result.get('ref') not in ('not_issued','applied','unknown')
                or result.get('cleanup') not in ('complete','unknown')): return {}
        return {'effect':{'ref':result['ref'],'cleanup':result['cleanup']}}

    def public_fetch_effect(receipt):
        result = receipt.get('gitFetchResult') if receipt.get('settled') is True else None
        if (type(result) is not dict or result.get('transfer') not in ('not_issued','completed','unknown')
                or result.get('ref') not in ('not_issued','applied','unknown') or result.get('cleanup') not in ('complete','unknown')): return {}
        return {'effect':{'transfer':result['transfer'],'ref':result['ref'],'cleanup':result['cleanup']}}

    def public_push_effect(receipt):
        result = receipt.get('gitPushResult') if receipt.get('settled') is True else None
        if (type(result) is not dict or result.get('remote') not in ('not_issued','accepted','unknown')
                or result.get('cleanup') not in ('complete','unknown')): return {}
        return {'effect':{'remote':result['remote'],'cleanup':result['cleanup']}}

    async def _index_preview_owned(request, account, owner, admin, auth, barrier, admission, utility, *, confirm=False):
        """Read-only preview from one retained human/worker lifetime."""
        from src.openclank.transcript_projection import get_managed_binding
        unavailable = {'version':1, 'state':'unavailable'}
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 32768: return unavailable
                raw.extend(chunk)
            body = json.loads(raw)
            partial = type(body) is dict and body.get('operation') in ('stage_hunks','unstage_hunks')
            keys = {'version','chatID','rootRef','fileRef','operation'} | ({'hunkIDs'} if partial else set()) | ({'requestID','expectedDigest'} if confirm else set())
            if (type(body) is not dict or set(body) != keys
                    or body['version'] != 1 or body['operation'] not in ('stage','unstage','stage_hunks','unstage_hunks')
                    or any(type(body[k]) is not str or not body[k] or len(body[k]) > 8192 for k in ('chatID','rootRef','fileRef'))):
                return unavailable
            if partial:
                ids = body['hunkIDs']
                if (type(ids) is not list or len(ids) > 128 or len(set(ids)) != len(ids)
                        or any(type(item) is not str or not re.fullmatch(r'[0-9a-f]{64}',item) for item in ids)
                        or (confirm and not ids)):
                    return unavailable
                ids = sorted(ids)
            if confirm and (type(body['requestID']) is not str or not re.fullmatch(r'[0-9a-f]{32}',body['requestID'])
                    or type(body['expectedDigest']) is not str or not re.fullmatch(r'[0-9a-f]{64}',body['expectedDigest'])):
                return unavailable
            if confirm:
                utility.idempotency_key, utility.request_hash = body['requestID'], body['expectedDigest']
                utility.git_index_selector_ready = True
                prior = repository.get_operation(owner_subject_id=account,operation_id=body['requestID'])
                if prior is not None:
                    if prior['digest'] != body['expectedDigest'] or row_profile(prior) != 'index':
                        return {'version':1,'state':'unknown'}
                    return {'version':1,'state':'status_only','policyTransition':policy_transition(account,body['requestID'],prior)}
            generation = repository.generation()
            active_generation = [generation]
            root = resolve_resource_ref(body['rootRef'], expected_owner_subject_id=account, current_policy_generation=generation)
            selected = resolve_resource_ref(body['fileRef'], expected_owner_subject_id=account, current_policy_generation=generation)
            if (root.provider != 'host' or root.kind != 'folder' or selected.provider != 'host'
                    or selected.kind not in ('file','document') or root.workspace_id != selected.workspace_id): return unavailable
            root_context = ProviderContext(account, owner, generation, is_admin=admin, workspace_id=root.workspace_id)
            registered_root = await provider.authorize_readonly_root(root_context,origin_id=root.origin_id,expected_revision=None)
            location_id = registered_root['root_id']
            if ((root.location_id is not None and root.location_id != location_id)
                    or (selected.location_id is not None and selected.location_id != location_id)):
                return unavailable
            user, supervisor, binding, mapping = await asyncio.to_thread(_get_observation_binding, request, body['chatID'], None, session_manager)
            if user != owner: return unavailable
            original = get_managed_binding(body['chatID'], owner=owner)
            cwd = original.get('physicalCwd')
            if (original.get('engineSessionID') != binding.engine_session_id or original.get('transition') is not None
                    or original.get('mappingRevision') != binding.binding_revision or not isinstance(cwd,str)):
                return unavailable
            pool = supervisor.owner_lifecycle_pool()
            lease = None
            try:
                lease, runtime, worker, bridge, process, holder, installed, epoch = await pool.admit_git_index_operation(owner=owner)
                utility.supervisor, utility.worker, utility.bridge, utility.process = pool, worker, bridge, process
                utility.holder, utility.generation, utility.epoch, utility.lease = holder, installed, epoch, lease
                async def current():
                    try:
                        pool._assert_owner_lifecycle(owner, epoch)
                        await asyncio.to_thread(_ensure_observation_binding_current, mapping, owner, body['chatID'], binding)
                        return bool(not utility.cancel_requested and utility.account_current() and await barrier.is_live(owner, admission)
                            and repository.generation() == active_generation[0] and pool._states.get(owner) is runtime
                            and runtime.active is worker and runtime.status == 'ready' and runtime.generation == installed
                            and worker.is_alive() and worker._proc is process and worker._bridge is bridge
                            and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) == holder
                            and get_managed_binding(body['chatID'], owner=owner) == original and not lease._released)
                    except Exception: return False
                async def read(op, path=None):
                    payload = {'stableChatID':body['chatID'], 'operation':op, **({'path':path} if path is not None else {})}
                    response = await worker.session_git_read_request(body['chatID'], binding.engine_session_id, cwd, payload, owner=owner)
                    if response.status_code != 200 or len(response.content) > 400000: raise ValueError('Git read unavailable')
                    value = response.json()
                    if (type(value) is not dict or set(value) != {'state','data','indexExists','digest','truncated'}
                            or value['state'] != 'ready' or value['truncated'] is not False
                            or not isinstance(value['digest'],str) or not re.fullmatch(r'[0-9a-f]{64}',value['digest'])):
                        raise ValueError('Git read unavailable')
                    data = base64.b64decode(value['data'],validate=True)
                    if len(data) > 262144 or hashlib.sha256(data).hexdigest() != value['digest']: raise ValueError('Git read unavailable')
                    return data
                def entry(raw, path, tree):
                    if raw == b'': return None
                    rows = raw.split(b'\0')
                    if len(rows) != 2 or rows[1] != b'': raise ValueError('Git entry unavailable')
                    metadata, name = rows[0].split(b'\t',1)
                    if name.decode('utf-8','strict') != path: raise ValueError('Git entry changed')
                    parts = metadata.decode('ascii','strict').split(' ')
                    if len(parts) != 3: raise ValueError('Git entry unavailable')
                    mode, kind, oid = parts if tree else (parts[0], parts[2], parts[1])
                    if mode not in ('100644','100755') or kind != ('blob' if tree else '0') or not re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?',oid):
                        raise ValueError('Git entry unavailable')
                    return {'path':path,'mode':mode,'oid':oid}
                async def native(path):
                    if not await current(): raise ValueError('Git authority changed')
                    response = await worker.session_git_index_preview_request(body['chatID'], binding.engine_session_id, cwd, owner=owner)
                    if response.status_code != 200 or len(response.content) > 16384: raise ValueError('Git preview unavailable')
                    value = response.json()
                    if type(value) is not dict or set(value) != {'version','state','gitIndexControlV1','physicalIndex'} or value['version'] != 1 or value['state'] != 'ready' or value['gitIndexControlV1'] is not True:
                        raise ValueError('Git preview unavailable')
                    roots = (await read('root')).decode('utf-8','strict').splitlines()
                    if len(roots) != 2: raise ValueError('Git root unavailable')
                    head = (await read('head')).decode('ascii','strict').strip()
                    physical = value['physicalIndex']
                    return {'root':os.path.realpath(roots[0]), 'git':os.path.realpath(os.path.join(cwd, roots[1])),
                        'objectFormat':physical['objectFormat'], 'head':head,
                        'indexEntry':entry(await read('indexEntry',path),path,False),
                        'treeEntry':entry(await read('treeEntry',path),path,True), 'physicalIndex':physical}
                async def work():
                    witness = await derive_git_index_witness(utility=utility, provider=provider, repository=repository,
                        account=account, root_origin=root.origin_id, file_origin=selected.origin_id,
                        location_id=location_id, workspace_id=root.workspace_id, policy_generation=generation,
                        chat_id=body['chatID'], is_admin=admin, group_ids=(), current=current, native_read=native)
                    git = witness['snapshot']['git']
                    index_entry, tree_entry = git['indexEntry'], git['treeEntry']
                    prior = await read('blob',index_entry['oid']) if index_entry else b''
                    proposed = witness['bytes'] if body['operation'] in ('stage','stage_hunks') else await read('blob',tree_entry['oid']) if tree_entry else b''
                    checked = await derive_git_index_witness(utility=utility, provider=provider, repository=repository,
                        account=account, root_origin=root.origin_id, file_origin=selected.origin_id,
                        location_id=location_id, workspace_id=root.workspace_id, policy_generation=generation,
                        chat_id=body['chatID'], is_admin=admin, group_ids=(), current=current, native_read=native)
                    if checked != witness or not await current(): raise ValueError('Git authority changed')
                    proposed_mode = ('100755' if witness['snapshot']['mode'] & 0o111 else '100644') if body['operation'] in ('stage','stage_hunks') else tree_entry['mode'] if tree_entry else None
                    if partial:
                        from src.openclank.git_hunks import project, HunkUnavailable
                        try:
                            projection = project(prior,proposed,before_mode=int(index_entry['mode'],8) if index_entry else 0,
                                after_mode=int(proposed_mode,8) if proposed_mode else 0,
                                before_exists=index_entry is not None,after_exists=body['operation']=='stage_hunks' or tree_entry is not None)
                            if not ids:
                                return {'version':1,'state':'choose_hunks','review':projection.review()}
                            proposed = projection.apply(ids)
                            proposed_mode = index_entry['mode']
                        except HunkUnavailable:
                            return {'version':1,'state':'stale' if ids else 'unavailable'}
                    unchanged = (prior == proposed and (index_entry['mode'] if index_entry else None) == proposed_mode)
                    request_id = body['requestID'] if confirm else uuid.uuid4().hex
                    digest_fields = {'version':1,'requestID':request_id,'operation':body['operation'],'witness':witness['digest']}
                    if partial: digest_fields.update({'hunkIDs':ids,'proposedSHA256':hashlib.sha256(proposed).hexdigest()})
                    digest = hashlib.sha256(json.dumps(digest_fields,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                    if confirm:
                        if digest != body['expectedDigest']:
                            return {'version':1,'state':'stale'}
                        return await execute_confirm(witness, request_id, digest, proposed, index_entry)
                    def side(data, mode, exists):
                        try:
                            if b'\0' in data: raise ValueError('binary')
                            text = data.decode('utf-8','strict')
                            binary = False
                        except (UnicodeError, ValueError):
                            text = None; binary = True
                        return {'exists':exists,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),
                            'mode':mode,'binary':binary,'truncated':False,'text':text}
                    return {'version':1,'state':'ready','requestID':request_id,'expectedDigest':digest,
                        'displayName':os.path.basename(witness['snapshot']['path']), 'comparison':'raw',
                        'summary':'unchanged' if unchanged else 'changed',
                        'before':side(prior,index_entry['mode'] if index_entry else None,index_entry is not None),
                        'after':side(proposed,proposed_mode,body['operation'] in ('stage','stage_hunks') or tree_entry is not None)}
                async def execute_confirm(witness, request_id, digest, proposed, index_entry):
                    from src.openclank.git_index_authority import GitIndexContext, native_action_sha256, _settle
                    utility.root, utility.idempotency_key, utility.request_hash = 'git-index', request_id, digest
                    candidate = secrets.token_hex(16)
                    pending = {'version':1,'state':'pending','candidateToken':candidate,
                        'operation':body['operation'],'intentDigest':digest,
                        'locationID':location_id,'workspaceID':root.workspace_id,'chatID':body['chatID'],
                        'policyTransition':{'fromGeneration':generation,'toGeneration':generation}}
                    existing = repository.reserve_operation(owner_subject_id=account,operation_id=request_id,
                        request_digest=digest,generation=generation,receipt=pending,phase='git_index_pending')
                    if existing is not None:
                        return {'version':1,'state':'status_only','policyTransition':policy_transition(account,request_id,existing)}
                    context = None
                    issued = False
                    http_task = None
                    try:
                        once = repository.create_git_index_once(subject_id=account,location_id=location_id,
                            workspace_id=root.workspace_id,chat_id=body['chatID'],operation_id=request_id,
                            request_digest=digest,expected_generation=generation,expires_unix_ms=int(time.time()*1000)+120000)
                        if once['generation'] != generation + 1: raise ValueError('Git policy changed')
                        pending['policyTransition']['toGeneration'] = once['generation']
                        repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,
                            generation=generation,receipt=pending,phase='git_index_pending')
                        consumed = repository.consume_git_index_once(subject_id=account,location_id=location_id,
                            workspace_id=root.workspace_id,chat_id=body['chatID'],operation_id=request_id,
                            request_digest=digest,binding_id=once['binding_id'],expected_generation=once['generation'])
                        if consumed['generation'] != generation + 2: raise ValueError('Git policy changed')
                        active_generation[0] = consumed['generation']
                        pending['policyTransition']['toGeneration'] = consumed['generation']
                        repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,
                            generation=generation,receipt=pending,phase='git_index_pending')
                        async def fresh():
                            binding_row = repository.get_binding(once['binding_id'])
                            if (binding_row.status != 'consumed' or binding_row.remaining_uses != 0
                                    or binding_row.generation != active_generation[0] or binding_row.revision != 2
                                    or binding_row.subject_id != account or binding_row.location_id != location_id
                                    or binding_row.workspace_id != root.workspace_id or binding_row.chat_id != body['chatID']
                                    or binding_row.resource_ref != 'git-index-action:sha256:' + digest
                                    or binding_row.operation != 'git_index_update'):
                                return False
                            renewed = await derive_git_index_witness(utility=utility,provider=provider,repository=repository,
                                account=account,root_origin=root.origin_id,file_origin=selected.origin_id,
                                location_id=location_id,workspace_id=root.workspace_id,policy_generation=active_generation[0],
                                chat_id=body['chatID'],is_admin=admin,group_ids=(),current=current,native_read=native)
                            normalized = dict(renewed['snapshot']); normalized['policyGeneration'] = generation
                            return normalized == witness['snapshot'] and renewed['bytes'] == witness['bytes']
                        if not await fresh(): raise ValueError('Git witness changed')
                        git = witness['snapshot']['git']; tree = git['treeEntry']
                        mode = 0o100755 if witness['snapshot']['mode'] & 0o111 else 0o100644
                        action = ({'operation':'stage','path':witness['snapshot']['path'],'mode':int(index_entry['mode'],8) if partial else mode,
                            'bytesBase64':base64.b64encode(proposed).decode('ascii')} if body['operation'] in ('stage','stage_hunks','unstage_hunks')
                            else {'operation':'unstage','path':witness['snapshot']['path'],
                                'mode':int(tree['mode'],8) if tree else 0o100644,'headOID':tree['oid'] if tree else None})
                        capability = secrets.token_hex(32)
                        action_hash = native_action_sha256(session_id=binding.engine_session_id,chat_id=body['chatID'],
                            operation_id=request_id,request_digest=digest,candidate_token=candidate,physical=git['physicalIndex'],
                            action=action,stage_bytes=proposed if action['operation']=='stage' else None)
                        pending['actionSHA256'] = action_hash
                        # The predeclared original row is CAS-replaced before issue, retaining G0.
                        repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,
                            generation=generation,receipt=pending,phase='git_index_pending')
                        context = GitIndexContext(utility,repository,request_id,digest,capability,action_hash,candidate,
                            generation,active_generation[0],dict(pending),fresh)
                        utility.git_index_context = context
                        payload = {'version':1,'stableChatID':body['chatID'],'operationID':request_id,'requestDigest':digest,
                            'capability':capability,'candidateToken':candidate,'physicalIndex':git['physicalIndex'],'action':action}
                        if not await fresh(): raise ValueError('Git witness changed')
                        http_task = asyncio.create_task(worker.session_git_index_apply_request(
                            body['chatID'],binding.engine_session_id,cwd,payload,owner=owner))
                        utility.git_index_http_task = http_task
                        utility.git_index_drained = False
                        utility.issued = issued = True
                    except Exception:
                        if not issued:
                            if context is None:
                                context = GitIndexContext(utility,repository,request_id,digest,'', '', candidate,generation,
                                    active_generation[0],dict(pending),lambda: None)
                            settled = _settle(context,{'state':'refused','object':'none','candidateRetained':False})
                            return {'version':1,'state':'refused' if settled else 'unknown',
                                'policyTransition':dict(pending['policyTransition']),
                                **({'effect':{'object':'none','candidateRetained':False}} if settled else {})}
                    finally:
                        if issued:
                            await _drain_git_index(utility)
                    receipt = repository.get_operation(owner_subject_id=account,operation_id=request_id)
                    if not context.finished or not receipt or receipt['digest'] != digest:
                        return {'version':1,'state':'unknown','policyTransition':dict(pending['policyTransition'])}
                    return {'version':1,'state':receipt['receipt'].get('state','unknown'),
                        'policyTransition':dict(pending['policyTransition']), **public_effect(receipt['receipt'])}

                task = asyncio.create_task(work())
                try: return await asyncio.shield(task)
                except asyncio.CancelledError:
                    utility.cancel_requested = True
                    while not task.done():
                        try: await asyncio.shield(task)
                        except asyncio.CancelledError: continue
                        except Exception: break
                    raise
            finally:
                if lease is not None and (not utility.issued or getattr(utility,'git_index_drained',False)):
                    await lease.release()
        except Exception:
            if utility.issued:
                return {'version':1,'state':'unknown','requestID':utility.idempotency_key,'expectedDigest':utility.request_hash}
            return unavailable

    async def _commit_preview_owned(request, account, owner, admin, auth, barrier, admission, utility, *, confirm=False):
        from src.openclank.transcript_projection import get_managed_binding
        from src.openclank.git_index_authority import GitIndexContext, native_commit_action_sha256, _settle
        unavailable = {'version':1,'state':'unavailable'}
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 128*1024: return unavailable
                raw.extend(chunk)
            body = json.loads(raw)
            keys = {'version','chatID','rootRef','message','author','committer'} | ({'requestID','expectedDigest','reviewTime'} if confirm else set())
            if (type(body) is not dict or set(body) != keys or type(body['version']) is not int or body['version'] != 1
                    or any(type(body[k]) is not str or not body[k] or len(body[k].encode('utf-8','strict')) > 8192 for k in ('chatID','rootRef'))): return unavailable
            if confirm and (type(body['requestID']) is not str or not re.fullmatch(r'[0-9a-f]{32}',body['requestID'])
                    or type(body['expectedDigest']) is not str or not re.fullmatch(r'[0-9a-f]{64}',body['expectedDigest'])
                    or type(body['reviewTime']) is not int or not 0 <= int(time.time()) - body['reviewTime'] <= 1800): return unavailable
            review_time = body['reviewTime'] if confirm else int(time.time())
            message = body['message']
            if type(message) is not str or '\0' in message or not message.strip(): return unavailable
            message = message if message.endswith('\n') else message+'\n'
            if len(message.encode('utf-8','strict')) > 16384: return unavailable
            def identity(value):
                if type(value) is not dict or set(value) != {'name','email'}: raise ValueError('identity unavailable')
                name,email = value['name'],value['email']
                if (type(name) is not str or type(email) is not str or not name.strip() or not email.strip()): raise ValueError('identity unavailable')
                name,email = name.strip(),email.strip()
                if (len(name.encode('utf-8','strict')) > 256 or len(email.encode('utf-8','strict')) > 256
                        or any(ord(c)<32 or ord(c)==127 or c in '<>' for c in name+email)): raise ValueError('identity unavailable')
                return {'name':name,'email':email,'seconds':review_time,'timezone':'+0000'}
            action = {'message':message,'author':identity(body['author']),'committer':identity(body['committer'])}
            if confirm:
                utility.idempotency_key,utility.request_hash = body['requestID'],body['expectedDigest']
                utility.git_index_selector_ready = True
                prior = repository.get_operation(owner_subject_id=account,operation_id=body['requestID'])
                if prior is not None:
                    if prior['digest'] != body['expectedDigest'] or row_profile(prior) != 'commit': return {'version':1,'state':'unknown'}
                    return {'version':1,'state':'status_only','policyTransition':policy_transition(account,body['requestID'],prior)}
            generation = repository.generation(); active_generation = [generation]
            root = resolve_resource_ref(body['rootRef'],expected_owner_subject_id=account,current_policy_generation=generation)
            if root.provider != 'host' or root.kind != 'folder': return unavailable
            root_context = ProviderContext(account,owner,generation,is_admin=admin,workspace_id=root.workspace_id)
            registered = await provider.authorize_readonly_root(root_context,origin_id=root.origin_id,expected_revision=None)
            location_id = registered['root_id']
            if root.location_id is not None and root.location_id != location_id: return unavailable
            user,supervisor,binding,mapping = await asyncio.to_thread(_get_observation_binding,request,body['chatID'],None,session_manager)
            if user != owner: return unavailable
            original = get_managed_binding(body['chatID'],owner=owner); cwd = original.get('physicalCwd')
            if (original.get('engineSessionID') != binding.engine_session_id or original.get('transition') is not None
                    or original.get('mappingRevision') != binding.binding_revision or cwd != registered['root']): return unavailable
            pool = supervisor.owner_lifecycle_pool(); lease = None
            try:
                lease,runtime,worker,bridge,process,holder,installed,epoch = await pool.admit_git_index_operation(owner=owner)
                utility.supervisor,utility.worker,utility.bridge,utility.process = pool,worker,bridge,process
                utility.holder,utility.generation,utility.epoch,utility.lease = holder,installed,epoch,lease
                async def current():
                    try:
                        pool._assert_owner_lifecycle(owner,epoch)
                        await asyncio.to_thread(_ensure_observation_binding_current,mapping,owner,body['chatID'],binding)
                        return bool(not utility.cancel_requested and utility.account_current() and await barrier.is_live(owner,admission)
                            and repository.generation() == active_generation[0] and pool._states.get(owner) is runtime
                            and runtime.active is worker and runtime.status == 'ready' and runtime.generation == installed
                            and worker.is_alive() and worker._proc is process and worker._bridge is bridge
                            and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) == holder
                            and get_managed_binding(body['chatID'],owner=owner) == original and not lease._released)
                    except Exception: return False
                async def native():
                    if not await current(): raise ValueError('Git authority changed')
                    response = await worker.session_git_commit_preview_request(body['chatID'],binding.engine_session_id,cwd,owner=owner)
                    if response.status_code != 200 or len(response.content) > 4*1024*1024+128*1024: raise ValueError('Git preview unavailable')
                    value = response.json()
                    if (type(value) is not dict or set(value) != {'version','state','gitCommitControlV1','preview'}
                            or value['version'] != 1 or value['state'] != 'ready' or value['gitCommitControlV1'] is not True): raise ValueError('Git preview unavailable')
                    return value['preview']
                async def witness_at(policy):
                    return await derive_git_commit_witness(utility=utility,provider=provider,repository=repository,account=account,
                        root_origin=root.origin_id,location_id=location_id,workspace_id=root.workspace_id,policy_generation=policy,
                        chat_id=body['chatID'],is_admin=admin,group_ids=(),current=current,native_read=native)
                witness = await witness_at(generation); git = witness['snapshot']['git']
                indexed = {entry['path']:entry for entry in git['indexEntries']}; head = {entry['path']:entry for entry in git['headEntries']}
                changes = []
                for path in sorted(set(indexed)|set(head),key=lambda p:p.encode('utf-8')):
                    before,after = head.get(path),indexed.get(path)
                    if before == after: continue
                    changes.append({'path':path,'kind':'added' if before is None else 'deleted' if after is None else 'modified',
                        'before':None if before is None else {'mode':before['mode'],'oid':before['oid']},
                        'after':None if after is None else {'mode':after['mode'],'oid':after['oid']}})
                if not changes: return {'version':1,'state':'stale' if confirm else 'no_changes'}
                request_id = body['requestID'] if confirm else uuid.uuid4().hex
                digest_fields = {'version':1,'profile':'reviewed_git_commit_v1','requestID':request_id,'witness':witness['digest'],'action':action}
                digest = hashlib.sha256(json.dumps(digest_fields,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8','strict')).hexdigest()
                if not confirm:
                    return {'version':1,'state':'ready','requestID':request_id,'expectedDigest':digest,'reviewTime':review_time,
                        'review':{'message':message,'author':action['author'],'committer':action['committer'],'branch':git['headRef'],
                            'parentOID':git['headOID'],'coverage':'staged_tree_metadata','unsigned':True,'hooks':False,'changes':changes}}
                if digest != body['expectedDigest']: return {'version':1,'state':'stale'}
                utility.root,utility.idempotency_key,utility.request_hash = 'git-commit',request_id,digest
                candidate = secrets.token_hex(16)
                pending = {'version':1,'state':'pending','candidateToken':candidate,'operation':'git_commit','intentDigest':digest,
                    'locationID':location_id,'workspaceID':root.workspace_id,'chatID':body['chatID'],
                    'policyTransition':{'fromGeneration':generation,'toGeneration':generation}}
                existing = repository.reserve_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,
                    generation=generation,receipt=pending,phase='git_commit_pending')
                if existing is not None:
                    if row_profile(existing) != 'commit': return {'version':1,'state':'unknown'}
                    return {'version':1,'state':'status_only','policyTransition':policy_transition(account,request_id,existing)}
                context = None; issued = False
                try:
                    once = repository.create_git_commit_once(subject_id=account,location_id=location_id,workspace_id=root.workspace_id,
                        chat_id=body['chatID'],operation_id=request_id,request_digest=digest,expected_generation=generation,expires_unix_ms=int(time.time()*1000)+120000)
                    if once['generation'] != generation+1: raise ValueError('Git policy changed')
                    pending['policyTransition']['toGeneration'] = once['generation']
                    repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,generation=generation,receipt=pending,phase='git_commit_pending')
                    consumed = repository.consume_git_commit_once(subject_id=account,location_id=location_id,workspace_id=root.workspace_id,
                        chat_id=body['chatID'],operation_id=request_id,request_digest=digest,binding_id=once['binding_id'],expected_generation=once['generation'])
                    if consumed['generation'] != generation+2: raise ValueError('Git policy changed')
                    active_generation[0] = consumed['generation']; pending['policyTransition']['toGeneration'] = consumed['generation']
                    repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,generation=generation,receipt=pending,phase='git_commit_pending')
                    async def fresh():
                        row = repository.get_binding(once['binding_id'])
                        if (row.status != 'consumed' or row.remaining_uses != 0 or row.generation != active_generation[0] or row.revision != 2
                                or row.subject_id != account or row.location_id != location_id or row.workspace_id != root.workspace_id
                                or row.chat_id != body['chatID'] or row.resource_ref != 'git-commit-action:sha256:'+digest or row.operation != 'git_commit'):
                            return False
                        renewed = await witness_at(active_generation[0]); normalized = dict(renewed['snapshot']); normalized['policyGeneration'] = generation
                        return normalized == witness['snapshot']
                    if not await fresh(): raise ValueError('Git witness changed')
                    capability = secrets.token_hex(32)
                    action_hash = native_commit_action_sha256(session_id=binding.engine_session_id,chat_id=body['chatID'],operation_id=request_id,
                        request_digest=digest,candidate_token=candidate,preview=git,action=action)
                    pending['actionSHA256'] = action_hash
                    repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,generation=generation,receipt=pending,phase='git_commit_pending')
                    context = GitIndexContext(utility,repository,request_id,digest,capability,action_hash,candidate,generation,
                        active_generation[0],dict(pending),fresh,profile='commit')
                    utility.git_index_context = context
                    payload = {'version':1,'stableChatID':body['chatID'],'operationID':request_id,'requestDigest':digest,
                        'capability':capability,'candidateToken':candidate,'expectedPreview':git,'action':action}
                    if len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8','strict')) > 4*1024*1024+128*1024: raise ValueError('Git request too large')
                    if not await fresh(): raise ValueError('Git witness changed')
                    http_task = asyncio.create_task(worker.session_git_commit_apply_request(body['chatID'],binding.engine_session_id,cwd,payload,owner=owner))
                    utility.git_index_http_task = http_task; utility.git_index_drained = False; utility.issued = issued = True
                except Exception:
                    if not issued:
                        if context is None:
                            context = GitIndexContext(utility,repository,request_id,digest,'','',candidate,generation,active_generation[0],dict(pending),lambda:None,profile='commit')
                        result = {'state':'refused','tree':'none','commit':'none','ref':'not_issued','candidateRetained':False,'cleanup':'complete'}
                        settled = _settle(context,result)
                        return {'version':1,'state':'refused' if settled else 'unknown','policyTransition':dict(pending['policyTransition']),
                            **({'effect':{'tree':'none','commit':'none','ref':'not_issued','cleanup':'complete'}} if settled else {})}
                finally:
                    if issued: await _drain_git_index(utility)
                receipt = repository.get_operation(owner_subject_id=account,operation_id=request_id)
                if not context.finished or not receipt or receipt['digest'] != digest or row_profile(receipt) != 'commit':
                    return {'version':1,'state':'unknown','policyTransition':dict(pending['policyTransition'])}
                return {'version':1,'state':receipt['receipt'].get('state','unknown'),'policyTransition':dict(pending['policyTransition']),**public_commit_effect(receipt['receipt'])}
            finally:
                if lease is not None and (not utility.issued or getattr(utility,'git_index_drained',False)): await lease.release()
        except Exception:
            if utility.issued: return {'version':1,'state':'unknown','requestID':utility.idempotency_key,'expectedDigest':utility.request_hash}
            return unavailable

    async def _branch_preview_owned(request, account, owner, admin, auth, barrier, admission, utility, *, confirm=False):
        from src.openclank.transcript_projection import get_managed_binding
        from src.openclank.git_index_authority import GitIndexContext, native_branch_action_sha256, _settle
        unavailable = {'version':1,'state':'unavailable'}
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 128*1024: return unavailable
                raw.extend(chunk)
            body = json.loads(raw)
            keys = {'version','chatID','rootRef','ref'} | ({'requestID','expectedDigest'} if confirm else set())
            if (type(body) is not dict or set(body) != keys or type(body['version']) is not int or body['version'] != 1
                    or any(type(body[k]) is not str or not body[k] or len(body[k].encode('utf-8','strict')) > 8192 for k in ('chatID','rootRef'))): return unavailable
            ref=body['ref']
            if (type(ref) is not str or not re.fullmatch(r'refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,254}',ref)
                    or '..' in ref or '//' in ref or '@{' in ref or ref.endswith(('/','.'))
                    or any(x.startswith('.') or x.endswith('.lock') for x in ref.split('/'))): return unavailable
            if confirm and (type(body['requestID']) is not str or not re.fullmatch(r'[0-9a-f]{32}',body['requestID'])
                    or type(body['expectedDigest']) is not str or not re.fullmatch(r'[0-9a-f]{64}',body['expectedDigest'])): return unavailable
            if confirm:
                utility.idempotency_key,utility.request_hash = body['requestID'],body['expectedDigest']
                utility.git_index_selector_ready = True
                prior = repository.get_operation(owner_subject_id=account,operation_id=body['requestID'])
                if prior is not None:
                    if (prior['digest'] != body['expectedDigest'] or row_profile(prior) != 'branch_create' or prior['receipt'].get('ref') != ref
                            or prior['receipt'].get('chatID') != body['chatID']
                            or prior['receipt'].get('rootRefSHA256') != hashlib.sha256(body['rootRef'].encode('utf-8','strict')).hexdigest()): return {'version':1,'state':'unknown'}
                    return {'version':1,'state':'status_only','policyTransition':policy_transition(account,body['requestID'],prior)}
            generation = repository.generation(); active_generation = [generation]
            root = resolve_resource_ref(body['rootRef'],expected_owner_subject_id=account,current_policy_generation=generation)
            if root.provider != 'host' or root.kind != 'folder': return unavailable
            root_context = ProviderContext(account,owner,generation,is_admin=admin,workspace_id=root.workspace_id)
            registered = await provider.authorize_readonly_root(root_context,origin_id=root.origin_id,expected_revision=None)
            location_id = registered['root_id']
            if root.location_id is not None and root.location_id != location_id: return unavailable
            user,supervisor,binding,mapping = await asyncio.to_thread(_get_observation_binding,request,body['chatID'],None,session_manager)
            if user != owner: return unavailable
            original = get_managed_binding(body['chatID'],owner=owner); cwd = original.get('physicalCwd')
            if (original.get('engineSessionID') != binding.engine_session_id or original.get('transition') is not None
                    or original.get('mappingRevision') != binding.binding_revision or cwd != registered['root']): return unavailable
            pool = supervisor.owner_lifecycle_pool(); lease = None
            try:
                lease,runtime,worker,bridge,process,holder,installed,epoch = await pool.admit_git_index_operation(owner=owner)
                utility.supervisor,utility.worker,utility.bridge,utility.process = pool,worker,bridge,process
                utility.holder,utility.generation,utility.epoch,utility.lease = holder,installed,epoch,lease
                async def current():
                    try:
                        pool._assert_owner_lifecycle(owner,epoch)
                        await asyncio.to_thread(_ensure_observation_binding_current,mapping,owner,body['chatID'],binding)
                        return bool(not utility.cancel_requested and utility.account_current() and await barrier.is_live(owner,admission)
                            and repository.generation() == active_generation[0] and pool._states.get(owner) is runtime
                            and runtime.active is worker and runtime.status == 'ready' and runtime.generation == installed
                            and worker.is_alive() and worker._proc is process and worker._bridge is bridge
                            and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) == holder
                            and get_managed_binding(body['chatID'],owner=owner) == original and not lease._released)
                    except Exception: return False
                async def native():
                    if not await current(): raise ValueError('Git authority changed')
                    response = await worker.session_git_commit_preview_request(body['chatID'],binding.engine_session_id,cwd,owner=owner)
                    if response.status_code != 200 or len(response.content) > 4*1024*1024+128*1024: raise ValueError('Git preview unavailable')
                    value = response.json()
                    if (type(value) is not dict or set(value) != {'version','state','gitCommitControlV1','preview'}
                            or value['version'] != 1 or value['state'] != 'ready' or value['gitCommitControlV1'] is not True): raise ValueError('Git preview unavailable')
                    return value['preview']
                async def witness_at(policy):
                    return await derive_git_commit_witness(utility=utility,provider=provider,repository=repository,account=account,
                        root_origin=root.origin_id,location_id=location_id,workspace_id=root.workspace_id,policy_generation=policy,
                        chat_id=body['chatID'],is_admin=admin,group_ids=(),current=current,native_read=native)
                witness = await witness_at(generation); git = witness['snapshot']['git']
                request_id = body['requestID'] if confirm else uuid.uuid4().hex
                digest_fields = {'version':1,'profile':'reviewed_git_branch_create_v1','requestID':request_id,'witness':witness['digest'],'ref':ref}
                digest = hashlib.sha256(json.dumps(digest_fields,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8','strict')).hexdigest()
                if not confirm:
                    return {'version':1,'state':'ready','requestID':request_id,'expectedDigest':digest,
                        'review':{'ref':ref,'targetOID':git['headOID'],'sourceRef':git['headRef'],'mustBeAbsent':True,'switches':False}}
                if digest != body['expectedDigest']: return {'version':1,'state':'stale'}
                utility.root,utility.idempotency_key,utility.request_hash = 'git-branch-create',request_id,digest
                pending = {'version':1,'state':'pending','operation':'git_branch_create','intentDigest':digest,'ref':ref,
                    'rootRefSHA256':hashlib.sha256(body['rootRef'].encode('utf-8','strict')).hexdigest(),
                    'locationID':location_id,'workspaceID':root.workspace_id,'chatID':body['chatID'],
                    'policyTransition':{'fromGeneration':generation,'toGeneration':generation}}
                existing = repository.reserve_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,
                    generation=generation,receipt=pending,phase='git_branch_create_pending')
                if existing is not None:
                    if row_profile(existing) != 'branch_create': return {'version':1,'state':'unknown'}
                    return {'version':1,'state':'status_only','policyTransition':policy_transition(account,request_id,existing)}
                context = None; issued = False
                try:
                    once = repository.create_git_branch_once(subject_id=account,location_id=location_id,workspace_id=root.workspace_id,
                        chat_id=body['chatID'],operation_id=request_id,request_digest=digest,expected_generation=generation,expires_unix_ms=int(time.time()*1000)+120000)
                    if once['generation'] != generation+1: raise ValueError('Git policy changed')
                    pending['policyTransition']['toGeneration'] = once['generation']
                    repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,generation=generation,receipt=pending,phase='git_branch_create_pending')
                    consumed = repository.consume_git_branch_once(subject_id=account,location_id=location_id,workspace_id=root.workspace_id,
                        chat_id=body['chatID'],operation_id=request_id,request_digest=digest,binding_id=once['binding_id'],expected_generation=once['generation'])
                    if consumed['generation'] != generation+2: raise ValueError('Git policy changed')
                    active_generation[0] = consumed['generation']; pending['policyTransition']['toGeneration'] = consumed['generation']
                    repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,generation=generation,receipt=pending,phase='git_branch_create_pending')
                    async def fresh():
                        row = repository.get_binding(once['binding_id'])
                        if (row.status != 'consumed' or row.remaining_uses != 0 or row.generation != active_generation[0] or row.revision != 2
                                or row.subject_id != account or row.location_id != location_id or row.workspace_id != root.workspace_id
                                or row.chat_id != body['chatID'] or row.resource_ref != 'git-branch-create-action:sha256:'+digest or row.operation != 'git_branch_create'):
                            return False
                        renewed = await witness_at(active_generation[0]); normalized = dict(renewed['snapshot']); normalized['policyGeneration'] = generation
                        return normalized == witness['snapshot']
                    if not await fresh(): raise ValueError('Git witness changed')
                    capability = secrets.token_hex(32)
                    action_hash = native_branch_action_sha256(session_id=binding.engine_session_id,chat_id=body['chatID'],operation_id=request_id,
                        request_digest=digest,preview=git,ref=ref)
                    pending['actionSHA256'] = action_hash
                    repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,generation=generation,receipt=pending,phase='git_branch_create_pending')
                    context = GitIndexContext(utility,repository,request_id,digest,capability,action_hash,None,generation,
                        active_generation[0],dict(pending),fresh,profile='branch_create')
                    utility.git_index_context = context
                    payload = {'version':1,'stableChatID':body['chatID'],'operationID':request_id,'requestDigest':digest,
                        'capability':capability,'expectedPreview':git,'ref':ref}
                    if len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8','strict')) > 4*1024*1024+128*1024: raise ValueError('Git request too large')
                    if not await fresh(): raise ValueError('Git witness changed')
                    http_task = asyncio.create_task(worker.session_git_branch_apply_request(body['chatID'],binding.engine_session_id,cwd,payload,owner=owner))
                    utility.git_index_http_task = http_task; utility.git_index_drained = False; utility.issued = issued = True
                except Exception:
                    if not issued:
                        if context is None:
                            context = GitIndexContext(utility,repository,request_id,digest,'','',None,generation,active_generation[0],dict(pending),lambda:None,profile='branch_create')
                        result = {'state':'refused','ref':'not_issued','cleanup':'complete'}
                        settled = _settle(context,result)
                        return {'version':1,'state':'refused' if settled else 'unknown','policyTransition':dict(pending['policyTransition']),
                            **({'effect':{'ref':'not_issued','cleanup':'complete'}} if settled else {})}
                finally:
                    if issued: await _drain_git_index(utility)
                receipt = repository.get_operation(owner_subject_id=account,operation_id=request_id)
                if not context.finished or not receipt or receipt['digest'] != digest or row_profile(receipt) != 'branch_create':
                    return {'version':1,'state':'unknown','policyTransition':dict(pending['policyTransition'])}
                return {'version':1,'state':receipt['receipt'].get('state','unknown'),'policyTransition':dict(pending['policyTransition']),**public_branch_effect(receipt['receipt'])}
            finally:
                if lease is not None and (not utility.issued or getattr(utility,'git_index_drained',False)): await lease.release()
        except Exception:
            if utility.issued: return {'version':1,'state':'unknown','requestID':utility.idempotency_key,'expectedDigest':utility.request_hash}
            return unavailable

    async def _fetch_preview_owned(request, account, owner, admin, auth, barrier, admission, utility, *, confirm=False):
        from src.openclank.transcript_projection import get_managed_binding
        from src.openclank.git_index_authority import GitIndexContext, native_fetch_action_sha256, _settle
        unavailable = {'version':1,'state':'unavailable'}
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 128*1024: return unavailable
                raw.extend(chunk)
            body = json.loads(raw)
            keys = {'version','chatID','rootRef','remoteName','branch','remoteRef'} | ({'requestID','expectedDigest','targetOID'} if confirm else set())
            if (type(body) is not dict or set(body) != keys or type(body['version']) is not int or body['version'] != 1
                    or any(type(body[k]) is not str or not body[k] or len(body[k].encode('utf-8','strict')) > 8192 for k in ('chatID','rootRef'))
                    or type(body['remoteName']) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',body['remoteName'])
                    or type(body['branch']) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,254}',body['branch'])
                    or '..' in body['branch'] or '//' in body['branch'] or body['branch'].endswith(('/','.'))
                    or any(x.startswith('.') or x.endswith('.lock') for x in body['branch'].split('/'))
                    or (body['remoteRef'] is not None and (type(body['remoteRef']) is not str or not body['remoteRef'] or len(body['remoteRef'])>8192))): return unavailable
            remote_ref_hash = None if body['remoteRef'] is None else hashlib.sha256(body['remoteRef'].encode('utf-8','strict')).hexdigest()
            if confirm and (type(body['requestID']) is not str or not re.fullmatch(r'[0-9a-f]{32}',body['requestID'])
                    or type(body['expectedDigest']) is not str or not re.fullmatch(r'[0-9a-f]{64}',body['expectedDigest'])
                    or type(body['targetOID']) is not str or not re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?',body['targetOID'])): return unavailable
            if confirm:
                utility.idempotency_key,utility.request_hash = body['requestID'],body['expectedDigest']
                utility.git_index_selector_ready = True
                prior = repository.get_operation(owner_subject_id=account,operation_id=body['requestID'])
                if prior is not None:
                    receipt=prior.get('receipt') or {}
                    if (prior['digest'] != body['expectedDigest'] or row_profile(prior) != 'fetch' or receipt.get('chatID') != body['chatID']
                            or receipt.get('rootRefSHA256') != hashlib.sha256(body['rootRef'].encode('utf-8','strict')).hexdigest()
                            or receipt.get('remoteRefSHA256') != remote_ref_hash or receipt.get('remoteName') != body['remoteName']
                            or receipt.get('branch') != body['branch'] or receipt.get('targetOID') != body['targetOID']): return {'version':1,'state':'unknown'}
                    return {'version':1,'state':'status_only','policyTransition':policy_transition(account,body['requestID'],prior)}
            generation = repository.generation(); active_generation = [generation]
            root = resolve_resource_ref(body['rootRef'],expected_owner_subject_id=account,current_policy_generation=generation)
            if root.provider != 'host' or root.kind != 'folder': return unavailable
            root_context = ProviderContext(account,owner,generation,is_admin=admin,workspace_id=root.workspace_id)
            registered = await provider.authorize_readonly_root(root_context,origin_id=root.origin_id,expected_revision=None)
            location_id = registered['root_id']
            if root.location_id is not None and root.location_id != location_id: return unavailable
            user,supervisor,binding,mapping = await asyncio.to_thread(_get_observation_binding,request,body['chatID'],None,session_manager)
            if user != owner: return unavailable
            original = get_managed_binding(body['chatID'],owner=owner); cwd = original.get('physicalCwd')
            if (original.get('engineSessionID') != binding.engine_session_id or original.get('transition') is not None
                    or original.get('mappingRevision') != binding.binding_revision or cwd != registered['root']): return unavailable
            pool = supervisor.owner_lifecycle_pool(); lease = None
            try:
                lease,runtime,worker,bridge,process,holder,installed,epoch = await pool.admit_git_index_operation(owner=owner)
                utility.supervisor,utility.worker,utility.bridge,utility.process = pool,worker,bridge,process
                utility.holder,utility.generation,utility.epoch,utility.lease = holder,installed,epoch,lease
                async def current():
                    try:
                        pool._assert_owner_lifecycle(owner,epoch)
                        await asyncio.to_thread(_ensure_observation_binding_current,mapping,owner,body['chatID'],binding)
                        return bool(not utility.cancel_requested and utility.account_current() and await barrier.is_live(owner,admission)
                            and repository.generation() == active_generation[0] and pool._states.get(owner) is runtime
                            and runtime.active is worker and runtime.status == 'ready' and runtime.generation == installed
                            and worker.is_alive() and worker._proc is process and worker._bridge is bridge
                            and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) == holder
                            and get_managed_binding(body['chatID'],owner=owner) == original and not lease._released)
                    except Exception: return False
                async def native():
                    if not await current(): raise ValueError('Git authority changed')
                    response = await worker.session_git_commit_preview_request(body['chatID'],binding.engine_session_id,cwd,owner=owner)
                    if response.status_code != 200 or len(response.content) > 4*1024*1024+128*1024: raise ValueError('Git preview unavailable')
                    value = response.json()
                    if (type(value) is not dict or set(value) != {'version','state','gitCommitControlV1','preview'}
                            or value['version'] != 1 or value['state'] != 'ready' or value['gitCommitControlV1'] is not True): raise ValueError('Git preview unavailable')
                    return value['preview']
                async def witness_at(policy):
                    return await derive_git_commit_witness(utility=utility,provider=provider,repository=repository,account=account,
                        root_origin=root.origin_id,location_id=location_id,workspace_id=root.workspace_id,policy_generation=policy,
                        chat_id=body['chatID'],is_admin=admin,group_ids=(),current=current,native_read=native)
                witness = await witness_at(generation); git = witness['snapshot']['git']
                async def remote_info(stage, remote_url=None, local_remote=None):
                    payload={'version':1,'stableChatID':body['chatID'],'expectedPreview':git,'remoteName':body['remoteName'],'branch':body['branch'],'stage':stage}
                    if stage == 'probe': payload.update({'remoteURL':remote_url,'localRemote':local_remote})
                    if not await current(): raise ValueError('Git authority changed')
                    response=await worker.session_git_remote_preview_request(body['chatID'],binding.engine_session_id,cwd,payload,owner=owner)
                    if response.status_code != 200 or len(response.content)>4*1024*1024+128*1024: raise ValueError('remote unavailable')
                    value=response.json(); expected={'version','state','info'} | ({'targetOID'} if stage == 'probe' else set())
                    if type(value) is not dict or set(value)!=expected or value['version']!=1 or value['state']!='ready' or type(value['info']) is not dict or set(value['info'])!={'remoteURL','sourceRef','trackingRef','oldOID'}: raise ValueError('remote unavailable')
                    info=value['info']; width=40 if git['physicalIndex']['objectFormat']=='sha1' else 64
                    if (type(info['remoteURL']) is not str or not info['remoteURL'] or len(info['remoteURL'].encode('utf-8','strict'))>4096
                            or any(ord(c)<=32 or ord(c)==127 for c in info['remoteURL'])
                            or info['sourceRef']!='refs/heads/'+body['branch'] or info['trackingRef']!='refs/remotes/'+body['remoteName']+'/'+body['branch']
                            or (info['oldOID'] is not None and (type(info['oldOID']) is not str or not re.fullmatch('[0-9a-f]{%d}'%width,info['oldOID'])))): raise ValueError('remote unavailable')
                    if stage == 'probe' and (type(value['targetOID']) is not str or not re.fullmatch('[0-9a-f]{%d}'%width,value['targetOID'])): raise ValueError('remote unavailable')
                    return value
                info=(await remote_info('config'))['info']; remote_url=info['remoteURL']
                from urllib.parse import urlsplit, unquote
                from src.openclank.files_host_provider import _path
                parsed=urlsplit(remote_url); local_path=None; display_host=None
                if remote_url.startswith('/'):
                    local_path=remote_url
                elif parsed.scheme=='file' and not parsed.netloc and not parsed.query and not parsed.fragment:
                    local_path=unquote(parsed.path,errors='strict')
                elif parsed.scheme=='https' and parsed.hostname and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment:
                    _=parsed.port; display_host=parsed.hostname
                else: return unavailable
                if local_path is not None and body['remoteRef'] is None: return {'version':1,'state':'choose_remote'}
                if local_path is None and body['remoteRef'] is not None: return unavailable
                remote_resource=None; initial_remote=None
                if local_path is not None:
                    if not os.path.isabs(local_path) or os.path.realpath(local_path)!=local_path: return unavailable
                    remote_resource=resolve_resource_ref(body['remoteRef'],expected_owner_subject_id=account,current_policy_generation=generation)
                    if remote_resource.provider!='host' or remote_resource.kind!='folder' or _path(remote_resource.origin_id)!=local_path: return unavailable
                async def remote_authority(policy):
                    if remote_resource is None: return None
                    ctx=ProviderContext(account,owner,policy,is_admin=admin,workspace_id=remote_resource.workspace_id or 'default')
                    selected=await provider.authorize_readonly_root(ctx,origin_id=remote_resource.origin_id,expected_revision=None)
                    if selected['root']!=local_path or os.path.realpath(local_path)!=local_path: raise ValueError('remote changed')
                    fd=os.open(local_path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
                    try:
                        held=os.fstat(fd); observed=os.lstat(local_path)
                        if (held.st_dev,held.st_ino)!=(observed.st_dev,observed.st_ino) or not 0<=held.st_dev<=9007199254740991 or not 0<held.st_ino<=9007199254740991: raise ValueError('remote changed')
                        return {'device':held.st_dev,'inode':held.st_ino}
                    finally: os.close(fd)
                initial_remote=await remote_authority(generation)
                if not await current(): return unavailable
                if confirm: target=body['targetOID']
                else:
                    probed=await remote_info('probe',remote_url,initial_remote)
                    if probed['info']!=info: return {'version':1,'state':'stale'}
                    target=probed['targetOID']
                if not await current() or await remote_authority(generation)!=initial_remote: return unavailable
                request_id = body['requestID'] if confirm else uuid.uuid4().hex
                digest_fields={'version':1,'profile':'reviewed_git_fetch_v1','requestID':request_id,'witness':witness['digest'],
                    'remoteName':body['remoteName'],'branch':body['branch'],'urlSHA256':hashlib.sha256(remote_url.encode('utf-8','strict')).hexdigest(),
                    'remoteRefSHA256':remote_ref_hash,'localRemote':initial_remote,'trackingRef':info['trackingRef'],'oldOID':info['oldOID'],'targetOID':target}
                digest=hashlib.sha256(json.dumps(digest_fields,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8','strict')).hexdigest()
                if not confirm:
                    return {'version':1,'state':'ready','requestID':request_id,'expectedDigest':digest,
                        'review':{'remoteName':body['remoteName'],'branch':body['branch'],'targetOID':target,'oldOID':info['oldOID'],
                            'transport':'local' if local_path is not None else 'https','host':display_host,'remoteMayMove':True}}
                if digest != body['expectedDigest']: return {'version':1,'state':'stale'}
                utility.root,utility.idempotency_key,utility.request_hash = 'git-fetch',request_id,digest
                pending = {'version':1,'state':'pending','operation':'git_fetch','intentDigest':digest,'remoteName':body['remoteName'],'branch':body['branch'],'targetOID':target,
                    'remoteRefSHA256':remote_ref_hash,'rootRefSHA256':hashlib.sha256(body['rootRef'].encode('utf-8','strict')).hexdigest(),
                    'locationID':location_id,'workspaceID':root.workspace_id,'chatID':body['chatID'],
                    'policyTransition':{'fromGeneration':generation,'toGeneration':generation}}
                existing = repository.reserve_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,
                    generation=generation,receipt=pending,phase='git_fetch_pending')
                if existing is not None:
                    if row_profile(existing) != 'fetch': return {'version':1,'state':'unknown'}
                    return {'version':1,'state':'status_only','policyTransition':policy_transition(account,request_id,existing)}
                context = None; issued = False
                try:
                    once = repository.create_git_fetch_once(subject_id=account,location_id=location_id,workspace_id=root.workspace_id,
                        chat_id=body['chatID'],operation_id=request_id,request_digest=digest,expected_generation=generation,expires_unix_ms=int(time.time()*1000)+120000)
                    if once['generation'] != generation+1: raise ValueError('Git policy changed')
                    pending['policyTransition']['toGeneration'] = once['generation']
                    repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,generation=generation,receipt=pending,phase='git_fetch_pending')
                    consumed = repository.consume_git_fetch_once(subject_id=account,location_id=location_id,workspace_id=root.workspace_id,
                        chat_id=body['chatID'],operation_id=request_id,request_digest=digest,binding_id=once['binding_id'],expected_generation=once['generation'])
                    if consumed['generation'] != generation+2: raise ValueError('Git policy changed')
                    active_generation[0] = consumed['generation']; pending['policyTransition']['toGeneration'] = consumed['generation']
                    repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,generation=generation,receipt=pending,phase='git_fetch_pending')
                    async def fresh():
                        row = repository.get_binding(once['binding_id'])
                        if (row.status != 'consumed' or row.remaining_uses != 0 or row.generation != active_generation[0] or row.revision != 2
                                or row.subject_id != account or row.location_id != location_id or row.workspace_id != root.workspace_id
                                or row.chat_id != body['chatID'] or row.resource_ref != 'git-fetch-action:sha256:'+digest or row.operation != 'git_fetch'):
                            return False
                        renewed = await witness_at(active_generation[0]); normalized = dict(renewed['snapshot']); normalized['policyGeneration'] = generation
                        return (normalized == witness['snapshot'] and (await remote_info('config'))['info'] == info
                            and await remote_authority(active_generation[0]) == initial_remote)
                    if not await fresh(): raise ValueError('Git witness changed')
                    capability = secrets.token_hex(32)
                    action_hash = native_fetch_action_sha256(session_id=binding.engine_session_id,chat_id=body['chatID'],operation_id=request_id,
                        request_digest=digest,preview=git,remote_url=remote_url,tracking_ref=info['trackingRef'],old_oid=info['oldOID'],target_oid=target,local_remote=initial_remote)
                    pending['actionSHA256'] = action_hash
                    repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,generation=generation,receipt=pending,phase='git_fetch_pending')
                    context = GitIndexContext(utility,repository,request_id,digest,capability,action_hash,None,generation,
                        active_generation[0],dict(pending),fresh,profile='fetch')
                    utility.git_index_context = context
                    payload = {'version':1,'stableChatID':body['chatID'],'operationID':request_id,'requestDigest':digest,
                        'capability':capability,'expectedPreview':git,'remoteURL':remote_url,'trackingRef':info['trackingRef'],
                        'oldOID':info['oldOID'],'targetOID':target,'localRemote':initial_remote}
                    if len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8','strict')) > 4*1024*1024+128*1024: raise ValueError('Git request too large')
                    if not await fresh(): raise ValueError('Git witness changed')
                    http_task = asyncio.create_task(worker.session_git_fetch_apply_request(body['chatID'],binding.engine_session_id,cwd,payload,owner=owner))
                    utility.git_index_http_task = http_task; utility.git_index_drained = False; utility.issued = issued = True
                except Exception:
                    if not issued:
                        if context is None:
                            context = GitIndexContext(utility,repository,request_id,digest,'','',None,generation,active_generation[0],dict(pending),lambda:None,profile='fetch')
                        result = {'state':'refused','transfer':'not_issued','ref':'not_issued','cleanup':'complete'}
                        settled = _settle(context,result)
                        return {'version':1,'state':'refused' if settled else 'unknown','policyTransition':dict(pending['policyTransition']),
                            **({'effect':{'transfer':'not_issued','ref':'not_issued','cleanup':'complete'}} if settled else {})}
                finally:
                    if issued: await _drain_git_index(utility)
                receipt = repository.get_operation(owner_subject_id=account,operation_id=request_id)
                if not context.finished or not receipt or receipt['digest'] != digest or row_profile(receipt) != 'fetch':
                    return {'version':1,'state':'unknown','policyTransition':dict(pending['policyTransition'])}
                return {'version':1,'state':receipt['receipt'].get('state','unknown'),'policyTransition':dict(pending['policyTransition']),**public_fetch_effect(receipt['receipt'])}
            finally:
                if lease is not None and (not utility.issued or getattr(utility,'git_index_drained',False)): await lease.release()
        except Exception:
            if utility.issued: return {'version':1,'state':'unknown','requestID':utility.idempotency_key,'expectedDigest':utility.request_hash}
            return unavailable

    async def _push_preview_owned(request, account, owner, admin, auth, barrier, admission, utility, *, confirm=False):
        from src.openclank.transcript_projection import get_managed_binding
        from src.openclank.git_index_authority import GitIndexContext, native_push_action_sha256, _settle
        unavailable = {'version':1,'state':'unavailable'}
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 128*1024: return unavailable
                raw.extend(chunk)
            body = json.loads(raw)
            keys = {'version','chatID','rootRef','remoteName','branch','remoteRef'} | ({'requestID','expectedDigest','targetOID','observedOID'} if confirm else set())
            if (type(body) is not dict or set(body) != keys or type(body['version']) is not int or body['version'] != 1
                    or any(type(body[k]) is not str or not body[k] or len(body[k].encode('utf-8','strict')) > 8192 for k in ('chatID','rootRef'))
                    or type(body['remoteName']) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',body['remoteName'])
                    or type(body['branch']) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,254}',body['branch'])
                    or '..' in body['branch'] or '//' in body['branch'] or body['branch'].endswith(('/','.'))
                    or any(x.startswith('.') or x.endswith('.lock') for x in body['branch'].split('/'))
                    or (body['remoteRef'] is not None and (type(body['remoteRef']) is not str or not body['remoteRef'] or len(body['remoteRef'])>8192))): return unavailable
            remote_ref_hash = None if body['remoteRef'] is None else hashlib.sha256(body['remoteRef'].encode('utf-8','strict')).hexdigest()
            if confirm and (type(body['requestID']) is not str or not re.fullmatch(r'[0-9a-f]{32}',body['requestID'])
                    or type(body['expectedDigest']) is not str or not re.fullmatch(r'[0-9a-f]{64}',body['expectedDigest'])
                    or type(body['targetOID']) is not str or not re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?',body['targetOID'])
                    or (body['observedOID'] is not None and (type(body['observedOID']) is not str or not re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?',body['observedOID'])))): return unavailable
            if confirm:
                utility.idempotency_key,utility.request_hash = body['requestID'],body['expectedDigest']
                utility.git_index_selector_ready = True
                prior = repository.get_operation(owner_subject_id=account,operation_id=body['requestID'])
                if prior is not None:
                    receipt=prior.get('receipt') or {}
                    if (prior['digest'] != body['expectedDigest'] or row_profile(prior) != 'push' or receipt.get('chatID') != body['chatID']
                            or receipt.get('rootRefSHA256') != hashlib.sha256(body['rootRef'].encode('utf-8','strict')).hexdigest()
                            or receipt.get('remoteRefSHA256') != remote_ref_hash or receipt.get('remoteName') != body['remoteName']
                            or receipt.get('branch') != body['branch'] or receipt.get('targetOID') != body['targetOID'] or receipt.get('observedOID') != body['observedOID']): return {'version':1,'state':'unknown'}
                    return {'version':1,'state':'status_only','policyTransition':policy_transition(account,body['requestID'],prior)}
            generation = repository.generation(); active_generation = [generation]
            root = resolve_resource_ref(body['rootRef'],expected_owner_subject_id=account,current_policy_generation=generation)
            if root.provider != 'host' or root.kind != 'folder': return unavailable
            root_context = ProviderContext(account,owner,generation,is_admin=admin,workspace_id=root.workspace_id)
            registered = await provider.authorize_readonly_root(root_context,origin_id=root.origin_id,expected_revision=None)
            location_id = registered['root_id']
            if root.location_id is not None and root.location_id != location_id: return unavailable
            user,supervisor,binding,mapping = await asyncio.to_thread(_get_observation_binding,request,body['chatID'],None,session_manager)
            if user != owner: return unavailable
            original = get_managed_binding(body['chatID'],owner=owner); cwd = original.get('physicalCwd')
            if (original.get('engineSessionID') != binding.engine_session_id or original.get('transition') is not None
                    or original.get('mappingRevision') != binding.binding_revision or cwd != registered['root']): return unavailable
            pool = supervisor.owner_lifecycle_pool(); lease = None
            try:
                lease,runtime,worker,bridge,process,holder,installed,epoch = await pool.admit_git_index_operation(owner=owner)
                utility.supervisor,utility.worker,utility.bridge,utility.process = pool,worker,bridge,process
                utility.holder,utility.generation,utility.epoch,utility.lease = holder,installed,epoch,lease
                async def current():
                    try:
                        pool._assert_owner_lifecycle(owner,epoch)
                        await asyncio.to_thread(_ensure_observation_binding_current,mapping,owner,body['chatID'],binding)
                        return bool(not utility.cancel_requested and utility.account_current() and await barrier.is_live(owner,admission)
                            and repository.generation() == active_generation[0] and pool._states.get(owner) is runtime
                            and runtime.active is worker and runtime.status == 'ready' and runtime.generation == installed
                            and worker.is_alive() and worker._proc is process and worker._bridge is bridge
                            and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) == holder
                            and get_managed_binding(body['chatID'],owner=owner) == original and not lease._released)
                    except Exception: return False
                async def native():
                    if not await current(): raise ValueError('Git authority changed')
                    response = await worker.session_git_commit_preview_request(body['chatID'],binding.engine_session_id,cwd,owner=owner)
                    if response.status_code != 200 or len(response.content) > 4*1024*1024+128*1024: raise ValueError('Git preview unavailable')
                    value = response.json()
                    if (type(value) is not dict or set(value) != {'version','state','gitCommitControlV1','preview'}
                            or value['version'] != 1 or value['state'] != 'ready' or value['gitCommitControlV1'] is not True): raise ValueError('Git preview unavailable')
                    return value['preview']
                async def witness_at(policy):
                    return await derive_git_commit_witness(utility=utility,provider=provider,repository=repository,account=account,
                        root_origin=root.origin_id,location_id=location_id,workspace_id=root.workspace_id,policy_generation=policy,
                        chat_id=body['chatID'],is_admin=admin,group_ids=(),current=current,native_read=native)
                witness = await witness_at(generation); git = witness['snapshot']['git']
                async def remote_info(stage, remote_url=None, local_remote=None):
                    payload={'version':1,'stableChatID':body['chatID'],'expectedPreview':git,'remoteName':body['remoteName'],'branch':body['branch'],'stage':stage}
                    if stage == 'pushProbe': payload.update({'remoteURL':remote_url,'localRemote':local_remote})
                    if not await current(): raise ValueError('Git authority changed')
                    response=await worker.session_git_remote_preview_request(body['chatID'],binding.engine_session_id,cwd,payload,owner=owner)
                    if response.status_code != 200 or len(response.content)>4*1024*1024+128*1024: raise ValueError('remote unavailable')
                    value=response.json(); expected={'version','state','info'} | ({'targetOID'} if stage == 'pushProbe' else set())
                    if type(value) is not dict or set(value)!=expected or value['version']!=1 or value['state']!='ready' or type(value['info']) is not dict or set(value['info'])!={'remoteURL','sourceRef','trackingRef','oldOID'}: raise ValueError('remote unavailable')
                    info=value['info']; width=40 if git['physicalIndex']['objectFormat']=='sha1' else 64
                    if (type(info['remoteURL']) is not str or not info['remoteURL'] or len(info['remoteURL'].encode('utf-8','strict'))>4096
                            or any(ord(c)<=32 or ord(c)==127 for c in info['remoteURL'])
                            or info['sourceRef']!='refs/heads/'+body['branch'] or info['trackingRef']!='refs/remotes/'+body['remoteName']+'/'+body['branch']
                            or (info['oldOID'] is not None and (type(info['oldOID']) is not str or not re.fullmatch('[0-9a-f]{%d}'%width,info['oldOID'])))): raise ValueError('remote unavailable')
                    if stage == 'pushProbe' and value['targetOID'] is not None and (type(value['targetOID']) is not str or not re.fullmatch('[0-9a-f]{%d}'%width,value['targetOID'])): raise ValueError('remote unavailable')
                    return value
                info=(await remote_info('pushConfig'))['info']; remote_url=info['remoteURL']
                from urllib.parse import urlsplit, unquote
                from src.openclank.files_host_provider import _path
                parsed=urlsplit(remote_url); local_path=None; display_host=None
                if remote_url.startswith('/'):
                    local_path=remote_url
                elif parsed.scheme=='file' and not parsed.netloc and not parsed.query and not parsed.fragment:
                    local_path=unquote(parsed.path,errors='strict')
                elif parsed.scheme=='https' and parsed.hostname and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment:
                    _=parsed.port; display_host=parsed.hostname
                else: return unavailable
                if local_path is not None and body['remoteRef'] is None: return {'version':1,'state':'choose_remote'}
                if local_path is None and body['remoteRef'] is not None: return unavailable
                remote_resource=None; initial_remote=None
                if local_path is not None:
                    if not os.path.isabs(local_path) or os.path.realpath(local_path)!=local_path: return unavailable
                    remote_resource=resolve_resource_ref(body['remoteRef'],expected_owner_subject_id=account,current_policy_generation=generation)
                    if remote_resource.provider!='host' or remote_resource.kind!='folder' or _path(remote_resource.origin_id)!=local_path: return unavailable
                async def remote_authority(policy):
                    if remote_resource is None: return None
                    ctx=ProviderContext(account,owner,policy,is_admin=admin,workspace_id=remote_resource.workspace_id or 'default')
                    selected=await provider.authorize_readonly_root(ctx,origin_id=remote_resource.origin_id,expected_revision=None)
                    if selected['root']!=local_path or os.path.realpath(local_path)!=local_path: raise ValueError('remote changed')
                    scope=provider._scope(ctx)
                    if not provider._path_writable(ctx,local_path,scope): raise ValueError('remote write unavailable')
                    effective=repository.resolve(subject_id=account,is_admin=admin,origin='app',location_id=selected['root_id'],capability='write',workspace_id=remote_resource.workspace_id,relative_resource='',chat_id=body['chatID'],group_ids=(),observation_only=True)
                    if not effective.allowed or effective.policy_generation!=policy: raise ValueError('remote write unavailable')
                    fd=os.open(local_path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
                    try:
                        held=os.fstat(fd); observed=os.lstat(local_path)
                        if (held.st_dev,held.st_ino)!=(observed.st_dev,observed.st_ino) or not 0<=held.st_dev<=9007199254740991 or not 0<held.st_ino<=9007199254740991: raise ValueError('remote changed')
                        return {'device':held.st_dev,'inode':held.st_ino}
                    finally: os.close(fd)
                initial_remote=await remote_authority(generation)
                if not await current(): return unavailable
                target=git['headOID']
                if confirm:
                    if body['targetOID'] != target: return {'version':1,'state':'stale'}
                    observed=body['observedOID']
                else:
                    probed=await remote_info('pushProbe',remote_url,initial_remote)
                    if probed['info']!=info: return {'version':1,'state':'stale'}
                    observed=probed['targetOID']
                if not await current() or await remote_authority(generation)!=initial_remote: return unavailable
                request_id = body['requestID'] if confirm else uuid.uuid4().hex
                digest_fields={'version':1,'profile':'reviewed_git_push_v1','requestID':request_id,'witness':witness['digest'],
                    'remoteName':body['remoteName'],'branch':body['branch'],'urlSHA256':hashlib.sha256(remote_url.encode('utf-8','strict')).hexdigest(),
                    'remoteRefSHA256':remote_ref_hash,'localRemote':initial_remote,'destinationRef':info['sourceRef'],'observedOID':observed,'targetOID':target}
                digest=hashlib.sha256(json.dumps(digest_fields,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8','strict')).hexdigest()
                if not confirm:
                    return {'version':1,'state':'ready','requestID':request_id,'expectedDigest':digest,
                        'review':{'remoteName':body['remoteName'],'branch':body['branch'],'targetOID':target,'observedOID':observed,
                            'transport':'local' if local_path is not None else 'https','host':display_host,'remoteMayMove':True,'force':False}}
                if digest != body['expectedDigest']: return {'version':1,'state':'stale'}
                utility.root,utility.idempotency_key,utility.request_hash = 'git-push',request_id,digest
                pending = {'version':1,'state':'pending','operation':'git_push','intentDigest':digest,'remoteName':body['remoteName'],'branch':body['branch'],'targetOID':target,'observedOID':observed,
                    'remoteRefSHA256':remote_ref_hash,'rootRefSHA256':hashlib.sha256(body['rootRef'].encode('utf-8','strict')).hexdigest(),
                    'locationID':location_id,'workspaceID':root.workspace_id,'chatID':body['chatID'],
                    'policyTransition':{'fromGeneration':generation,'toGeneration':generation}}
                existing = repository.reserve_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,
                    generation=generation,receipt=pending,phase='git_push_pending')
                if existing is not None:
                    if row_profile(existing) != 'push': return {'version':1,'state':'unknown'}
                    return {'version':1,'state':'status_only','policyTransition':policy_transition(account,request_id,existing)}
                context = None; issued = False
                try:
                    once = repository.create_git_push_once(subject_id=account,location_id=location_id,workspace_id=root.workspace_id,
                        chat_id=body['chatID'],operation_id=request_id,request_digest=digest,expected_generation=generation,expires_unix_ms=int(time.time()*1000)+120000)
                    if once['generation'] != generation+1: raise ValueError('Git policy changed')
                    pending['policyTransition']['toGeneration'] = once['generation']
                    repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,generation=generation,receipt=pending,phase='git_push_pending')
                    consumed = repository.consume_git_push_once(subject_id=account,location_id=location_id,workspace_id=root.workspace_id,
                        chat_id=body['chatID'],operation_id=request_id,request_digest=digest,binding_id=once['binding_id'],expected_generation=once['generation'])
                    if consumed['generation'] != generation+2: raise ValueError('Git policy changed')
                    active_generation[0] = consumed['generation']; pending['policyTransition']['toGeneration'] = consumed['generation']
                    repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,generation=generation,receipt=pending,phase='git_push_pending')
                    async def fresh():
                        row = repository.get_binding(once['binding_id'])
                        if (row.status != 'consumed' or row.remaining_uses != 0 or row.generation != active_generation[0] or row.revision != 2
                                or row.subject_id != account or row.location_id != location_id or row.workspace_id != root.workspace_id
                                or row.chat_id != body['chatID'] or row.resource_ref != 'git-push-action:sha256:'+digest or row.operation != 'git_push'):
                            return False
                        renewed = await witness_at(active_generation[0]); normalized = dict(renewed['snapshot']); normalized['policyGeneration'] = generation
                        return (normalized == witness['snapshot'] and (await remote_info('pushConfig'))['info'] == info
                            and await remote_authority(active_generation[0]) == initial_remote)
                    if not await fresh(): raise ValueError('Git witness changed')
                    capability = secrets.token_hex(32)
                    action_hash = native_push_action_sha256(session_id=binding.engine_session_id,chat_id=body['chatID'],operation_id=request_id,
                        request_digest=digest,preview=git,remote_url=remote_url,destination_ref=info['sourceRef'],observed_oid=observed,local_remote=initial_remote)
                    pending['actionSHA256'] = action_hash
                    repository.record_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,generation=generation,receipt=pending,phase='git_push_pending')
                    context = GitIndexContext(utility,repository,request_id,digest,capability,action_hash,None,generation,
                        active_generation[0],dict(pending),fresh,profile='push')
                    utility.git_index_context = context
                    payload = {'version':1,'stableChatID':body['chatID'],'operationID':request_id,'requestDigest':digest,
                        'capability':capability,'expectedPreview':git,'remoteURL':remote_url,'destinationRef':info['sourceRef'],
                        'observedOID':observed,'localRemote':initial_remote}
                    if len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8','strict')) > 4*1024*1024+128*1024: raise ValueError('Git request too large')
                    if not await fresh(): raise ValueError('Git witness changed')
                    http_task = asyncio.create_task(worker.session_git_push_apply_request(body['chatID'],binding.engine_session_id,cwd,payload,owner=owner))
                    utility.git_index_http_task = http_task; utility.git_index_drained = False; utility.issued = issued = True
                except Exception:
                    if not issued:
                        if context is None:
                            context = GitIndexContext(utility,repository,request_id,digest,'','',None,generation,active_generation[0],dict(pending),lambda:None,profile='push')
                        result = {'state':'refused','remote':'not_issued','cleanup':'complete'}
                        settled = _settle(context,result)
                        return {'version':1,'state':'refused' if settled else 'unknown','policyTransition':dict(pending['policyTransition']),
                            **({'effect':{'remote':'not_issued','cleanup':'complete'}} if settled else {})}
                finally:
                    if issued: await _drain_git_index(utility)
                receipt = repository.get_operation(owner_subject_id=account,operation_id=request_id)
                if not context.finished or not receipt or receipt['digest'] != digest or row_profile(receipt) != 'push':
                    return {'version':1,'state':'unknown','policyTransition':dict(pending['policyTransition'])}
                return {'version':1,'state':receipt['receipt'].get('state','unknown'),'policyTransition':dict(pending['policyTransition']),**public_push_effect(receipt['receipt'])}
            finally:
                if lease is not None and (not utility.issued or getattr(utility,'git_index_drained',False)): await lease.release()
        except Exception:
            if utility.issued: return {'version':1,'state':'unknown','requestID':utility.idempotency_key,'expectedDigest':utility.request_hash}
            return unavailable

    async def _discard_preview_owned(request, account, owner, admin, auth, barrier, admission, utility, *, confirm=False):
        """Read-only preview from one retained human/worker lifetime."""
        from src.openclank.transcript_projection import get_managed_binding
        unavailable = {'version':1, 'state':'unavailable'}
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 32768: return unavailable
                raw.extend(chunk)
            body = json.loads(raw)
            keys={'version','chatID','rootRef','fileRef'} | ({'requestID','expectedDigest'} if confirm else set())
            if (type(body) is not dict or set(body)!=keys or type(body['version']) is not int or body['version']!=1
                    or any(type(body[k]) is not str or not body[k] or len(body[k])>8192 for k in ('chatID','rootRef','fileRef'))): return unavailable
            if confirm and (type(body['requestID']) is not str or not re.fullmatch(r'[0-9a-f]{32}',body['requestID'])
                    or type(body['expectedDigest']) is not str or not re.fullmatch(r'[0-9a-f]{64}',body['expectedDigest'])): return unavailable
            if confirm:
                utility.idempotency_key,utility.request_hash=body['requestID'],body['expectedDigest'];utility.git_index_selector_ready=True
                prior=repository.get_operation(owner_subject_id=account,operation_id=body['requestID'])
                if prior is not None:
                    receipt=prior.get('receipt') or {}
                    if (prior['digest']!=body['expectedDigest'] or receipt.get('kind')!='git.discard.v1' or receipt.get('chatID')!=body['chatID']
                            or receipt.get('rootRefSHA256')!=hashlib.sha256(body['rootRef'].encode()).hexdigest()
                            or receipt.get('fileRefSHA256')!=hashlib.sha256(body['fileRef'].encode()).hexdigest()): return {'version':1,'state':'unknown'}
                    return {'version':1,'state':'status_only'}
            generation = repository.generation()
            active_generation = [generation]
            root = resolve_resource_ref(body['rootRef'], expected_owner_subject_id=account, current_policy_generation=generation)
            selected = resolve_resource_ref(body['fileRef'], expected_owner_subject_id=account, current_policy_generation=generation)
            if (root.provider != 'host' or root.kind != 'folder' or selected.provider != 'host'
                    or selected.kind not in ('file','document') or root.workspace_id != selected.workspace_id): return unavailable
            root_context = ProviderContext(account, owner, generation, is_admin=admin, workspace_id=root.workspace_id)
            registered_root = await provider.authorize_readonly_root(root_context,origin_id=root.origin_id,expected_revision=None)
            location_id = registered_root['root_id']
            if ((root.location_id is not None and root.location_id != location_id)
                    or (selected.location_id is not None and selected.location_id != location_id)):
                return unavailable
            user, supervisor, binding, mapping = await asyncio.to_thread(_get_observation_binding, request, body['chatID'], None, session_manager)
            if user != owner: return unavailable
            original = get_managed_binding(body['chatID'], owner=owner)
            cwd = original.get('physicalCwd')
            if (original.get('engineSessionID') != binding.engine_session_id or original.get('transition') is not None
                    or original.get('mappingRevision') != binding.binding_revision or not isinstance(cwd,str)):
                return unavailable
            pool = supervisor.owner_lifecycle_pool()
            lease = None
            try:
                lease, runtime, worker, bridge, process, holder, installed, epoch = await pool.admit_git_index_operation(owner=owner)
                utility.supervisor, utility.worker, utility.bridge, utility.process = pool, worker, bridge, process
                utility.holder, utility.generation, utility.epoch, utility.lease = holder, installed, epoch, lease
                async def current():
                    try:
                        pool._assert_owner_lifecycle(owner, epoch)
                        await asyncio.to_thread(_ensure_observation_binding_current, mapping, owner, body['chatID'], binding)
                        return bool(not utility.cancel_requested and utility.account_current() and await barrier.is_live(owner, admission)
                            and repository.generation() == active_generation[0] and pool._states.get(owner) is runtime
                            and runtime.active is worker and runtime.status == 'ready' and runtime.generation == installed
                            and worker.is_alive() and worker._proc is process and worker._bridge is bridge
                            and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) == holder
                            and get_managed_binding(body['chatID'], owner=owner) == original and not lease._released)
                    except Exception: return False
                async def read(op, path=None):
                    payload = {'stableChatID':body['chatID'], 'operation':op, **({'path':path} if path is not None else {})}
                    response = await worker.session_git_read_request(body['chatID'], binding.engine_session_id, cwd, payload, owner=owner)
                    if response.status_code != 200 or len(response.content) > 400000: raise ValueError('Git read unavailable')
                    value = response.json()
                    if (type(value) is not dict or set(value) != {'state','data','indexExists','digest','truncated'}
                            or value['state'] != 'ready' or value['truncated'] is not False
                            or not isinstance(value['digest'],str) or not re.fullmatch(r'[0-9a-f]{64}',value['digest'])):
                        raise ValueError('Git read unavailable')
                    data = base64.b64decode(value['data'],validate=True)
                    if len(data) > 262144 or hashlib.sha256(data).hexdigest() != value['digest']: raise ValueError('Git read unavailable')
                    return data
                def entry(raw, path, tree):
                    if raw == b'': return None
                    rows = raw.split(b'\0')
                    if len(rows) != 2 or rows[1] != b'': raise ValueError('Git entry unavailable')
                    metadata, name = rows[0].split(b'\t',1)
                    if name.decode('utf-8','strict') != path: raise ValueError('Git entry changed')
                    parts = metadata.decode('ascii','strict').split(' ')
                    if len(parts) != 3: raise ValueError('Git entry unavailable')
                    mode, kind, oid = parts if tree else (parts[0], parts[2], parts[1])
                    if mode not in ('100644','100755') or kind != ('blob' if tree else '0') or not re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?',oid):
                        raise ValueError('Git entry unavailable')
                    return {'path':path,'mode':mode,'oid':oid}
                async def native(path):
                    if not await current(): raise ValueError('Git authority changed')
                    response = await worker.session_git_index_preview_request(body['chatID'], binding.engine_session_id, cwd, owner=owner)
                    if response.status_code != 200 or len(response.content) > 16384: raise ValueError('Git preview unavailable')
                    value = response.json()
                    if type(value) is not dict or set(value) != {'version','state','gitIndexControlV1','physicalIndex'} or value['version'] != 1 or value['state'] != 'ready' or value['gitIndexControlV1'] is not True:
                        raise ValueError('Git preview unavailable')
                    roots = (await read('root')).decode('utf-8','strict').splitlines()
                    if len(roots) != 2: raise ValueError('Git root unavailable')
                    head = (await read('head')).decode('ascii','strict').strip()
                    physical = value['physicalIndex']
                    return {'root':os.path.realpath(roots[0]), 'git':os.path.realpath(os.path.join(cwd, roots[1])),
                        'objectFormat':physical['objectFormat'], 'head':head,
                        'indexEntry':entry(await read('indexEntry',path),path,False),
                        'treeEntry':entry(await read('treeEntry',path),path,True), 'physicalIndex':physical}
                witness=await derive_git_index_witness(utility=utility,provider=provider,repository=repository,account=account,
                    root_origin=root.origin_id,file_origin=selected.origin_id,location_id=location_id,workspace_id=root.workspace_id,
                    policy_generation=generation,chat_id=body['chatID'],is_admin=admin,group_ids=(),current=current,native_read=native)
                indexed=witness['snapshot']['git']['indexEntry']
                if indexed is None or bool(witness['snapshot']['mode'] & 0o111)!=(indexed['mode']=='100755'): return unavailable
                desired=await read('blob',indexed['oid']);before=witness['bytes']
                for value in (before,desired):
                    if b'\x00' in value: return unavailable
                    value.decode('utf-8','strict')
                checked=await derive_git_index_witness(utility=utility,provider=provider,repository=repository,account=account,
                    root_origin=root.origin_id,file_origin=selected.origin_id,location_id=location_id,workspace_id=root.workspace_id,
                    policy_generation=generation,chat_id=body['chatID'],is_admin=admin,group_ids=(),current=current,native_read=native)
                if checked!=witness or not await current(): return {'version':1,'state':'stale'}
                if desired==before: return {'version':1,'state':'no_changes'}
                request_id=body['requestID'] if confirm else uuid.uuid4().hex
                material={'version':1,'profile':'reviewed_git_discard_v1','requestID':request_id,'witness':witness['digest'],
                    'indexOID':indexed['oid'],'desiredSHA256':hashlib.sha256(desired).hexdigest()}
                digest=hashlib.sha256(json.dumps(material,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                if not confirm:
                    return {'version':1,'state':'ready','requestID':request_id,'expectedDigest':digest,
                        'review':{'name':os.path.basename(witness['snapshot']['path']),'indexOID':indexed['oid'],
                            'before':before.decode('utf-8'),'after':desired.decode('utf-8'),'beforeSHA256':hashlib.sha256(before).hexdigest(),
                            'afterSHA256':hashlib.sha256(desired).hexdigest(),'beforeCR':b'\r' in before,'afterCR':b'\r' in desired,
                            'beforeBOM':before.startswith(b'\xef\xbb\xbf'),'afterBOM':desired.startswith(b'\xef\xbb\xbf'),
                            'indexObserved':True,'atomicIndex':False}}
                if digest!=body['expectedDigest']: return {'version':1,'state':'stale'}
                pending={'version':1,'kind':'git.discard.v1','state':'pending','claim':None,'chatID':body['chatID'],
                    'rootRefSHA256':hashlib.sha256(body['rootRef'].encode()).hexdigest(),
                    'fileRefSHA256':hashlib.sha256(body['fileRef'].encode()).hexdigest(),
                    'rootID':root.stable_id,'fileID':selected.stable_id,'locationID':location_id,'workspaceID':root.workspace_id,
                    'originID':selected.origin_id,'indexOID':indexed['oid']}
                existing=repository.reserve_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,
                    generation=generation,receipt=pending,phase='git.discard.v1')
                if existing is not None: return {'version':1,'state':'status_only'}
                revision={'kind':'hostObjectIdentity','value':witness['snapshot']['objectTag']}
                class HumanGitTarget:
                    owner_subject_id=account;role='owner';context=root_context;resource={'origin_id':selected.origin_id,'resource_id':selected.stable_id}
                    def __init__(self): self.provider=provider;self.revision=revision
                    async def current(self, *, check_revision=True):
                        try:
                            if not await current(): return False
                            from src.openclank.files_host_provider import _path
                            scope=provider._scope(root_context); path=_path(selected.origin_id)
                            if not provider._path_writable(root_context,path,scope): return False
                            entry=await provider.stat_inspection(root_context,origin_id=selected.origin_id)
                            return entry.origin_id==selected.origin_id and entry.kind=='file' and (not check_revision or dict(entry.revision or {})==revision)
                        except Exception:return False
                from types import SimpleNamespace
                target=HumanGitTarget();reservation=SimpleNamespace(owner=account,operation=request_id,digest=digest)
                async def before_issue():
                    try:
                        latest=await derive_git_index_witness(utility=utility,provider=provider,repository=repository,account=account,
                            root_origin=root.origin_id,file_origin=selected.origin_id,location_id=location_id,workspace_id=root.workspace_id,
                            policy_generation=generation,chat_id=body['chatID'],is_admin=admin,group_ids=(),current=current,native_read=native)
                        return latest==witness and await read('blob',indexed['oid'])==desired and await current()
                    except Exception:return False
                result=await provider.git_discard_commit(target,reservation,expected_before=before,desired=desired,before_issue=before_issue)
                if not await current(): return {'version':1,'state':'unknown'}
                state=result.get('state') if isinstance(result,dict) else None
                return {'version':1,'state':'applied' if state=='complete' else 'refused' if state in ('unavailable','conflict') else 'unknown'}
            finally:
                if lease is not None and not lease._released: await lease.release()
        except Exception:
            return unavailable

    async def _human_index_request(request: Request, *, confirm: bool, commit: bool = False, cleanup: bool = False, branch: bool = False, fetch: bool = False, push: bool = False, discard: bool = False, checkout: bool = False):
        from src.auth_helpers import is_internal_tool_identity
        from src.openclank.account_request_barrier import UtilityLifetime
        unavailable = {'version':1, 'state':'unavailable'}
        try:
            account, owner, admin = files_principal(request)
            auth = request.app.state.auth_manager
            barrier = request.app.state.account_request_barrier
            if (getattr(request.state, 'api_token', False) or getattr(request.state, 'internal_tool_authenticated', False)
                    or is_internal_tool_identity(owner) or owner not in auth.users or auth.account_id(owner) != account
                    or auth.username_for_account_id(account) != owner):
                return unavailable
            credential_factory=credential_current=None
            if checkout:
                credential_factory=getattr(request.app.state,'capture_original_credential',None)
                if not getattr(request.state,'authenticated',False) or not callable(credential_factory): return unavailable
                credential_current=credential_factory(request,owner,account)
                if not callable(credential_current) or credential_current() is not True:return unavailable
            admission = await barrier.acquire(owner)
            utility = UtilityLifetime(barrier, admission, owner, account, 'git-index-preview', uuid.uuid4().hex, '0'*64, auth)
            if checkout:
                utility.checkout_credential_factory=credential_factory;utility.checkout_credential_current=credential_current
            try:
                await barrier.bind_utility(admission, utility)
            except BaseException:
                await barrier.release(admission)
                raise
            async def owned():
                try:
                    if cleanup:
                        from src.openclank.git_cleanup_review import preview_owned
                        return await preview_owned(request, account=account, owner=owner, admin=admin, auth=auth,
                            barrier=barrier, admission=admission, utility=utility, repository=repository,
                            provider=provider, session_manager=session_manager, confirm=confirm, drain=_drain_git_index)
                    if checkout:
                        from routes.git_checkout_workbench import preview_owned
                        return await preview_owned(request,account,owner,admin,auth,barrier,admission,utility,
                            repository=repository,provider=provider,session_manager=session_manager,confirm=confirm)
                    handler = _discard_preview_owned if discard else _push_preview_owned if push else _fetch_preview_owned if fetch else _branch_preview_owned if branch else _commit_preview_owned if commit else _index_preview_owned
                    return await handler(request, account, owner, admin, auth, barrier, admission, utility, confirm=confirm)
                finally:
                    if utility.issued and not getattr(utility,'git_index_drained',False):
                        if checkout:
                            from routes.git_checkout_workbench import drain_owned
                            await drain_owned(utility)
                        else: await _drain_git_index(utility)
                    if utility.lease is not None and not utility.lease._released:
                        await utility.lease.release()
                    utility.settled = True
                    await barrier.release(admission)
            task = asyncio.create_task(owned())
            utility.task = task
            try:
                if not confirm:
                    return await asyncio.shield(task)
                done, _ = await asyncio.wait((task,),timeout=5.0)
                if done:
                    return task.result()
                response = {'version':1,'state':'pending'}
                if getattr(utility,'git_index_selector_ready',False):
                    response.update({'requestID':utility.idempotency_key,'expectedDigest':utility.request_hash})
                    row = repository.get_operation(owner_subject_id=account,operation_id=utility.idempotency_key)
                    if row and row['digest'] == utility.request_hash and row_profile(row) == ('cleanup' if cleanup else 'discard' if discard else 'checkout' if checkout else 'push' if push else 'fetch' if fetch else 'branch_create' if branch else 'commit' if commit else 'index'):
                        response['policyTransition'] = policy_transition(account,utility.idempotency_key,row)
                task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
                return response
            except asyncio.CancelledError:
                utility.cancel_requested = True
                if confirm:
                    task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
                    raise
                while not task.done():
                    try: await asyncio.shield(task)
                    except asyncio.CancelledError: continue
                    except Exception: break
                raise
        except Exception:
            return unavailable

    @router.post('/index-preview')
    async def index_preview(request: Request):
        return await _human_index_request(request, confirm=False)

    @router.post('/index-confirm')
    async def index_confirm(request: Request):
        return await _human_index_request(request, confirm=True)

    @router.post('/commit-preview')
    async def commit_preview(request: Request):
        return await _human_index_request(request, confirm=False, commit=True)

    @router.post('/commit-confirm')
    async def commit_confirm(request: Request):
        return await _human_index_request(request, confirm=True, commit=True)

    async def _human_index_status(request: Request, *, commit: bool, cleanup: bool = False, branch: bool = False, fetch: bool = False, push: bool = False, checkout: bool = False):
        from src.auth_helpers import is_internal_tool_identity
        try:
            account, owner, _ = files_principal(request)
            auth = request.app.state.auth_manager
            barrier = request.app.state.account_request_barrier
            if (getattr(request.state,'api_token',False) or getattr(request.state,'internal_tool_authenticated',False)
                    or is_internal_tool_identity(owner) or owner not in auth.users or auth.account_id(owner) != account
                    or auth.username_for_account_id(account) != owner):
                return {'version':1,'state':'unknown'}
            credential_factory=credential_current=None
            if checkout:
                credential_factory=getattr(request.app.state,'capture_original_credential',None)
                if not getattr(request.state,'authenticated',False) or not callable(credential_factory): return {'version':1,'state':'unknown'}
                credential_current=credential_factory(request,owner,account)
                if not callable(credential_current) or credential_current() is not True:return {'version':1,'state':'unknown'}
            admission = await barrier.acquire(owner)
            try:
                raw = bytearray()
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 4096: return {'version':1,'state':'unknown'}
                    raw.extend(chunk)
                body = json.loads(raw)
                if (type(body) is not dict or set(body) != {'version','requestID','expectedDigest'} or body['version'] != 1
                        or type(body['requestID']) is not str or not re.fullmatch(r'[0-9a-f]{32}',body['requestID'])
                        or type(body['expectedDigest']) is not str or not re.fullmatch(r'[0-9a-f]{64}',body['expectedDigest'])):
                    return {'version':1,'state':'unknown'}
                row = repository.get_operation(owner_subject_id=account,operation_id=body['requestID'])
                if (not row or row['digest'] != body['expectedDigest'] or row_profile(row) != ('cleanup' if cleanup else 'checkout' if checkout else 'push' if push else 'fetch' if fetch else 'branch_create' if branch else 'commit' if commit else 'index') or auth.account_id(owner) != account
                        or auth.username_for_account_id(account) != owner or not await barrier.is_live(owner,admission)
                        or (checkout and (getattr(request.app.state,'capture_original_credential',None) is not credential_factory or credential_current() is not True))):
                    return {'version':1,'state':'unknown'}
                receipt = row['receipt']
                state = receipt.get('state') if receipt.get('settled') is True else 'pending'
                if checkout and state=='pending':
                    supervisor=getattr(request.app.state,'mimo_supervisor',None)
                    pool=supervisor.owner_lifecycle_pool() if supervisor else None
                    worker=pool.worker_for_owner(owner) if pool else None
                    process=getattr(worker,'_proc',None)
                    active=await barrier.utility_for_child(owner,worker,process) if worker is not None and process is not None else []
                    if not any(getattr(item,'root',None)=='git-checkout' and getattr(item,'idempotency_key',None)==body['requestID']
                            and getattr(item,'request_hash',None)==body['expectedDigest'] and not item.settled for item in active):state='unknown'
                if state not in ('pending','applied','refused','rolled_back','unknown'): state = 'unknown'
                return {'version':1,'state':state,'policyTransition':policy_transition(account,body['requestID'],row),
                    **({'effect':receipt['gitCheckoutResult']} if checkout and type(receipt.get('gitCheckoutResult')) is dict else {} if checkout else (__import__('src.openclank.git_cleanup_review',fromlist=['public_effect']).public_effect(receipt)) if cleanup else public_push_effect(receipt) if push else public_fetch_effect(receipt) if fetch else public_branch_effect(receipt) if branch else public_commit_effect(receipt) if commit else public_effect(receipt))}
            finally:
                await barrier.release(admission)
        except Exception:
            return {'version':1,'state':'unknown'}

    @router.post('/checkout-preview')
    async def checkout_preview(request: Request):
        return await _human_index_request(request,confirm=False,checkout=True)

    @router.post('/checkout-confirm')
    async def checkout_confirm(request: Request):
        return await _human_index_request(request,confirm=True,checkout=True)

    @router.post('/checkout-status')
    async def checkout_status(request: Request):
        return await _human_index_status(request,commit=False,checkout=True)

    @router.post('/branch-preview')
    async def branch_preview(request: Request):
        return await _human_index_request(request,confirm=False,branch=True)

    @router.post('/branch-confirm')
    async def branch_confirm(request: Request):
        return await _human_index_request(request,confirm=True,branch=True)

    @router.post('/branch-status')
    async def branch_status(request: Request):
        return await _human_index_status(request,commit=False,branch=True)

    @router.post('/fetch-preview')
    async def fetch_preview(request: Request):
        return await _human_index_request(request,confirm=False,fetch=True)

    @router.post('/fetch-confirm')
    async def fetch_confirm(request: Request):
        return await _human_index_request(request,confirm=True,fetch=True)

    @router.post('/fetch-status')
    async def fetch_status(request: Request):
        return await _human_index_status(request,commit=False,fetch=True)

    @router.post('/push-preview')
    async def push_preview(request: Request):
        return await _human_index_request(request,confirm=False,push=True)

    @router.post('/push-confirm')
    async def push_confirm(request: Request):
        return await _human_index_request(request,confirm=True,push=True)

    @router.post('/push-status')
    async def push_status(request: Request):
        return await _human_index_status(request,commit=False,push=True)

    @router.post('/discard-preview')
    async def discard_preview(request: Request):
        return await _human_index_request(request,confirm=False,discard=True)

    @router.post('/discard-confirm')
    async def discard_confirm(request: Request):
        return await _human_index_request(request,confirm=True,discard=True)

    @router.post('/discard-status')
    async def discard_status(request: Request):
        from src.auth_helpers import is_internal_tool_identity
        try:
            account,owner,admin=files_principal(request,repository=repository);auth=request.app.state.auth_manager;barrier=request.app.state.account_request_barrier
            if (getattr(request.state,'api_token',False) or getattr(request.state,'internal_tool_authenticated',False) or is_internal_tool_identity(owner)
                    or owner not in auth.users or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner): return {'version':1,'state':'unknown'}
            admission=await barrier.acquire(owner)
            try:
                raw=bytearray()
                async for chunk in request.stream():
                    if len(raw)+len(chunk)>32768: return {'version':1,'state':'unknown'}
                    raw.extend(chunk)
                body=json.loads(raw)
                if (type(body) is not dict or set(body)!={'version','requestID','expectedDigest','chatID','rootRef','fileRef'} or body['version']!=1
                        or type(body['requestID']) is not str or not re.fullmatch(r'[0-9a-f]{32}',body['requestID'])
                        or type(body['expectedDigest']) is not str or not re.fullmatch(r'[0-9a-f]{64}',body['expectedDigest'])
                        or any(type(body[k]) is not str or not body[k] or len(body[k])>8192 for k in ('chatID','rootRef','fileRef'))): return {'version':1,'state':'unknown'}
                row=repository.get_operation(owner_subject_id=account,operation_id=body['requestID'])
                if not row or row['digest']!=body['expectedDigest'] or row_profile(row)!='discard': return {'version':1,'state':'unknown'}
                receipt=row['receipt'];generation=repository.generation()
                root=resolve_resource_ref(body['rootRef'],expected_owner_subject_id=account,current_policy_generation=generation)
                selected=resolve_resource_ref(body['fileRef'],expected_owner_subject_id=account,current_policy_generation=generation)
                if (root.provider!='host' or root.kind!='folder' or selected.provider!='host' or selected.kind not in ('file','document')
                        or root.workspace_id!=selected.workspace_id or root.stable_id!=receipt.get('rootID') or selected.stable_id!=receipt.get('fileID')
                        or selected.origin_id!=receipt.get('originID') or body['chatID']!=receipt.get('chatID')): return {'version':1,'state':'unknown'}
                context=ProviderContext(account,owner,generation,is_admin=admin,workspace_id=root.workspace_id)
                registered=await provider.authorize_readonly_root(context,origin_id=root.origin_id,expected_revision=None)
                if registered['root_id']!=receipt.get('locationID'): return {'version':1,'state':'unknown'}
                user,supervisor,binding,mapping=await asyncio.to_thread(_get_observation_binding,request,body['chatID'],None,session_manager)
                from src.openclank.transcript_projection import get_managed_binding
                original=get_managed_binding(body['chatID'],owner=owner)
                if (user!=owner or original.get('engineSessionID')!=binding.engine_session_id or original.get('transition') is not None
                        or original.get('mappingRevision')!=binding.binding_revision or original.get('physicalCwd')!=registered['root']): return {'version':1,'state':'unknown'}
                entry=await provider.stat(context,origin_id=selected.origin_id)
                if entry.origin_id!=selected.origin_id or entry.kind!='file' or not isinstance(entry.revision,dict): return {'version':1,'state':'unknown'}
                class StatusTarget:
                    owner_subject_id=account;role='owner';resource={'origin_id':selected.origin_id,'resource_id':selected.stable_id}
                    def __init__(self):self.provider=provider;self.context=context;self.revision=dict(entry.revision)
                    async def current(self,*,check_revision=True):
                        try:
                            await asyncio.to_thread(_ensure_observation_binding_current,mapping,owner,body['chatID'],binding)
                            if (auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner or not await barrier.is_live(owner,admission)
                                    or repository.generation()!=generation or get_managed_binding(body['chatID'],owner=owner)!=original): return False
                            if await provider.authorize_readonly_root(context,origin_id=root.origin_id,expected_revision=None)!=registered:return False
                            item=await provider.stat(context,origin_id=selected.origin_id)
                            return item.origin_id==selected.origin_id and item.kind=='file' and (not check_revision or dict(item.revision or {})==self.revision)
                        except Exception:return False
                target=StatusTarget()
                if not await target.current(): return {'version':1,'state':'unknown'}
                if receipt.get('state')=='pending' and receipt.get('retired') is True:
                    receipt=await provider.collaboration_status(target,receipt)
                if not await target.current(check_revision=False): return {'version':1,'state':'unknown'}
                state=receipt.get('state');return {'version':1,'state':'applied' if state=='complete' else 'refused' if state in ('conflict','unavailable') else 'pending' if state=='pending' else 'unknown'}
            finally: await barrier.release(admission)
        except Exception:return {'version':1,'state':'unknown'}

    @router.post('/cleanup-preview')
    async def cleanup_preview(request: Request):
        return await _human_index_request(request,confirm=False,cleanup=True)

    @router.post('/cleanup-confirm')
    async def cleanup_confirm(request: Request):
        return await _human_index_request(request,confirm=True,cleanup=True)

    @router.post('/cleanup-status')
    async def cleanup_status(request: Request):
        return await _human_index_status(request,commit=False,cleanup=True)

    @router.post('/index-status')
    async def index_status(request: Request):
        return await _human_index_status(request,commit=False)

    @router.post('/commit-status')
    async def commit_status(request: Request):
        return await _human_index_status(request,commit=True)

    @router.post('/folder-state')
    async def folder_state(request: Request):
        """Is the open Files folder inside a Git repository? A filesystem fact (no Git command, no chat needed)."""
        unavailable = {'state': 'unavailable'}
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {'rootRef'} or not isinstance(body['rootRef'], str):
                return unavailable
            from src.openclank.files_host_provider import _path
            from src.openclank.git_repo_detect import detect_repo
            account, owner, admin = files_principal(request, repository=repository)
            generation = repository.generation()
            ref = resolve_resource_ref(body['rootRef'], expected_owner_subject_id=account, current_policy_generation=generation)
            if ref.provider != 'host' or ref.kind != 'folder': return unavailable
            context = ProviderContext(account, owner, generation, is_admin=admin, workspace_id=ref.workspace_id or 'default')
            entry = await provider.stat(context, origin_id=ref.origin_id)
            folder = _path(ref.origin_id)
            if (entry.origin_id != ref.origin_id or not provider._is_directory_kind(entry.kind)
                    or 'children' not in entry.capabilities or os.path.realpath(folder) != folder): return unavailable
            roots = [os.path.normpath(str(l.canonical_path)) for l in repository.list_locations()
                     if l.enabled and l.availability == 'available' and l.kind == 'directory'
                     and os.path.commonpath((os.path.normpath(str(l.canonical_path)), folder)) == os.path.normpath(str(l.canonical_path))]
            if not roots: return unavailable
            return detect_repo(folder, max(roots, key=len))
        except Exception:
            return unavailable

    @router.post('/read')
    async def read(request: Request):
        unavailable = {'state': 'unavailable'}
        try:
            body = await request.json()
            if (not isinstance(body, dict) or set(body) - {'chatID', 'rootRef', 'operation', 'path', 'expected'}
                    or not isinstance(body.get('chatID'), str) or not isinstance(body.get('rootRef'), str)
                    or body.get('operation') not in {'status', 'worktree', 'staged', 'headDiff', 'branches', 'log', 'worktrees'}):
                return unavailable
            operation = body['operation']; selected = body.get('path')
            if operation in {'branches', 'log', 'worktrees'} and ('path' in body or 'expected' in body): return unavailable
            if operation in {'worktree', 'staged', 'headDiff'} and not allowed_relative_path(selected): return unavailable
            account, owner, admin = files_principal(request, repository=repository)
            generation = repository.generation()
            ref = resolve_resource_ref(body['rootRef'], expected_owner_subject_id=account, current_policy_generation=generation)
            if ref.provider != 'host' or ref.kind != 'folder': return unavailable
            context = ProviderContext(account, owner, generation, is_admin=admin, workspace_id=ref.workspace_id or 'default')
            root = await provider.authorize_readonly_root(context, origin_id=ref.origin_id, expected_revision=None)
            user, supervisor, binding, mapping = await asyncio.to_thread(_get_observation_binding, request, body['chatID'], None, session_manager)
            if user != owner: return unavailable
            from src.openclank.transcript_projection import get_managed_binding
            authority = get_managed_binding(body['chatID'], owner=owner)
            cwd = authority.get('physicalCwd')
            if (authority.get('engineSessionID') != binding.engine_session_id or authority.get('transition') is not None
                    or authority.get('mappingRevision') != binding.binding_revision or cwd != root['root']): return unavailable
            pool = supervisor.owner_lifecycle_pool()
            key = pool._key(owner); state = pool._states.get(key)
            worker = state.active if state else None
            holder = getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None)
            holder_generation = getattr(worker, 'installed_generation', None)
            if (not state or state.status != 'ready' or not worker or not worker.is_alive() or not holder
                    or holder_generation != state.generation): return unavailable
            async def native(op, path=None):
                payload = {'stableChatID':body['chatID'], 'operation':op, **({'path':path} if path is not None else {})}
                response = await pool.session_git_read_request(body['chatID'], binding.engine_session_id, cwd, payload, owner=owner)
                if response.status_code != 200 or len(response.content) > 400000: raise ValueError('unavailable')
                value = response.json()
                if (not isinstance(value, dict) or set(value) != {'state','data','indexExists','digest','truncated'} or value['state'] != 'ready'
                        or not isinstance(value['digest'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['digest']) or type(value['truncated']) is not bool): raise ValueError('unavailable')
                data = base64.b64decode(value['data'], validate=True)
                if len(data) > 262144: raise ValueError('unavailable')
                return data, value['indexExists'], value['digest'], value['truncated']
            async def snapshot():
                roots, _, _, _ = await native('root')
                lines = roots.decode('utf-8','strict').splitlines()
                if len(lines) != 2 or os.path.realpath(lines[0]) != cwd or os.path.realpath(os.path.join(cwd, lines[1])) != os.path.join(cwd,'.git'):
                    raise ValueError('unsupported')
                head, _, _, _ = await native('head')
                index, exists, digest, _ = await native('index')
                if type(exists) is not bool: raise ValueError('unavailable')
                if not exists and index: raise ValueError('unavailable')
                return {'head':head.decode('ascii','strict').strip(), 'index':'sha256:'+digest if exists else 'absent'}
            async def current():
                await asyncio.to_thread(_ensure_observation_binding_current, mapping, owner, body['chatID'], binding)
                if await provider.authorize_readonly_root(context, origin_id=ref.origin_id, expected_revision=None) != root: raise ValueError('stale')
                auth = request.app.state.auth_manager
                if auth.account_id(owner) != account or auth.username_for_account_id(account) != owner: raise ValueError('stale')
                if (pool._states.get(key) is not state or state.active is not worker or state.status != 'ready'
                        or state.generation != holder_generation or not worker.is_alive()
                        or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != holder): raise ValueError('stale')
            async def file_proof(path):
                origin = _origin(os.path.join(cwd, path))
                item = await provider.stat(context, origin_id=origin)
                if item.kind != 'file' or item.origin_id != origin or not item.revision: raise ValueError('unavailable')
                content = await provider.content(context, origin_id=origin)
                if type(content.size) is not int or not 0 <= content.size <= 262144 or content.stream is None: raise ValueError('unsupported')
                data = b''
                async for chunk in content.stream(0, content.size):
                    data += chunk
                    if len(data) > 262144: raise ValueError('unsupported')
                if len(data) != content.size: raise ValueError('stale')
                return {'revision':dict(item.revision), 'sha256':hashlib.sha256(data).hexdigest()}, data
            def records(data, *, tree=False):
                rows = data.split(b'\0')
                if rows[-1] != b'' or len(rows) > 65: raise ValueError('unsupported')
                result = {}; partial = False
                for row in rows[:-1]:
                    meta, name = row.split(b'\t', 1)
                    path = name.decode('utf-8','strict')
                    fields = meta.decode('ascii').split(' ')
                    if len(fields) != 3: raise ValueError('unsupported')
                    mode, kind, oid = fields if tree else (fields[0], fields[2], fields[1])
                    if not re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', oid): raise ValueError('unsupported')
                    if mode not in {'100644','100755'} or kind != ('blob' if tree else '0') or path in result:
                        partial = True; continue
                    result[path] = oid
                return result, partial
            if operation in {'branches', 'log', 'worktrees'}:
                roots, _, _, _ = await native('root')
                root_lines = roots.decode('utf-8','strict').splitlines()
                if (len(root_lines) != 2 or os.path.realpath(root_lines[0]) != cwd
                        or os.path.realpath(os.path.join(cwd,root_lines[1])) != os.path.join(cwd,'.git')):
                    return {'state':'unavailable'}
                raw, _, _, truncated = await native(operation)
                if truncated: return {'state':'unavailable'}
                rows = []; partial = False
                if operation == 'branches':
                    for line in raw.splitlines():
                        parts = line.split(b'\0')
                        if len(parts) != 4 or parts[-1] or not parts[0].startswith(b'refs/heads/') or not re.fullmatch(rb'[0-9a-f]{40}(?:[0-9a-f]{24})?', parts[1]) or parts[2] not in (b'*', b' '): raise ValueError('invalid branch record')
                        rows.append({'name':parts[0][11:].decode('utf-8','strict'), 'oid':parts[1].decode('ascii'), 'current':parts[2] == b'*'})
                elif operation == 'log':
                    fields = raw.split(b'\0')
                    if fields[-1] != b'' or (len(fields)-1) % 4: raise ValueError('invalid log record')
                    for i in range(0,len(fields)-1,4):
                        oid, parents, stamp, subject = fields[i:i+4]
                        if not re.fullmatch(rb'[0-9a-f]{40}(?:[0-9a-f]{24})?', oid) or not re.fullmatch(rb'[0-9]+',stamp): raise ValueError('invalid log record')
                        parent_list = parents.split() if parents else []
                        if any(not re.fullmatch(rb'[0-9a-f]{40}(?:[0-9a-f]{24})?', p) for p in parent_list): raise ValueError('invalid parent')
                        rows.append({'oid':oid.decode('ascii'), 'parents':[p.decode('ascii') for p in parent_list], 'time':int(stamp), 'subject':subject.decode('utf-8','strict')})
                else:
                    records = raw.split(b'\0\0')
                    if records[-1] != b'' or len(records) < 2: raise ValueError('invalid worktree record')
                    for record in records[:-1]:
                        fields = record.split(b'\0')
                        if not fields or not fields[0].startswith(b'worktree '): raise ValueError('invalid worktree record')
                        path = fields[0][9:].decode('utf-8','strict')
                        # Public rows are only directories under this authorized root.
                        if os.path.realpath(path) != path or os.path.commonpath((cwd,path)) != cwd:
                            partial = True; continue
                        item = await provider.stat(context, origin_id=_origin(path))
                        if not provider._is_directory_kind(item.kind) or item.origin_id != _origin(path) or 'children' not in item.capabilities:
                            partial = True; continue
                        head = None; branch = None; detached = locked = prunable = False
                        for field in fields[1:]:
                            if field.startswith(b'HEAD ') and head is None:
                                value=field[5:]
                                if not re.fullmatch(rb'[0-9a-f]{40}(?:[0-9a-f]{24})?',value): raise ValueError('invalid worktree head')
                                head=value.decode('ascii')
                            elif field.startswith(b'branch ') and branch is None:
                                value=field[7:]
                                if not value.startswith(b'refs/heads/'): raise ValueError('invalid worktree branch')
                                branch=value[11:].decode('utf-8','strict')
                            elif field == b'detached' and not detached: detached=True
                            elif field == b'locked' or field.startswith(b'locked '): locked=True
                            elif field == b'prunable' or field.startswith(b'prunable '): prunable=True
                            else: partial=True
                        if head is None or (detached and branch is not None): raise ValueError('invalid worktree identity')
                        relative = os.path.relpath(path,cwd)
                        rows.append({'relativePath':relative, 'resourceID':stable_resource_id(owner_subject_id=account, provider='host', origin_id=item.origin_id),
                            'headOID':head, 'branch':branch, 'detached':detached, 'locked':locked, 'prunable':prunable})
                if len(rows) > 100: partial = True; rows = rows[:100]
                await current()
                return {'version':1, 'state':'ready', 'operation':operation, 'items':rows, 'partial':partial}
            before = await snapshot()
            if operation == 'status':
                index_data, _, _, index_truncated = await native('index'); tree_data, _, _, tree_truncated = await native('tree')
                index, index_partial = records(index_data); tree, tree_partial = records(tree_data, tree=True)
                data, _, _, others_truncated = await native('others'); rows = data.split(b'\0')
                if rows[-1] != b'' or len(rows) > 65: return {'state':'unsupported'}
                result = []; partial = index_truncated or tree_truncated or others_truncated or index_partial or tree_partial
                others = {row.decode('utf-8','strict') for row in rows[:-1]}
                paths = set(index) | set(tree) | others
                if len(paths) > 16: partial = True
                for path in sorted(paths)[:16]:
                    if not allowed_relative_path(path): partial = True; continue
                    try:
                        _, raw = await file_proof(path)
                        indexed = (await native('blob', index[path]))[0] if path in index else None
                    except Exception: partial = True; continue
                    x = '?' if path in others or (index_truncated and path not in index) or (tree_truncated and path not in tree) else ' ' if index.get(path) == tree.get(path) else ('A' if path not in tree else 'D' if path not in index else 'M')
                    y = '?' if path in others else 'M' if indexed != raw else ' '
                    if x != ' ' or y != ' ': result.append({'path':path, 'index':x, 'worktree':y})
                await current()
                if await snapshot() != before: return {'state':'stale'}
                return {'state':'ready', 'snapshot':before, 'items':result, 'partial':partial, 'clean':False, 'comparison':'raw'}
            proof, raw = await file_proof(selected)
            if body.get('expected') != before: return {'state':'stale'}
            index_data, _, _, index_truncated = await native('indexEntry', selected)
            tree_data, _, _, tree_truncated = await native('treeEntry', selected)
            index, index_partial = records(index_data); tree, tree_partial = records(tree_data, tree=True)
            if index_truncated or tree_truncated or index_partial or tree_partial or set(index) - {selected} or set(tree) - {selected}:
                return {'state':'unsupported'}
            left_oid = tree.get(selected) if operation == 'headDiff' else index.get(selected) if operation == 'worktree' else tree.get(selected)
            right_oid = index.get(selected) if operation == 'staged' else None
            left = (await native('blob', left_oid))[0] if left_oid else b''
            right = (await native('blob', right_oid))[0] if right_oid else raw if operation != 'staged' else b''
            await current()
            if await snapshot() != before or (await file_proof(selected))[0] != proof: return {'state':'stale'}
            if b'\x00' in left or b'\x00' in right: return {'state':'unsupported'}
            if len(left) + len(right) > 65536: return {'state':'unsupported'}
            left_lines = left.decode('utf-8','strict').splitlines(keepends=True)
            right_lines = right.decode('utf-8','strict').splitlines(keepends=True)
            if len(left_lines) + len(right_lines) > 1000: return {'state':'unsupported'}
            diff = await asyncio.to_thread(lambda: ''.join(difflib.unified_diff(left_lines, right_lines, fromfile='before', tofile='after')))
            if len(diff.encode('utf-8')) > 262144: return {'state':'unsupported'}
            await current()
            if await snapshot() != before or (await file_proof(selected))[0] != proof: return {'state':'stale'}
            return {'state':'ready', 'snapshot':before, 'path':selected, 'diff':diff, 'comparison':'raw'}
        except Exception:
            return unavailable
    from routes.git_worktree_workbench import install_worktree_routes
    install_worktree_routes(router,repository=repository,provider=provider,session_manager=session_manager)
    return router

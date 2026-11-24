"""One admitted managed Files mutation, retaining the original approval owner."""
from __future__ import annotations
import asyncio
import hashlib
import json
from core.models import get_session_manager_instance
from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result

METHOD = '_openclank/session/v1/files/mutate'


def _digest(args):
    if args.get('action') in ('write', 'edit'):
        action = args['action']
        expected_keys = ({'action', 'path', 'content', 'expectedFingerprint'} if action == 'write' else
                         {'action', 'path', 'oldText', 'newText', 'replaceAll', 'expectedFingerprint'})
        if set(args) != expected_keys:
            raise ValueError('ordinary mutation unavailable')
        path = args['path']; expected = args['expectedFingerprint']
        first = args['content'] if action == 'write' else args['oldText']
        second = None if action == 'write' else args['newText']
        replace_all = None if action == 'write' else args['replaceAll']
        if (not isinstance(path, str) or not path or path != path.strip() or '\0' in path
                or len(path.encode('utf-8', 'strict')) > 4096 or not isinstance(first, str)
                or (expected is not None and (not isinstance(expected, str) or not expected or '\0' in expected or len(expected) > 512))
                or (action == 'edit' and (not isinstance(second, str) or type(replace_all) is not bool
                                          or not first or first == second))):
            raise ValueError('ordinary mutation unavailable')
        body = json.dumps(['openclank.file-mutation.v2', action, path, first, second, replace_all, expected],
                          ensure_ascii=False, separators=(',', ':')).encode('utf-8', 'strict')
        if len(body) > 131072:
            raise ValueError('ordinary mutation unavailable')
        return hashlib.sha256(body).hexdigest()
    path, dest, fingerprint = args['path'], args['destination'], args['expectedFingerprint']
    for value in (path, dest):
        if value is not None and (not value or '\0' in value or value != value.strip() or len(value.encode('utf-8','strict')) > 4096):
            raise ValueError('path unavailable')
    if fingerprint is not None and ('\0' in fingerprint or not fingerprint or len(fingerprint) > 512):
        raise ValueError('fingerprint unavailable')
    return hashlib.sha256(json.dumps(['openclank.file-mutation.v1', args['action'], path, dest, fingerprint],
                                     ensure_ascii=False, separators=(',', ':')).encode('utf-8','strict')).hexdigest()


async def dispatch(bridge, params):
    from src.agent_tools.filesystem_tools import FileApprovalAuthority, ManageFilesTool, current_file_approval
    from src.openclank.acp_bridge import PermissionRequest
    from src.openclank.tui_interactions import ManagedFileApproval
    from src.tool_execution import _active_workspace
    request = validate_managed_method_request(METHOD, params)
    unknown = {'state':'unknown'}
    refused = {'state':'refused'}
    identity = {k:request[k] for k in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
    key = bridge._managed_turn_key(identity)
    try:
        args = request['args']; digest = _digest(args)
        if digest != request['argsSHA256']: return refused
        if args.get('action') in ('write','edit'):
            from src.openclank.ordinary_managed_dispatch import dispatch as ordinary_dispatch
            answer = await ordinary_dispatch(bridge,request,digest)
            try: return validate_managed_method_result(METHOD,answer)
            except Exception: return unknown
        if not request['toolName'].endswith('_manage_files'): return refused
        async with bridge._managed_execution_lock:
            entry = bridge._managed_actor_turns.get(key)
            if not isinstance(entry,dict) or entry.get('state') != 'active' or entry.get('backgroundStartClosing'): return refused
            owner = entry.get('owner'); account = entry.get('ownerAccountID') or entry.get('foregroundAccountID')
            policy = entry.get('policyRevision')
            admission = entry.get('backgroundStartAdmitted',{}).get(request['toolCallID'])
            if not isinstance(admission,dict) or admission.get('state') != 'admitted': return unknown
            admission['state'] = 'files_reserved'
        if not isinstance(owner,str) or not owner or not isinstance(account,str) or not account: return refused
        allowed = await bridge._handle_managed_tool_admit({**identity,'toolName':'manage_files','kind':'tool','toolCallID':request['toolCallID']})
        if allowed.get('allowed') is not True: return refused
        resolved = await bridge.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
        root = resolved.get('rootSessionID')
        if not isinstance(root,str) or not root: return refused
        binding = await bridge._handle_session_binding_read({'sessionID':root})
        manager = get_session_manager_instance(); state = getattr(manager,'lifecycle_context',None) if manager else None
        auth = getattr(state,'auth_manager',None); barrier = getattr(state,'account_request_barrier',None)
        supervisor = getattr(state,'mimo_supervisor',None); pool = supervisor.owner_lifecycle_pool() if supervisor else None
        worker = pool.worker_for_owner(owner) if pool else None
        holder = bridge._managed_provider_generation; generation = getattr(worker,'installed_generation',None)
        process = getattr(worker,'_proc',None); chat = binding.get('stableChatID'); workspace_id = binding.get('authorityWorkspaceID')
        workspace = worker._bridge.mapped_session_workspace(chat) if worker and chat else None
        session = manager.get_session(chat) if manager and chat else None
        if (not auth or not barrier or not worker or not holder or type(generation) is not int or process is None
                or not workspace or not session or session.owner != owner or getattr(session,'incognito',False)
                or getattr(session,'archived',False) or binding.get('owner') != owner
                or chat != entry.get('stableChatID') or workspace_id != entry.get('authorityWorkspaceID')): return refused
        frozen_binding = dict(binding)
        source = {name:request[name] for name in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')}
        source['stableChatID'] = chat
        if identity['sessionID'] != root: source['sourceSessionID'] = identity['sessionID']
        async def current():
            try:
                if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                    or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker
                    or worker._bridge is not bridge or worker._proc is not process or not worker.is_alive()
                    or bridge._managed_provider_generation != holder or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) != holder
                    or worker.installed_generation != generation or manager.get_session(chat) is not session or session.owner != owner
                    or getattr(session,'workspace_id',None) != workspace_id or getattr(session,'incognito',False) or getattr(session,'archived',False)
                    or await bridge._handle_session_binding_read({'sessionID':root}) != frozen_binding): return False
                async with bridge._managed_execution_lock:
                    if (bridge._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                            or entry.get('backgroundStartClosing') or entry.get('policyRevision') != policy
                            or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission): return False
                response = await worker.session_background_start_source_request(chat,root,workspace,source,owner=owner,_mode='file-mutation')
                if response.status_code != 200 or response.json() != {'state':'current','argsSHA256':digest}: return False
                if await bridge._handle_session_binding_read({'sessionID':root}) != frozen_binding: return False
                async with bridge._managed_execution_lock:
                    return (bridge._managed_actor_turns.get(key) is entry and entry.get('state') == 'active'
                            and not entry.get('backgroundStartClosing') and entry.get('policyRevision') == policy
                            and entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is admission
                            and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                            and pool.worker_for_owner(owner) is worker and worker._bridge is bridge and worker._proc is process
                            and worker.is_alive() and bridge._managed_provider_generation == holder
                            and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) == holder
                            and worker.installed_generation == generation and manager.get_session(chat) is session
                            and session.owner == owner and getattr(session,'workspace_id',None) == workspace_id
                            and not getattr(session,'incognito',False) and not getattr(session,'archived',False))
            except Exception:
                return False
        issued = [False]
        async def issue():
            async with barrier.admitted(owner):
                if not await current(): return refused
                authority = FileApprovalAuthority(owner,account,chat,workspace_id,tuple(source.items()),current,issued)
                async def progress(payload):
                    data = payload.get('data') if type(payload) is dict and payload.get('type') == 'permission_request' else None
                    request_id = data.get('request_id') if type(data) is dict else None
                    if (not isinstance(request_id,str) or not request_id.startswith('file_perm_') or data.get('session_id') != chat
                            or not isinstance(data.get('detail'),dict) or not await current()): raise ValueError('approval unavailable')
                    pending = current_file_approval(request_id,owner=owner,session_id=chat,account_id=account)
                    if pending is None or pending.authority is not authority: raise ValueError('approval unavailable')
                    context = bridge._session_context.get(root) or {}
                    if context.get('interaction_policy') == 'fail_on_interaction': raise ValueError('interaction unavailable')
                    from src.openclank.transcript_projection import get_projection
                    projection = await asyncio.to_thread(get_projection,chat,owner=owner)
                    if (not projection or projection.get('mimo_session_id') != root
                            or projection.get('active_turn_id') != identity['rootOperationID'] or not await current()): raise ValueError('projection changed')
                    req = PermissionRequest(request_id=request_id,tool_call={},raw_input=data['detail'],options=data['options'],
                        title=str(data.get('permission_type') or ''),session_id=root,odysseus_session_id=chat,owner=owner,
                        workspace=workspace,authority_workspace_id=workspace_id,turn_id=identity['rootOperationID'],revision=int(projection.get('transcript_revision') or 0))
                    evidence = ManagedFileApproval(owner,account,chat,req,pending,(('sessionID',identity['sessionID']),)+tuple(source.items()),current,policy,holder,generation)
                    async with bridge._managed_execution_lock:
                        if (bridge._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                                or entry.get('backgroundStartClosing') or entry.get('policyRevision') != policy
                                or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission): raise ValueError('turn closed')
                        admission['fileApproval'] = (request_id,owner,chat,account,pending)
                        admission['fileApprovalEvidence'] = evidence
                    await bridge._surface_permission(req)
                    if not await current(): raise ValueError('approval authority changed')
                action = 'move' if args['action'] == 'move' else 'delete'
                tool_args = {'action':action,'path':args['path']}
                if action == 'move': tool_args['destination'] = args['destination']
                if args['expectedFingerprint'] is not None: tool_args['expected_fingerprint'] = args['expectedFingerprint']
                ctx = {'owner':owner,'session_id':chat,'workspace':workspace,'authority_workspace_id':workspace_id,
                       'file_approval_authority':authority,'progress_cb':progress}
                token = _active_workspace.set(workspace)
                try:
                    result = await ManageFilesTool().execute(json.dumps(tool_args,ensure_ascii=False),ctx)
                except Exception:
                    return unknown if authority.issued[0] else refused
                finally:
                    _active_workspace.reset(token)
                    async with bridge._managed_execution_lock:
                        admission.pop('fileApproval',None); admission.pop('fileApprovalEvidence',None)
                if not await current() or type(result) is not dict or result.get('exit_code') != 0:
                    return unknown if authority.issued[0] else refused
                if action == 'move':
                    response = {'state':'applied','action':'move','source':result.get('source'),'destination':result.get('path')}
                else:
                    response = {'state':'applied','action':'trash','entry':{'id':result.get('trash_id'),'rootID':result.get('root_id'),
                        'originalPath':result.get('original_path'),'trashedPath':result.get('trashed_path'),'fingerprint':result.get('fingerprint')}}
                try: return validate_managed_method_result(METHOD,response)
                except Exception: return unknown
        async with bridge._managed_execution_lock:
            if bridge._managed_actor_turns.get(key) is not entry or entry.get('backgroundStartClosing'):
                return refused
            task = asyncio.create_task(issue())
            admission['task'] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Owned task is drained; never replay an unproved mutation.
            if not issued[0]:
                task.cancel()
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
                except Exception: break
            raise
    except asyncio.CancelledError:
        raise
    except Exception:
        return unknown

"""Closed phased Host owner for one admitted ordinary managed Files call.

The phase selector and identifiers locate an existing admission. They never
supply path or History authority. Each phase holds a fresh existing chat guard.
"""
from __future__ import annotations

import asyncio
import mimetypes
import re
from types import SimpleNamespace

from core.models import get_session_manager_instance
from src.openclank.files_facade import ProviderContext
from src.openclank.files_service_client import _checkpoint_owned_thread
from src.openclank.ordinary_agent_client import OrdinaryWriterClient
from src.openclank.ordinary_create import managed_create_binding, prepare_managed_create
from src.openclank.ordinary_managed_files import qualified_locator, read_existing
from src.openclank.ordinary_managed_replace import original_binding, prepare
from src.openclank.workspace_policy_service import resolve_owned_workspace
from src.tool_security import is_public_blocked_tool, is_scoped_file_tool_allowed


async def dispatch(bridge, request, digest):
    unknown = {'state':'unknown'}; refused = {'state':'refused'}
    args = request['args']; phase = request['phase']; action = args['action']
    canonical = 'write_file' if action == 'write' else 'edit_file'
    if not re.fullmatch(r'lifetools_[a-f0-9]+_' + canonical, request['toolName']):
        return refused
    identity = {k:request[k] for k in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
    key = bridge._managed_turn_key(identity)
    async with bridge._managed_execution_lock:
        entry = bridge._managed_actor_turns.get(key)
        if not isinstance(entry,dict) or entry.get('state') != 'active' or entry.get('backgroundStartClosing'):
            return refused
        admission = entry.get('backgroundStartAdmitted',{}).get(request['toolCallID'])
        if not isinstance(admission,dict): return refused
        frozen = (request['messageID'],request['toolCallID'],request['toolName'],digest)
        snapshot = {field:entry.get(field) for field in ('owner','ownerAccountID','stableChatID','authorityWorkspaceID',
            'mappingRevision','workspaceRevision','mapRevision','policyRevision','holderID')}
        if phase == 'prepare':
            if admission.get('state') != 'admitted': return unknown
            admission['state'] = 'files_reserved'
            admission['ordinaryOriginal'] = frozen
            admission['ordinaryEntrySnapshot'] = snapshot
        elif (admission.get('state') != 'files_reserved' or admission.get('ordinaryOriginal') != frozen
                or admission.get('ordinaryEntrySnapshot') != snapshot):
            return unknown
        previous = admission.get('task')
        if isinstance(previous,asyncio.Task) and not previous.done(): return unknown
        owner = entry.get('owner'); account = entry.get('ownerAccountID') or entry.get('foregroundAccountID')
        policy = entry.get('policyRevision')
    if not isinstance(owner,str) or not owner or not isinstance(account,str) or not account:
        return refused
    allowed = await bridge._handle_managed_tool_admit({**identity,'toolName':canonical,'kind':'tool','toolCallID':request['toolCallID']})
    if allowed.get('allowed') is not True: return refused
    resolved = await bridge.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
    root_session = resolved.get('rootSessionID')
    if not isinstance(root_session,str) or not root_session: return refused
    binding = await bridge._handle_session_binding_read({'sessionID':root_session})
    manager = get_session_manager_instance(); state = getattr(manager,'lifecycle_context',None) if manager else None
    auth = getattr(state,'auth_manager',None); barrier = getattr(state,'account_request_barrier',None)
    supervisor = getattr(state,'mimo_supervisor',None); repository = getattr(state,'files_policy_repository',None)
    factory = getattr(state,'files_facade_factory',None); history = getattr(state,'history_supervisor',None)
    pool = supervisor.owner_lifecycle_pool() if supervisor else None; worker = pool.worker_for_owner(owner) if pool else None
    chat = binding.get('stableChatID'); workspace_id = binding.get('authorityWorkspaceID'); workspace = binding.get('canonicalCwd')
    session = manager.get_session(chat) if manager and chat else None
    execution = bridge._managed_actor_executions.get(key[:4]); owner_admission = execution.get('ownerAdmission') if isinstance(execution,dict) else None
    holder = bridge._managed_provider_generation; process = getattr(worker,'_proc',None); generation = getattr(worker,'installed_generation',None)
    if (not auth or not barrier or not repository or not callable(factory) or not history or not worker or not session
            or not holder or process is None or type(generation) is not int or not owner_admission
            or execution.get('ownerAdmissionBarrier') is not barrier or execution.get('ownerAdmissionAccount') != account
            or not isinstance(workspace,str) or not isinstance(workspace_id,str) or not workspace_id
            or session.owner != owner or getattr(session,'incognito',False) or getattr(session,'archived',False)
            or binding.get('owner') != owner or chat != entry.get('stableChatID') or workspace_id != entry.get('authorityWorkspaceID')):
        return refused
    files_binding = resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,
                                             purpose='agent_workspace',observation_only=True)
    if files_binding.path != workspace: return refused
    files_generation = repository.generation(); policy_context = ProviderContext(account,owner,files_generation,False,workspace_id)
    facade = factory(SimpleNamespace(app=SimpleNamespace(state=state)),policy_context); provider = facade._providers.get('host')
    if provider is None: return refused
    pool_key = pool._key(owner); epoch = pool._owner_lifecycle_epoch(pool_key); root_state = bridge._turns.get(root_session)
    if root_state is None or root_state.root_operation_id != identity['rootOperationID']: return refused
    source = {name:request[name] for name in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')}
    source['stableChatID'] = chat
    if identity['sessionID'] != root_session: source['sourceSessionID'] = identity['sessionID']
    original_binding_value = dict(binding)

    async def run_phase():
        async with barrier.admitted(owner):
            async with bridge._durable_session_map.async_chat_admission_lock(chat,blocking=False,existing_only=True) as guard:
                async with bridge._managed_execution_lock:
                    if (bridge._managed_actor_turns.get(key) is not entry or entry.get('backgroundStartClosing')
                            or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission): return refused
                    slot = admission.setdefault('ordinaryGuard',{})
                    if slot.get('guard') is not None: return unknown
                    slot['guard'] = guard
                def current_sync():
                    try:
                        active_guard = slot.get('guard')
                        if active_guard is None: return False
                        bridge._durable_session_map.validate_chat_guard(chat,active_guard); pool._assert_owner_lifecycle(pool_key,epoch)
                        return bool(auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                            and not getattr(bridge,'_ownerAdmissionHolderStopped',False) and not getattr(owner_admission,'released',True)
                            and bridge._managed_actor_executions.get(key[:4]) is execution and not execution.get('ownerAdmissionStopped')
                            and execution.get('ownerAdmission') is owner_admission and execution.get('ownerAdmissionBarrier') is barrier
                            and execution.get('ownerAdmissionAccount')==account
                            and bridge._managed_actor_turns.get(key) is entry and entry.get('state')=='active' and not entry.get('backgroundStartClosing')
                            and not entry.get('ownerAdmissionStopped') and entry.get('ownerAdmission') is owner_admission
                            and entry.get('ownerAdmissionBarrier') is barrier and entry.get('ownerAdmissionAccount')==account
                            and all(entry.get(field)==value for field,value in snapshot.items())
                            and entry.get('policyRevision')==policy and entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is admission
                            and admission.get('ordinaryOriginal')==frozen and pool.worker_for_owner(owner) is worker
                            and worker._bridge is bridge and worker._proc is process and worker.is_alive() and worker.installed_generation==generation
                            and bridge._managed_provider_generation==holder and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder
                            and repository.generation()==files_generation and manager.get_session(chat) is session and session.owner==owner
                            and getattr(session,'workspace_id',None)==workspace_id and not getattr(session,'incognito',False)
                            and not getattr(session,'archived',False) and bridge._turns.get(root_session) is root_state
                            and root_state.root_operation_id==identity['rootOperationID'] and not getattr(root_state,'provider_root_closing',False)
                            and (not is_public_blocked_tool(canonical) or bool(auth.is_admin(owner)) or is_scoped_file_tool_allowed(canonical,owner)))
                    except Exception:
                        return False
                async def current():
                    try:
                        if not current_sync() or not await barrier.is_live(owner,owner_admission): return False
                        if await bridge._handle_session_binding_read({'sessionID':root_session}) != original_binding_value: return False
                        if resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,
                                purpose='agent_workspace',observation_only=True) != files_binding: return False
                        response = await worker.session_background_start_source_request(chat,root_session,workspace,source,owner=owner,_mode='file-mutation')
                        return response.status_code==200 and response.json()=={'state':'current','argsSHA256':digest} and current_sync()
                    except Exception:
                        return False
                unpublished = None
                try:
                    if not await current(): return refused
                    record = admission.get('ordinaryRecord')
                    if phase == 'prepare':
                        if record is not None: return unknown
                        observed = await read_existing(provider=provider,context=policy_context,repository=repository,workspace=workspace,
                            chat=chat,requested_path=args['path'],action=action,args=args,current=current)
                        origin={'version':1,'host_chat_id':chat,'engine_session_id':identity['sessionID'],
                            'root_operation_id':identity['rootOperationID'],'actor_id':identity['actorID'],
                            'execution_revision':identity['executionRevision'],'inner_turn_id':identity['innerTurnID'],
                            'tool_call_id':request['toolCallID'],'assistant_message_id':request['messageID']}
                        if observed['kind'] == 'create':
                            recovery,native_binding,receipt = managed_create_binding(account=account,workspace=workspace_id,session=chat,
                                origin=origin,invocation_digest=digest,path=observed['path'],parent_revision=observed['parent_revision'],content=observed['content'])
                            operation,request_digest = recovery['operation_id'],recovery['request_digest']
                        else:
                            recovery = None
                            operation,request_digest,native_binding,receipt = original_binding(account=account,workspace=workspace_id,
                                origin=origin,invocation_digest=digest,path=observed['path'],expected=observed['expected'],content=observed['content'])
                        client = OrdinaryWriterClient(owner,workspace,workspace_id=workspace_id,chat_id=chat,
                            history_root_id=observed['root']['root_id'],history_root_path=observed['root']['root'],account_id=account,
                            origin=origin,canonical_tool=canonical,operation_id=operation,native_digest=native_binding['request_digest'],
                            invocation_digest=digest,target=observed['path'],current_sync=current_sync)
                        if recovery is not None:
                            owned = await prepare_managed_create(client=client,repository=repository,account=account,workspace=workspace_id,
                                session=chat,generation=files_generation,path=observed['path'],content=observed['content'],current=current,
                                recovery=recovery,binding=native_binding,receipt=receipt)
                        else:
                            owned = await prepare(client=client,repository=repository,account=account,workspace=workspace_id,
                                generation=files_generation,path=observed['path'],content=observed['content'],current=current,
                                operation=operation,request_digest=request_digest,binding=native_binding,receipt=receipt)
                        if owned is None: return unknown
                        unpublished = owned
                        capture = dict(owned.prepared.capture_identity)
                        if capture.get('account_id') != account or await current() is not True: return refused
                        record={'owner':owned,'observed':observed,'origin':origin,'operation':operation,'requestDigest':request_digest,
                                'nativeBinding':dict(native_binding),'invocationDigest':digest,'capture':capture,'slot':slot,
                                'filesGeneration':files_generation,'isAdmin':policy_context.is_admin}
                        async with bridge._managed_execution_lock:
                            if (bridge._managed_actor_turns.get(key) is not entry or entry.get('backgroundStartClosing')
                                    or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission): return refused
                            admission['ordinaryRecord']=record; admission['ordinaryPrepared']=owned
                            unpublished = None
                        return {'state':'prepared','operationID':operation,'actionID':native_binding['action_id'],
                            'source':{'accountID':account,'authorityWorkspaceID':workspace_id,'hostChatID':chat,
                                'sessionID':identity['sessionID'],'actorID':identity['actorID'],'executionRevision':identity['executionRevision'],
                                'rootOperationID':identity['rootOperationID'],'innerTurnID':identity['innerTurnID'],
                                'toolCallID':request['toolCallID'],'assistantMessageID':request['messageID']},
                            'capture':{'historyActorID':capture['actor_id'],'historyWorkspaceID':capture['workspace_id']}}
                    if (not isinstance(record,dict) or record.get('slot') is not slot or record.get('invocationDigest') != digest
                            or record.get('filesGeneration') != files_generation or record.get('isAdmin') != policy_context.is_admin):
                        return unknown
                    owned = record['owner']
                    if phase == 'abort':
                        return refused if await _checkpoint_owned_thread(owned.abort_unissued) else unknown
                    if phase == 'apply':
                        result = await owned.apply()
                        if result.get('state') != 'complete': return unknown
                    elif phase == 'status':
                        row = repository.get_operation(owner_subject_id=account,operation_id=record['operation'])
                        receipt = row.get('receipt') if isinstance(row,dict) else None
                        if not isinstance(row,dict) or row.get('digest') != record['requestDigest'] or not isinstance(receipt,dict): return unknown
                        if record['observed']['kind'] == 'create':
                            if (receipt.get('kind') != 'ordinary_create' or receipt.get('binding') != record['nativeBinding']
                                    or receipt.get('capture_identity') != record['capture'] or receipt.get('managed_origin') != record['origin']
                                    or receipt.get('invocation_digest') != digest or receipt.get('settled') is not True): return unknown
                            status_client = OrdinaryWriterClient(owner,workspace,workspace_id=workspace_id,chat_id=chat,
                                history_root_id=record['observed']['root']['root_id'],history_root_path=record['observed']['root']['root'],
                                account_id=account,origin=record['origin'],canonical_tool=canonical,operation_id=record['operation'],
                                native_digest=record['nativeBinding']['request_digest'],invocation_digest=digest,
                                target=record['observed']['path'],current_sync=current_sync)
                            try:
                                response = await status_client.ordinary_create_status_exact(record['observed']['path'],
                                    binding=record['nativeBinding'],capture_identity=record['capture'])
                                data=response.get('data') if isinstance(response,dict) else None
                                expected_after=f"sha256:{record['nativeBinding']['desired_sha256']}:{record['nativeBinding']['desired_size']}"
                                if data != {'state':'committed','fingerprint':expected_after} or not await current(): return unknown
                            finally:
                                status_client.close()
                        elif (receipt.get('kind') != 'ordinary.model.v1' or receipt.get('native_binding') != record['nativeBinding']
                                or receipt.get('capture_identity') != record['capture']): return unknown
                        elif receipt.get('state') != 'complete':
                            claim = receipt.get('claim')
                            if receipt.get('state') != 'pending' or receipt.get('retired') is not True or not isinstance(claim,dict): return unknown
                            status_client = OrdinaryWriterClient(owner,workspace,workspace_id=workspace_id,chat_id=chat,
                                history_root_id=record['observed']['root']['root_id'],history_root_path=record['observed']['root']['root'],
                                account_id=account,origin=record['origin'],canonical_tool=canonical,operation_id=record['operation'],
                                native_digest=record['nativeBinding']['request_digest'],invocation_digest=digest,
                                target=record['observed']['path'],current_sync=current_sync)
                            try:
                                response = await status_client.ordinary_replace_status_exact(record['observed']['path'],
                                    binding=record['nativeBinding'],capture_identity=record['capture'])
                                if not await current(): return unknown
                                settled = repository.settle_collaboration_text(owner_subject_id=account,operation_id=record['operation'],
                                    request_digest=record['requestDigest'],claim=claim,
                                    native_status=response.get('data') if isinstance(response,dict) else None)
                                if settled.get('state') != 'complete': return unknown
                            finally:
                                status_client.close()
                    else:
                        return refused
                    native_binding=record['nativeBinding']; fingerprint=f"sha256:{native_binding['desired_sha256']}:{native_binding['desired_size']}"
                    locator=await qualified_locator(provider=provider,context=policy_context,workspace=workspace,chat=chat,
                        root=record['observed']['root'],path=record['observed']['path'],action_id=native_binding['action_id'],
                        origin=record['origin'],expected=record['observed']['expected'],fingerprint=fingerprint,
                        operation=record['observed']['kind'],tool=canonical,history_supervisor=history,current=current)
                    if not await current(): return unknown
                    observed = record['observed']
                    media = mimetypes.guess_type(observed['path'])[0] or 'text/plain'
                    if not isinstance(media,str) or not media or len(media)>256: return unknown
                    return {'state':'applied','action':action,'actionRef':locator,'fingerprint':fingerprint,
                        'file':{'path':observed['path'],'mediaType':media,'bytes':native_binding['desired_size'],
                                'lines':observed['lines'],'newline':observed['newline']}}
                finally:
                    # Revoke the synchronous witness before any cancellation point.
                    if slot.get('guard') is guard: slot['guard']=None
                    if unpublished is not None:
                        await _checkpoint_owned_thread(unpublished.abort_unissued)
    async with bridge._managed_execution_lock:
        if bridge._managed_actor_turns.get(key) is not entry or entry.get('backgroundStartClosing'): return refused
        previous = admission.get('task')
        if isinstance(previous,asyncio.Task) and not previous.done(): return unknown
        task = asyncio.create_task(run_phase()); admission['task']=task
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
            except Exception: break
        raise
    except Exception:
        return unknown

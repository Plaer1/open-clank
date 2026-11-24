"""Managed workflow writes: live writer admission to existing Files/History owners."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from types import SimpleNamespace

_FIELDS=('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')

def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()

async def dispatch(bridge, request: dict, *, status: bool = False, read: bool = False) -> dict:
    """Admit one actual Native writer invocation; selectors are never authority."""
    operation_id=request.get('operationID','') if isinstance(request,dict) else ''
    unavailable={'state':'unavailable'} if read else {'state':'unavailable','operationID':operation_id}
    possible_issue=False
    try:
        from core.models import get_session_manager_instance
        from src.openclank.files_facade import ProviderContext
        from src.openclank.files_host_provider import _origin
        from src.openclank.workspace_policy_service import resolve_owned_workspace
        from src.openclank.workflow_artifact_set import apply
        from src.openclank.workflow_file_access import lookup_workflow_history_identity, authorize_workflow_agent_root

        identity={field:request[field] for field in _FIELDS}
        if ((not read and (not isinstance(operation_id,str) or not re.fullmatch(r'workflowop_[a-f0-9]{64}',operation_id)))
                or (read and status) or type(identity['executionRevision']) is not int or not 0<=identity['executionRevision']<=9007199254740991): return unavailable
        role,name=request['role'],request['name']
        if role not in {'metaplan','ledger','concern','checkpoint','progress','slice'}: return unavailable
        if role in {'concern','slice'}:
            if not isinstance(name,str) or len(name)>60 or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name): return unavailable
        elif name is not None: return unavailable
        text=None if status or read else request['text']; expected=None if status or read else request['expectedRevision']
        if not status and not read and (not isinstance(text,str) or '\x00' in text or len(text.encode('utf-8','strict'))>262144): return unavailable
        if expected is not None and (type(expected) is not dict or set(expected)!={'kind','value'} or expected.get('kind')!='hostFingerprint'
                or not isinstance(expected.get('value'),str) or len(expected['value'])>100
                or not re.fullmatch(r'sha256:[a-f0-9]{64}:(?:0|[1-9][0-9]*)',expected['value'])): return unavailable
        args_hash=_digest(['openclank.workflow-artifact.v1','read' if read else 'status' if status else 'write',role,name,expected,operation_id if status else text])
        if args_hash != request.get('argsSHA256'): return unavailable
        key=bridge._managed_turn_key(identity)
        async with bridge._managed_execution_lock:
            entry=bridge._managed_actor_turns.get(key)
            if not isinstance(entry,dict) or entry.get('state')!='active': return unavailable
            owner,account,chat=(entry.get('owner'),entry.get('ownerAccountID'),entry.get('stableChatID'))
            snapshot={field:entry.get(field) for field in ('owner','ownerAccountID','stableChatID','authorityWorkspaceID','mappingRevision','workspaceRevision','mapRevision','policyRevision','holderID')}
            policy_inputs=copy.deepcopy(entry.get('policyInputs'))
        # Tool admission is name-based. Enforce operation-specific chat mode
        # here so read/status remain available without granting chat writes.
        if not status and not read and (not isinstance(policy_inputs,list) or not policy_inputs or
                any(not isinstance(item,dict) or not isinstance(item.get('turnPolicy'),dict) or
                    item['turnPolicy'].get('mode') not in ('agent','plan') for item in policy_inputs)):
            return unavailable
        if not all(isinstance(v,str) and v for v in (owner,account,chat)): return unavailable
        if not status and not read and operation_id != 'workflowop_'+_digest(['openclank.workflow.operation.v1',account,identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'],identity['innerTurnID'],request['toolCallID']]): return unavailable
        execution=bridge._managed_actor_executions.get(key[:4]); admission=execution.get('ownerAdmission') if isinstance(execution,dict) else None
        owner_barrier=execution.get('ownerAdmissionBarrier') if isinstance(execution,dict) else None
        if not execution or admission is None or execution.get('ownerAdmissionStopped') or getattr(admission,'released',True): return unavailable
        decision=await bridge._handle_managed_tool_admit({**identity,'toolName':'workflow_artifact','kind':'tool','toolCallID':request['toolCallID']})
        if decision.get('allowed') is not True: return unavailable
        privacy=copy.deepcopy(entry.get('receiptPrivacy'))
        if not isinstance(privacy,dict) or any(privacy.get(k) is not False for k in ('incognito','noMemory')): return unavailable
        resolved=await bridge.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
        root=resolved.get('rootSessionID'); binding=await bridge._handle_session_binding_read({'sessionID':root})
        root_self = bool(root and identity['sessionID'] == root and identity['actorID'] == 'main' and role in {'metaplan','ledger','concern','slice'})
        if (not root or (identity['sessionID']!=root if role=='progress' else identity['sessionID']==root and not root_self) or binding.get('authority') or binding.get('memoryEnabled') is not True or binding.get('transition')
                or binding.get('owner')!=owner or binding.get('stableChatID')!=chat): return unavailable
        manager=get_session_manager_instance(); state=getattr(manager,'lifecycle_context',None) if manager else None
        auth=getattr(state,'auth_manager',None); barrier=getattr(state,'account_request_barrier',None)
        supervisor=getattr(state,'mimo_supervisor',None); repository=getattr(state,'files_policy_repository',None)
        factory=getattr(state,'files_facade_factory',None); history=getattr(state,'history_supervisor',None)
        pool=supervisor.owner_lifecycle_pool() if supervisor else None; worker=pool.worker_for_owner(owner) if pool else None
        session=manager.get_session(chat) if manager else None
        workspace=binding.get('canonicalCwd'); workspace_id=binding.get('authorityWorkspaceID')
        if (not auth or not barrier or owner_barrier is not barrier or not repository or not callable(factory) or not history or not worker
                or not session or session.owner!=owner or getattr(session,'incognito',False) or getattr(session,'archived',False)
                or not isinstance(workspace,str) or not isinstance(workspace_id,str) or not await barrier.is_live(owner,admission)): return unavailable
        files_binding=resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True)
        if files_binding.path!=workspace: return unavailable
        generation=repository.generation(); context=ProviderContext(account,owner,generation,bool(auth.is_admin(owner)),workspace_id)
        facade=factory(SimpleNamespace(app=SimpleNamespace(state=state)),context); provider=facade._providers.get('host')
        if provider is None: return unavailable
        holder=bridge._managed_provider_generation; process=getattr(worker,'_proc',None); worker_generation=worker.installed_generation
        pool_key=pool._key(owner); pool_state=pool._states.get(pool_key); epoch=pool._owner_lifecycle_epoch(pool_key)
        root_state=bridge._turns.get(root)
        if not holder or not process or not pool_state or not root_state or root_state.root_operation_id!=identity['rootOperationID']: return unavailable
        async with barrier.admitted(owner):
            async with bridge._durable_session_map.async_chat_admission_lock(chat,blocking=False,existing_only=True) as lock:
                mapping=bridge._durable_session_map
                def current_sync():
                    try:
                        mapping.validate_chat_guard(chat,lock); pool._assert_owner_lifecycle(pool_key,epoch)
                        root_context=bridge._session_context.get(root); now_privacy=entry.get('receiptPrivacy')
                        return bool(auth.account_id(owner)==account and auth.username_for_account_id(account)==owner and
                            not getattr(bridge,'_ownerAdmissionHolderStopped',False) and not getattr(admission,'released',True) and
                            bridge._managed_actor_executions.get(key[:4]) is execution and execution.get('ownerAdmission') is admission and
                            execution.get('ownerAdmissionBarrier') is barrier and execution.get('ownerAdmissionAccount')==account and not execution.get('ownerAdmissionStopped') and
                            bridge._managed_actor_turns.get(key) is entry and entry.get('state')=='active' and not entry.get('ownerAdmissionStopped') and
                            entry.get('ownerAdmission') is admission and entry.get('ownerAdmissionBarrier') is barrier and entry.get('ownerAdmissionAccount')==account and
                            all(entry.get(k)==v for k,v in snapshot.items()) and entry.get('policyInputs')==policy_inputs and isinstance(now_privacy,dict) and all(now_privacy.get(k) is False for k in ('incognito','noMemory')) and
                            isinstance(root_context,dict) and root_context.get('root_operation_id')==identity['rootOperationID'] and root_context.get('memory_read_allowed') is True and
                            not root_context.get('incognito') and not root_context.get('auxiliary') and bridge._turns.get(root) is root_state and
                            root_state.root_operation_id==identity['rootOperationID'] and not getattr(root_state,'provider_root_closing',False) and not getattr(root_state,'privacy_capture_veto',False) and
                            pool.worker_for_owner(owner) is worker and pool._states.get(pool_key) is pool_state and worker._proc is process and worker.is_alive() and
                            worker.installed_generation==worker_generation and bridge._managed_provider_generation==holder and
                            getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder and repository.generation()==generation and
                            manager.get_session(chat) is session and session.owner==owner and not getattr(session,'incognito',False) and not getattr(session,'archived',False))
                    except Exception: return False
                source_frozen=None
                task_frozen=None
                async def proof():
                    nonlocal source_frozen,task_frozen
                    response=await worker.session_workflow_source_request(chat,root,workspace,identity['sessionID'],request['sourceToken'],request['messageID'],request['toolCallID'],args_hash,owner=owner,task_assignment=role=='progress',root_self=root_self)
                    data=response.json() if response.status_code==200 else None
                    source=data.get('source') if isinstance(data,dict) else None
                    valid=(isinstance(data,dict) and data.get('state')=='current' and data.get('argsSHA256')==args_hash and data.get('writer')==identity
                            and isinstance(source,dict) and source.get('sessionID')==root and source.get('rootOperationID')==identity['rootOperationID'])
                    if not valid: return False
                    frozen={field:source.get(field) for field in _FIELDS}; origin=source.get('source')
                    if role=='progress':
                        if (frozen!=identity or not isinstance(origin,dict) or set(origin)!={'kind','sessionID','taskID','assignmentEventID'}
                                or origin.get('kind')!='native-task' or origin.get('sessionID')!=root
                                or not isinstance(origin.get('taskID'),str) or not re.fullmatch(r'T[0-9]+(?:\.[0-9]+)*',origin['taskID'])
                                or type(origin.get('assignmentEventID')) is not int or not 0<origin['assignmentEventID']<=9007199254740991): return False
                        if task_frozen is None: task_frozen=dict(origin)
                        if task_frozen!=origin: return False
                    elif origin!={'kind':'root-operation','rootOperationID':identity['rootOperationID']}: return False
                    if root_self and frozen != identity: return False
                    if source_frozen is None: source_frozen=frozen
                    return source_frozen==frozen
                async def current():
                    try:
                        return (current_sync() and await barrier.is_live(owner,admission) and await bridge._handle_session_binding_read({'sessionID':root})==binding
                            and resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True)==files_binding
                            and await proof() and current_sync())
                    except Exception: return False
                if not await current(): return unavailable
                # Choose one independently registered History root that contains
                # the authorized workspace; never infer it from the logical ID.
                choices=[]
                for location in repository.list_locations():
                    path=os.path.realpath(str(getattr(location,'canonical_path','') or ''))
                    if getattr(location,'kind',None)!='directory' or not getattr(location,'enabled',False) or not os.path.isabs(path): continue
                    try:
                        if os.path.commonpath((path,os.path.realpath(workspace))) != path: continue
                        candidate=dict(await authorize_workflow_agent_root(provider,context,path=path,current=current,workspace_root=workspace,chat_id=chat))
                        choices.append(candidate)
                    except Exception: continue
                if not choices: return unavailable
                choices.sort(key=lambda item:len(item['root']),reverse=True)
                if len(choices)>1 and len(choices[0]['root'])==len(choices[1]['root']): return unavailable
                history_root_path=choices[0]['root']; history_root_id=choices[0]['root_id']
                history_root=_origin(history_root_path)
                async def history_identity(path):
                    return await lookup_workflow_history_identity(provider,context,root_origin_id=history_root,root_revision=None,path=path,history_supervisor=history,current=current,workspace_root=workspace,chat_id=chat)
                from src.openclank.workflow_agent_client import WorkflowAgentFilesClient
                def client_factory(path, native_binding):
                    if source_frozen is None or current_sync() is not True: raise ValueError('workflow writer unavailable')
                    names={'sessionID':'session_id','actorID':'actor_id','executionRevision':'execution_revision',
                           'rootOperationID':'root_operation_id','innerTurnID':'inner_turn_id'}
                    writer={names[k]:identity[k] for k in _FIELDS}
                    source={names[k]:source_frozen[k] for k in _FIELDS}
                    return WorkflowAgentFilesClient(owner,workspace,workspace_id=workspace_id,chat_id=chat,account_id=account,
                        writer=writer,source=source,tool_call_id=request['toolCallID'],operation_id=native_binding['operation_id'],
                        request_digest=native_binding['request_digest'],target=path,current_sync=current_sync,
                        history_root_id=history_root_id,history_root_path=history_root_path)
                member_name=('task-'+_digest(['openclank.workflow.task-ref.v1',task_frozen['sessionID'],task_frozen['taskID'],task_frozen['assignmentEventID']])) if role=='progress' and task_frozen else name
                if role=='progress' and not member_name: return unavailable
                progress_source=None
                if role=='progress' and not status and not read and task_frozen:
                    from src.openclank.workflow_contributions import record_progress
                    candidate_source={**identity,'toolCallID':request['toolCallID'],'messageID':request['messageID']}
                    try:
                        if await current():
                            record_progress(chat,owner=owner,account=account,workspace=workspace_id,task_ref=task_frozen,
                                source=candidate_source,operation=operation_id,current=current_sync)
                            progress_source=candidate_source
                    except Exception:
                        pass
                possible_issue=not status and not read
                result=await apply(facade=facade,context=context,chat_id=chat,workspace_root=workspace,
                    subject={'kind':'root_operation','ref':identity['rootOperationID']},role=role,name=member_name,operation_id=operation_id,
                    text=text,expected_revision=expected,status=status,read=read,current=current,current_sync=current_sync,history_identity=history_identity,
                    writer_session_id=identity['sessionID'],client_factory=client_factory,history_root_id=history_root_id,history_root_path=history_root_path,task_ref=task_frozen,
                    initial_plan_allowed=root_self and source_frozen==identity, writer_identity={**identity, "toolCallID":request["toolCallID"]})
                if progress_source is not None:
                    try:
                        if await current():
                            record_progress(chat,owner=owner,account=account,workspace=workspace_id,task_ref=task_frozen,
                                source=progress_source,operation=operation_id,current=current_sync,outcome=result)
                    except Exception:
                        pass
                return result
    except Exception:
        return {'state':'unknown','operationID':operation_id} if possible_issue else unavailable


async def dispatch_snapshot(bridge, request: dict) -> dict:
    """Read only the fixed operational context for a current admitted parent turn."""
    unavailable={'state':'unavailable'}
    refused={'checkpoint':dict(unavailable),'notes':dict(unavailable),'progress':{'items':[],'complete':False}}
    try:
        from core.models import get_session_manager_instance
        from src.openclank.files_facade import ProviderContext
        from src.openclank.files_host_provider import _origin
        from src.openclank.workspace_policy_service import resolve_owned_workspace
        from src.openclank.workflow_artifact_set import apply
        from src.openclank.workflow_file_access import lookup_workflow_history_identity, authorize_workflow_agent_root
        identity={field:request[field] for field in _FIELDS}; key=bridge._managed_turn_key(identity)
        if type(identity['executionRevision']) is not int or not 0<=identity['executionRevision']<=9007199254740991: return refused
        # Reuse current turn policy admission without inventing receipt identities.
        # The start resolver supplies policyInputs before queued receipts exist.
        admitted_turn=await bridge.resolve_managed_recall_turn(identity)
        async with bridge._managed_execution_lock:
            entry=bridge._managed_actor_turns.get(key)
            if not isinstance(entry,dict) or entry.get('state')!='active': return refused
            owner,account,chat=(entry.get('owner'),entry.get('ownerAccountID'),entry.get('stableChatID'))
            snapshot={field:entry.get(field) for field in ('owner','ownerAccountID','stableChatID','authorityWorkspaceID','mappingRevision','workspaceRevision','mapRevision','policyRevision','holderID')}
            privacy=copy.deepcopy(entry.get('receiptPrivacy'))
            ceiling=copy.deepcopy(entry.get('rawPolicyCeiling'))
            inputs=copy.deepcopy(entry.get('policyInputs'))
            projection=copy.deepcopy(entry.get('activePolicyProjection'))
        def private_policy_clear():
            choice=ceiling.get('choiceSnapshot') if isinstance(ceiling,dict) else None
            return (isinstance(choice,dict) and all(choice.get(k) is False for k in ('incognito','noMemory'))
                and isinstance(inputs,list) and bool(inputs)
                and all(isinstance(item,dict) and isinstance(item.get('turnPolicy'),dict)
                    and all(item['turnPolicy'].get(k) is False for k in ('incognito','noMemory')) for item in inputs)
                and isinstance(projection,dict) and projection.get('blockAllToolCalls') is False
                and (privacy is None or (isinstance(privacy,dict) and all(privacy.get(k) is False for k in ('incognito','noMemory')))))
        if (not isinstance(admitted_turn,dict) or admitted_turn.get('owner')!=owner or admitted_turn.get('ownerAccountID')!=account
                or admitted_turn.get('stableChatID')!=chat): return refused
        if not all(isinstance(v,str) and v for v in (owner,account,chat)) or not private_policy_clear(): return refused
        execution=bridge._managed_actor_executions.get(key[:4]); admission=execution.get('ownerAdmission') if isinstance(execution,dict) else None
        owner_barrier=execution.get('ownerAdmissionBarrier') if isinstance(execution,dict) else None
        if not execution or admission is None or execution.get('ownerAdmissionStopped') or getattr(admission,'released',True): return refused
        resolved=await bridge.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
        root=resolved.get('rootSessionID')
        if not root or identity['sessionID']!=root: return refused
        binding=await bridge._handle_session_binding_read({'sessionID':root})
        if binding.get('authority') or binding.get('memoryEnabled') is not True or binding.get('transition') or binding.get('owner')!=owner or binding.get('stableChatID')!=chat: return refused
        manager=get_session_manager_instance();state=getattr(manager,'lifecycle_context',None) if manager else None
        auth=getattr(state,'auth_manager',None);barrier=getattr(state,'account_request_barrier',None)
        supervisor=getattr(state,'mimo_supervisor',None);repository=getattr(state,'files_policy_repository',None)
        factory=getattr(state,'files_facade_factory',None);history=getattr(state,'history_supervisor',None)
        pool=supervisor.owner_lifecycle_pool() if supervisor else None;worker=pool.worker_for_owner(owner) if pool else None
        session=manager.get_session(chat) if manager else None;workspace=binding.get('canonicalCwd');workspace_id=binding.get('authorityWorkspaceID')
        if (not auth or not barrier or owner_barrier is not barrier or not repository or not callable(factory) or not history or not worker
                or not session or session.owner!=owner or getattr(session,'incognito',False) or getattr(session,'archived',False)
                or not isinstance(workspace,str) or not isinstance(workspace_id,str) or not await barrier.is_live(owner,admission)): return refused
        files_binding=resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True)
        if files_binding.path!=workspace: return refused
        generation=repository.generation();context=ProviderContext(account,owner,generation,bool(auth.is_admin(owner)),workspace_id)
        facade=factory(SimpleNamespace(app=SimpleNamespace(state=state)),context);provider=facade._providers.get('host')
        if provider is None: return refused
        holder=bridge._managed_provider_generation;process=getattr(worker,'_proc',None);worker_generation=worker.installed_generation
        pool_key=pool._key(owner);pool_state=pool._states.get(pool_key);epoch=pool._owner_lifecycle_epoch(pool_key);root_state=bridge._turns.get(root)
        if not holder or not process or not pool_state or not root_state or root_state.root_operation_id!=identity['rootOperationID']: return refused
        async with barrier.admitted(owner):
            async with bridge._durable_session_map.async_chat_admission_lock(chat,blocking=False,existing_only=True) as lock:
                mapping=bridge._durable_session_map
                def current_sync():
                    try:
                        mapping.validate_chat_guard(chat,lock);pool._assert_owner_lifecycle(pool_key,epoch)
                        root_context=bridge._session_context.get(root); now_privacy=entry.get('receiptPrivacy')
                        return bool(auth.account_id(owner)==account and auth.username_for_account_id(account)==owner and not getattr(bridge,'_ownerAdmissionHolderStopped',False)
                            and not getattr(admission,'released',True) and bridge._managed_actor_executions.get(key[:4]) is execution
                            and execution.get('ownerAdmission') is admission and execution.get('ownerAdmissionBarrier') is barrier
                            and execution.get('ownerAdmissionAccount')==account and not execution.get('ownerAdmissionStopped')
                            and bridge._managed_actor_turns.get(key) is entry and entry.get('state')=='active' and not entry.get('ownerAdmissionStopped')
                            and entry.get('ownerAdmission') is admission and entry.get('ownerAdmissionBarrier') is barrier and entry.get('ownerAdmissionAccount')==account
                            and all(entry.get(k)==v for k,v in snapshot.items()) and now_privacy==privacy
                            and entry.get('rawPolicyCeiling')==ceiling and entry.get('policyInputs')==inputs and entry.get('activePolicyProjection')==projection and private_policy_clear()
                            and isinstance(root_context,dict) and root_context.get('root_operation_id')==identity['rootOperationID'] and root_context.get('memory_read_allowed') is True
                            and not root_context.get('incognito') and not root_context.get('auxiliary') and bridge._turns.get(root) is root_state
                            and root_state.root_operation_id==identity['rootOperationID'] and not getattr(root_state,'provider_root_closing',False) and not getattr(root_state,'privacy_capture_veto',False)
                            and pool.worker_for_owner(owner) is worker and pool._states.get(pool_key) is pool_state and worker._proc is process and worker.is_alive()
                            and worker.installed_generation==worker_generation and bridge._managed_provider_generation==holder
                            and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder and repository.generation()==generation
                            and manager.get_session(chat) is session and session.owner==owner and not getattr(session,'incognito',False) and not getattr(session,'archived',False))
                    except Exception:return False
                async def current():
                    try:return (current_sync() and await barrier.is_live(owner,admission) and await bridge.resolve_managed_recall_turn(identity)==admitted_turn
                        and await bridge._handle_session_binding_read({'sessionID':root})==binding and resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True)==files_binding and current_sync())
                    except Exception:return False
                if not await current(): return refused
                choices=[]
                for location in repository.list_locations():
                    path=os.path.realpath(str(getattr(location,'canonical_path','') or ''))
                    if getattr(location,'kind',None)!='directory' or not getattr(location,'enabled',False) or not os.path.isabs(path): continue
                    try:
                        if os.path.commonpath((path,os.path.realpath(workspace)))!=path: continue
                        choices.append(dict(await authorize_workflow_agent_root(provider,context,path=path,current=current,workspace_root=workspace,chat_id=chat)))
                    except Exception:continue
                if not choices:return refused
                choices.sort(key=lambda item:len(item['root']),reverse=True)
                if len(choices)>1 and len(choices[0]['root'])==len(choices[1]['root']):return refused
                history_root_path=choices[0]['root'];history_root_id=choices[0]['root_id']
                async def history_identity(path):
                    return await lookup_workflow_history_identity(provider,context,root_origin_id=_origin(history_root_path),root_revision=None,
                        path=path,history_supervisor=history,current=current,workspace_root=workspace,chat_id=chat)
                from src.openclank.transcript_projection import get_mimo_state
                initial_plan=get_mimo_state(chat,owner=owner).get('plan_state')
                if not isinstance(initial_plan,dict): return refused
                initial_assoc=initial_plan.get('artifact_association')
                if not isinstance(initial_assoc,dict): return refused
                plan_identity=(initial_plan.get('revision'),initial_plan.get('digest'),json.dumps(initial_assoc,sort_keys=True,separators=(',',':'),ensure_ascii=False))
                async def snapshot_current():
                    if not await current(): return False
                    latest=get_mimo_state(chat,owner=owner).get('plan_state')
                    assoc=latest.get('artifact_association') if isinstance(latest,dict) else None
                    return isinstance(assoc,dict) and (latest.get('revision'),latest.get('digest'),json.dumps(assoc,sort_keys=True,separators=(',',':'),ensure_ascii=False))==plan_identity
                from src.openclank.workflow_file_access import resolve_workflow_member, workflow_status, workflow_create_status
                from src.openclank.workflow_agent_client import RootedAgentFilesClient
                def status_client(_path,_binding):
                    return RootedAgentFilesClient(owner,workspace,workspace_id=workspace_id,chat_id=chat,
                        history_root_id=history_root_id,history_root_path=history_root_path)
                async def original_operation(role,name,member):
                    key=role if name is None else role+':'+name
                    intent=(member.get('pending_write') if isinstance(member,dict) else None)
                    create=False
                    if intent is None and member is None:
                        intent=(initial_plan.get('artifact_set') or {}).get('metaplan_create') if role=='metaplan' else (initial_assoc.get('create_intents') or {}).get(key)
                        create=True
                    if not isinstance(intent,dict): return None
                    op=intent.get('operation_id')
                    if not isinstance(op,str) or not re.fullmatch(r'workflowop_[a-f0-9]{64}',op): return None
                    answer={'operationID':op,'state':'unknown'}
                    try:
                        if create:
                            from src.plan_approval import workflow_member_path
                            relative=intent.get('relative_path'); session_id=intent.get('writer_session_id')
                            meta=(initial_assoc.get('members') or {}).get('metaplan')
                            if (not isinstance(relative,str) or not isinstance(session_id,str) or not isinstance(meta,dict)
                                    or relative!=workflow_member_path(meta.get('relative_path'),role,name)): return answer
                            target=os.path.abspath(os.path.join(os.path.realpath(workspace),*relative.split('/')))
                            if os.path.commonpath((os.path.realpath(workspace),target))!=os.path.realpath(workspace) or os.path.realpath(target)!=target: return answer
                            result=await workflow_create_status(client_factory=status_client,repository=provider.operation_store,account=account,
                                workspace=workspace_id,session_id=session_id,key=op,path=target,current=snapshot_current)
                            if result.get('state')=='committed': answer['state']='committed'
                            elif result.get('state')=='not_applied': answer['state']='not_applied'
                        else:
                            resource=await resolve_workflow_member(facade,context,workspace_root=workspace,member=member,history_identity=history_identity,current=snapshot_current)
                            result=await workflow_status(facade,context,resource_ref=resource['ref'],resource_id=member['resource_id'],key=op,
                                history_resource_id=member['history_resource_id'],history_identity=history_identity,current=snapshot_current,client_factory=status_client)
                            if result.get('state')=='complete': answer['state']='committed'
                    except Exception: pass
                    return answer
                async def item(role,name):
                    result=await apply(facade=facade,context=context,chat_id=chat,workspace_root=workspace,
                        subject={'kind':'root_operation','ref':identity['rootOperationID']},role=role,name=name,operation_id='',text=None,
                        expected_revision=None,status=False,read=True,current=snapshot_current,current_sync=current_sync,history_identity=history_identity,
                        writer_session_id=identity['sessionID'],client_factory=lambda *_:None,history_root_id=history_root_id,history_root_path=history_root_path)
                    key=role if name is None else role+':'+name
                    member=(initial_assoc.get('members') or {}).get(key)
                    operation=await original_operation(role,name,member)
                    if result.get('state')!='found':
                        answer={'state':result.get('state','unavailable')}
                    else:
                        text=result['text'];answer={'state':'found','text':text,'textSHA256':hashlib.sha256(text.encode('utf-8')).hexdigest(),'revision':result['revision']}
                    if operation is not None: answer['operation']=operation
                    return answer
                checkpoint=await item('checkpoint',None);notes=await item('concern','notes')
                plan=get_mimo_state(chat,owner=owner).get('plan_state'); assoc=plan.get('artifact_association') if isinstance(plan,dict) else None
                members=assoc.get('members') if isinstance(assoc,dict) else None
                progress=[];complete=isinstance(members,dict);total=0
                if isinstance(members,dict):
                    intents=assoc.get('create_intents') if isinstance(assoc,dict) else None
                    intents=intents if isinstance(intents,dict) else {}
                    keys=sorted({key for key in (*members.keys(),*intents.keys()) if isinstance(key,str) and key.startswith('progress:')})
                    if len(keys)>32: complete=False
                    for key in keys[:32]:
                        member=members.get(key); intent=intents.get(key)
                        if member is None: complete=False
                        provenance=member if isinstance(member,dict) else intent
                        ref=provenance.get('task_ref') if isinstance(provenance,dict) else None
                        if (not isinstance(ref,dict) or set(ref)!={'kind','sessionID','taskID','assignmentEventID'} or ref.get('kind')!='native-task'
                                or not isinstance(ref.get('sessionID'),str) or not isinstance(ref.get('taskID'),str)
                                or not re.fullmatch(r'T[0-9]+(?:\.[0-9]+)*',ref['taskID']) or type(ref.get('assignmentEventID')) is not int
                                or not 0<ref['assignmentEventID']<=9007199254740991): complete=False; continue
                        name='task-'+_digest(['openclank.workflow.task-ref.v1',ref['sessionID'],ref['taskID'],ref['assignmentEventID']])
                        if key!='progress:'+name: complete=False; continue
                        public_ref={'sessionID':ref['sessionID'],'taskID':ref['taskID'],'assignmentEventID':ref['assignmentEventID']}
                        operation=await original_operation('progress',name,member)
                        if operation is not None and operation.get('state')=='unknown': complete=False
                        if member is None:
                            value={'taskRef':public_ref,'state':'unknown'}
                            if operation is not None: value['operation']=operation
                            progress.append(value)
                            continue
                        try:
                            result=await apply(facade=facade,context=context,chat_id=chat,workspace_root=workspace,
                                subject={'kind':'root_operation','ref':identity['rootOperationID']},role='progress',name=name,operation_id='',text=None,
                                expected_revision=None,status=False,read=True,current=snapshot_current,current_sync=current_sync,history_identity=history_identity,
                                writer_session_id=identity['sessionID'],client_factory=lambda *_:None,history_root_id=history_root_id,history_root_path=history_root_path,task_ref=ref)
                            state_name=result.get('state')
                            if state_name=='found':
                                text=result['text'];size=len(text.encode('utf-8'))
                                if total+size>262144: complete=False;continue
                                total+=size;value={'taskRef':public_ref,'state':'found','text':text,
                                    'textSHA256':hashlib.sha256(text.encode('utf-8')).hexdigest(),'revision':result['revision']}
                            else:
                                complete=False;value={'taskRef':public_ref,'state':state_name if state_name in {'not_found','unknown','unavailable'} else 'unavailable'}
                        except Exception:
                            complete=False;value={'taskRef':public_ref,'state':'unavailable'}
                        if operation is not None: value['operation']=operation
                        progress.append(value)
                if not await snapshot_current(): return refused
                return {'checkpoint':checkpoint,'notes':notes,'progress':{'items':progress,'complete':complete}}
    except Exception:return refused

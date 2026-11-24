"""Human-owned capture and comparison of bounded indexed Graph topology."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import threading
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from src.auth_helpers import is_internal_tool_identity
from src.openclank.account_request_barrier import UtilityLifetime
from src.openclank.files_facade import ProviderContext
from src.openclank.files_principal import files_principal
from src.openclank.graph_topology_codec import canonical, compare_artifacts, validate_artifact, validate_receipt, _path
from src.openclank.graph_topology_service import capture_current_topology

_ID=re.compile(r'[a-f0-9]{32}\Z')
_ACTIVE={}
_HEADERS={'Cache-Control':'private, no-store'}


def _human(request, provider):
    account,owner,admin=files_principal(request)
    state=request.app.state; auth=getattr(state,'auth_manager',None)
    barrier=getattr(state,'account_request_barrier',None); repository=getattr(state,'files_policy_repository',None)
    factory=getattr(state,'files_facade_factory',None); capture=getattr(state,'capture_original_credential',None)
    history=getattr(state,'history_supervisor',None); process=getattr(history,'process',None)
    if (not account or not owner or is_internal_tool_identity(owner) or not getattr(request.state,'authenticated',False)
        or getattr(request.state,'api_token',False) or getattr(request.state,'internal_tool_authenticated',False)
        or auth is None or barrier is None or repository is None or not callable(factory) or not callable(capture)
        or history is None or process is None or process.poll() is not None or getattr(state,'memory_provider',None) is not provider
        or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner):
        raise ValueError('Topology authority unavailable')
    credential=capture(request,owner,account)
    if not callable(credential) or credential() is not True: raise ValueError('Topology authority unavailable')
    return account,owner,admin,state,auth,barrier,repository,factory,capture,credential,history,process


async def _body(request,limit=196*1024):
    raw=bytearray()
    async for part in request.stream():
        if len(raw)+len(part)>limit: raise ValueError('Topology request too large')
        raw.extend(part)
    def pairs(items):
        value={}
        for key,item in items:
            if key in value: raise ValueError('Duplicate topology input')
            value[key]=item
        return value
    value=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid topology input')))
    if type(value) is not dict: raise ValueError('Invalid topology input')
    return value


def _failure():
    return JSONResponse({'version':1,'state':'unavailable'},status_code=409,headers=_HEADERS)


def _producer_context(*, account, owner, operation, action, target, scope, receipt, raw):
    result=hashlib.sha256(raw).hexdigest(); receipt_sha=hashlib.sha256(canonical(receipt)).hexdigest()
    producer=receipt['config_fingerprint']; length=len(raw)
    material=['openclank.graph-topology.capture.v1',account,owner,operation,action,target,scope,producer,receipt_sha,result,length]
    return {'version':1,'account_id':account,'actor_id':owner,'operation_id':operation,'action_id':action,
        'request_sha256':hashlib.sha256(canonical(material)).hexdigest(),'target_sha256':target,'scope_sha256':scope,
        'producer_sha256':producer,'native_receipt_sha256':receipt_sha,'result_sha256':result,'result_length':length}


async def _qualified_action(*, client, account, owner, action_id, files_provider, context, current, deadline,
                            selected_resource=None, selected_version=None):
    """Derive the private marker from History and reauthorize its Files mapping."""
    from src.openclank.graph_capture_files import qualify_graph_action
    from src.openclank.history_change_qualifier import resolve_registered_history_ids
    if current() is not True: raise ValueError('Topology authority changed')
    loop=asyncio.get_running_loop()
    def worker_current():
        async def check(): return current() is True
        future=asyncio.run_coroutine_threadsafe(check(),loop)
        try: return future.result(timeout=max(0.01,deadline-time.monotonic())) is True
        except Exception: future.cancel(); return False
    response=await asyncio.to_thread(client.get_capture_action,action_id)
    if current() is not True: raise ValueError('Topology authority changed')
    action=response.get('Action') if isinstance(response,dict) else None
    if not isinstance(action,dict): raise ValueError('Topology action unavailable')
    key=action.get('resource_key'); after=action.get('after_resources'); coverage=action.get('coverage')
    marker=coverage.get('metadata',{}).get('graph_topology_capture_v1') if isinstance(coverage,dict) and isinstance(coverage.get('metadata'),dict) else None
    if (not isinstance(key,dict) or key.get('account_id')!=account or not isinstance(key.get('resource_id'),str)
        or not isinstance(after,list) or len(after)!=1 or not isinstance(after[0],dict)
        or not isinstance(marker,dict) or marker.get('action_id')!=action_id
        or marker.get('account_id')!=account or marker.get('actor_id')!=owner):
        raise ValueError('Topology action unavailable')
    private_id=key['resource_id']; paths={}
    resolved=await resolve_registered_history_ids(client, selected_resource or private_id, account=account,
        context=context,provider=files_provider,resolved_paths=paths)
    if resolved != [private_id] or not paths.get(private_id): raise ValueError('Topology resource unavailable')
    if selected_version is not None:
        selected=await asyncio.to_thread(client.resolve_resource_version,private_id,selected_version)
        version=selected.get('version') if isinstance(selected,dict) else None
        if (not isinstance(version,dict) or version.get('phase')!='after' or version.get('availability')!='available'
            or version.get('can_read') is not True or version.get('action_id')!=action_id
            or version.get('version_id')!=(after[0].get('after') or {}).get('version_id')):
            raise ValueError('Topology version unavailable')
    qualified=await asyncio.to_thread(qualify_graph_action,client,context=marker,target=paths[private_id],
        history_workspace=key.get('workspace_id'),current=worker_current)
    if current() is not True: raise ValueError('Topology authority changed')
    again={}
    if await resolve_registered_history_ids(client,selected_resource or private_id,account=account,
            context=context,provider=files_provider,resolved_paths=again)!=[private_id] or again.get(private_id)!=paths[private_id]:
        raise ValueError('Topology resource changed')
    record=validate_artifact(qualified['bytes'],max_bytes=marker['result_length'])
    if (hashlib.sha256(canonical(record['scope'])).hexdigest()!=marker['scope_sha256']
        or hashlib.sha256(canonical(record['receipt'])).hexdigest()!=marker['native_receipt_sha256']
        or record['producer_fingerprint']!=marker['producer_sha256']):
        raise ValueError('Topology provenance changed')
    return record,qualified,private_id


def install_topology_routes(router, provider):
    @router.post('/code/topology/capture')
    async def topology_capture(request:Request):
        try:
            account,owner,admin,state,auth,barrier,repository,factory,capture,credential,history,process=_human(request,provider)
            body=await _body(request)
            if set(body)!={'version','operation_id','workspace_id','paths','expected_receipt','parent_ref','name','parent_revision','limits'} or body['version']!=1:
                raise ValueError('Topology input unavailable')
            workspace=body['workspace_id'];paths=body['paths'];receipt=validate_receipt(body['expected_receipt']);limits=body['limits']
            if type(body['operation_id']) is not str or not _ID.fullmatch(body['operation_id']):raise ValueError('Topology operation unavailable')
            if (not isinstance(workspace,str) or not workspace or len(workspace)>256 or receipt['workspace_id']!=workspace
                or type(paths) is not list or not paths or any(_path(v)!=v for v in paths)
                or paths!=sorted(set(paths),key=lambda v:v.encode('utf-8'))
                or type(limits) is not dict or set(limits)!={'nodes','edges','work'}
                or any(type(limits[k]) is not int or not 1<=limits[k]<=4_000_000 for k in limits)
                or type(body['parent_ref']) is not str or not body['parent_ref'] or len(body['parent_ref'])>16384
                or type(body['name']) is not str or not body['name'] or type(body['parent_revision']) is not dict):
                raise ValueError('Topology input unavailable')
            operation=body['operation_id'];action='graph-topology-'+operation; generation=repository.generation()
            material={'version':1,'workspace_id':workspace,'paths':paths,'expected_receipt':receipt,
                'parent_ref':body['parent_ref'],'name':body['name'],'parent_revision':body['parent_revision'],'limits':limits}
            digest=hashlib.sha256(canonical(material)).hexdigest()
            admission=await barrier.acquire(owner)
            utility=UtilityLifetime(barrier,admission,owner,account,'graph-topology',operation,digest,auth)
            try: await barrier.bind_utility(admission,utility)
            except BaseException: await barrier.release(admission);raise
            record={'kind':'graph_topology_capture_v1','state':'pending','action_id':action,'workspace_id':workspace,
                'parent_ref':body['parent_ref'],'name':body['name']}
            try:
                existing=repository.reserve_operation(owner_subject_id=account,operation_id=operation,request_digest=digest,
                    generation=generation,receipt=record,phase='graph_topology_pending')
                if existing is not None:
                    utility.settled=True;await barrier.release(admission)
                    prior=existing.get('receipt') or {}; prior_state=prior.get('state')
                    if prior.get('kind')!='graph_topology_capture_v1':raise ValueError('Topology operation unavailable')
                    if prior_state in {'pending','prepared'} and (account,operation) not in _ACTIVE:prior_state='unknown'
                    return JSONResponse({'version':1,'state':prior_state if prior_state in {'pending','prepared','saved','unknown','unavailable'} else 'unknown',
                        'operation_id':operation},headers=_HEADERS)
            except BaseException:
                utility.settled=True; await barrier.release(admission); raise
            deadline=time.monotonic()+120; loop=asyncio.get_running_loop(); loop_thread=threading.get_ident()
            def on_loop_current():
                try:
                    barrier.assert_live_utility(utility)
                    return bool(time.monotonic()<deadline and credential() is True and utility.account_current()
                        and getattr(state,'capture_original_credential',None) is capture and getattr(state,'auth_manager',None) is auth
                        and getattr(state,'account_request_barrier',None) is barrier and getattr(state,'files_policy_repository',None) is repository
                        and repository.generation()==generation and getattr(state,'files_facade_factory',None) is factory
                        and getattr(state,'history_supervisor',None) is history and history.process is process and process.poll() is None
                        and getattr(state,'memory_provider',None) is provider)
                except Exception:return False
            def current():
                if threading.get_ident()==loop_thread:return on_loop_current()
                async def check():return on_loop_current()
                future=asyncio.run_coroutine_threadsafe(check(),loop)
                try:return future.result(timeout=max(.01,deadline-time.monotonic())) is True
                except Exception:future.cancel();return False
            async def owned():
                nonlocal record
                try:
                    from src.openclank.graph_capture_files import graph_stage_capacity,create_graph_capture
                    if not current(): raise ValueError('Topology authority unavailable')
                    context=ProviderContext(account,owner,generation,admin,workspace);facade=factory(request,context)
                    files_provider=facade._providers.get('host')
                    if files_provider is None or files_provider.operation_store is not repository: raise ValueError('Files unavailable')
                    capacity=await graph_stage_capacity(facade,context,parent_ref=body['parent_ref'],name=body['name'],
                        expected_parent_revision=body['parent_revision'],current=current)
                    native_limits={'files':len(paths),'nodes':limits['nodes'],'edges':limits['edges'],'work':limits['work'],
                        'bytes':capacity['max_total_bytes'],'page_bytes':min(128*1024,capacity['max_chunk_bytes'])}
                    result=await capture_current_topology(repository=repository,provider=provider,files_provider=files_provider,
                        files_context=context,auth=auth,owner=owner,workspace_id=workspace,receipt=receipt,paths=paths,
                        limits=native_limits,current=current,deadline=deadline)
                    producer=_producer_context(account=account,owner=owner,operation=operation,action=action,
                        target=capacity['target_sha256'],scope=result['scope_sha256'],receipt=receipt,raw=result['bytes'])
                    record={**record,'state':'prepared','producer_context':producer}
                    repository.record_operation(owner_subject_id=account,operation_id=operation,request_digest=digest,
                        generation=generation,receipt=record,phase='graph_topology_pending')
                    async def before_dispatch():
                        if not current() or await result['before_dispatch']() is not True:return False
                        utility.issued=True
                        return True
                    await create_graph_capture(facade,context,parent_ref=body['parent_ref'],name=body['name'],
                        expected_parent_revision=body['parent_revision'],producer_context=producer,content=result['bytes'],
                        current=current,before_dispatch=before_dispatch)
                    client=history.client_for(owner,account)
                    await _qualified_action(client=client,account=account,owner=owner,action_id=action,
                        files_provider=files_provider,context=context,current=current,deadline=deadline)
                    record={**record,'state':'saved'}
                    repository.record_operation(owner_subject_id=account,operation_id=operation,request_digest=digest,
                        generation=generation,receipt=record,phase='graph_topology_complete')
                except Exception as exc:
                    uncertain=utility.issued and getattr(exc,'code',None)!='graph_capture_unissued'
                    record={**record,'state':'unknown' if uncertain else 'unavailable'}
                    try:repository.record_operation(owner_subject_id=account,operation_id=operation,request_digest=digest,
                        generation=generation,receipt=record,phase='graph_topology_unknown' if uncertain else 'graph_topology_unavailable')
                    except Exception:pass
                finally:
                    utility.settled=True
                    await barrier.release(admission)
            task=asyncio.create_task(owned());utility.task=task;_ACTIVE[(account,operation)]=task
            task.add_done_callback(lambda done:_ACTIVE.pop((account,operation),None))
            return JSONResponse({'version':1,'state':'pending','operation_id':operation},headers=_HEADERS)
        except Exception:return _failure()

    @router.post('/code/topology/status')
    async def topology_status(request:Request):
        try:
            account,owner,admin,state,auth,barrier,repository,factory,capture,credential,history,process=_human(request,provider)
            body=await _body(request,2048)
            if set(body)!={'version','operation_id'} or body['version']!=1 or type(body['operation_id']) is not str or not _ID.fullmatch(body['operation_id']):raise ValueError()
            loaded=repository.get_operation(owner_subject_id=account,operation_id=body['operation_id'])
            rec=loaded.get('receipt') if isinstance(loaded,dict) else None
            if not isinstance(rec,dict) or rec.get('kind')!='graph_topology_capture_v1':raise ValueError()
            operation=body['operation_id']; status=rec.get('state') if rec.get('state') in {'pending','prepared','saved','unknown','unavailable'} else 'unknown'
            if status=='pending' and (account,operation) not in _ACTIVE:status='unknown'
            # Reconcile only the same terminal original. A live original keeps custody.
            if status in {'prepared','unknown'} and (account,operation) not in _ACTIVE:
                producer=rec.get('producer_context'); generation=repository.generation()
                if not isinstance(producer,dict) or not isinstance(rec.get('parent_ref'),str) or not isinstance(rec.get('name'),str):
                    return JSONResponse({'version':1,'state':'unknown','operation_id':operation},headers=_HEADERS)
                admission=await barrier.acquire(owner)
                utility=UtilityLifetime(barrier,admission,owner,account,'graph-topology-status',operation,loaded['digest'],auth)
                try: await barrier.bind_utility(admission,utility)
                except BaseException: await barrier.release(admission);raise
                deadline=time.monotonic()+30; loop=asyncio.get_running_loop(); loop_thread=threading.get_ident()
                def on_loop_current():
                    try:
                        barrier.assert_live_utility(utility)
                        return bool(time.monotonic()<deadline and credential() is True and utility.account_current()
                            and getattr(state,'capture_original_credential',None) is capture and getattr(state,'auth_manager',None) is auth
                            and getattr(state,'account_request_barrier',None) is barrier and getattr(state,'files_policy_repository',None) is repository
                            and repository.generation()==generation and getattr(state,'files_facade_factory',None) is factory
                            and getattr(state,'history_supervisor',None) is history and history.process is process and process.poll() is None
                            and getattr(state,'memory_provider',None) is provider)
                    except Exception:return False
                def current():
                    if threading.get_ident()==loop_thread:return on_loop_current()
                    async def check():return on_loop_current()
                    future=asyncio.run_coroutine_threadsafe(check(),loop)
                    try:return future.result(timeout=max(.01,deadline-time.monotonic())) is True
                    except Exception:future.cancel();return False
                async def settle():
                    try:
                        from src.openclank.graph_capture_files import reconcile_graph_capture
                        context=ProviderContext(account,owner,generation,admin,rec['workspace_id']);facade=factory(request,context)
                        files_provider=facade._providers.get('host');client=history.client_for(owner,account)
                        if files_provider is None or files_provider.operation_store is not repository or not current():raise ValueError()
                        _,qualified,_=await _qualified_action(client=client,account=account,owner=owner,action_id=rec['action_id'],
                            files_provider=files_provider,context=context,current=current,deadline=deadline)
                        original={k:producer[k] for k in ('operation_id','action_id','request_sha256')}
                        if original['operation_id']!=operation or original['action_id']!=rec['action_id']:raise ValueError()
                        await reconcile_graph_capture(facade,context,parent_ref=rec['parent_ref'],name=rec['name'],original=original,
                            history_client=client,history_workspace=qualified['resource_key']['workspace_id'],actor_id=owner,current=current)
                        if not current():raise ValueError()
                        repository.record_operation(owner_subject_id=account,operation_id=operation,request_digest=loaded['digest'],
                            generation=loaded['generation'],receipt={**rec,'state':'saved'},phase='graph_topology_complete')
                        return 'saved'
                    except Exception:return 'unknown'
                    finally:utility.settled=True;await barrier.release(admission)
                task=asyncio.create_task(settle());utility.task=task;_ACTIVE[(account,operation)]=task
                task.add_done_callback(lambda done:_ACTIVE.pop((account,operation),None))
                status=await asyncio.shield(task)
            return JSONResponse({'version':1,'state':status,'operation_id':operation},headers=_HEADERS)
        except Exception:return _failure()

    @router.post('/code/topology/compare')
    async def topology_compare(request:Request):
        try:
            account,owner,admin,state,auth,barrier,repository,factory,capture,credential,history,process=_human(request,provider)
            body=await _body(request,32768)
            if set(body)!={'version','workspace_id','resource_id','version_ref','expected_receipt','limits'} or body['version']!=1:raise ValueError()
            workspace=body['workspace_id'];receipt=validate_receipt(body['expected_receipt']);limits=body['limits']
            if (not isinstance(workspace,str) or not workspace or receipt['workspace_id']!=workspace
                or not isinstance(body['resource_id'],str) or not body['resource_id'] or len(body['resource_id'])>256
                or not isinstance(body['version_ref'],str) or not body['version_ref'] or len(body['version_ref'])>1024
                or type(limits) is not dict or set(limits)!={'nodes','edges','work','bytes'}
                or any(type(limits[k]) is not int or not 1<=limits[k]<=4_000_000 for k in ('nodes','edges','work'))
                or type(limits['bytes']) is not int or not 512<=limits['bytes']<=64*1024*1024):raise ValueError()
            generation=repository.generation();admission=await barrier.acquire(owner)
            utility=UtilityLifetime(barrier,admission,owner,account,'graph-topology-compare',uuid.uuid4().hex,'0'*64,auth)
            try: await barrier.bind_utility(admission,utility)
            except BaseException:await barrier.release(admission);raise
            deadline=time.monotonic()+120
            def current():
                try:
                    barrier.assert_live_utility(utility)
                    return bool(time.monotonic()<deadline and credential() is True and utility.account_current()
                        and getattr(state,'auth_manager',None) is auth and getattr(state,'capture_original_credential',None) is capture
                        and getattr(state,'account_request_barrier',None) is barrier and getattr(state,'files_policy_repository',None) is repository
                        and repository.generation()==generation and getattr(state,'files_facade_factory',None) is factory
                        and getattr(state,'history_supervisor',None) is history and history.process is process and process.poll() is None
                        and getattr(state,'memory_provider',None) is provider)
                except Exception:return False
            async def owned():
                try:
                    context=ProviderContext(account,owner,generation,admin,workspace);facade=factory(request,context)
                    files_provider=facade._providers.get('host');client=history.client_for(owner,account)
                    if files_provider is None or files_provider.operation_store is not repository or not current():raise ValueError()
                    from src.openclank.history_change_qualifier import resolve_registered_history_ids
                    found={};private=await resolve_registered_history_ids(client,body['resource_id'],account=account,context=context,
                        provider=files_provider,resolved_paths=found)
                    if len(private)!=1:raise ValueError()
                    selection=await asyncio.to_thread(client.resolve_resource_version,private[0],body['version_ref'])
                    version=selection.get('version') if isinstance(selection,dict) else None
                    if not isinstance(version,dict) or not isinstance(version.get('action_id'),str):raise ValueError()
                    recorded,_,_=await _qualified_action(client=client,account=account,owner=owner,action_id=version['action_id'],
                        files_provider=files_provider,context=context,current=current,deadline=deadline,
                        selected_resource=body['resource_id'],selected_version=body['version_ref'])
                    paths=recorded['scope']['paths']; native_limits={'files':len(paths),'nodes':limits['nodes'],'edges':limits['edges'],
                        'work':limits['work'],'bytes':limits['bytes'],'page_bytes':128*1024}
                    now=await capture_current_topology(repository=repository,provider=provider,files_provider=files_provider,
                        files_context=context,auth=auth,owner=owner,workspace_id=workspace,receipt=receipt,paths=paths,
                        limits=native_limits,current=current,deadline=deadline)
                    result=compare_artifacts(recorded,now['record'])
                    result['scope_paths']=list(recorded['scope']['paths'])
                    if not current():raise ValueError()
                    return {'version':1,'state':'compared','comparison':result}
                finally:
                    utility.settled=True;await barrier.release(admission)
            task=asyncio.create_task(owned());utility.task=task
            return JSONResponse(await asyncio.shield(task),headers=_HEADERS)
        except Exception:return _failure()

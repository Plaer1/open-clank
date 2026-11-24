"""Human-reviewed worktree creation and exact owned Undo/Redo."""
from __future__ import annotations
import asyncio,hashlib,json,os,re,uuid
from fastapi import Request
from src.auth_helpers import is_internal_tool_identity
from src.openclank.account_request_barrier import UtilityLifetime
from src.openclank.files_facade import ProviderContext
from src.openclank.files_host_provider import _origin,_path
from src.openclank.files_principal import files_principal
from src.openclank.git_checkout_authority import issue_guard
from src.openclank.git_checkout_files_client import CheckoutFilesCustody
from src.openclank.git_worktree_lookup import lookup_worktree_version
from src.openclank.files_service_client import client_for_owner
from src.openclank.git_worktree_stage_owner import reviewed_digest,stage_reviewed,register_staged,materialize_registered,prepare_materialized_history,publish_prepared
from src.openclank.git_worktree_inverse_owner import reviewed_inverse_digest,prepare_inverse,run_inverse
from src.openclank.git_worktree_redo_owner import reviewed_redo_digest,prepare_redo,run_redo
from src.openclank.resource_refs import resolve_resource_ref
from src.openclank.transcript_projection import get_managed_binding
from routes.orchestration_routes import _get_observation_binding,_ensure_observation_binding_current

_UNAVAILABLE={'version':1,'state':'unavailable'}
_ID=re.compile(r'^[a-f0-9]{32}$');_SHA=re.compile(r'^[a-f0-9]{64}$');_OID=re.compile(r'^(?:[a-f0-9]{40}|[a-f0-9]{64})$')

def _human(request):
    account,owner,admin=files_principal(request);auth=request.app.state.auth_manager
    factory=getattr(request.app.state,'capture_original_credential',None)
    if (getattr(request.state,'api_token',False) or getattr(request.state,'internal_tool_authenticated',False)
            or is_internal_tool_identity(owner) or not getattr(request.state,'authenticated',False) or not callable(factory)
            or owner not in auth.users or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner):raise ValueError('human unavailable')
    current=factory(request,owner,account)
    if not callable(current) or current() is not True:raise ValueError('human unavailable')
    return account,owner,admin,auth,factory,current

async def _body(request,limit=32768):
    raw=bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk)>limit:raise ValueError('request unavailable')
        raw.extend(chunk)
    value=json.loads(raw)
    if type(value) is not dict:return None
    return value

def _input_digest(body):
    value={k:v for k,v in body.items() if k not in ('requestID','expectedDigest')}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8','strict')).hexdigest()

def _permission(repository,provider,context,location,path,chat,generation):
    scope=provider._scope(context)
    if not provider._path_writable(context,path,scope):return False
    relative=os.path.relpath(path,location['root']) if path!=location['root'] else ''
    return all((p:=repository.resolve(subject_id=context.owner_subject_id,is_admin=context.is_admin,origin='app',location_id=location['root_id'],
        capability=cap,workspace_id=context.workspace_id,relative_resource=relative,chat_id=chat,group_ids=(),observation_only=True)).allowed
        and p.policy_generation==generation for cap in ('read','write'))

async def _source(request,body,account,owner,admin,generation,provider,session_manager):
    ref=resolve_resource_ref(body['rootRef'],expected_owner_subject_id=account,current_policy_generation=generation)
    if ref.provider!='host' or ref.kind!='folder':raise ValueError('source unavailable')
    ctx=ProviderContext(account,owner,generation,is_admin=admin,workspace_id=ref.workspace_id)
    registered=await provider.authorize_readonly_root(ctx,origin_id=ref.origin_id,expected_revision=None)
    if ref.location_id is not None and ref.location_id!=registered['root_id']:raise ValueError('source unavailable')
    user,supervisor,binding,mapping=await asyncio.to_thread(_get_observation_binding,request,body['chatID'],None,session_manager)
    original=get_managed_binding(body['chatID'],owner=owner);cwd=original.get('physicalCwd')
    if (user!=owner or cwd!=registered['root'] or original.get('engineSessionID')!=binding.engine_session_id
            or original.get('transition') is not None or original.get('mappingRevision')!=binding.binding_revision):raise ValueError('source unavailable')
    return ref,registered,supervisor,binding,mapping,original,cwd

async def _owned(request,body,account,owner,admin,auth,factory,credential,barrier,admission,utility,repository,provider,session_manager,confirm):
    generation=repository.generation();source_ref,source,supervisor,binding,mapping,original,cwd=await _source(request,body,account,owner,admin,generation,provider,session_manager)
    source_hash=hashlib.sha256(cwd.encode('utf-8','strict')).hexdigest();kind=body['operation'];selected=None
    if kind=='create':
        ref=resolve_resource_ref(body['destinationRef'],expected_owner_subject_id=account,current_policy_generation=generation)
        if ref.provider!='host' or ref.kind!='folder':raise ValueError('destination unavailable')
        dest_ctx=ProviderContext(account,owner,generation,is_admin=admin,workspace_id=ref.workspace_id)
        entry=await provider.stat(dest_ctx,origin_id=ref.origin_id)
        location=dict(await provider.authorize_selected_folder(dest_ctx,origin_id=ref.origin_id,expected_revision=entry.revision));parent=location['selected_folder']['path'];name=body['name']
        if ref.location_id is not None and ref.location_id!=location['root_id']:raise ValueError('destination unavailable')
    else:
        selected=repository.get_operation(owner_subject_id=account,operation_id=body['selectedOperationID']);receipt=selected.get('receipt') if type(selected) is dict else None
        expected='git_worktree_stage' if kind=='undo' else 'git_worktree_inverse'
        if (type(receipt) is not dict or receipt.get('operation')!=expected or receipt.get('settled') is not True or receipt.get('state')!='applied'
                or receipt.get('chatID')!=body['chatID'] or receipt.get('sourcePathSHA256')!=source_hash or type(receipt.get('destination')) is not dict):raise ValueError('original unavailable')
        record=repository.get_location(receipt['locationID']);root_path=os.path.normpath(str(record.canonical_path));workspace=receipt.get('workspaceID')
        dest_ctx=ProviderContext(account,owner,generation,is_admin=admin,workspace_id=workspace)
        location=dict(await provider.authorize_managed_root(dest_ctx,origin_id=_origin(root_path),expected_revision=None));parent=receipt['destination'].get('parent');name=receipt['destination'].get('name')
        if location['root_id']!=receipt['locationID'] or type(parent) is not str or type(name) is not str or os.path.commonpath((root_path,parent))!=root_path:raise ValueError('original unavailable')
    if not _permission(repository,provider,dest_ctx,location,parent,body['chatID'],generation):raise ValueError('destination unavailable')
    pool=supervisor.owner_lifecycle_pool();lease,runtime,worker,bridge,process,holder,installed,epoch=await pool.admit_git_index_operation(owner=owner)
    utility.supervisor,utility.worker,utility.bridge,utility.process=pool,worker,bridge,process;utility.holder,utility.generation,utility.epoch,utility.lease=holder,installed,epoch,lease
    async def current(expected):
        try:
            pool._assert_owner_lifecycle(owner,epoch);await asyncio.to_thread(_ensure_observation_binding_current,mapping,owner,body['chatID'],binding)
            ctx=ProviderContext(account,owner,expected,is_admin=admin,workspace_id=dest_ctx.workspace_id)
            return bool(not utility.cancel_requested and utility.account_current() and getattr(request.app.state,'capture_original_credential',None) is factory and credential() is True
                and await barrier.is_live(owner,admission) and repository.generation()==expected and _permission(repository,provider,ctx,location,parent,body['chatID'],expected)
                and pool._states.get(owner) is runtime and runtime.active is worker and runtime.status=='ready' and runtime.generation==installed
                and worker.is_alive() and worker._proc is process and worker._bridge is bridge and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder
                and get_managed_binding(body['chatID'],owner=owner)==original and not lease._released)
        except Exception:return False
    if not await current(generation):raise ValueError('authority changed')
    target=body['targetOID'] if kind=='create' else receipt.get('targetOID')
    if type(target) is not str or not _OID.fullmatch(target):raise ValueError('target unavailable')
    response=await worker.session_git_worktree_stage_preview_request(body['chatID'],binding.engine_session_id,cwd,{'version':1,'stableChatID':body['chatID'],'targetOID':target},owner=owner)
    value=response.json()
    if response.status_code!=200 or type(value) is not dict or value.get('state')!='ready' or type(value.get('preview')) is not dict:raise ValueError('preview unavailable')
    preview=value['preview'];operation_id=body['requestID'] if confirm else uuid.uuid4().hex
    def client_at(gen):
        ctx=ProviderContext(account,owner,gen,is_admin=admin,workspace_id=dest_ctx.workspace_id);scope=provider._scope(ctx)
        return client_for_owner(owner,app_scope=None if scope.get('host') else scope,transport='framed')
    client=client_at(generation);guard=issue_guard(lambda:current(generation),deadline=asyncio.get_running_loop().time()+20)
    probe=await CheckoutFilesCustody.acquire(client,location['root'],operation_id,before_issue=guard)
    root_tag=(await probe.probe_child(parent_path=location['root'],root_id=location['root_id'],parent_object_tag=None,component=None))['object_tag']
    parent_tag=(await probe.probe_child(parent_path=parent,root_id=location['root_id'],parent_object_tag=None,component=None))['object_tag']
    if kind=='create':
        occupied=await probe.probe_child(parent_path=parent,root_id=location['root_id'],parent_object_tag=parent_tag,component=name)
        if occupied.get('state')!='absent':raise ValueError('destination occupied')
    tokens=json.loads(probe.history.get('OPENCLANK_HISTORY_WORKTREE_TOKENS','{}'))
    if type(tokens) is not dict or not tokens.get(location['root_id']):raise ValueError('History unavailable')
    args=dict(account=account,location_id=location['root_id'],workspace_id=dest_ctx.workspace_id,chat_id=body['chatID'],operation_id=operation_id,native_id=binding.engine_session_id,source_path=cwd,root_path=location['root'],root_object_tag=root_tag,preview=preview)
    if kind=='create':
        destination={'parent':parent,'objectTag':parent_tag,'name':name};digest,_,_=reviewed_digest(**{k:v for k,v in args.items() if k!='root_path'},destination=destination)
    else:
        lookup=await lookup_worktree_version(client=client,root_path=location['root'],root_id=location['root_id'],root_object_tag=root_tag,operation_id=body['selectedOperationID'],current=lambda:current(generation))
        expected_kind='source_worktree_create_v2' if kind=='undo' else 'source_worktree_inverse_v2'
        if lookup['operation']!=expected_kind:raise ValueError('original unavailable')
        version=lookup['versionID'];extra={'original_operation_id':body['selectedOperationID'],'original_version_id':version} if kind=='undo' else {'inverse_operation_id':body['selectedOperationID'],'inverse_version_id':version}
        digest,_=(reviewed_inverse_digest if kind=='undo' else reviewed_redo_digest)(**args,**extra)
    if not await current(generation):raise ValueError('authority changed')
    if not confirm:return {'version':1,'state':'ready','requestID':operation_id,'expectedDigest':digest,'review':{'operation':kind,'targetOID':target,'files':len(preview['entries']),'bytes':preview['totalBytes']}}
    if digest!=body['expectedDigest']:return {'version':1,'state':'stale'}
    utility.root={'create':'git-worktree-stage','undo':'git-worktree-inverse','redo':'git-worktree-redo'}[kind];utility.idempotency_key=operation_id;utility.request_hash=digest
    utility.git_worktree_selector_ready=True
    if kind=='create':
        result=await stage_reviewed(utility=utility,repository=repository,client=client_at,location_id=location['root_id'],workspace_id=dest_ctx.workspace_id,chat_id=body['chatID'],native_id=binding.engine_session_id,source_path=cwd,root_path=location['root'],root_object_tag=root_tag,preview=preview,expected_digest=digest,current=current,destination=destination,product_input_sha=_input_digest(body))
        if result.get('state')=='sealed':
            result=await register_staged(utility=utility,chat_id=body['chatID'],native_id=binding.engine_session_id,source_path=cwd,preview=preview,destination_parent=parent,destination_parent_object_tag=parent_tag,destination_name=name)
            if result.get('state')=='registered':result=await materialize_registered(utility=utility)
            if result.get('state')=='materialized':result=await prepare_materialized_history(utility=utility)
            if result.get('state')=='history_prepared':result=await publish_prepared(utility=utility,chat_id=body['chatID'],native_id=binding.engine_session_id,source_path=cwd,preview=preview)
    else:
        result=await (prepare_inverse if kind=='undo' else prepare_redo)(utility=utility,repository=repository,client=client_at,location_id=location['root_id'],workspace_id=dest_ctx.workspace_id,chat_id=body['chatID'],native_id=binding.engine_session_id,source_path=cwd,root_path=location['root'],root_object_tag=root_tag,preview=preview,expected_digest=digest,current=current,product_input_sha=_input_digest(body),**extra)
        if result.get('state')=='history_prepared':result=await (run_inverse if kind=='undo' else run_redo)(utility=utility,chat_id=body['chatID'],native_id=binding.engine_session_id,workspace=cwd)
    saved=repository.get_operation(owner_subject_id=account,operation_id=operation_id)
    transition=(saved.get('receipt') or {}).get('policyTransition') if type(saved) is dict else None
    state='applied' if result.get('state')=='completed' else 'status_only' if result.get('state')=='status_only' else 'unknown'
    if utility.issued and state=='unknown':utility.host_cleanup_unknown=True
    return {'version':1,'state':state,'requestID':operation_id,'expectedDigest':digest,'cleanup':result.get('cleanup'),'policyTransition':transition}

def install_worktree_routes(router,*,repository,provider,session_manager):
    async def handle(request:Request,confirm:bool):
        try:
            account,owner,admin,auth,factory,credential=_human(request);body=await _body(request)
            if type(body) is not dict or body.get('version')!=1 or body.get('operation') not in ('create','undo','redo'):return _UNAVAILABLE
            kind=body['operation'];keys={'version','operation','chatID','rootRef'}|({'destinationRef','name','targetOID'} if kind=='create' else {'selectedOperationID'})|({'requestID','expectedDigest'} if confirm else set())
            if set(body)!=keys or any(type(body[k]) is not str or not body[k] or len(body[k].encode('utf-8','strict'))>8192 for k in keys-{'version','operation'}):return _UNAVAILABLE
            if kind!='create' and not _ID.fullmatch(body['selectedOperationID']):return _UNAVAILABLE
            if confirm and (not _ID.fullmatch(body['requestID']) or not _SHA.fullmatch(body['expectedDigest'])):return _UNAVAILABLE
            if confirm:
                old=repository.get_operation(owner_subject_id=account,operation_id=body['requestID'])
                if old is not None:
                    expected={'create':'git_worktree_stage','undo':'git_worktree_inverse','redo':'git_worktree_redo'}[kind]
                    return {'version':1,'state':'status_only' if old.get('digest')==body['expectedDigest'] and old.get('receipt',{}).get('operation')==expected and old.get('receipt',{}).get('productInputSHA256')==_input_digest(body) else 'unknown'}
            barrier=request.app.state.account_request_barrier;admission=await barrier.acquire(owner)
            utility=UtilityLifetime(barrier,admission,owner,account,'git-index-preview',uuid.uuid4().hex,'0'*64,auth)
            try:await barrier.bind_utility(admission,utility)
            except BaseException:
                await barrier.release(admission);raise
            async def task_owner():
                try:return await _owned(request,body,account,owner,admin,auth,factory,credential,barrier,admission,utility,repository,provider,session_manager,confirm)
                except asyncio.CancelledError:
                    if utility.issued:utility.host_cleanup_unknown=True;utility.cancel_requested=True
                    raise
                except Exception:
                    if utility.issued:utility.host_cleanup_unknown=True
                    return {'version':1,'state':'unknown','requestID':utility.idempotency_key,'expectedDigest':utility.request_hash} if utility.issued or getattr(utility,'git_worktree_selector_ready',False) else _UNAVAILABLE
                finally:
                    drained=True
                    if utility.issued and utility.host_cleanup_unknown:
                        if utility.process is None:drained=False
                        else:
                            child=asyncio.create_task(utility.process.wait())
                            while not child.done():
                                try:await asyncio.shield(child)
                                except asyncio.CancelledError:utility.cancel_requested=True
                                except Exception:break
                            try:drained=not child.cancelled() and type(child.result()) is int and utility.process.returncode==child.result()
                            except BaseException:drained=False
                    if drained:
                        if utility.lease is not None and not utility.lease._released:await utility.lease.release()
                        utility.settled=True;await barrier.release(admission)
            task=asyncio.create_task(task_owner());utility.task=task
            if not confirm:return await asyncio.shield(task)
            done,_=await asyncio.wait((task,),timeout=5)
            if done:return task.result()
            row=repository.get_operation(owner_subject_id=account,operation_id=body['requestID'])
            return {'version':1,'state':'pending','requestID':body['requestID'],'expectedDigest':body['expectedDigest'],
                    'policyTransition':(row.get('receipt') or {}).get('policyTransition') if type(row) is dict and row.get('digest')==body['expectedDigest'] else None}
        except Exception:return _UNAVAILABLE
    @router.post('/worktree-preview')
    async def worktree_preview(request:Request):return await handle(request,False)
    @router.post('/worktree-confirm')
    async def worktree_confirm(request:Request):return await handle(request,True)
    @router.post('/worktree-status')
    async def worktree_status(request:Request):
        try:
            account,owner,admin,auth,factory,credential=_human(request);body=await _body(request,4096)
            if type(body) is not dict or set(body)!={'version','requestID','expectedDigest'} or body['version']!=1 or not _ID.fullmatch(body['requestID']) or not _SHA.fullmatch(body['expectedDigest']):return {'version':1,'state':'unknown'}
            barrier=request.app.state.account_request_barrier;admission=await barrier.acquire(owner)
            try:
                row=repository.get_operation(owner_subject_id=account,operation_id=body['requestID'])
                if not row or row['digest']!=body['expectedDigest'] or row['receipt'].get('operation') not in ('git_worktree_stage','git_worktree_inverse','git_worktree_redo') or not await barrier.is_live(owner,admission) or getattr(request.app.state,'capture_original_credential',None) is not factory or credential() is not True:return {'version':1,'state':'unknown'}
                receipt=row['receipt'];state=receipt.get('state') if receipt.get('settled') is True else 'pending'
                if state=='pending':
                    supervisor=getattr(request.app.state,'mimo_supervisor',None);pool=supervisor.owner_lifecycle_pool() if supervisor else None;worker=pool.worker_for_owner(owner) if pool else None;process=getattr(worker,'_proc',None)
                    active=await barrier.utility_for_child(owner,worker,process) if worker is not None and process is not None else []
                    if not any(getattr(v,'idempotency_key',None)==body['requestID'] and getattr(v,'request_hash',None)==body['expectedDigest'] and not v.settled for v in active):state='unknown'
                return {'version':1,'state':state if state in ('applied','pending','unknown') else 'unknown','cleanup':receipt.get('cleanup'),'policyTransition':receipt.get('policyTransition')}
            finally:await barrier.release(admission)
        except Exception:return {'version':1,'state':'unknown'}
    @router.post('/worktree-owned')
    async def worktree_owned(request:Request):
        try:
            account,owner,admin,auth,factory,credential=_human(request);body=await _body(request,8192)
            if type(body) is not dict or set(body)!={'version','chatID','rootRef'} or body['version']!=1:return _UNAVAILABLE
            barrier=request.app.state.account_request_barrier;admission=await barrier.acquire(owner)
            try:
                generation=repository.generation();ref=resolve_resource_ref(body['rootRef'],expected_owner_subject_id=account,current_policy_generation=generation)
                ctx=ProviderContext(account,owner,generation,is_admin=admin,workspace_id=ref.workspace_id);source=await provider.authorize_readonly_root(ctx,origin_id=ref.origin_id,expected_revision=None)
                source_hash=hashlib.sha256(source['root'].encode('utf-8','strict')).hexdigest();items=[];rows=repository.list_operations(owner_subject_id=account,limit=100)
                for row in rows:
                    rec=row['receipt'];kind={'git_worktree_stage':'undo','git_worktree_inverse':'redo'}.get(rec.get('operation'))
                    if kind and rec.get('settled') is True and rec.get('state')=='applied' and rec.get('cleanup')=='released' and rec.get('chatID')==body['chatID'] and rec.get('sourcePathSHA256')==source_hash and type(rec.get('targetOID')) is str:
                        items.append({'operationID':row['operation_id'],'action':kind,'targetOID':rec['targetOID']})
                if (not await barrier.is_live(owner,admission) or getattr(request.app.state,'capture_original_credential',None) is not factory or credential() is not True
                        or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner or repository.generation()!=generation):return _UNAVAILABLE
                return {'version':1,'state':'ready','items':items,'partial':len(rows)==100}
            finally:await barrier.release(admission)
        except Exception:return _UNAVAILABLE

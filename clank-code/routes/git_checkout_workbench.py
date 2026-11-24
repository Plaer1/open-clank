"""Human-owned reviewed Git checkout admission; private Git workbench owner."""
from __future__ import annotations

import asyncio,base64,hashlib,json,re,secrets,time,uuid
from src.openclank.files_facade import ProviderContext
from src.openclank.git_checkout_authority import CheckoutContext,_settle,issue_guard,issue_current,native_checkout_action_sha256,reserve_checkout_once
from src.openclank.git_checkout_files_client import CheckoutFilesCustody
from src.openclank.git_checkout_phase import CheckoutPhase
from src.openclank.git_checkout_plan import build_checkout_files,files_prepare_request,verify_checkout_git_state
from src.openclank.git_workbench import derive_git_commit_witness
from src.openclank.resource_refs import resolve_resource_ref
from src.openclank.transcript_projection import get_managed_binding
from routes.orchestration_routes import _get_observation_binding,_ensure_observation_binding_current


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8','strict')).hexdigest()


async def preview_owned(request,account,owner,admin,auth,barrier,admission,utility,*,repository,provider,session_manager,confirm=False):
    unavailable={'version':1,'state':'unavailable'}
    try:
        raw=bytearray()
        async for chunk in request.stream():
            if len(raw)+len(chunk)>32768:return unavailable
            raw.extend(chunk)
        body=json.loads(raw);keys={'version','chatID','rootRef','operation','targetRef'}|({'requestID','expectedDigest'} if confirm else set())
        if (type(body) is not dict or set(body)!=keys or type(body['version']) is not int or body['version']!=1
                or body['operation'] not in ('switch','pull') or any(type(body[k]) is not str or not body[k] or len(body[k].encode('utf-8','strict'))>8192 for k in ('chatID','rootRef','targetRef'))):return unavailable
        prefix='refs/heads/' if body['operation']=='switch' else 'refs/remotes/'
        if not body['targetRef'].startswith(prefix):return unavailable
        if confirm and (not re.fullmatch(r'[0-9a-f]{32}',str(body['requestID'])) or not re.fullmatch(r'[0-9a-f]{64}',str(body['expectedDigest']))):return unavailable
        root_hash=hashlib.sha256(body['rootRef'].encode()).hexdigest()
        if confirm:
            utility.idempotency_key,utility.request_hash=body['requestID'],body['expectedDigest'];utility.git_index_selector_ready=True
            prior=repository.get_operation(owner_subject_id=account,operation_id=body['requestID'])
            if prior is not None:
                receipt=prior.get('receipt') or {}
                if prior.get('digest')!=body['expectedDigest'] or receipt.get('operation')!='git_checkout' or receipt.get('chatID')!=body['chatID'] or receipt.get('rootRefSHA256')!=root_hash or receipt.get('targetRef')!=body['targetRef'] or receipt.get('checkoutOperation')!=body['operation']:return {'version':1,'state':'unknown'}
                return {'version':1,'state':'status_only'}
        generation=repository.generation();active=[generation]
        root=resolve_resource_ref(body['rootRef'],expected_owner_subject_id=account,current_policy_generation=generation)
        if root.provider!='host' or root.kind!='folder':return unavailable
        root_context=ProviderContext(account,owner,generation,is_admin=admin,workspace_id=root.workspace_id)
        registered=await provider.authorize_readonly_root(root_context,origin_id=root.origin_id,expected_revision=None);location=registered['root_id']
        if root.location_id is not None and root.location_id!=location:return unavailable
        user,supervisor,binding,mapping=await asyncio.to_thread(_get_observation_binding,request,body['chatID'],None,session_manager)
        if user!=owner:return unavailable
        original=get_managed_binding(body['chatID'],owner=owner);cwd=original.get('physicalCwd')
        if original.get('engineSessionID')!=binding.engine_session_id or original.get('transition') is not None or original.get('mappingRevision')!=binding.binding_revision or cwd!=registered['root']:return unavailable
        pool=supervisor.owner_lifecycle_pool();lease=None
        try:
            lease,runtime,worker,bridge,process,holder,installed,epoch=await pool.admit_git_index_operation(owner=owner)
            utility.supervisor,utility.worker,utility.bridge,utility.process=pool,worker,bridge,process
            utility.holder,utility.generation,utility.epoch,utility.lease=holder,installed,epoch,lease
            async def current():
                try:
                    pool._assert_owner_lifecycle(owner,epoch)
                    await asyncio.to_thread(_ensure_observation_binding_current,mapping,owner,body['chatID'],binding)
                    return bool(not utility.cancel_requested and utility.account_current() and getattr(request.app.state,'capture_original_credential',None) is utility.checkout_credential_factory and utility.checkout_credential_current() is True and await barrier.is_live(owner,admission)
                        and repository.generation()==active[0] and pool._states.get(owner) is runtime and runtime.active is worker
                        and runtime.status=='ready' and runtime.generation==installed and worker.is_alive() and worker._proc is process
                        and worker._bridge is bridge and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder
                        and get_managed_binding(body['chatID'],owner=owner)==original and not lease._released)
                except Exception:return False
            async def native_commit():
                if not await current():raise ValueError('checkout authority changed')
                response=await worker.session_git_commit_preview_request(body['chatID'],binding.engine_session_id,cwd,owner=owner)
                value=response.json()
                if response.status_code!=200 or len(response.content)>4*1024*1024+131072 or type(value) is not dict or value.get('state')!='ready' or value.get('gitCommitControlV1') is not True:raise ValueError('Git unavailable')
                return value['preview']
            async def read(op,path=None):
                if not await current():raise ValueError('checkout authority changed')
                payload={'stableChatID':body['chatID'],'operation':op,**({'path':path} if path is not None else {})}
                response=await worker.session_git_read_request(body['chatID'],binding.engine_session_id,cwd,payload,owner=owner);value=response.json()
                if (response.status_code!=200 or len(response.content)>400000 or type(value) is not dict
                        or set(value)!={'state','data','indexExists','digest','truncated'} or value['state']!='ready' or value['truncated'] is not False):raise ValueError('Git read unavailable')
                data=base64.b64decode(value['data'],validate=True)
                if len(data)>262144 or hashlib.sha256(data).hexdigest()!=value['digest']:raise ValueError('Git read changed')
                return data
            async def observe():
                if not await current():raise ValueError('checkout authority changed')
                response=await worker.session_git_checkout_preview_request(body['chatID'],binding.engine_session_id,cwd,{'version':1,'stableChatID':body['chatID'],'operation':body['operation'],'targetRef':body['targetRef']},owner=owner);value=response.json()
                if (response.status_code!=200 or len(response.content)>4*1024*1024+131072 or type(value) is not dict
                        or set(value)!={'version','state','gitCheckoutControlV1','preview','candidate'} or value['version']!=1
                        or value['state']!='ready' or value['gitCheckoutControlV1'] is not True):raise ValueError('checkout preview unavailable')
                preview=value['preview'];candidate=value['candidate']
                if type(candidate) is not dict or set(candidate)!={'sha256','size'} or not re.fullmatch(r'[0-9a-f]{64}',str(candidate['sha256'])) or type(candidate['size']) is not int:return None
                witness=await derive_git_commit_witness(utility=utility,provider=provider,repository=repository,account=account,root_origin=root.origin_id,
                    location_id=location,workspace_id=root.workspace_id,policy_generation=active[0],chat_id=body['chatID'],is_admin=admin,group_ids=(),current=current,native_read=native_commit)
                if witness['snapshot']['git']!=preview['source']:raise ValueError('Git preview changed')
                ctx=ProviderContext(account,owner,active[0],is_admin=admin,workspace_id=root.workspace_id)
                client=provider._client(ctx,provider._scope(ctx));guard=issue_guard(current,deadline=asyncio.get_running_loop().time()+55.0)
                custody=await CheckoutFilesCustody.acquire(client,cwd,operation_id,before_issue=guard)
                plan=await build_checkout_files(preview=preview,expected_operation=body['operation'],expected_ref=body['targetRef'],provider=provider,repository=repository,
                    context=ctx,root_origin=root.origin_id,location_id=location,chat_id=body['chatID'],is_admin=admin,current=current,read_blob=lambda oid:read('blob',oid),probe_child=custody.probe_child)
                if custody.history.get('OPENCLANK_HISTORY_ACCOUNT_ID')!=account:raise ValueError('History account changed')
                prepare=files_prepare_request(plan=plan,account_id=account,operation_id=operation_id,root_id=location,root_path=cwd)
                return preview,candidate,plan,prepare,custody
            operation_id=body['requestID'] if confirm else uuid.uuid4().hex
            observed=await observe()
            if observed is None:return unavailable
            preview,candidate,plan,prepare,custody=observed
            digest=_digest({'version':1,'profile':'reviewed_git_checkout_v1','requestID':operation_id,'account':account,'chatID':body['chatID'],'rootRefSHA256':root_hash,
                'gitPlanDigest':plan['gitPlanDigest'],'filesRequestDigest':prepare['request_digest'],'candidateSHA256':candidate['sha256']})
            if not confirm:return {'version':1,'state':'ready','requestID':operation_id,'expectedDigest':digest,'review':{'operation':body['operation'],'targetRef':preview['targetRef'],'targetOID':preview['targetOID'],'sourceRef':preview['source']['headRef'],'files':plan['review']}}
            if digest!=body['expectedDigest']:return {'version':1,'state':'stale'}
            utility.root,utility.idempotency_key,utility.request_hash='git-checkout',operation_id,digest
            pending={'version':1,'state':'pending','operation':'git_checkout','intentDigest':digest,'chatID':body['chatID'],'rootRefSHA256':root_hash,'locationID':location,'workspaceID':root.workspace_id,'targetRef':body['targetRef'],'checkoutOperation':body['operation']}
            admitted=reserve_checkout_once(repository=repository,account=account,location_id=location,workspace_id=root.workspace_id,chat_id=body['chatID'],operation_id=operation_id,request_digest=digest,generation=generation,expires_unix_ms=int(time.time()*1000)+120000,pending=pending)
            if admitted['state']!='admitted':return {'version':1,'state':'status_only'}
            active[0]=admitted['generation'];pending=admitted['pending']
            renewed=await observe()
            if renewed is None or renewed[0]!=preview or renewed[1]!=candidate or renewed[2]!=plan or renewed[3]!=prepare:raise ValueError('checkout changed')
            custody=renewed[4];capability=secrets.token_hex(32)
            action=native_checkout_action_sha256(session_id=binding.engine_session_id,chat_id=body['chatID'],operation_id=operation_id,request_digest=digest,git_plan_digest=plan['gitPlanDigest'],files_request_digest=prepare['request_digest'],candidate_sha256=candidate['sha256'],count=len(plan['items']),preview=preview)
            pending['actionSHA256']=action
            repository.record_operation(owner_subject_id=account,operation_id=operation_id,request_digest=digest,generation=generation,receipt=pending,phase='git_checkout_pending')
            identity={'operationID':operation_id,'requestDigest':digest,'capability':capability,'actionSHA256':action,'gitPlanDigest':plan['gitPlanDigest'],'filesRequestDigest':prepare['request_digest']}
            phase_fields={k:prepare[k] for k in ('operation_id','request_digest','git_plan_digest','root_id','root_object_tag')}
            machine=CheckoutPhase(custody=custody,verify=current,identity=identity,prepare=prepare,phase=phase_fields,count=len(plan['items']),source_head=preview['source']['headOID'],source_ref=preview['source']['headRef'],source_index=preview['source']['physicalIndex']['sha256'],target_head=preview['targetOID'],target_ref=preview['targetRef'] if body['operation']=='switch' else preview['source']['headRef'],target_index=candidate['sha256'])
            context=None
            def policy_current():
                try:
                    row=repository.get_binding(admitted['binding_id'])
                    return bool(getattr(request.app.state,'capture_original_credential',None) is utility.checkout_credential_factory and utility.checkout_credential_current() is True and repository.generation()==active[0] and row.status=='consumed' and row.remaining_uses==0 and row.generation==active[0] and row.revision==2 and row.subject_id==account and row.location_id==location and row.workspace_id==root.workspace_id and row.chat_id==body['chatID'] and row.resource_ref=='git-checkout-action:sha256:'+digest and row.operation=='git_checkout')
                except Exception:return False
            async def fresh():
                if not await current() or not policy_current():return False
                event=context.current_event if context else None
                if event and event.get('kind') in ('before_ref','failure','finalize'):
                    async def read_index():
                        response=await worker.session_git_index_preview_request(body['chatID'],binding.engine_session_id,cwd,owner=owner);value=response.json()
                        if response.status_code!=200 or value.get('state')!='ready' or value.get('gitIndexControlV1') is not True:raise ValueError('index unavailable')
                        return value['physicalIndex']
                    return await verify_checkout_git_state(preview=preview,candidate_sha256=candidate['sha256'],event=event,read_index=read_index,read_git=read,current=current)
                return True
            context=CheckoutContext(utility,machine,repository,generation,dict(pending),fresh);utility.git_checkout_context=context
            custody.before_issue=issue_guard(lambda:issue_current(utility,context,policy_current),deadline=asyncio.get_running_loop().time()+55.0)
            payload={'version':1,'stableChatID':body['chatID'],'operationID':operation_id,'requestDigest':digest,'capability':capability,'gitPlanDigest':plan['gitPlanDigest'],'filesRequestDigest':prepare['request_digest'],'candidateSHA256':candidate['sha256'],'count':len(plan['items']),'expectedPreview':preview}
            if not await fresh():raise ValueError('checkout changed')
            task=asyncio.create_task(worker.session_git_checkout_apply_request(body['chatID'],binding.engine_session_id,cwd,payload,owner=owner))
            utility.git_checkout_http_task=task;utility.issued=True
            await drain_owned(utility)
            row=repository.get_operation(owner_subject_id=account,operation_id=operation_id)
            return {'version':1,'state':(row.get('receipt') or {}).get('state','unknown') if row and context.finished else 'unknown','requestID':operation_id,'expectedDigest':digest,'policyTransition':dict(pending['policyTransition'])}
        finally:
            if lease is not None and (not utility.issued or getattr(utility,'git_index_drained',False)):await lease.release()
    except Exception:
        return {'version':1,'state':'unknown','requestID':utility.idempotency_key,'expectedDigest':utility.request_hash} if utility.issued or getattr(utility,'git_index_selector_ready',False) else unavailable


async def drain_owned(utility):
    """Retain original child until its result or positively observed exit."""
    async def drain():
        task=utility.git_checkout_http_task;context=utility.git_checkout_context
        try:
            response=await asyncio.shield(task)
            if not context.finished and response.status_code==200:
                value=response.json()
                if (type(value) is dict and set(value)=={'state','index','ref','steps'}
                        and value['state'] in ('applied','refused','rolled_back','unknown')
                        and value['index'] in ('not_issued','applied','restored','unknown')
                        and value['ref'] in ('not_issued','applied','unknown') and type(value['steps']) is int):
                    async with context.lock:
                        if not context.finished and _settle(context,original_exit=True):context.finished=True;context.completion.set()
        except Exception:
            pass
        if not context.finished:
            finish=asyncio.create_task(context.completion.wait());child=asyncio.create_task(utility.process.wait())
            try:
                done,_=await asyncio.wait((finish,child),return_when=asyncio.FIRST_COMPLETED)
                if child in done and not child.cancelled() and type(child.result()) is int and utility.process.returncode==child.result():
                    async with context.lock:
                        if not context.finished and _settle(context,original_exit=True):context.finished=True;context.completion.set()
                if not context.finished:
                    utility.host_cleanup_unknown=True
                    await finish
            finally:
                for pending in (finish,child):
                    if not pending.done():pending.cancel()
                await asyncio.gather(finish,child,return_exceptions=True)
        if not context.finished:raise RuntimeError('original checkout outcome unavailable')
    owned=asyncio.create_task(drain())
    while not owned.done():
        try:await asyncio.shield(owned)
        except asyncio.CancelledError:utility.cancel_requested=True
    await owned;utility.git_index_drained=True

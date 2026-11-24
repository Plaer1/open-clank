"""One named human's reviewed, account-owned regular-file History restore."""
from __future__ import annotations
import hashlib,json,os,re,secrets,stat
from dataclasses import replace
from pathlib import Path
from src.openclank.history_client import HistoryClient,registered_resource_pages
from src.openclank.history_io import bounded_history_io
from src.openclank.history_restore_custody import owned_host_restore
from src.openclank.files_host_provider import host_origin_for_path
from src.openclank.resource_refs import stable_resource_id

_ID=re.compile(r'[A-Za-z0-9_-]{16,80}')
_DIGEST=re.compile(r'[0-9a-f]{64}')
_KIND='regular_file_restore_v1'
def _hash(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
def _selector(source):
    if type(source) is not dict or len(source)!=1:raise ValueError('restore source unavailable')
    if set(source)=={'version_ref'} and type(source['version_ref']) is str and 1<=len(source['version_ref'])<=2048:return {'version_ref':source['version_ref']}
    if set(source)=={'inverse_request_id'} and type(source['inverse_request_id']) is str and _ID.fullmatch(source['inverse_request_id']):return {'inverse_request_id':source['inverse_request_id']}
    raise ValueError('restore source unavailable')
def _current(owner,account,auth,barrier,supervisor,process,store,generation,original_current,admin):
    try:return bool(original_current() is True and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner and admin is False and supervisor.process is process and process.poll() is None and store.generation()==generation)
    except Exception:return False

async def _destination(*,client,resource_id,account,owner,provider,store,context):
    if not re.fullmatch(r'resource-[0-9a-f]{64}',resource_id or ''):raise ValueError('restore destination unavailable')
    matches=[]
    async def page(cursor):return await bounded_history_io(lambda:client.list_registered_resources(cursor=cursor,limit=100))
    async for result in registered_resource_pages(page):
        for row in result['items']:
            root=row.get('root_path');relative=row.get('relative_path');root_id=row.get('root_id');actors=row.get('actor_ids')
            if (type(row.get('active')) is not bool or row.get('account_id')!=account or not row.get('workspace_id') or type(root) is not str or not os.path.isabs(root) or type(root_id) is not str or not root_id or type(relative) is not str or not relative or Path(relative).is_absolute() or any(p in {'.','..'} for p in Path(relative).parts) or type(actors) is not list or (actors and client.actor_id not in actors)):continue
            path=os.path.join(root,relative)
            if row['active'] is False and os.path.lexists(path):continue  # a removed file is restored only into an absent leaf (the History service enforces it again)
            if stable_resource_id(owner_subject_id=account,provider='host',origin_id=host_origin_for_path(path))!=resource_id:continue
            private=row.get('resource_id')
            if type(private) is not str or not private.startswith('file:'):raise ValueError('restore registration unavailable')
            matches.append((private,path,row))
            if len(matches)>1:raise ValueError('restore registration ambiguous')
    if len(matches)!=1:raise ValueError('restore registration unavailable')
    private,path,row=matches[0];root=row['root_path'];parent=os.path.dirname(path)
    if os.path.realpath(root)!=root or os.path.realpath(parent)!=parent or os.path.commonpath((root,parent))!=root:raise ValueError('restore parent unavailable')
    info=os.lstat(parent)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):raise ValueError('restore parent unavailable')
    # History Agent roots are binding identities, not Location identities.
    # The registry identifies the current binding; Human app policy below is
    # still independently required for each actual destination.
    from src.openclank.filesystem_registry import FilesystemRootRegistry
    registry=FilesystemRootRegistry(repository=store)
    roots=[item for item in registry.list(owner) if item.get('id')==row['root_id']]
    if len(roots)>1 or store.generation()!=context.policy_generation:raise ValueError('restore root unavailable')
    if roots:
        selected_root=roots[0]
        if (not selected_root.get('enabled') or selected_root.get('availability')!='available'
                or selected_root.get('kind')!='recursive_directory' or not selected_root.get('location_id')):
            raise ValueError('restore root unavailable')
        location=store.get_location(selected_root['location_id'])
        if os.path.normpath(str(selected_root.get('canonical_path') or ''))!=os.path.normpath(root):
            raise ValueError('restore root unavailable')
    else:
        # Existing Human registrations name the exact Location directly.
        location=store.get_location(row['root_id'])
        if os.path.normpath(location.canonical_path)!=os.path.normpath(root):raise ValueError('restore root unavailable')
    location_root=os.path.normpath(location.canonical_path)
    if (not location.enabled or location.availability!='available' or location.kind!='directory'
            or not os.path.isabs(location_root) or os.path.realpath(location_root)!=location_root
            or os.path.commonpath((location_root,root))!=location_root):raise ValueError('restore root unavailable')
    app_scope=provider._scope(context)
    response=await provider._client(context,app_scope).request('stat',parent,{'include_fingerprint':False})
    data=response.get('data') if isinstance(response,dict) else None
    if type(data) is not dict or data.get('path')!=parent or type(data.get('kind')) is not str or data['kind'].lower() not in {'directory','recursive_directory'}:raise ValueError('restore parent unavailable')
    parent_resource=provider._resource(parent,name=Path(parent).name or parent,kind=data['kind'],writable=provider._path_writable(context,parent,app_scope))
    if parent_resource.origin_id!=host_origin_for_path(parent) or parent_resource.kind not in {'folder','directory'}:raise ValueError('restore parent unavailable')
    after=os.lstat(parent)
    if not stat.S_ISDIR(after.st_mode) or stat.S_ISLNK(after.st_mode) or (after.st_dev,after.st_ino)!=(info.st_dev,info.st_ino):raise ValueError('restore parent changed')
    workspace=context.workspace_id or parent_resource.workspace_id
    if context.workspace_id and parent_resource.workspace_id and context.workspace_id!=parent_resource.workspace_id:
        raise ValueError('restore workspace unavailable')
    if workspace is not None:
        selected=store.get_workspace(workspace)
        if selected.archived or selected.owner_subject_id!=account or selected.location_id!=location.id:raise ValueError('restore workspace unavailable')
    for target in (parent,path):
        relative_target=os.path.relpath(target,location_root);relative_target='' if relative_target=='.' else relative_target
        for capability in ('read','write'):
            scope=store.resolve(subject_id=account,is_admin=context.is_admin,origin='app',location_id=location.id,workspace_id=workspace,relative_resource=relative_target,capability=capability,observation_only=True)
            if not scope.allowed or scope.policy_generation!=context.policy_generation:raise ValueError('restore policy unavailable')
        if not provider._path_writable(replace(context,workspace_id=workspace),target):raise ValueError('restore write unavailable')
    try: leaf=os.lstat(path)
    except FileNotFoundError:leaf=None
    if leaf is not None:
        if not stat.S_ISREG(leaf.st_mode) or stat.S_ISLNK(leaf.st_mode):raise ValueError('restore destination unavailable')
        actual=await provider.stat_inspection(replace(context,workspace_id=workspace),origin_id=host_origin_for_path(path))
        if actual.origin_id!=host_origin_for_path(path) or actual.kind!='file':raise ValueError('restore destination unavailable')
    return {'private_id':private,'path':path,'registration':row,'workspace_id':workspace,'location_id':location.id,'location_revision':location.revision,'parent_device':info.st_dev,'parent_inode':info.st_ino,'present':leaf is not None}

async def _review(*,resource_id,source,request_id,owner,account,auth,barrier,supervisor,process,store,context,provider,original_current):
    source=_selector(source)
    if not _current(owner,account,auth,barrier,supervisor,process,store,context.policy_generation,original_current,context.is_admin):raise ValueError('restore authority unavailable')
    client=supervisor.client_for(owner,account);dest=await _destination(client=client,resource_id=resource_id,account=account,owner=owner,provider=provider,store=store,context=context)
    if 'version_ref' in source:native={'Selected':{'version_ref':source['version_ref']}}
    else:
        prior=store.get_operation(owner_subject_id=account,operation_id=source['inverse_request_id']);row=prior.get('receipt') if isinstance(prior,dict) else None
        if not isinstance(row,dict) or row.get('kind')!=_KIND or row.get('state')!='complete' or row.get('verified') is not True or row.get('resource_id')!=resource_id or type(row.get('restore_id')) is not str:raise ValueError('inverse unavailable')
        native={'Inverse':{'restore_id':row['restore_id']}}
    preview=await bounded_history_io(lambda:client.preview_regular_file_v1(dest['private_id'],native))
    fresh=await _destination(client=client,resource_id=resource_id,account=account,owner=owner,provider=provider,store=store,context=context)
    if fresh!=dest or not _current(owner,account,auth,barrier,supervisor,process,store,context.policy_generation,original_current,context.is_admin):raise ValueError('restore authority changed')
    desired=preview['desired'];expected=preview['expected'];current=expected['state']
    if (current['state']=='Present')!=dest['present']:raise ValueError('restore destination changed')
    effect='delete' if desired['state']=='Absent' else 'create' if current['state']=='Absent' else 'replace'
    material={'profile':_KIND,'account':account,'owner':owner,'request_id':request_id,'resource_id':resource_id,'source':source,'native':native,'destination':dest,'preview':preview,'generation':context.policy_generation,'process':id(process)}
    name=Path(dest['path']).name
    if not name or len(name.encode('utf-8','strict'))>1024:raise ValueError('restore name unavailable')
    return {'client':client,'dest':dest,'native':native,'preview':preview,'digest':_hash(material),'review':{'name':name,'current':current,'desired':desired,'effect':effect,'consistency':'optimistic_path_guard'}}

async def preview(*,resource_id,source,owner,account,auth,barrier,supervisor,store,context,provider,original_current):
    request_id=secrets.token_urlsafe(24);process=supervisor.process
    async with barrier.admitted(owner):
        value=await _review(resource_id=resource_id,source=source,request_id=request_id,owner=owner,account=account,auth=auth,barrier=barrier,supervisor=supervisor,process=process,store=store,context=context,provider=provider,original_current=original_current)
    return {'version':1,'profile':'regular_file_v1','state':'ready','resource_id':resource_id,'source':source,'request_id':request_id,'expected_digest':value['digest'],'review':value['review']}

async def status(*,resource_id,source,request_id,expected_digest,owner,account,auth,barrier,supervisor,store,original_current):
    _selector(source)
    if not _ID.fullmatch(request_id or '') or not _DIGEST.fullmatch(expected_digest or ''):raise ValueError('restore status unavailable')
    async with barrier.admitted(owner):
        if original_current() is not True or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:raise ValueError('restore status unavailable')
        saved=store.get_operation(owner_subject_id=account,operation_id=request_id);row=saved.get('receipt') if isinstance(saved,dict) else None
        if saved is None:return {'version':1,'state':'unknown','request_id':request_id,'expected_digest':expected_digest}
        if saved.get('digest')!=expected_digest or not isinstance(row,dict) or row.get('kind')!=_KIND or row.get('resource_id')!=resource_id or row.get('source')!=source:raise ValueError('restore status conflict')
        if row.get('state') in {'complete','conflict','partial','refused'}:return {'version':1,'state':row['state'],'request_id':request_id,'expected_digest':expected_digest,'resource_id':resource_id,'verified':row.get('verified') is True}
        try:
            process=supervisor.process
            if process is None or process.poll() is not None or str(supervisor.socket_path)!=row['history_socket']:raise ValueError()
            client=HistoryClient(row['history_socket'],actor_id=row['history_actor'],account_id=account,token=supervisor.credential_token(row['history_actor'],account),timeout=1.5)
            result=await bounded_history_io(lambda:client.regular_file_status_v1(row['restore_id'],row['native_resource_id'],row['native_source'],row['expected']))
            if original_current() is not True or supervisor.process is not process or process.poll() is not None or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:raise ValueError()
            if result['state'] in {'complete','conflict'} and (result['state']!='complete' or result['verified']) and type(row.get('claim')) is dict:
                store.settle_regular_file_restore(owner_subject_id=account,operation_id=request_id,request_digest=expected_digest,claim=row['claim'],state=result['state'],verified=result['verified'])
            return {'version':1,'state':result['state'],'request_id':request_id,'expected_digest':expected_digest,'resource_id':resource_id,'verified':result['verified'],'current_matches':result['current_matches']}
        except Exception:return {'version':1,'state':'unknown','request_id':request_id,'expected_digest':expected_digest}

async def confirm(*,resource_id,source,request_id,expected_digest,owner,account,auth,barrier,supervisor,store,context,provider,original_current):
    _selector(source)
    if not _ID.fullmatch(request_id or '') or not _DIGEST.fullmatch(expected_digest or ''):raise ValueError('restore request unavailable')
    existing=store.get_operation(owner_subject_id=account,operation_id=request_id)
    if existing is not None:
        return await status(resource_id=resource_id,source=source,request_id=request_id,expected_digest=expected_digest,owner=owner,account=account,auth=auth,barrier=barrier,supervisor=supervisor,store=store,original_current=original_current)
    process=supervisor.process
    async with barrier.admitted(owner):
        try:
            value=await _review(resource_id=resource_id,source=source,request_id=request_id,owner=owner,account=account,auth=auth,barrier=barrier,supervisor=supervisor,process=process,store=store,context=context,provider=provider,original_current=original_current)
            if value['digest']!=expected_digest:raise ValueError('restore review changed')
        except Exception:
            if not _current(owner,account,auth,barrier,supervisor,process,store,context.policy_generation,original_current,context.is_admin):return {'version':1,'state':'unknown','request_id':request_id,'expected_digest':expected_digest}
            refused={'kind':_KIND,'state':'refused','claim':None,'resource_id':resource_id,'source':source,'verified':False}
            previous=store.reserve_operation(owner_subject_id=account,operation_id=request_id,request_digest=expected_digest,generation=context.policy_generation,receipt=refused,phase=_KIND)
            if previous is not None:return await status(resource_id=resource_id,source=source,request_id=request_id,expected_digest=expected_digest,owner=owner,account=account,auth=auth,barrier=barrier,supervisor=supervisor,store=store,original_current=original_current)
            return {'version':1,'state':'refused','request_id':request_id,'expected_digest':expected_digest,'resource_id':resource_id,'verified':False}
        restore_id='regular-file-restore-'+_hash(['openclank.regular-file-restore.v1',account,request_id]);dest=value['dest'];native=value['native'];preview_value=value['preview'];client=value['client']
        row={'kind':_KIND,'state':'pending','claim':None,'resource_id':resource_id,'source':source,'verified':False,'restore_id':restore_id,'native_resource_id':dest['private_id'],'native_source':native,'expected':preview_value['expected'],'history_socket':str(client.socket_path),'history_actor':client.actor_id}
        previous=store.reserve_operation(owner_subject_id=account,operation_id=request_id,request_digest=expected_digest,generation=context.policy_generation,receipt=row,phase=_KIND)
        if previous is not None:return await status(resource_id=resource_id,source=source,request_id=request_id,expected_digest=expected_digest,owner=owner,account=account,auth=auth,barrier=barrier,supervisor=supervisor,store=store,original_current=original_current)
    def dispatch_current():return _current(owner,account,auth,barrier,supervisor,process,store,context.policy_generation,original_current,context.is_admin)
    async def destination():return await _destination(client=client,resource_id=resource_id,account=account,owner=owner,provider=provider,store=store,context=context)
    async def revalidate():
        fresh=await _review(resource_id=resource_id,source=source,request_id=request_id,owner=owner,account=account,auth=auth,barrier=barrier,supervisor=supervisor,process=process,store=store,context=context,provider=provider,original_current=original_current)
        return fresh['digest']==expected_digest
    def claim(workspace):return store.claim_regular_file_restore(owner_subject_id=account,operation_id=request_id,request_digest=expected_digest,generation=context.policy_generation,workspace_id=workspace,path=dest['path'])
    def refuse():store.refuse_unclaimed_regular_file_restore(owner_subject_id=account,operation_id=request_id,request_digest=expected_digest)
    def terminal(result):
        if type(result) is not dict or result.get('state') not in {'complete','conflict'} or type(result.get('verified')) is not bool or (result['state']=='complete' and not result['verified']):return None
        return {'terminal':True,'state':result['state'],'verified':result['verified']}
    def settle(claim_value,receipt):
        state=receipt['state'] if receipt is not None else 'refused';verified=receipt['verified'] if receipt is not None else False
        store.settle_regular_file_restore(owner_subject_id=account,operation_id=request_id,request_digest=expected_digest,claim=claim_value,state=state,verified=verified)
    try:
        await owned_host_restore(owner=owner,account_id=account,auth=auth,barrier=barrier,client=client,requested_id=resource_id,private_id=dest['private_id'],path=dest['path'],restore_id=restore_id,action_id=preview_value['source_action_id'],version_id=preview_value['source_version_id'],version_ref=None,
            call=lambda:client.execute_regular_file_v1(restore_id,dest['private_id'],native,preview_value['expected']),provider=provider,repository=store,context=context,generation=context.policy_generation,revalidate_source=revalidate,original_current=dispatch_current,claim_factory=claim,settle_claim=settle,refuse_unclaimed=refuse,destination_authorizer=destination,terminal_parser=terminal)
    except Exception:
        # The exact guarded transition succeeds only before a claim was ever
        # attached. An issued or uncertain owner retains its claim and row.
        try:store.refuse_unclaimed_regular_file_restore(owner_subject_id=account,operation_id=request_id,request_digest=expected_digest)
        except Exception:pass
    return await status(resource_id=resource_id,source=source,request_id=request_id,expected_digest=expected_digest,owner=owner,account=account,auth=auth,barrier=barrier,supervisor=supervisor,store=store,original_current=original_current)

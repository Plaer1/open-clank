"""Current Files authority for a bound plan artifact; no saved ref is a grant."""
import hashlib
import os
from pathlib import Path, PurePosixPath
from src.clanker_paths import canonical_plan_path
from src.openclank.files_facade import ProviderContext
from src.openclank.files_host_provider import _origin, _path
from src.openclank.files_principal import files_principal
from src.openclank.workspace_policy_service import resolve_owned_workspace
from src.openclank.plan_feedback import _file_target


async def capture_feedback_file(request, base, account, *, resource_ref=None, expected_revision=None, frozen=None, selection=None, return_ref=False):
    state=request.app.state; repository=state.files_policy_repository; auth=state.auth_manager
    actual,owner,admin=files_principal(request,repository=repository)
    binding=base['binding'];workspace=binding.get('authorityWorkspaceID')
    if actual!=account or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner or not workspace:raise ValueError('feedback authority unavailable')
    resolved=resolve_owned_workspace(repository,workspace_id=workspace,owner_username=owner,auth_manager=auth,purpose='app_folder')
    if resolved.workspace.id!=workspace or os.path.realpath(resolved.path)!=binding.get('physicalCwd'):raise ValueError('feedback workspace changed')
    ctx=ProviderContext(account,owner,repository.generation(),admin,workspace)
    facade=state.files_facade_factory(request,ctx);host=facade._providers.get('host')
    if host is None:raise ValueError('feedback Files unavailable')
    root=canonical_plan_path(base['artifact'],allow_legacy=True);root_path=PurePosixPath(root)
    if frozen is not None:
        _file_target(frozen);relative=frozen['relativePath'];path=os.path.join(resolved.path,*PurePosixPath(relative).parts)
        token=None
    else:
        if not isinstance(resource_ref,str) or not isinstance(expected_revision,dict):raise ValueError('feedback locator unavailable')
        provider,ref=facade._provider_for_ref(ctx,resource_ref,capability='download')
        if provider is not host or ref.provider!='host':raise ValueError('feedback locator unavailable')
        path=_path(ref.origin_id)
        relative=Path(path).relative_to(Path(resolved.path)).as_posix();token=resource_ref
    canonical=canonical_plan_path(relative,allow_legacy=False)
    slice_root=root_path.with_suffix('')
    if canonical!=root and (root_path.suffix!='.md' or PurePosixPath(canonical)==slice_root or not PurePosixPath(canonical).is_relative_to(slice_root)):raise ValueError('feedback target outside plan')
    if os.path.realpath(path)!=path:raise ValueError('feedback target alias unavailable')
    if frozen is not None:
        bounded=await host.stat_inspection(ctx,origin_id=_origin(path))
        if bounded.kind!='file' or bounded.origin_id!=_origin(path) or type(bounded.size) is not int or not 0<bounded.size<=262144:raise ValueError('feedback target unavailable')
        resource=await host.resource_for_path(ctx,path=path)
        token=facade._resource(ctx,'host',resource)['ref']
    provider,ref=facade._provider_for_ref(ctx,token,capability='download')
    if provider is not host or ref.origin_id!=_origin(path):raise ValueError('feedback target changed')
    meta=await host.stat_inspection(ctx,origin_id=ref.origin_id)
    if meta.kind!='file' or meta.origin_id!=ref.origin_id or type(meta.size) is not int or not 0<meta.size<=262144:raise ValueError('feedback target unavailable')
    before=await host.stat(ctx,origin_id=ref.origin_id);revision=facade._provider_revision(before)
    expected=frozen['revision'] if frozen is not None else expected_revision
    if before.size!=meta.size or revision!=expected:raise ValueError('feedback target changed')
    stream=await host.content(ctx,origin_id=ref.origin_id)
    if stream.origin_id!=ref.origin_id or stream.size!=meta.size or stream.etag!=meta.revision['value'] or stream.stream is None:raise ValueError('feedback target changed')
    chunks=[];total=0
    async for chunk in stream.stream(0,meta.size):
        if not isinstance(chunk,bytes):raise ValueError('feedback target unavailable')
        total+=len(chunk)
        if total>meta.size or total>262144:raise ValueError('feedback target unavailable')
        chunks.append(chunk)
    data=b''.join(chunks)
    if len(data)!=meta.size:raise ValueError('feedback target changed')
    digest=hashlib.sha256(data).hexdigest()
    if revision!={'kind':'hostFingerprint','value':f'sha256:{digest}:{len(data)}'}:raise ValueError('feedback fingerprint changed')
    data.decode('utf-8','strict')
    after=await host.stat_inspection(ctx,origin_id=ref.origin_id)
    if after.revision!=meta.revision or after.size!=meta.size or repository.generation()!=ctx.policy_generation or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:raise ValueError('feedback target changed')
    selected=frozen['selection'] if frozen is not None else selection
    if selected is not None:
        if type(selected) is not dict or set(selected)!={'start','end','sha256'} or type(selected['start']) is not int or type(selected['end']) is not int or not 0<=selected['start']<selected['end']<=len(data) or selected['end']-selected['start']>8192:raise ValueError('feedback selection unavailable')
        data[:selected['start']].decode('utf-8','strict');data[selected['start']:selected['end']].decode('utf-8','strict')
        if hashlib.sha256(data[selected['start']:selected['end']]).hexdigest()!=selected['sha256']:raise ValueError('feedback selection changed')
    value={'kind':'selection' if selected else 'file','workspace':workspace,'relativePath':canonical,'resourceID':ref.stable_id,'revision':dict(revision),'sha256':digest,'selection':selected}
    if frozen is not None and value!=frozen:raise ValueError('feedback target changed')
    result=_file_target(value)
    return (result,token) if return_ref else result


async def capture_feedback_resource(request, binding, account, *, resource_ref=None, expected_revision=None, anchor=None, frozen=None, return_ref=False, require_write=False, max_bytes=16*1024*1024, return_bytes=False, standard_source=False):
    """Reauthorize one exact resource; non-text anchor object existence is unverified."""
    from src.openclank.plan_feedback import _anchor
    if return_bytes and (return_ref or max_bytes>(65536 if standard_source is True else 16384)): raise ValueError('bounded data unavailable')
    state=request.app.state; repository=state.files_policy_repository; auth=state.auth_manager
    actual,owner,admin=files_principal(request,repository=repository); workspace=binding.get('authorityWorkspaceID')
    if actual!=account or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner or not workspace: raise ValueError('feedback authority unavailable')
    resolved=resolve_owned_workspace(repository,workspace_id=workspace,owner_username=owner,auth_manager=auth,purpose='app_folder')
    if resolved.workspace.id!=workspace or os.path.realpath(resolved.path)!=binding.get('physicalCwd'): raise ValueError('feedback workspace changed')
    ctx=ProviderContext(account,owner,repository.generation(),admin,workspace); facade=state.files_facade_factory(request,ctx); host=facade._providers.get('host')
    image_frozen=frozen is not None and frozen.get('resource',{}).get('provider')=='files'
    image_requested=frozen is None and isinstance(expected_revision,dict) and expected_revision.get('kind')=='imageSha256'
    if image_frozen or image_requested:
        if require_write or return_bytes or standard_source: raise ValueError('feedback image unavailable')
        images=facade._providers.get('files')
        if images is None: raise ValueError('feedback image unavailable')
        if image_frozen:
            saved=frozen['resource']; entry=await images.resolve_resource_key(ctx,resource_key={'provider':'files','account_id':account,'workspace_id':None,'resource_id':saved['originID']})
            token=facade._resource(ctx,'files',entry)['ref']; expected_revision=saved['revision']; anchor=frozen['anchor']
        else: token=resource_ref
        provider,ref=facade._provider_for_ref(ctx,token,capability='download')
        if provider is not images or ref.provider!='files': raise ValueError('feedback image unavailable')
        before=await images.stat(ctx,origin_id=ref.origin_id); revision=facade._provider_revision(before)
        if before.kind!='image' or revision!=expected_revision or type(before.size) is not int or not 0<before.size<=max_bytes: raise ValueError('feedback image changed')
        checked=_anchor(anchor,ref.stable_id)
        if checked['kind']!='image': raise ValueError('feedback image anchor unavailable')
        content=await images.content(ctx,origin_id=ref.origin_id)
        if content.origin_id!=ref.origin_id or content.size!=before.size or content.path is None or content.expected_identity is None: raise ValueError('feedback image changed')
        def hash_image():
            from routes.files_facade_routes import _open_path_content
            fd,opened=_open_path_content(content); digest=hashlib.sha256(); total=0
            try:
                while True:
                    chunk=os.read(fd,65536)
                    if not chunk: break
                    total+=len(chunk)
                    if total>before.size: raise ValueError('feedback image changed')
                    digest.update(chunk)
                final=os.fstat(fd)
                if (final.st_dev,final.st_ino,final.st_size,final.st_mtime_ns)!=(opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns): raise ValueError('feedback image changed')
                return total,digest.hexdigest()
            finally: os.close(fd)
        import asyncio
        issue=asyncio.create_task(asyncio.to_thread(hash_image))
        try:
            total,sha=await asyncio.shield(issue)
        except asyncio.CancelledError:
            while not issue.done():
                try: await asyncio.shield(issue)
                except asyncio.CancelledError: continue
                except Exception: break
            issue.result()
            raise
        after=await images.stat(ctx,origin_id=ref.origin_id)
        if total!=before.size or revision!={'kind':'imageSha256','value':'sha256:'+sha} or facade._provider_revision(after)!=revision or after.size!=before.size or repository.generation()!=ctx.policy_generation or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner: raise ValueError('feedback image changed')
        value={'resource':{'provider':'files','originID':ref.origin_id,'resourceID':ref.stable_id,'revision':dict(revision),'sha256':sha},'anchor':checked}
        if image_frozen and value!={'resource':frozen['resource'],'anchor':frozen['anchor']}: raise ValueError('feedback image changed')
        return (value,token) if return_ref else value
    if host is None: raise ValueError('feedback Files unavailable')
    if frozen is not None:
        relative=frozen['resource']['relativePath']; path=os.path.join(resolved.path,*PurePosixPath(relative).parts)
        if not PurePosixPath(relative).parts or PurePosixPath(relative).is_absolute() or '..' in PurePosixPath(relative).parts: raise ValueError('feedback target unavailable')
        resource=await host.resource_for_path(ctx,path=path); token=facade._resource(ctx,'host',resource)['ref']; expected_revision=frozen['resource']['revision']; anchor=frozen['anchor']
    else:
        if not isinstance(resource_ref,str) or not isinstance(expected_revision,dict): raise ValueError('feedback locator unavailable')
        token=resource_ref
    provider,ref=facade._provider_for_ref(ctx,token,capability='download')
    if provider is not host or ref.provider!='host': raise ValueError('feedback locator unavailable')
    path=_path(ref.origin_id)
    try: relative=Path(path).relative_to(Path(resolved.path)).as_posix()
    except ValueError: raise ValueError('feedback target outside workspace')
    if os.path.realpath(path)!=path: raise ValueError('feedback target alias unavailable')
    checked=_anchor(anchor,ref.stable_id)
    if checked['kind']=='dom' and checked['sourceID']!=ref.stable_id: raise ValueError('HTML source mapping unavailable')
    video_anchor=checked['kind'] in ('frame','time_range') and str(path).lower().endswith('.webm')
    verify_object=checked['kind'] in ('cell','slide') and (frozen is None or frozen.get('anchorVerification') == {'version':1,'kind':'source-object'})
    limit=262144 if checked['kind']=='dom' or verify_object else 64*1024*1024 if video_anchor and max_bytes==16*1024*1024 else max_bytes
    meta=await host.stat_inspection(ctx,origin_id=ref.origin_id)
    if meta.kind!='file' or meta.origin_id!=ref.origin_id or type(meta.size) is not int or not 0<=meta.size<=limit: raise ValueError('feedback target unavailable')
    before=await host.stat(ctx,origin_id=ref.origin_id); revision=facade._provider_revision(before)
    if before.size!=meta.size or revision!=expected_revision or (require_write and 'write' not in before.capabilities): raise ValueError('feedback target changed')
    stream=await host.content(ctx,origin_id=ref.origin_id)
    if stream.origin_id!=ref.origin_id or stream.size!=meta.size or stream.etag!=meta.revision['value'] or stream.stream is None: raise ValueError('feedback target changed')
    chunks=[];total=0; hasher=hashlib.sha256()
    async for chunk in stream.stream(0,meta.size):
        if not isinstance(chunk,bytes): raise ValueError('feedback target unavailable')
        total+=len(chunk)
        if total>meta.size: raise ValueError('feedback target changed')
        hasher.update(chunk)
        if checked['kind'] in ('text','dom') or verify_object or return_bytes: chunks.append(chunk)
    data=b''.join(chunks); digest=hasher.hexdigest()
    if total!=meta.size or revision!={'kind':'hostFingerprint','value':f'sha256:{digest}:{total}'}: raise ValueError('feedback fingerprint changed')
    if verify_object:
        text=data.decode('utf-8','strict')
        if checked['kind']=='cell':
            from src.openclank.collaboration_workbook import verify_workbook_range
            verify_workbook_range(text,checked['sheetID'],checked['range']['startCellID'],checked['range']['endCellID'])
        else:
            from src.openclank.collaboration_slides import verify_slide_object
            verify_slide_object(text,checked['deckID'],checked['slideID'],checked['objectID'])
    if checked['kind']=='dom':
        from src.openclank.html_feedback_source import verify_html_feedback_node
        if not str(path).lower().endswith(('.html','.htm')): raise ValueError('HTML source mapping unavailable')
        verify_html_feedback_node(data,checked['nodeID'])
    if checked['kind']=='text':
        start,end=checked['start'],checked['end']
        if end>len(data): raise ValueError('feedback selection changed')
        data.decode('utf-8','strict');data[:start].decode('utf-8','strict');data[start:end].decode('utf-8','strict')
        if hashlib.sha256(data[start:end]).hexdigest()!=checked['sha256']: raise ValueError('feedback selection changed')
    after=await host.stat_inspection(ctx,origin_id=ref.origin_id)
    if after.revision!=meta.revision or after.size!=meta.size or repository.generation()!=ctx.policy_generation or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner: raise ValueError('feedback target changed')
    value={'resource':{'workspace':workspace,'relativePath':relative,'resourceID':ref.stable_id,'revision':dict(revision),'sha256':digest},'anchor':checked,**({'anchorVerification':{'version':1,'kind':'source-object'}} if verify_object else {})}
    if frozen is not None and value!={'resource':frozen['resource'],'anchor':frozen['anchor'],**({'anchorVerification':frozen['anchorVerification']} if 'anchorVerification' in frozen else {})}: raise ValueError('feedback target changed')
    return (value,token) if return_ref else (value,data) if return_bytes else value


async def capture_design_folder(request,binding,account,value,*,frozen=False):
    """Pin an existing authorized same-workspace Host directory, never create."""
    state=request.app.state;repository=state.files_policy_repository;auth=state.auth_manager
    actual,owner,admin=files_principal(request,repository=repository);workspace=binding.get('authorityWorkspaceID')
    if actual!=account or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner or not workspace: raise ValueError('design folder unavailable')
    resolved=resolve_owned_workspace(repository,workspace_id=workspace,owner_username=owner,auth_manager=auth,purpose='app_folder')
    if resolved.workspace.id!=workspace or os.path.realpath(resolved.path)!=binding.get('physicalCwd'): raise ValueError('design workspace changed')
    ctx=ProviderContext(account,owner,repository.generation(),admin,workspace);facade=state.files_facade_factory(request,ctx);host=facade._providers.get('host')
    if host is None: raise ValueError('design folder unavailable')
    if frozen:
        if type(value) is not dict or set(value)!={'workspace','relativePath','resourceID','revision'} or value['workspace']!=workspace: raise ValueError('design folder unavailable')
        relative=value['relativePath'];parts=PurePosixPath(relative).parts
        if not isinstance(relative,str) or PurePosixPath(relative).is_absolute() or '..' in parts: raise ValueError('design folder unavailable')
        path=os.path.join(resolved.path,*parts)
        if os.path.realpath(path)!=path: raise ValueError('design folder unavailable')
        entry=await host.resource_for_path(ctx,path=path);token=facade._resource(ctx,'host',entry)['ref'];expected=value['revision']
    else:
        if type(value) is not dict or set(value)!={'resourceRef','expectedRevision'}: raise ValueError('design folder unavailable')
        token=value['resourceRef'];expected=value['expectedRevision']
    provider,ref=facade._provider_for_ref(ctx,token,capability='write')
    if provider is not host or ref.provider!='host': raise ValueError('design folder unavailable')
    path=_path(ref.origin_id)
    try: relative=Path(path).relative_to(Path(resolved.path)).as_posix()
    except ValueError: raise ValueError('design folder outside workspace')
    if os.path.realpath(path)!=path: raise ValueError('design folder unavailable')
    observed=await facade.capture_media_destination(ctx,resource_ref=token)
    if observed['destination_revision']!=expected or expected.get('kind')!='hostDirectoryRevision': raise ValueError('design folder changed')
    again=await facade.capture_media_destination(ctx,resource_ref=token)
    if (again['destination_revision']!=expected or repository.generation()!=ctx.policy_generation
        or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner): raise ValueError('design folder changed')
    result={'workspace':workspace,'relativePath':relative,'resourceID':ref.stable_id,'revision':dict(expected)}
    if frozen and result!=value: raise ValueError('design folder changed')
    return result


async def capture_design_context(request,binding,account,value,resource,*,frozen=False,return_standard=False):
    from src.openclank.plan_feedback import _design_context
    from src.openclank.deliverable_appearance import validate_deliverable_appearance
    base={'version','standard','appearance','assets','output'}
    if type(value) is not dict or type(value.get('version')) is not int or value['version'] not in (1,2): raise ValueError('design context unavailable')
    keys=base|({'standardSource'} if value['version']==2 else set())
    if set(value) not in (keys,keys|{'appearanceAssetIndex'}) or type(value['assets']) is not list or len(value['assets'])>4: raise ValueError('design context unavailable')
    appearance=validate_deliverable_appearance(value['appearance'])
    if value['standard']!=appearance['standard']: raise ValueError('design standard changed')
    index=value.get('appearanceAssetIndex')
    if 'appearanceAssetIndex' in value and (type(index) is not int or not 0<=index<len(value['assets'])): raise ValueError('appearance source unavailable')
    rows=[]; total=0
    for position,item in enumerate(value['assets']):
        is_appearance = index is not None and position == index
        if frozen:
            if type(item) is not dict: raise ValueError('design asset unavailable')
            result=await capture_feedback_resource(request,binding,account,frozen={'resource':item,'anchor':{'kind':'whole'}},max_bytes=16384 if is_appearance else 8*1024*1024,return_bytes=is_appearance)
        else:
            if type(item) is not dict or set(item)!={'resourceRef','expectedRevision'}: raise ValueError('design asset unavailable')
            result=await capture_feedback_resource(request,binding,account,resource_ref=item['resourceRef'],expected_revision=item['expectedRevision'],anchor={'kind':'whole'},max_bytes=16384 if is_appearance else 8*1024*1024,return_bytes=is_appearance)
        if is_appearance:
            result,raw=result
            try: imported=validate_deliverable_appearance(raw.decode('utf-8','strict'))
            except Exception as exc: raise ValueError('appearance source unavailable') from exc
            if imported!=appearance: raise ValueError('appearance source changed')
        row=result['resource']; total+=int(row['revision']['value'].rsplit(':',1)[-1]); rows.append(row)
        if total>32*1024*1024: raise ValueError('design assets too large')
    output=value['output']
    if type(output) is dict and output.get('destination')=='new-file':
        if set(output)!={'format','destination','folder','name','collision'}: raise ValueError('design destination unavailable')
        output={**output,'folder':await capture_design_folder(request,binding,account,output['folder'],frozen=frozen)}
    standard=None; standard_source=None
    if value['version']==2:
        from src.openclank.design_standard import validate_design_standard
        selected=value['standardSource']
        if frozen:
            if type(selected) is not dict or set(selected)!={'resource','id','version','sha256'}: raise ValueError('standard source unavailable')
            result,raw=await capture_feedback_resource(request,binding,account,frozen={'resource':selected['resource'],'anchor':{'kind':'whole'}},max_bytes=65536,return_bytes=True,standard_source=True)
        else:
            if type(selected) is not dict or set(selected)!={'resourceRef','expectedRevision'}: raise ValueError('standard source unavailable')
            result,raw=await capture_feedback_resource(request,binding,account,resource_ref=selected['resourceRef'],expected_revision=selected['expectedRevision'],anchor={'kind':'whole'},max_bytes=65536,return_bytes=True,standard_source=True)
        standard,digest=validate_design_standard(raw)
        standard_source={'resource':result['resource'],'id':standard['id'],'version':standard['version'],'sha256':digest}
        if frozen and standard_source!=selected: raise ValueError('design standard changed')
    checked=_design_context({'version':value['version'],'standard':value['standard'],'appearance':appearance,'assets':rows,'output':output,**({'appearanceAssetIndex':index} if index is not None else {}),**({'standardSource':standard_source} if standard_source is not None else {})},resource)
    if frozen and checked!=value: raise ValueError('design context changed')
    return (checked,standard) if return_standard else checked


async def compare_design_input(request, binding, account, saved):
    """Report a revision only after current Files authority and exact byte read."""
    try:
        state=request.app.state; repository=state.files_policy_repository; auth=state.auth_manager
        actual,owner,admin=files_principal(request,repository=repository); workspace=binding.get('authorityWorkspaceID')
        if actual!=account or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner or not workspace: raise ValueError('scope unavailable')
        resolved=resolve_owned_workspace(repository,workspace_id=workspace,owner_username=owner,auth_manager=auth,purpose='app_folder')
        if resolved.workspace.id!=workspace or os.path.realpath(resolved.path)!=binding.get('physicalCwd'): raise ValueError('scope changed')
        relative=saved['relativePath']; parts=PurePosixPath(relative).parts
        if not parts or PurePosixPath(relative).is_absolute() or '..' in parts: raise ValueError('resource unavailable')
        path=os.path.join(resolved.path,*parts)
        if os.path.realpath(path)!=path: raise ValueError('resource unavailable')
        ctx=ProviderContext(account,owner,repository.generation(),admin,workspace);facade=state.files_facade_factory(request,ctx);host=facade._providers.get('host')
        if host is None: raise ValueError('resource unavailable')
        item=await host.resource_for_path(ctx,path=path); token=facade._resource(ctx,'host',item)['ref']
        provider,ref=facade._provider_for_ref(ctx,token,capability='download')
        if provider is not host or ref.stable_id!=saved['resourceID']: raise ValueError('identity changed')
        observed=await host.stat(ctx,origin_id=ref.origin_id);revision=facade._provider_revision(observed)
        checked=await capture_feedback_resource(request,binding,account,resource_ref=token,expected_revision=revision,anchor={'kind':'whole'},max_bytes=8*1024*1024)
        if checked['resource']['resourceID']!=saved['resourceID'] or repository.generation()!=ctx.policy_generation: raise ValueError('identity changed')
        return {'state':'current' if checked['resource']['revision']==saved['revision'] and checked['resource']['sha256']==saved['sha256'] else 'changed',
                'expectedRevision':saved['revision'],'observedRevision':checked['resource']['revision']}
    except Exception:
        return {'state':'unavailable'}

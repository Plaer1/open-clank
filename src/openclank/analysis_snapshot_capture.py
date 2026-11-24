"""Current Files-authorized bounded analysis snapshot capture and preview."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
import re
import time
from typing import Any
from src.openclank.analysis_snapshot_contract import (SCHEMA_VERSION, HOST_SCHEMA_VERSION, DEFAULT_FILE_BYTES, DEFAULT_TOTAL_BYTES,
    MAX_FILE_BYTES, MAX_TOTAL_BYTES, MAX_COMPONENTS, MAX_DEPTH, MAX_MANIFEST_BYTES, MAX_SECONDS, _path, directory_digest, scope_digest,
    validate_manifest, validate_reference, encode_manifest)
from src.openclank.files_host_provider import _origin, _path as host_path
from src.openclank.resource_refs import stable_resource_id

DEFAULT_LIMITS={'nodes':MAX_COMPONENTS,'depth':MAX_DEPTH,'file_bytes':DEFAULT_FILE_BYTES,
    'total_bytes':DEFAULT_TOTAL_BYTES,'manifest_bytes':MAX_MANIFEST_BYTES,'seconds':MAX_SECONDS}


def _allowed_path(project, repository, path):
    workspace_path=os.path.normpath(str(project.binding.path));rel=os.path.relpath(path,workspace_path)
    if rel=='.':rel=''
    if rel.startswith('..'+os.sep) or rel=='..' or os.path.isabs(rel):return False
    relative='/'.join(filter(None,(project.binding.workspace.relative_folder,rel.replace(os.sep,'/'))))
    ctx=project.context
    decision=repository.resolve(subject_id=ctx.owner_subject_id,is_admin=ctx.is_admin,origin='app',location_id=project.binding.workspace.location_id,
        capability='read',workspace_id=project.identity['workspaceID'],relative_resource=relative,observation_only=True)
    return decision.allowed and decision.policy_generation==ctx.policy_generation


async def revalidate_snapshot_root(*, project, repository, root_ref, root_revision, manifest, current=None):
    """Fresh folder ref is a selector; current Files policy supplies authority."""
    validate_manifest(manifest);await project._check()
    if current is not None and not await current():raise ValueError('snapshot authority changed')
    ctx=project.context;provider,root=project.facade._provider_for_ref(ctx,root_ref,capability='children')
    if (provider is not project.host or root.provider!='host' or root.stable_id!=manifest['scope']['root_resource_id']
        or ctx.owner_subject_id!=manifest['account_id'] or project.identity['workspaceID']!=manifest['workspace_id']
        or project.identity['projectKey']!=manifest['project_key']):raise ValueError('snapshot root unavailable')
    path=host_path(root.origin_id)
    if not _allowed_path(project,repository,path):raise ValueError('snapshot root unavailable')
    entry=await project.host.stat(ctx,origin_id=root.origin_id)
    if entry.origin_id!=root.origin_id or entry.kind!='folder' or dict(entry.revision or {})!=root_revision:raise ValueError('snapshot root changed')
    view=await project.host.strict_snapshot_directory(ctx,origin_id=root.origin_id,limit=1)
    final=await project.host.stat(ctx,origin_id=root.origin_id)
    await project._check()
    if (final.origin_id!=root.origin_id or final.kind!='folder' or dict(final.revision or {})!=root_revision
        or not _allowed_path(project,repository,path) or 'dir-'+view['directory_id']!=manifest['root_identity']
        or current is not None and not await current()):raise ValueError('snapshot root changed')


def _preview_current(authority):
    if authority is None or authority.admission.released or authority.settled or authority.admission.utility is not authority or not authority.account_current():
        raise ValueError('snapshot preview authority unavailable')


def seal_snapshot_preview(authority, reference, manifest):
    from src.secret_storage import encrypt
    _preview_current(authority);validate_manifest(manifest);validate_reference(reference,encode_manifest(manifest))
    if manifest['account_id']!=authority.account:raise ValueError('snapshot account changed')
    now=int(time.time())
    data={'v':1,'account':authority.account,'workspace':manifest['workspace_id'],'project':manifest['project_key'],
        'root':manifest['root_identity'],'interval':manifest['capture_interval'],'reference':reference,'issued':now,'expires':now+86400}
    value=encrypt(json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':')))
    if not value.startswith('enc:'):raise ValueError('snapshot preview unavailable')
    return 'ma4p1.'+value[4:]


def open_snapshot_preview(authority, token):
    from cryptography.fernet import Fernet, InvalidToken
    from src import secret_storage
    _preview_current(authority)
    if not isinstance(token,str) or not token.startswith('ma4p1.') or len(token)>16384:raise ValueError('snapshot preview unavailable')
    try:
        value=json.loads(Fernet(secret_storage._KEY_PATH.read_bytes()).decrypt(token[6:].encode('ascii')).decode('utf-8'))
        if (type(value) is not dict or set(value)!={'v','account','workspace','project','root','interval','reference','issued','expires'}
            or value['v']!=1 or value['account']!=authority.account or type(value['issued']) is not int or type(value['expires']) is not int
            or value['issued']>int(time.time()) or value['expires']<=int(time.time()) or value['expires']-value['issued']>86400):raise ValueError('invalid preview')
        validate_reference(value['reference'])
        for key in ('workspace','project','root'):
            if not isinstance(value[key],str) or not value[key] or len(value[key].encode('utf-8','strict'))>512:raise ValueError('invalid preview')
        if type(value['interval']) is not dict or set(value['interval'])!={'started_ms','finished_ms'}:raise ValueError('invalid preview')
        return value
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,InvalidToken) as exc:raise ValueError('snapshot preview unavailable') from exc


async def capture_snapshot(*, project, repository, root_ref: str, root_revision: dict, mode: str,
                           selected_refs: list[str], dossier_id: str, dossier_revision: dict,
                           source_ref: str, limits: dict | None=None, current=None, historical_manifest=None, observations=None):
    """Capture an interval, returning data only; caller owns admission and retention."""
    limits=dict(DEFAULT_LIMITS if limits is None else limits)
    maxima={**DEFAULT_LIMITS,'file_bytes':MAX_FILE_BYTES,'total_bytes':MAX_TOTAL_BYTES}
    if set(limits)!=set(DEFAULT_LIMITS) or any(type(limits[k]) is not int or not 1<=limits[k]<=maxima[k] for k in limits):raise ValueError('snapshot limits unavailable')
    if mode not in {'folder','selected'} or type(selected_refs) is not list or len(selected_refs)>limits['nodes'] or (mode=='folder' and selected_refs) or (mode=='selected' and not selected_refs and historical_manifest is None):raise ValueError('snapshot selection unavailable')
    ctx=project.context;host=project.host;facade=project.facade;start=int(time.time()*1000);deadline=time.monotonic()+limits['seconds']
    async def check():
        if time.monotonic()>=deadline or (current is not None and not await current()):raise ValueError('snapshot authority or deadline changed')
        await project._check()
    await check();provider,root=facade._provider_for_ref(ctx,root_ref,capability='children')
    if provider is not host or root.provider!='host':raise ValueError('snapshot root unavailable')
    root_path=host_path(root.origin_id);workspace_path=os.path.normpath(str(project.binding.path))
    if os.path.commonpath((workspace_path,root_path))!=workspace_path:raise ValueError('snapshot root outside workspace')
    entry=await host.stat(ctx,origin_id=root.origin_id)
    if entry.origin_id!=root.origin_id or entry.kind!='folder' or dict(entry.revision or {})!=root_revision:raise ValueError('snapshot root changed')
    if historical_manifest is None:
        dossier=await project.read_provider_dossier(dossier_id,source_ref,include_result_bytes=True)
        if dossier.get('revision')!=dossier_revision or dossier.get('sourceState')!='current':raise ValueError('snapshot dossier changed')
        facts=dossier['resultIdentity']
        result={'encoding':'base64','size_bytes':len(dossier['_result_bytes']),'sha256':facts['resultSHA256'],
                'data':base64.b64encode(dossier['_result_bytes']).decode('ascii')}
        dfacts={'dossier_id':dossier_id,'dossier_revision':dossier_revision,'source_id':facts['sourceID'],'source_revision':facts['sourceRevision'],
            'source_sha256':facts['sourceSHA256'],'result_sha256':facts['resultSHA256'],'operation':facts['operation'],'result':result,'interpretations':[]}
        if facts.get('producer')=='openclank.elf-metadata':
            dfacts.update(producer=facts['producer'],producer_version=facts['producerVersion'],producer_evidence=facts['producerEvidence'],
                          coverage=facts['coverage'],payload_integrity=facts['payloadIntegrity'])
        else:
            dfacts.update(provider=facts['provider'],provider_sha256=facts['providerSHA256'],provider_version=facts['providerVersion'],
                          provider_evidence=facts['executableEvidence'],version_evidence=facts['versionEvidence'])
    else:
        validate_manifest(historical_manifest)
        if (historical_manifest['account_id']!=ctx.owner_subject_id or historical_manifest['workspace_id']!=project.identity['workspaceID']
            or historical_manifest['project_key']!=project.identity['projectKey'] or historical_manifest['scope']['root_resource_id']!=root.stable_id
            or historical_manifest['scope']['mode']!=mode or historical_manifest['scope']['filters']):raise ValueError('snapshot scope changed')
        dfacts=historical_manifest['dossier'];facts=None
    def rel_policy(path):return _allowed_path(project,repository,path)
    if not rel_policy(root_path):raise ValueError('snapshot root denied')
    selected=[]
    for ref_token in selected_refs:
        _,ref=facade._provider_for_ref(ctx,ref_token,capability='download')
        if ref.provider!='host':raise ValueError('snapshot selection unavailable')
        child=host_path(ref.origin_id)
        if os.path.commonpath((root_path,child))!=root_path or child==root_path or not rel_policy(child):raise ValueError('snapshot selection unavailable')
        parts=os.path.relpath(child,root_path).split(os.sep);_path(parts);selected.append(tuple(parts))
    if historical_manifest is not None and mode=='selected':
        if selected_refs:raise ValueError('snapshot scope changed')
        selected=[tuple(p) for p in historical_manifest['scope']['selections']]
    selected.sort(key=lambda p:tuple(v.encode('utf-8') for v in p))
    if len(set(selected))!=len(selected):raise ValueError('duplicate snapshot selection')
    scope={'mode':mode,'root_resource_id':root.stable_id,'selections':[list(p) for p in selected] if mode=='selected' else [[]],
        'filters':[],'policy_generation':ctx.policy_generation};scope_sha=scope_digest(scope)
    components=[];total=0;attempts=0;root_baseline=None
    known_paths={tuple(n['path']) for n in historical_manifest['components']} if historical_manifest is not None else set()
    if observations is not None and type(observations) is not dict:raise ValueError('invalid observations')
    def unavailable(parts,reason):
        if observations is not None and parts in known_paths:observations[parts]=reason
    async def hash_file(path,parts):
        nonlocal total,attempts
        attempts+=1;await check()
        if not rel_policy(path):raise PermissionError('denied')
        before=await host.stat_inspection(ctx,origin_id=_origin(path));rev=facade._provider_revision(before)
        if before.origin_id!=_origin(path) or before.kind!='file' or type(before.size) is not int or before.size>limits['file_bytes'] or before.size>limits['total_bytes']-total or not rev or rev.get('kind')!='hostObjectIdentity':raise ValueError('file unavailable')
        remaining=max(1,int((deadline-time.monotonic())*1000))
        result=await host.snapshot_file_hash(ctx,origin_id=_origin(path),max_bytes=max(1,min(limits['file_bytes'],limits['total_bytes']-total)),max_ms=min(30000,remaining))
        after=await host.stat_inspection(ctx,origin_id=_origin(path));await check()
        if not rel_policy(path) or after.origin_id!=before.origin_id or after.size!=before.size or facade._provider_revision(after)!=rev or result['object_tag']!=rev['value'] or result['size']!=before.size:raise ValueError('file changed')
        total+=result['size'];node={'path':list(parts),'kind':'file','resource_id':stable_resource_id(owner_subject_id=ctx.owner_subject_id,provider='host',origin_id=_origin(path)),
            'revision':{'kind':'hostFingerprint','value':f"sha256:{result['sha256']}:{result['size']}"},'size_bytes':result['size'],'sha256':result['sha256']}
        components.append(node);return node
    async def walk(path,parts):
        nonlocal attempts,root_baseline
        await check();attempts+=1
        if not rel_policy(path):raise PermissionError('denied')
        available=limits['nodes']-attempts
        first=await host.strict_snapshot_directory(ctx,origin_id=_origin(path),limit=max(1,available))
        if not parts:
            if not first.get('directory_id'):raise ValueError('snapshot root unavailable')
            root_baseline=first
        rows=[];reasons=set(first['reasons']);omitted=first['omitted_count']
        for raw in first['entries']:
            if type(raw) is not dict or set(raw)!={'name','kind','size'} or raw['kind'] not in {'file','directory'}:raise ValueError('invalid listing')
            name=raw['name'];_path([name]);child_parts=parts+(name,);child_path=os.path.join(path,name)
            if len(child_parts)>limits['depth'] or attempts>=limits['nodes']:
                reasons.add('overflow');omitted+=1;continue
            try:
                node=await walk(child_path,child_parts) if raw['kind']=='directory' else await hash_file(child_path,child_parts)
                rows.append(node)
            except PermissionError:reasons.add('denied');omitted+=1;unavailable(child_parts,'denied')
            except Exception:
                await check();reasons.add('unavailable');omitted+=1;unavailable(child_parts,'unreadable')
        if first.get('directory_id'):
            after=await host.strict_snapshot_directory(ctx,origin_id=_origin(path),limit=max(1,min(limits['nodes'],available)))
            await check()
            if not rel_policy(path) or any(first[k]!=after[k] for k in ('directory_id','object_tag','entries','complete','omitted_count','reasons')):reasons.add('changed')
        coverage='complete' if not reasons and all(n.get('coverage','complete')=='complete' for n in rows) else 'partial'
        direct=[{'name':n['path'][-1],'kind':n['kind'],'coverage':n.get('coverage'),'size_bytes':n.get('size_bytes'),'sha256':n['sha256']} for n in rows]
        node={'path':list(parts),'kind':'directory','coverage':coverage,'child_count':len(rows),'omitted_count':min(omitted,MAX_COMPONENTS),'reasons':sorted(reasons),
            'sha256':directory_digest(scope_sha256=scope_sha,coverage=coverage,omitted_count=min(omitted,MAX_COMPONENTS),reasons=sorted(reasons),children=direct)}
        components.append(node);return node
    if mode=='selected':
        directories={()}
        for parts in selected:
            for index in range(1,len(parts)):directories.add(parts[:index])
        if len(directories)+len(selected)>limits['nodes']:raise ValueError('snapshot selection exceeds node bound')
        observed={}
        for parts in sorted(directories,key=lambda p:(len(p),p)):
            path=os.path.join(root_path,*parts);await check()
            if parts and parts[:-1] not in observed:
                if observations is not None and observations.get(parts[:-1])=='removed':observations[parts]='removed'
                continue
            if not rel_policy(path):
                if historical_manifest is None:raise ValueError('snapshot directory unavailable')
                unavailable(parts,'denied');continue
            try:observed[parts]=await host.strict_snapshot_directory(ctx,origin_id=_origin(path),limit=1)
            except Exception:
                await check()
                if historical_manifest is None or not parts:raise
                parent=await host.strict_snapshot_directory(ctx,origin_id=_origin(os.path.dirname(path)),limit=limits['nodes'])
                if parent['complete'] and not any(row['name']==parts[-1] for row in parent['entries']):
                    if observations is not None:observations[parts]='removed'
                elif any(row['name']==parts[-1] for row in parent['entries']):unavailable(parts,'unreadable')
        root_baseline=observed[()]
        for parts in selected:
            path=os.path.join(root_path,*parts)
            if parts[:-1] not in observed:
                if observations is not None and observations.get(parts[:-1])=='removed':observations[parts]='removed'
                continue
            if historical_manifest is None:
                await hash_file(path,parts);continue
            if not rel_policy(path):unavailable(parts,'denied');continue
            try:await hash_file(path,parts)
            except Exception:
                await check()
                parent=await host.strict_snapshot_directory(ctx,origin_id=_origin(os.path.dirname(path)),limit=limits['nodes'])
                if parent['complete'] and not any(row['name']==parts[-1] for row in parent['entries']):
                    if observations is not None:observations[parts]='removed'
                elif any(row['name']==parts[-1] for row in parent['entries']):unavailable(parts,'unreadable')
        for parts in sorted(directories,key=lambda p:(-len(p),p)):
            if parts not in observed:continue
            path=os.path.join(root_path,*parts);after=await host.strict_snapshot_directory(ctx,origin_id=_origin(path),limit=1);await check()
            if not rel_policy(path) or any(observed[parts][key]!=after[key] for key in ('directory_id','object_tag','entries','complete','omitted_count','reasons')):raise ValueError('snapshot directory changed')
            direct=[{'name':n['path'][-1],'kind':n['kind'],'coverage':n.get('coverage'),'size_bytes':n.get('size_bytes'),'sha256':n['sha256']} for n in components if tuple(n['path'][:-1])==parts and n['path']]
            components.append({'path':list(parts),'kind':'directory','coverage':'selected','child_count':len(direct),'omitted_count':0,'reasons':[],
                'sha256':directory_digest(scope_sha256=scope_sha,coverage='selected',omitted_count=0,reasons=[],children=direct)})
    else:
        await walk(root_path,())
        if observations is not None:
            captured={tuple(node['path']) for node in components}
            for parts in known_paths-captured:
                if parts and not rel_policy(os.path.join(root_path,*parts)):unavailable(parts,'denied')
    root_now=await host.stat(ctx,origin_id=root.origin_id);await check()
    if root_now.origin_id!=root.origin_id or not rel_policy(root_path):raise ValueError('snapshot root changed')
    if historical_manifest is None:
        second=await project.read_provider_dossier(dossier_id,source_ref);await check()
        if second['revision']!=dossier_revision or second['sourceState']!='current' or second['resultIdentity']!=facts:raise ValueError('snapshot dossier changed')
    # The path-free directory identity is observed from the same native authority.
    root_scan=await host.strict_snapshot_directory(ctx,origin_id=root.origin_id,limit=1);await check()
    if not root_baseline or root_scan['directory_id']!=root_baseline['directory_id'] or root_scan['object_tag']!=root_baseline['object_tag']:raise ValueError('snapshot root changed')
    if historical_manifest is not None and historical_manifest['root_identity']!='dir-'+root_scan['directory_id']:raise ValueError('snapshot root identity changed')
    manifest={'schema_version':(historical_manifest['schema_version'] if historical_manifest is not None else HOST_SCHEMA_VERSION if dfacts.get('producer')=='openclank.elf-metadata' else SCHEMA_VERSION),'account_id':ctx.owner_subject_id,'workspace_id':project.identity['workspaceID'],
        'project_key':project.identity['projectKey'],'root_identity':'dir-'+root_scan['directory_id'],'capture_interval':{'started_ms':start,'finished_ms':int(time.time()*1000)},
        'scope':scope,'limits':limits,'dossier':dfacts,'components':sorted(components,key=lambda n:tuple(v.encode('utf-8') for v in n['path']))}
    return validate_manifest(manifest)


def compare_snapshots(previous: dict, observed: dict, *, observations=None) -> dict:
    """Compare captured intervals; unknown coverage never proves absence."""
    validate_manifest(previous);validate_manifest(observed)
    if (previous['account_id']!=observed['account_id'] or previous['workspace_id']!=observed['workspace_id']
        or previous['project_key']!=observed['project_key'] or previous['root_identity']!=observed['root_identity']
        or scope_digest(previous['scope'])!=scope_digest(observed['scope'])):raise ValueError('snapshot scope changed')
    old={tuple(n['path']):n for n in previous['components']};new={tuple(n['path']):n for n in observed['components']}
    def known_absent(path, nodes):
        if not path:return False
        for index in range(len(path)-1,-1,-1):
            parent=nodes.get(path[:index])
            if parent is not None:return parent['kind']=='directory' and parent['coverage']=='complete'
        return False
    rows=[]
    for path in sorted(set(old)|set(new),key=lambda p:tuple(part.encode('utf-8') for part in p)):
        a,b=old.get(path),new.get(path)
        if a is None:state='added' if known_absent(path,old) else 'unknown'
        elif b is None:
            fact=observations.get(path) if observations else None
            state='unavailable' if fact in {'denied','unreadable'} else ('removed' if fact=='removed' or known_absent(path,new) else 'unknown')
        elif a['kind']!=b['kind']:state='changed'
        elif a['kind']=='file':state='unchanged' if a['sha256']==b['sha256'] and a['size_bytes']==b['size_bytes'] else 'changed'
        elif a['coverage']!='complete' or b['coverage']!='complete':state='unknown'
        else:state='unchanged' if a['sha256']==b['sha256'] else 'changed'
        row={'path':list(path),'kind':(b or a)['kind'],'state':state}
        if state=='unavailable':row['reason']=observations[path]
        if a and b and a['kind']==b['kind']=='file':row['identity_changed']=a['resource_id']!=b['resource_id']
        rows.append(row)
    return {'version':1,'coverage_before':previous['components'][0]['coverage'],
        'coverage_after':observed['components'][0]['coverage'],'interval_before':previous['capture_interval'],
        'interval_after':observed['capture_interval'],'components':rows}

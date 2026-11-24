"""Original named-human route for one recorded file's current Graph impact."""
from __future__ import annotations

import asyncio
import copy
import json
import time

from fastapi.responses import JSONResponse

from src.auth_helpers import effective_user, is_internal_tool_identity, require_user
from src.openclank.account_request_barrier import _current_admission
from src.openclank.files_facade import ProviderContext

_HEADERS = {'Cache-Control': 'private, no-store'}


def _failure(status):
    from src.openclank.recorded_code_impact_contract import unavailable_result
    return JSONResponse(unavailable_result(), status_code=status, headers=_HEADERS)


def _capture(request, memory_provider):
    owner = require_user(request)
    state = request.app.state
    if (not owner or is_internal_tool_identity(owner) or getattr(request.state, 'authenticated', False) is not True
            or getattr(request.state, 'api_token', False) or getattr(request.state, 'internal_tool_authenticated', False)
            or (effective_user(request) or owner) != owner):
        raise ValueError('Named browser required')
    auth = getattr(state, 'auth_manager', None)
    barrier = getattr(state, 'account_request_barrier', None)
    credential_factory = getattr(state, 'capture_original_credential', None)
    repository = getattr(state, 'files_policy_repository', None)
    facade_factory = getattr(state, 'files_facade_factory', None)
    supervisor = getattr(state, 'history_supervisor', None)
    process = getattr(supervisor, 'process', None)
    admission = _current_admission.get()
    if (auth is None or barrier is None or not callable(credential_factory) or repository is None
            or not callable(facade_factory) or supervisor is None or process is None or process.poll() is not None
            or memory_provider is None):
        raise ValueError('Authority unavailable')
    account = auth.account_id(owner)
    if not account or auth.username_for_account_id(account) != owner: raise ValueError('Account unavailable')
    credential = credential_factory(request, owner, account)
    if not callable(credential): raise ValueError('Credential unavailable')
    privileges = copy.deepcopy(auth.get_privileges(owner))
    if type(privileges) is not dict: raise ValueError('Privilege unavailable')
    admin = auth.is_admin(owner)
    if type(admin) is not bool: raise ValueError('Privilege unavailable')
    generation = repository.generation()
    client = supervisor.client_for(owner, account)
    deadline = time.monotonic() + 30
    def current():
        try:
            barrier.assert_live_admission(owner, admission)
            return bool(time.monotonic() < deadline and credential() is True
                and getattr(state,'memory_provider',None) is memory_provider
                and getattr(state,'auth_manager',None) is auth
                and getattr(state,'account_request_barrier',None) is barrier
                and getattr(state,'capture_original_credential',None) is credential_factory
                and getattr(state,'files_policy_repository',None) is repository and repository.generation() == generation
                and getattr(state,'files_facade_factory',None) is facade_factory
                and getattr(state,'history_supervisor',None) is supervisor
                and supervisor.process is process and process.poll() is None
                and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                and auth.get_privileges(owner) == privileges and auth.is_admin(owner) is admin
                and not getattr(request.state,'api_token',False) and not getattr(request.state,'internal_tool_authenticated',False)
                and (effective_user(request) or owner) == owner)
        except Exception:
            return False
    if not current(): raise ValueError('Authority unavailable')
    return owner, account, auth, repository, facade_factory, client, generation, deadline, current


async def recorded_impact(request, memory_provider):
    try:
        owner, account, auth, repository, factory, client, generation, deadline, current = _capture(request, memory_provider)
        payload = bytearray()
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            async for chunk in request.stream():
                if len(payload) + len(chunk) > 16384: return _failure(413)
                payload.extend(chunk)
        if not current(): return _failure(409)
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result: raise ValueError('Duplicate key')
                result[key] = value
            return result
        body = json.loads(payload, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON')))
        from src.openclank.recorded_code_impact_contract import validate_request
        from src.openclank.recorded_code_impact import recorded_code_impact
        body = validate_request(body)
        context = ProviderContext(account, owner, generation, False, body['workspace_id'])
        facade = factory(request, context)
        files_provider = facade._providers.get('host')
        if files_provider is None or getattr(files_provider, 'operation_store', None) is not repository or not current():
            return _failure(409)
        result = await recorded_code_impact(repository=repository, provider=memory_provider, auth_manager=auth,
            owner=owner, account=account, body=body, history_client=client, history_context=context,
            files_provider=files_provider, current=current, deadline=deadline)
        if not current(): return _failure(409)
        response = JSONResponse(result, headers=_HEADERS)
        if not current(): return _failure(409)
        return response
    except Exception:
        return _failure(409)


async def cross_project_manifest(request, memory_provider):
    """Compare two separately authorized Human workspaces without exporting specs."""
    import hashlib
    import os
    import re
    from pathlib import PurePosixPath
    from src.openclank.code_query import MAX_QUERY_WORK, query_code, resolve_query_binding, validate_query
    def unavailable():
        return JSONResponse({'schema':'open-clank.code-cross-project/v1','state':'unavailable','partial':True,
            'source':None,'target':None,'relation':None,'reasons':['authority_or_source_unavailable']},status_code=409,headers=_HEADERS)
    try:
        owner, account, auth, repository, _, _, _, deadline, current = _capture(request,memory_provider)
        payload=bytearray()
        async with asyncio.timeout(max(0,deadline-time.monotonic())):
            async for chunk in request.stream():
                if len(payload)+len(chunk)>16384:return unavailable()
                payload.extend(chunk)
        def pairs(items):
            value={}
            for key,item in items:
                if key in value: raise ValueError('duplicate key')
                value[key]=item
            return value
        body=json.loads(payload,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if isinstance(body, dict) and type(body.get("version")) is int and body["version"] == 2:
            from src.openclank.code_compiler_evidence import cross_workspace_compiler_import, unavailable_cross_workspace
            if set(body) != {"version", "kind", "source", "target"} or body.get("kind") != "python_compiler_import":
                return JSONResponse(unavailable_cross_workspace(), status_code=409, headers=_HEADERS)
            try:
                async with asyncio.timeout(max(0, deadline - time.monotonic())):
                    result = await cross_workspace_compiler_import(
                        owner=owner, repository=repository, auth_manager=auth, provider=memory_provider,
                        source=body["source"], target=body["target"], current=current,
                    )
                if not current():
                    result = unavailable_cross_workspace()
            except Exception:
                result = unavailable_cross_workspace()
            return JSONResponse(result, status_code=409 if result["state"] == "unavailable" else 200, headers=_HEADERS)
        if type(body) is not dict or set(body)!={'version','source','target'} or body['version']!=1 or type(body['version']) is not int:return unavailable()
        source,target=body['source'],body['target']
        if type(source) is not dict or set(source)!={'workspace_id','file_id','receipt'} or type(target) is not dict or set(target)!={'workspace_id','receipt'}:return unavailable()
        sw,tw=source['workspace_id'],target['workspace_id']
        if any(type(v) is not str or not v or len(v.encode())>256 for v in (sw,tw)) or sw==tw or type(source['file_id']) is not str or re.fullmatch(r'file:[0-9a-f]{64}',source['file_id']) is None:return unavailable()
        for workspace,item in ((sw,source),(tw,target)):
            validate_query({'operation':'capabilities','fields':[],'channels':[],'expected_receipt':item['receipt']})
            if type(item['receipt']) is not dict or item['receipt'].get('workspace_id')!=workspace:return unavailable()
        sb,si=resolve_query_binding(repository=repository,auth_manager=auth,owner=owner,workspace_id=sw,purpose='app_folder')
        tb,ti=resolve_query_binding(repository=repository,auth_manager=auth,owner=owner,workspace_id=tw,purpose='app_folder')
        if os.path.commonpath((sb['root'],tb['root'])) in (sb['root'],tb['root']):return unavailable()
        def live():
            try:
                return current() is True and resolve_query_binding(repository=repository,auth_manager=auth,owner=owner,workspace_id=sw,purpose='app_folder')[1]==si and resolve_query_binding(repository=repository,auth_manager=auth,owner=owner,workspace_id=tw,purpose='app_folder')[1]==ti
            except Exception:return False
        async def query(workspace,q):
            if not live(): raise ValueError('authority unavailable')
            value=await query_code(repository=repository,provider=memory_provider,auth_manager=auth,owner=owner,workspace_id=workspace,purpose='app_folder',query=q)
            if not live() or value.get('error'):raise ValueError('query unavailable')
            return value
        limits={'rows':1,'bytes':16384,'tokens':8192,'work':MAX_QUERY_WORK}
        selected=await query(sw,{'operation':'inspect','seed_ids':[source['file_id']],'expected_receipt':source['receipt'],'fields':['source','properties'],'channels':[],'budgets':limits})
        rows=selected.get('hits') or []
        if len(rows)!=1 or rows[0].get('id')!=source['file_id']:return unavailable()
        loc=rows[0].get('source') or {}; rel=loc.get('rel_path') or (rows[0].get('properties') or {}).get('rel_path')
        if type(rel) is not str or PurePosixPath(rel).name!='package.json':return unavailable()
        async def read(workspace,receipt,path,required_id=None):
            value=await query(workspace,{'operation':'read','expected_receipt':receipt,'fields':['source','excerpt','properties'],'channels':['raw'],
                'read':{'rel_path':path,'start_byte':0,'length':1},'budgets':limits})
            rows=value.get('hits') or []
            if len(rows)!=1:return None
            row=rows[0]; raw=row.get('raw_source') or {}; locator=row.get('source')
            if (row.get('current_source_verified') is not True or type(locator) is not dict or raw.get('hash_scope')!='file'
                or type(raw.get('file_size')) is not int or raw['file_size']>524288 or type(raw.get('hash')) is not str
                or re.fullmatch(r'[0-9a-f]{64}',raw['hash']) is None or value.get('receipt')!=receipt
                or (required_id is not None and row.get('id')!=required_id)):return None
            return {'workspace_id':workspace,'file_id':row['id'],'receipt':receipt,'source_hash':locator['source_hash'],
                'sha256':raw['hash'],'source':locator,'size':raw['file_size']}
        a=await read(sw,source['receipt'],rel,source['file_id']); b=await read(tw,target['receipt'],'package.json')
        if a is None or b is None:return unavailable()
        relative=os.path.relpath(tb['root'],os.path.join(sb['root'],*PurePosixPath(rel).parts[:-1]))
        spec='file:'+relative
        parts=relative.split('/')
        if (not spec.isascii() or len(spec)>512 or len(parts)>32 or not parts or any(not p or p=='.' for p in parts)):
            return unavailable()
        normal=False
        for part in parts:
            if part=='..':
                if normal:return unavailable()
            else:
                normal=True
                if not re.fullmatch(r'[A-Za-z0-9_.@-]+',part):return unavailable()
        if not normal:return unavailable()
        digest=hashlib.sha256(spec.encode('ascii')).hexdigest()
        compared=await memory_provider._host_code_manifest_compare(owner=owner,binding=sb,receipt=source['receipt'],source=a['source'],expected_spec_sha256=digest,deadline=deadline,current=live)
        del spec,digest,relative
        if not live() or type(compared) is not dict or compared.get('state') not in {'match','no_match'}:return unavailable()
        a2=await read(sw,source['receipt'],rel,source['file_id']); b2=await read(tw,target['receipt'],'package.json')
        if not live() or a2!=a or b2!=b:return unavailable()
        pair=lambda x:{k:x[k] for k in ('workspace_id','file_id','receipt','source_hash','sha256')}
        result={'schema':'open-clank.code-cross-project/v1','state':compared['state'],'partial':True,'source':pair(a),'target':pair(b),
            'relation':None,'reasons':['selected_manifest_only','symlinks_installation_and_runtime_unproved']}
        if compared['state']=='match':
            if compared['value_end']>a['size']:return unavailable()
            result['relation']={'kind':'declared_local_file_dependency','semantics':'lexical_only',
                'key_span':{'start_byte':compared['key_start'],'end_byte':compared['key_end']},
                'value_span':{'start_byte':compared['value_start'],'end_byte':compared['value_end']}}
        if not live():return unavailable()
        return JSONResponse(result,headers=_HEADERS)
    except Exception:
        return unavailable()


async def select_graph_scip(request, memory_provider):
    """A named human selects a read-only local SCIP artifact for this receipt."""
    try:
        owner, _, auth, repository, _, _, _, deadline, current = _capture(request, memory_provider)
        payload = bytearray()
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            async for chunk in request.stream():
                if len(payload) + len(chunk) > 16384:
                    raise ValueError("request limit")
                payload.extend(chunk)
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result
            body = json.loads(payload, object_pairs_hook=pairs)
            if not isinstance(body, dict) or set(body) != {"workspace_id", "expected_receipt", "resource_ref"}:
                raise ValueError("invalid SCIP selection")
            workspace, token = body["workspace_id"], body["resource_ref"]
            if (not isinstance(workspace, str) or not 1 <= len(workspace.encode()) <= 256
                    or token is not None and (not isinstance(token, str) or not 1 <= len(token.encode()) <= 8192)):
                raise ValueError("invalid SCIP resource")
            from src.openclank.code_scip import select_scip
            result = await select_scip(owner=owner, workspace=workspace, expected=body["expected_receipt"],
                resource_ref=token, repository=repository, provider=memory_provider, auth_manager=auth, current=current)
            if not current():
                raise ValueError("workspace changed")
        return JSONResponse({"schema": "open-clank.code-scip-selection/v1", **result}, headers=_HEADERS)
    except Exception:
        return JSONResponse({"schema": "open-clank.code-scip-selection/v1", "state": "unavailable"},
                            status_code=409, headers=_HEADERS)


async def on_disk_library(request, memory_provider):
    """Human-selected on-disk npm declaration observation; no index admission."""
    import hashlib
    import os
    import re
    from src.openclank.code_query import resolve_query_binding, _files_client
    def unavailable():
        return JSONResponse({'schema':'open-clank.library-observation/v1','state':'unavailable','partial':True},status_code=409,headers=_HEADERS)
    try:
        owner, _, auth, repository, _, _, _, deadline, current = _capture(request,memory_provider)
        payload=bytearray()
        async with asyncio.timeout(max(0,deadline-time.monotonic())):
            async for chunk in request.stream():
                if len(payload)+len(chunk)>4096:return unavailable()
                payload.extend(chunk)
            def pairs(items):
                result={}
                for key,value in items:
                    if key in result:raise ValueError('duplicate key')
                    result[key]=value
                return result
            body=json.loads(payload,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if type(body) is not dict or set(body)!={'workspace_id','package'}:return unavailable()
            workspace,package=body['workspace_id'],body['package']
            if (type(workspace) is not str or not workspace or len(workspace.encode())>256 or type(package) is not str
                    or len(package)>214 or re.fullmatch(r'(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*',package) is None or '..' in package):return unavailable()
            from src.openclank.code_query import observe_library_scoped
            observed=await observe_library_scoped(repository=repository,provider=memory_provider,auth_manager=auth,owner=owner,
                workspace=workspace,purpose='app_folder',session=None,package=package,deadline=deadline,current=current)
            if observed is None:return unavailable()
            return JSONResponse(observed,headers=_HEADERS)
    except Exception:
        return unavailable()

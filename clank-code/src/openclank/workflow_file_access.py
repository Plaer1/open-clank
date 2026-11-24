"""Private Files target adapter for an already admitted workflow artifact."""
from __future__ import annotations

import os
import asyncio
from collections.abc import Mapping

from src.openclank.files_host_provider import _origin, _path
from src.openclank.workflow_file_replace import agent_replace_once, agent_replace_status


async def authorize_workflow_agent_root(provider, context, *, path, current, workspace_root, chat_id):
    """Recheck the existing AgentScope and its selected History principal."""
    from src.openclank.workflow_agent_client import RootedAgentFilesClient
    if not callable(current) or await current() is not True:
        raise ValueError('workflow Agent root unavailable')
    if (not isinstance(path,str) or not os.path.isabs(path) or os.path.realpath(path)!=path or
            os.path.commonpath((path,os.path.realpath(workspace_root)))!=path):
        raise ValueError('workflow Agent root unavailable')
    store=provider.operation_store
    if store is None or store.generation()!=context.policy_generation: raise ValueError('workflow Agent root unavailable')
    locations=[item for item in store.list_locations() if item.kind=='directory' and item.enabled and
        item.availability=='available' and os.path.normpath(str(item.canonical_path))==path and 'read' in set(item.capabilities)]
    if len(locations)!=1: raise ValueError('workflow Agent root unavailable')
    location=locations[0];probe=None;selector=None
    try:
        # Agent root IDs are binding identities, not Files location IDs.
        # Select the actual current AgentScope root linked to this location.
        from src.openclank.files_service_client import AgentFilesClient, owner_is_admin_or_single_user
        selector=AgentFilesClient(context.owner_username,workspace_root,workspace_id=context.workspace_id,chat_id=chat_id)
        registry=selector._registry()
        visibility=None if owner_is_admin_or_single_user(context.owner_username) else registry.visibility_for_subject(context.owner_username)
        scope=registry.agent_scope(context.owner_username,workspace_root,app_visibility=visibility,workspace_id=context.workspace_id,
            chat_id=chat_id,registered_only=True)
        roots=[item for item in registry.list(context.owner_username) if item.get('id') in set(scope.get('approved_root_ids') or []) and
            item.get('location_id')==location.id and item.get('canonical_path')==path and item.get('enabled') and item.get('availability')=='available']
        if len(roots)!=1 or scope.get('generation')!=context.policy_generation:
            raise ValueError('workflow Agent root unavailable')
        root_id=str(roots[0]['id'])
        probe=RootedAgentFilesClient(context.owner_username,workspace_root,workspace_id=context.workspace_id,chat_id=chat_id,
            history_root_id=root_id,history_root_path=path)
        scope=probe.app_scope
        if (root_id not in set(scope.get('visible_root_ids') or []) or
                'read' not in set((scope.get('root_capabilities') or {}).get(root_id) or [])):
            raise ValueError('workflow Agent root unavailable')
        env=probe.history_binding_environment()
        if (env.get('OPEN_CLANK_FILES_LANE')!='agent' or env.get('OPENCLANK_HISTORY_ACCOUNT_ID')!=context.owner_subject_id or
                env.get('OPENCLANK_HISTORY_ACTOR_ID')!='agent:'+context.owner_username or
                not env.get('OPENCLANK_HISTORY_WORKSPACE_ID') or not env.get('OPENCLANK_HISTORY_SOCKET')):
            raise ValueError('workflow Agent root unavailable')
        observed=await provider.stat(context,origin_id=_origin(path))
        if (observed.origin_id!=_origin(path) or observed.kind not in {'folder','provider_root'} or
                'children' not in observed.capabilities or store.generation()!=context.policy_generation or await current() is not True):
            raise ValueError('workflow Agent root unavailable')
        return {'root_id':root_id,'root':path,'location_revision':location.revision,'generation':context.policy_generation,
            'history_actor':env['OPENCLANK_HISTORY_ACTOR_ID'],'history_workspace':env['OPENCLANK_HISTORY_WORKSPACE_ID'],
            'history_socket':env['OPENCLANK_HISTORY_SOCKET']}
    finally:
        if probe is not None: probe.close()
        if selector is not None: selector.close()


async def lookup_workflow_history_identity(provider, context, *, root_origin_id, root_revision, path, history_supervisor, current, workspace_root, chat_id):
    """Read the actual selected Agent/History root binding and unique row."""
    from pathlib import Path
    from src.openclank.files_service_client import _checkpoint_owned_thread
    from src.openclank.history_client import HistoryClient, registered_resource_pages
    from src.openclank.workflow_agent_client import RootedAgentFilesClient
    if not callable(current) or await current() is not True: return None
    process=getattr(history_supervisor,'process',None)
    if process is None or process.poll() is not None: return None
    probe=None
    try:
        root=dict(await authorize_workflow_agent_root(provider,context,path=_path(root_origin_id),current=current,workspace_root=workspace_root,chat_id=chat_id))
        if not os.path.isabs(path) or os.path.commonpath((root['root'],path))!=root['root'] or path==root['root']: return None
        probe=RootedAgentFilesClient(context.owner_username,workspace_root,workspace_id=context.workspace_id,chat_id=chat_id,
            history_root_id=root['root_id'],history_root_path=root['root'])
        env=probe.history_binding_environment()
        if env.get('OPENCLANK_HISTORY_ACCOUNT_ID')!=context.owner_subject_id: return None
        actor=env['OPENCLANK_HISTORY_ACTOR_ID']; history_workspace=env['OPENCLANK_HISTORY_WORKSPACE_ID']
        history=HistoryClient(env['OPENCLANK_HISTORY_SOCKET'],actor_id=actor,account_id=context.owner_subject_id,
            token=env['OPENCLANK_HISTORY_TOKEN'],timeout=1.5)
        rows=[]
        async def fetch(cursor): return await _checkpoint_owned_thread(lambda:history.list_registered_resources(cursor=cursor,limit=100))
        async for page in registered_resource_pages(fetch):
            for row in page['items']:
                if (row.get('account_id')!=context.owner_subject_id or row.get('workspace_id')!=history_workspace
                        or row.get('root_path')!=root['root'] or row.get('active') is not True): continue
                relative=str(row.get('relative_path') or '')
                if not relative or os.path.isabs(relative) or '..' in Path(relative).parts: continue
                if os.path.normpath(os.path.join(root['root'],relative))==path: rows.append(row)
        if (await current() is not True or getattr(history_supervisor,'process',None) is not process or process.poll() is not None
                or dict(await authorize_workflow_agent_root(provider,context,path=_path(root_origin_id),current=current,workspace_root=workspace_root,chat_id=chat_id))!=root
                or len(rows)!=1): return None
        row=rows[0];resource=row.get('resource_id')
        if (row.get('root_id')!=root['root_id'] or not isinstance(row.get('actor_ids'),list) or (row['actor_ids'] and actor not in row['actor_ids'])
                or not isinstance(resource,str) or not resource.startswith('file:')): return None
        return resource
    except Exception:
        return None
    finally:
        if probe is not None: probe.close()


async def workflow_create(facade, context, *, parent_ref: str, name: str, text: str,
                          expected_parent_revision: Mapping, key: str, session_id: str, client_factory, current, history_identity, bind, workflow_origin=None):
    """Strict Agent-lane create and bind after original Files/History proof."""
    from src.openclank.ordinary_create import ordinary_create_once, create_recovery_binding
    if (not callable(current) or not callable(history_identity) or not callable(bind) or not callable(client_factory)
            or not isinstance(name,str) or not name.endswith('.md') or '/' in name or '\\' in name
            or not isinstance(text,str) or '\x00' in text or len(text.encode('utf-8'))>262144 or await current() is not True):
        return {'state':'unavailable'}
    provider,parent=facade._provider_for_ref(context,parent_ref,capability='write')
    if parent.provider!='host' or not isinstance(expected_parent_revision,Mapping) or expected_parent_revision.get('kind')!='hostDirectoryRevision':
        return {'state':'unavailable'}
    target=os.path.join(_path(parent.origin_id),name);content=text.encode('utf-8','strict')
    recovery=create_recovery_binding(context.owner_subject_id,context.workspace_id,session_id,'agent',key,target,expected_parent_revision['value'],content)
    client=client_factory(target,{'operation_id':recovery['operation_id'],'request_digest':recovery['native_digest']})
    try:
        async def work(begin_mutation,known_no_effect):
            async def before_issue():
                if await current() is not True: return False
                entry=await provider.stat(context,origin_id=parent.origin_id)
                if entry.origin_id!=parent.origin_id or dict(entry.revision or {})!=dict(expected_parent_revision): return False
                begin_mutation(); return True
            result=await ordinary_create_once(client=client,repository=provider.operation_store,account=context.owner_subject_id,
                workspace=context.workspace_id,session=session_id,lane='agent',key=key,path=target,parent_revision=expected_parent_revision['value'],
                content=content,generation=context.policy_generation,before_issue=before_issue,workflow_origin=workflow_origin)
            if result.get('state')=='not_applied':
                known_no_effect()
                return result
            if result.get('state')!='committed':
                from src.openclank.files_facade import FilesFacadeError
                raise FilesFacadeError('Original create outcome requires reconciliation',code='mutation_outcome_unknown')
            return result
        task=asyncio.create_task(provider._run_claimed_host_mutation(context,[target],work,delayed_start=True,recovery=recovery))
        try:
            result=await asyncio.shield(task)
        except asyncio.CancelledError:
            # The exact client remains alive until its owned exchange retires.
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
                except Exception: break
            if task.done() and not task.cancelled(): task.exception()
            raise
        if result.get('state')!='committed': return {'state':'conflict' if result.get('reason')=='conflict' else 'unknown'}
        # The Agent service independently reauthorizes the actual file before
        # the Host issues its opaque reference.
        observed=await client.request('stat',target,{'include_fingerprint':True})
        data=observed.get('data') if isinstance(observed,Mapping) else None
        if not isinstance(data,Mapping) or data.get('path')!=target: return {'state':'partial'}
        entry=await provider.stat(context,origin_id=_origin(target))
        resource=facade._resource(context,'host',entry,parent_stable_id=parent.stable_id)
        if resource.get('revision')!={'kind':'hostFingerprint','value':result['fingerprint']}: return {'state':'partial'}
        history_id=await history_identity(target)
        if not isinstance(history_id,str) or not history_id.startswith('file:') or await current() is not True or await bind(resource,history_id) is not True:
            return {'state':'partial'}
        if await current() is not True: return {'state':'partial'}
        return {'state':'complete','resource':resource}
    except Exception:
        return {'state':'unknown'}
    finally:
        client.close()


async def workflow_create_status(*, client_factory, repository, account: str, workspace: str, session_id: str, key: str, path: str, current):
    """Read an existing strict Files create receipt without the original text."""
    from src.openclank.ordinary_create import _identity, _digest
    if (not callable(current) or not isinstance(path,str) or not os.path.isabs(path)
            or any(not isinstance(x,str) or not x or x != x.strip() for x in (account,workspace,key))
            or await current() is not True): return {'state':'unavailable'}
    operation = _identity(account,workspace,session_id,'agent',key)
    row = repository.get_operation(owner_subject_id=account,operation_id=operation)
    if row is None: return {'state':'unknown'}
    receipt = row.get('receipt')
    if (not isinstance(receipt,Mapping) or receipt.get('version') != 1 or receipt.get('kind') != 'ordinary_create'
            or receipt.get('workspace') != workspace or receipt.get('session') != session_id or receipt.get('lane') != 'agent'
            or receipt.get('path') != path): return {'state':'unavailable'}
    if receipt.get('state') == 'not_applied' and receipt.get('reason') in ('not_issued','conflict'):
        return {'state':'not_applied'} if await current() is True else {'state':'unavailable'}
    binding=receipt.get('binding'); capture=receipt.get('capture_identity')
    if receipt.get('settled') is not True or not isinstance(binding,Mapping) or not isinstance(capture,Mapping): return {'state':'unknown'}
    action='create_' + _digest(['openclank.files.create-action.v1',operation])
    desired=binding.get('desired_sha256'); size=binding.get('desired_size'); parent_revision=binding.get('parent_revision')
    if (binding.get('operation_id') != operation or binding.get('action_id') != action or capture.get('account_id') != account
            or binding.get('request_digest') != _digest(['openclank.files.ordinary-create.v1',account,path,parent_revision,desired,size,operation,action])
            or row.get('digest') != _digest(['openclank.files.create-request.v1',account,workspace,session_id,'agent',path,parent_revision,desired,size,operation,action])):
        return {'state':'unavailable'}
    client=client_factory(path,dict(binding))
    try:
        response=await client.ordinary_create_status_exact(path,binding=dict(binding),capture_identity=dict(capture))
        data=response.get('data') if isinstance(response,Mapping) else None
        if (isinstance(data,Mapping) and set(data)=={'state','fingerprint'} and data.get('state')=='committed'
                and data.get('fingerprint')==f'sha256:{desired}:{size}' and await current() is True):
            recovery={'kind':'ordinary_create','operation_id':operation,'action_id':action,
                      'native_digest':binding['request_digest'],'request_digest':row['digest']}
            offset=0; matched=[]
            while offset<2048:
                batch=repository.list_operations(owner_subject_id=account,operation_prefix='__host_path_claim__',phase='host_path_claim',offset=offset,limit=256)
                for item in batch:
                    claim=(item.get('receipt') or {}).get('_host_writer_claim')
                    if (isinstance(claim,Mapping) and claim.get('recovery')==recovery and claim.get('paths')==[path]
                            and claim.get('workspace_id')==workspace and item.get('generation')==row.get('generation')
                            and claim.get('token')==item.get('digest')): matched.append((item['operation_id'],claim['token']))
                if len(batch)<256: break
                offset+=len(batch)
            else: return {'state':'unknown'}
            if len(matched)>1 or await current() is not True: return {'state':'unknown'}
            if matched: repository.release_host_mutation(owner_subject_id=account,operation_id=matched[0][0],token=matched[0][1])
            return {'state':'committed','revision':{'kind':'hostFingerprint','value':data['fingerprint']}}
    except Exception: pass
    finally: client.close()
    return {'state':'unknown'}


async def resolve_workflow_member(facade, context, *, workspace_root: str, member: Mapping, history_identity, current):
    """Resolve a stored member through today's authorized root and both identities."""
    if (not isinstance(workspace_root, str) or not os.path.isabs(workspace_root) or not isinstance(member, Mapping)
            or not callable(history_identity) or not callable(current) or await current() is not True):
        raise ValueError('workflow target unavailable')
    relative = member.get('relative_path')
    if (not isinstance(relative, str) or not relative.startswith(('.clanker/futures/', '.clanker/robonotes/'))
            or any(part in ('', '.', '..') for part in relative.split('/')) or '\\' in relative):
        raise ValueError('workflow target unavailable')
    root = os.path.realpath(workspace_root)
    path = os.path.abspath(os.path.join(root, *relative.split('/')))
    # Never follow a moved/replaced parent into an unrelated resource.
    if os.path.commonpath((root, path)) != root or os.path.realpath(path) != path:
        raise ValueError('workflow target unavailable')
    provider = facade._providers.get('host')
    if provider is None: raise ValueError('workflow target unavailable')
    entry = await provider.stat(context, origin_id=_origin(path))
    if entry.origin_id != _origin(path) or entry.kind != 'file' or 'open' not in entry.capabilities:
        raise ValueError('workflow target unavailable')
    result = facade._resource(context, 'host', entry, parent_stable_id=None)
    if (result.get('id') != member.get('resource_id')
            or await history_identity(path) != member.get('history_resource_id') or await current() is not True):
        raise ValueError('workflow target unavailable')
    return result


async def qualified_workflow_object_tag(context, *, path, expected_revision, workspace_root, chat_id, history_root_id, history_root_path, current):
    """Observe the same current Agent-authorized object and fingerprint twice."""
    import re
    from src.openclank.workflow_agent_client import RootedAgentFilesClient
    if not callable(current) or await current() is not True: return None
    client=RootedAgentFilesClient(context.owner_username,workspace_root,workspace_id=context.workspace_id,chat_id=chat_id,
        history_root_id=history_root_id,history_root_path=history_root_path)
    try:
        first=(await client.request('stat',path,{'object_identity':True})).get('data') or {}
        second=(await client.request('stat',path,{'include_fingerprint':True})).get('data') or {}
        last=(await client.request('stat',path,{'object_identity':True})).get('data') or {}
        tag=first.get('object_tag')
        fingerprint=second.get('fingerprint')
        if isinstance(fingerprint,Mapping): fingerprint=fingerprint.get('value')
        if (first.get('path')!=path or second.get('path')!=path or str(first.get('kind','')).lower()!='file' or str(second.get('kind','')).lower()!='file'
                or last.get('path')!=path or str(last.get('kind','')).lower()!='file' or not isinstance(tag,str)
                or not re.fullmatch(r'[0-9a-f]{64}',tag) or last.get('object_tag')!=tag
                or expected_revision!={'kind':'hostFingerprint','value':fingerprint} or await current() is not True): return None
        return tag
    except Exception:return None
    finally:client.close()


async def workflow_read(facade, context, *, resource_ref: str, resource_id: str, workspace_root: str, chat_id: str,
                        history_resource_id: str, history_identity, current, history_root_id: str, history_root_path: str):
    """Read current bounded text through a fresh scoped Agent Files client."""
    from pathlib import Path
    from src.openclank.workflow_agent_client import RootedAgentFilesClient
    provider,ref=facade._provider_for_ref(context,resource_ref,capability='open')
    if (ref.provider!='host' or ref.stable_id!=resource_id or not callable(history_identity)
            or not isinstance(history_resource_id,str) or not history_resource_id.startswith('file:')
            or await current() is not True): return {'state':'unavailable'}
    path=_path(ref.origin_id)
    if await history_identity(path)!=history_resource_id: return {'state':'unavailable'}
    client=RootedAgentFilesClient(context.owner_username,workspace_root,workspace_id=context.workspace_id,chat_id=chat_id,
        history_root_id=history_root_id,history_root_path=history_root_path)
    try:
        response=await client.request('read_lines',path,{})
        data=response.get('data') if isinstance(response,Mapping) else None
        if not isinstance(data,Mapping): return {'state':'unavailable'}
        name=Path(path).name or 'Untitled'
        snapshot=provider._snapshot(name,dict(data),provider._representation(name,data))
        text=snapshot.get('envelope',{}).get('text'); revision=snapshot.get('revision')
        if (not isinstance(text,str) or '\x00' in text or len(text.encode('utf-8','strict'))>262144
                or not isinstance(revision,Mapping) or set(revision)!={'kind','value'} or revision.get('kind')!='hostFingerprint'
                or await current() is not True or await history_identity(path)!=history_resource_id): return {'state':'unavailable'}
        again=await provider.stat(context,origin_id=ref.origin_id)
        if again.origin_id!=ref.origin_id or dict(again.revision or {})!=dict(revision) or await current() is not True:
            return {'state':'unavailable'}
        return {'state':'found','resourceRef':resource_ref,'revision':dict(revision),'text':text}
    except Exception:
        return {'state':'unavailable'}
    finally:
        client.close()


async def workflow_replace(facade, context, *, resource_ref: str, resource_id: str, key: str,
                           expected_revision: Mapping, text: str, history_resource_id: str, history_identity, current, client_factory, workflow_origin=None):
    """Use the exact owner-bound Host ref and strict Files/Lore replace owner.

    The caller must establish task/root and plan CAS authority, and `current`
    must recheck it; neither an opaque ref nor a receipt is authorization.
    """
    provider, ref = facade._provider_for_ref(context, resource_ref, capability='open')
    if ref.provider != 'host' or ref.stable_id != resource_id or not callable(history_identity) or not isinstance(history_resource_id, str) or not history_resource_id.startswith('file:'):
        raise ValueError('workflow target unavailable')
    async def qualified():
        if await current() is not True: return False
        try:
            p, r = facade._provider_for_ref(context, resource_ref, capability='open')
            if p is not provider or r.provider != 'host' or r.stable_id != resource_id or r.origin_id != ref.origin_id: return False
            entry = await provider.stat(context, origin_id=ref.origin_id)
            scope = provider._scope(context)
            return (entry.origin_id == ref.origin_id and entry.kind == 'file' and 'open' in entry.capabilities
                    and provider._path_writable(context, _path(ref.origin_id), scope)
                    and await history_identity(_path(ref.origin_id)) == history_resource_id and await current() is True)
        except Exception:
            return False
    if (not callable(history_identity) or not isinstance(history_resource_id, str) or not history_resource_id.startswith('file:')
            or not isinstance(expected_revision, Mapping) or set(expected_revision) != {'kind','value'}
            or expected_revision.get('kind') != 'hostFingerprint' or not isinstance(text,str)):
        raise ValueError('workflow target unavailable')
    if not callable(client_factory) or not await qualified(): return {'state':'unavailable'}
    return await agent_replace_once(client_factory=client_factory, repository=provider.operation_store, account=context.owner_subject_id,
        workspace=context.workspace_id, generation=context.policy_generation, key=key, path=_path(ref.origin_id),
        expected_fingerprint=expected_revision['value'], content=text.encode('utf-8'), current=qualified, workflow_origin=workflow_origin)


async def workflow_status(facade, context, *, resource_ref: str, resource_id: str, key: str, history_resource_id: str, history_identity, current, client_factory):
    """Read the original receipt after reauthorizing its currently bound target."""
    provider, ref = facade._provider_for_ref(context, resource_ref, capability='open')
    if ref.provider != 'host' or ref.stable_id != resource_id or not callable(history_identity) or not isinstance(history_resource_id, str) or not history_resource_id.startswith('file:'):
        raise ValueError('workflow target unavailable')
    async def qualified():
        if await current() is not True: return False
        try:
            p, r = facade._provider_for_ref(context, resource_ref, capability='open')
            if p is not provider or r.provider != 'host' or r.stable_id != resource_id or r.origin_id != ref.origin_id: return False
            entry = await provider.stat(context, origin_id=ref.origin_id)
            return entry.origin_id == ref.origin_id and entry.kind == 'file' and 'open' in entry.capabilities and await history_identity(_path(ref.origin_id)) == history_resource_id and await current() is True
        except Exception:
            return False
    if not callable(client_factory) or not await qualified(): return {'state':'unavailable'}
    return await agent_replace_status(client_factory=client_factory, repository=provider.operation_store, account=context.owner_subject_id,
        workspace=context.workspace_id, key=key, path=_path(ref.origin_id), current=qualified)

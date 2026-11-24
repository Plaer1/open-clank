"""Current-authority projection of exact managed Lore action selectors.

The caller supplies an independently authenticated context and currentness
predicate; a locator or registered path is never a Files grant.
"""
from __future__ import annotations
import os
import re
from pathlib import Path
from fastapi import HTTPException
from src.openclank.files_facade import FilesFacadeError
from src.openclank.files_host_provider import host_origin_for_path
from src.openclank.history_change_sets import parse_locator
from src.openclank.history_client import HistoryAvailabilityError, registered_resource_pages
from src.openclank.history_io import bounded_history_io
from src.openclank.resource_refs import stable_resource_id


async def resolve_registered_history_ids(client, resource_id, *, account, context, provider, resolved_paths, allow_absent=False):
    """Resolve current Files-authorized registrations, retaining no path authority."""
    requested = str(resource_id or '').strip()
    private_requested = requested.startswith('file:')
    if not private_requested and not re.fullmatch(r'resource-[0-9a-f]{64}', requested):
        raise HistoryAvailabilityError('resource_not_found', 'registered Files resource is unavailable')
    matches = []; seen = set()
    async def fetch_page(cursor):
        return await bounded_history_io(lambda:client.list_registered_resources(cursor=cursor, limit=100), timeout=1.5)
    async for page in registered_resource_pages(fetch_page):
        for registration in page['items']:
            if registration.get('account_id') != account or not registration.get('workspace_id'): continue
            root = str(registration.get('root_path') or ''); relative_path = str(registration.get('relative_path') or '')
            relative = Path(relative_path)
            if not root or not Path(root).is_absolute() or not relative_path or relative.is_absolute() or '..' in relative.parts: continue
            path = str(Path(root) if relative_path == '.' else Path(root) / relative)
            try:
                public_id = stable_resource_id(owner_subject_id=account, provider='host', origin_id=host_origin_for_path(path))
            except (OSError, TypeError, ValueError): continue
            private_id = str(registration.get('resource_id') or '').strip()
            if (private_requested and private_id != requested) or (not private_requested and public_id != requested): continue
            try:
                current = await provider.resource_for_path(context, path=path)
            except FilesFacadeError as exc:
                if exc.code not in {'resource_unavailable','invalid_resource_ref'}:
                    raise HTTPException(503, 'Files authorization could not be checked') from exc
                # A trashed or deleted original has no Files row to stat. Its history stays reachable only when the
                # leaf is really absent and its parent folder is still a current, writable Host resource of this account.
                parent_path = os.path.dirname(path)
                if not allow_absent or os.path.lexists(path) or os.path.realpath(parent_path) != parent_path: continue   # symlinked parent: refused
                try:
                    parent = await provider.resource_for_path(context, path=parent_path)
                except FilesFacadeError: continue
                if not {'children', 'write'}.issubset(set(parent.capabilities)): continue
                current = None
            if current is not None and stable_resource_id(owner_subject_id=account, provider='host', origin_id=current.origin_id) != public_id: continue
            if private_id in seen and resolved_paths.get(private_id) != path:
                raise HistoryAvailabilityError('resource_unavailable', 'History registration is ambiguous')
            if not private_id.startswith('file:') or private_id in seen: continue
            seen.add(private_id); matches.append(private_id); resolved_paths[private_id] = path
            if len(matches) > 16: raise HistoryAvailabilityError('resource_unavailable', 'Files identity has too many matching History registrations')
    if not matches: raise HistoryAvailabilityError('resource_not_found', 'registered Files resource is unavailable')
    return sorted(matches)


async def _qualify_captured_change(locator, *, client, account, context, provider, current, metadata=False, private_paths=None):
    """Read exact selected action and recheck all mappings; caller owns authority."""
    value = parse_locator(locator); paths = {}
    def check():
        if current() is not True: raise ValueError('history authority changed')
    async def resolve(resource):
        check(); found = {}
        result = await resolve_registered_history_ids(client, resource, account=account, context=context,
            provider=provider, resolved_paths=found)
        check()
        if result != [resource] or not found.get(resource): raise ValueError('history resource unavailable')
        return found[resource]
    if value['version'] == 2:
        def read_batch():
            return client.get_managed_action_batch(action_id=value['actionID'],request_digest=value['requestDigest'],
                complete_digest=value['completeDigest'],origin=value['origin'])
        check(); batch = await bounded_history_io(read_batch); check()
        for member in batch['members']:
            resource = member['resource_id']; paths[resource] = await resolve(resource)
        members = []
        for member in batch['members']:
            resource = member['resource_id']; path = paths[resource]
            item = {'state':'available','resource_id':resource,'current_resource_id':stable_resource_id(
                owner_subject_id=account,provider='host',origin_id=host_origin_for_path(path))}
            if metadata:
                name = os.path.basename(path)
                if not name or len(name.encode('utf-8','strict')) > 1024: raise ValueError('history name unavailable')
                item['current_name'] = name
            try:
                check(); item['item'] = await bounded_history_io(lambda member=member:client.get_managed_resource_change(
                    resource_id=member['resource_id'],action_id=value['actionID'],before_version_id=member['before_version_id'],
                    after_version_id=member['after_version_id'],origin=value['origin']))
            except HistoryAvailabilityError as exc:
                item['state'] = 'pruned' if exc.code == 'version_expired' else 'unavailable'
            check(); members.append(item)
        if await bounded_history_io(read_batch) != batch: raise ValueError('history batch changed')
        check()
        for member in batch['members']:
            resource = member['resource_id']
            if await resolve(resource) != paths[resource]: raise ValueError('history mapping changed')
        check()
        if private_paths is not None: private_paths.update(paths)
        return {'version':2,'coverage':'partial_recorded_action','action_id':value['actionID'],'members':members}
    resource = value['resourceID']; paths[resource] = await resolve(resource)
    check(); item = await bounded_history_io(lambda:client.get_managed_resource_change(resource_id=resource,
        action_id=value['actionID'],before_version_id=value['beforeVersionID'],after_version_id=value['afterVersionID'],origin=value['origin']))
    check()
    if await resolve(resource) != paths[resource]: raise ValueError('history mapping changed')
    result = {'version':1,'coverage':'one_recorded_action','resource_id':resource,'item':item,
        'current_resource_id':stable_resource_id(owner_subject_id=account,provider='host',origin_id=host_origin_for_path(paths[resource]))}
    if metadata:
        name = os.path.basename(paths[resource])
        if not name or len(name.encode('utf-8','strict')) > 1024: raise ValueError('history name unavailable')
        result['current_name'] = name
    check()
    if private_paths is not None: private_paths.update(paths)
    return result


async def qualify_captured_change(locator, *, client, account, context, provider, current, metadata=False):
    """Public projection retains its original authorization and path-free DTO."""
    return await _qualify_captured_change(locator, client=client, account=account, context=context,
        provider=provider, current=current, metadata=metadata)


async def qualify_selected_change(locator, selected_resource_id, *, client, account, context, provider, current):
    """Private exact recorded member and authorized current path; never serialize this result."""
    if type(selected_resource_id) is not str or not re.fullmatch(r'resource-[0-9a-f]{64}', selected_resource_id):
        raise ValueError('history member unavailable')
    value=parse_locator(locator)
    if value['version']==2:
        def check():
            if current() is not True: raise ValueError('history authority changed')
        def read_batch():
            return client.get_managed_action_batch(action_id=value['actionID'],request_digest=value['requestDigest'],
                complete_digest=value['completeDigest'],origin=value['origin'])
        async def resolve():
            check();paths={}
            ids=await resolve_registered_history_ids(client,selected_resource_id,account=account,context=context,
                provider=provider,resolved_paths=paths)
            check();return ids,paths
        check();batch=await bounded_history_io(read_batch);check()
        ids,paths=await resolve()
        matches=[member for member in batch['members'] if member.get('resource_id') in ids]
        if len(matches)!=1: raise ValueError('history member unavailable')
        member=matches[0];private_id=member['resource_id'];path=paths.get(private_id)
        if not path: raise ValueError('history member unavailable')
        item=await bounded_history_io(lambda:client.get_managed_resource_change(resource_id=private_id,
            action_id=value['actionID'],before_version_id=member['before_version_id'],
            after_version_id=member['after_version_id'],origin=value['origin']))
        check()
        if await bounded_history_io(read_batch)!=batch: raise ValueError('history batch changed')
        again,again_paths=await resolve()
        if again!=ids or again_paths!=paths: raise ValueError('history mapping changed')
        check()
        return {'resource_id':private_id,'path':path,'current_resource_id':selected_resource_id,
            'item':item,'coverage':'partial_recorded_action'}
    paths = {}
    result = await _qualify_captured_change(locator, client=client, account=account, context=context,
        provider=provider, current=current, private_paths=paths)
    if result['version'] == 1:
        if result['current_resource_id'] != selected_resource_id: raise ValueError('history member unavailable')
        selected = {'resource_id':result['resource_id'],'current_resource_id':result['current_resource_id'],'item':result['item']}
    else:
        matches = [member for member in result['members'] if member.get('current_resource_id') == selected_resource_id]
        if len(matches) != 1 or matches[0].get('state') != 'available': raise ValueError('history member unavailable')
        selected = matches[0]
    private_id = selected['resource_id']
    path = paths.get(private_id)
    if not path or current() is not True: raise ValueError('history member unavailable')
    return {'resource_id':private_id,'path':path,'current_resource_id':selected['current_resource_id'],
        'item':selected['item'],'coverage':result['coverage']}

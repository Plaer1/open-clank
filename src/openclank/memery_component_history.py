"""Retain approved snapshot components under current Files and Memery authority."""
import asyncio
import hashlib
import os
from pathlib import Path

from services.memory.memery_history import client_for, reference
from src.openclank.analysis_snapshot_capture import _allowed_path, revalidate_snapshot_root
from src.openclank.analysis_snapshot_host import _owned
from src.openclank.files_host_provider import host_origin_for_path, _path
from src.openclank.history_capture import _excluded_secret
from src.openclank.history_change_qualifier import resolve_registered_history_ids
from src.openclank.history_client import HistoryAvailabilityError
from src.openclank.resource_refs import stable_resource_id

MAX_FILE = 64 * 1024 * 1024
MAX_PASS = 256 * 1024 * 1024


def component_path(project, repository, relative, resource):
    if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('component location unavailable')
    root = os.path.normpath(str(project.binding.path))
    path = os.path.normpath(os.path.join(root, relative))
    if (os.path.commonpath((root, path)) != root or _excluded_secret(path)
            or any(part.startswith('.env') for part in Path(path).parts)
            or os.path.realpath(path) != path or not _allowed_path(project, repository, path)):
        raise ValueError('component access unavailable')
    identity = stable_resource_id(owner_subject_id=project.context.owner_subject_id, provider='host',
                                  origin_id=host_origin_for_path(path))
    if identity != resource:
        raise ValueError('component resource changed')
    return path


async def shared_version(client, project, node):
    try:
        resources = await resolve_registered_history_ids(client, node['resource_id'],
            account=project.context.owner_subject_id, context=project.context, provider=project.host,
            resolved_paths={}, allow_absent=True)
    except HistoryAvailabilityError as exc:
        if exc.code == 'resource_not_found':
            return False
        raise
    for resource in resources:
        page = await _owned(client.list_resource_versions, resource, limit=50)
        for version in page['items']:
            if (version.get('can_read') and version.get('size_bytes') == node['size_bytes']
                    and version.get('fingerprint') == node['revision']['value']):
                return True
    return False


async def retain_components(*, project, repository, root_ref, root_revision, state, owner,
                            account, memory_id, provider, manifest, current):
    human = state.history_supervisor.client_for(owner, account)
    policy = await _owned(human.get_capture_policy)
    if not policy['enabled'] or not policy['memery_evidence'] or policy['frequency'] == 'manual':
        return {'owned': 0, 'shared': 0, 'unavailable': 0, 'state': 'disabled'}
    await revalidate_snapshot_root(project=project, repository=repository, root_ref=root_ref,
        root_revision=root_revision, manifest=manifest)
    await current()
    _, root = project.facade._provider_for_ref(project.context, root_ref, capability='children')
    parent = _path(root.origin_id)
    client = client_for(state.history_supervisor, account)
    detail = await provider.versioned_detail(memory_id, owner=owner)
    row = detail['current']
    if row['reason'] != 'approved analysis snapshot':
        raise ValueError('original publication unavailable')
    item = reference({**row, 'evidence': True})
    catalog = await _owned(client.get_memery_manifest)
    catalog = await _owned(client.settle_memery_manifest, catalog['generation'], catalog['position'],
                           catalog['witness'], [item], [])
    if not any(entry['change'] == item['change'] for entry in catalog['entries']):
        raise ValueError('component history is unavailable')
    counts = {'owned': 0, 'shared': 0, 'unavailable': 0, 'state': 'settled'}
    read_size = 0
    deadline = asyncio.get_running_loop().time() + 30
    for node in manifest['components']:
        if node['kind'] != 'file':
            continue
        size = node['size_bytes']
        if size > MAX_FILE or read_size + size > MAX_PASS or asyncio.get_running_loop().time() >= deadline:
            counts['unavailable'] += 1
            continue
        try:
            await current()
            await project._check()
            rel = os.path.relpath(os.path.join(parent, *node['path']), str(project.binding.path))
            path = component_path(project, repository, rel, node['resource_id'])
            location = await _owned(client.memery_component_location, item['change'], node['sha256'], node['resource_id'])
            if location == rel:
                counts['owned'] += 1
                continue
            if await shared_version(human, project, node):
                counts['shared'] += 1
                continue
            read_size += size
            content = await project.host.content(project.context, origin_id=host_origin_for_path(path))
            if content.size != size or content.stream is None:
                raise ValueError('snapshot component changed')
            data = bytearray()
            async for chunk in content.stream(0, size):
                if len(data) + len(chunk) > size:
                    raise ValueError('snapshot component changed')
                data.extend(chunk)
            if len(data) != size or hashlib.sha256(data).hexdigest() != node['sha256']:
                raise ValueError('snapshot component changed')
            await current()
            await project._check()
            component_path(project, repository, rel, node['resource_id'])
            await _owned(client.put_memery_component, item['change'], node['sha256'], bytes(data), policy['revision'],
                         node['resource_id'], rel)
            counts['owned'] += 1
        except Exception:
            counts['unavailable'] += 1
    if counts['unavailable']:
        counts['state'] = 'incomplete'
    return counts

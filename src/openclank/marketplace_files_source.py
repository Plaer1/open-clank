"""Bounded selected Host Files reader for marketplace review, not an access grant."""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time

from src.openclank.analysis_snapshot_contract import _path
from src.openclank.files_host_provider import HostFilesProvider, _origin, _path as host_path


class MarketplaceFilesSource:
    """Retain only the observed directory membership and exact bounded file bytes."""
    MAX_ENTRIES = 1024
    MAX_FILE = 400000
    MAX_TOTAL = 2000000
    MAX_DEPTH = 32
    MAX_SECONDS = 30

    @classmethod
    async def open(cls, *, facade, context, repository, root_ref, expected_revision, current):
        if sys.platform != 'linux' or not callable(current) or type(expected_revision) is not dict:
            raise ValueError('Files source unavailable')
        self = cls()
        self.facade, self.context, self.repository, self.current = facade, context, repository, current
        self.deadline = time.monotonic() + cls.MAX_SECONDS
        self.directories, self.files, self.total, self.count = {}, {}, 0, 0
        await self._check()
        provider, root = facade._provider_for_ref(context, root_ref, capability='children')
        facade._provider_for_ref(context, root_ref, capability='stat')
        if not isinstance(provider, HostFilesProvider) or root.provider != 'host' or root.kind != 'folder':
            raise ValueError('Files source unavailable')
        self.host, self.root_ref, self.root = provider, root_ref, root
        self.root_path = host_path(root.origin_id)
        workspace_id = await self._await(asyncio.to_thread(repository.legacy_target, 'workspace',
            str(context.owner_subject_id) + ':' + str(context.workspace_id), fallback=str(context.workspace_id)))
        workspace = await self._await(asyncio.to_thread(repository.get_workspace, workspace_id))
        location = await self._await(asyncio.to_thread(repository.get_location, workspace.location_id))
        if workspace.archived or workspace.owner_subject_id != context.owner_subject_id or not location.enabled:
            raise ValueError('Files source workspace unavailable')
        self.workspace, self.location = workspace, location
        self.workspace_path = os.path.normpath(os.path.join(location.canonical_path, workspace.relative_folder))
        if os.path.commonpath((self.workspace_path, self.root_path)) != self.workspace_path:
            raise ValueError('Files source outside workspace')
        self.expected_revision = dict(expected_revision)
        await self._authorized(())
        entry = await self._await(provider.stat(context, origin_id=root.origin_id))
        if entry.origin_id != root.origin_id or entry.kind != 'folder' or facade._provider_revision(entry) != self.expected_revision:
            raise ValueError('Files source changed')
        listing = await self._directory(())
        self.identity = {'kind':'files','provider':'host','workspace_id':str(context.workspace_id),
            'root_resource_id':root.stable_id,'root_revision':dict(self.expected_revision),
            'root_directory_id':listing['directory_id']}
        await self._check()
        return self

    async def _check(self):
        if time.monotonic() >= self.deadline or await self.current() is not True:
            raise ValueError('Files source authority changed')
        if self.facade._generation_current(self.context) is not True:
            raise ValueError('Files source authority changed')

    async def _await(self, value):
        await self._check()
        result = await value
        await self._check()
        return result

    async def _authorized(self, parts):
        path = os.path.join(self.root_path, *parts)
        relative = os.path.relpath(path, self.workspace_path)
        if relative == '.': relative = ''
        if relative == '..' or relative.startswith('..' + os.sep) or os.path.isabs(relative):
            raise ValueError('Files source outside workspace')
        relative = '/'.join(filter(None, (self.workspace.relative_folder, relative.replace(os.sep, '/'))))
        workspace = await self._await(asyncio.to_thread(self.repository.get_workspace, self.workspace.id))
        location = await self._await(asyncio.to_thread(self.repository.get_location, self.location.id))
        if workspace != self.workspace or location != self.location:
            raise ValueError('Files source workspace changed')
        decision = await self._await(asyncio.to_thread(self.repository.resolve,
            subject_id=self.context.owner_subject_id, is_admin=self.context.is_admin, origin='app',
            location_id=self.location.id, capability='read', workspace_id=self.workspace.id,
            relative_resource=relative, observation_only=True))
        if not decision.allowed or decision.policy_generation != self.context.policy_generation:
            raise ValueError('Files source access changed')

    def _parts(self, relative):
        if type(relative) is not str or relative.startswith('/') or '\\' in relative:
            raise ValueError('Files source path unavailable')
        parts = [] if relative == '' else relative.split('/')
        if len(parts) > self.MAX_DEPTH:
            raise ValueError('Files source path unavailable')
        _path(parts)
        return tuple(parts)

    def _origin(self, parts):
        return _origin(os.path.join(self.root_path, *parts))

    async def _directory(self, parts):
        await self._authorized(parts)
        listing = await self._await(self.host.strict_snapshot_directory(self.context, origin_id=self._origin(parts), limit=self.MAX_ENTRIES))
        if listing.get('complete') is not True or listing.get('omitted_count') != 0 or listing.get('reasons'):
            raise ValueError('Files source directory incomplete')
        await self._authorized(parts)
        previous = self.directories.get(parts)
        if previous is not None and any(listing[k] != previous[k] for k in ('directory_id','object_tag','entries','complete','omitted_count','reasons')):
            raise ValueError('Files source directory changed')
        if previous is None:
            self.count += len(listing['entries']) + 1
            if self.count > self.MAX_ENTRIES:
                raise ValueError('Files source exceeds bounds')
            self.directories[parts] = listing
        return listing

    async def _resolve(self, parts, kind):
        if not parts:
            if kind != 'directory': raise ValueError('Files source path unavailable')
            return
        for index, name in enumerate(parts):
            listing = await self._directory(parts[:index])
            row = next((row for row in listing['entries'] if row['name'] == name), None)
            if row is None:
                from services.memory.marketplace_importer import MarketplaceNotFound
                raise MarketplaceNotFound('Files source entry absent')
            wanted = kind if index == len(parts)-1 else 'directory'
            if row['kind'] != wanted: raise ValueError('Files source entry unavailable')

    async def list(self, relative):
        parts = self._parts(relative)
        await self._resolve(parts, 'directory')
        listing = await self._directory(parts)
        return [{'name':row['name'], 'kind':row['kind']} for row in listing['entries']]

    async def _read_exact(self, parts):
        await self._authorized(parts)
        origin = self._origin(parts)
        before = await self._await(self.host.stat_inspection(self.context, origin_id=origin))
        revision = self.facade._provider_revision(before)
        if (before.origin_id != origin or before.kind != 'file' or type(before.size) is not int
                or not 0 <= before.size <= self.MAX_FILE or not revision or revision.get('kind') != 'hostObjectIdentity'):
            raise ValueError('Files source file unavailable')
        proof = await self._await(self.host.snapshot_file_hash(self.context, origin_id=origin, max_bytes=self.MAX_FILE, max_ms=30000))
        content = await self._await(self.host.content(self.context, origin_id=origin))
        if content.origin_id != origin or content.size != before.size or content.etag != revision['value'] or content.path is not None:
            raise ValueError('Files source file changed')
        if type(content.data) is bytes and content.stream is None:
            data = content.data
        elif content.data is None and content.stream is not None:
            chunks = []; size = 0
            async for chunk in content.stream(0, before.size + 1):
                await self._check()
                if type(chunk) is not bytes: raise ValueError('Files source unavailable')
                size += len(chunk)
                if size > before.size: raise ValueError('Files source changed')
                chunks.append(chunk)
            data = b''.join(chunks)
        else:
            raise ValueError('Files source unavailable')
        after = await self._await(self.host.stat_inspection(self.context, origin_id=origin))
        await self._authorized(parts)
        digest = hashlib.sha256(data).hexdigest()
        if (len(data) != before.size or after.origin_id != origin or after.size != before.size
                or self.facade._provider_revision(after) != revision or proof['object_tag'] != revision['value']
                or proof['size'] != len(data) or proof['sha256'] != digest):
            raise ValueError('Files source file changed')
        return revision, digest, data

    async def read(self, relative):
        parts = self._parts(relative)
        await self._resolve(parts, 'file')
        value = await self._read_exact(parts)
        prior = self.files.get(parts)
        if prior is not None and prior != value: raise ValueError('Files source file changed')
        if prior is None:
            self.total += len(value[2])
            if self.total > self.MAX_TOTAL or len(self.files) >= 64: raise ValueError('Files source exceeds bounds')
            self.files[parts] = value
        return value[2]

    async def finish(self):
        await self._check()
        provider, ref = self.facade._provider_for_ref(self.context, self.root_ref, capability='children')
        self.facade._provider_for_ref(self.context, self.root_ref, capability='stat')
        if provider is not self.host or ref.stable_id != self.root.stable_id or ref.origin_id != self.root.origin_id:
            raise ValueError('Files source root changed')
        root = await self._await(self.host.stat(self.context, origin_id=self.root.origin_id))
        if root.origin_id != self.root.origin_id or root.kind != 'folder' or self.facade._provider_revision(root) != self.expected_revision:
            raise ValueError('Files source root changed')
        for parts in sorted(self.directories): await self._directory(parts)
        for parts, old in sorted(self.files.items()):
            await self._resolve(parts, 'file')
            if await self._read_exact(parts) != old: raise ValueError('Files source file changed')
        for parts in sorted(self.directories): await self._directory(parts)
        await self._check()

"""Temporary exact Skills History roots; no independent identity or store."""
from __future__ import annotations

import asyncio
import os
import secrets

_AGENT_SKILL_SCOPE_SEAL = object()


class _AgentSkillScope:
    """A retained pinned scope, issued only from the current M5 session."""
    def __init__(self, seal, *, issuer, session, descriptor, parent_fd, identity, process,
                 mode='create', child_fd=None, child_identity=None):
        if seal is not _AGENT_SKILL_SCOPE_SEAL:
            raise ValueError('agent skill scope unavailable')
        self._seal, self.issuer, self.session, self.descriptor = seal, issuer, session, descriptor
        self.parent_fd, self.identity, self.process = parent_fd, tuple(identity), process
        self.root_id, self.directory = descriptor['root_id'], descriptor['canonical_path']
        self.actor, self.account = descriptor['actor_ids'][0], descriptor['account_ids'][0]
        self.drain_session_id = secrets.token_hex(32)
        self.revoking = False
        self.active = True
        self.mode = mode
        self.child_fd, self.child_identity = child_fd, child_identity
        self.active_credential = None
        self.published = False

    def assert_retained(self):
        import stat
        if (self._seal is not _AGENT_SKILL_SCOPE_SEAL or not self.active or self.issuer._failed
                or self.issuer.supervisor.process is not self.process or self.process is None or self.process.poll() is not None):
            raise ValueError('agent skill scope unavailable')
        held = os.fstat(self.parent_fd)
        named = os.stat(self.directory, follow_symlinks=False)
        if (not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(named.st_mode)
                or (held.st_dev, held.st_ino) != self.identity or (named.st_dev, named.st_ino) != self.identity):
            raise ValueError('agent skill parent changed')

    def assert_current(self):
        from src.openclank.skill_draft_reservation import _assert_skill_draft_save_current, _assert_skill_draft_status_current
        self.assert_retained()
        if (self.revoking or self.issuer._scopes.get(self.root_id) is not self.descriptor
                or (self.published and self.active_credential not in self.issuer.supervisor.credentials)):
            raise ValueError('agent skill scope changed')
        if self.mode == 'read':
            import stat
            _assert_skill_draft_status_current(self.session)
            original = self.session.save_original
            if (type(original) is not dict or original.get('directory') != self.directory
                    or original.get('child') != self.descriptor.get('selected_child') or self.child_fd is None):
                raise ValueError('agent skill original changed')
            held = os.fstat(self.child_fd)
            named = os.stat(self.descriptor['selected_child'], dir_fd=self.parent_fd, follow_symlinks=False)
            if (not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(named.st_mode)
                    or (held.st_dev, held.st_ino) != self.child_identity
                    or (named.st_dev, named.st_ino) != self.child_identity):
                raise ValueError('agent skill child changed')
        else:
            _assert_skill_draft_save_current(self.session)
        if (self.actor != 'agent:' + self.session.lifecycle.owner
                or self.account != self.session.lifecycle.account_id):
            raise ValueError('agent skill identity changed')



class SkillHistoryScopes:
    def __init__(self, supervisor, files_roots):
        self.supervisor = supervisor
        self._files = list(files_roots)
        self._scopes = {}
        self._lock = asyncio.Lock()
        self._failed = False

    async def _fence(self, original):
        self._failed = True
        # Stop/reap both the original and any unexpected replacement before
        # returning to the thread that still owns the Skills locks.
        current = self.supervisor.process
        try:
            await self.supervisor.fence()
        except Exception:
            pass
        for process in (original, current):
            if process is None:
                continue
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait)
            if process.poll() is None:
                raise RuntimeError('History process could not be fenced')

    async def _publish(self, original, actor, account):
        try:
            if self._failed or self.supervisor.process is not original or original is None or original.poll() is not None:
                raise ValueError('History scope unavailable')
            await self.supervisor.sync_authorized_roots(self._files + list(self._scopes.values()))
            if self.supervisor.process is not original or original.poll() is not None:
                raise ValueError('History process changed')
            if not await asyncio.to_thread(self.supervisor.authority_barrier, actor, account):
                raise ValueError('History authority drain unavailable')
            if self.supervisor.process is not original or original.poll() is not None:
                raise ValueError('History process changed')
        except BaseException:
            await self._fence(original)
            raise

    async def update_files(self, roots):
        async with self._lock:
            if self._failed:
                raise ValueError('History scope unavailable')
            self._files = list(roots)
            await self.supervisor.sync_authorized_roots(self._files + list(self._scopes.values()))

    async def open(self, *, owner, account, directory, identity):
        async with self._lock:
            if self._failed or not owner or not account or owner.startswith('agent:'):
                raise ValueError('History scope unavailable')
            info = os.stat(directory, follow_symlinks=False)
            if os.path.islink(directory) or not os.path.isdir(directory) or (info.st_dev, info.st_ino) != tuple(identity):
                raise ValueError('skill directory changed')
            nonce = secrets.token_hex(24)
            root_id = 'skill-history-' + nonce
            workspace = 'skills:' + nonce
            process = self.supervisor.process
            await self.supervisor.hold_temporary_scope(process)
            self._scopes[root_id] = {'root_id':root_id, 'canonical_path':directory,
                'kind':'recursive_directory', 'account_ids':[account], 'actor_ids':[owner], 'workspace_ids':[workspace]}
            try:
                await self._publish(process, owner, account)
            except BaseException:
                self._scopes.pop(root_id, None)
                raise
            return {'root_id':root_id, 'directory':directory, 'workspace_id':workspace,
                    'owner':owner, 'account':account, 'process':process, 'identity':tuple(identity)}

    async def open_create(self, *, owner, account, directory, identity, parent_fd, child):
        """Temporarily authorize one absent child of a caller-pinned parent.

        The caller retains parent_fd and its original authority through close.
        This scope is not permission to retry an uncertain creation.
        """
        if (type(parent_fd) is not int or parent_fd < 0 or type(child) is not str
                or not child or child in ('.', '..') or '/' in child or '\\' in child or '\0' in child
                or not os.path.isabs(directory) or os.path.realpath(directory) != directory):
            raise ValueError('invalid skill creation scope')

        def check_parent_and_absence():
            held = os.fstat(parent_fd)
            named = os.stat(directory, follow_symlinks=False)
            import stat
            if (not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(named.st_mode)
                    or (held.st_dev, held.st_ino) != tuple(identity)
                    or (named.st_dev, named.st_ino) != tuple(identity)):
                raise ValueError('skill parent changed')
            try:
                os.stat(child, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise ValueError('skill destination already exists')

        async with self._lock:
            if self._failed or not owner or not account or owner.startswith('agent:'):
                raise ValueError('History scope unavailable')
            check_parent_and_absence()
            nonce = secrets.token_hex(24)
            root_id = 'skill-history-' + nonce
            workspace = 'skills:' + nonce
            process = self.supervisor.process
            await self.supervisor.hold_temporary_scope(process)
            self._scopes[root_id] = {'root_id': root_id, 'canonical_path': directory,
                'kind': 'recursive_directory', 'selected_child': child,
                'parent_device': identity[0], 'parent_inode': identity[1],
                'account_ids': [account], 'actor_ids': [owner], 'workspace_ids': [workspace]}
            try:
                await self._publish(process, owner, account)
                check_parent_and_absence()
            except BaseException:
                self._scopes.pop(root_id, None)
                try:
                    await self._publish(process, owner, account)
                    await self.supervisor.release_temporary_scope()
                except BaseException:
                    await self._fence(process)
                raise
            return {'root_id': root_id, 'directory': directory, 'workspace_id': workspace,
                    'owner': owner, 'account': account, 'process': process,
                    'identity': tuple(identity), 'selected_child': child}

    async def _fence_agent(self, original):
        """Retain the caller's pinned scope until every owned worker is reaped."""
        self._failed = True
        async def reap():
            current = self.supervisor.process
            try:
                await self.supervisor.fence()
            except Exception:
                pass
            for process in (original, current):
                if process is None or process.poll() is not None:
                    continue
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    pass
                while process.poll() is None:
                    try:
                        await asyncio.to_thread(process.wait)
                    except Exception:
                        if process.poll() is None:
                            await asyncio.sleep(0.1)
        task = asyncio.create_task(reap())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def open_agent_create(self, *, session, directory, identity, parent_fd, child):
        """Publish one absent child only for the exact currently held M5 session."""
        from src.openclank.skill_draft_reservation import _assert_skill_draft_save_current
        if (type(parent_fd) is not int or parent_fd < 0 or type(child) is not str
                or not child or child in ('.', '..') or '/' in child or '\\' in child or '\0' in child
                or not os.path.isabs(directory) or os.path.realpath(directory) != directory):
            raise ValueError('invalid agent skill creation scope')
        def check_absent():
            _assert_skill_draft_save_current(session)
            import stat
            held, named = os.fstat(parent_fd), os.stat(directory, follow_symlinks=False)
            if (not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(named.st_mode)
                    or (held.st_dev, held.st_ino) != tuple(identity) or (named.st_dev, named.st_ino) != tuple(identity)):
                raise ValueError('skill parent changed')
            try:
                os.stat(child, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise ValueError('skill destination already exists')
        async with self._lock:
            if self._failed:
                raise ValueError('agent skill scope unavailable')
            await asyncio.to_thread(check_absent)
            owner, account = session.lifecycle.owner, session.lifecycle.account_id
            actor = 'agent:' + owner
            root_id, workspace = 'skill-history-' + secrets.token_hex(24), 'skills:' + secrets.token_hex(24)
            process = self.supervisor.process
            await self.supervisor.hold_temporary_scope(process)
            descriptor = {'root_id': root_id, 'canonical_path': directory, 'kind': 'recursive_directory',
                'selected_child': child, 'parent_device': identity[0], 'parent_inode': identity[1],
                'account_ids': [account], 'actor_ids': [actor], 'workspace_ids': [workspace]}
            scope = _AgentSkillScope(_AGENT_SKILL_SCOPE_SEAL, issuer=self, session=session,
                descriptor=descriptor, parent_fd=parent_fd, identity=identity, process=process)
            self._scopes[root_id] = descriptor
            try:
                await self.supervisor._publish_skill_agent_scope(scope, self._files + list(self._scopes.values()))
                await asyncio.to_thread(check_absent)
                await asyncio.to_thread(scope.assert_current)
                return scope
            except BaseException:
                self._scopes.pop(root_id, None)
                try:
                    await self._fence_agent(process)
                finally:
                    scope.active = False
                raise

    async def open_agent_read(self, *, session, directory, identity, parent_fd, child):
        """Pin the exact existing original child under a source-qualified read-only scope."""
        from src.openclank.skill_draft_reservation import _assert_skill_draft_status_current
        if (type(parent_fd) is not int or parent_fd < 0 or type(child) is not str
                or not child or child in ('.', '..') or '/' in child or '\\' in child or '\0' in child
                or not os.path.isabs(directory) or os.path.realpath(directory) != directory):
            raise ValueError('invalid agent skill status scope')
        original = getattr(session, 'save_original', None)
        if type(original) is not dict or original.get('directory') != directory or original.get('child') != child:
            raise ValueError('agent skill original unavailable')
        async with self._lock:
            if self._failed:
                raise ValueError('agent skill scope unavailable')
            await asyncio.to_thread(_assert_skill_draft_status_current, session)
            import stat
            held, named = os.fstat(parent_fd), os.stat(directory, follow_symlinks=False)
            if (not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(named.st_mode)
                    or (held.st_dev, held.st_ino) != tuple(identity) or (named.st_dev, named.st_ino) != tuple(identity)):
                raise ValueError('skill parent changed')
            child_fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                child_info = os.fstat(child_fd)
                child_named = os.stat(child, dir_fd=parent_fd, follow_symlinks=False)
                if (not stat.S_ISDIR(child_info.st_mode) or (child_info.st_dev, child_info.st_ino) != (child_named.st_dev, child_named.st_ino)):
                    raise ValueError('skill child changed')
            except BaseException:
                os.close(child_fd)
                raise
            account, actor = session.lifecycle.account_id, 'agent:' + session.lifecycle.owner
            root_id, workspace = 'skill-history-' + secrets.token_hex(24), 'skills:' + secrets.token_hex(24)
            process = self.supervisor.process
            try:
                await self.supervisor.hold_temporary_scope(process)
            except BaseException:
                os.close(child_fd)
                raise
            descriptor = {'root_id': root_id, 'canonical_path': directory, 'kind': 'recursive_directory',
                'selected_child': child, 'parent_device': identity[0], 'parent_inode': identity[1],
                'account_ids': [account], 'actor_ids': [actor], 'workspace_ids': [workspace]}
            scope = _AgentSkillScope(_AGENT_SKILL_SCOPE_SEAL, issuer=self, session=session,
                descriptor=descriptor, parent_fd=parent_fd, identity=identity, process=process,
                mode='read', child_fd=child_fd, child_identity=(child_info.st_dev, child_info.st_ino))
            self._scopes[root_id] = descriptor
            try:
                await self.supervisor._publish_skill_agent_scope(scope, self._files + list(self._scopes.values()))
                await asyncio.to_thread(scope.assert_current)
                return scope
            except BaseException:
                self._scopes.pop(root_id, None)
                try:
                    await self._fence_agent(process)
                finally:
                    scope.active = False
                    os.close(child_fd)
                raise

    async def close_agent_create(self, scope):
        """Revoke then drain this scope even if its originating sources retired."""
        async with self._lock:
            if (type(scope) is not _AgentSkillScope or scope._seal is not _AGENT_SKILL_SCOPE_SEAL
                    or scope.issuer is not self or self._scopes.get(scope.root_id) is not scope.descriptor):
                raise ValueError('agent skill scope changed')
            try:
                await asyncio.to_thread(scope.assert_retained)
                scope.revoking = True
                self._scopes.pop(scope.root_id)
                await self.supervisor._revoke_skill_agent_scope(scope, self._files + list(self._scopes.values()))
                await self.supervisor.release_temporary_scope()
                scope.active = False
                if scope.child_fd is not None:
                    os.close(scope.child_fd); scope.child_fd = None
            except BaseException:
                try:
                    await self._fence_agent(scope.process)
                finally:
                    scope.active = False
                    if scope.child_fd is not None:
                        os.close(scope.child_fd); scope.child_fd = None
                raise

    async def close(self, scope):
        async with self._lock:
            root_id = scope['root_id']
            expected = self._scopes.get(root_id)
            if expected is None or expected['account_ids'] != [scope['account']] or expected['actor_ids'] != [scope['owner']]:
                await self._fence(scope['process'])
                raise ValueError('History scope changed')
            self._scopes.pop(root_id)
            await self._publish(scope['process'], scope['owner'], scope['account'])
            try:
                await self.supervisor.release_temporary_scope()
            except BaseException:
                await self._fence(scope['process'])
                raise

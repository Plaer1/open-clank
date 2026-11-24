"""Host-injected admission seam for local emoji asset publication.

This module does not start workers, create clients, or schedule jobs.
"""
from __future__ import annotations

import asyncio

# The app-owned service is available after explicit per-owner consent.
THEME_EMOJI_ACQUISITION_READY = True

async def _drain_cancelled(task):
    """Keep the owner's admission until an already-started commit exits."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    # Consume a failure while preserving the caller's cancellation.
    if not task.cancelled():
        task.exception()
    raise asyncio.CancelledError


def acquisition_http_client():
    """Create a bounded direct client only when an admitted caller requests one."""
    import httpx
    return httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        follow_redirects=False, trust_env=False)


class ThemeEmojiAcquisition:
    def __init__(self, *, inventory, local_state, auth_manager, account_barrier, run_authorized=None):
        self.inventory = inventory
        self.local_state = local_state
        self.auth_manager = auth_manager
        self.account_barrier = account_barrier
        self.run_authorized = run_authorized

    async def read_overlay(self, *, owner, account_id, expected, kind, render_id=None):
        """Read only under current account/consent; missing overlay stays missing."""
        if kind not in {'g', 'k'} or (render_id is not None and not render_id.startswith(kind + ':')):
            raise ValueError('Invalid emoji identity')
        async with self.account_barrier.admitted(owner):
            def authorized():
                return (self.auth_manager.account_id(owner) == account_id
                    and self.auth_manager.username_for_account_id(account_id) == owner
                    and self.local_state.obligation(account_id) == expected
                    and expected.get('consent') == 'accepted')
            if not authorized():
                raise PermissionError('Emoji authority changed')
            result = await asyncio.to_thread(
                self.inventory.ready_catalog if render_id is None else self.inventory.read_ready,
                kind if render_id is None else render_id)
            if not authorized():
                raise PermissionError('Emoji authority changed')
            return result

    async def publish(self, *, owner, account_id, expected, candidate, data, lease, epoch_id=None):
        """Commit staged bytes after revalidating identity, lease and consent.

        The host owns `lease`; no publication is enabled until the shared
        installation lease implementation is connected. Cancellation drains the
        thread before releasing the app's account mutation barrier.
        """
        if not lease or not lease.valid() or not callable(self.run_authorized) or not self.run_authorized():
            raise PermissionError('Emoji acquisition lease unavailable')
        async with self.account_barrier.admitted(owner):
            def current_account():
                if not lease.valid() or not self.run_authorized():
                    return None
                current = self.auth_manager.account_id(owner)
                if not current or self.auth_manager.username_for_account_id(current) != owner:
                    return None
                return current
            if current_account() != account_id:
                raise PermissionError('Account changed')
            task = asyncio.create_task(asyncio.to_thread(
                self.inventory.publish, candidate, data, local_state=self.local_state,
                account_id=account_id, expected=expected, current_account=current_account, epoch_id=epoch_id))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await _drain_cancelled(task)

    async def prepare_inventory(self, *, owner, account_id, expected, lease):
        """Initialize the local overlay only under the admitted consent fence."""
        from core.atomic_io import _path_locks
        if not lease or not lease.valid() or not callable(self.run_authorized) or not self.run_authorized():
            raise PermissionError('Emoji acquisition lease unavailable')
        async with self.account_barrier.admitted(owner):
            def commit():
                with _path_locks([str(self.local_state._owner_path(account_id))]):
                    if (not lease.valid() or not self.run_authorized() or self.auth_manager.account_id(owner) != account_id
                            or self.auth_manager.username_for_account_id(account_id) != owner
                            or self.local_state.obligation(account_id) != expected
                            or expected.get('consent') != 'accepted'):
                        raise PermissionError('Emoji authority changed')
                    self.inventory.initialize()
            task = asyncio.create_task(asyncio.to_thread(commit))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await _drain_cancelled(task)

    async def prune_terminal_epochs(self, *, owner, account_id, expected, lease, epoch_ids, scanned):
        """Prune only terminal run IDs already proven by the host scheduler."""
        from core.atomic_io import _path_locks
        if not lease or not lease.valid() or not callable(self.run_authorized) or not self.run_authorized():
            raise PermissionError('Emoji acquisition lease unavailable')
        async with self.account_barrier.admitted(owner):
            def commit():
                with _path_locks([str(self.local_state._owner_path(account_id))]):
                    if (not lease.valid() or not self.run_authorized() or self.auth_manager.account_id(owner) != account_id
                            or self.auth_manager.username_for_account_id(account_id) != owner
                            or self.local_state.obligation(account_id) != expected
                            or expected.get('consent') != 'accepted'):
                        raise PermissionError('Emoji authority changed')
                    self.inventory.prune_terminal_epochs(epoch_ids, scanned)
            task = asyncio.create_task(asyncio.to_thread(commit))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await _drain_cancelled(task)

    async def merge_expected(self, *, owner, account_id, expected, lease, candidates, replace_current=True):
        """Commit one bounded discovered batch under current account/consent."""
        from core.atomic_io import _path_locks
        if not lease or not lease.valid() or not callable(self.run_authorized) or not self.run_authorized():
            raise PermissionError('Emoji acquisition lease unavailable')
        async with self.account_barrier.admitted(owner):
            def commit():
                with _path_locks([str(self.local_state._owner_path(account_id))]):
                    if (not lease.valid() or not self.run_authorized() or self.auth_manager.account_id(owner) != account_id
                            or self.auth_manager.username_for_account_id(account_id) != owner
                            or self.local_state.obligation(account_id) != expected
                            or expected.get('consent') != 'accepted'):
                        raise PermissionError('Emoji authority changed')
                    self.inventory.merge(candidates, replace_current=replace_current)
            task = asyncio.create_task(asyncio.to_thread(commit))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await _drain_cancelled(task)

    async def discover_noto(self, *, owner, account_id, expected, lease, client):
        """Discover one verified pinned tree, then merge bounded batches."""
        from .theme_emoji_discovery import read_noto_tree
        if not lease or not lease.valid() or not callable(self.run_authorized) or not self.run_authorized():
            raise PermissionError('Emoji acquisition lease unavailable')
        async with self.account_barrier.admitted(owner):
            if (not self.run_authorized() or self.auth_manager.account_id(owner) != account_id
                    or self.auth_manager.username_for_account_id(account_id) != owner
                    or self.local_state.obligation(account_id) != expected
                    or expected.get('consent') != 'accepted'):
                raise PermissionError('Emoji authority changed')
        total = 0
        for latest in (False, True):
            candidates = await read_noto_tree(client, latest=latest)
            for offset in range(0, len(candidates), 10000):
                await self.merge_expected(owner=owner, account_id=account_id, expected=expected,
                    lease=lease, candidates=candidates[offset:offset+10000], replace_current=latest)
            total += len(candidates)
        return total

    async def discover_kitchen_baseline(self, *, owner, account_id, expected, lease, manifest, starter=False):
        """Ingest only the shipped, pinned candidate manifest supplied by the host."""
        import hashlib
        if not lease or not lease.valid() or not callable(self.run_authorized) or not self.run_authorized():
            raise PermissionError('Emoji acquisition lease unavailable')
        from .theme_emoji_discovery import kitchen_candidates, cancellable_parse, _check_cancel
        def parse_baseline(raw, *, cancel):
            if (not isinstance(raw, bytes) or len(raw) > 16 * 1024 * 1024
                    or hashlib.sha256(raw).hexdigest() != '7ca3964943ca7eadb64fadea62bad818c5064f61909926e97aaeb61caf3fe1ad'):
                raise ValueError('Kitchen candidate manifest unavailable')
            try:
                lines = raw.decode('utf-8').splitlines()
            except UnicodeError as exc:
                raise ValueError('Invalid Kitchen candidate manifest') from exc
            if len(lines) != 147000:
                raise ValueError('Kitchen candidate manifest incomplete')
            candidates = []
            # Spread the first scene across the verified manifest so it can
            # load varied artwork before the full expected inventory is ready.
            for candidate in kitchen_candidates(lines[::1024] if starter else lines):
                _check_cancel(cancel)
                candidates.append(candidate)
            if not starter and len(candidates) != 147000:
                raise ValueError('Kitchen candidate manifest incomplete')
            return candidates
        candidates = await cancellable_parse(parse_baseline, manifest)
        for offset in range(0, len(candidates), 10000):
            await self.merge_expected(owner=owner, account_id=account_id, expected=expected,
                                      lease=lease, candidates=candidates[offset:offset+10000], replace_current=False)
        return len(candidates)

    async def discover_kitchen_latest(self, *, owner, account_id, expected, lease, client):
        """Refresh expected Kitchen candidates from a complete verified source."""
        from .theme_emoji_discovery import read_kitchen_metadata
        if not lease or not lease.valid() or not callable(self.run_authorized) or not self.run_authorized():
            raise PermissionError('Emoji acquisition lease unavailable')
        async with self.account_barrier.admitted(owner):
            if (not self.run_authorized() or self.auth_manager.account_id(owner) != account_id
                    or self.auth_manager.username_for_account_id(account_id) != owner
                    or self.local_state.obligation(account_id) != expected
                    or expected.get('consent') != 'accepted'):
                raise PermissionError('Emoji authority changed')
        candidates = await read_kitchen_metadata(client)
        for offset in range(0, len(candidates), 10000):
            await self.merge_expected(owner=owner, account_id=account_id, expected=expected,
                lease=lease, candidates=candidates[offset:offset+10000])
        return len(candidates)

    async def run_epoch(self, *, owner, account_id, expected, lease, client, epoch_id, limit=100, budget_bytes=64 * 1024 * 1024, deadline_seconds=120):
        """Run one bounded caller-admitted epoch; never reschedule or loop."""
        from .theme_emoji_discovery import SourceHTTPError, fetch_candidate
        import httpx
        import time
        from core.atomic_io import _path_locks
        from datetime import datetime, timezone

        if (type(limit) is not int or not 1 <= limit <= 1000
                or type(budget_bytes) is not int or not 1 <= budget_bytes <= 512 * 1024 * 1024
                or type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 300
                or not lease or not lease.valid() or not callable(self.run_authorized) or not self.run_authorized()):
            raise PermissionError('Emoji acquisition unavailable')
        if not isinstance(epoch_id, str) or not 1 <= len(epoch_id) <= 128:
            raise ValueError('Invalid acquisition epoch')
        candidates = await asyncio.to_thread(self.inventory.pending, limit, epoch_id=epoch_id)
        counts = {'ready':0, 'failed':0}
        received_bytes = 0
        stop_reason = None
        retry_after = None
        deadline = time.monotonic() + deadline_seconds
        for candidate in candidates:
            if time.monotonic() >= deadline:
                stop_reason = 'time_budget'
                break
            async with self.account_barrier.admitted(owner):
                if (not self.run_authorized() or self.auth_manager.account_id(owner) != account_id
                        or self.auth_manager.username_for_account_id(account_id) != owner
                        or self.local_state.obligation(account_id) != expected
                        or expected.get('consent') != 'accepted' or not lease.valid()):
                    raise PermissionError('Emoji authority changed')
            try:
                data, _ = await fetch_candidate(candidate, client)
                if received_bytes + len(data) > budget_bytes:
                    stop_reason = 'byte_budget'
                    break
                import shutil
                if shutil.disk_usage(self.inventory.path.parent).free < len(data) + 128 * 1024 * 1024:
                    stop_reason = 'disk_reserve'
                    break
                received_bytes += len(data)
                await self.publish(owner=owner, account_id=account_id, expected=expected,
                                   candidate=candidate, data=data, lease=lease, epoch_id=epoch_id)
                counts['ready'] += 1
            except (ValueError, OSError, httpx.HTTPError) as exc:
                # Failure metadata is also fenced and never retires the source.
                async with self.account_barrier.admitted(owner):
                    def record():
                        with _path_locks([str(self.local_state._owner_path(account_id))]):
                            if (not lease.valid() or not self.run_authorized() or self.auth_manager.account_id(owner) != account_id
                                    or self.auth_manager.username_for_account_id(account_id) != owner
                                    or self.local_state.obligation(account_id) != expected):
                                raise PermissionError('Emoji authority changed')
                            self.inventory.record_failure(candidate,
                                attempted_at=datetime.now(timezone.utc).isoformat(), error=str(exc)[:240] or 'unavailable', epoch_id=epoch_id)
                    task = asyncio.create_task(asyncio.to_thread(record))
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        await _drain_cancelled(task)
                counts['failed'] += 1
                if (isinstance(exc, SourceHTTPError) and exc.status in (429, 502, 503, 504)) or isinstance(exc, httpx.HTTPError):
                    stop_reason = 'rate_limited'
                    retry_after = exc.retry_after if isinstance(exc, SourceHTTPError) else 1200
                    break  # No automatic retry or rate-limit burst in this epoch.
        more = bool(await asyncio.to_thread(self.inventory.pending, 1, epoch_id=epoch_id))
        return {**counts, 'bytes':received_bytes, 'pending':more,
                'reason':stop_reason or ('batch_limit' if more else 'complete'), 'retryAfter':retry_after}

class InstallationLease:
    """Try the atomic-IO path lock once; a busy epoch never queues a worker."""
    def __init__(self, local_state):
        self._path = str(local_state.root / 'acquisition.lease')
        self._handle = None

    def valid(self):
        return self._handle is not None

    async def __aenter__(self):
        import hashlib
        import os
        import tempfile
        from core.atomic_io import _canonical_target
        lock_root = os.path.join(tempfile.gettempdir(), 'open-clank-file-locks')
        os.makedirs(lock_root, mode=0o700, exist_ok=True)
        key = os.path.normcase(_canonical_target(self._path))
        digest = hashlib.sha256(key.encode('utf-8', errors='surrogatepass')).hexdigest()
        handle = open(os.path.join(lock_root, digest + '.lock'), 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b'\0'); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            import errno
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RuntimeError('Emoji acquisition is busy') from exc
            raise
        self._handle = handle
        return self

    async def __aexit__(self, *exc):
        import os
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

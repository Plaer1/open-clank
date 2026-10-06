"""Repair only a proven original after-copy; never repeat a provider mutation."""
from __future__ import annotations
import asyncio
import hashlib
import json
from typing import Any, Awaitable, Callable


class CaptureRepairUnavailable(ValueError):
    pass


def _fingerprints(content: bytes) -> tuple[str, str]:
    digest = 'sha256:' + hashlib.sha256(content).hexdigest()
    return digest, f'{digest}:{len(content)}'


async def repair_capture(
    client: Any, action_id: str, *, account_id: str,
    copal_snapshot: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    file_snapshot: Callable[[str], Awaitable[bytes]],
    resource_snapshot: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    response = await asyncio.to_thread(client.get_capture_action, action_id)
    action = response.get('Action') or {}
    key = action.get('resource_key') or {}
    if action.get('action_id') != action_id or action.get('actor_account_id') != account_id or key.get('account_id') != account_id:
        raise CaptureRepairUnavailable('Recovery action is unavailable for this account.')
    if action.get('state') == 'Complete':
        return {'action_id': action_id, 'status': 'complete', 'message': 'Recovery copy already available.'}
    live = action.get('live') or {}
    if live.get('status') != 'Committed' or action.get('state') not in {'Applied', 'AfterCaptureFailed'}:
        raise CaptureRepairUnavailable('The live outcome requires reconciliation. No mutation was repeated.')
    proofs = live.get('committed_resources') or []
    if key.get('provider') == 'odysseus-files' and proofs:
        resources = action.get('before_resources') or []
        prepared = {json.dumps(item.get('resource_key'), sort_keys=True) for item in resources}
        committed = {json.dumps(item.get('resource_key'), sort_keys=True) for item in proofs}
        if not resource_snapshot or len(prepared) != len(resources) or len(committed) != len(proofs) or prepared != committed:
            raise CaptureRepairUnavailable('Recovery proof does not cover the exact prepared resource batch.')
        entries = []
        for proof in proofs:
            if proof['resource_key'].get('account_id') != account_id or proof['resource_key'].get('provider') != 'odysseus-files':
                raise CaptureRepairUnavailable('Recovery resource requires its original authorized owner.')
            snapshot = await resource_snapshot(proof)
            if snapshot.get('existence') != proof.get('existence'):
                raise CaptureRepairUnavailable('A saved resource changed after commit; the gap remains.')
            content = snapshot.get('content')
            if proof.get('existence') == 'Present':
                coverage = proof.get('coverage') or {}
                if not isinstance(content, bytes) or snapshot.get('resource_type') != proof.get('resource_type') or proof.get('fingerprint') not in _fingerprints(content) or coverage.get('content_digest') != _fingerprints(content)[0] or coverage.get('byte_len') != len(content):
                    raise CaptureRepairUnavailable('A saved resource changed after commit; the gap remains.')
                if snapshot.get('mode') != (proof.get('metadata') or {}).get('mode'):
                    raise CaptureRepairUnavailable('Saved resource permissions changed; the gap remains.')
            elif content is not None or proof.get('fingerprint') != 'missing':
                raise CaptureRepairUnavailable('The original tombstone proof is incomplete.')
            entry = dict(proof)
            entry['content'] = content
            entry['outcome'] = {'resource_id': proof['resource_key']['resource_id'], 'status': 'Committed', 'revision': {'Opaque': {'kind': 'fingerprint', 'value': proof['fingerprint']}}}
            entries.append(entry)
        await asyncio.to_thread(client.complete_batch, action_id, entries)
        return {'action_id': action_id, 'status': 'complete', 'message': 'Every recovery resource repaired from its original committed proof. No saved operation was repeated.'}
    expected = str(live.get('fingerprint') or '')
    if not expected or expected in {'missing', 'absent', 'present'}:
        raise CaptureRepairUnavailable('The original after-state has no verifiable revision. The recovery gap remains.')
    if key.get('provider') == 'copal' and not action.get('before_resources'):
        document = await copal_snapshot(key)
        if not isinstance(document, dict) or str(document.get('id') or '') != str(key.get('resource_id') or ''):
            raise CaptureRepairUnavailable('The original Copal revision is unavailable.')
        tool = action.get('tool_id')
        if tool == 'copal-route':
            document_bytes = {field: document[field] for field in ('id', 'name', 'kind', 'text', 'properties', 'relations', 'extensions', 'attachments', 'propertyDefinitions', 'tags', 'blocks') if field in document}
        elif tool in {'manage_copal', 'files-facade'}:
            document_bytes = document
        else:
            raise CaptureRepairUnavailable('This capture adapter has no verified repair encoding.')
        content = json.dumps(document_bytes, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')
        proven = expected in _fingerprints(content)
        # A native head proves commit content/name, but not mutable DocRecord
        # envelope metadata. Require the original whole-envelope digest.
        if not expected.startswith('sha256:'):
            raise CaptureRepairUnavailable('This action has only a native head revision; its full capture envelope requires provider reconciliation.')
        if not proven:
            raise CaptureRepairUnavailable('The document changed after the save. Newer content was not captured as the original revision.')
        await asyncio.to_thread(client.complete, action_id, content=content, fingerprint=expected)
    elif key.get('provider') == 'odysseus-files':
        resources = action.get('before_resources') or []
        if len(resources) != 1 or resources[0].get('resource_type') != 'File':
            raise CaptureRepairUnavailable('This capture needs per-resource reconciliation; the original fingerprints are incomplete.')
        resource = resources[0]
        if resource.get('resource_key') != key:
            raise CaptureRepairUnavailable('Recovery resource identity is incomplete.')
        locator = resource.get('new_locator') or resource.get('old_locator') or {}
        path = str(locator.get('location_label') or '')
        if not path:
            raise CaptureRepairUnavailable('The original resource locator is unavailable.')
        content = await file_snapshot(path)
        if expected not in _fingerprints(content):
            raise CaptureRepairUnavailable('The file changed after the save. Newer bytes were not captured as the original revision.')
        # This repairs content only. Never relabel today's timestamps/mode as
        # metadata observed at the original commit. Preserve prepared mode.
        entry = {
            'resource_key': key, 'locator': locator, 'existence': 'Present', 'resource_type': 'File',
            'metadata': {'mode': (resource.get('metadata') or {}).get('mode'), 'size': len(content), 'modified_millis': None, 'opaque': None},
            'content': content, 'fingerprint': expected,
            'coverage': {'byte_len': len(content), 'content_digest': _fingerprints(content)[0], 'metadata': {'exact_after': True, 'repair_content_only': True}},
            'outcome': {'resource_id': key['resource_id'], 'status': 'Committed', 'revision': {'Opaque': {'kind': 'fingerprint', 'value': expected}}},
        }
        await asyncio.to_thread(client.complete_batch, action_id, [entry])
    else:
        raise CaptureRepairUnavailable('This provider has no verified capture-repair adapter.')
    return {'action_id': action_id, 'status': 'complete', 'message': 'Recovery copy repaired. The saved operation was not repeated.'}


async def snapshot_native_resource(proof, *, authorize, read_file, max_bytes=10 * 1024 * 1024):
    """Bounded native canonical snapshot after current Files policy authorization.

    Every existing entry (and absent target's parent) is admitted by the real
    provider. Symlinks are payloads, never followed into an unauthorized tree.
    """
    import base64
    import os
    import stat
    path = str((proof.get('locator') or {}).get('location_label') or '')
    if not os.path.isabs(path) or '\x00' in path:
        raise CaptureRepairUnavailable('The original resource locator is unavailable.')
    await authorize(os.path.dirname(path))
    try:
        metadata = await asyncio.to_thread(os.lstat, path)
    except FileNotFoundError:
        return {'existence': 'Absent', 'content': None}
    budget = max_bytes
    count = 0

    async def observe(current):
        nonlocal budget, count
        count += 1
        if count > 2048:
            raise CaptureRepairUnavailable('Recovery directory exceeds the bounded snapshot limit.')
        await authorize(os.path.dirname(current) if os.path.islink(current) else current)
        observed = await asyncio.to_thread(os.lstat, current)
        if stat.S_ISLNK(observed.st_mode):
            payload = (await asyncio.to_thread(os.readlink, current)).encode('utf-8')
            kind = 'Symlink'
        elif stat.S_ISDIR(observed.st_mode):
            payload = None
            kind = 'Directory'
        elif stat.S_ISREG(observed.st_mode):
            payload = await read_file(current)
            kind = 'File'
        else:
            raise CaptureRepairUnavailable('This resource type has no canonical snapshot adapter.')
        budget -= len(payload or b'')
        if budget < 0:
            raise CaptureRepairUnavailable('Recovery snapshot exceeds the bounded byte limit.')
        return observed, kind, payload

    metadata, kind, payload = await observe(path)
    if kind == 'Directory':
        entries = []
        stack = [path]
        while stack:
            current = stack.pop()
            children = await asyncio.to_thread(lambda: sorted(os.listdir(current), reverse=True))
            for name in children:
                child = os.path.join(current, name)
                observed, child_kind, content = await observe(child)
                record = {'path': os.path.relpath(child, path).replace('\\', '/'), 'mode': observed.st_mode, 'mtime_millis': observed.st_mtime_ns // 1_000_000}
                if child_kind == 'Symlink':
                    record.update(type='symlink', target=content.decode('utf-8'))
                elif child_kind == 'Directory':
                    record['type'] = 'directory'
                    stack.append(child)
                else:
                    record.update(type='file', size=len(content), sha256=_fingerprints(content)[0], content=base64.b64encode(content).decode('ascii'))
                entries.append(record)
        payload = json.dumps({'version': 1, 'root_type': 'directory', 'entries': entries}, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        if len(payload) > max_bytes:
            raise CaptureRepairUnavailable('Recovery manifest exceeds the bounded byte limit.')
    return {'existence': 'Present', 'resource_type': kind, 'mode': metadata.st_mode, 'content': payload}

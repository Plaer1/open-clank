"""Owned, verified upload bytes for approved analysis snapshots.

The caller holds the bound utility lifetime and checks its asynchronous barrier
and domain policy before and after these synchronous operations.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
from dataclasses import dataclass

from starlette.datastructures import UploadFile
from src.openclank.account_request_barrier import AccountRequestBarrier, UtilityLifetime, _Admission
from src.openclank.analysis_snapshot_contract import MAX_MANIFEST_BYTES, decode_manifest, reference_for, validate_reference


@dataclass(frozen=True)
class SnapshotPublicationIdentity:
    """Host-derived original native request identity; never an access grant."""
    account: str
    request_key: str
    request_sha256: str


@dataclass(frozen=True)
class SnapshotClaim:
    upload_id: str
    manifest_sha256: str
    size_bytes: int
    pending_scope: str
    newly_created: bool


def _current(authority: UtilityLifetime) -> None:
    if (type(authority) is not UtilityLifetime or type(authority.barrier) is not AccountRequestBarrier
        or type(authority.admission) is not _Admission or authority.admission.utility is not authority
        or authority.admission.released or authority.settled or authority.admission.owner != authority.owner
        or not authority.account_current()):
        raise ValueError('snapshot upload authority unavailable')


def _read_nofollow(root: str, path: str, size: int) -> bytes:
    if not all(hasattr(os, name) for name in ('O_NOFOLLOW', 'O_DIRECTORY', 'O_NONBLOCK')):
        raise ValueError('verified upload reads unavailable')
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    if os.path.commonpath((root, path)) != root or path == root:
        raise ValueError('invalid upload path')
    parts = [p for p in root.split(os.sep) if p] + os.path.relpath(path, root).split(os.sep)
    if any(p in ('', '.', '..') for p in parts):
        raise ValueError('invalid upload path')
    directory = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = nxt
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                raise ValueError('snapshot upload changed')
            chunks = []
            remaining = size
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    raise ValueError('snapshot upload truncated')
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if identity(before) != identity(after) or os.read(fd, 1):
                raise ValueError('snapshot upload changed')
            return b''.join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def verify_snapshot_upload(handler, *, authority: UtilityLifetime, reference: dict) -> bytes:
    _current(authority)
    validate_reference(reference)
    size, digest, upload_id = reference['size_bytes'], reference['manifest_sha256'], reference['upload_id']
    if type(size) is not int or not 0 < size <= min(MAX_MANIFEST_BYTES, handler.max_upload_size):
        raise ValueError('snapshot upload size unavailable')
    with handler._index_guard():
        rows = [row for row in handler._load_upload_index(fail_on_error=True).values()
                if isinstance(row, dict) and row.get('id') == upload_id]
        if not rows:
            raise ValueError('snapshot upload unavailable')
        identities = {(row.get('owner'), row.get('path'), row.get('size'), row.get('hash'), row.get('checksum_sha256')) for row in rows}
        if len(identities) != 1:
            raise ValueError('ambiguous snapshot upload')
        owner, path, stored_size, stored_hash, checksum = next(iter(identities))
        if (owner != authority.owner or stored_size != size or stored_hash != digest
            or checksum not in (None, digest) or not isinstance(path, str)):
            raise ValueError('snapshot upload identity mismatch')
        data = _read_nofollow(handler.upload_dir, path, size)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('snapshot upload digest mismatch')
        validate_reference(reference, data)
        _current(authority)
        return data


def create_snapshot_upload(handler, *, authority: UtilityLifetime, canonical_bytes: bytes) -> dict:
    _current(authority)
    decode_manifest(canonical_bytes)
    if len(canonical_bytes) > handler.max_upload_size:
        raise ValueError('snapshot upload too large')
    saved = handler.save_upload(UploadFile(file=io.BytesIO(canonical_bytes), filename='analysis-snapshot.json'),
                                client_ip='analysis-snapshot', owner=authority.owner)
    reference = reference_for(canonical_bytes, saved['id'])
    verify_snapshot_upload(handler, authority=authority, reference=reference)
    return reference


def snapshot_pending_scope(*, authority: UtilityLifetime, identity: SnapshotPublicationIdentity) -> str:
    """Compute a selector only; callers must validate the original native receipt."""
    _current(authority)
    if (type(identity) is not SnapshotPublicationIdentity or identity.account != authority.account
        or not isinstance(identity.request_key, str) or not 1 <= len(identity.request_key.encode('utf-8')) <= 512
        or not isinstance(identity.request_sha256, str)
        or re.fullmatch(r'[0-9a-f]{64}', identity.request_sha256) is None):
        raise ValueError('snapshot publication identity unavailable')
    fields = [identity.account, 'analysis-snapshot-publication', identity.request_key, identity.request_sha256]
    return 'pending:ma4:' + hashlib.sha256(json.dumps(fields, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def claim_snapshot_upload(handler, *, authority: UtilityLifetime, reference: dict,
                          identity: SnapshotPublicationIdentity) -> SnapshotClaim:
    verify_snapshot_upload(handler, authority=authority, reference=reference)
    scope = snapshot_pending_scope(authority=authority, identity=identity)
    with handler._index_guard():
        _current(authority)
        index = handler._load_upload_index(fail_on_error=True)
        keys = [key for key, row in index.items() if isinstance(row, dict) and row.get('id') == reference['upload_id']]
        if not keys or any(index[key].get('owner') != authority.owner
            or index[key].get('hash') != reference['manifest_sha256']
            or index[key].get('size') != reference['size_bytes'] for key in keys):
            raise ValueError('snapshot claim unavailable')
        present = [scope in (index[key].get('reference_claims') or []) for key in keys]
        if any(present) and not all(present):
            raise ValueError('ambiguous snapshot claim')
        created = not any(present)
        if created:
            for key in keys:
                updated = dict(index[key])
                claims = set(updated.get('reference_claims') or [])
                claims.add(scope)
                updated['reference_claims'] = sorted(claims)
                updated['reference_generation'] = int(updated.get('reference_generation') or 0) + 1
                index[key] = updated
            _current(authority)
            handler._atomic_write_json(os.path.join(handler.upload_dir, 'uploads.json'), index, sync_backup=True)
    verify_snapshot_upload(handler, authority=authority, reference=reference)
    return SnapshotClaim(reference['upload_id'], reference['manifest_sha256'], reference['size_bytes'], scope, created)


def retire_snapshot_import_claims(*, reset_claim, staging_store, handler, claims,
                                  owner: str, account_id: str, provider, operation_id: str) -> None:
    """Retire frozen stage claims only under the existing positive reset claim."""
    from services.memory.nuke_coordinator import MemoryNukeUnavailable, MemoryResetClaim
    if type(reset_claim) is not MemoryResetClaim or type(claims) is not list or len(claims) > 4096:
        raise MemoryNukeUnavailable('snapshot staging retirement unavailable')
    def current():
        reset_claim.assert_current(owner=owner, account_id=account_id, provider=provider, operation_id=operation_id)
    current()
    staging_store.assert_snapshot_import_rows_removed(owner=owner, claims=claims)
    with handler._index_guard():
        current()
        index = handler._load_upload_index(fail_on_error=True)
        rows_by_id = {}
        for key, row in index.items():
            if isinstance(row, dict):
                rows_by_id.setdefault(row.get('id'), []).append(key)
        for claim in claims:
            if (type(claim) is not dict or set(claim) != {'item_id', 'account', 'upload_id', 'scope'}
                or claim['account'] != account_id or not isinstance(claim['scope'], str)
                or re.fullmatch(r'pending:ma4import:[0-9a-f]{64}', claim['scope']) is None):
                raise MemoryNukeUnavailable('snapshot staging inventory changed')
            keys = rows_by_id.get(claim['upload_id'], [])
            if not keys or any(index[key].get('owner') != owner for key in keys):
                raise MemoryNukeUnavailable('snapshot upload ownership changed')
            for key in keys:
                updated = dict(index[key])
                scopes = set(updated.get('reference_claims') or [])
                if claim['scope'] in scopes:
                    scopes.remove(claim['scope'])
                    updated['reference_claims'] = sorted(scopes)
                    updated['reference_generation'] = int(updated.get('reference_generation') or 0) + 1
                    index[key] = updated
        current()
        handler._atomic_write_json(os.path.join(handler.upload_dir, 'uploads.json'), index, sync_backup=True)

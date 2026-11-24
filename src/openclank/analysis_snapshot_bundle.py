"""Bounded snapshot attachment projection for the explicit portable bundle."""
from __future__ import annotations

import json
from src.openclank.analysis_snapshot_contract import decode_manifest, validate_reference
from src.openclank.analysis_snapshot_upload import verify_snapshot_upload
from src.openclank.attachment_inventory import resource_references

from services.memory.import_batch import MAX_BATCH_FILES

MAX_SNAPSHOTS = MAX_BATCH_FILES
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_VISITED = 100000
class SnapshotDescriptionLimitError(ValueError):
    pass


_MEMORY_KEYS = {'version', 'classification', 'capture_origin', 'description', 'dossier', 'manifest'}


def _snapshot_values(payload):
    """Find bounded exact snapshot values without treating them as authority."""
    pending = [payload]
    visited = 0
    selected = {}
    while pending:
        value = pending.pop()
        visited += 1
        if visited > MAX_VISITED:
            raise ValueError('snapshot export exceeds traversal limit')
        if isinstance(value, dict):
            if (set(value) == _MEMORY_KEYS and value.get('version') == 'openclank.analysis-snapshot-memory.v1'
                and value.get('classification') == 'reviewed_procedural_data'
                and value.get('capture_origin') in ('verified_capture', 'imported_unverified')):
                description = value.get('description')
                if (not isinstance(description, str) or not description or description != description.strip()
                    or len(description.encode('utf-8', 'strict')) > 2048
                    or any(ord(char) < 32 or ord(char) == 127 for char in description)):
                    raise ValueError('invalid snapshot description')
                manifest = value.get('manifest')
                if (type(manifest) is not dict or set(manifest) != {'type', 'attachment_id', 'reference'}
                    or manifest.get('type') != 'attachment_ref'):
                    raise ValueError('invalid snapshot attachment')
                ref = validate_reference(manifest.get('reference'))
                if manifest.get('attachment_id') != ref['upload_id'] or ('upload', ref['upload_id']) not in resource_references(manifest):
                    raise ValueError('snapshot attachment mismatch')
                old = selected.setdefault(ref['upload_id'], (ref, []))
                if old[0] != ref:
                    raise ValueError('ambiguous snapshot attachment')
                if value not in old[1]:
                    old[1].append(value)
                if sum(len(pair[1]) for pair in selected.values()) > MAX_SNAPSHOTS:
                    raise ValueError('too many snapshot values')
                if len(selected) > MAX_SNAPSHOTS:
                    raise ValueError('too many snapshot attachments')
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
        elif isinstance(value, str) and len(value) <= MAX_SNAPSHOT_BYTES and value.lstrip().startswith(('{', '[')):
            try:
                pending.append(json.loads(value))
            except (ValueError, RecursionError):
                pass
    return selected


def snapshot_bundle_members(payload, *, handler, authority):
    """Return verified portable members; source IDs are remapping selectors only."""
    selected = _snapshot_values(payload)
    members = []
    blobs = {}
    total = 0
    for upload_id, (ref, snapshot_values) in sorted(selected.items()):
        data = verify_snapshot_upload(handler, authority=authority, reference=ref)
        dossier = dict(decode_manifest(data)['dossier'])
        dossier.pop('result')
        if any(value.get('dossier') != dossier for value in snapshot_values):
            raise ValueError('snapshot dossier mismatch')
        digest = ref['manifest_sha256']
        if digest in blobs and blobs[digest] != data:
            raise ValueError('snapshot digest collision')
        if digest not in blobs:
            total += len(data)
            if total > MAX_SNAPSHOT_BYTES:
                raise ValueError('snapshot export exceeds byte limit')
            blobs[digest] = data
        members.append({'source_upload_id': upload_id, 'sha256': digest, 'size': len(data),
                        'member': f'snapshots/{digest}.json', 'reference': ref})
    return members, blobs


def imported_snapshot_members(archive, manifest):
    """Validate exact source members; imported provenance is always unverified."""
    import hashlib
    from src.openclank.analysis_snapshot_contract import validate_reference
    values = _snapshot_values(manifest.get('memory'))
    entries = manifest.get('snapshot_attachments')
    if type(entries) is not list or len(entries) != len(values) or len(entries) > MAX_SNAPSHOTS:
        raise ValueError('invalid snapshot members')
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError('duplicate bundle member')
    total = 0
    seen = set()
    grouped = {}
    blobs = {}
    for entry in entries:
        if type(entry) is not dict or set(entry) != {'source_upload_id', 'sha256', 'size', 'member', 'reference'}:
            raise ValueError('invalid snapshot member')
        source_id = entry['source_upload_id']
        if source_id in seen or source_id not in values:
            raise ValueError('invalid snapshot selector')
        seen.add(source_id)
        ref, snapshot_values = values[source_id]
        if entry['reference'] != ref or entry['sha256'] != ref['manifest_sha256'] or entry['size'] != ref['size_bytes']:
            raise ValueError('snapshot reference mismatch')
        member = f"snapshots/{ref['manifest_sha256']}.json"
        if entry['member'] != member or member not in names or archive.getinfo(member).file_size != ref['size_bytes']:
            raise ValueError('snapshot member unavailable')
        data = blobs.get(ref['manifest_sha256'])
        if data is None:
            data = archive.read(member)
            total += len(data)
            if total > MAX_SNAPSHOT_BYTES or hashlib.sha256(data).hexdigest() != ref['manifest_sha256']:
                raise ValueError('snapshot member mismatch')
            blobs[ref['manifest_sha256']] = data
        validate_reference(ref, data)
        dossier = dict(decode_manifest(data)['dossier'])
        dossier.pop('result')
        for value in snapshot_values:
            if value.get('dossier') != dossier:
                raise ValueError('snapshot dossier mismatch')
            description = value.get('description')
            if not isinstance(description, str) or not 1 <= len(description) <= 5000:
                raise ValueError('invalid snapshot description')
            group = grouped.setdefault(ref['manifest_sha256'], {'data': data, 'descriptions': set()})
            group['descriptions'].add(description)
    result = []
    for digest in sorted(grouped):
        group = grouped[digest]
        descriptions = sorted(group['descriptions'], key=lambda text: text.encode('utf-8'))
        description = descriptions[0] if len(descriptions) == 1 else (
            'Imported descriptions: ' + json.dumps(descriptions, ensure_ascii=False, separators=(',', ':')))
        if len(description.encode('utf-8')) > 2048:
            raise SnapshotDescriptionLimitError('combined snapshot descriptions exceed the review limit; select fewer snapshots')
        result.append((group['data'], description))
    return result

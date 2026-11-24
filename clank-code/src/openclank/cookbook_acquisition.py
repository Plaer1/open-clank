"""Bounded, closed review material for future public Cookbook acquisition.

Construction is not authorization or dispatch: callers must obtain every value
from current authenticated, server-resolved sources and revalidate at confirm.
"""
import hashlib
import json
import re

_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _text(value, maximum):
    if not isinstance(value, str) or not value or len(value.encode('utf-8')) > maximum:
        raise ValueError('invalid acquisition text')
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('invalid acquisition text')
    return value


def reviewed_request(*, manifest, selected_paths, destination):
    """Freeze exact selected public metadata and a server-pinned cache identity."""
    if not isinstance(manifest, dict) or not _REPO.fullmatch(manifest.get('repoID', '')):
        raise ValueError('invalid repository')
    revision = manifest.get('revision')
    if not isinstance(revision, str) or not _SHA.fullmatch(revision):
        raise ValueError('full immutable revision required')
    if manifest.get('gating') != {'state': 'ungated'}:
        raise ValueError('public ungated repository required')
    if not isinstance(selected_paths, list) or not 1 <= len(selected_paths) <= 100:
        raise ValueError('invalid file selection')
    if any(not isinstance(path, str) for path in selected_paths):
        raise ValueError('invalid selection')
    if len(set(selected_paths)) != len(selected_paths):
        raise ValueError('duplicate selection')
    available = manifest.get('files')
    if not isinstance(available, list) or len(available) > 100:
        raise ValueError('invalid manifest')
    indexed = {}
    for entry in available:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            raise ValueError('invalid manifest file')
        path = entry['path']
        if path in indexed:
            raise ValueError('ambiguous manifest file')
        indexed[path] = entry
    files = []
    for path in selected_paths:
        _text(path, 1024)
        if path.startswith('/') or '\\' in path or any(p in ('', '.', '..') for p in path.split('/')):
            raise ValueError('unsupported file path')
        entry = indexed.get(path)
        if entry is None or type(entry.get('size')) is not int or not 0 <= entry['size'] <= 2**53-1:
            raise ValueError('selected size unavailable')
        blob, digest = entry.get('blobID'), entry.get('lfsSHA256')
        if blob is not None and (not isinstance(blob, str) or not _SHA.fullmatch(blob)):
            raise ValueError('invalid blob metadata')
        if digest is not None and (not isinstance(digest, str) or not _HASH.fullmatch(digest)):
            raise ValueError('invalid LFS metadata')
        files.append({'path':path, 'size':entry['size'], 'blobID':blob, 'lfsSHA256':digest})
    license_data = manifest.get('license')
    if not isinstance(license_data, dict) or set(license_data) != {'value','source','status'}:
        raise ValueError('invalid license observation')
    if license_data['status'] == 'unknown':
        if license_data['value'] is not None or license_data['source'] is not None:
            raise ValueError('invalid unknown license')
    elif license_data['status'] == 'declared':
        _text(license_data['value'], 128)
        if license_data['source'] not in ('cardData','tag'):
            raise ValueError('invalid license source')
    else:
        raise ValueError('invalid license status')
    if not isinstance(destination, dict) or set(destination) != {'path','device','inode'}:
        raise ValueError('invalid cache identity')
    path = _text(destination['path'], 4096)
    if not path.startswith('/') or type(destination['device']) is not int or type(destination['inode']) is not int or destination['device'] < 0 or destination['inode'] <= 0:
        raise ValueError('invalid cache identity')
    result = {'version':1, 'repoID':manifest['repoID'], 'revision':revision,
              'files':sorted(files, key=lambda x:x['path']), 'license':dict(license_data),
              'gating':'ungated', 'destination':dict(destination)}
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',',':')).encode('utf-8')
    if len(encoded) > 131072:
        raise ValueError('review too large')
    return result, hashlib.sha256(encoded).hexdigest()


def validate_review(review):
    if not isinstance(review, dict) or set(review) != {'version','repoID','revision','files','license','gating','destination'} or type(review['version']) is not int or review['version'] != 1 or review['gating'] != 'ungated':
        raise ValueError('invalid stored review')
    files = review['files']
    if not isinstance(files, list):
        raise ValueError('invalid stored files')
    manifest = {'repoID':review['repoID'], 'revision':review['revision'], 'files':files,
                'license':review['license'], 'gating':{'state':'ungated'}}
    if any(not isinstance(f, dict) or set(f) != {'path','size','blobID','lfsSHA256'} for f in files):
        raise ValueError('invalid stored file')
    normalized, digest = reviewed_request(manifest=manifest, selected_paths=[f['path'] for f in files], destination=review['destination'])
    if normalized != review:
        raise ValueError('noncanonical stored review')
    return digest

"""Closed data projection for private reviewed snapshot publication.

These helpers provide no authority.  Callers must retain a current named-human
admission, memory policy and (for a capture) Files authority independently.
"""
from __future__ import annotations

import base64
import hashlib
import json

from src.openclank.analysis_snapshot_contract import canonical_json, decode_manifest, validate_reference, HOST_SCHEMA_VERSION

_DOSSIER_FIELDS = ('dossier_id','dossier_revision','source_id','source_revision','source_sha256',
                   'result_sha256','operation','provider','provider_sha256','provider_version',
                   'provider_evidence','version_evidence','interpretations')

_HOST_DOSSIER_FIELDS = ('dossier_id','dossier_revision','source_id','source_revision','source_sha256','result_sha256',
                        'operation','producer','producer_version','producer_evidence','coverage','payload_integrity','interpretations')


def publication_request(*, account, owner, workspace, origin, reference, manifest_bytes, description, policy):
    """Project verified manifest bytes into the bounded private native request."""
    validate_reference(reference, manifest_bytes)
    manifest = decode_manifest(manifest_bytes)
    if origin not in ('verified_capture', 'imported_unverified'):
        raise ValueError('snapshot origin unavailable')
    if origin == 'verified_capture' and manifest['account_id'] != account:
        raise ValueError('snapshot account changed')
    if not isinstance(description, str) or not description or description != description.strip() or len(description.encode('utf-8','strict')) > 2048 or any(ord(c)<32 or ord(c)==127 for c in description):
        raise ValueError('snapshot description unavailable')
    if any(not isinstance(x, str) or not x or x != x.strip() or len(x.encode('utf-8','strict')) > 512 for x in (account,owner,workspace)) or type(policy) is not dict:
        raise ValueError('snapshot authority unavailable')
    host = manifest['schema_version'] == HOST_SCHEMA_VERSION
    if host and origin != 'verified_capture': raise ValueError('snapshot origin unavailable')
    dossier = {key:manifest['dossier'][key] for key in (_HOST_DOSSIER_FIELDS if host else _DOSSIER_FIELDS)}
    identity = ['openclank.analysis-snapshot-copy.v2' if host else 'openclank.analysis-snapshot-copy.v1',account,owner,workspace,origin,reference['manifest_sha256'],reference['capture_scope_sha256']]
    key = 'ma4_' + hashlib.sha256(canonical_json(identity)).hexdigest()
    request = {'version':2 if host else 1,'owner':owner,'workspace_id':workspace,'account_id':account,'request_key':key,
               'description':description,'capture_origin':origin,'manifest':reference,'dossier':dossier,'policy_projection':policy}
    return request, hashlib.sha256(canonical_json(request)).hexdigest()


def saved_analysis(*, reference, manifest_bytes):
    """Read bounded retained result data after the caller proves current ownership."""
    validate_reference(reference, manifest_bytes)
    manifest = decode_manifest(manifest_bytes)
    result = manifest['dossier']['result']
    raw = base64.b64decode(result['data'], validate=True)
    if len(raw) != result['size_bytes'] or hashlib.sha256(raw).hexdigest() != result['sha256']:
        raise ValueError('saved analysis unavailable')
    return {'operation':manifest['dossier']['operation'],'result':json.loads(raw.decode('utf-8','strict'))}

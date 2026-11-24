"""Closed Host projection of one retained Source worktree inverse callback.

No public route lives here. The original worker, utility, Once binding and
private Files custody must remain current for every phase.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re

from src.openclank.git_worktree_inverse_owner import WorktreeInverseContext, inverse_current
from src.secret_storage import decrypt, encrypt

_HEX = re.compile(r'^[0-9a-f]{64}$')
_OID = re.compile(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
_FIELDS = {'originalOperationID', 'originalVersionID', 'adminName', 'adminIdentity', 'worktreeIdentity',
           'commonGitIdentity', 'worktreesIdentity', 'targetOID', 'treeOID', 'marker', 'backlink', 'head', 'index', 'locks'}


def _identity(value):
    if type(value) is not dict or set(value) != {'device', 'inode'}:
        return False
    for key in ('device', 'inode'):
        item = value[key]
        if type(item) is not str or not re.fullmatch(r'(?:0|[1-9][0-9]{0,19})', item) or int(item) > 2**64-1:
            return False
    return value['inode'] != '0'


def _file(value, limit):
    return bool(type(value) is dict and set(value) == {'device', 'inode', 'size', 'sha256'}
        and _identity({'device': value.get('device'), 'inode': value.get('inode')})
        and type(value.get('size')) is int and 1 <= value['size'] <= limit
        and type(value.get('sha256')) is str and _HEX.fullmatch(value['sha256']))


def _proof(context: WorktreeInverseContext, event):
    if type(event) is not dict or set(event) != _FIELDS | {'kind'}:
        raise ValueError('worktree inverse proof unavailable')
    proof = {key: event[key] for key in _FIELDS}
    original = context.original
    if type(original) is not dict or type(original.get('registration')) is not dict:
        raise ValueError('worktree original unavailable')
    registration = original['registration']
    if (proof['originalOperationID'] != context.original_operation_id
            or proof['originalVersionID'] != context.original_version_id
            or proof['adminName'] != registration.get('adminName')
            or proof['adminIdentity'] != registration.get('adminIdentity')
            or proof['worktreeIdentity'] != registration.get('privateIdentity')
            or proof['marker'] != registration.get('marker')
            or proof['backlink'] != original.get('finalBacklink')
            or proof['targetOID'] != original.get('targetOID')
            or proof['treeOID'] != original.get('treeOID')):
        raise ValueError('worktree inverse proof changed')
    if (type(proof['adminName']) is not str or not proof['adminName']
            or len(proof['adminName'].encode('utf-8', 'strict')) > 255
            or any(ord(ch) < 32 or ord(ch) == 127 or ch in '/\\' for ch in proof['adminName'])
            or proof['adminName'] in ('.', '..')
            or any(not _identity(proof[k]) for k in ('adminIdentity', 'worktreeIdentity', 'commonGitIdentity', 'worktreesIdentity'))
            or not _file(proof['backlink'], 4096) or not _file(proof['head'], 4096) or not _file(proof['index'], 8*1024*1024)
            or type(proof['targetOID']) is not str or not _OID.fullmatch(proof['targetOID'])
            or type(proof['treeOID']) is not str or not _OID.fullmatch(proof['treeOID'])
            or len(proof['treeOID']) != len(proof['targetOID'])):
        raise ValueError('worktree inverse proof invalid')
    head = (proof['targetOID'] + '\n').encode('ascii')
    if proof['head']['size'] != len(head) or proof['head']['sha256'] != hashlib.sha256(head).hexdigest():
        raise ValueError('worktree head changed')
    locks = proof['locks']
    if type(locks) is not dict or set(locks) != {'head', 'backlink', 'index'} or any(not _identity(v) for v in locks.values()):
        raise ValueError('worktree locks unavailable')
    if len({(v['device'], v['inode']) for v in locks.values()}) != 3:
        raise ValueError('worktree lock identity conflict')
    expected_git = context.preview['source']['physicalIndex']['git']
    if (type(expected_git) is not dict or type(expected_git.get('device')) is not int or type(expected_git.get('inode')) is not int
            or proof['commonGitIdentity'] != {'device': str(expected_git['device']), 'inode': str(expected_git['inode'])}):
        raise ValueError('worktree source identity changed')
    return proof


def _settle(context: WorktreeInverseContext) -> bool:
    """CAS only the exact original Once-backed pending receipt after ACKs."""
    utility, identity = context.utility, context.identity
    with context.repository._connect() as db:
        context.repository._begin(db)
        row = db.execute('SELECT request_digest,generation,receipt_ciphertext,lifecycle_phase FROM file_operations WHERE owner_subject_id=? AND operation_id=?',
            (utility.account, identity['operationID'])).fetchone()
        if (row is None or row['request_digest'] != identity['requestDigest']
                or row['generation'] != context.reservation_generation or row['lifecycle_phase'] != 'git_worktree_inverse_pending'):
            db.rollback(); return False
        old = row['receipt_ciphertext']
        try:
            pending = json.loads(decrypt(str(old)))
        except Exception:
            db.rollback(); return False
        if pending != context.pending_receipt:
            db.rollback(); return False
        receipt = dict(pending)
        receipt.update({'state': 'applied', 'settled': True, 'settlement': 'original_git_files_history_ack', 'cleanup': 'pending'})
        encoded = encrypt(json.dumps(receipt, sort_keys=True, separators=(',', ':'), ensure_ascii=False))
        changed = db.execute("UPDATE file_operations SET receipt_ciphertext=?,lifecycle_phase='git_worktree_inverse_settled' WHERE owner_subject_id=? AND operation_id=? AND request_digest=? AND generation=? AND lifecycle_phase='git_worktree_inverse_pending' AND receipt_ciphertext=?",
            (encoded, utility.account, identity['operationID'], identity['requestDigest'], context.reservation_generation, old))
        if changed.rowcount != 1:
            db.rollback(); return False
        db.commit(); return True


async def dispatch_inverse(context: WorktreeInverseContext, request):
    """Handle only one original authenticated Source invocation in strict order."""
    if type(context) is not WorktreeInverseContext or type(request) is not dict:
        return {'state': 'refused'}
    if set(request) != {'version', 'operationID', 'requestDigest', 'capability', 'invocationID', 'actionSHA256', 'gitPlanDigest', 'event'} or request.get('version') != 1:
        return {'state': 'refused'}
    if any(type(request.get(k)) is not str or not hmac.compare_digest(request[k], value)
           for k, value in context.identity.items()):
        return {'state': 'refused'}
    invocation = request.get('invocationID')
    if type(invocation) is not str or not re.fullmatch(r'[0-9a-f]{32}', invocation):
        return {'state': 'refused'}
    async with context.lock:
        if context.state in ('unknown', 'completed') or not await inverse_current(context.utility, context):
            context.state = 'unknown'; context.utility.host_cleanup_unknown = True
            return {'state': 'unknown'}
        try:
            if context.invocation is None:
                context.invocation = invocation
            elif not hmac.compare_digest(context.invocation, invocation):
                raise ValueError('worktree invocation changed')
            event = request['event']; proof = _proof(context, event); kind = event['kind']
            remaining = min(300000, max(0, int((context.deadline-asyncio.get_running_loop().time())*1000)))
            if remaining <= 0:
                raise ValueError('worktree inverse deadline')
            if kind == 'before_quarantine' and context.state == 'history_prepared':
                context.source_proof = json.loads(json.dumps(proof, sort_keys=True, separators=(',', ':'), ensure_ascii=False))
                result = await context.custody.exchange('worktree_inverse_quarantine', {**context.binding, 'timeout_ms': remaining})
                if result.get('data') != {'state': 'quarantined'} or not await inverse_current(context.utility, context):
                    raise ValueError('worktree quarantine unavailable')
                context.state = 'quarantined'
                return {'state': 'quarantined'}
            if proof != context.source_proof:
                raise ValueError('worktree proof changed')
            witness = {key: proof[key] for key in ('adminName', 'adminIdentity', 'commonGitIdentity', 'worktreesIdentity',
                       'targetOID', 'treeOID', 'head', 'backlink', 'index', 'locks')}
            if kind == 'before_admin_relocation' and context.state == 'quarantined':
                result = await context.custody.exchange('worktree_inverse_relocate_admin', {**context.binding, 'timeout_ms': remaining, 'witness': witness})
                if result.get('data') != {'state': 'admin_relocated'} or not await inverse_current(context.utility, context):
                    raise ValueError('worktree metadata unavailable')
                context.state = 'admin_relocated'
                return {'state': 'admin_relocated'}
            if kind == 'admin_relocated' and context.state == 'admin_relocated':
                result = await context.custody.exchange('worktree_inverse_finish', {**context.binding, 'timeout_ms': remaining, 'witness': witness})
                if result.get('data') != {'state': 'completed'}:
                    raise ValueError('worktree completion unavailable')
                if not await inverse_current(context.utility, context) or not _settle(context):
                    raise ValueError('worktree receipt unavailable')
                context.state = 'completed'
                return {'state': 'completed'}
            raise ValueError('worktree phase unavailable')
        except asyncio.CancelledError:
            context.state = 'unknown'; context.utility.cancel_requested = True; context.utility.host_cleanup_unknown = True
            raise
        except Exception:
            context.state = 'unknown'; context.utility.host_cleanup_unknown = True
            return {'state': 'unknown'}

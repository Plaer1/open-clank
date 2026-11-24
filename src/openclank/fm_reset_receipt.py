"""Read the committed SQL receipt for one already-dispatched guarded reset.

The caller owns the current reset claim and all post-commit cleanup. This module
does not start a provider, retry a reset, or establish that cleanup completed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from pathlib import Path
from typing import Callable

_MAX_REQUEST = 65536
_MAX_RECEIPT = 262144
_TIME_LIMIT = 2.0
_COMPONENTS = frozenset(('memories', 'graph', 'ingest'))


class ResetReceiptUnavailable(ValueError):
    """The exact committed receipt cannot be proved."""


def _json(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ResetReceiptUnavailable('reset receipt unavailable') from exc


def _parse(raw: str) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ResetReceiptUnavailable('reset receipt unavailable') from exc


def _identity(preparation) -> dict:
    from src.memery_database import GuardedStorePreparation, _GUARDED_PREPARATION_SEAL
    if type(preparation) is not GuardedStorePreparation or preparation._seal is not _GUARDED_PREPARATION_SEAL:
        raise ResetReceiptUnavailable('guarded database unavailable')
    path = preparation.database_path
    if (type(path) is not str or not path or len(path) > 4096 or not os.path.isabs(path)
            or os.path.realpath(path) != path or type(preparation.schema_version) is not int or preparation.schema_version not in (17, 18)
            or type(preparation.database_id) is not str or not preparation.database_id
            or len(preparation.database_id) > 512 or preparation.database_id.strip() != preparation.database_id
            or type(preparation.device) is not int or preparation.device < 0
            or type(preparation.inode) is not int or preparation.inode <= 0):
        raise ResetReceiptUnavailable('guarded database unavailable')
    try:
        if preparation.schema_version == 18: preparation._owned_move_schema_cookie()
        st = os.stat(path, follow_symlinks=False)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise ResetReceiptUnavailable('guarded database unavailable') from exc
    if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (preparation.device, preparation.inode):
        raise ResetReceiptUnavailable('guarded database changed')
    return {'path': path, 'database_id': preparation.database_id, 'device': preparation.device,
            'inode': preparation.inode, 'schema_version': preparation.schema_version}



def validate_selective_ingest_summary(value: object) -> dict:
    """Validate the final public count summary without performing I/O."""
    keys = {'version', 'state', 'unit', 'erasable_candidates', 'retained_approved_candidates',
            'retained_raw', 'retained_recovery', 'retained_quarantine', 'retained_jobs', 'partition_sha256'}
    if (type(value) is not dict or set(value) != keys
            or type(value['version']) is not int or value['version'] != 1
            or value['state'] != 'complete' or value['unit'] != 'candidate'
            or any(type(value[k]) is not int or not 0 <= value[k] <= 9007199254740991
                   for k in ('erasable_candidates', 'retained_approved_candidates'))
            or type(value['partition_sha256']) is not str
            or not re.fullmatch(r'[0-9a-f]{64}', value['partition_sha256'])):
        raise ResetReceiptUnavailable('selective ingest summary unavailable')
    for key, unit in (('retained_raw', 'raw_record'), ('retained_recovery', 'recovery_unit'),
                      ('retained_quarantine', 'quarantine_record'), ('retained_jobs', 'job_record')):
        entry = value[key]
        if (type(entry) is not dict or set(entry) != {'unit', 'count'} or entry['unit'] != unit
                or type(entry['count']) is not int or not 0 <= entry['count'] <= 9007199254740991):
            raise ResetReceiptUnavailable('selective ingest summary unavailable')
    return value


def _request(*, owner, account_id, operation_id, workspace_id, components, expected_counts, queued_retired, selective_manifest_sha256=None) -> dict:
    for value in (owner, account_id, operation_id, workspace_id):
        if type(value) is not str or not value or len(value) > 512 or value.strip() != value:
            raise ResetReceiptUnavailable('reset request unavailable')
        _json(value)  # Reject invalid Unicode before encoding for Rust's byte limit.
        if len(value.encode('utf-8')) > 512:
            raise ResetReceiptUnavailable('reset request unavailable')
    if (type(components) is not list or not 1 <= len(components) <= 3
            or any(type(v) is not str or v not in _COMPONENTS for v in components)
            or len(set(components)) != len(components) or type(queued_retired) is not bool):
        raise ResetReceiptUnavailable('reset request unavailable')
    expanded = set(components) | (_COMPONENTS if 'memories' in components else set())
    if type(expected_counts) is not dict or len(expected_counts) > 3 or set(expected_counts) != expanded:
        raise ResetReceiptUnavailable('reset request unavailable')
    for name, entry in expected_counts.items():
        if type(entry) is not dict:
            raise ResetReceiptUnavailable('reset request unavailable')
        keys = set(entry)
        selective = 'selective' in keys
        allowed = {'count', 'fingerprint'}
        if name in ('memories', 'ingest'):
            allowed.add('s05_quota_binding_sha256')
        if name == 'memories':
            allowed.add('s08_policy_binding_sha256')
        if name == 'ingest' and 'memories' not in expanded:
            allowed.add('selective')
        digests = ('s05_quota_binding_sha256', 's08_policy_binding_sha256')
        if (not {'count', 'fingerprint'} <= keys or not keys <= allowed
                or any(key in entry and (type(entry[key]) is not str
                    or not re.fullmatch(r'[0-9a-f]{64}', entry[key])) for key in digests)
                or type(entry.get('count')) is not int or not 0 <= entry['count'] <= 9007199254740991
                or type(entry.get('fingerprint')) is not str or not re.fullmatch(r'[0-9a-f]{64}', entry['fingerprint'])):
            raise ResetReceiptUnavailable('reset request unavailable')
        if selective:
            validate_selective_ingest_summary(entry['selective'])
    has_selective = 'ingest' in expected_counts and 'selective' in expected_counts['ingest']
    if (has_selective != (selective_manifest_sha256 is not None)
            or (has_selective and (queued_retired or type(selective_manifest_sha256) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', selective_manifest_sha256)))):
        raise ResetReceiptUnavailable('reset request unavailable')
    request = {'version': 1, 'owner': owner, 'account_id': account_id, 'operation_id': operation_id,
               'workspace_id': workspace_id, 'components': list(components), 'expected_counts': expected_counts}
    if queued_retired:
        request['queued_retired'] = True
    if selective_manifest_sha256 is not None:
        request['selective_manifest_sha256'] = selective_manifest_sha256
    if len(_json(request)) > _MAX_REQUEST:
        raise ResetReceiptUnavailable('reset request unavailable')
    return _parse(_json(request).decode('utf-8'))


def _open(preparation, expected: dict, deadline: float):
    if _identity(preparation) != expected:
        raise ResetReceiptUnavailable('guarded database changed')
    try:
        db = sqlite3.connect(Path(expected['path']).as_uri() + '?mode=ro', uri=True,
                             timeout=0.25, isolation_level=None)
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_RECEIPT + 8192)
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute('BEGIN')
        row = db.execute("SELECT value FROM fm_meta WHERE key='database_id' LIMIT 2").fetchall()
        version = db.execute('PRAGMA user_version').fetchone()
        if row != [(expected['database_id'],)] or version != (expected['schema_version'],):
            raise ResetReceiptUnavailable('guarded database changed')
        return db
    except (sqlite3.Error, OSError) as exc:
        if 'db' in locals():
            db.close()
        raise ResetReceiptUnavailable('guarded database unavailable') from exc
    except BaseException:
        if 'db' in locals():
            db.close()
        raise


def freeze_reset_receipt(preparation, *, owner, account_id, operation_id, workspace_id,
                         components, expected_counts, queued_retired, selective_manifest_sha256=None) -> dict:
    """Capture the exact original request and guarded identity before dispatch."""
    request = _request(owner=owner, account_id=account_id, operation_id=operation_id,
                       workspace_id=workspace_id, components=components,
                       expected_counts=expected_counts, queued_retired=queued_retired,
                       selective_manifest_sha256=selective_manifest_sha256)
    identity = _identity(preparation)
    deadline = time.monotonic() + _TIME_LIMIT
    db = _open(preparation, identity, deadline)
    try:
        if time.monotonic() >= deadline or _identity(preparation) != identity:
            raise ResetReceiptUnavailable('guarded database changed')
    finally:
        db.close()
    return {'version': 1, 'database': identity, 'request': request}


def read_committed_reset_receipt(preparation, frozen: dict, *, current: Callable[[], bool]) -> dict:
    """Return the exact SQL result with cleanup explicitly unproved, or refuse."""
    if not callable(current):
        raise ResetReceiptUnavailable('reset claim unavailable')
    if (type(frozen) is not dict or len(frozen) != 3 or set(frozen) != {'version', 'database', 'request'}
            or type(frozen['version']) is not int or frozen['version'] != 1
            or type(frozen['database']) is not dict or type(frozen['request']) is not dict):
        raise ResetReceiptUnavailable('reset receipt unavailable')
    request = frozen['request']
    allowed = {'version', 'owner', 'account_id', 'operation_id', 'workspace_id', 'components', 'expected_counts'}
    if (set(request) not in (allowed, allowed | {'queued_retired'}, allowed | {'selective_manifest_sha256'})
            or type(request.get('version')) is not int or request['version'] != 1
            or ('queued_retired' in request and request['queued_retired'] is not True)):
        raise ResetReceiptUnavailable('reset request unavailable')
    checked = _request(owner=request['owner'], account_id=request['account_id'], operation_id=request['operation_id'],
                       workspace_id=request['workspace_id'], components=request['components'],
                       expected_counts=request['expected_counts'], queued_retired=request.get('queued_retired', False),
                       selective_manifest_sha256=request.get('selective_manifest_sha256'))
    identity = _identity(preparation)
    saved = frozen['database']
    if (len(saved) != len(identity) or set(saved) != set(identity) or any(type(saved[k]) is not type(identity[k]) for k in identity)
            or checked != request or saved != identity or not _current(current)):
        raise ResetReceiptUnavailable('reset claim unavailable')
    # Only bounded, validated data crosses the read; caller-owned objects may
    # change concurrently without changing the receipt we compare.
    request = checked
    deadline = time.monotonic() + _TIME_LIMIT
    db = _open(preparation, identity, deadline)
    try:
        receipt_id = 'cs_reset_' + hashlib.sha256(_json([request['account_id'], request['operation_id']])).hexdigest()
        rows = db.execute("SELECT content_hash,operation_metadata_json FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=? AND reason='s06.guarded-reset.v1' AND length(CAST(operation_metadata_json AS BLOB))<=? LIMIT 2", (request['owner'], receipt_id, _MAX_RECEIPT)).fetchall()
        if len(rows) != 1 or type(rows[0][0]) is not str or type(rows[0][1]) is not str:
            raise ResetReceiptUnavailable('reset receipt unavailable')
        stored_hash, raw = rows[0]
        if hashlib.sha256(raw.encode('utf-8')).hexdigest() != stored_hash:
            raise ResetReceiptUnavailable('reset receipt unavailable')
        receipt = _parse(raw)
        if (type(receipt) is not dict or set(receipt) != {'version', 'request', 'request_sha256', 'result'}
                or receipt['version'] != 's06.guarded-reset.v1' or receipt['request'] != request
                or receipt['request_sha256'] != hashlib.sha256(_json(request)).hexdigest()):
            raise ResetReceiptUnavailable('reset receipt unavailable')
        result = receipt['result']
        if (type(result) is not dict or set(result) != {'complete', 'categories'} or result['complete'] is not True
                or type(result['categories']) is not dict or set(result['categories']) != set(request['expected_counts'])):
            raise ResetReceiptUnavailable('reset receipt unavailable')
        for name, expected in request['expected_counts'].items():
            if result['categories'][name] != {**expected, 'state': 'complete'}:
                raise ResetReceiptUnavailable('reset receipt unavailable')
        events = db.execute("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind='s06_guarded_reset' AND length(CAST(payload_json AS BLOB))<=? LIMIT 2", (request['owner'], 'outbox_' + receipt_id + '_s06_guarded_reset', receipt_id, _MAX_RECEIPT)).fetchall()
        if events != [(raw, 0)] or time.monotonic() >= deadline:
            raise ResetReceiptUnavailable('reset receipt unavailable')
        db.execute('COMMIT')
    except sqlite3.Error as exc:
        raise ResetReceiptUnavailable('reset receipt unavailable') from exc
    finally:
        db.close()
    if _identity(preparation) != identity or not _current(current) or time.monotonic() >= deadline:
        raise ResetReceiptUnavailable('reset claim unavailable')
    return {'state': 'committed', 'sql_result': result, 'cleanup': 'unproved'}


def _current(check: Callable[[], bool]) -> bool:
    try:
        return check() is True
    except Exception:
        return False

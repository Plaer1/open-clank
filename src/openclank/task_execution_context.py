"""Private, optional task-run witness; caller owns authorization and transaction."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3


class ExecutionContextUnavailable(ValueError):
    pass


# These are the persisted execution inputs, not mutable scheduling counters or
# a claim about the model eventually selected by an executor.
TASKSPEC_FIELDS = (
    'name', 'prompt', 'task_type', 'action', 'output_target', 'session_id',
    'workspace', 'workspace_id', 'copal_workspace', 'allowed_tools',
    'interaction_policy', 'max_tool_calls', 'crew_member_id', 'character_id',
    'max_steps', 'email_results', 'notifications_enabled',
)
MANUAL_TASK_FIELDS = TASKSPEC_FIELDS + (
    'schedule', 'scheduled_time', 'scheduled_day', 'scheduled_date', 'trigger_type',
    'trigger_event', 'trigger_count', 'cron_expression', 'then_task_id', 'provider_model_route_id',
    'endpoint_id', 'endpoint_url', 'model',
)
_KEYS = frozenset({'version', 'account_id', 'task_id', 'run_id', 'workspace_id', 'taskspec_sha256'})
_SHA = re.compile(r'^[0-9a-f]{64}$')


def _identifier(value):
    return isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 512 and value == value.strip() and '\x00' not in value


def profile_available(db: sqlite3.Connection) -> bool:
    """Recognize only the additive nullable TEXT column; never alter a DB."""
    columns = db.execute('PRAGMA table_info(task_runs)').fetchall()
    matches = [row for row in columns if row[1] == 'execution_context_json']
    return len(matches) == 1 and matches[0][2].upper() == 'TEXT' and matches[0][3:] == (0, None, 0)


def taskspec_sha256(values: dict) -> str:
    if set(values) != set(TASKSPEC_FIELDS):
        raise ExecutionContextUnavailable('task specification is incomplete')
    if any(value is not None and type(value) not in (str, int) for value in values.values()):
        raise ExecutionContextUnavailable('task specification has unsupported values')
    raw = json.dumps([values[k] for k in TASKSPEC_FIELDS], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > 1024 * 1024:
        raise ExecutionContextUnavailable('task specification is too large')
    return hashlib.sha256(raw).hexdigest()


def manual_task_sha256(task) -> str:
    """Freeze execution/configuration without mutable scheduler counters."""
    from datetime import datetime
    values = []
    for key in MANUAL_TASK_FIELDS:
        value = getattr(task, key)
        if isinstance(value, datetime): value = value.isoformat()
        if type(value) is bool: value = int(value)
        if value is not None and type(value) not in (str, int):
            raise ExecutionContextUnavailable('manual task configuration unavailable')
        values.append(value)
    raw = json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > 1024 * 1024: raise ExecutionContextUnavailable('manual task configuration too large')
    return hashlib.sha256(raw).hexdigest()


def _decode(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 4096:
        raise ExecutionContextUnavailable('execution context is invalid')
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ExecutionContextUnavailable('execution context is invalid') from exc
    if (not isinstance(obj, dict) or set(obj) != _KEYS or type(obj['version']) is not int
            or obj['version'] != 1 or any(not _identifier(obj[k]) for k in ('account_id', 'task_id', 'run_id', 'workspace_id'))
            or not isinstance(obj['taskspec_sha256'], str) or not _SHA.fullmatch(obj['taskspec_sha256'])):
        raise ExecutionContextUnavailable('execution context is invalid')
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    if raw != canonical:
        raise ExecutionContextUnavailable('execution context is not canonical')
    return obj


def read_context(db: sqlite3.Connection, *, task_id: str, run_id: str):
    if not profile_available(db):
        raise ExecutionContextUnavailable('execution context profile unavailable')
    row = db.execute('SELECT execution_context_json FROM task_runs WHERE id=? AND task_id=?', (run_id, task_id)).fetchone()
    if row is None or row[0] is None:
        return None
    result = _decode(row[0])
    if result['task_id'] != task_id or result['run_id'] != run_id:
        raise ExecutionContextUnavailable('execution context identity mismatch')
    return result


def write_context(db: sqlite3.Connection, *, account_id: str, owner: str, task_id: str,
                  run_id: str, workspace_id: str, expected_taskspec_sha256: str) -> str:
    """CAS within caller's existing IMMEDIATE transaction; never begin or commit.

    The caller must hold the original account admission and validate the actual
    current workspace binding immediately before calling. No username lookup or
    historical account assignment is performed here.
    """
    if not db.in_transaction or not profile_available(db):
        raise ExecutionContextUnavailable('execution context transaction/profile unavailable')
    if (any(not _identifier(v) for v in (account_id, owner, task_id, run_id, workspace_id))
            or not isinstance(expected_taskspec_sha256, str) or not _SHA.fullmatch(expected_taskspec_sha256)):
        raise ExecutionContextUnavailable('execution context input invalid')
    columns = ','.join(TASKSPEC_FIELDS)
    row = db.execute(f'SELECT owner,host_account_id,{columns} FROM scheduled_tasks WHERE id=?', (task_id,)).fetchone()
    if row is None or row[0] != owner or row[1] != account_id:
        raise ExecutionContextUnavailable('task account is unavailable')
    values = dict(zip(TASKSPEC_FIELDS, row[2:]))
    if values['workspace_id'] != workspace_id or taskspec_sha256(values) != expected_taskspec_sha256:
        raise ExecutionContextUnavailable('task specification changed')
    occurrence = db.execute('SELECT status FROM task_runs WHERE id=? AND task_id=?', (run_id, task_id)).fetchone()
    if occurrence is None or occurrence[0] not in ('running', 'outcome_unknown'):
        raise ExecutionContextUnavailable('task occurrence unavailable')
    obj = {'version': 1, 'account_id': account_id, 'task_id': task_id, 'run_id': run_id,
           'workspace_id': workspace_id, 'taskspec_sha256': expected_taskspec_sha256}
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    existing = read_context(db, task_id=task_id, run_id=run_id)
    if existing is not None:
        if existing == obj:
            return 'replay'
        raise ExecutionContextUnavailable('execution context conflict')
    result = db.execute('UPDATE task_runs SET execution_context_json=? WHERE id=? AND task_id=? AND execution_context_json IS NULL',
                        (raw, run_id, task_id))
    if result.rowcount != 1:
        raise ExecutionContextUnavailable('task occurrence unavailable')
    return 'inserted'

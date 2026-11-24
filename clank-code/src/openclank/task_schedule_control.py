"""Shared task scheduling control; the caller owns transaction and authority."""
from __future__ import annotations

import hashlib
import json
from sqlalchemy import LargeBinary, func
from core.database import ScheduledTask
from src.openclank.task_execution_context import MANUAL_TASK_FIELDS, manual_task_sha256
from src.openclank.task_observations import task_account_scope


def scoped_task(db, *, owner, account, task_id, workspace_id=None, include_legacy=False, bounded=True):
    q = db.query(ScheduledTask).filter(ScheduledTask.id == task_id,
        *task_account_scope(owner, account, include_legacy=include_legacy))
    if workspace_id is not None: q = q.filter(ScheduledTask.workspace_id == workspace_id)
    # Bound every input before ORM materializes the full specification.
    if bounded:
        for field in MANUAL_TASK_FIELDS:
            column = getattr(ScheduledTask, field)
            limit = 16384 if field == 'prompt' else 65536
            q = q.filter(func.length(func.cast(func.coalesce(column, ''), LargeBinary)) <= limit)
    return q.first()


def task_revision(task):
    payload = [task.id, task.owner, task.host_account_id, task.workspace_id, task.status, manual_task_sha256(task)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def delete_settled_task(db, *, owner, account, task_id, workspace_id, expected_revision):
    """Delete only an inspected LLM task with bounded, terminal history."""
    from core.database import TaskRun, TaskWaitRequest
    task = scoped_task(db, owner=owner, account=account, task_id=task_id, workspace_id=workspace_id)
    if not task or task.task_type != 'llm' or task.action or task_revision(task) != expected_revision:
        raise ValueError('task unavailable')
    if db.query(ScheduledTask.id).filter(ScheduledTask.then_task_id == task_id).first():
        raise ValueError('task has inbound links')
    runs = db.query(TaskRun.id, TaskRun.status).filter(TaskRun.task_id == task_id).limit(1025).all()
    waits = db.query(TaskWaitRequest.id, TaskWaitRequest.state).filter(TaskWaitRequest.task_id == task_id).limit(1025).all()
    if (len(runs) > 1024 or len(waits) > 1024
            or any(status not in ('success', 'error', 'skipped', 'aborted', 'cancelled') for _, status in runs)
            or any(state not in ('consumed', 'cancelled') for _, state in waits)):
        raise ValueError('task history is active or unavailable')
    if db.query(TaskWaitRequest.id).join(TaskRun, TaskWaitRequest.run_id == TaskRun.id).filter(
            TaskRun.task_id == task_id, TaskWaitRequest.task_id != task_id).first():
        raise ValueError('task history linkage is unavailable')
    if db.query(TaskWaitRequest.id).outerjoin(TaskRun, TaskWaitRequest.run_id == TaskRun.id).filter(
            TaskWaitRequest.task_id == task_id,
            (TaskRun.id.is_(None) | (TaskRun.task_id != task_id))).first():
        raise ValueError('task history linkage is unavailable')
    db.query(TaskWaitRequest).filter(TaskWaitRequest.task_id == task_id).delete(synchronize_session=False)
    db.query(TaskRun).filter(TaskRun.task_id == task_id).delete(synchronize_session=False)
    db.query(ScheduledTask).filter(ScheduledTask.id == task_id).delete(synchronize_session=False)


def apply_schedule(db, *, owner, account, task_id, action, workspace_id=None,
                   expected_revision=None, include_legacy=False, strict_schedule=False):
    """Apply within caller's already-open transaction; no running-work control."""
    from src.task_scheduler import compute_next_run
    task = scoped_task(db, owner=owner, account=account, task_id=task_id,
                       workspace_id=workspace_id, include_legacy=include_legacy, bounded=expected_revision is not None)
    if not task: raise ValueError('task unavailable')
    if expected_revision is not None and task_revision(task) != expected_revision:
        raise ValueError('task revision changed')
    if action == 'pause':
        task.status = 'paused'
    elif action == 'resume':
        if (task.trigger_type or 'schedule') == 'schedule':
            next_run = compute_next_run(task.schedule, task.scheduled_time, task.scheduled_day,
                                        task.scheduled_date, cron_expression=task.cron_expression)
            if next_run is None and strict_schedule: raise ValueError('schedule has no future occurrence')
            task.next_run = next_run
        elif strict_schedule:
            raise ValueError('schedule unavailable')
        task.status = 'active'
    else:
        raise ValueError('unsupported scheduling action')
    return task


def apply_content_edit(db, *, owner, account, task_id, workspace_id, expected_revision, fields):
    """Edit only bounded LLM content inside the caller's IMMEDIATE transaction."""
    from core.database import TaskRun
    if not isinstance(fields, dict) or not fields or set(fields) - {'name', 'prompt'}:
        raise ValueError('task edit unavailable')
    for key, value in fields.items():
        if (not isinstance(value, str) or not value.strip()
                or len(value.encode('utf-8')) > (200 if key == 'name' else 16384)):
            raise ValueError('task edit unavailable')
    task = scoped_task(db, owner=owner, account=account, task_id=task_id, workspace_id=workspace_id)
    if not task or task.task_type != 'llm' or task_revision(task) != expected_revision:
        raise ValueError('task edit unavailable')
    # Unknown run states are not evidence that an issued run has settled.
    terminal_or_queued = ('queued', 'success', 'error', 'skipped', 'aborted', 'cancelled')
    if db.query(TaskRun.id).filter(TaskRun.task_id == task_id,
            (TaskRun.status.is_(None) | ~TaskRun.status.in_(terminal_or_queued))).first():
        raise ValueError('task edit unavailable while a run is active or unknown')
    for key, value in fields.items():
        setattr(task, key, value)
    return task

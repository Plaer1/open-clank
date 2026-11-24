"""Bounded current-workspace task reads under caller-held authority."""
from __future__ import annotations

import time
from sqlalchemy import LargeBinary, case, func
from core.database import ScheduledTask
from src.openclank.orchestration_projection import decode_scheduler_observation_cursor, project_scheduler_observations
from src.openclank.task_observations import task_account_scope


def read_tasks(db, *, owner, account, workspace_id, policy_generation, action, task_id, cursor, limit, cursor_scope):
    if not workspace_id or db.get_bind().dialect.name != 'sqlite':
        raise ValueError('task read unavailable')
    after = decode_scheduler_observation_cursor(owner, workspace_id, 'scheduler_routine', cursor,
        account_id=cursor_scope, policy_generation=policy_generation)
    fields = (ScheduledTask.id, ScheduledTask.name, ScheduledTask.status, ScheduledTask.task_type,
              ScheduledTask.schedule, ScheduledTask.scheduled_time, ScheduledTask.scheduled_day, ScheduledTask.scheduled_date)
    # SQL CASE prevents oversized prompt materialization; NULL is refused below.
    prompt = case((func.length(func.cast(ScheduledTask.prompt, LargeBinary)) <= 16384,
                   ScheduledTask.prompt), else_=None)
    prompt_size = func.length(func.cast(ScheduledTask.prompt, LargeBinary))
    query = db.query(*fields, prompt if action == 'inspect' else ScheduledTask.id,
                     prompt_size if action == 'inspect' else ScheduledTask.id).filter(
        *task_account_scope(owner, account), ScheduledTask.workspace_id == workspace_id,
        func.length(func.cast(ScheduledTask.id, LargeBinary)) <= 512,
        func.length(func.cast(ScheduledTask.name, LargeBinary)) <= 200,
        func.length(func.cast(func.coalesce(ScheduledTask.status, ''), LargeBinary)) <= 128,
        func.length(func.cast(func.coalesce(ScheduledTask.task_type, ''), LargeBinary)) <= 128,
        func.length(func.cast(func.coalesce(ScheduledTask.schedule, ''), LargeBinary)) <= 128,
        func.length(func.cast(func.coalesce(ScheduledTask.scheduled_time, ''), LargeBinary)) <= 128)
    if action == 'inspect': query = query.filter(ScheduledTask.id == task_id)
    elif action == 'list' and after: query = query.filter(ScheduledTask.id > after)
    elif action != 'list': raise ValueError('task read unavailable')
    raw = db.connection().connection.driver_connection
    deadline = time.monotonic() + .15; calls = [0]
    def budget():
        calls[0] += 1
        return int(calls[0] > 1000 or time.monotonic() > deadline)
    raw.set_progress_handler(budget, 1000)
    try:
        rows = query.order_by(ScheduledTask.id).limit((limit + 1) if action == 'list' else 2).all()
        if action == 'inspect' and len(rows) != 1: raise ValueError('task unavailable')
        items = []
        for row in rows[:limit if action == 'list' else 1]:
            values = [row.id, row.name, row.status, row.task_type, row.schedule, row.scheduled_time]
            if any(value is not None and (not isinstance(value, str) or len(value.encode('utf-8')) > cap)
                   for value, cap in zip(values, (512, 200, 128, 128, 128, 128))):
                raise ValueError('task value unavailable')
            if action == 'inspect' and row[-1] is not None and row[-1] > 16384:
                raise ValueError('task prompt unavailable')
            items.append({'taskID':row.id, 'name':row.name, 'status':row.status, 'taskType':row.task_type,
                'schedule':row.schedule, 'scheduledTime':row.scheduled_time, 'scheduledDay':row.scheduled_day,
                'scheduledDate':row.scheduled_date.isoformat() + 'Z' if row.scheduled_date else None,
                'prompt':row[-2] if action == 'inspect' else None})
        if action == 'inspect':
            from src.openclank.task_schedule_control import scoped_task, task_revision
            task = scoped_task(db, owner=owner, account=account, task_id=task_id, workspace_id=workspace_id)
            if not task: raise ValueError('task unavailable')
            items[0]['_revision'] = task_revision(task)
        next_key = rows[limit - 1].id if action == 'list' and len(rows) > limit else None
        page = project_scheduler_observations(owner, workspace_id, 'scheduler_routine', [], time.time_ns() // 1_000_000,
            next_key, account_id=cursor_scope, policy_generation=policy_generation)
        return {'state':'partial', 'items':items, 'cursor':page['sources']['scheduler_routine'].get('nextCursor')}
    finally:
        raw.set_progress_handler(None, 0)

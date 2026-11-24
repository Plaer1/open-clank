"""Private task creation under a caller-held current account/actor admission."""
from __future__ import annotations

import hashlib
import json


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def create_or_status(*, account, owner, chat, workspace_id, copal_workspace, allowed_tools, route_id, provider_binding, identity, args, args_sha256, allow_insert):
    """One existing task row is the receipt; caller owns freshness and barrier.

    A missing row on a non-inserting request is unknown, never proof of nonissue.
    This helper does no authorization or native-source lookup on its own.
    """
    from sqlalchemy import text
    from core.database import SessionLocal, ScheduledTask, Session
    from src.task_scheduler import compute_next_run
    from src.openclank.chat_routing import resolve_chat_route, MANAGED_ENGINE_PUBLIC_URL

    identity_raw = _canonical(['openclank.managed-task-id.v1', account, identity])
    task_id = 'managed-task-' + hashlib.sha256(identity_raw.encode('utf-8')).hexdigest()
    witness = _canonical({'version': 1, 'account_id': account, 'identity_sha256': hashlib.sha256(identity_raw.encode('utf-8')).hexdigest(),
                          'args_sha256': args_sha256, 'source_chat': chat, 'workspace_id': workspace_id,
                          'copal_workspace': copal_workspace, 'allowed_tools': allowed_tools, 'route_id': route_id,
                          'provider_binding': provider_binding})
    if len(witness.encode('utf-8')) > 4096:
        raise ValueError('task creation unavailable')
    with SessionLocal() as db:
        if db.get_bind().dialect.name != 'sqlite':
            raise ValueError('task creation profile unavailable')
        db.connection().exec_driver_sql('BEGIN IMMEDIATE' if allow_insert else 'BEGIN')
        columns = db.connection().exec_driver_sql('PRAGMA table_info(scheduled_tasks)').fetchall()
        matches = [row for row in columns if row[1] == 'creation_witness_json']
        if len(matches) != 1 or matches[0][2].upper() != 'TEXT' or tuple(matches[0][3:]) != (0, None, 0):
            raise ValueError('task creation profile unavailable')
        source = db.query(Session).filter_by(id=chat, owner=owner).first()
        if (not source or source.archived or source.workspace_id != workspace_id or source.provider_model_route_id != route_id):
            raise ValueError('task source unavailable')
        existing = db.connection().execute(text('SELECT host_account_id, creation_witness_json FROM scheduled_tasks WHERE id=:id'), {'id': task_id}).fetchone()
        if existing is not None:
            if existing[0] != account or existing[1] != witness:
                raise ValueError('task creation conflict')
            return {'taskID': task_id, 'state': 'already_created'}
        if not allow_insert:
            return {'taskID': task_id, 'state': 'unknown'}
        route = resolve_chat_route(owner=owner, model_route_id=source.provider_model_route_id,
                                   endpoint_id=source.endpoint_id or None)
        scheduled_date = None
        if args['schedule'] == 'once':
            from datetime import datetime
            scheduled_date = datetime.strptime(args['scheduled_date'], '%Y-%m-%dT%H:%M:%SZ')
        next_run = compute_next_run(args['schedule'], args.get('scheduled_time'), args.get('scheduled_day'), scheduled_date)
        if next_run is None:
            raise ValueError('task schedule unavailable')
        task = ScheduledTask(id=task_id, owner=owner, host_account_id=account, name=args['name'],
            prompt=args['prompt'], task_type='llm', trigger_type='schedule', schedule=args['schedule'],
            scheduled_time=args.get('scheduled_time'), scheduled_day=args.get('scheduled_day'), scheduled_date=scheduled_date,
            next_run=next_run, status='active', output_target='session',
            endpoint_id=route.public_endpoint_id, endpoint_url=MANAGED_ENGINE_PUBLIC_URL, model=route.provider_model_id,
            provider_model_route_id=route.model_route_id, workspace_id=workspace_id, copal_workspace=copal_workspace,
            allowed_tools=_canonical(allowed_tools), interaction_policy='fail_on_interaction', max_tool_calls=20)
        db.add(task)
        db.flush()
        result = db.connection().execute(text('UPDATE scheduled_tasks SET creation_witness_json=:w WHERE id=:id AND creation_witness_json IS NULL'),
                                         {'w': witness, 'id': task_id})
        if result.rowcount != 1:
            raise ValueError('task creation unavailable')
        db.commit()
        return {'taskID': task_id, 'state': 'created'}

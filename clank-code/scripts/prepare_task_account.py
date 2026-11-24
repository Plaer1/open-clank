#!/usr/bin/env python3
"""Prepare a separate SQLite host backup and task-account shadow; never replace source."""
import argparse
import os
import sqlite3
from pathlib import Path


OLD_TASK_COLUMNS = frozenset({
    'id','owner','name','prompt','task_type','action','schedule','scheduled_time','scheduled_day',
    'scheduled_date','trigger_type','trigger_event','trigger_count','trigger_counter','next_run',
    'last_run','status','output_target','session_id','model','endpoint_url','endpoint_id',
    'provider_model_route_id','workspace','workspace_id','copal_workspace','allowed_tools',
    'interaction_policy','max_tool_calls','run_count','cron_expression','then_task_id','webhook_token',
    'crew_member_id','character_id','max_steps','email_results','notifications_enabled','created_at','updated_at',
})


_TEXT = {'prompt', 'allowed_tools'}
_INTEGER = {'scheduled_day', 'trigger_count', 'trigger_counter', 'max_tool_calls', 'run_count', 'max_steps'}
_DATETIME = {'scheduled_date', 'next_run', 'last_run', 'created_at', 'updated_at'}
_BOOLEAN = {'email_results', 'notifications_enabled'}
_REQUIRED = {'id', 'name', 'copal_workspace', 'allowed_tools', 'interaction_policy', 'max_tool_calls', 'created_at', 'updated_at'}


def old_task_profile(db):
    columns = db.execute('PRAGMA table_info(scheduled_tasks)').fetchall()
    names = [row[1] for row in columns]
    if len(names) != len(OLD_TASK_COLUMNS) or set(names) != OLD_TASK_COLUMNS:
        raise ValueError('old task profile unavailable')
    for _, name, kind, required, default, primary in columns:
        expected = ('TEXT' if name in _TEXT else 'INTEGER' if name in _INTEGER else
                    'DATETIME' if name in _DATETIME else 'BOOLEAN' if name in _BOOLEAN else 'VARCHAR')
        if (kind.upper() != expected or required != int(name in _REQUIRED)
                or default is not None or primary != int(name == 'id')):
            raise ValueError('old task column profile unavailable')
    foreign = {(r[2],r[3],r[4],r[5],r[6]) for r in db.execute('PRAGMA foreign_key_list(scheduled_tasks)')}
    if foreign != {('sessions','session_id','id','NO ACTION','SET NULL'),
                   ('scheduled_tasks','then_task_id','id','NO ACTION','SET NULL')}:
        raise ValueError('old task foreign-key profile unavailable')
    return columns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'backup', 'shadow'):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    paths = (args.source, args.backup, args.shadow)
    if any(not p.is_absolute() or p.is_symlink() for p in paths):
        raise ValueError('distinct absolute non-symlink paths required')
    source = args.source.resolve(strict=True)
    backup, shadow = (p.resolve(strict=False) for p in (args.backup, args.shadow))
    if len({source, backup, shadow}) != 3 or not source.is_file() or backup.exists() or shadow.exists():
        raise ValueError('existing source and two new destinations required')
    identity = source.stat()
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
        if src.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('source integrity unavailable')
        columns = old_task_profile(src)
        version = src.execute('PRAGMA data_version').fetchone()[0]
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        with sqlite3.connect(backup) as dst:
            src.backup(dst)
        after = source.stat()
        if (identity.st_dev, identity.st_ino) != (after.st_dev, after.st_ino) or src.execute('PRAGMA data_version').fetchone()[0] != version:
            raise ValueError('source changed during backup')
    fd = os.open(shadow, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(fd)
    with sqlite3.connect(backup.as_uri() + '?mode=ro', uri=True) as saved, sqlite3.connect(shadow) as dst:
        if saved.execute('PRAGMA integrity_check').fetchone() != ('ok',) or old_task_profile(saved) != columns:
            raise ValueError('captured task profile changed')
        count = saved.execute('SELECT count(*) FROM scheduled_tasks').fetchone()[0]
        saved.backup(dst)
        with dst:
            dst.execute('ALTER TABLE scheduled_tasks ADD COLUMN host_account_id VARCHAR')
        if (dst.execute('PRAGMA table_info(scheduled_tasks)').fetchall()[:-1] != columns
                or dst.execute('PRAGMA table_info(scheduled_tasks)').fetchall()[-1][1:3] != ('host_account_id', 'VARCHAR')
                or dst.execute('SELECT count(*) FROM scheduled_tasks WHERE host_account_id IS NULL').fetchone()[0] != count
                or dst.execute('PRAGMA integrity_check').fetchone() != ('ok',)
                or dst.execute('PRAGMA foreign_key_check').fetchone() is not None):
            raise ValueError('prepared task shadow unavailable')
    print('Prepared separate task-account backup and shadow; source was not replaced. Legacy account values remain unknown.')


if __name__ == '__main__':
    main()

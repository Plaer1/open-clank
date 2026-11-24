#!/usr/bin/env python3
"""Prepare an offline task-run provenance shadow; never modify the source."""
import argparse
import os
import sqlite3
from pathlib import Path


def run_profile(db):
    columns = db.execute('PRAGMA table_info(task_runs)').fetchall()
    expected = {
        'id': ('VARCHAR', 1, 1), 'task_id': ('VARCHAR', 1, 0),
        'started_at': ('DATETIME', 1, 0), 'finished_at': ('DATETIME', 0, 0),
        'status': ('VARCHAR', 0, 0), 'result': ('TEXT', 0, 0),
        'error': ('TEXT', 0, 0), 'tokens_used': ('INTEGER', 0, 0),
        'steps': ('TEXT', 0, 0), 'model': ('VARCHAR', 0, 0),
    }
    if len(columns) != len(expected):
        raise ValueError('task-run profile unavailable')
    for _, name, kind, required, default, primary in columns:
        if name not in expected or (kind.upper(), required, primary) != expected[name] or default is not None:
            raise ValueError('task-run profile unavailable')
    foreign = {(r[2], r[3], r[4], r[5], r[6]) for r in db.execute('PRAGMA foreign_key_list(task_runs)')}
    if foreign != {('scheduled_tasks', 'task_id', 'id', 'NO ACTION', 'CASCADE')}:
        raise ValueError('task-run foreign-key profile unavailable')
    task_columns = {row[1] for row in db.execute('PRAGMA table_info(scheduled_tasks)')}
    if 'host_account_id' not in task_columns:
        raise ValueError('task-account profile unavailable')
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
        columns = run_profile(src)
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
        if saved.execute('PRAGMA integrity_check').fetchone() != ('ok',) or run_profile(saved) != columns:
            raise ValueError('captured task-run profile changed')
        count = saved.execute('SELECT count(*) FROM task_runs').fetchone()[0]
        saved.backup(dst)
        with dst:
            dst.execute('ALTER TABLE task_runs ADD COLUMN execution_context_json TEXT')
        prepared = dst.execute('PRAGMA table_info(task_runs)').fetchall()
        if (prepared[:-1] != columns or prepared[-1][1:] != ('execution_context_json', 'TEXT', 0, None, 0)
                or dst.execute('SELECT count(*) FROM task_runs WHERE execution_context_json IS NULL').fetchone()[0] != count
                or dst.execute('PRAGMA integrity_check').fetchone() != ('ok',)
                or dst.execute('PRAGMA foreign_key_check').fetchone() is not None):
            raise ValueError('prepared task-run shadow unavailable')
    print('Prepared separate task-run backup and shadow; legacy execution contexts remain unknown.')


if __name__ == '__main__':
    main()

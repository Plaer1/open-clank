#!/usr/bin/env python3
"""Prepare a separate Host source-profile backup and shadow; never replace source.

All writers must be stopped before use. Legacy messages are not baselined and
no capture admission, privacy decision or source event is created.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.openclank.host_source_schema import convert, upgrade_origin_producer, upgrade_execution_snapshot, install_reset_root_index, install_assistant_reverse_index, require_current_connection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('backup', type=Path)
    parser.add_argument('shadow', type=Path)
    parser.add_argument('--writers-stopped', required=True, action='store_true')
    parser.add_argument('--response-style', action='store_true', help='Prepare response-style columns on the new shadow only.')
    args = parser.parse_args()
    paths = (args.source, args.backup, args.shadow)
    if any(not p.is_absolute() or p.is_symlink() for p in paths):
        raise ValueError('absolute non-symlink paths required')
    source = args.source.resolve(strict=True)
    backup, shadow = args.backup.resolve(strict=False), args.shadow.resolve(strict=False)
    if len({source, backup, shadow}) != 3 or not source.is_file() or backup.exists() or shadow.exists():
        raise ValueError('existing source and distinct new backup and shadow required')
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
        if src.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('source integrity unavailable')
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        with sqlite3.connect(backup) as dst:
            src.backup(dst)
    fd = os.open(shadow, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(fd)
    with sqlite3.connect(backup.as_uri() + '?mode=ro', uri=True) as saved, sqlite3.connect(shadow) as dst:
        if saved.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('backup integrity unavailable')
        saved.backup(dst)
    with sqlite3.connect(shadow) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if 'chat_messages' not in tables:
            raise ValueError('Host source schema unavailable')
        if 'host_source_schema_marker' not in tables:
            version = 0
        else:
            rows = db.execute('SELECT version FROM host_source_schema_marker').fetchall()
            if rows not in ([(1,)], [(2,)], [(3,)]):
                raise ValueError('Host source profile unavailable')
            version = rows[0][0]
    if version == 0:
        convert(str(shadow)); version = 1
    if version == 1:
        upgrade_origin_producer(str(shadow)); version = 2
    if version == 2:
        upgrade_execution_snapshot(str(shadow)); version = 3
    install_reset_root_index(str(shadow))
    install_assistant_reverse_index(str(shadow))
    if args.response_style:
        from src.openclank.response_style import prepare_response_style_schema
        with sqlite3.connect(shadow) as db:
            prepare_response_style_schema(db)
    with sqlite3.connect(shadow.as_uri() + '?mode=ro', uri=True) as db:
        if (db.execute('PRAGMA integrity_check').fetchone() != ('ok',)
                or db.execute('PRAGMA foreign_key_check').fetchone() is not None
                or db.execute('SELECT version FROM host_source_schema_marker').fetchall() != [(3,)]):
            raise ValueError('prepared Host source profile unavailable')
    from sqlalchemy import create_engine
    from urllib.parse import quote
    readonly = create_engine('sqlite:///file:' + quote(str(shadow), safe='/') + '?mode=ro&uri=true')
    try:
        with readonly.connect() as connection:
            require_current_connection(connection)
    finally:
        readonly.dispose()
    print('Prepared separate Host backup and source-profile shadow; source was not changed.')


if __name__ == '__main__':
    main()

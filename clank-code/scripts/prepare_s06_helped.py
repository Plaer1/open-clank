#!/usr/bin/env python3
"""Prepare a separate S06 helped profile copy; never activate or replace the source."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / 'native/memery/crates/memery-core/src/store'


def definition(db, name):
    row = db.execute("SELECT sql FROM sqlite_schema WHERE type='index' AND name=?", (name,)).fetchone()
    return row[0] if row else None


def canonical_profiles():
    with sqlite3.connect(':memory:') as db:
        for name in ('current-schema.sql', 'schema-v17-additions.sql', 'schema-v17-authority.sql',
                     'schema-v17-s06-additions.sql', 'schema-v17-s06-admitted.sql', 'schema-v17-s06-erase.sql'):
            db.executescript((SCHEMA / name).read_text())
        old = dict(db.execute("SELECT name,sql FROM sqlite_schema WHERE type='index' AND name LIKE 's06_%'"))
        db.executescript((SCHEMA / 'schema-v17-s06-context.sql').read_text())
        context = definition(db, 's06_context_admitted_marker')
        db.executescript((SCHEMA / 'schema-v17-s06-helped.sql').read_text())
        new = {name: definition(db, name) for name in ('s06_source_erase', 's06_helped_source', 's06_helped_invalid')}
    return old, new, context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path); parser.add_argument('backup', type=Path); parser.add_argument('shadow', type=Path)
    args = parser.parse_args(); paths = (args.source, args.backup, args.shadow)
    if any(not p.is_absolute() or p.is_symlink() for p in paths):
        raise SystemExit('distinct absolute non-symlink paths required')
    source = args.source.resolve(strict=True); backup = args.backup.resolve(); shadow = args.shadow.resolve()
    if len({source, backup, shadow}) != 3 or not source.is_file() or backup.exists() or shadow.exists():
        raise SystemExit('existing source and new distinct destinations required')
    old, new, context = canonical_profiles()
    before = source.stat()
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
        src.execute('PRAGMA query_only=ON')
        if src.execute('PRAGMA integrity_check').fetchone() != ('ok',) or src.execute('PRAGMA user_version').fetchone() != (17,):
            raise ValueError('source profile unavailable')
        identity = src.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
        if not identity or not isinstance(identity[0], str) or not identity[0]: raise ValueError('source identity unavailable')
        if any(definition(src, name) != sql for name, sql in old.items()): raise ValueError('source S06 profile unavailable')
        if definition(src, 's06_context_admitted_marker') not in (None, context): raise ValueError('source context profile unavailable')
        names = {row[0] for row in src.execute("SELECT name FROM sqlite_schema WHERE type='index' AND name LIKE 's06_%'")}
        if names - set(old) - {'s06_context_admitted_marker'}: raise ValueError('unknown S06 profile')
        if any(definition(src, name) is not None for name in ('s06_helped_source', 's06_helped_invalid')):
            raise ValueError('source already has helped profile')
        if src.execute("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE kind='s06_helped') OR EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE reason='s06.helped.v1')").fetchone()[0]:
            raise ValueError('unqualified helped data')
        version = src.execute('PRAGMA data_version').fetchone()[0]
        for path in (backup, shadow):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600); os.close(fd)
        with sqlite3.connect(backup) as dest: src.backup(dest)
        if src.execute('PRAGMA data_version').fetchone()[0] != version or (source.stat().st_dev, source.stat().st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('source changed during backup')
    with sqlite3.connect(backup.as_uri() + '?mode=ro', uri=True) as saved, sqlite3.connect(shadow) as dest:
        saved.backup(dest)
        dest.executescript((SCHEMA / 'schema-v17-s06-helped.sql').read_text())
        if (dest.execute('PRAGMA integrity_check').fetchone() != ('ok',) or
            dest.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone() != identity or
            any(definition(dest, name) != sql for name, sql in new.items())):
            raise ValueError('shadow validation failed')
    print('Prepared backup and helped-profile shadow; stopped-writer cutover requires separate authorization.')


if __name__ == '__main__': main()

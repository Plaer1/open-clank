#!/usr/bin/env python3
"""Prepare separate SQLite backup and S11 shadow; never replace the source."""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.openclank.chat_ratings_schema import CREATE, CREATE_V1, TABLE


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('backup',type=Path);parser.add_argument('shadow',type=Path)
    args=parser.parse_args();paths=(args.source,args.backup,args.shadow)
    if any(not path.is_absolute() for path in paths) or any(path.is_symlink() for path in paths):
        raise SystemExit('distinct absolute non-symlink paths required')
    source=args.source.resolve(strict=True);backup=args.backup.resolve(strict=False);shadow=args.shadow.resolve(strict=False)
    if len({source,backup,shadow})!=3 or not source.is_file() or backup.exists() or shadow.exists():
        raise SystemExit('existing source and two new distinct destinations required')
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src:
        if src.execute('PRAGMA integrity_check').fetchone()!=('ok',): raise ValueError('source integrity unavailable')
        cols={row[1] for row in src.execute('PRAGMA table_info(chat_messages)')}
        if not {'id','session_id','role','content','source_body_revision','source_body_hash'}.issubset(cols): raise ValueError('host source profile unavailable')
        if src.execute('SELECT version FROM host_source_schema_marker').fetchone() not in ((1,),(2,),(3,)): raise ValueError('host source profile unavailable')
        if not src.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='host_source_delivery_intent'").fetchone(): raise ValueError('host source profile unavailable')
        existing=src.execute("SELECT type,sql FROM sqlite_schema WHERE name=?",(TABLE,)).fetchone()
        if existing:
            shape=lambda sql:' '.join((sql or '').rstrip(';').split()).lower()
            if existing[0]=='table' and shape(existing[1])==shape(CREATE_V1):raise ValueError('outdated rating profile requires a separate copy-only upgrade')
            raise ValueError('rating profile already present or ambiguous')
        for path in (backup,):
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600);os.close(fd)
        with sqlite3.connect(backup) as dst:src.backup(dst)
    fd=os.open(shadow,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600);os.close(fd)
    with sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True) as saved, sqlite3.connect(shadow) as dst:
        # Verify the captured snapshot, not only the possibly changing source.
        cols = {row[1] for row in saved.execute('PRAGMA table_info(chat_messages)')}
        if (saved.execute('PRAGMA integrity_check').fetchone() != ('ok',)
            or not {'id','session_id','role','content','source_body_revision','source_body_hash'}.issubset(cols)
            or saved.execute('SELECT version FROM host_source_schema_marker').fetchone() not in ((1,), (2,), (3,))
            or not saved.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='host_source_delivery_intent'").fetchone()
            or saved.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (TABLE,)).fetchone()):
            raise ValueError('captured host source profile unavailable')
        saved.backup(dst)
        with dst:dst.execute(CREATE)
        if dst.execute('PRAGMA integrity_check').fetchone()!=('ok',) or dst.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('prepared shadow integrity unavailable')
    print('Prepared separate rating backup and shadow; source was not replaced. A later stopped-writer cutover requires separate review.')


if __name__=='__main__': main()

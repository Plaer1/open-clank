#!/usr/bin/env python3
"""Copy-only offline preparation of the optional queued-source receipt profile.

Stop all writers first. This never opens the source for writing and never
changes an existing destination. Retain a separate complete backup as well.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('backup',type=Path);parser.add_argument('destination',type=Path)
    parser.add_argument('--writers-stopped',action='store_true',required=True)
    args=parser.parse_args()
    source=args.source.resolve(strict=True);backup=args.backup.resolve(strict=False);destination=args.destination.resolve(strict=False)
    if len({source,backup,destination}) != 3 or not source.is_file() or backup.exists() or destination.exists():
        raise SystemExit('source, new backup and new destination must be distinct')
    schema=Path(__file__).resolve().parents[1]/'packages/mimo-code/packages/cli/src/storage/current-schema.ts'
    text=schema.read_text(encoding='utf-8')
    match=re.search(r'^export const currentObjects = (\[.*\])$',text,re.M)
    stamp_match=re.search(r'^export const currentStamp = (\d+)',text,re.M)
    if not match or not stamp_match: raise SystemExit('pinned schema unavailable')
    objects=json.loads(match.group(1)); stamp=int(stamp_match.group(1))
    src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    try:
        before=src.execute('PRAGMA data_version').fetchone()[0]
        if src.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('source integrity unavailable')
        for obj in objects:
            name=obj['name'];escaped=name.replace("'","''")
            row=src.execute('SELECT sql FROM sqlite_schema WHERE type=? AND name=?',(obj['type'],name)).fetchone()
            if obj['type']=='table':
                columns=src.execute(f"PRAGMA table_info('{escaped}')").fetchall()
                actual=sorted([{'name':c[1],'type':c[2],'notnull':c[3],'dflt_value':c[4],'pk':c[5]} for c in columns],key=lambda c:c['name'])
                if actual != obj['columns']: raise ValueError(f'old schema mismatch: {name}')
            elif not row or ' '.join((row[0] or '').split()) != ' '.join(obj['sql'].split()):
                raise ValueError(f'old schema mismatch: {name}')
        actual_stamp=src.execute('SELECT created_at FROM __drizzle_migrations ORDER BY created_at DESC LIMIT 1').fetchone()
        if not actual_stamp or actual_stamp[0] != stamp: raise ValueError('schema journal mismatch')
        fd=os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        backup_db=sqlite3.connect(backup)
        try: src.backup(backup_db)
        finally: backup_db.close()
        if src.execute('PRAGMA data_version').fetchone()[0] != before: raise ValueError('source changed during backup')
        fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        saved=sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True)
        dst=sqlite3.connect(destination)
        try:
            saved.backup(dst)
            with dst:
                for column in ('source_proof','source_message_id','source_part_id'):
                    dst.execute(f'ALTER TABLE user_input_receipt ADD COLUMN {column} TEXT')
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('copy integrity unavailable')
        finally:
            dst.close();saved.close()
    finally: src.close()
    print('Prepared a separate queued-source profile copy; source was not changed.')

if __name__=='__main__': main()

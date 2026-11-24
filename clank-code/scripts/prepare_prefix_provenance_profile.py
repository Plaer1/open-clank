#!/usr/bin/env python3
"""Prepare a separate, backup-first prefix-provenance database copy offline.

All writers must be stopped. Guarded queued-source profiles are intentionally
not converted by this narrow preparer; retain them for a qualified converter.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3

def validate(db: sqlite3.Connection, objects: list, stamp: int, *, converted: bool) -> str:
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('copy integrity unavailable')
    if db.execute("SELECT 1 FROM sqlite_schema WHERE name LIKE 'queued_guard_%' LIMIT 1").fetchone():
        raise ValueError('guarded queued-source profile is unsupported')
    profile = 'legacy'
    for obj in objects:
        name = obj['name']; escaped = name.replace("'", "''")
        row = db.execute('SELECT sql FROM sqlite_schema WHERE type=? AND name=?', (obj['type'], name)).fetchone()
        if obj['type'] == 'table':
            columns = db.execute(f"PRAGMA table_info('{escaped}')").fetchall()
            actual = sorted([{'name':c[1], 'type':c[2], 'notnull':c[3], 'dflt_value':c[4], 'pk':c[5]} for c in columns], key=lambda c:c['name'])
            expected = obj['columns']
            if name == 'session_prefix_snapshot' and converted:
                expected = sorted([*expected, {'name':'provenance','type':'TEXT','notnull':0,'dflt_value':None,'pk':0}], key=lambda c:c['name'])
            if name == 'user_input_receipt':
                producer = sorted([*expected, *[{'name':n,'type':'TEXT','notnull':0,'dflt_value':None,'pk':0} for n in ('source_proof','source_message_id','source_part_id')]], key=lambda c:c['name'])
                if actual == producer: profile = 'producer'; continue
            if actual != expected: raise ValueError(f'unsupported source schema: {name}')
        elif not row or ' '.join((row[0] or '').split()) != ' '.join(obj['sql'].split()):
            raise ValueError(f'unsupported source schema: {name}')
    actual_stamp = db.execute('SELECT created_at FROM __drizzle_migrations ORDER BY created_at DESC LIMIT 1').fetchone()
    if not actual_stamp or actual_stamp[0] != stamp: raise ValueError('schema journal mismatch')
    return profile

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path); parser.add_argument('backup', type=Path); parser.add_argument('destination', type=Path)
    parser.add_argument('--writers-stopped', action='store_true', required=True)
    args = parser.parse_args()
    source = args.source.resolve(strict=True); backup = args.backup.resolve(strict=False); destination = args.destination.resolve(strict=False)
    if len({source, backup, destination}) != 3 or not source.is_file() or backup.exists() or destination.exists():
        raise SystemExit('source, new backup and new destination must be distinct')
    text = (Path(__file__).resolve().parents[1] / 'packages/mimo-code/packages/cli/src/storage/current-schema.ts').read_text(encoding='utf-8')
    objects_match = re.search(r'^export const currentObjects = (\[.*\])$', text, re.M)
    stamp_match = re.search(r'^export const currentStamp = (\d+)', text, re.M)
    if not objects_match or not stamp_match: raise SystemExit('pinned schema unavailable')
    objects = json.loads(objects_match.group(1)); stamp = int(stamp_match.group(1))
    src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    try:
        version = src.execute('PRAGMA data_version').fetchone()[0]
        profile = validate(src, objects, stamp, converted=False)
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
        saved = sqlite3.connect(backup)
        try: src.backup(saved)
        finally: saved.close()
        if src.execute('PRAGMA data_version').fetchone()[0] != version: raise ValueError('source changed during backup')
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
        saved = sqlite3.connect(backup.as_uri() + '?mode=ro', uri=True); dst = sqlite3.connect(destination)
        try:
            if validate(saved, objects, stamp, converted=False) != profile: raise ValueError('backup profile changed')
            saved.backup(dst)
            if validate(dst, objects, stamp, converted=False) != profile: raise ValueError('shadow profile changed')
            with dst: dst.execute('ALTER TABLE session_prefix_snapshot ADD COLUMN provenance TEXT')
            if validate(dst, objects, stamp, converted=True) != profile: raise ValueError('converted profile changed')
        finally: dst.close(); saved.close()
    finally: src.close()
    print('Prepared a separate prefix-provenance profile copy; source was not changed.')

if __name__ == '__main__': main()

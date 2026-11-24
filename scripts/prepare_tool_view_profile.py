#!/usr/bin/env python3
"""Prepare a separate optional tool-view profile copy; never modify the source."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3


def validate(db, objects, stamp, *, converted):
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('integrity unavailable')
    if db.execute("SELECT 1 FROM sqlite_schema WHERE name LIKE 'queued_guard_%' LIMIT 1").fetchone():
        raise ValueError('guarded queued-source profile unsupported')
    profile = []
    for obj in objects:
        name = obj['name']; escaped = name.replace("'", "''")
        row = db.execute('SELECT sql FROM sqlite_schema WHERE type=? AND name=?', (obj['type'], name)).fetchone()
        if obj['type'] != 'table':
            if not row or ' '.join((row[0] or '').split()) != ' '.join(obj['sql'].split()): raise ValueError(f'unsupported schema: {name}')
            continue
        cols = db.execute(f"PRAGMA table_info('{escaped}')").fetchall()
        actual = sorted([{'name':c[1], 'type':c[2], 'notnull':c[3], 'dflt_value':c[4], 'pk':c[5]} for c in cols], key=lambda c:c['name'])
        expected = obj['columns']
        extra = lambda n: {'name':n,'type':'TEXT','notnull':0,'dflt_value':None,'pk':0}
        if name == 'part' and converted: expected = sorted([*expected, extra('openclank_view_decision')],key=lambda c:c['name'])
        choices = [('base', expected)]
        if name == 'session_prefix_snapshot': choices.append(('prefix',sorted([*expected,extra('provenance')],key=lambda c:c['name'])))
        if name == 'user_input_receipt': choices.append(('producer',sorted([*expected,*[extra(n) for n in ('source_proof','source_message_id','source_part_id')]],key=lambda c:c['name'])))
        matches = [label for label, shape in choices if actual == shape]
        if len(matches) != 1: raise ValueError(f'unsupported schema: {name}')
        if matches[0] != 'base': profile.append(matches[0])
    row = db.execute('SELECT created_at FROM __drizzle_migrations ORDER BY created_at DESC LIMIT 1').fetchone()
    if not row or row[0] != stamp: raise ValueError('schema journal mismatch')
    return profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path); parser.add_argument('backup',type=Path); parser.add_argument('destination',type=Path)
    parser.add_argument('--writers-stopped',action='store_true',required=True)
    args = parser.parse_args()
    source=args.source.resolve(strict=True); backup=args.backup.resolve(strict=False); dest=args.destination.resolve(strict=False)
    if len({source,backup,dest}) != 3 or not source.is_file() or backup.exists() or dest.exists(): raise ValueError('distinct new backup and destination required')
    text=(Path(__file__).resolve().parents[1]/'packages/mimo-code/packages/cli/src/storage/current-schema.ts').read_text(encoding='utf-8')
    objects=re.search(r'^export const currentObjects = (\[.*\])$',text,re.M); stamp=re.search(r'^export const currentStamp = (\d+)',text,re.M)
    if not objects or not stamp: raise ValueError('pinned schema unavailable')
    objects=json.loads(objects.group(1)); stamp=int(stamp.group(1))
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src:
        version=src.execute('PRAGMA data_version').fetchone()[0]; profile=validate(src,objects,stamp,converted=False)
        fd=os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        with sqlite3.connect(backup) as saved: src.backup(saved)
        if src.execute('PRAGMA data_version').fetchone()[0] != version: raise ValueError('source changed during backup')
    fd=os.open(dest,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    with sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True) as saved, sqlite3.connect(dest) as target:
        if validate(saved,objects,stamp,converted=False) != profile: raise ValueError('backup changed')
        saved.backup(target)
        if validate(target,objects,stamp,converted=False) != profile: raise ValueError('copy changed')
        with target: target.execute('ALTER TABLE part ADD COLUMN openclank_view_decision TEXT')
        if validate(target,objects,stamp,converted=True) != profile: raise ValueError('converted profile changed')
    print('Prepared a separate optional tool-view profile copy; source unchanged.')

if __name__ == '__main__': main()

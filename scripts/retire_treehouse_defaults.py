#!/usr/bin/env python3
"""Explicit offline preview, recoverable retirement/activation and guarded restore."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.openclank.copal_treehouse import new_treehouse_state
from src.openclank.copal_treehouse_repository import GLOBAL_SCHEMA
from src.openclank.treehouse_field_guide import CLASS_KEYS, FIELD_GUIDE_TEMPLATE_KEY, FIELD_GUIDE_TEMPLATE_VERSION, instantiate_field_guide


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def logical_digest(db):
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    snapshot = [['schema', list(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))]]
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        rows = [list(r) for r in db.execute('SELECT * FROM ' + quoted)]
        if table == 'treehouse_default_retirements':
            for row in rows:
                outcome = json.loads(row[2]); outcome.pop('outputDigest', None)
                row[2] = json.dumps(outcome, sort_keys=True, separators=(',', ':'))
        rows.sort(key=lambda r: json.dumps(r, sort_keys=True, default=str))
        snapshot.append([table, rows])
    return digest(snapshot)


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def definitions(state, course_id):
    result = {'courses': {course_id: state['courses'][course_id]}}
    for kind in ('modules', 'activities', 'assignments', 'skills', 'badges', 'quests'):
        result[kind] = {k: v for k, v in state.get(kind, {}).items() if v.get('courseId') == course_id}
    def normalize(value):
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items() if k not in {'createdAt', 'updatedAt', 'publishedAt', 'curriculumRevision', 'retiredAt', 'retirementOperationId'}}
        if isinstance(value, list): return [normalize(x) for x in value]
        return value
    return normalize(result)


def preview(path, operation_id):
    with readonly(path) as db:
        rows, counts = [], {'pristine': 0, 'edited': 0, 'custom': 0, 'unknown': 0, 'already-retired': 0}
        for owner, workspace, raw, revision in db.execute('SELECT owner_account_id,workspace_id,state_json,revision FROM treehouse_catalogues'):
            state = json.loads(raw)
            canonical = None
            installed = state.get('fieldGuide', {})
            if installed.get('templateKey') == FIELD_GUIDE_TEMPLATE_KEY and installed.get('templateVersion') == FIELD_GUIDE_TEMPLATE_VERSION:
                canonical = instantiate_field_guide(new_treehouse_state(owner), owner)
            suffix = hashlib.sha256(owner.encode()).hexdigest()[:12]
            for cid, course in state.get('courses', {}).items():
                category = 'custom'
                key = course.get('fieldGuideKey')
                provenance = installed.get('templateKey') == FIELD_GUIDE_TEMPLATE_KEY and key in CLASS_KEYS and cid == f'course:{key}:{suffix}'
                if course.get('retiredAt'):
                    category = 'already-retired'
                elif provenance and canonical and cid in canonical['courses']:
                    category = 'pristine' if definitions(state, cid) == definitions(canonical, cid) else 'edited'
                elif provenance:
                    category = 'unknown'
                elif cid == 'course:openclank-copal-101' or key in CLASS_KEYS:
                    category = 'unknown'
                grants = [r[0] for r in db.execute('SELECT grant_id FROM treehouse_grants WHERE owner_account_id=? AND workspace_id=? AND course_id=? AND revoked_at IS NULL', (owner, workspace, cid))]
                progress = db.execute('SELECT count(*) FROM treehouse_progress WHERE owner_account_id=? AND workspace_id=? AND course_id=?', (owner, workspace, cid)).fetchone()[0]
                counts[category] += 1
                rows.append({'ownerAccountId': owner, 'workspaceId': workspace, 'courseId': cid, 'classification': category, 'catalogueRevision': revision, 'definitionDigest': digest(definitions(state, cid)), 'grantIds': grants, 'progressRowsPreserved': progress})
        return {'format': 'treehouse-default-retirement-v1', 'operationId': operation_id,
                'inputDigest': logical_digest(db), 'templateVersion': FIELD_GUIDE_TEMPLATE_VERSION,
                'counts': counts, 'courses': rows, 'unknownPolicy': 'Preserve unchanged pending operator provenance review',
                'actions': 'Archive proven defaults, retain all historic evidence, deactivate only their grants, activate global-progress tables offline'}


def apply(path, plan, backup, *, inject_failure=False):
    if plan.get('format') != 'treehouse-default-retirement-v1' or not plan.get('operationId'):
        raise ValueError('Invalid reviewed retirement preview')
    with readonly(path) as source:
        exists = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='treehouse_default_retirements'").fetchone()
        if exists:
            row = source.execute('SELECT input_digest,outcome_json FROM treehouse_default_retirements WHERE operation_id=?', (plan['operationId'],)).fetchone()
            if row:
                if row[0] != plan['inputDigest']: raise ValueError('Operation ID conflicts with another preview')
                return {**json.loads(row[1]), 'replayed': True}
        if logical_digest(source) != plan['inputDigest']: raise ValueError('Database changed; generate a fresh preview')
        if preview(path, plan['operationId']) != plan: raise ValueError('Preview was modified after classification')
        backup = Path(backup)
        if backup.exists(): raise ValueError('Recovery backup already exists; never overwrite it')
        os.close(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        with sqlite3.connect(backup) as destination: source.backup(destination)
    with readonly(backup) as recovery:
        if logical_digest(recovery) != plan['inputDigest']: raise ValueError('Recovery snapshot verification failed')
        if recovery.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('Recovery snapshot is corrupt')
    with sqlite3.connect(path, isolation_level=None, uri=True) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if logical_digest(db) != plan['inputDigest']: raise ValueError('Revision race; no retirement applied')
            # execute, not executescript: keep DDL and retirement in this transaction.
            for statement in GLOBAL_SCHEMA.split(';'):
                if statement.strip(): db.execute(statement)
            db.execute('CREATE TABLE IF NOT EXISTS treehouse_default_retirements(operation_id TEXT PRIMARY KEY,input_digest TEXT NOT NULL,outcome_json TEXT NOT NULL,created_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS treehouse_retired_default_archives(owner_account_id TEXT,workspace_id TEXT,course_id TEXT,operation_id TEXT,classification TEXT,state_json TEXT,PRIMARY KEY(owner_account_id,workspace_id,course_id))')
            changed, now = [], str(time.time())
            grouped = {}
            for row in plan['courses']:
                if row['classification'] not in {'pristine', 'edited'}: continue
                grouped.setdefault((row['ownerAccountId'], row['workspaceId']), []).append(row)
            for (owner, workspace), courses in grouped.items():
                raw, revision = db.execute('SELECT state_json,revision FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?', (owner, workspace)).fetchone()
                state = json.loads(raw)
                for item in courses:
                    cid = item['courseId']
                    db.execute('INSERT INTO treehouse_retired_default_archives VALUES(?,?,?,?,?,?)', (owner, workspace, cid, plan['operationId'], item['classification'], raw))
                    state['courses'][cid].update(status='archived', retiredAt=now, retirementOperationId=plan['operationId'], replacementMainQuestId='treehouse:ai')
                    db.execute('UPDATE treehouse_grants SET revoked_at=?,revision=revision+1 WHERE owner_account_id=? AND workspace_id=? AND course_id=? AND revoked_at IS NULL', (now, owner, workspace, cid))
                    db.execute('UPDATE treehouse_recipient_index SET active=0 WHERE owner_account_id=? AND workspace_id=? AND course_id=?', (owner, workspace, cid))
                    changed.append(cid)
                state['revision'] = revision + 1
                db.execute('UPDATE treehouse_catalogues SET state_json=?,revision=?,updated_at=? WHERE owner_account_id=? AND workspace_id=?', (json.dumps(state), revision + 1, time.time(), owner, workspace))
            if inject_failure: raise RuntimeError('Injected transaction failure')
            receipt = {'operationId': plan['operationId'], 'inputDigest': plan['inputDigest'], 'retiredCourseIds': changed,
                       'counts': plan['counts'], 'backupPath': str(backup.resolve()),
                       'backupDigest': hashlib.sha256(backup.read_bytes()).hexdigest(),
                       'historicalProgressAndAwards': 'Preserved; no new-course credit', 'replayed': False}
            db.execute('INSERT INTO treehouse_default_retirements VALUES(?,?,?,?)', (plan['operationId'], plan['inputDigest'], json.dumps(receipt), time.time()))
            receipt['outputDigest'] = logical_digest(db)
            db.execute('UPDATE treehouse_default_retirements SET outcome_json=? WHERE operation_id=?', (json.dumps(receipt), plan['operationId']))
            db.commit()
            return receipt
        except BaseException:
            db.rollback(); raise


def restore(path, backup, receipt):
    """Stopped-writer restore refuses newer work or changed recovery bytes."""
    backup = Path(backup)
    if hashlib.sha256(backup.read_bytes()).hexdigest() != receipt['backupDigest']:
        raise ValueError('Recovery snapshot changed')
    # Restore schema/data under SQLite's write boundary, not check-close-overwrite.
    # Callers still stop app writers; a racing writer cannot pass this lock/check.
    with sqlite3.connect(path, isolation_level=None, uri=True) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('ATTACH DATABASE ? AS recovery', (backup.resolve().as_uri() + '?mode=ro',))
        db.execute('BEGIN EXCLUSIVE')
        try:
            if logical_digest(db) != receipt.get('outputDigest'): raise ValueError('Newer database state exists; selective recovery required')
            old = list(db.execute("SELECT type,name FROM main.sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY type DESC"))
            for kind, name in old: db.execute('DROP ' + kind.upper() + ' "' + name.replace('"', '""') + '"')
            schemas = list(db.execute("SELECT type,name,sql FROM recovery.sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END,name"))
            for kind, name, sql in schemas:
                if kind == 'table':
                    db.execute(sql)
                    quoted = '"' + name.replace('"', '""') + '"'
                    db.execute('INSERT INTO main.' + quoted + ' SELECT * FROM recovery.' + quoted)
            if db.execute("SELECT 1 FROM recovery.sqlite_master WHERE name='sqlite_sequence'").fetchone():
                db.execute('DELETE FROM main.sqlite_sequence')
                db.execute('INSERT INTO main.sqlite_sequence SELECT * FROM recovery.sqlite_sequence')
            for kind, name, sql in schemas:
                if kind != 'table': db.execute(sql)
            if logical_digest(db) != receipt['inputDigest']: raise ValueError('Restored schema/data differs')
            db.commit()
        except BaseException:
            db.rollback(); raise
    with readonly(path) as recovered:
        if logical_digest(recovered) != receipt['inputDigest']: raise ValueError('Restored database differs')
    return {'restored': True, 'inputDigest': receipt['inputDigest']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['preview', 'apply', 'verify', 'restore'])
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--operation-id', default='treehouse-defaults-2026-10-14')
    parser.add_argument('--preview', type=Path)
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.mode == 'preview': result = preview(args.db, args.operation_id)
    elif args.mode == 'apply': result = apply(args.db, json.loads(args.preview.read_text()), args.backup)
    elif args.mode == 'restore': result = restore(args.db, args.backup, json.loads(args.receipt.read_text()))
    else:
        with readonly(args.db) as db: result = {'integrity': db.execute('PRAGMA integrity_check').fetchone()[0], 'logicalDigest': logical_digest(db)}
    if args.output:
        with os.fdopen(os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as output:
            json.dump(result, output, indent=2)
    else: print(json.dumps(result, indent=2))


if __name__ == '__main__': main()

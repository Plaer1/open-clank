"""Explicit offline schema-only activation. Runtime status never creates tables."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def _schema():
    # Lazy import avoids a repository/status import cycle; one DDL authority.
    from src.openclank.copal_treehouse_repository import GLOBAL_SCHEMA
    return GLOBAL_SCHEMA


def storage_status(db):
    with sqlite3.connect(':memory:') as expected:
        expected.executescript(_schema())
        names = [r[0] for r in expected.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing, incompatible = [], []
        for name in names:
            if name not in present:
                # A same-name view is incompatible, not an absent table.
                if db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone(): incompatible.append(name)
                else: missing.append(name)
                continue
            shape = lambda connection: [(r[1], r[2].upper(), r[3], r[4], r[5], r[6]) for r in connection.execute('PRAGMA table_xinfo("' + name + '")')]
            def unique_keys(connection):
                keys = []
                for idx in connection.execute('PRAGMA index_list("' + name + '")'):
                    if idx[2]:
                        quoted = '"' + idx[1].replace('"', '""') + '"'
                        # Equality, ordering and partial-key semantics are identity constraints.
                        columns = tuple((r[2], r[3], str(r[4]).upper()) for r in connection.execute('PRAGMA index_xinfo(' + quoted + ')') if r[5])
                        keys.append((columns, idx[4]))
                return sorted(keys, key=repr)
            sql = db.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (name,)).fetchone()[0]
            if (shape(db) != shape(expected) or unique_keys(db) != unique_keys(expected)
                    or list(db.execute('PRAGMA foreign_key_list("' + name + '")'))
                    or re.search(r'\bCHECK\s*\(', sql, re.I)
                    or db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (name,)).fetchone()):
                incompatible.append(name)
        state = 'incompatible' if incompatible else 'activation-required' if missing else 'ready'
        code = {'ready': None, 'activation-required': 'global_progress_activation_required', 'incompatible': 'global_progress_schema_incompatible'}[state]
        message = {'ready': 'Global learning storage is ready', 'activation-required': 'Global learning storage needs explicit offline schema activation; private courses remain available', 'incompatible': 'Global learning storage has an incompatible schema; stop writers and request offline review'}[state]
        return {'status': state, 'code': code, 'message': message, 'missingTables': missing, 'incompatibleTables': incompatible}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def logical_snapshot(db):
    snapshot = [['schema', list(db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))]]
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        rows = [[{'sqliteBlobHex': cell.hex()} if isinstance(cell, bytes) else cell for cell in r] for r in db.execute('SELECT * FROM "' + name.replace('"', '""') + '"')]
        if name == 'treehouse_storage_activations':
            for row in rows:
                receipt = json.loads(row[2]); receipt.pop('outputDigest', None)
                row[2] = json.dumps(receipt, sort_keys=True, separators=(',', ':'))
        rows.sort(key=lambda r: json.dumps(r, sort_keys=True))
        snapshot.append([name, rows])
    return snapshot


def logical_digest(db):
    return _digest(logical_snapshot(db))


def preview(path, operation_id):
    if not isinstance(operation_id, str) or not operation_id.strip(): raise ValueError('Operation ID is required')
    with readonly(path) as db:
        return {'format': 'treehouse-storage-activation-v1', 'operationId': operation_id,
                'inputDigest': logical_digest(db), 'availability': storage_status(db),
                'action': 'Create only missing global tables; preserve all existing schema and rows'}


def apply(path, plan, backup, *, writers_stopped=False, inject_failure=False):
    if not writers_stopped: raise ValueError('Stop all writers and explicitly confirm --writers-stopped')
    if plan.get('format') != 'treehouse-storage-activation-v1' or not plan.get('operationId'): raise ValueError('Invalid activation preview')
    with readonly(path) as source:
        if source.execute("SELECT 1 FROM sqlite_master WHERE name='treehouse_storage_activations'").fetchone():
            row = source.execute('SELECT input_digest,receipt_json FROM treehouse_storage_activations WHERE operation_id=?', (plan['operationId'],)).fetchone()
            if row:
                if row[0] != plan['inputDigest']: raise ValueError('Operation ID conflicts with another preview')
                return {**json.loads(row[1]), 'replayed': True}
        if preview(path, plan['operationId']) != plan: raise ValueError('Database or preview changed; generate a fresh preview')
        status = storage_status(source)
        if status['status'] == 'incompatible': raise ValueError('Incompatible global schema; no changes applied')
        if status['status'] == 'ready':
            return {'operationId': plan['operationId'], 'changed': False, 'replayed': False, 'inputDigest': plan['inputDigest'], 'outputDigest': plan['inputDigest'], 'createdTables': [], 'availability': status}
        backup = Path(backup)
        os.close(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        with sqlite3.connect(backup) as destination: source.backup(destination)
    with readonly(backup) as recovery:
        if recovery.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or logical_digest(recovery) != plan['inputDigest']: raise ValueError('Recovery backup verification failed')
        before = logical_snapshot(recovery)
    with sqlite3.connect(path, isolation_level=None, uri=True) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if logical_digest(db) != plan['inputDigest']: raise ValueError('Database changed after backup; no activation applied')
            if storage_status(db) != status: raise ValueError('Schema changed; no activation applied')
            for statement in _schema().split(';'):
                if statement.strip(): db.execute(statement)
            if storage_status(db)['status'] != 'ready': raise ValueError('Schema activation did not produce compatible tables')
            # Every preexisting schema object and every row must remain identical.
            after = logical_snapshot(db)
            existing = {item[0] for item in before[1:]}
            after_schema = {tuple(r) for r in after[0][1]}
            if not set(map(tuple, before[0][1])).issubset(after_schema): raise ValueError('Preexisting schema changed')
            if {n: rows for n, rows in after[1:] if n in existing} != {n: rows for n, rows in before[1:]}: raise ValueError('Preexisting rows changed')
            db.execute('CREATE TABLE treehouse_storage_activations(operation_id TEXT PRIMARY KEY,input_digest TEXT NOT NULL,receipt_json TEXT NOT NULL)') if 'treehouse_storage_activations' not in existing else None
            receipt = {'operationId': plan['operationId'], 'changed': True, 'replayed': False, 'inputDigest': plan['inputDigest'], 'createdTables': status['missingTables'], 'backupPath': str(backup.resolve()), 'backupDigest': hashlib.sha256(backup.read_bytes()).hexdigest(), 'preexistingSchemaAndRowsUnchanged': True, 'availability': storage_status(db)}
            db.execute('INSERT INTO treehouse_storage_activations VALUES(?,?,?)', (plan['operationId'], plan['inputDigest'], json.dumps(receipt)))
            receipt['outputDigest'] = logical_digest(db)
            db.execute('UPDATE treehouse_storage_activations SET receipt_json=? WHERE operation_id=?', (json.dumps(receipt), plan['operationId']))
            if inject_failure: raise RuntimeError('Injected activation failure')
            db.commit()
            return receipt
        except BaseException:
            db.rollback(); raise


def verify(path):
    with readonly(path) as db:
        return {'integrity': db.execute('PRAGMA integrity_check').fetchone()[0], 'logicalDigest': logical_digest(db), 'availability': storage_status(db)}


def restore(path, backup, receipt, *, writers_stopped=False):
    if not writers_stopped: raise ValueError('Stop all writers and explicitly confirm --writers-stopped')
    backup = Path(backup)
    if hashlib.sha256(backup.read_bytes()).hexdigest() != receipt.get('backupDigest'): raise ValueError('Recovery backup changed')
    with readonly(backup) as recovery:
        if logical_digest(recovery) != receipt['inputDigest'] or recovery.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('Recovery backup differs')
    with sqlite3.connect(path, isolation_level=None, uri=True) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('ATTACH DATABASE ? AS recovery', (backup.resolve().as_uri() + '?mode=ro',))
        db.execute('BEGIN EXCLUSIVE')
        try:
            if logical_digest(db) != receipt.get('outputDigest'): raise ValueError('Newer database state exists; selective recovery required')
            for kind, name in list(db.execute("SELECT type,name FROM main.sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY type DESC")):
                db.execute('DROP ' + kind.upper() + ' "' + name.replace('"', '""') + '"')
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
    return {'restored': True, 'inputDigest': receipt['inputDigest']}

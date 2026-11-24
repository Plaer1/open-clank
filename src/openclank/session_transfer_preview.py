"""Bounded preview of an explicitly selected Codex thread-history snapshot.

Migration fingerprints are SHA-384 of the pinned 0001–0006 SQL files in the
2026-09-10 Codex reference, canonized here; runtime never reads the reference.
The copy is a byte snapshot only, not evidence that a live WAL was included.
"""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import stat
import sys
import time

MAX_SOURCE = 64 * 1024 * 1024
MAX_RESULT = 256 * 1024
MAX_ITEM = 32768
CHECKSUMS = (
 '2ad9a4ab17df511fb73f03337133de94a172dc0a1f5729b5ef61fe106db03d1a43bdb5978b0cb7d7bc74071d3ca34a92',
 'ae61dbc1a6422530aa93914dbcac363a0238b86ae1a9d77eb041c75330e1283a0fcf93a043deeefcd6fd2c4037694bc4',
 'a51da2a11df7760d8f8cab221e82b36ce8c0a30f16d44f3602e7f0fbc622ca72f1c3e3ecf22a194e12c2222bdcb51afe',
 '65f33b171bd3aafe9d24728b2ca6a5c720a3ab54aed472998f8acf7489f0d35b2dfd12258a2d17be993bf17098b5e50c',
 '2fdad8e361dc1f828afacf10bd2eb1f511b02deef7fa234a9c412d0211de126d1cfad54b175c9c3a9731548f9084b437',
 '02ed82d375adb58b5c0c09e0154f3331d507cea7af446d12324e5653cdba9a91f84afd38cf2a29f33030614689deb9de',
)
TABLES = {
 '_sqlx_migrations': ('version','description','installed_on','success','checksum','execution_time'),
 'thread_turns': ('thread_id','turn_id','rollout_ordinal','status','error_json','started_at','completed_at','duration_ms','first_user_item_id','final_agent_item_id','rollout_byte_offset','rollout_end_ordinal','rollout_end_byte_offset'),
 'thread_items': ('thread_id','turn_id','item_id','rollout_ordinal','created_at_ms','item_json','item_type','updated_at_ordinal'),
 'thread_history_projection_state': ('thread_id','next_rollout_byte_offset','next_rollout_ordinal'),
 'thread_realtime_items': ('thread_id','item_id','rollout_ordinal','created_at_ms','item_type','item_json'),
}

class PreviewUnavailable(ValueError): pass


def _id(value):
    if type(value) is not str or not value or len(value.encode('utf-8')) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise PreviewUnavailable('invalid source identity')
    return value


def cursor_encode(scope, kind, last):
    from src.secret_storage import keyed_digest
    raw = json.dumps({'v':1,'scope':scope,'kind':kind,'last':last}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    mac = keyed_digest(raw, context='openclank.selected-history.cursor.v1')
    return base64.urlsafe_b64encode(json.dumps({'payload':raw,'mac':mac}, separators=(',', ':')).encode()).decode().rstrip('=')


def cursor_decode(token, scope, kind):
    from src.secret_storage import keyed_digest
    if type(token) is not str or len(token) > 2048: raise PreviewUnavailable('cursor unavailable')
    try:
        wrapper = json.loads(base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)))
        if type(wrapper) is not dict or set(wrapper) != {'payload','mac'} or type(wrapper['payload']) is not str or type(wrapper['mac']) is not str: raise ValueError()
        if not hmac.compare_digest(wrapper['mac'], keyed_digest(wrapper['payload'], context='openclank.selected-history.cursor.v1')): raise ValueError()
        value = json.loads(wrapper['payload'])
        if type(value) is not dict or set(value) != {'v','scope','kind','last'} or value['v'] != 1 or value['scope'] != scope or value['kind'] != kind: raise ValueError()
        return value['last']
    except Exception as exc: raise PreviewUnavailable('cursor unavailable') from exc


def copy_path(source, destination, expected_size):
    if sys.platform not in {'linux','darwin'} or os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_NONBLOCK'):
        raise PreviewUnavailable('safe path staging unavailable')
    expected = source.expected_identity
    if not source.path or not isinstance(expected, tuple) or len(expected) != 4: raise PreviewUnavailable('source identity unavailable')
    fd = os.open(source.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0))
    try:
        before = os.fstat(fd)
        identity = lambda st: (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))
        if not stat.S_ISREG(before.st_mode) or identity(before) != expected or before.st_size != expected_size or before.st_size > MAX_SOURCE:
            raise PreviewUnavailable('source changed')
        sha = hashlib.sha256(); total = 0
        with open(destination, 'wb') as out:
            while True:
                chunk = os.read(fd, 262144)
                if not chunk: break
                total += len(chunk)
                if total > MAX_SOURCE or total > expected_size: raise PreviewUnavailable('source exceeds limit')
                sha.update(chunk); out.write(chunk)
        if total != expected_size or identity(os.fstat(fd)) != expected: raise PreviewUnavailable('source changed')
        return sha.hexdigest()
    finally: os.close(fd)


def _verify(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 16")}
    if tables != set(TABLES): raise PreviewUnavailable('unsupported schema')
    for table, names in TABLES.items():
        rows = list(db.execute('PRAGMA table_info("' + table + '")'))
        if tuple(row[1] for row in rows) != names: raise PreviewUnavailable('unsupported schema')
    rows = list(db.execute('SELECT version,success,checksum FROM _sqlx_migrations ORDER BY version LIMIT 8'))
    if len(rows) != 6 or any(type(row[0]) is not int or row[0] != i or row[1] != 1 or not isinstance(row[2], bytes) or row[2].hex() != CHECKSUMS[i-1] for i,row in enumerate(rows,1)):
        raise PreviewUnavailable('unsupported migrations')
    expected_columns = {
        'thread_turns': [('TEXT',1),('TEXT',1),('INTEGER',1),('TEXT',1),('TEXT',0),('INTEGER',0),('INTEGER',0),('INTEGER',0),('TEXT',0),('TEXT',0),('INTEGER',0),('INTEGER',0),('INTEGER',0)],
        'thread_items': [('TEXT',1),('TEXT',1),('TEXT',1),('INTEGER',1),('INTEGER',1),('TEXT',1),('TEXT',1),('INTEGER',1)],
        'thread_history_projection_state': [('TEXT',0),('INTEGER',1),('INTEGER',1)],
        'thread_realtime_items': [('TEXT',1),('TEXT',1),('INTEGER',1),('INTEGER',1),('TEXT',1),('TEXT',1)],
    }
    for table, expected in expected_columns.items():
        columns = list(db.execute('PRAGMA table_info("' + table + '")'))
        if [(str(r[2]).upper(),r[3]) for r in columns] != expected: raise PreviewUnavailable('unsupported schema')
    expected_primary = {'thread_turns':('thread_id','turn_id'), 'thread_items':('thread_id','turn_id','item_id'),
        'thread_history_projection_state':('thread_id',), 'thread_realtime_items':('thread_id','item_id'), '_sqlx_migrations':('version',)}
    for table, primary in expected_primary.items():
        columns = list(db.execute('PRAGMA table_info("' + table + '")'))
        actual = tuple(row[1] for row in sorted((r for r in columns if r[5]), key=lambda r:r[5]))
        if actual != primary: raise PreviewUnavailable('unsupported schema')
    indexes = (
        ('idx_thread_turns_page','thread_turns',('thread_id','rollout_ordinal'),1,0),
        ('idx_thread_items_page','thread_items',('thread_id','rollout_ordinal'),1,0),
        ('idx_thread_items_by_turn_page','thread_items',('thread_id','turn_id','rollout_ordinal'),0,0),
        ('idx_thread_items_user_messages','thread_items',('thread_id','rollout_ordinal'),0,1),
        ('idx_thread_items_updated_page','thread_items',('thread_id','updated_at_ordinal'),0,0),
        ('idx_thread_items_by_turn_updated_page','thread_items',('thread_id','turn_id','updated_at_ordinal'),0,0),
        ('idx_thread_realtime_items_page','thread_realtime_items',('thread_id','rollout_ordinal'),1,0),
        ('idx_thread_realtime_items_boundary','thread_realtime_items',('thread_id','rollout_ordinal'),0,1),
        ('idx_thread_turns_end_page','thread_turns',('thread_id','rollout_end_ordinal','turn_id'),0,1),
    )
    for name, table, cols, unique, partial in indexes:
        entry = db.execute("SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        index = next((r for r in db.execute('PRAGMA index_list("' + table + '")') if r[1] == name), None)
        if (not entry or entry[0] != table or not index or index[2] != unique or index[4] != partial
                or tuple(x[2] for x in db.execute('PRAGMA index_info("' + name + '")')) != cols):
            raise PreviewUnavailable('unsupported indexes')
    explicit_indexes = db.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL LIMIT 16").fetchall()
    if {r[0] for r in explicit_indexes} != {item[0] for item in indexes}: raise PreviewUnavailable('unsupported indexes')
    triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' LIMIT 8").fetchall()
    views = db.execute("SELECT name FROM sqlite_master WHERE type='view' LIMIT 1").fetchall()
    expected_trigger = 'createtriggerthread_realtime_items_projection_cleanupafterdeleteonthread_history_projection_statebegindeletefromthread_realtime_itemswherethread_id=old.thread_id;end'
    if (len(triggers) != 1 or triggers[0][0] != 'thread_realtime_items_projection_cleanup'
            or ''.join(str(triggers[0][1]).lower().split()).rstrip(';') != expected_trigger or views):
        raise PreviewUnavailable('unsupported schema')



def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate item field')
        result[key] = value
    return result


def _item(row):
    turn, item, ordinal, kind, size, raw = row
    _id(turn); _id(item)
    if type(ordinal) is not int or ordinal < 0: raise PreviewUnavailable('invalid item order')
    out = {'turnID':turn,'itemID':item,'ordinal':ordinal,'kind':kind if type(kind) is str and len(kind) <= 128 else 'unknown'}
    if raw is None or type(size) is not int or size > MAX_ITEM:
        return out | {'role':None,'unsupportedReason':'oversized'}
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if type(value) is not dict or value.get('id') != item or value.get('type') != kind: raise ValueError()
        if kind == 'userMessage':
            content = value.get('content')
            if type(content) is not list or len(content) > 128 or any(type(x) is not dict or x.get('type') != 'text' or type(x.get('text')) is not str for x in content): raise ValueError()
            text = '\n'.join(x['text'] for x in content); role = 'user'
        elif kind == 'agentMessage' and type(value.get('text')) is str:
            text = value['text']; role = 'assistant'
        else:
            return out | {'role':None,'unsupportedReason':'unsupported_kind'}
        if len(text.encode('utf-8')) > 16384: return out | {'role':None,'unsupportedReason':'oversized_text'}
        return out | {'role':role,'text':text}
    except (ValueError, TypeError, UnicodeError, RecursionError, json.JSONDecodeError):
        return out | {'role':None,'unsupportedReason':'malformed_or_unsupported'}


def inspect_snapshot(path, *, scope, thread_id=None, cursor=None):
    from pathlib import Path
    deadline = time.monotonic() + 2.0
    db = sqlite3.connect(Path(path).as_uri() + '?mode=ro&immutable=1', uri=True, timeout=0)
    try:
        db.execute('PRAGMA query_only=ON'); db.execute('PRAGMA trusted_schema=OFF')
        ticks = [0]
        def progress():
            ticks[0] += 1
            return int(ticks[0] > 1000 or time.monotonic() > deadline)
        db.set_progress_handler(progress, 1000)
        _verify(db)
        if thread_id is None:
            last = cursor_decode(cursor, scope, 'threads') if cursor else ''
            if last: _id(last)
            rows = db.execute('SELECT thread_id FROM thread_history_projection_state WHERE thread_id > ? ORDER BY thread_id LIMIT 21', (last,)).fetchall()
            ids = [_id(r[0]) for r in rows[:20]]
            return {'kind':'threads','threads':[{'threadID':x,'title':None,'sourceWorkspace':None} for x in ids], 'nextCursor':cursor_encode(scope,'threads',ids[-1]) if len(rows)>20 and ids else None, 'coverage':'partial','unsupportedCount':None}
        thread_id = _id(thread_id)
        if not db.execute('SELECT 1 FROM thread_history_projection_state WHERE thread_id=?', (thread_id,)).fetchone(): raise PreviewUnavailable('thread unavailable')
        last = cursor_decode(cursor, scope, 'items:'+thread_id) if cursor else -1
        if type(last) is not int or last < -1: raise PreviewUnavailable('cursor unavailable')
        rows = db.execute('SELECT turn_id,item_id,rollout_ordinal,item_type,length(CAST(item_json AS BLOB)),CASE WHEN length(CAST(item_json AS BLOB)) <= ? THEN item_json ELSE NULL END FROM thread_items WHERE thread_id=? AND rollout_ordinal>? ORDER BY rollout_ordinal LIMIT 101', (MAX_ITEM,thread_id,last)).fetchall()
        items=[]; next_cursor=None
        for row in rows[:100]:
            item = _item(row)
            trial = items + [item]
            if len(json.dumps(trial, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_RESULT - 4096:
                if not items: raise PreviewUnavailable('item exceeds output budget')
                next_cursor = cursor_encode(scope,'items:'+thread_id,items[-1]['ordinal']); break
            items = trial
        if next_cursor is None and len(rows)>100 and items: next_cursor = cursor_encode(scope,'items:'+thread_id,items[-1]['ordinal'])
        return {'kind':'items','threadID':thread_id,'title':None,'sourceWorkspace':None,'items':items,'nextCursor':next_cursor,'coverage':'partial','unsupportedCount':sum(1 for x in items if x.get('unsupportedReason')),'unsupportedCountScope':'page','realtimeCoverage':'not_inspected'}
    except (sqlite3.Error, UnicodeError, TypeError, OverflowError) as exc:
        raise PreviewUnavailable('snapshot unavailable or work limit exceeded') from exc
    finally: db.close()


def inspect_selected_snapshot(path, *, thread_id, selectors):
    """Re-read exact preview identities from the staged v6 copy; never trust browser text."""
    from pathlib import Path
    thread_id = _id(thread_id)
    if type(selectors) is not list or not 1 <= len(selectors) <= 100:
        raise PreviewUnavailable('invalid selection')
    normalized = []
    previous = -1
    seen = set()
    for selector in selectors:
        if type(selector) is not dict or set(selector) != {'turnID','itemID','ordinal'}:
            raise PreviewUnavailable('invalid selection')
        turn, item, ordinal = _id(selector['turnID']), _id(selector['itemID']), selector['ordinal']
        if type(ordinal) is not int or ordinal <= previous or (turn,item) in seen:
            raise PreviewUnavailable('invalid selection order')
        previous = ordinal; seen.add((turn,item)); normalized.append((turn,item,ordinal))
    deadline = time.monotonic() + 2.0
    db = sqlite3.connect(Path(path).as_uri() + '?mode=ro&immutable=1', uri=True, timeout=0)
    try:
        db.execute('PRAGMA query_only=ON'); db.execute('PRAGMA trusted_schema=OFF')
        ticks = [0]
        def progress():
            ticks[0] += 1
            return int(ticks[0] > 1000 or time.monotonic() > deadline)
        db.set_progress_handler(progress, 1000)
        _verify(db)
        if not db.execute('SELECT 1 FROM thread_history_projection_state WHERE thread_id=?', (thread_id,)).fetchone():
            raise PreviewUnavailable('thread unavailable')
        items = []; total = 0
        for turn,item,ordinal in normalized:
            row = db.execute('SELECT turn_id,item_id,rollout_ordinal,item_type,length(CAST(item_json AS BLOB)),CASE WHEN length(CAST(item_json AS BLOB)) <= ? THEN item_json ELSE NULL END FROM thread_items WHERE thread_id=? AND turn_id=? AND item_id=?', (MAX_ITEM,thread_id,turn,item)).fetchone()
            if not row or row[2] != ordinal: raise PreviewUnavailable('selection changed')
            projected = _item(row)
            if projected.get('role') not in {'user','assistant'} or 'text' not in projected:
                raise PreviewUnavailable('selection contains unsupported item')
            total += len(projected['text'].encode('utf-8'))
            if total > 1024 * 1024: raise PreviewUnavailable('selection exceeds limit')
            items.append(projected)
        if items[0]['role'] != 'user': raise PreviewUnavailable('selection must begin with a user message')
        return {'kind':'selection','threadID':thread_id,'items':items,'coverage':'partial','unsupportedCount':None,'gaps':'not_enumerated'}
    except (sqlite3.Error, UnicodeError, TypeError, OverflowError) as exc:
        raise PreviewUnavailable('snapshot unavailable or work limit exceeded') from exc
    finally: db.close()

"""Explicit owner-owned General Hex revisions in Frankenmemory's SQLite authority.

This library is declarative data; it neither publishes workspace policy nor
infers applicability. Callers supply authenticated owners and explicit tags.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import unicodedata
import uuid


class GeneralHexError(ValueError):
    pass


class GeneralHexNotFound(GeneralHexError):
    pass


class GeneralHexConflict(GeneralHexError):
    pass


def normalize_tags(tags):
    if not isinstance(tags, (list, tuple)) or len(tags) > 64:
        raise GeneralHexError("tags must be a list of at most 64 positive labels")
    labels, seen = [], set()
    for tag in tags:
        if not isinstance(tag, str):
            raise GeneralHexError("tag labels must be strings")
        label = " ".join(unicodedata.normalize("NFKC", tag).split())
        if not label or len(label) > 100 or label.startswith(("-", "!")):
            raise GeneralHexError("tags must be nonempty positive labels of at most 100 characters")
        key = label.casefold()
        if key not in seen:
            labels.append(label)
            seen.add(key)
    return labels


def tag_keys(tags):
    return {label.casefold() for label in normalize_tags(tags)}


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise GeneralHexError("values must be finite, serializable JSON") from exc


def _identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise GeneralHexError("a nonempty scoped identity is required")
    return value.strip()


def _revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GeneralHexError("expected_revision must be a nonnegative integer")
    return value


def ensure_general_hex_schema(db_path):
    """Initialize a fresh policy store or validate current schema without upgrades."""
    expected = {'fm_general_hex_heads': ['owner_id', 'hex_id', 'revision', 'deleted'], 'fm_general_hex_revisions': ['owner_id', 'hex_id', 'revision', 'record_json'], 'fm_general_hex_context_tags': ['owner_id', 'context_kind', 'context_id', 'revision', 'tags_json', 'updated_at']}
    with sqlite3.connect(db_path, timeout=30) as conn:
        present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if present.intersection(expected):
            for table, columns in expected.items():
                found = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if not set(columns).issubset(found):
                    raise GeneralHexError("Legacy Hex schema requires .clanker/tools/migrations/python/secondary.py general-hex-schema")
            return
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS fm_general_hex_heads (
            owner_id TEXT NOT NULL, hex_id TEXT NOT NULL,
            revision INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(owner_id,hex_id));
        CREATE TABLE IF NOT EXISTS fm_general_hex_revisions (
            owner_id TEXT NOT NULL, hex_id TEXT NOT NULL, revision INTEGER NOT NULL,
            record_json TEXT NOT NULL,
            PRIMARY KEY(owner_id,hex_id,revision));
        CREATE TABLE IF NOT EXISTS fm_general_hex_context_tags (
            owner_id TEXT NOT NULL, context_kind TEXT NOT NULL,
            context_id TEXT NOT NULL, revision INTEGER NOT NULL, tags_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(owner_id,context_kind,context_id),
            CHECK(context_kind IN ('workspace','task')));
        
        """)



def erase_general_hex_owner(conn, owner_id):
    """Participate in the caller's account-erasure transaction; do not commit."""
    for table in ("fm_general_hex_revisions", "fm_general_hex_heads", "fm_general_hex_context_tags"):
        conn.execute(f"DELETE FROM {table} WHERE owner_id=?", (_identity(owner_id),))


class GeneralHexRepository:
    def __init__(self, db_path=None):
        if db_path is None:
            from src.constants import FM_DB_PATH
            db_path = FM_DB_PATH
        self.db_path = str(db_path)

    @contextmanager
    def _connection(self, *, write=False):
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _get(self, conn, owner_id, hex_id, revision=None, *, include_deleted=False):
        owner_id, hex_id = _identity(owner_id), _identity(hex_id)
        head = conn.execute("SELECT revision,deleted FROM fm_general_hex_heads WHERE owner_id=? AND hex_id=?", (owner_id, hex_id)).fetchone()
        if not head or (head[1] and revision is None and not include_deleted):
            raise GeneralHexNotFound("General Hex not found")
        revision = head[0] if revision is None else _revision(revision)
        row = conn.execute("SELECT record_json FROM fm_general_hex_revisions WHERE owner_id=? AND hex_id=? AND revision=?", (owner_id, hex_id, revision)).fetchone()
        if not row:
            raise GeneralHexNotFound("General Hex revision not found")
        return self._owned_record(row[0], owner_id)

    @staticmethod
    def _owned_record(payload, owner_id):
        record = json.loads(payload)
        if record['owner_id'] != owner_id:
            record['historical_owner_id'] = record['owner_id']
            record['owner_id'] = owner_id
        return record

    def get(self, owner_id, hex_id, revision=None):
        with self._connection() as conn:
            return self._get(conn, owner_id, hex_id, revision)

    def _list(self, conn, owner_id, search='', tags=None, include_deleted=False, limit=None, offset=0):
        rows = conn.execute('''SELECT r.record_json FROM fm_general_hex_heads h
            JOIN fm_general_hex_revisions r USING(owner_id,hex_id,revision)
            WHERE h.owner_id=? AND (? OR h.deleted=0) ORDER BY h.hex_id''', (_identity(owner_id), int(include_deleted)))
        keys = tag_keys(tags) if tags is not None else set()
        query = search.casefold() if isinstance(search, str) else ''
        result = []
        for row in rows:
            record = self._owned_record(row[0], _identity(owner_id))
            if query and query not in (record['title'] + '\n' + record['body']).casefold():
                continue
            if keys and not keys.intersection(tag_keys(record['tags'])):
                continue
            if offset:
                offset -= 1
                continue
            result.append(record)
            if limit is not None and len(result) >= limit:
                break
        return result

    def list(self, owner_id, *, search='', tags=None, include_deleted=False, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise GeneralHexError('limit must be 1–100 and offset nonnegative')
        with self._connection() as conn:
            return self._list(conn, owner_id, search, tags, include_deleted, limit, offset)

    def history(self, owner_id, hex_id, *, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise GeneralHexError('limit must be 1–100 and offset nonnegative')
        with self._connection() as conn:
            self._get(conn, owner_id, hex_id, include_deleted=True)
            return [self._owned_record(row[0], _identity(owner_id)) for row in conn.execute("SELECT record_json FROM fm_general_hex_revisions WHERE owner_id=? AND hex_id=? ORDER BY revision DESC LIMIT ? OFFSET ?", (_identity(owner_id), _identity(hex_id), limit, offset))]

    def _save(self, conn, owner_id, hex_id, revision, values):
        title, body = values.get('title'), values.get('body')
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise GeneralHexError("title must contain 1–300 characters")
        if not isinstance(body, str) or not body.strip() or len(body) > 100000:
            raise GeneralHexError("body must contain 1–100000 characters")
        authorship = values.get('authorship', 'user')
        if authorship not in ('user', 'agent'):
            raise GeneralHexError("authorship must be user or agent")
        enabled, accepted = values.get('enabled', True), values.get('accepted')
        if type(enabled) is not bool or (accepted is not None and type(accepted) is not bool):
            raise GeneralHexError("enabled and accepted must be booleans")
        if accepted is None:
            accepted = authorship == 'user'
        provenance = values.get('provenance') or {}
        if not isinstance(provenance, dict) or len(_json(provenance)) > 30000:
            raise GeneralHexError("provenance must be a bounded JSON object")
        if accepted and authorship == 'user':
            provenance = dict(provenance, acceptance=dict(owner_id=_identity(owner_id), source='explicit-user-save'))
        record = dict(owner_id=_identity(owner_id), hex_id=hex_id, revision=revision,
                      title=title.strip(), body=body, tags=normalize_tags(values.get('tags', [])),
                      enabled=enabled, accepted=accepted, authorship=authorship,
                      provenance=provenance, deleted=bool(values.get('deleted', False)),
                      created_at=datetime.now(timezone.utc).isoformat())
        record['hash'] = hashlib.sha256(_json(record).encode()).hexdigest()
        conn.execute("INSERT INTO fm_general_hex_revisions VALUES(?,?,?,?)", (owner_id, hex_id, revision, _json(record)))
        conn.execute('''INSERT INTO fm_general_hex_heads VALUES(?,?,?,?)
            ON CONFLICT(owner_id,hex_id) DO UPDATE SET revision=excluded.revision,deleted=excluded.deleted''', (owner_id, hex_id, revision, int(record['deleted'])))
        return record

    def create(self, owner_id, *, title, body, tags=(), enabled=True, authorship='user', accepted=None, provenance=None):
        owner_id = _identity(owner_id)
        with self._connection(write=True) as conn:
            return self._save(conn, owner_id, uuid.uuid4().hex, 1, dict(title=title, body=body, tags=tags, enabled=enabled, authorship=authorship, accepted=accepted, provenance=provenance))

    def update(self, owner_id, hex_id, *, expected_revision, **changes):
        allowed = {'title', 'body', 'tags', 'enabled', 'accepted', 'authorship', 'provenance'}
        if set(changes) - allowed:
            raise GeneralHexError("unsupported General Hex fields")
        with self._connection(write=True) as conn:
            before = self._get(conn, owner_id, hex_id)
            if before['revision'] != _revision(expected_revision):
                raise GeneralHexConflict("General Hex changed; refresh before saving")
            values = dict(before, **changes)
            # Editing assisted content invalidates its earlier acceptance unless
            # this explicit save accepts the reviewed new body.
            if values['authorship'] == 'agent' and ('body' in changes or 'authorship' in changes) and 'accepted' not in changes:
                values['accepted'] = False
            return self._save(conn, before['owner_id'], before['hex_id'], before['revision'] + 1, values)

    def restore(self, owner_id, hex_id, *, revision, expected_revision):
        with self._connection(write=True) as conn:
            before = self._get(conn, owner_id, hex_id, include_deleted=True)
            if before['revision'] != _revision(expected_revision):
                raise GeneralHexConflict("General Hex changed; refresh before restoring")
            original = self._get(conn, owner_id, hex_id, revision)
            values = dict(original, deleted=False)
            values['provenance'] = dict(original['provenance'], restored_from_revision=revision)
            return self._save(conn, before['owner_id'], before['hex_id'], before['revision'] + 1, values)

    def delete(self, owner_id, hex_id, *, expected_revision):
        with self._connection(write=True) as conn:
            before = self._get(conn, owner_id, hex_id)
            if before['revision'] != _revision(expected_revision):
                raise GeneralHexConflict("General Hex changed; refresh before deleting")
            return self._save(conn, before['owner_id'], before['hex_id'], before['revision'] + 1, dict(before, deleted=True, enabled=False))

    def _context(self, conn, owner_id, context_kind, context_id):
        owner_id, context_id = _identity(owner_id), _identity(context_id)
        if context_kind not in ('workspace', 'task'):
            raise GeneralHexError("context_kind must be workspace or task")
        row = conn.execute("SELECT revision,tags_json,updated_at FROM fm_general_hex_context_tags WHERE owner_id=? AND context_kind=? AND context_id=?", (owner_id, context_kind, context_id)).fetchone()
        return dict(context_kind=context_kind, context_id=context_id, revision=row[0] if row else 0, tags=json.loads(row[1]) if row else [], updated_at=row[2] if row else None)

    def get_context_tags(self, owner_id, context_kind, context_id):
        with self._connection() as conn:
            return self._context(conn, owner_id, context_kind, context_id)

    def set_context_tags(self, owner_id, context_kind, context_id, *, tags, expected_revision):
        with self._connection(write=True) as conn:
            before = self._context(conn, owner_id, context_kind, context_id)
            if before['revision'] != _revision(expected_revision):
                raise GeneralHexConflict("Context tags changed; refresh before saving")
            labels, now = normalize_tags(tags), datetime.now(timezone.utc).isoformat()
            conn.execute('''INSERT INTO fm_general_hex_context_tags VALUES(?,?,?,?,?,?)
                ON CONFLICT(owner_id,context_kind,context_id) DO UPDATE SET revision=excluded.revision,tags_json=excluded.tags_json,updated_at=excluded.updated_at''', (_identity(owner_id), context_kind, _identity(context_id), before['revision'] + 1, _json(labels), now))
            return self._context(conn, owner_id, context_kind, context_id)

    def read_composition_inputs(self, owner_id, *, workspace_id=None, task_id=None):
        with self._connection() as conn:
            return dict(entries=self._list(conn, owner_id),
                        workspace_tags=self._context(conn, owner_id, 'workspace', workspace_id) if workspace_id else None,
                        task_tags=self._context(conn, owner_id, 'task', task_id) if task_id else None)

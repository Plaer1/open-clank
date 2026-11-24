"""Explicit local expected-candidate inventory; no work runs at import."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from .theme_emoji_discovery import Candidate, validate_original
from core.atomic_io import _path_locks

# Display/count classification only. Candidate adapters remain sole source authority.
# More specific prefixes must precede the fallback for the same actual source.
SOURCE_DESCRIPTORS = (('flags', 'noto', 'third_party/region-flags/'),
                      ('noto', 'noto', None), ('kitchen', 'kitchen', None))
_MAX_SAFE_INTEGER = 9007199254740991

def source_descriptor_ids():
    if not 1 <= len(SOURCE_DESCRIPTORS) <= 32: raise ValueError('Emoji source descriptors unavailable')
    names=[]
    for name, source, prefix in SOURCE_DESCRIPTORS:
        if (type(name) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',name) or name in names
                or type(source) is not str or not source or (prefix is not None and (type(prefix) is not str or not prefix))):
            raise ValueError('Emoji source descriptors unavailable')
        names.append(name)
    return tuple(names)


class EmojiInventory:
    def __init__(self, path: Path):
        self.path = Path(path)

    def initialize(self) -> None:
        """Create only when the consent-fenced service explicitly requests it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            expected_tables = {'candidate_versions', 'candidates', 'asset_blobs', 'catalogue_state', 'epoch_attempts'}
            table_names = {row[0] for row in tables}
            if (version not in (0, 1) or (version == 0 and tables)
                    or (version == 1 and table_names != expected_tables)):
                raise ValueError('Unsupported emoji inventory')
            db.execute('''CREATE TABLE IF NOT EXISTS candidate_versions (
                identity TEXT NOT NULL, fingerprint TEXT NOT NULL, source TEXT NOT NULL,
                path TEXT NOT NULL, mime TEXT NOT NULL, revision TEXT NOT NULL,
                git_blob TEXT, source_bytes INTEGER, render_id TEXT, state TEXT NOT NULL, digest TEXT, last_error TEXT, attempt_at TEXT,
                PRIMARY KEY(identity, fingerprint))''')
            db.execute('''CREATE TABLE IF NOT EXISTS candidates (
                identity TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS asset_blobs (
                digest TEXT PRIMARY KEY, mime TEXT NOT NULL, original BLOB NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS catalogue_state (
                id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL, scan_cursor TEXT, total_bytes INTEGER NOT NULL, prune_cursor TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS epoch_attempts (
                epoch_id TEXT NOT NULL, identity TEXT NOT NULL, fingerprint TEXT NOT NULL,
                PRIMARY KEY(epoch_id,identity,fingerprint))''')
            db.execute('INSERT OR IGNORE INTO catalogue_state(id,generation,scan_cursor,total_bytes,prune_cursor) VALUES(1,0,NULL,0,NULL)')
            db.execute('PRAGMA user_version=1')

    def merge(self, candidates: list[Candidate], *, replace_current: bool = True) -> None:
        if len(candidates) > 10000:
            raise ValueError('Candidate batch exceeds limit')
        fingerprints = []
        for c in candidates:
            c.url()
            encoded = json.dumps([c.source,c.path,c.mime,c.revision,c.git_blob,c.source_bytes,c.render_id], separators=(',', ':')).encode()
            fingerprints.append(hashlib.sha256(encoded).hexdigest())
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True)) as db, db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported emoji inventory')
            for c, fingerprint in zip(candidates, fingerprints):
                db.execute('''INSERT OR IGNORE INTO candidate_versions
                    (identity,fingerprint,source,path,mime,revision,git_blob,source_bytes,render_id,state) VALUES(?,?,?,?,?,?,?,?,?,'pending')''',
                    (c.identity,fingerprint,c.source,c.path,c.mime,c.revision,c.git_blob,c.source_bytes,c.render_id))
                if replace_current:
                    db.execute('''INSERT INTO candidates(identity,fingerprint) VALUES(?,?)
                        ON CONFLICT(identity) DO UPDATE SET fingerprint=excluded.fingerprint''',
                        (c.identity,fingerprint))
                else:
                    db.execute('INSERT OR IGNORE INTO candidates(identity,fingerprint) VALUES(?,?)',
                        (c.identity,fingerprint))

    def pending(self, limit: int = 100, *, epoch_id: str | None = None) -> list[Candidate]:
        if not 1 <= limit <= 1000:
            raise ValueError('Invalid candidate limit')
        if epoch_id is not None and (not isinstance(epoch_id, str) or not 1 <= len(epoch_id) <= 128):
            raise ValueError('Invalid acquisition epoch')
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True)) as db, db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported emoji inventory')
            rows = db.execute('''SELECT v.identity,v.source,v.path,v.mime,v.revision,v.git_blob,v.source_bytes,v.render_id
                FROM candidates c JOIN candidate_versions v
                ON c.identity=v.identity AND c.fingerprint=v.fingerprint
                WHERE v.state != ? AND (? IS NULL OR NOT EXISTS (
                  SELECT 1 FROM epoch_attempts e WHERE e.epoch_id=?
                  AND e.identity=v.identity AND e.fingerprint=v.fingerprint))
                ORDER BY CASE WHEN v.identity <= COALESCE((SELECT scan_cursor FROM catalogue_state WHERE id=1),'') THEN 1 ELSE 0 END, v.identity LIMIT ?''', ('ready',epoch_id,epoch_id,limit)).fetchall()
        return [Candidate(*row) for row in rows]

    def epoch_prune_page(self) -> list[str]:
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as db, db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported emoji inventory')
            rows = db.execute("""SELECT DISTINCT epoch_id FROM epoch_attempts ORDER BY
                CASE WHEN epoch_id <= COALESCE((SELECT prune_cursor FROM catalogue_state WHERE id=1),'')
                THEN 1 ELSE 0 END, epoch_id LIMIT 256""").fetchall()
        return [row[0] for row in rows]

    def prune_terminal_epochs(self, epoch_ids: list[str], scanned: list[str]) -> None:
        """Remove attempts only for host-proven terminal task runs."""
        if (len(scanned) > 256 or not set(epoch_ids).issubset(scanned)
                or any(not isinstance(x, str) or not 1 <= len(x) <= 128 for x in scanned)):
            raise ValueError('Invalid terminal epoch set')
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True)) as db, db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported emoji inventory')
            db.executemany('DELETE FROM epoch_attempts WHERE epoch_id=?', ((x,) for x in epoch_ids))
            if scanned:
                db.execute('UPDATE catalogue_state SET prune_cursor=? WHERE id=1', (scanned[-1],))

    def record_failure(self, candidate: Candidate, *, attempted_at: str, error: str, epoch_id: str | None = None) -> None:
        """Record a bounded failure without excluding the candidate from retry."""
        candidate.url()
        if not isinstance(attempted_at, str) or not 1 <= len(attempted_at) <= 40:
            raise ValueError('Invalid attempt timestamp')
        if not isinstance(error, str) or not 1 <= len(error) <= 240:
            raise ValueError('Invalid attempt error')
        encoded = json.dumps([candidate.source,candidate.path,candidate.mime,candidate.revision,candidate.git_blob,candidate.source_bytes,candidate.render_id], separators=(',', ':')).encode()
        fingerprint = hashlib.sha256(encoded).hexdigest()
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True)) as db, db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported emoji inventory')
            current = db.execute('SELECT fingerprint FROM candidates WHERE identity=?', (candidate.identity,)).fetchone()
            if not current or current[0] != fingerprint:
                raise ValueError('Emoji candidate changed')
            db.execute('''UPDATE candidate_versions SET state='failed',last_error=?,attempt_at=?
                WHERE identity=? AND fingerprint=? AND state!='ready' ''',
                (error,attempted_at,candidate.identity,fingerprint))
            if epoch_id is not None:
                if not isinstance(epoch_id, str) or not 1 <= len(epoch_id) <= 128:
                    raise ValueError('Invalid acquisition epoch')
                db.execute('INSERT OR IGNORE INTO epoch_attempts VALUES(?,?,?)',
                           (epoch_id,candidate.identity,fingerprint))
                db.execute('UPDATE catalogue_state SET scan_cursor=? WHERE id=1', (candidate.identity,))

    def publish(self, candidate: Candidate, data: bytes, *, local_state, account_id: str,
                expected: dict, current_account, epoch_id: str | None = None) -> int:
        """Publish only with current consent; caller owns the installation lease."""
        if candidate.source == 'noto' and (candidate.git_blob is None or candidate.source_bytes is None):
            raise ValueError('Noto source proof is required')
        digest = validate_original(candidate, data)
        consent_path = local_state._owner_path(account_id)
        # Consent transitions acquire this same cross-process path lock.
        with _path_locks([str(consent_path)]):
            if (current_account() != account_id or local_state.obligation(account_id) != expected
                    or expected.get('consent') != 'accepted'):
                raise PermissionError('Emoji consent changed')
            with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True)) as db, db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                    raise ValueError('Unsupported emoji inventory')
                row = db.execute('''SELECT v.fingerprint,v.source,v.path,v.mime,v.revision,v.git_blob,v.source_bytes,v.render_id,v.state,v.digest
                    FROM candidates c JOIN candidate_versions v
                    ON c.identity=v.identity AND c.fingerprint=v.fingerprint WHERE c.identity=?''',
                    (candidate.identity,)).fetchone()
                if not row or tuple(row[1:8]) != (candidate.source,candidate.path,candidate.mime,candidate.revision,candidate.git_blob,candidate.source_bytes,candidate.render_id):
                    raise ValueError('Emoji candidate changed')
                prior = db.execute('SELECT 1 FROM asset_blobs WHERE digest=?', (digest,)).fetchone()
                if not prior:
                    total = db.execute('SELECT total_bytes FROM catalogue_state WHERE id=1').fetchone()
                    if not total or type(total[0]) is not int or total[0] < 0 or total[0] + len(data) > 8 * 1024 * 1024 * 1024:
                        raise ValueError('Emoji artwork storage budget reached')
                    db.execute('INSERT INTO asset_blobs(digest,mime,original) VALUES(?,?,?)',
                               (digest,candidate.mime,data))
                    db.execute('UPDATE catalogue_state SET total_bytes=total_bytes+? WHERE id=1', (len(data),))
                stored = db.execute('SELECT mime,original FROM asset_blobs WHERE digest=?', (digest,)).fetchone()
                if not stored or stored[0] != candidate.mime or bytes(stored[1]) != data:
                    raise ValueError('Emoji digest conflict')
                db.execute('UPDATE candidate_versions SET state=?,digest=?,last_error=NULL WHERE identity=? AND fingerprint=?',
                           ('ready',digest,candidate.identity,row[0]))
                if epoch_id is not None:
                    if not isinstance(epoch_id, str) or not 1 <= len(epoch_id) <= 128:
                        raise ValueError('Invalid acquisition epoch')
                    db.execute('INSERT OR IGNORE INTO epoch_attempts VALUES(?,?,?)',
                               (epoch_id,candidate.identity,row[0]))
                    db.execute('UPDATE catalogue_state SET scan_cursor=? WHERE id=1', (candidate.identity,))
                if row[8] != 'ready' or row[9] != digest:
                    db.execute('UPDATE catalogue_state SET generation=generation+1 WHERE id=1')
                return db.execute('SELECT generation FROM catalogue_state WHERE id=1').fetchone()[0]

    def has_ready(self) -> bool:
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as db, db:
            db.execute('PRAGMA query_only=ON')
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported emoji inventory')
            row = db.execute("""SELECT v.render_id FROM candidates c JOIN candidate_versions v
                ON c.identity=v.identity
                WHERE v.state='ready' AND v.render_id IS NOT NULL ORDER BY v.render_id LIMIT 1""").fetchone()
        if not row:
            return False
        self.read_ready(row[0])
        return True

    def verify_deletable(self) -> None:
        """Refuse unknown schemas before removing the local overlay."""
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as db, db:
            db.execute('PRAGMA query_only=ON')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if version != 1 or tables != {'candidate_versions','candidates','asset_blobs','catalogue_state','epoch_attempts'}:
                raise ValueError('Unsupported emoji inventory')
        self.statistics()

    def statistics(self) -> dict:
        """Project current expected/ready/failure totals without creating a store."""
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as db, db:
            db.execute('PRAGMA query_only=ON')
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported emoji inventory')
            totals = db.execute("""SELECT COUNT(*),
                COALESCE(SUM(CASE WHEN v.state='ready' THEN 1 ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN v.state='failed' THEN 1 ELSE 0 END),0)
                FROM candidates c JOIN candidate_versions v
                ON c.identity=v.identity AND c.fingerprint=v.fingerprint""").fetchone()
            names = source_descriptor_ids()
            cases=[]; parameters=[]
            for name, source, prefix in SOURCE_DESCRIPTORS:
                if prefix is None:
                    cases.append('WHEN v.source=? THEN ?'); parameters.extend((source,name))
                else:
                    cases.append('WHEN v.source=? AND substr(v.path,1,length(?))=? THEN ?'); parameters.extend((source,prefix,prefix,name))
            groups = db.execute("""SELECT CASE """+' '.join(cases)+""" ELSE NULL END AS kind,
                COUNT(*), COALESCE(SUM(CASE WHEN v.state='ready' THEN 1 ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN v.state='failed' THEN 1 ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN v.state='ready' THEN COALESCE(length(b.original),0) ELSE 0 END),0)
                FROM candidates c JOIN candidate_versions v
                ON c.identity=v.identity AND c.fingerprint=v.fingerprint
                LEFT JOIN asset_blobs b ON v.digest=b.digest GROUP BY kind""",parameters).fetchall()
            state = db.execute('SELECT generation,total_bytes FROM catalogue_state WHERE id=1').fetchone()
        if not state or any(type(n) is not int or not 0 <= n <= _MAX_SAFE_INTEGER for n in (*totals,*state)):
            raise ValueError('Emoji inventory unavailable')
        by_source = {name:{'expected':0,'readyCount':0,'pendingCount':0,'failedCount':0,'bytes':0}
                     for name in names}
        for name, expected, ready, failed, size in groups:
            if name not in by_source or any(type(n) is not int or not 0 <= n <= _MAX_SAFE_INTEGER for n in (expected,ready,failed,size)) or ready+failed>expected:
                raise ValueError('Emoji inventory unavailable')
            by_source[name] = {'expected':expected, 'readyCount':ready, 'failedCount':failed,
                               'pendingCount':expected-ready-failed, 'bytes':size}
        return {'catalogueGeneration':state[0], 'bytes':state[1], 'expected':totals[0],
                'readyCount':totals[1], 'failedCount':totals[2], 'pendingCount':totals[0]-totals[1]-totals[2],
                'bySource':by_source}

    def catalogue_generation(self) -> int:
        """Return the local generation without creating an absent store."""
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as db, db:
            db.execute('PRAGMA query_only=ON')
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported emoji inventory')
            row = db.execute('SELECT generation FROM catalogue_state WHERE id=1').fetchone()
        if not row or type(row[0]) is not int or row[0] < 0:
            raise ValueError('Emoji catalogue unavailable')
        return row[0]

    def ready_catalog(self, kind: str, *, limit: int = 200000) -> list[str]:
        """Read ready render identities only; never create a missing store."""
        if kind not in {'g', 'k'} or not 1 <= limit <= 200000:
            raise ValueError('Invalid emoji catalogue request')
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as db, db:
            db.execute('PRAGMA query_only=ON')
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported emoji inventory')
            rows = db.execute('''SELECT DISTINCT v.render_id FROM candidates c
                JOIN candidate_versions v ON c.identity=v.identity
                WHERE v.state='ready' AND v.render_id LIKE ? ORDER BY v.render_id LIMIT ?''',
                (kind + ':%',limit+1)).fetchall()
        if len(rows) > limit:
            raise ValueError('Emoji catalogue exceeds limit')
        return [row[0] for row in rows]

    def read_ready(self, render_id: str) -> tuple[bytes, str, str]:
        """Return a checked, deterministic ready original, preferring PNG."""
        import re
        if not isinstance(render_id, str) or not re.fullmatch(r'[gk]:[0-9a-f]+(?:-[0-9a-f]+)*(?:_[0-9a-f]+(?:-[0-9a-f]+)*)?', render_id):
            raise KeyError('Invalid emoji identity')
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as db, db:
            db.execute('PRAGMA query_only=ON')
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported emoji inventory')
            row = db.execute('''SELECT b.original,b.mime,b.digest,v.identity,v.source,v.path,v.revision,v.git_blob,v.source_bytes,v.render_id FROM candidates c
                JOIN candidate_versions v ON c.identity=v.identity
                JOIN asset_blobs b ON v.digest=b.digest
                WHERE v.state='ready' AND v.render_id=?
                ORDER BY CASE WHEN v.fingerprint=c.fingerprint THEN 0 ELSE 1 END,
                    CASE b.mime WHEN 'image/png' THEN 0 ELSE 1 END, v.rowid DESC, v.identity LIMIT 1''', (render_id,)).fetchone()
        if not row:
            raise KeyError('Emoji identity unavailable')
        data, mime, digest = bytes(row[0]), row[1], row[2]
        if mime not in {'image/png','image/svg+xml'} or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Emoji artwork changed')
        candidate = Candidate(row[3],row[4],row[5],mime,row[6],row[7],row[8],row[9])
        if candidate.render_id != render_id or validate_original(candidate,data) != digest:
            raise ValueError('Emoji artwork failed validation')
        return data, mime, digest

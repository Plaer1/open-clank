"""Bounded read-only observations of positively source-linked S05 jobs."""
from __future__ import annotations

from dataclasses import asdict
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from src.memery_external_sources import SourceRef, _source_ref_error
from src.memery_provider import _canonical_index_sql

_INDEXES = {
    's04_s05_source_account': 'CREATE INDEX s04_s05_source_account ON fm_v2_s05_job_refs(source_account_id,owner_id,job_id)',
    's04_s05_invalid_owner': 'CREATE INDEX s04_s05_invalid_owner ON fm_v2_s05_job_refs(owner_id) WHERE NOT json_valid(source_ref_json)',
    's04_s05_invalid_account': 'CREATE INDEX s04_s05_invalid_account ON fm_v2_s05_job_refs(source_account_id) WHERE NOT json_valid(source_ref_json)',
    's04_s05_ref_account': "CREATE INDEX s04_s05_ref_account ON fm_v2_s05_job_refs(json_extract(source_ref_json,'$.account_id'),owner_id,job_id) WHERE json_valid(source_ref_json)",
}
_STATES = frozenset(('queued', 'active', 'awaiting_review', 'retry_wait', 'succeeded', 'failed_terminal', 'cancelled'))
_JOB = re.compile(r's05_[0-9a-f]{32}\Z')


def qualification_current(path, *, coverage, clock):
    """Check the existing qualified generation without qualifying or mutating it."""
    uri = Path(path).resolve(strict=True).as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=.2, isolation_level=None)) as db:
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT verified,coverage_generation,clock_generation FROM fm_v2_s05_occupancy WHERE singleton=1').fetchone()
        return row == (1, coverage, clock)


def source_job_page(path, *, owner, account, memory_workspace, chat, source_workspace, coverage, clock, after='', limit=16, preparation=None):
    """Read safe fixed columns only; caller owns account, source and profile guards.

    The returned SourceRefs are private evidence for a further current-source
    check, never UI data. SQL work/value/time exhaustion fails closed.
    """
    values = (owner, account, memory_workspace, chat, coverage, clock)
    if (any(type(x) is not str or not x or x != x.strip() or len(x) > 512 for x in values)
            or (source_workspace is not None and (type(source_workspace) is not str or not source_workspace or source_workspace != source_workspace.strip() or len(source_workspace) > 512))
            or type(limit) is not int or not 1 <= limit <= 16 or type(after) is not str
            or (after and not _JOB.fullmatch(after))):
        raise ValueError('memory job observation unavailable')
    if source_workspace is None:
        from src.memery_database import GuardedStorePreparation
        if type(preparation) is not GuardedStorePreparation or preparation.database_path != str(Path(path).resolve(strict=True)):
            raise ValueError('account chat profile unavailable')
        preparation.assert_account_chat()
    deadline = time.monotonic() + .5
    uri = Path(path).resolve(strict=True).as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=.2, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        if not hasattr(db, 'setlimit') or not hasattr(sqlite3, 'SQLITE_LIMIT_LENGTH'):
            raise ValueError('memory job read budget unavailable')
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 262144)
        remaining = [200000]
        def progress():
            remaining[0] -= 500
            return remaining[0] <= 0 or time.monotonic() >= deadline
        db.set_progress_handler(progress, 500)
        db.execute('BEGIN')
        try:
            versions = db.execute('SELECT version FROM fm_v2_s05_schema').fetchall()
            if len(versions) != 1 or versions[0][0] != 4:
                raise ValueError('memory job profile unavailable')
            if source_workspace is None:
                marker = db.execute("SELECT value FROM fm_meta WHERE key='openclank.s05.account_chat.v1'").fetchone()
                if marker is None or tuple(marker) != ('1',): raise ValueError('account chat profile unavailable')
            occupancy = db.execute('SELECT verified,coverage_generation,clock_generation FROM fm_v2_s05_occupancy WHERE singleton=1').fetchone()
            if not occupancy or tuple(occupancy) != (1, coverage, clock):
                raise ValueError('memory job qualification unavailable')
            found = {row['name']: row['sql'] for row in db.execute(
                "SELECT name,sql FROM sqlite_schema WHERE type='index' AND name IN (?,?,?,?)", tuple(_INDEXES))}
            if any(_canonical_index_sql(found.get(name)) != _canonical_index_sql(sql) for name, sql in _INDEXES.items()):
                raise ValueError('memory job indexes unavailable')
            if db.execute("SELECT 1 FROM fm_v2_s05_job_refs INDEXED BY s04_s05_invalid_owner WHERE owner_id=? AND NOT json_valid(source_ref_json) LIMIT 1", (owner,)).fetchone():
                raise ValueError('memory job association unavailable')
            if db.execute("SELECT 1 FROM fm_v2_s05_job_refs INDEXED BY s04_s05_ref_account WHERE json_valid(source_ref_json) AND json_extract(source_ref_json,'$.account_id')=? AND owner_id=? AND source_account_id!=? LIMIT 1", (account, owner, account)).fetchone():
                raise ValueError('memory job association unavailable')
            rows = db.execute("""SELECT r.job_id,r.source_key,r.source_hash,r.policy_fingerprint,r.binding_fingerprint,r.source_workspace_id,r.target_workspace_id,
                CASE WHEN length(CAST(r.source_ref_json AS BLOB))<=16384 THEN r.source_ref_json END AS source_json,
                j.kind,j.state,j.updated_at,j.workspace_key,j.project_key
                FROM fm_v2_s05_job_refs r INDEXED BY s04_s05_source_account
                JOIN fm_v2_jobs j ON j.owner_id=r.owner_id AND j.job_id=r.job_id
                WHERE r.source_account_id=? AND r.owner_id=? AND r.job_id>? AND r.source_workspace_id IS ?
                  AND r.target_workspace_id=? AND j.kind IN ('s05_host_user_capture','completed_turn_review')
                  AND CASE WHEN length(CAST(r.source_ref_json AS BLOB))<=16384 THEN
                    CASE WHEN json_valid(r.source_ref_json) THEN json_extract(r.source_ref_json,'$.chat_id')=? ELSE 0 END
                    ELSE 0 END
                ORDER BY r.job_id LIMIT ?""", (account, owner, after, source_workspace, memory_workspace, chat, limit + 1)).fetchall()
            items = []
            for row in rows[:limit]:
                if (not _JOB.fullmatch(row['job_id'] or '') or row['state'] not in _STATES
                        or row['workspace_key'] != memory_workspace or row['project_key'] != ''
                        or type(row['updated_at']) is not str or len(row['updated_at']) > 64
                        or type(row['source_json']) is not str):
                    raise ValueError('memory job row unavailable')
                try:
                    if datetime.fromisoformat(row['updated_at'].replace('Z', '+00:00')).tzinfo is None:
                        raise ValueError('timestamp missing timezone')
                except ValueError as exc:
                    raise ValueError('memory job timestamp unavailable') from exc
                value = json.loads(row['source_json'])
                if type(value) is not dict:
                    raise ValueError('memory source unavailable')
                ref = SourceRef(**value)
                source_key = hashlib.sha256(json.dumps([row['kind'], asdict(ref)], sort_keys=True,
                    separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
                if (_source_ref_error(ref) or ref.revision != 1
                        or ref.version != (2 if source_workspace is None else 1)
                        or (ref.account_id, ref.chat_id, ref.workspace_id) != (account, chat, source_workspace)
                        or ref.content_hash != row['source_hash']
                        or row['source_key'] != source_key or not re.fullmatch(r'[0-9a-f]{64}', row['policy_fingerprint'] or '')
                        or not re.fullmatch(r'[0-9a-f]{64}', row['binding_fingerprint'] or '')
                        or json.dumps(asdict(ref), sort_keys=True, separators=(',', ':')) != row['source_json']):
                    raise ValueError('memory source unavailable')
                items.append({'id': row['job_id'], 'kind': row['kind'], 'state': row['state'],
                              'updated_at': row['updated_at'], 'source': ref})
            db.commit()
            if source_workspace is None: preparation.assert_account_chat()
            return {'items': items, 'after': rows[limit - 1]['job_id'] if len(rows) > limit else None}
        except BaseException:
            db.rollback()
            raise
        finally:
            db.set_progress_handler(None, 0)

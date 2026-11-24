"""Read-only helpers for the journal delta (BQ-8): the S05 job store view used by dedupe, and the page view of the route.

Nothing in this module moves a cursor or writes anything. The ``journal_delta.v1`` cursor is moved only by
``ConversationArchive.ack_journal_delta`` (typed outcome proof + CAS); a read route can never advance it.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

PREF_KEY = 'memory_policy_consolidation_enabled'  # read as `is True`; unset = Off
_JOB_STATES = frozenset(('queued', 'active', 'awaiting_review', 'retry_wait', 'succeeded', 'failed_terminal', 'cancelled'))
_OVERLAP_KINDS = ('completed_turn_review', 'assistant_interpretation_review')


def load_job_rows(db_path, *, memory_owner, account_id, chat_id, events):
    """Read-only completed-turn job rows for exactly these events, scoped by owner, account and chat.

    Opens the store like ``memory_job_observations.source_job_page`` (mode=ro +
    ``PRAGMA query_only``). A store without the S05 tables (legacy-v16) or a
    schema other than version 4 (the only one ``memory_job_admission._conn``
    accepts) yields ``{'status': 'unavailable'}``.
    """
    try:
        uri = Path(db_path).resolve(strict=True).as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=.2, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fm_v2_s05_schema'").fetchone() is None:
                return {'status': 'unavailable'}
            if [tuple(r) for r in db.execute('SELECT version FROM fm_v2_s05_schema').fetchall()] != [(4,)]:
                return {'status': 'unavailable'}
            ids = sorted({e['message_id'] for e in events})
            rows = []
            db.execute('BEGIN')
            if ids:
                marks = ','.join('?' * len(ids))
                rows = db.execute(
                    "SELECT j.state,r.source_account_id,r.source_ref_json FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r USING(owner_id,job_id) "
                    "WHERE j.owner_id=? AND j.kind='completed_turn_review' AND r.source_account_id=? AND json_valid(r.source_ref_json) "
                    "AND json_extract(r.source_ref_json,'$.chat_id')=? AND json_extract(r.source_ref_json,'$.message_id') IN (" + marks + ')',
                    (memory_owner, account_id, chat_id, *ids)).fetchall()
            db.commit()
    except (sqlite3.Error, OSError, ValueError):
        return {'status': 'unavailable'}
    out = []
    for row in rows:
        try:
            ref = json.loads(row['source_ref_json'])
            out.append({'state': row['state'], 'account_id': ref['account_id'], 'chat_id': ref['chat_id'], 'actor_id': ref['actor_id'],
                        'message_id': ref['message_id'], 'part_id': ref['part_id'], 'revision': ref['revision']})
        except (ValueError, TypeError, KeyError):
            out.append({'state': None, 'account_id': None, 'chat_id': None})  # unparseable: reader drops and counts it
    return {'status': 'ok', 'rows': out}


def _read_only(db_path, query, args):
    """Rows of one read-only query on a version-4 S05 store (same open recipe as ``load_job_rows``), else None."""
    try:
        uri = Path(db_path).resolve(strict=True).as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=.2, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fm_v2_s05_schema'").fetchone() is None:
                return None
            if [tuple(r) for r in db.execute('SELECT version FROM fm_v2_s05_schema').fetchall()] != [(4,)]:
                return None
            db.execute('BEGIN')
            rows = db.execute(query, args).fetchall()
            db.commit()
            return rows
    except (sqlite3.Error, OSError, ValueError):
        return None




def load_job_source_refs(db_path, *, memory_owner, account_id, chat_id, entries):
    """``{seq: SourceRef}`` of the succeeded completed-turn review of each entry (its recorded hash, workspace); {} if unavailable."""
    from dataclasses import asdict
    from src.memery_external_sources import SourceRef
    ids = sorted({e['message_id'] for e in entries})
    if not ids:
        return {}
    marks = ','.join('?' * len(ids))
    rows = _read_only(db_path,
        "SELECT r.source_ref_json FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r USING(owner_id,job_id) "
        "WHERE j.owner_id=? AND j.kind='completed_turn_review' AND j.state='succeeded' AND r.source_account_id=? AND json_valid(r.source_ref_json) "
        "AND json_extract(r.source_ref_json,'$.chat_id')=? AND json_extract(r.source_ref_json,'$.message_id') IN (" + marks + ')',
        (memory_owner, account_id, chat_id, *ids))
    by_key = {}
    for row in rows or ():
        try:
            ref = SourceRef(**json.loads(row['source_ref_json']))
            if json.dumps(asdict(ref), sort_keys=True, separators=(',', ':')) == row['source_ref_json']:
                by_key[(ref.message_id, ref.revision)] = ref
        except (ValueError, TypeError):
            pass
    return {e['seq']: by_key[(e['message_id'], e['revision'])] for e in entries if (e['message_id'], e['revision']) in by_key}




def load_overlap_jobs(db_path, *, memory_owner, account_id, chat_id, message_ids):
    """Existing ``completed_turn_review`` / paired jobs of this account and chat for these message ids (exact identity, contract B).

    Returns ``None`` when the job store cannot answer (the caller treats that as a blocking gap, never as "no overlap"), else a
    list of ``{'kind','state','job_id','message_id','part_id','revision','content_hash','receipt_id','purpose'}``. ``receipt_id`` and
    ``purpose`` are read only from an explicit ``result_json`` field; their absence means the job is NOT proven coverage.
    """
    ids = sorted(set(message_ids))
    if not ids:
        return []
    marks = ','.join('?' * len(ids))
    kinds = ','.join('?' * len(_OVERLAP_KINDS))
    rows = _read_only(db_path,
        "SELECT j.kind,j.state,j.job_id,j.result_json,r.source_ref_json FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r USING(owner_id,job_id) "
        "WHERE j.owner_id=? AND j.kind IN (" + kinds + ") AND r.source_account_id=? AND json_valid(r.source_ref_json) "
        "AND json_extract(r.source_ref_json,'$.chat_id')=? AND json_extract(r.source_ref_json,'$.message_id') IN (" + marks + ')',
        (memory_owner, *_OVERLAP_KINDS, account_id, chat_id, *ids))
    if rows is None:
        return None
    out = []
    for row in rows:
        try:
            ref = json.loads(row['source_ref_json'])
            result = json.loads(row['result_json']) if row['result_json'] else {}
            out.append({'kind': row['kind'], 'state': row['state'] if row['state'] in _JOB_STATES else 'unknown', 'job_id': row['job_id'],
                        'message_id': ref['message_id'], 'part_id': ref['part_id'], 'revision': ref['revision'],
                        'content_hash': ref.get('content_hash'), 'receipt_id': result.get('receipt_id') if type(result) is dict else None,
                        'purpose': result.get('purpose') if type(result) is dict else None})
        except (ValueError, TypeError, KeyError):
            out.append({'kind': row['kind'], 'state': 'unknown', 'job_id': row['job_id'], 'message_id': None, 'part_id': None,
                        'revision': None, 'content_hash': None, 'receipt_id': None, 'purpose': None})
    return out


def overlap_rows_for(db_path, *, memory_owner, account_id, chat_id):
    """``job_rows(row)`` for :func:`fm_journal_delta.dedupe_for`: jobs of the same part and revision; an unreadable store blocks."""
    cache = {}

    def jobs(row):
        key = row['message_id']
        if key not in cache:
            cache[key] = load_overlap_jobs(db_path, memory_owner=memory_owner, account_id=account_id, chat_id=chat_id, message_ids=[key])
        found = cache[key]
        if found is None:
            return [{'kind': 'unknown', 'state': 'unknown', 'job_id': None}]   # fail closed: overlap unknown blocks the page
        return [job for job in found if (job['part_id'], job['revision']) == (row['part_id'], row['revision']) or job['message_id'] is None]
    return jobs


def page_view(page):
    """The content-free body of the read-only route: class counts and cursor positions (no ids, no digest, no account)."""
    from src.openclank import fm_journal_coverage as core
    counts = core.counts(page['rows'])
    return {'state': 'ok', 'counts': counts, 'ack_seq': page['ack_seq'], 'high_seq': page['high_seq'], 'end_seq': page['end_seq'],
            'blocking_gaps': core.blocking_gaps(page['rows'])}

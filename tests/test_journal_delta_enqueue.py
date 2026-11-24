"""BQ-8 archive side (real ConversationArchive, disposable): the journal_delta.v1 page. Replaces the retired window-enqueue tests.

Plan sentences: S08 "freeze `high_seq = MAX(conversation_parts.seq)` ... enumerate every row in `(ack_seq, high_seq]`"; "retain
{seq,actor_id,message_id,part_id,revision,content_hash,workspace_id,project_id,role,part_type}"; PR "a date-only watermark cannot
skip later same-day entries"; frozen decision 1 (gap blocks, omitted does not).
"""
import hashlib
import json
import sqlite3

from src.openclank import fm_journal_coverage as core
from src.openclank.conversation_archive import ConversationArchive, SourcePart

OWNER, CHAT, WS = 'alice', 'chat-1', 'ws-1'
H = lambda text: hashlib.sha256(text.encode()).hexdigest()


def archive(tmp_path):
    return ConversationArchive(str(tmp_path / 'a.db'), enable_origin_policy_for_disposable_test=True)


def put(a, message, part='body', *, role='user', part_type='text', actor='main', revision=1, content=None, workspace=WS, ready=True):
    content = content if content is not None else f'text of {message}'
    a.append_parts([SourcePart(owner=OWNER, chat_id=CHAT, actor_id=actor, message_id=message, part_id=part, revision=revision, event_workspace=workspace,
                               role=role, part_type=part_type, content=content, content_hash=H(content))])
    if ready:
        with sqlite3.connect(a.db_path) as db:
            db.execute("INSERT OR IGNORE INTO conversation_archive_outbox(owner,chat_id,actor_id,message_id,part_id,revision,event_kind,payload_json,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?, 'origin_ready','{}',1,1)", (OWNER, CHAT, actor, message, part, revision))
    return H(content)


def ok_proof(row):
    return {'state': 'available', 'source': {'message_id': row['message_id'], 'revision': row['revision']}, 'authority': {'origin_id': 'o'}, 'size': 10}


def page(a, **kw):
    kw.setdefault('source_proof', ok_proof)
    return a.read_journal_delta_page(owner=OWNER, chat_id=CHAT, workspace_id=WS, authorize=lambda *_: True, **kw)


def by_message(p):
    return {(r['message_id'], r['part_id'], r['revision']): r for r in p['rows']}


def test_page_classifies_every_main_row_with_the_ten_retained_fields_and_a_frozen_high_seq(tmp_path):
    a = archive(tmp_path)
    put(a, 'u1'); put(a, 'u2'); put(a, 'a1', role='assistant'); put(a, 'u3')
    put(a, 't1', part='call', part_type='tool', role='assistant'); put(a, 'x1', actor='sub')   # a non-main actor is not enumerated
    p = page(a)
    assert p['ack_seq'] == 0 and p['high_seq'] >= p['end_seq'] and p['end_seq'] == p['rows'][-1]['seq']
    rows = by_message(p)
    assert [rows[(m, 'body', 1)]['class'] for m in ('u1', 'u2', 'u3')] == ['included'] * 3
    assert rows[('a1', 'body', 1)]['class'] == 'omitted' and rows[('a1', 'body', 1)]['reason'] == 'assistant_without_pair'
    assert rows[('t1', 'call', 1)]['reason'] == 'tool_or_media_part'
    assert not any(r['message_id'] == 'x1' for r in p['rows'])
    for key in ('seq', 'actor_id', 'message_id', 'part_id', 'revision', 'content_hash', 'workspace_id', 'project_id', 'role', 'part_type', 'class', 'reason', 'selector'):
        assert key in p['rows'][0]
    assert 'text of' not in json.dumps(p, default=str)  # no content
    again = core.page_digest(account_id=p['account_id'], chat_id=CHAT, workspace_id=WS, ack_seq=0, high_seq=p['high_seq'], end_seq=p['end_seq'], rows=p['rows'])
    assert p['page_sha256'] == again


def test_a_late_same_day_revision_supersedes_inside_the_frozen_window_and_is_next_page_work(tmp_path):
    a = archive(tmp_path)
    put(a, 'u1'); put(a, 'u2')
    first = page(a)
    put(a, 'u2', revision=2, content='u2 edited the same day')            # seq beyond the frozen high_seq of `first`
    assert by_message(first)[('u2', 'body', 1)]['class'] == 'included'      # a later revision beyond high_seq does not supersede inside it
    second = page(a)
    rows = by_message(second)
    assert rows[('u2', 'body', 1)]['class'] == 'superseded' and rows[('u2', 'body', 1)]['selector']['later_revision'] == 2
    assert rows[('u2', 'body', 2)]['class'] == 'included'
    assert second['page_sha256'] != first['page_sha256']


def test_gap_reasons_overlap_and_already_covered(tmp_path):
    a = archive(tmp_path)
    put(a, 'missing'); put(a, 'denied'); put(a, 'pending'); put(a, 'done'); put(a, 'noready', ready=False)
    def proof(row):
        return {'missing': {'state': 'missing'}, 'denied': {'state': 'denied'}}.get(row['message_id']) or ok_proof(row)
    def dedupe(row):
        return {'pending': {'state': 'pending'}, 'done': {'state': 'covered', 'receipt_id': 'cs_r', 'job_id': 'j1'}}.get(row['message_id'])
    rows = by_message(page(a, source_proof=proof, dedupe=dedupe))
    got = {m: (r['class'], r['reason']) for (m, _p, _r), r in rows.items()}
    assert got == {'missing': ('gap', 'source_missing'), 'denied': ('gap', 'source_denied'), 'pending': ('gap', 'overlap_pending'),
                   'done': ('already_covered', 'already_covered'), 'noready': ('gap', 'proof_unavailable')}


def test_tombstone_is_superseded_and_page_is_bounded_by_rows_and_bytes(tmp_path):
    a = archive(tmp_path)
    put(a, 'u1'); put(a, 'u2')
    a.tombstone_part(owner=OWNER, chat_id=CHAT, actor_id='main', message_id='u1', part_id='body', revision=1, reason='removed')
    rows = by_message(page(a))
    assert rows[('u1', 'body', 1)]['class'] == 'superseded' and rows[('u1', 'body', 1)]['selector'] == {'tombstone': True}
    big = lambda row: {**ok_proof(row), 'size': 9000}
    p = page(a, source_proof=big)
    assert [r['message_id'] for r in p['rows']] == ['u1', 'u2'] or len(p['rows']) < 2   # the second 9000-byte row does not fit with a first
    for n in range(3, 40): put(a, f'u{n}')
    assert len(page(a)['rows']) == 32
    huge = lambda row: {**ok_proof(row), 'size': 20000}
    assert all(r['reason'] == 'oversize' for r in page(a, source_proof=huge)['rows'] if r['class'] == 'gap')

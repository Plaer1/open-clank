"""BQ-8 archive ACK (real ConversationArchive, disposable): CAS on the prior cursor + exact page digest + typed outcome proof.

Plan sentences: S08 "commit original outcome first, then idempotent ACK CAS"; "a gap blocks advancing that prefix";
"Re-read exact page ... under the guard before durable selection and ACK"; failure/cancel/unknown settlement is never coverage;
"the existing completed-turn staging cursor cannot be advanced"; a read moves nothing.
"""
import pytest

from src.openclank import chat_source_bridge
from src.openclank import fm_journal_coverage as core
from src.openclank.fm_journal_delta import JournalDeltaError, JournalOutcomeProof, mint_outcome_proof
from tests.test_journal_delta_enqueue import CHAT, OWNER, WS, archive, ok_proof, page, put

GUARD = object()


class FakeMapping:
    def __init__(self):
        self.valid = True

    def validate_chat_guard(self, chat_id, guard):
        if not self.valid or guard is not GUARD:
            raise ValueError('guard')


class FakeOwner:
    def __init__(self):
        self.account_id, self.mapping = OWNER, FakeMapping()


@pytest.fixture(autouse=True)
def fake_owner_type(monkeypatch):
    monkeypatch.setattr(chat_source_bridge, 'CompletedTurnLifecycleOwner', FakeOwner)


def status_for(p, state='recorded_proposal', digest='d' * 64):
    return {'version': 1, 'state': state, 'selection_sha256': digest, 'page_sha256': p['page_sha256'], 'prior_cursor': p['ack_seq'],
            'end_seq': p['end_seq'], 'job_id': 's08job_' + 'a' * 64, 'candidate_id': None, 'receipt_ids': ['cs_s08r']}


def proof_for(p, **kw):
    return mint_outcome_proof(status=status_for(p, **kw), page=p, selection_digest='d' * 64)


def ack(a, owner, p, coverage, *, expected=None, digest=None, end=None, **kw):
    return a.ack_journal_delta(owner=OWNER, chat_id=CHAT, workspace_id=WS, authorize=lambda *_: True, lifecycle_owner=owner, guard=GUARD,
        expected_cursor=p['ack_seq'] if expected is None else expected, page_digest=digest or p['page_sha256'],
        end_seq=p['end_seq'] if end is None else end, coverage=coverage, source_proof=ok_proof, **kw)


def seed(tmp_path):
    a = archive(tmp_path)
    put(a, 'u1'); put(a, 'a1', role='assistant'); put(a, 'u2')
    return a, FakeOwner()


def test_ack_advances_only_with_an_exact_page_and_a_recorded_outcome_and_replays(tmp_path):
    a, owner = seed(tmp_path)
    p = page(a)
    assert a.cursor(owner=OWNER, chat_id=CHAT, consumer='journal_delta.v1') == 0      # reading moved nothing
    assert ack(a, owner, p, [proof_for(p)]) == {'cursor': p['end_seq'], 'replayed': False}
    assert a.cursor(owner=OWNER, chat_id=CHAT, consumer='journal_delta.v1') == p['end_seq']
    assert ack(a, owner, p, [proof_for(p)]) == {'cursor': p['end_seq'], 'replayed': True}
    assert page(a)['rows'] == [] and a.cursor(owner=OWNER, chat_id=CHAT, consumer='completed_turn_review.v1') == 0
    with pytest.raises(ValueError):
        a.advance_cursor(owner=OWNER, chat_id=CHAT, consumer='journal_delta.v1', last_outbox_seq=99)   # only the typed ACK moves it


@pytest.mark.parametrize('case', ['stale_cursor', 'wrong_digest', 'missing_proof', 'wrong_end', 'guard', 'foreign_proof_page'])
def test_ack_refusals_leave_the_cursor_untouched(tmp_path, case):
    a, owner = seed(tmp_path)
    p = page(a)
    coverage = [proof_for(p)]
    kw = {}
    if case == 'stale_cursor': kw['expected'] = 7
    if case == 'wrong_digest': kw['digest'] = '0' * 64
    if case == 'missing_proof': coverage = []
    if case == 'wrong_end': kw['end'] = p['end_seq'] - 1
    if case == 'guard': owner.mapping.valid = False
    if case == 'foreign_proof_page':
        coverage = [mint_outcome_proof(status={**status_for(p), 'page_sha256': '1' * 64}, page={**p, 'page_sha256': '1' * 64}, selection_digest='d' * 64)]
    with pytest.raises(ValueError):
        ack(a, owner, p, coverage, **kw)
    assert a.cursor(owner=OWNER, chat_id=CHAT, consumer='journal_delta.v1') == 0


def test_a_gap_blocks_the_prefix_and_omitted_does_not(tmp_path):
    a, owner = seed(tmp_path)                        # includes an omitted assistant row: it never blocks (test above)
    put(a, 'u3')
    dedupe = lambda row: {'state': 'pending'} if row['message_id'] == 'u3' else None
    p = page(a, dedupe=dedupe)
    assert any(r['class'] == 'gap' and r['reason'] == 'overlap_pending' for r in p['rows'])
    with pytest.raises(ValueError, match='gap_blocks_prefix'):
        ack(a, owner, p, [proof_for(p)], dedupe=dedupe)
    assert a.cursor(owner=OWNER, chat_id=CHAT, consumer='journal_delta.v1') == 0


def test_failure_cancel_and_unknown_outcomes_cannot_be_minted_into_coverage(tmp_path):
    a, _owner = seed(tmp_path)
    p = page(a)
    for state in ('terminal_failed', 'cancelled', 'unknown', 'selected', 'absent'):
        with pytest.raises(JournalDeltaError):
            proof_for(p, state=state)
    with pytest.raises(JournalDeltaError):
        mint_outcome_proof(status=status_for(p), page=p, selection_digest='e' * 64)      # another selection's status
    with pytest.raises(ValueError):
        JournalOutcomeProof('x', 'y', 0, 1, 'proposal', ('r',), ())                       # not mintable by hand
    p2 = page(a)
    assert a.cursor(owner=OWNER, chat_id=CHAT, consumer='journal_delta.v1') == 0 and p2['rows']


def test_a_stale_page_acknowledgement_refuses_page_changed_after_a_late_same_day_revision(tmp_path):
    a, owner = seed(tmp_path)
    p = page(a)
    put(a, 'u2', revision=2, content='edited later the same day')
    with pytest.raises(ValueError, match='page_changed'):
        ack(a, owner, p, [proof_for(p)])
    assert a.cursor(owner=OWNER, chat_id=CHAT, consumer='journal_delta.v1') == 0
    q = page(a)                                       # the next page carries the superseded row and the new included revision
    assert {(r['message_id'], r['revision'], r['class']) for r in q['rows'] if r['message_id'] == 'u2'} == {('u2', 1, 'superseded'), ('u2', 2, 'included')}


def test_chat_erasure_deletes_the_cursor_row_and_a_later_ack_refuses(tmp_path):
    import sqlite3
    a, owner = seed(tmp_path)
    p = page(a)
    ack(a, owner, p, [proof_for(p)])
    a.erase_chat(owner=OWNER, chat_id=CHAT)
    with sqlite3.connect(a.db_path) as db:
        assert db.execute("SELECT count(*) FROM conversation_archive_cursor WHERE consumer='journal_delta.v1'").fetchone()[0] == 0
    with pytest.raises(ValueError):
        ack(a, owner, p, [proof_for(p)])


def test_erasure_intent_alone_refuses_the_ack_before_any_cross_store_mutation(tmp_path):
    """H4: chat_lifecycle persists `begin_chat_erasure` first; from that moment the ACK refuses `erased_chat` (cursor untouched)."""
    import sqlite3
    a, owner = seed(tmp_path)
    p = page(a)
    a.begin_chat_erasure(owner=OWNER, chat_id=CHAT)
    with pytest.raises(ValueError, match='erased_chat'):
        ack(a, owner, p, [proof_for(p)])
    with sqlite3.connect(a.db_path) as db:
        assert db.execute("SELECT count(*) FROM conversation_archive_cursor WHERE consumer='journal_delta.v1'").fetchone()[0] == 0

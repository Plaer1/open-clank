"""BQ-8 selection identity, outcome proof and the pass (FAKE NATIVE and fake archive: marked; the real-binary case waits for the signal)."""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from src.openclank import fm_journal_coverage as core
from src.openclank import fm_journal_delta as delta


BODY = 'text of m4'
H4 = hashlib.sha256(BODY.encode()).hexdigest()
SRC = {'version': 1, 'kind': 'host_user_body', 'account_id': 'acct', 'chat_id': 'c', 'actor_id': 'main', 'message_id': 'm4', 'part_id': 'body',
       'revision': 1, 'content_hash': H4, 'workspace_id': 'ws', 'project_id': None}


def page(**kw):
    rows = [{'seq': 4, 'actor_id': 'main', 'message_id': 'm4', 'part_id': 'body', 'revision': 1, 'content_hash': H4, 'workspace_id': 'ws',
             'project_id': None, 'role': 'user', 'part_type': 'text', 'class': 'included', 'reason': 'host_user_body', 'selector': {},
             'proof': {'source': SRC, 'authority': {'origin_id': 'o'}, 'size': 100}},
            {'seq': 5, 'actor_id': 'main', 'message_id': 'a5', 'part_id': 'body', 'revision': 1, 'content_hash': 'h5', 'workspace_id': 'ws',
             'project_id': None, 'role': 'assistant', 'part_type': 'text', 'class': 'omitted', 'reason': 'assistant_without_pair',
             'selector': {'seq': 5}, 'proof': None}]
    p = {'account_id': 'acct', 'chat_id': 'c', 'workspace_id': 'ws', 'ack_seq': 3, 'high_seq': 9, 'end_seq': 5, 'rows': rows}
    p['page_sha256'] = core.page_digest(account_id='acct', chat_id='c', workspace_id='ws', ack_seq=3, high_seq=9, end_seq=5, rows=rows)
    return {**p, **kw}


def test_selection_identity_binds_page_cursor_policy_and_authority():
    p = page()
    a = delta.selection_sha256(delta.selection_binding(p, policy_fingerprint='pol', authority_fingerprint='auth'))
    assert a == delta.selection_sha256(delta.selection_binding(p, policy_fingerprint='pol', authority_fingerprint='auth'))
    for change in ({'policy_fingerprint': 'pol2'}, {'authority_fingerprint': 'auth2'}):
        assert delta.selection_sha256(delta.selection_binding(p, **{'policy_fingerprint': 'pol', 'authority_fingerprint': 'auth', **change})) != a
    assert delta.selection_sha256(delta.selection_binding(page(ack_seq=2), policy_fingerprint='pol', authority_fingerprint='auth')) != a
    assert delta.idempotency_key(a) == 's08_' + a
    obj = delta.journal_object(p, a)
    assert obj['rows'][0]['source'] == SRC and obj['rows'][1]['source'] is None and 'proof' not in obj['rows'][0]


def test_decide():
    assert delta.decide(page()) == 'stage'
    assert delta.decide(page(rows=[])) == 'empty'
    only_omitted = page()['rows'][1:]
    assert delta.decide(page(rows=only_omitted)) == 'advance'
    gap = [dict(only_omitted[0], **{'class': 'gap', 'reason': 'source_missing'})]
    assert delta.decide(page(rows=gap)) == 'blocked_gap'
    assert delta.decide(page(rows=gap + [dict(gap[0], reason='overlap_pending')])) == 'blocked_overlap'


class FakeProvider:
    """FAKE NATIVE: scripted journal_delta_status answers; records prepare calls."""
    def __init__(self, state):
        self.state, self.prepared = state, []

    async def _host_journal_delta_status(self, request):
        sel = request['selection_sha256']
        p = self.page
        return {'version': 1, 'state': self.state, 'selection_sha256': sel, 'page_sha256': p['page_sha256'], 'prior_cursor': p['ack_seq'],
                'end_seq': p['end_seq'], 'job_id': 's08job_' + 'a' * 64, 'candidate_id': None, 'receipt_ids': ['cs_s08r']}

    async def _host_journal_delta_prepare(self, original, *, before_issue):
        if getattr(self, 'prepare_error', None): raise self.prepare_error
        assert before_issue() is True and original['output_kind'] == 'journal_synthesis' and original['selection']['candidates'] == []
        self.prepared.append(original)
        return {'version': 1, 'job_id': 's08job_' + 'b' * 64}


class FakeArchive:
    def __init__(self, p): self.p, self.acked = p, []
    def read_journal_delta_page(self, **kw): return self.p
    def ack_journal_delta(self, **kw): self.acked.append(kw['coverage']); return {'cursor': self.p['end_seq'], 'replayed': False}


def run(state, prefs=True, resolve_ok=True, gates=None, prepare_error=None):
    p = page()
    provider = FakeProvider(state); provider.page = p; provider.prepare_error = prepare_error
    archive = FakeArchive(p)
    hydrate = lambda ref: {'state': 'available', 'content': BODY}
    lifecycle = SimpleNamespace(binding=SimpleNamespace(memory_owner='owner', fingerprint='target'), source_authority=SimpleNamespace(hydrate=hydrate))
    def resolve(kind, source):
        assert kind == 'synthesis'
        if not resolve_ok: raise ValueError('unanchored assignment unavailable')
        return {'connectionID': 'c', 'modelRouteID': 'r'}
    assignment = SimpleNamespace(resolve=resolve)
    limits = SimpleNamespace(owner_queue=4, global_queue=8, job_output_tokens=512, daily_calls=10, daily_tokens=1000)
    out = asyncio.run(delta.run_pass(archive=archive, provider=provider, lifecycle=lifecycle, assignment=assignment, account_id='acct', chat_id='c',
        workspace_id='ws', guard=object(), authorize=lambda *_: True, load_prefs=lambda: {'memory_policy_consolidation_enabled': True} if prefs else {},
        source_proof=None, dedupe=None, policy_refs=lambda: {'refs': 1}, limits=limits, policy_fingerprint='pol', before_issue=lambda: True, gates=gates))
    return out, provider, archive


def test_default_off_stages_nothing():
    out, provider, archive = run('absent', prefs=False)
    assert out == {'state': 'off'} and provider.prepared == [] and archive.acked == []


def test_absent_stages_once_through_s08_prepare_and_never_acks():
    out, provider, archive = run('absent')
    assert out['state'] == 'staged' and len(provider.prepared) == 1 and archive.acked == []
    original = provider.prepared[0]
    assert original['selection']['idempotency_key'] == 's08_' + original['snapshots_sha256']


def test_recorded_outcome_retries_only_the_ack_and_never_prepares_again():
    for state in ('recorded_proposal', 'recorded_no_finding'):
        out, provider, archive = run(state)
        assert out['state'] == 'acked' and provider.prepared == [] and len(archive.acked) == 1
        assert archive.acked[0][0].outcome == ('proposal' if state == 'recorded_proposal' else 'no_finding')


def test_failure_cancel_unknown_selected_never_ack_or_redispatch():
    for state in ('terminal_failed', 'unknown', 'selected'):
        out, provider, archive = run(state)
        assert provider.prepared == [] and archive.acked == []


def test_unanchored_assignment_defers():
    out, provider, archive = run('absent', resolve_ok=False)
    assert out['state'] == 'deferred' and provider.prepared == [] and archive.acked == []


def test_closed_s05_gates_defer_before_any_prepare():
    async def closed(original): return False
    for state in ('absent', 'cancelled'):
        out, provider, archive = run(state, gates=closed)
        assert out['state'] == 'deferred' and out['reason'] == 'gates_closed' and provider.prepared == [] and archive.acked == []
    seen = []
    async def open_(original): seen.append(original['max_input_tokens']); return True
    out, provider, _ = run('absent', gates=open_)
    assert out['state'] == 'staged' and len(provider.prepared) == 1 and seen == [provider.prepared[0]['max_input_tokens']]


def test_a_cancelled_original_is_reprepared_once_and_a_refusal_is_not_coverage_without_a_loop():
    out, provider, archive = run('cancelled')
    assert out['state'] == 'staged' and len(provider.prepared) == 1 and archive.acked == []
    out, provider, archive = run('cancelled', prepare_error=RuntimeError('terminal'))
    assert out['state'] == 'not_coverage' and out['reason'] == 'reprepare_refused:RuntimeError' and provider.prepared == [] and archive.acked == []
    # a failure to prepare an ABSENT selection is not swallowed
    import pytest
    with pytest.raises(RuntimeError):
        run('absent', prepare_error=RuntimeError('boom'))

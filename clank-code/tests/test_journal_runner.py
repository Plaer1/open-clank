"""BQ-8 H5: the journal variant of the S08 runner, driven by a FAKE NATIVE and a FAKE MODEL (marked: the real binary waits for Native).

The provider's session machinery is shared with the memory consolidation runner; here the journal-specific pieces are exercised:
the selection/original, the hydrate-and-verify model input, proposal validation, and the settle() orchestration (stage -> run -> status
-> proof -> ACK) including the crash between commit and ACK.
"""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from src.openclank import fm_journal_coverage as core
from src.openclank import fm_journal_delta as delta

BODIES = {'m1': 'I moved to Lisbon.', 'm2': 'My cat is called Pixel.'}


def src(mid):
    return {'version': 1, 'kind': 'host_user_body', 'account_id': 'acct', 'chat_id': 'c', 'actor_id': 'main', 'message_id': mid, 'part_id': 'body',
            'revision': 1, 'content_hash': hashlib.sha256(BODIES[mid].encode()).hexdigest(), 'workspace_id': 'ws', 'project_id': None}


def page():
    rows = []
    for seq, mid in ((4, 'm1'), (6, 'm2')):
        s = src(mid)
        rows.append({'seq': seq, 'actor_id': 'main', 'message_id': mid, 'part_id': 'body', 'revision': 1, 'content_hash': s['content_hash'],
                     'workspace_id': 'ws', 'project_id': None, 'role': 'user', 'part_type': 'text', 'class': 'included', 'reason': 'host_user_body',
                     'selector': {}, 'proof': {'source': s, 'authority': {'origin_id': 'o' + mid}, 'size': 20}})
    rows.insert(1, {'seq': 5, 'actor_id': 'main', 'message_id': 'a5', 'part_id': 'body', 'revision': 1, 'content_hash': 'h5', 'workspace_id': 'ws',
                    'project_id': None, 'role': 'assistant', 'part_type': 'text', 'class': 'omitted', 'reason': 'assistant_without_pair',
                    'selector': {'seq': 5}, 'proof': None})
    p = {'account_id': 'acct', 'chat_id': 'c', 'workspace_id': 'ws', 'ack_seq': 3, 'high_seq': 9, 'end_seq': 6, 'rows': rows}
    p['page_sha256'] = core.page_digest(account_id='acct', chat_id='c', workspace_id='ws', ack_seq=3, high_seq=9, end_seq=6, rows=rows)
    return p


def hydrate(ref):
    return {'state': 'available', 'content': BODIES[ref.message_id]}


LIMITS = SimpleNamespace(owner_queue=4, global_queue=8, job_output_tokens=512, daily_calls=10, daily_tokens=1000)


def test_selection_anchor_pairs_and_digest_match_the_pass_original():
    sel = delta.JournalSelection(page())
    assert [i.source.message_id for i, origin in sel.pairs()] == ['m1', 'm2'] and all(o is None for _, o in sel.pairs())
    assert sel.anchor.message_id == 'm2'
    selected, digest = sel.selected(owner='owner', workspace_id='ws', account_id='acct', target_fingerprint='t', policy_fingerprint='p', limits=LIMITS)
    original = delta.prepare_original(page(), owner='owner', workspace_id='ws', account_id='acct', target_fingerprint='t', policy_fingerprint='p',
                                      limits=LIMITS, route={'connectionID': 'c', 'modelRouteID': 'r'}, assignment_sha256='x', policy_refs={})
    assert original['selection'] == selected and original['snapshots_sha256'] == digest and selected['candidates'] == [] == selected['admissions']
    with pytest.raises(delta.JournalDeltaError):
        delta.JournalSelection(dict(page(), rows=[page()['rows'][1]]))       # no included user row: no selection


def test_model_input_hydrates_and_verifies_every_body_immediately():
    sel = delta.JournalSelection(page())
    operation, bound = sel.model_input(hydrate, 512)
    user = operation['messages'][1]['content']
    assert BODIES['m1'] in user and BODIES['m2'] in user and bound >= 1024 and operation['maxOutputTokens'] == 512
    for changed in ({'state': 'missing'}, {'state': 'available', 'content': 'edited'}, {'state': 'available', 'content': 5}):
        with pytest.raises(delta.JournalDeltaError):
            sel.model_input(lambda ref: changed, 512)
    assert sel.source_unchanged(hydrate) is True and sel.source_unchanged(lambda ref: {'state': 'denied'}) is False


def test_model_output_is_one_proposal_or_explicit_no_finding():
    assert delta.validate_model_proposal('NO_FINDING') == {'version': 1, 'kind': 'no_proposal'}
    assert delta.validate_model_proposal(' The user lives in Lisbon. ') == {'version': 1, 'kind': 'journal_proposal', 'content': 'The user lives in Lisbon.'}
    for bad in ('', '   ', 'x' * 4097, None, 5):
        with pytest.raises(ValueError):
            delta.validate_model_proposal(bad)


class Native:
    """FAKE NATIVE: records the committed outcome per selection; status reflects it."""
    def __init__(self):
        self.state, self.ack_fail = 'absent', 0
        self.acks = []


def make(native, model_calls, *, model_reply='NO_FINDING', change_source=False, refuse=False, crash_ack_once=False):
    p = page()
    holder = {}

    async def one_pass(hold):
        sel = delta.JournalSelection(p)
        if native.state == 'absent':
            if hold is not None: hold.append(p)
            return {'state': 'staged'}
        if native.state == 'selected':
            if hold is not None: hold.append(p)
            return {'state': 'selected'}
        if native.state in ('recorded_proposal', 'recorded_no_finding'):
            if crash_ack_once and not holder.get('crashed'):
                holder['crashed'] = True
                raise RuntimeError('crash before the ACK landed')
            native.acks.append(native.state)
            return {'state': 'acked'}
        return {'state': 'not_coverage'}

    async def run_job(journal):
        if refuse:
            return False
        try:
            operation, _ = journal.model_input((lambda r: {'state': 'available', 'content': 'edited'}) if change_source else hydrate, 512)
        except delta.JournalDeltaError:
            return False                                    # aborted: nothing was written
        model_calls.append(operation)
        try:
            outcome = delta.validate_model_proposal(model_reply() if callable(model_reply) else model_reply)
        except Exception:
            return False                                    # model failure: no commit
        native.state = 'recorded_no_finding' if outcome['kind'] == 'no_proposal' else 'recorded_proposal'
        return True
    return one_pass, run_job


def settle(*a, **kw):
    return asyncio.run(delta.settle(*a, **kw))


def test_full_pass_for_a_proposal_and_for_no_finding():
    for reply, state, outcome in (('The user lives in Lisbon.', 'recorded_proposal', 'recorded_proposal'), ('NO_FINDING', 'recorded_no_finding', 'recorded_no_finding')):
        native, calls = Native(), []
        one_pass, run_job = make(native, calls, model_reply=reply)
        assert settle(one_pass, run_job)['state'] == 'acked'
        assert native.state == state and native.acks == [outcome] and len(calls) == 1


def test_model_failure_commits_nothing_and_never_acks():
    for reply in ('', 'x' * 5000):
        native, calls = Native(), []
        one_pass, run_job = make(native, calls, model_reply=reply)
        out = settle(one_pass, run_job)
        assert out['state'] == 'deferred' and native.state == 'absent' and native.acks == []


def test_source_changed_between_selection_and_run_aborts_with_no_write():
    native, calls = Native(), []
    one_pass, run_job = make(native, calls, change_source=True)
    assert settle(one_pass, run_job)['state'] == 'deferred' and native.state == 'absent' and calls == [] and native.acks == []


def test_budget_or_occupancy_refusal_is_deferred_not_failed():
    native, calls = Native(), []
    one_pass, run_job = make(native, calls, refuse=True)
    out = settle(one_pass, run_job)
    assert out['state'] == 'deferred' and out['reason'] == 'run_unavailable' and native.state == 'absent' and calls == []


def test_crash_between_commit_and_ack_retries_only_the_ack():
    native, calls = Native(), []
    one_pass, run_job = make(native, calls, model_reply='The user lives in Lisbon.', crash_ack_once=True)
    with pytest.raises(RuntimeError):
        settle(one_pass, run_job)                           # committed, then the ACK crashed
    assert native.state == 'recorded_proposal' and len(calls) == 1 and native.acks == []
    assert settle(one_pass, run_job)['state'] == 'acked'    # next tick: status says recorded -> ACK only
    assert len(calls) == 1 and native.acks == ['recorded_proposal']   # the model was never called again


def test_nothing_runs_without_a_runner():
    native, calls = Native(), []
    one_pass, _ = make(native, calls)
    assert settle(one_pass, None)['state'] == 'staged' and calls == []


# --- H1: origin authority from the existing S08/S06 origin projection (real archive page, fake provider/source authority) --------------

def _archive_with_ready_rows(tmp_path):
    import sqlite3
    from src.openclank.conversation_archive import ConversationArchive, SourcePart
    a = ConversationArchive(str(tmp_path / 'a.db'), enable_origin_policy_for_disposable_test=True)
    for mid in ('m1', 'm2'):
        content = BODIES[mid]
        a.append_parts([SourcePart(owner='acct', chat_id='c', actor_id='main', message_id=mid, part_id='body', revision=1, event_workspace='ws',
                                   role='user', part_type='text', content=content, content_hash=hashlib.sha256(content.encode()).hexdigest())])
        with sqlite3.connect(a.db_path) as db:
            db.execute("INSERT INTO conversation_archive_outbox(owner,chat_id,actor_id,message_id,part_id,revision,event_kind,payload_json,created_at,updated_at) "
                       "VALUES ('acct','c','main',?, 'body',1,'origin_ready','{}',1,1)", (mid,))
    return a


def test_origin_authority_comes_from_the_existing_projection_or_the_row_is_a_blocking_gap(tmp_path):
    from src.memery_external_sources import SourceRef
    from src.memery_provider import _HostObservationProjection
    a = _archive_with_ready_rows(tmp_path)
    calls = []

    class Provider:
        def _host_source_refs(self, **kw):
            calls.append(kw)
            ref = SourceRef(1, 'host_user_body', 'acct', 'c', 'main', kw['message_id'], 'body', 1, hashlib.sha256(BODIES[kw['message_id']].encode()).hexdigest(), 'ws', None)
            if kw['message_id'] == 'm2':          # no projection for m2 (e.g. capture off / not yet observed)
                return (), None
            return (_HostObservationProjection('o', 'w', 'cs_s06origin_' + 'a' * 64, ref, 'owner', 'b' * 64, 'c' * 64, None, None),), None

    source = SimpleNamespace(hydrate=lambda ref: {'state': 'available', 'content': BODIES[ref.message_id]})
    proof = delta.source_proof_for(source_authority=source, account_id='acct', chat_id='c',
                                   origin_authority=delta.origin_authority_for(provider=Provider(), target_authority='ta', account_id='acct'))
    page_ = a.read_journal_delta_page(owner='acct', chat_id='c', workspace_id='ws', authorize=lambda *_: True, source_proof=proof, dedupe=None)
    rows = {r['message_id']: r for r in page_['rows']}
    assert rows['m1']['class'] == 'included' and set(rows['m1']['proof']['authority']) == {
        'origin_id', 'origin_hash', 'captured_owner', 'current_fingerprint', 'rebind_id', 'rebind_hash'}
    assert rows['m1']['proof']['authority']['origin_id'] == 'cs_s06origin_' + 'a' * 64
    assert (rows['m2']['class'], rows['m2']['reason']) == ('gap', 'proof_unavailable')
    assert core.blocking_gaps(page_['rows']) and all(c['after_job_id'] == 'o:' and c['target_authority'] == 'ta' for c in calls)
    # no authority reader at all (and not a read-only view) fails closed
    closed = delta.source_proof_for(source_authority=source, account_id='acct', chat_id='c')
    assert a.read_journal_delta_page(owner='acct', chat_id='c', workspace_id='ws', authorize=lambda *_: True, source_proof=closed, dedupe=None)['rows'][0]['reason'] == 'proof_unavailable'


# --- H7: the S05 gates are read before prepare (real sqlite tables, fake profile) --------------------------------------------------

def _gate_db(tmp_path, *, quiet_tick=0, foreground=False, reservations=()):
    import sqlite3
    path = tmp_path / 'gates.db'
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE fm_v2_s05_schema(version INTEGER); INSERT INTO fm_v2_s05_schema VALUES (4);
      CREATE TABLE fm_v2_s05_occupancy(singleton INTEGER, verified INTEGER, coverage_generation TEXT, clock_generation TEXT, last_quiet_tick INTEGER, foreground_epoch INTEGER);
      CREATE TABLE fm_v2_s05_foreground(state TEXT);
      CREATE TABLE fm_v2_s05_reservations(owner_id TEXT, state TEXT, charged_calls INTEGER, charged_seconds INTEGER, charged_input_tokens INTEGER,
                                          charged_output_tokens INTEGER, settled_at_ms INTEGER, reserved_at_ms INTEGER);""")
    db.execute("INSERT INTO fm_v2_s05_occupancy VALUES (1,1,'cg','ck',?,0)", (quiet_tick,))
    if foreground: db.execute("INSERT INTO fm_v2_s05_foreground VALUES ('active')")
    for row in reservations: db.execute("INSERT INTO fm_v2_s05_reservations VALUES ('owner',?,?,?,?,?,NULL,?)", row)
    db.commit(); db.close()
    return SimpleNamespace(path=str(path), coverage_generation='cg', clock_generation='ck', assert_current=lambda: None, monotonic_tick=lambda: 100_000)


GATE_LIMITS = SimpleNamespace(quiet_seconds=10, job_seconds=60, daily_seconds=600, job_output_tokens=100, daily_calls=2, daily_tokens=10_000)


def test_s05_gates_open_only_when_idle_quiet_and_within_budget(tmp_path):
    import time
    now = time.time_ns() // 1_000_000
    n = iter(range(100))
    def gate(tokens=500, **kw):
        sub = tmp_path / str(next(n)); sub.mkdir()
        return delta.s05_gates_open(_gate_db(sub, **kw), owner='owner', limits=GATE_LIMITS, input_bound=tokens)
    assert gate() is True
    assert gate(quiet_tick=95_000) is False                                                    # quiet window not elapsed
    assert gate(foreground=True) is False                                                      # foreground active
    assert gate(reservations=[('reserved', 1, 1, 1, 1, now)]) is False                         # live reservation
    assert gate(reservations=[('settled', 1, 1, 1, 1, now), ('settled', 1, 1, 1, 1, now)]) is False   # daily calls
    assert gate(tokens=20_000) is False                                                        # daily tokens
    assert delta.s05_gates_open(SimpleNamespace(path=str(tmp_path / 'none.db'), assert_current=lambda: None), owner='o', limits=GATE_LIMITS, input_bound=1) is False


def test_the_runner_prepare_uses_the_shared_original_builder_and_pass_original_equals_it():
    """No existing test builds an S08 session (searched tests/*.py for _S08PolicySession/_host_s08_/_s08_rules_/s08_model_prepare: no hit),
    so the request builder is shared code: provider and pass both call journal_original / journal_assignment_sha256."""
    import inspect
    from src.memery_provider import MemeryProvider
    source = inspect.getsource(MemeryProvider._host_s08_prepare_policy)
    assert 'journal_original(selected=selected' in source and 'journal_assignment_sha256(journal,route)' in source
    route = {'connectionID': 'c', 'modelRouteID': 'r'}
    sel = delta.JournalSelection(page())
    selected, digest = sel.selected(owner='owner', workspace_id='ws', account_id='acct', target_fingerprint='t', policy_fingerprint='p', limits=LIMITS)
    operation, bound = sel.model_input(hydrate, LIMITS.job_output_tokens)
    sha = delta.journal_assignment_sha256(sel, route)
    built = delta.journal_original(selected=selected, digest=digest, route=route, assignment_sha256=sha, input_bound=bound, limits=LIMITS, policy_refs={'refs': 1})
    p = page(); p['input_bound'] = bound
    assert delta.prepare_original(p, owner='owner', workspace_id='ws', account_id='acct', target_fingerprint='t', policy_fingerprint='p',
                                  limits=LIMITS, route=route, assignment_sha256=sha, policy_refs={'refs': 1}) == built
    assert built['mode'] == 'model' and built['output_kind'] == 'journal_synthesis' and built['snapshots_sha256'] == digest == selected['journal']['selection_sha256']


# --- barrier: the journal run's admission (the existing consolidation callback's `barrier.acquire`), real AccountRequestBarrier -------

def test_the_run_admission_does_not_deadlock_under_a_held_admission_and_respects_a_fence():
    from src.openclank.account_request_barrier import AccountRequestBarrier, AccountRequestFenced

    async def scenario():
        fenced = set()
        barrier = AccountRequestBarrier(lambda owner: owner in fenced)
        async with barrier.admitted('Alice'):
            nested = await asyncio.wait_for(barrier.acquire('alice'), 1)       # a second admission registers; it does not wait on the first
            await barrier.release(nested)
        held = await barrier.acquire('alice')                                   # a running journal job's admission
        fenced.add('alice')
        with pytest.raises(AccountRequestFenced):
            await barrier.acquire('alice')                                      # reset/erase fence: no new run is admitted
        drain = asyncio.create_task(barrier.drain('alice'))
        await asyncio.sleep(0.05)
        assert not drain.done()                                                 # the drain waits for the running job ...
        await barrier.release(held)
        assert (await asyncio.wait_for(drain, 1))['drained'] == 1               # ... and finishes when it releases
    asyncio.run(scenario())


# --- H4: whole-chat erasure fences exactly the journal selection's sources (fixtures of tests/test_memery_external_sources.py) ----

def test_whole_chat_erasure_fences_every_source_of_a_journal_selection(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    from src.memery_external_sources import SourceRef
    from src.memery_provider import _HostObservationProjection
    from src.openclank import acp_supervisor_adapter as supervisor_module
    from src.openclank import chat_source_bridge as bridge
    from src.openclank import memory_job_admission as admission
    from src.openclank import session_map as map_module
    guard, binding = object(), object()
    projections = tuple(sorted((_HostObservationProjection('o', 'w', 'cs_s06origin_' + ('a' if mid == 'm1' else 'b') * 64,
                                SourceRef(**src(mid)), 'owner', 'h' * 64, 'c' * 64, None, None) for mid in ('m1', 'm2')), key=lambda r: r.origin_id))
    fenced = []

    class Provider:
        def _host_chat_source_refs(self, **kw):          # the same inventory the S08/H1 origin reader walks
            return projections, None

        async def _host_source_fence(self, ref):         # Native observation_fence for one source
            fenced.append(ref.source.message_id)
            return {'visibility': 'closed', 'state': 'fenced'}
    provider = Provider()

    class Pool:
        def _runtime_home(self, _owner): return tmp_path
    class Supervisor:
        def owner_lifecycle_pool(self): return Pool()
    class Authority:
        def __init__(self, **_kwargs): pass
        def resolve(self, _account): return binding
    class Mapping:
        def __init__(self, _path, _owner): pass
        @asynccontextmanager
        async def async_chat_admission_lock(self, chat):
            yield guard
        def validate_chat_guard(self, _chat, value): assert value is guard
    state = SimpleNamespace(mimo_supervisor=Supervisor(), auth_manager=SimpleNamespace(account_id=lambda _: 'acct', username_for_account_id=lambda _: 'alice'))
    monkeypatch.setattr(bridge, 'canonical_source_provider', lambda _: provider)
    monkeypatch.setattr(bridge, '_source_fence_covered', lambda receipt: True)
    monkeypatch.setattr(supervisor_module, 'AcpSupervisorAdapter', Supervisor)
    monkeypatch.setattr(admission, 'TargetAuthority', Authority)
    monkeypatch.setattr(map_module, 'OwnerSessionMap', Mapping)

    async def run():
        async with bridge.guarded_source_mutation(state=state, owner='alice', chat_id='c', whole_chat=True):
            pass
    asyncio.run(run())
    selected = {item.source.message_id for item, _ in delta.JournalSelection(page()).pairs()}
    assert selected == {'m1', 'm2'} and sorted(fenced) == sorted(selected)

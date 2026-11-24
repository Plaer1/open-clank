"""BQ-8 contract H, the CONNECTED case on the REAL binary: Python provider -> real memery-worker -> a disposable guarded-v18 store with the
S06/S02/S08/S08-model profiles (`tests/_s08_journal_fixture.py`), a real `ConversationArchive`, and a stub model reply (the model is the
only fake; no network, throwaway data only).

Chain per scenario: archive parts -> journal page -> Native `s08_model_prepare` (kind journal_synthesis) -> status `selected` -> reserve ->
dispatch -> stub model reply -> `s08_policy_commit` (`journal_proposal` | `no_proposal`) or `s08_policy_terminal` / `s08_policy_prepared_cancel`
-> `journal_delta_status` -> outcome proof -> `ack_journal_delta` (the cursor moves ONLY from a recorded outcome) -> Host review lane
(`_host_s08_review_kind`, manifest, apply, original) for the staged proposal.

The S08 daily scope limit allows one journal job per workspace and day, so every scenario builds its own store.
"""
import asyncio
import hashlib
import sqlite3
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from src.memery_external_sources import SourceRef
from src.openclank import fm_journal_coverage as core
from src.openclank import fm_journal_delta as delta
from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner, _PUBLICATION_CAPABILITY_SEAL
from src.openclank.conversation_archive import ConversationArchive, SourcePart
from tests import _s08_journal_fixture as S

OWNER, ACCOUNT, WORKSPACE, CHAT, BODIES = S.OWNER, S.ACCOUNT, S.WORKSPACE, S.CHAT, S.BODIES
LIMITS = SimpleNamespace(owner_queue=4, global_queue=8, job_output_tokens=512, daily_calls=10, daily_tokens=100000)
ROUTE = {'connectionID': 'conn-1', 'modelRouteID': 'route-1'}


@pytest.fixture
def fx():
    fixture = S.build_s08_fixture()
    yield fixture
    fixture.close()


def authority(message_id):
    origin_id, raw = S.origin_authority(message_id)
    return {'origin_id': origin_id, 'origin_hash': hashlib.sha256(raw.encode()).hexdigest(), 'captured_owner': OWNER,
            'current_fingerprint': 'c' * 64, 'rebind_id': None, 'rebind_hash': None}


def make_archive(tmp_path):
    a = ConversationArchive(str(tmp_path / 'a.db'), enable_origin_policy_for_disposable_test=True)
    for mid, text in BODIES.items():
        a.append_parts([SourcePart(owner=ACCOUNT, chat_id=CHAT, actor_id='main', message_id=mid, part_id='body', revision=1, event_workspace=WORKSPACE,
                                   role='user', part_type='text', content=text, content_hash=hashlib.sha256(text.encode()).hexdigest())])
        with sqlite3.connect(a.db_path) as db:      # the origin-ready fact the archive's own producer writes
            db.execute("INSERT INTO conversation_archive_outbox(owner,chat_id,actor_id,message_id,part_id,revision,event_kind,payload_json,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?, 'origin_ready','{}',1,1)", (ACCOUNT, CHAT, 'main', mid, 'body', 1))
    return a


def source_proof(row):
    ref = SourceRef(1, 'host_user_body', ACCOUNT, CHAT, row['actor_id'], row['message_id'], row['part_id'], row['revision'], row['content_hash'], WORKSPACE, None)
    return {'state': 'available', 'source': asdict(ref), 'authority': authority(row['message_id']), 'size': len(BODIES[row['message_id']])}


def read_page(archive):
    return archive.read_journal_delta_page(owner=ACCOUNT, chat_id=CHAT, workspace_id=WORKSPACE, authorize=lambda *_: True, source_proof=source_proof, dedupe=None)


def cursor(archive):
    with sqlite3.connect(archive.db_path) as db:
        row = db.execute("SELECT last_outbox_seq FROM conversation_archive_cursor WHERE owner=? AND chat_id=? AND consumer='journal_delta.v1'", (ACCOUNT, CHAT)).fetchone()
    return row[0] if row else 0


def ack(archive, page, proofs):
    lifecycle = CompletedTurnLifecycleOwner(mapping=SimpleNamespace(validate_chat_guard=lambda chat, guard: None), source_authority=None, owner=OWNER,
        account_id=ACCOUNT, provider=None, profile=None, target_authority=None, account_lifecycle=None, binding=None, _seal=_PUBLICATION_CAPABILITY_SEAL)
    return archive.ack_journal_delta(owner=ACCOUNT, chat_id=CHAT, workspace_id=WORKSPACE, authorize=lambda *_: True, lifecycle_owner=lifecycle, guard=object(),
        expected_cursor=page['ack_seq'], page_digest=page['page_sha256'], end_seq=page['end_seq'], coverage=proofs, source_proof=source_proof, dedupe=None)


class Flow:
    """One selection driven through the real Native phases."""

    def __init__(self, fx, tmp_path):
        self.fx, self.archive = fx, make_archive(tmp_path)
        self.provider, self.coordinator = fx.providers[OWNER], fx.coordinator
        self.refs = None

    async def start(self):
        provider = self.provider
        await provider.initialize()
        assert await provider._host_negotiate_s08_policy(owner=OWNER, workspace_id=WORKSPACE) is True
        assert provider._host_s08_policy_ready('journal_synthesis') is True
        self.refs = self.coordinator.checked_refs(OWNER, ACCOUNT, WORKSPACE)
        self.before_issue = lambda: self.coordinator.checked_refs(OWNER, ACCOUNT, WORKSPACE) == self.refs
        self.page = read_page(self.archive)
        assert delta.decide(self.page) == 'stage' and cursor(self.archive) == 0
        journal = delta.JournalSelection(self.page)
        _, self.page['input_bound'] = journal.model_input(lambda ref: {'state': 'available', 'content': BODIES[ref.message_id]}, 512)
        self.original = delta.prepare_original(self.page, owner=OWNER, workspace_id=WORKSPACE, account_id=ACCOUNT, target_fingerprint='a' * 64,
            policy_fingerprint='b' * 64, limits=LIMITS, route=ROUTE, assignment_sha256=delta.journal_assignment_sha256(journal, ROUTE), policy_refs=self.refs)
        self.digest = self.original['snapshots_sha256']
        self.status_request = {'version': 1, 'owner': OWNER, 'workspace_id': WORKSPACE, 'account_id': ACCOUNT, 'policy_projection': self.refs,
                               'selection_sha256': self.digest, 'prepare': self.original}

    async def prepare(self):
        self.job = (await self.provider._host_journal_delta_prepare(self.original, before_issue=self.before_issue))['job_id']
        return self.job

    async def status(self):
        return await self.provider._host_journal_delta_status(self.status_request)

    async def native(self, phase, request):
        return await self.provider._host_candidate_request(OWNER, WORKSPACE, {'phase': phase, 'request': request}, require_live=True,
            write_deadline=float(asyncio.get_running_loop().time() + 30.0), before_issue=self.before_issue, write_status={})

    async def reserve_dispatch(self):
        selection = self.original['selection']
        self.reserve = {'version': 1, 'original': self.original, 'reservation': {'version': 1, 'owner': OWNER, 'job_id': self.job, 'worker_id': 'w1',
            'coverage_generation': 'cg', 'clock_generation': 'ck', 'now_tick': 100000, 'quiet_seconds': 10, 'job_seconds': 60, 'daily_seconds': 600,
            'target_fingerprint': selection['target_fingerprint'], 'policy_fingerprint': selection['policy_fingerprint']}}
        reserved = await self.native('s08_model_reserve', self.reserve)
        self.attempt = {'version': 1, 'original': self.original, 'reservation_original': self.reserve, 'job_id': self.job,
                        'attempt_id': reserved['attempt_id'], 'lease_epoch': reserved['lease_epoch']}
        assert (await self.native('s08_model_dispatch', self.attempt))['state'] == 'dispatched'

    async def commit(self, model_reply):
        proposal = delta.validate_model_proposal(model_reply)         # the stub model's reply, validated exactly as the Host runner does
        return await self.native('s08_policy_commit', {'version': 1, 'attempt': self.attempt, 'proposal': proposal,
                                                       'usage': {'elapsed_seconds': 1, 'input_tokens': 10, 'output_tokens': 10}})

    async def terminal(self, disposition, reason=None):
        return await self.native('s08_policy_terminal', {'version': 1, 'attempt': self.attempt, 'disposition': disposition, 'reason': reason, 'usage': None})

    def proof(self, status):
        return delta.mint_outcome_proof(status=status, page=self.page, selection_digest=self.digest)

    def review_kwargs(self, candidate_id):
        return dict(owner=OWNER, account_id=ACCOUNT, workspace_id=WORKSPACE, candidate_id=candidate_id, policy_user=OWNER,
                    policy_projection=self.coordinator.projection, current=lambda: True)

    def review_request(self, candidate_id, sha, action, key):
        return {'version': 1, 'owner': OWNER, 'account_id': ACCOUNT, 'workspace_id': WORKSPACE, 'candidate_id': candidate_id, 'candidate_revision': 3,
                'candidate_sha256': sha, 'request_key': key, 'reason': 'reviewed', 'action': action, 'policy_projection': self.refs}


def run(coro):
    asyncio.run(coro)


# The store is prepared with the S08 model v2 profile (`schema-v17-s08-model-v2.sql`): its triggers admit the journal_synthesis tool hash, so a
# journal job can be reserved. On a v1 store journal is unavailable (see tests/test_journal_s08_fixture_smoke.py).


def test_proposal_is_recorded_acked_reviewed_and_published(fx, tmp_path):
    async def scenario():
        f = Flow(fx, tmp_path)
        await f.start()
        job = await f.prepare()
        status = await f.status()
        assert status['state'] == 'selected' and status['job_id'] == job and cursor(f.archive) == 0
        await f.reserve_dispatch()
        assert (await f.status())['state'] == 'selected', 'an issued attempt without a recorded outcome is never a recorded outcome'
        result = await f.commit('The user lives in Lisbon and has a cat called Pixel.')
        assert result['state'] == 'staged' and result['candidate_id'].startswith('s08candidate_')
        status = await f.status()
        assert status['state'] == 'recorded_proposal' and status['candidate_id'] == result['candidate_id']
        assert cursor(f.archive) == 0, 'a status read never moves coverage'
        proof = f.proof(status)
        assert proof.outcome == 'proposal'
        assert ack(f.archive, f.page, [proof]) == {'cursor': f.page['end_seq'], 'replayed': False}
        assert cursor(f.archive) == f.page['end_seq']
        assert ack(f.archive, f.page, [proof])['replayed'] is True
        # ---- the review lane, the SAME methods the summary proposals use
        candidate = result['candidate_id']
        assert (await f.provider._host_s08_review_kind(**f.review_kwargs(candidate), ))['kind'] == 'journal_synthesis'
        manifest = await f.provider._host_s08_policy_review_manifest(**f.review_kwargs(candidate), output_kind='journal_synthesis')
        assert manifest['content'] == 'The user lives in Lisbon and has a cat called Pixel.' and manifest['output_kind'] == 'journal_synthesis'
        assert [c['message_id'] for c in manifest['journal']['cited']] == ['m1', 'm2'] and manifest['producer']['model_route_id'] == 'route-1'
        assert manifest['producer']['actor'] == 's08_journal' and manifest['journal']['selection_sha256'] == f.digest
        request = f.review_request(candidate, manifest['candidate_sha256'], 'accept', 'key-accept-1')
        applied = await f.provider._host_s08_policy_review_apply(request, output_kind='journal_synthesis', before_issue=lambda: True)
        assert applied['state'] == 'applied' and applied['revision'] == 1 and applied['block_id'].startswith('s08policyblock_')
        original = await f.provider._host_s08_policy_review_original(**f.review_kwargs(candidate), output_kind='journal_synthesis')
        assert original['state'] == 'found' and original['original']['action'] == 'accept' and original['receipt']['block_id'] == applied['block_id']
        assert cursor(f.archive) == f.page['end_seq'], 'the decision does not move coverage'
        # ---- Packet R: the accepted block is returned by every normal reader, with lineage; the same shared path serves accepted summaries
        from tests._native_ops_fixture import rows
        from src import memory_versioned
        text = 'The user lives in Lisbon and has a cat called Pixel.'
        block = applied['block_id']
        curated = rows(fx.db, "SELECT accepted_curated_id FROM candidates WHERE id=? AND status='accepted'", (candidate,))[0][0]
        assert curated.startswith('s08policycurated_')
        provider = f.provider

        async def seen(owner):
            out = {'memories': [r.id for r in await provider.list_memories(owner=owner) if r.text == text],
                   'v2': [r.id for r in await provider.list_v2_records(owner=owner, require_legacy_presence=True) if r.text == text],
                   'v2_raw': [r.id for r in await provider.list_v2_records(owner=owner) if r.text == text],
                   'search': [r['id'] for r in ((await provider.search_v2_with_receipt('Pixel', owner=owner)) or ([],))[0] if r.get('text') == text]}
            out['graph'] = bool(memory_versioned.canonical_graph('fetch', owner=owner, workspace_id=WORKSPACE, node_id=block, db_path=str(fx.db))['node'])
            return out
        assert await seen(OWNER) == {'memories': [curated], 'v2': [curated], 'v2_raw': [curated], 'search': [curated], 'graph': True}
        explained = await provider.explain(curated, owner=OWNER)
        lineage = explained['lineage']
        assert lineage['producer']['actor'] == 's08_journal' and lineage['reviewer'] == OWNER
        assert [r['message_id'] for r in lineage['journal']['rows']] == ['m1', 'm2'] and lineage['journal']['selection_sha256'] == f.digest
        assert await seen('bob') == {'memories': [], 'v2': [], 'v2_raw': [], 'search': [], 'graph': False}
        assert await provider.explain(curated, owner='bob') is None
        # ---- the forget direction (BQ-9 rule): the generic (non-batch) forget removes the curated record, which hides the block from every reader;
        # the canonical rows stay by design (restorable), so only the raw canonical reader still counts it
        preview = await provider.forget('preview', owner=OWNER, selector_kind='record_id', selector=curated)
        await provider.forget('commit', owner=OWNER, selector_kind='record_id', selector=curated, preview_token=preview['token'], operation_id='7' * 32)
        assert rows(fx.db, "SELECT count(*) FROM curated WHERE id=?", (curated,)) == [(0,)]
        gone = await seen(OWNER)
        assert gone['memories'] == gone['v2'] == gone['search'] == [] and gone['graph'] is False and gone['v2_raw'] == [curated]
        assert await provider.explain(curated, owner=OWNER) is None
    run(scenario())


def test_explicit_no_finding_is_recorded_and_acknowledged(fx, tmp_path):
    async def scenario():
        f = Flow(fx, tmp_path)
        await f.start(); await f.prepare(); await f.reserve_dispatch()
        result = await f.commit(delta.NO_FINDING)
        assert result['state'] == 'no_proposal' and result['candidate_id'] is None
        status = await f.status()
        assert status['state'] == 'recorded_no_finding' and status['candidate_id'] is None
        proof = f.proof(status)
        assert proof.outcome == 'no_finding' and ack(f.archive, f.page, [proof])['cursor'] == f.page['end_seq']
    run(scenario())


def test_model_failure_never_acknowledges_and_the_selection_is_offered_again(fx, tmp_path):
    async def scenario():
        f = Flow(fx, tmp_path)
        await f.start(); await f.prepare(); await f.reserve_dispatch()
        assert (await f.terminal('failed'))['state'] == 'settled'
        status = await f.status()
        assert status['state'] == 'terminal_failed'
        with pytest.raises(delta.JournalDeltaError):
            f.proof(status)
        assert cursor(f.archive) == 0
        again = read_page(f.archive)
        assert delta.decide(again) == 'stage' and again['page_sha256'] == f.page['page_sha256'], 'the same page is offered again'
        # the same key is never prepared again after dispatch
        with pytest.raises(Exception):
            await f.prepare()
        assert (await f.status())['state'] == 'terminal_failed'
    run(scenario())


def test_cancel_before_dispatch_does_not_acknowledge_and_the_same_key_can_be_prepared_again(fx, tmp_path):
    async def scenario():
        f = Flow(fx, tmp_path)
        await f.start(); job = await f.prepare()
        cancelled = await f.native('s08_policy_prepared_cancel', f.original)
        assert cancelled == {'version': 1, 'state': 'cancelled', 'job_id': job}
        status = await f.status()
        assert status['state'] == 'cancelled' and cursor(f.archive) == 0
        with pytest.raises(delta.JournalDeltaError):
            f.proof(status)
        assert await f.prepare() == job, 'H7: cancelled before dispatch, the same idempotency key is prepared again'
        assert (await f.status())['state'] == 'selected'
    run(scenario())


def test_reject_leaves_no_block_and_a_replay_returns_the_original(fx, tmp_path):
    async def scenario():
        f = Flow(fx, tmp_path)
        await f.start(); await f.prepare(); await f.reserve_dispatch()
        candidate = (await f.commit('The user lives in Lisbon.'))['candidate_id']
        proof = f.proof(await f.status())
        ack(f.archive, f.page, [proof])
        manifest = await f.provider._host_s08_policy_review_manifest(**f.review_kwargs(candidate), output_kind='journal_synthesis')
        preview = await f.provider._host_s08_policy_review_reject_preview(**f.review_kwargs(candidate), output_kind='journal_synthesis')
        assert preview['candidate_sha256'] == manifest['candidate_sha256']
        request = f.review_request(candidate, preview['candidate_sha256'], 'reject', 'key-reject-1')
        rejected = await f.provider._host_s08_policy_review_apply(request, output_kind='journal_synthesis', before_issue=lambda: True)
        assert rejected['state'] == 'rejected'
        from tests._native_ops_fixture import rows
        assert rows(fx.db, "SELECT count(*) FROM fm_v2_knowledge_blocks WHERE predicate='s08.composite'") == [(0,)]
        assert rows(fx.db, "SELECT status FROM candidates WHERE id=?", (candidate,)) == [('rejected',)]
        original = await f.provider._host_s08_policy_review_original(**f.review_kwargs(candidate), output_kind='journal_synthesis')
        assert original['state'] == 'found' and original['original']['action'] == 'reject'
        assert cursor(f.archive) == f.page['end_seq'], 'the coverage decision is independent of the review decision'
    run(scenario())


def test_prepare_is_selected_on_the_real_store_and_the_page_is_not_acknowledged(fx, tmp_path):
    async def scenario():
        f = Flow(fx, tmp_path)
        await f.start()
        assert (await f.status())['state'] == 'absent'
        job = await f.prepare()
        status = await f.status()
        assert status['state'] == 'selected' and status['job_id'] == job and status['page_sha256'] == f.page['page_sha256']
        assert cursor(f.archive) == 0 and delta.decide(read_page(f.archive)) == 'stage'
        assert await f.prepare() == job, 'a live original replays idempotently'
        # a foreign selection digest answers as absent
        assert (await f.provider._host_journal_delta_status({**f.status_request, 'selection_sha256': '7' * 64}))['state'] == 'absent'
        # the review kind of a candidate that does not exist is refused like any unknown candidate
        with pytest.raises(Exception):
            await f.provider._host_s08_review_kind(**f.review_kwargs('s08candidate_' + '1' * 64))
    run(scenario())


def test_the_v2_store_schema_admits_the_journal_tool_hash(fx):
    """The seed store carries the v2 trigger lists (read only)."""
    import re
    from tests._native_ops_fixture import rows
    sql = ' '.join(r[0] for r in rows(fx.db, "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name IN ('s08_reservation_shape','s08_reservation_shape_update','s08_job_result_guard')"))
    listed = set(re.findall(r'[0-9a-f]{64}', sql))
    for mode in ('model', 'hybrid'):
        assert hashlib.sha256(f's08-policy-{mode}-summary-v1'.encode()).hexdigest() in listed
        assert hashlib.sha256(f's08-policy-{mode}-journal_synthesis-v1'.encode()).hexdigest() in listed
    assert rows(fx.db, "SELECT value FROM fm_meta WHERE key='openclank.s08.model.v2'") == [('1',)]


def test_optional_cadence_runs_through_real_journal_prepare_and_reservation(fx, tmp_path):
    """The Host-built policy preimage survives the real Native prepare/reserve path."""
    from src.openclank.memory_policy_projection import exact_json, preference_inputs
    async def case():
        prefs={'memory_policy_consolidation_enabled':True,'memory_policy_consolidation_interval_days':1}
        raw=exact_json({'preferences':preference_inputs(prefs),'can_manage_memory':True})
        fingerprint=hashlib.sha256(raw.encode()).hexdigest()
        projection=fx.coordinator.projection
        current=projection.read_scope(ACCOUNT, WORKSPACE)
        fenced=projection.fence(ACCOUNT,WORKSPACE,current)
        projection.ready(ACCOUNT,WORKSPACE,fenced,fingerprint)
        fx.coordinator.fingerprint=lambda user, current_prefs: fingerprint
        flow=Flow(fx,tmp_path)
        await flow.start()
        old=flow.original
        flow.original=delta.journal_original(selected=old['selection'],digest=old['snapshots_sha256'],route=ROUTE,
            assignment_sha256=old['assignment_sha256'],input_bound=old['max_input_tokens'],limits=LIMITS,
            policy_refs=flow.refs,cadence_policy={'version':1,'accepted_policy_input_json':raw})
        flow.status_request['prepare']=flow.original
        await flow.prepare()
        await flow.reserve_dispatch()
        result=await flow.terminal('failed')
        assert result
    run(case())

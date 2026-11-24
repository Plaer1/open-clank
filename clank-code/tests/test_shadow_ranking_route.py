"""Stubbed route coverage for POST /shadow-ranking/report (no app, provider, store, preference file or model)."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from routes.memory.shadow_ranking import register_shadow_ranking

HEX = '0123456789abcdef' * 4


class Proj:
    def __init__(self, n, account='acct-1'):
        self.source = SimpleNamespace(account_id=account, chat_id='chat-%d' % n)


def item(n):
    return {'admitted_event_id': 'cs_s06admitted_' + HEX, 'found_event_id': 'FOUND-%d' % n, 'recall_attempt_id': 'ATT-%d' % n,
            'features': {'v': 1}, 'recorded': {'positive': False, 'negative': False}}


REPORT = {'v': 1, 'mode': 'shadow', 'state': 'ok', 'receipts': 4, 'attempts': 2, 'labelled': 3, 'positive': 2, 'negative': 1,
          'conflicting_labels': 0, 'labels_without_receipt': 1, 'distinct_feature_vectors': 3,
          'uninformative_features': {'freshness': 'constant'}, 'min_labelled': 20, 'route_kinds': ['graph', 'fts'],
          'agreement': None, 'agreement_withheld': 'below_min_labelled',
          'shadow_order': [{'recall_attempt_id': 'ATT-1', 'found_event_id': ['FOUND-1'], 'scores': [0.1]}]}
KEYS = {'state', 'receipts', 'attempts', 'labelled', 'positive', 'negative', 'conflicting_labels', 'distinct_feature_vectors',
        'uninformative_features', 'min_labelled', 'agreement', 'agreement_withheld', 'truncated', 'sources_seen', 'sources_skipped', 'state_keys'}
STATE_KEYS = {'policy_revision', 'policy_revision_state', 'feature_version', 'label_version', 'route_kinds', 'training_cutoff'}


def make(pref=True, capability=True, sources=1, pages_per_source=1, fail=(), authed=True, live=True, profile=True, report=None, revision=3):
    log = {'prefs': 0, 'negotiate': [], 'refs': 0, 'pages': 0, 'page_args': [], 'report': [], 'lives': 0}
    def refs(account_id, target_authority, after_job_id, limit):
        log['refs'] += 1
        n = 0 if after_job_id == 'o:' else int(after_job_id)
        if n >= sources: return [], None
        return [Proj(n)], (str(n + 1) if n + 1 < sources else None)
    async def negotiate(owner, workspace_id):
        log['negotiate'].append((owner, workspace_id)); return capability
    async def page(origin, after=None, limit=64):
        log['pages'] += 1; log['page_args'].append((origin.source.chat_id, after, limit))
        n = int(origin.source.chat_id.split('-')[1])
        if n in fail: raise RuntimeError('secret-source-id')
        k = 0 if after is None else int(after)
        return {'version': 1, 'state': 'partial', 'items': [item(n * 100 + k)],
                'next_after': str(k + 1) if k + 1 < pages_per_source else None}
    provider = SimpleNamespace(_host_account_source_refs=refs, _host_negotiate_found_features=negotiate, _host_found_features_page=page)
    @asynccontextmanager
    async def lock(chat):
        yield 'guard'
    lifecycle = SimpleNamespace(provider=provider, binding='b', owner='owner-1',
        target_authority=SimpleNamespace(resolve=lambda account: 'b'),
        account_lifecycle=SimpleNamespace(owner_has_active_operation=lambda owner: False),
        mapping=SimpleNamespace(async_chat_admission_lock=lock, validate_chat_guard=lambda chat, guard: None),
        for_source=lambda source, guard: None)
    @asynccontextmanager
    async def admitted(user):
        yield
    def current_live():
        log['lives'] += 1
        return live(log['lives']) if callable(live) else live
    ctx = {'account': 'acct-1', 'owner': 'owner-1', 'workspace': 'ws-1', 'user': 'alice',
           'barrier': SimpleNamespace(admitted=admitted), 'current_live': current_live}
    def require_human(request):
        if not authed: raise HTTPException(401, 'no')
        return 'alice'
    async def owned(fn, *a, **k): return fn(*a, **k)
    def load_prefs(user):
        log['prefs'] += 1
        return {'memory_shadow_ranking_enabled': pref} if pref is not None else {}
    def report_fn(scope, receipts, labels):
        log['report'].append((scope, receipts, labels)); return report or REPORT
    def policy_revision(user):
        log['rev_user'] = user
        if isinstance(revision, Exception): raise revision
        return revision
    router = APIRouter()
    register_shadow_ranking(router, require_human=require_human, capture_context=lambda request, user: ctx, owned=owned,
                            load_prefs=load_prefs, lifecycle_for=lambda request, account: lifecycle, report_fn=report_fn,
                            projection_type=Proj, policy_revision_fn=policy_revision)
    app = FastAPI(); app.include_router(router)
    app.state.s05_profile = SimpleNamespace(assert_current=lambda: None) if profile else None
    return TestClient(app), log


def post(client, raw='{}'):
    return client.post('/shadow-ranking/report', content=raw, headers={'Content-Type': 'application/json'})


def untouched(log):
    return log['negotiate'] == [] and log['refs'] == 0 and log['pages'] == 0 and log['report'] == []


def test_pref_off_reads_nothing():
    for pref in (False, None, 'yes', 1):
        client, log = make(pref=pref)
        r = post(client)
        assert r.status_code == 200 and r.json() == {'state': 'off'} and untouched(log)
        assert r.headers['cache-control'] == 'private, no-store'


def test_capability_false_is_unavailable():
    client, log = make(capability=False)
    r = post(client)
    assert r.json() == {'state': 'unavailable'} and log['refs'] == 0 and log['pages'] == 0 and log['report'] == []


def test_no_guarded_profile_is_unavailable_and_reads_nothing():
    client, log = make(profile=False)
    r = post(client)
    assert r.status_code == 200 and r.json() == {'state': 'unavailable'} and untouched(log) and log['prefs'] == 0 and log['lives'] == 0
    assert r.headers['cache-control'] == 'private, no-store'


def test_happy_path_closed_projection():
    client, log = make()
    r = post(client)
    body = r.json()
    assert r.status_code == 200 and set(body) == KEYS and body['state'] == 'ok'
    assert body['truncated'] is False and body['sources_seen'] == 1 and body['sources_skipped'] == 0
    assert body['uninformative_features'] == {'freshness': 'constant'} and body['agreement'] is None
    assert body['agreement_withheld'] == 'below_min_labelled' and body['min_labelled'] == 20
    for bad in ('shadow_order', 'FOUND', 'ATT-', 'scores', 'labels_without_receipt', 'chat-'):
        assert bad not in r.text
    # the one id that leaves is the state key `training_cutoff` (ranking contract v1); no other field carries an event id
    assert r.text.count('cs_s06') == 1 and 'cs_s06' in body['state_keys']['training_cutoff']
    assert r.headers['cache-control'] == 'private, no-store'
    assert len(log['report']) == 1 and log['report'][0][0] == 'owner-1'


def test_agreement_projected_closed():
    agreement = {'basis': 'leave_one_attempt_out', 'mixed_pairs': 5, 'shadow_pair_accuracy': 0.6, 'delivered_pair_accuracy': 0.5,
                 'attempts_same_order': 1, 'attempts_multi_item': 2, 'extra_id': 'ATT-9'}
    client, _ = make(report={**REPORT, 'agreement': agreement, 'agreement_withheld': None})
    body = post(client).json()
    assert set(body['agreement']) == set(agreement) - {'extra_id'} and 'ATT-9' not in str(body)


def test_bounds_with_many_sources():
    client, log = make(sources=30)
    body = post(client).json()
    assert body['truncated'] is True and body['sources_seen'] == 20 and log['pages'] == 20
    assert log['refs'] <= 21 and all(a[2] == 200 for a in log['page_args'])


def test_page_bound_within_one_source():
    client, log = make(sources=1, pages_per_source=25)
    body = post(client).json()
    assert body['truncated'] is True and log['pages'] == 20 and body['sources_seen'] == 1
    assert [a[1] for a in log['page_args']] == [None] + [str(i) for i in range(1, 20)]


def test_failures_consume_budget():
    client, log = make(sources=30, fail=tuple(range(30)))
    body = post(client).json()
    assert body['truncated'] is True and log['pages'] == 20 and body['sources_skipped'] == 20 and body['sources_seen'] == 20


def test_mixed_failures_and_pages_share_budget():
    client, log = make(sources=4, pages_per_source=8, fail=(1,))
    body = post(client).json()
    # source 0: 8 calls, source 1: 1 failed call, source 2: 8 calls, source 3: 3 of 8 calls
    assert body['truncated'] is True and log['pages'] == 20 and body['sources_skipped'] == 1 and body['sources_seen'] == 4


def test_not_truncated_when_within_bounds():
    client, log = make(sources=3, pages_per_source=1)
    body = post(client).json()
    assert body['truncated'] is False and body['sources_seen'] == 3 and log['pages'] == 3


def test_failing_source_skipped_and_counted():
    client, log = make(sources=3, fail=(1,))
    r = post(client)
    body = r.json()
    assert r.status_code == 200 and body['sources_skipped'] == 1 and body['sources_seen'] == 3
    assert 'secret' not in r.text and 'chat-' not in r.text
    assert len(log['report'][0][1]) == 2


def test_live_target_false_before_is_503_with_nothing_read():
    client, log = make(live=False)
    r = post(client)
    assert r.status_code == 503 and r.json() == {'state': 'unavailable'} and untouched(log) and log['prefs'] == 0
    assert r.headers['cache-control'] == 'private, no-store'


def test_live_target_false_after_is_503_without_figures():
    client, log = make(live=lambda n: n < 2)
    r = post(client)
    assert r.status_code == 503 and r.json() == {'state': 'unavailable'} and 'receipts' not in r.text
    assert len(log['report']) == 1
    assert r.headers['cache-control'] == 'private, no-store'


def test_bad_bodies_refused_before_anything_read():
    for raw in ('{"a":1}', '[]', 'nope', '', '{"account_id":"evil"}', '{' + ' ' * 300 + '}', '{"a":1,"a":2}', 'null'):
        client, log = make()
        r = post(client, raw)
        assert r.status_code == 400 and r.json() == {'state': 'unavailable'} and untouched(log) and log['prefs'] == 0
        assert r.headers['cache-control'] == 'private, no-store'


def test_unauthenticated_refused():
    client, log = make(authed=False)
    r = post(client)
    assert r.status_code == 403 and r.json() == {'state': 'unavailable'} and untouched(log)
    assert r.headers['cache-control'] == 'private, no-store'


def test_account_owner_from_context_not_body():
    client, log = make()
    post(client)
    assert log['negotiate'] == [('owner-1', 'ws-1')] and log['report'][0][0] == 'owner-1'
    assert all(row['owner'] == 'owner-1' for row in log['report'][0][1])
    c2, l2 = make()
    assert post(c2, '{"owner":"evil","account_id":"evil"}').status_code == 400 and untouched(l2)


def test_state_keys_are_closed_and_name_policy_revision_cutoff_and_versions():
    client, log = make(sources=2)
    body = post(client).json()
    keys = body['state_keys']
    assert set(keys) == STATE_KEYS
    assert keys['policy_revision'] == 3 and keys['policy_revision_state'] == 'ok' and log['rev_user'] == 'alice'
    assert keys['feature_version'] == 1 and keys['label_version'] == 'host_human_review.v1'
    assert keys['route_kinds'] == ['fts', 'graph']                                   # sorted
    assert keys['training_cutoff'] == 'cs_s06admitted_' + HEX                        # the last admitted event id read
    assert 'FOUND' not in str(keys) and 'ATT-' not in str(keys)


def test_state_keys_never_invent_a_policy_revision_or_cutoff():
    for revision in (0, None, True, -1, RuntimeError('store down')):
        client, _ = make(revision=revision)
        keys = post(client).json()['state_keys']
        assert keys['policy_revision'] is None and keys['policy_revision_state'] == 'unavailable', revision
    client, _ = make(sources=0)
    assert post(client).json()['state_keys']['training_cutoff'] is None


def test_state_keys_are_not_read_when_the_switch_is_off():
    client, log = make(pref=False)
    assert post(client).json() == {'state': 'off'} and 'rev_user' not in log

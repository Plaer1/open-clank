"""Host side of the pre-extracted candidate stage and the atomic update batch (fake provider, no binary)."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, FastAPI

import routes.memory.preextracted_candidates as stage_module
from routes.memory.preextracted_candidates import register_preextracted_candidates
from routes.memory.update_batch import register_update_batch
from src.memery_provider import MemoryRequestRejectedError
from src.openclank.account_request_barrier import AccountRequestBarrier

SHA = 'a' * 64
KEY = 'k' * 24


class Provider:
    _guarded_v17 = True

    def __init__(self):
        self.calls = []
        self.fail = None

    async def _host_negotiate_native_memory_ops(self, *, owner, workspace_id):
        return True

    async def _host_preextracted_stage(self, request, *, before_issue):
        self.calls.append(('stage', request)); assert before_issue() is True
        if self.fail: raise self.fail
        return {'version': 1, 'items': [{'candidate_id': 'preextracted_' + SHA, 'state': 'staged'} for _ in request['items']]}

    async def _host_preextracted_list(self, request):
        self.calls.append(('list', request)); return {'version': 1, 'items': [], 'next': None}

    async def _host_preextracted_review_preview(self, request):
        self.calls.append(('review_preview', request)); return {'version': 1, 'state': 'preview'}

    async def _host_preextracted_review(self, request, *, before_issue):
        self.calls.append(('review', request)); assert before_issue() is True
        return {'version': 1, 'state': 'rejected', 'candidate_id': request['candidate_id']}

    async def _host_update_batch_preview(self, request):
        self.calls.append(('preview', request))
        if self.fail: raise self.fail
        return {'state': 'preview', 'preview_sha256': SHA, 'items': [{'memory_id': i['memory_id']} for i in request['items']]}

    async def _host_update_batch_apply(self, request, *, before_issue):
        self.calls.append(('apply', request)); assert before_issue() is True
        if self.fail: raise self.fail
        return {'state': 'committed', 'batch_id': 'b', 'items': [{'memory_id': i['memory_id'], 'revision': 2} for i in request['items']]}

    async def _host_update_batch_status(self, request):
        self.calls.append(('status', request)); return {'state': 'unknown'}


def client(provider, owner='alice', monkeypatch=None):
    coordinator = SimpleNamespace(projection=object(), checked_refs=lambda user, account, workspace: {'ref': 1})
    monkeypatch.setattr(stage_module, 'policy_coordinator', lambda: coordinator)
    app = FastAPI(); app.state.memory_policy_projection = coordinator.projection
    barrier = AccountRequestBarrier(lambda _: False)
    context = {'barrier': barrier, 'account': 'acct-' + owner, 'workspace': 'ws', 'owner': owner,
               'current_write': lambda: True, 'current_live': lambda: True}
    async def owned(fn, *args): return fn(*args)
    router = APIRouter(prefix='/api/memory')
    kw = dict(require_human=lambda request: owner, capture_context=lambda request, user: context, provider=provider, owned=owned)
    register_preextracted_candidates(router, **kw); register_update_batch(router, **kw)
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t')


def item(**over):
    base = {'kind': 'fact', 'text': 'Lives in Lisbon', 'source': {'uri': 'doc://a', 'revision': 1, 'content_sha256': SHA}}
    return {**base, **over}


def post(path, body, provider, monkeypatch, owner='alice'):
    async def go():
        async with client(provider, owner, monkeypatch) as c: return await c.post('/api/memory' + path, json=body)
    return asyncio.run(go())


@pytest.mark.parametrize('bad', [
    [], [item()] * 101, [item(kind='nope')], [item(provenance={'kind': 'manual'})], [item(evidence_role='manual')],
    [item(source={'uri': 'u', 'revision': 0, 'content_sha256': SHA})], [item(source={'uri': 'u', 'revision': True, 'content_sha256': SHA})],
    [item(source={'uri': 'u', 'revision': 1, 'content_sha256': 'zz'})], [item(source={'uri': 'u', 'revision': 1, 'content_sha256': SHA, 'x': 1})],
    [item(target={'predicate': 'p'})], [item(text='  ')], [item(text='x' * 4001)], [item(source={'uri': 'memory://x', 'revision': 1, 'content_sha256': SHA})], ['text']])
def test_stage_refuses_invalid_items_before_the_provider(bad, monkeypatch):
    provider = Provider()
    assert post('/candidates/preextracted', {'request_key': KEY, 'items': bad}, provider, monkeypatch).status_code == 422
    assert provider.calls == []


def test_stage_refuses_unknown_top_keys_and_bad_key_and_legacy(monkeypatch):
    provider = Provider()
    for body in ({'request_key': KEY, 'items': [item()], 'owner': 'bob'}, {'request_key': ' padded ', 'items': [item()]}, {'items': [item()]}):
        assert post('/candidates/preextracted', body, provider, monkeypatch).status_code == 422
    provider._guarded_v17 = False
    response = post('/candidates/preextracted', {'request_key': KEY, 'items': [item()]}, provider, monkeypatch)
    assert response.status_code == 409 and response.json()['reason'] == 'guarded_store_required' and provider.calls == []


def test_stage_uses_the_authenticated_owner_and_replay_passes_the_same_key(monkeypatch):
    provider = Provider()
    body = {'request_key': KEY, 'items': [item(target={'predicate': 'lives_in', 'claim_slot': 'home'}), item()]}
    first = post('/candidates/preextracted', body, provider, monkeypatch, owner='alice')
    second = post('/candidates/preextracted', body, provider, monkeypatch, owner='alice')
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    (_, one), (_, two) = provider.calls
    assert one == two and one['owner'] == 'alice' and one['request_key'] == KEY and one['account_id'] == 'acct-alice'
    post('/candidates/preextracted', body, provider, monkeypatch, owner='bob')
    assert provider.calls[-1][1]['owner'] == 'bob'  # a caller can never name another owner


def update_item(mid='m1', **over):
    return {'memory_id': mid, 'candidate_id': 'c', 'origin_request_key': 'o', 'prior_receipt_id': 'p', 'expected_revision': 1,
            'selected_paths': ['/value/value'], 'payload': {'value': {'type': 'string', 'value': 'x'}}, **over}


@pytest.mark.parametrize('bad', [[], [update_item(str(i)) for i in range(101)], [update_item('a'), update_item('a')],
    [update_item(expected_revision=0)], [update_item(selected_paths=[])], [update_item(selected_paths=['/value/other'])],
    [update_item(extra=1)], [update_item(payload=[])]])
def test_update_batch_validation_refusals(bad, monkeypatch):
    provider = Provider()
    assert post('/update-batch/preview', {'items': bad}, provider, monkeypatch).status_code == 422
    assert post('/update-batch/apply', {'items': bad, 'request_key': KEY, 'preview_sha256': SHA}, provider, monkeypatch).status_code == 422
    assert provider.calls == []


def test_update_batch_preview_apply_status(monkeypatch):
    provider = Provider(); items = [update_item('m1'), update_item('m2')]
    assert post('/update-batch/preview', {'items': items}, provider, monkeypatch).json()['preview_sha256'] == SHA
    applied = post('/update-batch/apply', {'items': items, 'request_key': KEY, 'preview_sha256': SHA}, provider, monkeypatch)
    assert applied.status_code == 200 and [i['memory_id'] for i in applied.json()['items']] == ['m1', 'm2']

    async def status():
        async with client(provider, 'alice', monkeypatch) as c:
            return await c.get('/api/memory/update-batch/status', params={'request_key': KEY, 'preview_sha256': SHA})
    assert asyncio.run(status()).json() == {'state': 'unknown'}


@pytest.mark.parametrize('refusal', ['foreign id', 'stale revision'])
def test_update_batch_refusal_is_one_batch_error(refusal, monkeypatch):
    provider = Provider(); provider.fail = MemoryRequestRejectedError(refusal)
    response = post('/update-batch/apply', {'items': [update_item('m1'), update_item('m2')], 'request_key': KEY, 'preview_sha256': SHA}, provider, monkeypatch)
    assert response.status_code == 503 and response.json() == {'state': 'unavailable'}  # no per-item partial result exists
    assert len(provider.calls) == 1


def test_provider_result_validators():
    from src.memery_provider import MemeryProvider as P
    ok = {'state': 'committed', 'batch_id': 'b', 'items': [{'memory_id': 'm1', 'revision': 2}]}
    assert P._update_batch_result(ok, ['m1']) == ok
    for bad in ({**ok, 'items': [{'memory_id': 'm2', 'revision': 2}]}, {**ok, 'items': [{'memory_id': 'm1', 'revision': 1}]}, {'state': 'preview'}):
        with pytest.raises(MemoryRequestRejectedError): P._update_batch_result(bad, ['m1'])


CID = 'preextracted_' + SHA


def test_preextracted_list_and_review_pass_owner_scope_and_validate(monkeypatch):
    provider = Provider()
    async def go():
        async with client(provider, 'alice', monkeypatch) as c:
            listed = await c.get('/api/memory/candidates/preextracted', params={'limit': '10'})
            bad = await c.get('/api/memory/candidates/preextracted', params={'limit': '0'})
            preview = await c.post('/api/memory/candidates/preextracted/review-preview', json={'candidate_id': CID})
            wrong = await c.post('/api/memory/candidates/preextracted/review-preview', json={'candidate_id': 'manualadd_' + SHA})
            body = {'candidate_id': CID, 'candidate_digest': SHA, 'action': 'reject', 'request_key': 'rk-1', 'reason': 'no'}
            review = await c.post('/api/memory/candidates/preextracted/review', json=body)
            extra = await c.post('/api/memory/candidates/preextracted/review', json={**body, 'owner': 'bob'})
            return listed, bad, preview, wrong, review, extra
    listed, bad, preview, wrong, review, extra = asyncio.run(go())
    assert listed.status_code == 200 and bad.status_code == 422 and wrong.status_code == 422 and extra.status_code == 422
    assert preview.status_code == 200 and review.json()['state'] == 'rejected'
    assert all(call[1]['owner'] == 'alice' for call in provider.calls)

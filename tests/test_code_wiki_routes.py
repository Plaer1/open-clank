"""Stubbed route coverage for /code/wiki/plan and /code/wiki/page (no app, no Graph, no model)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from routes.memory.code_wiki import register_code_wiki

RECEIPT = {'workspace_id': 'ws', 'registration_id': 'r', 'source_snapshot_id': 'snap-1', 'checkout_id': 'c',
           'head_generation': 'h', 'admission_epoch': 1, 'policy_generation': 2, 'config_fingerprint': 'f'}
FILE_ID = 'file:' + hashlib.sha256(b'1').hexdigest()


def env(operation, hits, receipt=RECEIPT, **extra):
    return {'schema': 'open-clank.code-query/v1', 'operation': operation, 'receipt': receipt, 'hits': hits, 'edges': [],
            'omissions': [], 'partial': False, 'degraded': False, 'next_cursor': None, 'error': None, **extra}


class Barrier:
    live = True
    def assert_live_admission(self, owner, admission):
        if not self.live: raise RuntimeError('lost')


class Auth:
    def account_id(self, owner): return 'acct'
    def username_for_account_id(self, account): return 'alice'


def make_client(on_query=None):
    seen = []
    barrier = Barrier()
    async def query_code(*, repository, provider, auth_manager, owner, workspace_id, purpose, session, query):
        seen.append((owner, workspace_id, purpose, query['operation']))
        if on_query: on_query(barrier)
        op = query['operation']
        if op == 'catalog':
            return env('catalog', [{'id': FILE_ID, 'kind': 'file', 'label': 'src/a.py'}])
        if op == 'outline':
            return env('outline', [{'id': 'decl:' + 'a' * 64, 'kind': 'declaration', 'label': 'f', 'signature': 'def f()',
                                    'properties': {'declaration_kind': 'function'}}])
        return env('neighbors', [], edges=[])
    def require_user(request):
        user = request.headers.get('x-user')
        if not user: raise HTTPException(401, 'Authentication required')
        return user
    router = APIRouter()
    register_code_wiki(router, require_user=require_user, provider=object(), query_code=query_code, current_admission=lambda: 'adm')
    app = FastAPI()
    @app.middleware('http')
    async def mark(request, call_next):
        request.state.authenticated = True
        return await call_next(request)
    app.include_router(router)
    app.state.files_policy_repository = object()
    app.state.auth_manager = Auth()
    app.state.account_request_barrier = barrier
    app.state.capture_original_credential = lambda request, owner, account: (lambda: True)
    return TestClient(app), seen, barrier


H = {'x-user': 'alice'}


def post(client, path, raw, headers=H):
    data = raw if isinstance(raw, (bytes, str)) else json.dumps(raw)
    return client.post(path, content=data, headers={**headers, 'content-type': 'application/json'})


def plan_of(client):
    r = post(client, '/code/wiki/plan', {'workspace_id': 'ws'})
    assert r.status_code == 200, r.text
    return r.json()


def test_plan_returns_pages_and_index():
    client, seen, _ = make_client()
    data = plan_of(client)
    assert [p['rel_path'] for p in data['pages']] == ['src/a.py']
    assert 'openclank-wiki-index:v1:' in data['index_markdown'] and data['plan_hash']
    assert seen[0][:3] == ('alice', 'ws', 'app_folder')


def test_page_returns_markdown_with_marker():
    client, _, _ = make_client()
    data = plan_of(client)
    page = data['pages'][0]
    r = post(client, '/code/wiki/page', {'workspace_id': 'ws', 'receipt': data['receipt'], 'page': page})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out['page_id'] == page['page_id'] and f"openclank-wiki-page:v1:{page['page_id']}" in out['markdown']


def test_cache_control_on_success_and_error():
    client, _, _ = make_client()
    ok = post(client, '/code/wiki/plan', {'workspace_id': 'ws'})
    bad = post(client, '/code/wiki/plan', {'workspace_id': 'ws', 'x': 1})
    assert ok.headers['cache-control'] == 'private, no-store' and bad.headers['cache-control'] == 'private, no-store'


def test_over_cap_is_413():
    client, seen, _ = make_client()
    r = post(client, '/code/wiki/plan', {'workspace_id': 'w' * 5000})
    assert r.status_code == 413 and not seen
    data = plan_of(client)
    r = post(client, '/code/wiki/page', {'workspace_id': 'ws', 'receipt': data['receipt'], 'page': {**data['pages'][0], 'rel_path': 'p' * 9000}})
    assert r.status_code == 413


def test_bad_bodies_are_409_before_any_query():
    client, seen, _ = make_client()
    page = {'page_id': 'a' * 64, 'file_id': FILE_ID, 'rel_path': 'x', 'bytes_est': 1}
    for raw in ({'workspace_id': 'ws', 'extra': 1}, {'workspace_id': 5}, {'workspace_id': 'ws', 'cap': '5'},
                '{"workspace_id":"ws","workspace_id":"ws"}', '[1]', 'not json', '{"workspace_id":NaN}'):
        r = post(client, '/code/wiki/plan', raw)
        assert r.status_code == 409, (raw, r.status_code)
    for raw in ({'workspace_id': 'ws', 'receipt': RECEIPT, 'page': {**page, 'zz': 1}},
                {'workspace_id': 'ws', 'receipt': RECEIPT, 'page': {**page, 'bytes_est': '1'}},
                {'workspace_id': 'ws', 'receipt': RECEIPT},
                {'workspace_id': 'other', 'receipt': RECEIPT, 'page': page}):
        assert post(client, '/code/wiki/page', raw).status_code == 409
    assert not seen


def test_receipt_mismatch_is_409():
    client, _, _ = make_client()
    data = plan_of(client)
    wrong = {**data['receipt'], 'source_snapshot_id': 'snap-2'}
    r = post(client, '/code/wiki/page', {'workspace_id': 'ws', 'receipt': wrong, 'page': data['pages'][0]})
    assert r.status_code == 409 and 'snap' not in r.text
    r = post(client, '/code/wiki/plan', {'workspace_id': 'ws', 'receipt': wrong})
    assert r.status_code == 409


def test_unauthenticated_refused():
    client, seen, _ = make_client()
    r = post(client, '/code/wiki/plan', {'workspace_id': 'ws'}, headers={})
    assert r.status_code == 401 and not seen


def test_currentness_lost_is_refused():
    def lose(barrier): barrier.live = False
    client, _, _ = make_client(on_query=lose)
    r = post(client, '/code/wiki/plan', {'workspace_id': 'ws'})
    assert r.status_code == 409 and 'pages' not in r.text
    client, seen, barrier = make_client()
    barrier.live = False
    assert post(client, '/code/wiki/plan', {'workspace_id': 'ws'}).status_code == 409 and not seen


def test_missing_authority_is_503():
    client, _, _ = make_client()
    client.app.state.files_policy_repository = None
    assert post(client, '/code/wiki/plan', {'workspace_id': 'ws'}).status_code == 503


def test_source_has_no_model_path():
    text = (Path(__file__).resolve().parents[1] / 'routes' / 'memory' / 'code_wiki.py').read_text().lower()
    for word in ('generate', 'chat.complete', 'router_for', 'execute_model_operation', 'managed_operation', 'code_summary'):
        assert word not in text, word

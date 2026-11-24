"""BQ-8: POST /journal-coverage is READ-ONLY (stubbed archive, no app). Plan: "no cursor movement on a read"; preference default Off."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from routes.memory.journal_coverage import register_journal_coverage

PAGE = {'account_id': 'acct-secret', 'page_sha256': 'f' * 64, 'ack_seq': 2, 'high_seq': 9, 'end_seq': 5,
        'rows': [{'class': 'included', 'reason': 'host_user_body', 'message_id': 'mm'}, {'class': 'omitted', 'reason': 'unsupported_part', 'message_id': 'nn'}]}


def make(prefs=None, authed=True, live=True, profile=True, page=PAGE):
    touched, seen = [], []
    @asynccontextmanager
    async def admitted(user):
        yield
    ctx = {'account': 'acct-1', 'owner': 'owner-1', 'workspace': 'ws', 'state': SimpleNamespace(), 'barrier': SimpleNamespace(admitted=admitted),
           'current_live': lambda: live}
    def require_human(request):
        if not authed: raise HTTPException(401, 'no')
        return 'alice'
    async def owned(fn, *a, **k): return fn(*a, **k)
    archive = SimpleNamespace(advance_cursor=lambda **k: touched.append('advance_cursor'), ack_journal_delta=lambda **k: touched.append('ack'))
    router = APIRouter()
    register_journal_coverage(router, require_human=require_human, capture_context=lambda request, user: ctx, owned=owned,
        archive=lambda: archive, session_factory=lambda: None, profile_path=lambda request: '/p', lifecycle_for=lambda request, account: None,
        load_prefs=lambda user: prefs if prefs is not None else {}, page_reader=lambda request, context, user, chat: seen.append((context['account'], chat)) or page)
    app = FastAPI(); app.include_router(router)
    app.state.s05_profile = SimpleNamespace(assert_current=lambda: None, path='/p') if profile else None
    client = TestClient(app); client.touched, client.seen = touched, seen
    return client


def post(client, raw='{"chat_id":"c1"}'):
    return client.post('/journal-coverage', content=raw, headers={'Content-Type': 'application/json'})


def test_default_off_reads_nothing_and_moves_nothing():
    client = make()
    r = post(client)
    assert r.json() == {'state': 'off'} and client.seen == [] and client.touched == []


def test_ok_is_a_read_only_count_view_with_no_cursor_movement_or_secrets():
    client = make(prefs={'memory_policy_consolidation_enabled': True})
    r = post(client)
    body = r.json()
    assert r.status_code == 200 and body['state'] == 'ok' and body['counts']['included'] == 1 and body['counts']['omitted'] == 1
    assert (body['ack_seq'], body['high_seq'], body['end_seq']) == (2, 9, 5) and 'advanced' not in body and 'included_failed' not in body
    assert 'acct-secret' not in r.text and 'f' * 64 not in r.text and 'mm' not in r.text
    assert r.headers['cache-control'] == 'private, no-store' and client.touched == []
    assert client.seen == [('acct-1', 'c1')]                                  # account from the session context, chat from the body only


def test_refusals_and_strict_body():
    assert post(make(authed=False)).status_code == 403
    assert post(make(prefs={'memory_policy_consolidation_enabled': True}), '{"chat_id":"c1","account_id":"evil"}').status_code == 400
    assert post(make(prefs={'memory_policy_consolidation_enabled': True}), '{"chat_id":"a","chat_id":"b"}').status_code == 400
    assert post(make(prefs={'memory_policy_consolidation_enabled': True}, live=False)).status_code == 503
    assert post(make(profile=False)).json() == {'state': 'unavailable'}

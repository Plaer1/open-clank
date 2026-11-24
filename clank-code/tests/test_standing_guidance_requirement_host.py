"""C3-P2: optional typed `requirement` on standing guidance, Host side only (fake owner-RPC, fake provider at the route).

The no-requirement payload literals below were captured from the UNMODIFIED provider before the edit, then frozen.
"""
import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from src.memery_provider import MemeryProvider as P
from src.memory_provider import MemoryRequestRejectedError

REQ = {'version': 1, 'effect': 'publish_file', 'deny': {'audience': 'public'}}
H = 'a' * 64
TEXT = 'text'
ITEM = {'curated_id': 'c', 'block_id': 'b', 'revision': 2, 'receipt_id': 'cs_s10_purpose_' + H, 'receipt_sha256': H,
        'value_sha256': hashlib.sha256(TEXT.encode()).hexdigest(), 'text': TEXT}
BASE = {'version': 1, 'owner': 'o', 'workspace_id': 'w', 'account_id': 'a', 'curated_id': 'c', 'decision': 'approve', 'policy_projection': {'x': 1}}


def bad_requirements():
    out = []
    for path, val in (('effect', 'send_mail'), ('effect', 5), ('version', 2), ('version', True), ('version', 1.0), ('version', '1')):
        r = copy.deepcopy(REQ); r[path] = val; out.append(r)
    for val in ('allow', 'owner', ['public'], None):
        r = copy.deepcopy(REQ); r['deny']['audience'] = val; out.append(r)
    r = copy.deepcopy(REQ); r['extra'] = 1; out.append(r)
    r = copy.deepcopy(REQ); r['deny']['extra'] = 1; out.append(r)
    r = copy.deepcopy(REQ); del r['deny']; out.append(r)
    r = copy.deepcopy(REQ); r['deny'] = {}; out.append(r)
    r = copy.deepcopy(REQ); del r['version']; out.append(r)
    r = copy.deepcopy(REQ); r['deny'] = 'public'; out.append(r)
    out += [None, [], 'publish_file', 1, {}]
    return out


def make(canned=None):
    p = object.__new__(P); calls = []
    canned = canned or {}
    async def rpc(owner, ws, payload, deadline):
        calls.append(payload)
        ph = payload['phase']
        if ph == 'standing_guidance_preview':
            return canned.get(ph, {'version': 1, 'state': 'preview', 'request': dict(payload['request']), 'selector': {}, 'value': 'v'})
        if ph == 'standing_guidance_read':
            return {'version': 1, 'state': 'ready', 'decision': 'approve', 'revision': 1}
        if ph == 'standing_guidance_status':
            return {'state': 'unknown'}
        if ph == 'standing_guidance_select':
            return canned.get(ph, {'version': 1, 'state': 'ready', 'coverage': 'complete', 'items': [ITEM]})
        return {'version': 1, 'state': 'current'}
    async def cand(owner, ws, payload, require_live, write_deadline):
        calls.append(payload)
        r = payload['request']
        return {'state': 'applied', 'receipt_id': 'r', 'revision': r['expected_revision'] + 1, 'decision': r['decision']}
    p._s07_owner_rpc = rpc; p._host_candidate_request = cand; p._host_bounded_write_available = lambda: True
    return p, calls


def phase(p, ph, request):
    return asyncio.run(p._host_standing_guidance(owner='o', workspace_id='w', phase=ph, request=request, deadline=1.5))


def refused(coro_fn):
    try:
        coro_fn()
    except MemoryRequestRejectedError:
        return
    raise AssertionError('not refused')


def test_seven_key_items_unchanged():
    P._validate_standing_guidance_items([ITEM])
    for extra in ({'x': 1}, {'requirement': None}):
        refused(lambda: P._validate_standing_guidance_items([{**ITEM, **extra}]))
    refused(lambda: P._validate_standing_guidance_items([{k: v for k, v in ITEM.items() if k != 'text'}]))


def test_eight_key_item_validates_and_roundtrips_through_select_and_revalidate():
    item = {**ITEM, 'requirement': REQ}
    P._validate_standing_guidance_items([item])
    p, calls = make({'standing_guidance_select': {'version': 1, 'state': 'ready', 'coverage': 'complete', 'items': [item]}})
    async def go():
        r = await p._host_standing_guidance_select(owner='o', workspace_id='w', account_id='a', policy_projection={'p': 1}, deadline=1.0)
        assert r['items'] == [item] and r['items'][0]['requirement'] == REQ
        await p._host_standing_guidance_revalidate(owner='o', workspace_id='w', account_id='a', policy_projection={'p': 1}, items=r['items'], deadline=1.0)
    asyncio.run(go())
    assert calls[1]['request']['items'] == [item]


def test_bad_requirement_shapes_refused_in_items_and_requests():
    for bad in bad_requirements():
        refused(lambda: P._validate_standing_guidance_items([{**ITEM, 'requirement': bad}]))
        p, calls = make()
        for ph in ('preview', 'read', 'status', 'apply'):
            refused(lambda: phase(p, ph, {**BASE, 'expected_revision': 0, 'requirement': bad}))
        assert calls == [], bad  # refused before any Native call


def test_requirement_with_withdraw_refused_before_native():
    p, calls = make()
    for ph in ('preview', 'read', 'apply'):
        refused(lambda: phase(p, ph, {**BASE, 'decision': 'withdraw', 'expected_revision': 0, 'requirement': REQ}))
    assert calls == []


def test_preview_request_reaches_apply_unchanged_and_status_resends():
    p, calls = make()
    request = {**BASE, 'requirement': REQ}
    preview = phase(p, 'preview', request)
    assert preview['request'] == request
    applied = phase(p, 'apply', {**preview['request'], 'expected_revision': 0})
    assert applied['state'] == 'applied'
    status = {'owner': 'o', 'workspace_id': 'w', 'curated_id': 'c', 'requirement': REQ}
    assert phase(p, 'status', status) == {'state': 'unknown'}
    assert phase(p, 'read', request)['decision'] == 'approve'
    assert [c['request'].get('requirement') for c in calls] == [REQ] * 4
    assert calls[1]['request'] == {**request, 'expected_revision': 0}


def test_preview_must_not_invent_or_drop_requirement():
    p, _ = make({'standing_guidance_preview': {'version': 1, 'state': 'preview', 'request': {**BASE, 'requirement': REQ}, 'selector': {}, 'value': 'v'}})
    refused(lambda: phase(p, 'preview', dict(BASE)))  # invented
    p, _ = make({'standing_guidance_preview': {'version': 1, 'state': 'preview', 'request': dict(BASE), 'selector': {}, 'value': 'v'}})
    refused(lambda: phase(p, 'preview', {**BASE, 'requirement': REQ}))  # dropped


def test_no_requirement_payloads_identical_to_unmodified_code():
    # Literals captured from the unmodified provider (same fakes), then frozen.
    p, calls = make()
    status = {'owner': 'o', 'workspace_id': 'w', 'curated_id': 'c'}
    for ph, rq in (('preview', BASE), ('read', BASE), ('status', status), ('apply', {**BASE, 'expected_revision': 0})):
        phase(p, ph, rq)
    assert json.dumps(calls, sort_keys=True) == (
        '[{"phase": "standing_guidance_preview", "request": {"account_id": "a", "curated_id": "c", "decision": "approve", "owner": "o", "policy_projection": {"x": 1}, "version": 1, "workspace_id": "w"}}, '
        '{"phase": "standing_guidance_read", "request": {"account_id": "a", "curated_id": "c", "decision": "approve", "owner": "o", "policy_projection": {"x": 1}, "version": 1, "workspace_id": "w"}}, '
        '{"phase": "standing_guidance_status", "request": {"curated_id": "c", "owner": "o", "workspace_id": "w"}}, '
        '{"phase": "standing_guidance_apply", "request": {"account_id": "a", "curated_id": "c", "decision": "approve", "expected_revision": 0, "owner": "o", "policy_projection": {"x": 1}, "version": 1, "workspace_id": "w"}}]')
    p, calls = make()
    async def go():
        r = await p._host_standing_guidance_select(owner='o', workspace_id='w', account_id='a', policy_projection={'p': 1}, deadline=1.0)
        await p._host_standing_guidance_revalidate(owner='o', workspace_id='w', account_id='a', policy_projection={'p': 1}, items=r['items'], deadline=1.0)
    asyncio.run(go())
    assert json.dumps(calls, sort_keys=True) == (
        '[{"phase": "standing_guidance_select", "request": {"account_id": "a", "owner": "o", "policy_projection": {"p": 1}, "version": 1, "workspace_id": "w"}}, '
        '{"phase": "standing_guidance_revalidate", "request": {"account_id": "a", "items": [{"block_id": "b", "curated_id": "c", "receipt_id": "cs_s10_purpose_' + H + '", '
        '"receipt_sha256": "' + H + '", "revision": 2, "text": "text", "value_sha256": "982d9e3eb996f559e633f4d194def3761d909f5a3b647d1a851fead67c32c9d1"}], '
        '"owner": "o", "policy_projection": {"p": 1}, "version": 1, "workspace_id": "w"}}]')


# ---- route: the real handler with its authority collaborators replaced by fakes ----

class _Provider:
    def __init__(self):
        self.calls = []
    async def _host_standing_guidance(self, *, owner, workspace_id, phase, request=None, deadline=None):
        self.calls.append((phase, copy.deepcopy(request)))
        if phase == 'capability':
            return True
        if phase in ('preview', 'read'):
            if phase == 'read':
                return {'version': 1, 'state': 'ready', 'decision': 'inactive', 'revision': 1}
            return {'version': 1, 'state': 'preview', 'request': {**request, 'expected_revision': 1}, 'selector': {'block_id': 'b'}, 'value': 'v'}
        if phase == 'apply':
            return {'state': 'applied', 'receipt_id': 'r', 'revision': 2, 'decision': request['decision']}
        return {'state': 'unknown'}


def route_call(action, body, provider=None):
    from routes.memory.memory_routes import setup_memory_routes
    from src.memory_scope import chat_workspace
    provider = provider or _Provider()
    router = setup_memory_routes(SimpleNamespace(), SimpleNamespace(), memory_provider=provider)
    endpoint = next(r.endpoint for r in router.routes if r.path.endswith('/{memory_id}/standing-guidance/{action}'))
    class Profile:
        def __init__(self): self.provider = provider
        def assert_current(self): pass
    class Auth:
        def account_id(self, user): return 'acct'
        def username_for_account_id(self, acct): return 'alice'
    class Authority:
        def __init__(self, *, auth_manager, supervisor): self._auth = auth_manager
        def resolve(self, account): return SimpleNamespace(memory_owner='alice', memory_workspace=chat_workspace())
    class Coord:
        projection = object()
        def checked_refs(self, *a): return {'refs': 1}
    coord = Coord()
    @asynccontextmanager
    async def admitted(user): yield
    state = SimpleNamespace(auth_manager=Auth(), s05_profile=Profile(), account_request_barrier=SimpleNamespace(admitted=admitted),
        mimo_supervisor=SimpleNamespace(owner_lifecycle_pool=lambda: None), memory_reset_coordinator=lambda: SimpleNamespace(owner_is_fenced=lambda u: False),
        account_owner_lifecycle=SimpleNamespace(owner_has_active_operation=lambda u: False), memory_policy_projection=coord.projection)
    async def jbody(): return body
    request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=state), json=jbody)
    store = SimpleNamespace(effective_snapshot=lambda *a, **k: {'read_allowed': True})
    with patch('src.auth_helpers.require_privilege', lambda r, k: 'alice'), patch('src.auth_helpers.require_user', lambda r: 'alice'), \
         patch('src.auth_helpers.is_internal_tool_identity', lambda u: False), \
         patch('src.openclank.memory_job_admission.SingleHostProfile', Profile), patch('src.openclank.memory_job_admission.TargetAuthority', Authority), \
         patch('src.openclank.memory_policy.STORE', store), patch('src.openclank.memory_policy.explicit_human_review_allowed', lambda s: True), \
         patch('src.openclank.memory_policy_projection.policy_coordinator', lambda: coord), \
         patch('src.openclank.chat_source_bridge.canonical_source_provider', lambda s: provider):
        try:
            return asyncio.run(endpoint('c', action, request)), provider
        except Exception as exc:
            return exc, provider


def test_route_forwards_valid_requirement_and_echoes_preview():
    result, prov = route_call('preview', {'decision': 'approve', 'requirement': REQ})
    assert result['requirement'] == REQ and result['state'] == 'preview', result
    assert prov.calls[1][0] == 'preview' and prov.calls[1][1]['requirement'] == REQ
    selector = result['selector']
    result, prov = route_call('apply', {'decision': 'approve', 'requirement': REQ, 'selector': selector, 'confirmed': True})
    assert result['state'] == 'applied', result
    assert [c[0] for c in prov.calls] == ['capability', 'preview', 'apply'] and prov.calls[2][1]['requirement'] == REQ
    status = {'version': 1, 'scope': selector['scope'], 'curated_id': 'c', 'block_id': 'b', 'predecessor_receipt_id': 'p', 'expected_revision': 1, 'request_sha256': H, 'requirement': REQ}
    result, prov = route_call('status', status)
    assert result == {'state': 'unknown'} and prov.calls[1][1]['requirement'] == REQ


def test_route_without_requirement_sends_exactly_todays_request():
    result, prov = route_call('preview', {'decision': 'approve'})
    assert 'requirement' not in result and 'requirement' not in prov.calls[1][1]
    assert prov.calls[1][1] == {'version': 1, 'owner': 'alice', 'workspace_id': prov.calls[1][1]['workspace_id'], 'account_id': 'acct',
        'curated_id': 'c', 'decision': 'approve', 'policy_projection': {'refs': 1}}


def test_route_refuses_bad_requirement_before_provider_call():
    from fastapi import HTTPException
    bads = [(a, {'decision': 'approve', 'requirement': b}) for b in bad_requirements() for a in ('preview',)]
    bads += [('preview', {'decision': 'withdraw', 'requirement': REQ}), ('read', {'decision': 'approve', 'requirement': REQ}),
             ('preview', {'requirement': REQ})]
    for action, body in bads:
        result, prov = route_call(action, body)
        assert isinstance(result, HTTPException) and result.status_code == 503, (body, result)
        assert [c[0] for c in prov.calls] == ['capability'], body

"""M-2: the relation (supersede/detach) front uses the same human-review authority as the batch routes:
apply needs current_write (explicit_human_review_allowed), preview needs current_live, and the body is bounded and duplicate-key strict."""
import asyncio
from types import SimpleNamespace

import httpx
from fastapi import APIRouter, FastAPI

from routes.memory.native_ops_supersede import register_native_ops_supersede
from src.openclank.account_request_barrier import AccountRequestBarrier

FIELDS = {'candidate_id': 'c', 'origin_request_key': 'o', 'prior_receipt_id': 'p', 'expected_revision': 1, 'request_key': 'rk', 'reason': 'r'}


class Provider:
    _guarded_v17 = True
    _workspace_id = 'ws'

    def __init__(self):
        self.reads = 0

    async def versioned_relation(self, memory_id, *, owner):
        self.reads += 1
        return {'block_id': 'b-' + memory_id, 'revision': 2, 'relation': None}


def call(path, raw, *, write_ok, provider):
    live = {'write': write_ok}
    context = {'barrier': AccountRequestBarrier(lambda _: False), 'owner': 'alice',
               'current_live': lambda: True, 'current_write': lambda: live['write']}

    async def owned(fn, *args): return fn(*args)
    app = FastAPI(); router = APIRouter(prefix='/api/memory')
    register_native_ops_supersede(router, require_human=lambda request: 'alice', capture_context=lambda request, user: context,
                                  owned=owned, provider=provider, mirror_request_data=lambda body, apply: body)
    app.include_router(router)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
            return await c.post('/api/memory' + path, content=raw, headers={'Content-Type': 'application/json'})
    return asyncio.run(go())


def body(phase, **extra):
    import json
    payload = {'phase': phase, **FIELDS, 'target_id': 't1', **extra}
    if phase == 'apply': payload |= {'operation_json': '{}', 'operation_sha256': 'a' * 64}
    return json.dumps(payload)


def test_apply_is_refused_when_the_review_predicate_no_longer_allows_it_but_preview_is_not_a_write():
    provider = Provider()
    refused = call('/m1/supersede', body('apply'), write_ok=False, provider=provider)
    assert refused.status_code == 503 and refused.json() == {'state': 'unavailable'} and provider.reads == 0
    assert refused.headers['cache-control'] == 'private, no-store'
    call('/m1/supersede', body('preview'), write_ok=False, provider=provider)
    assert provider.reads == 2  # preview ran its reads (current_live only)
    # with write authority the apply gets past the gate and reaches the provider
    provider.reads = 0
    call('/m1/supersede', body('apply'), write_ok=True, provider=provider)
    assert provider.reads == 2


def test_relation_body_is_duplicate_key_strict_and_bounded():
    provider = Provider()
    dup = '{"phase":"preview","phase":"apply"}'
    assert call('/m1/detach', dup, write_ok=True, provider=provider).status_code == 422
    assert call('/m1/detach', '{"phase":"preview","x":"' + 'a' * 200_000 + '"}', write_ok=True, provider=provider).status_code == 422
    assert provider.reads == 0

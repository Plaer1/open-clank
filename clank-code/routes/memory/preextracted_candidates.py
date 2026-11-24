"""Named-human entry point for staging pre-extracted typed facts as pending inferred candidates.

Also holds the guard shared with routes/memory/update_batch.py. Provenance is set by Native only:
a request carrying any key outside the closed shape is refused before it reaches the provider.
"""
import asyncio
import json
import urllib.parse

from fastapi import Request
from fastapi.responses import JSONResponse
from src.openclank.memory_batch_wire import WireError, candidate_id, stage_request_key, validate_preextracted_items
from src.openclank.memory_policy_projection import policy_coordinator

NO_STORE = {'Cache-Control': 'private, no-store'}


def refuse(status, state, reason=None):
    return JSONResponse({'state': state, **({'reason': reason} if reason else {})}, status_code=status, headers=NO_STORE)


async def read_json_body(request, limit=128 * 1024):
    chunks = []; size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit: raise WireError('body too large')
        chunks.append(chunk)
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result: raise WireError('duplicate key')
            result[key] = value
        return result
    try:
        body = json.loads(b''.join(chunks).decode('utf-8', 'strict'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError())) if chunks else {}
    except (ValueError, UnicodeError) as exc:
        raise WireError('invalid JSON') from exc
    return body


async def guarded_native_op(request: Request, *, require_human, capture_context, provider, owned, write, parse, call, operation=None):
    """Same guard as explicit_add_batch: named human, original-credential context, policy projection, one admission."""
    try:
        user = require_human(request)
        context = capture_context(request, user)  # original credential before the body await
    except Exception:
        return refuse(403, 'unavailable')
    try:
        if not getattr(provider, '_guarded_v17', False):
            return refuse(409, 'unavailable', 'guarded_store_required')
        try:
            fields = parse(request, await read_json_body(request) if request.method == 'POST' else None)
        except WireError as exc:
            return JSONResponse({'state': 'invalid', 'detail': str(exc)}, status_code=422, headers=NO_STORE)
        coordinator = policy_coordinator()
        if coordinator is None or coordinator.projection is not getattr(request.app.state, 'memory_policy_projection', None):
            raise ValueError('policy unavailable')
        async with context['barrier'].admitted(user):
            check = context['current_write'] if write else context['current_live']
            async def current():
                if not await owned(check): raise ValueError('authority changed')
                refs = await owned(coordinator.checked_refs, user, context['account'], context['workspace'])
                if coordinator is not policy_coordinator() or not await owned(check): raise ValueError('authority changed')
                return refs
            refs = await current()
            if await provider._host_negotiate_native_memory_ops(owner=context['owner'], workspace_id=context['workspace']) is not True:
                raise ValueError('capability unavailable')
            if operation is not None and operation not in provider._native_memory_ops_support(): raise ValueError('capability unavailable')
            if await current() != refs: raise ValueError('authority changed')
            base = {'version': 1, 'owner': context['owner'], 'workspace_id': context['workspace'],
                    'account_id': context['account'], 'policy_projection': refs}
            def before_issue():
                try:
                    return (check() is True and coordinator is policy_coordinator()
                            and coordinator.checked_refs(user, context['account'], context['workspace']) == refs and check() is True)
                except Exception: return False
            task = asyncio.create_task(call(base, fields, before_issue))
            try: result = await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try: await asyncio.shield(task)
                    except asyncio.CancelledError: continue
                task.result(); raise
            if await current() != refs: raise ValueError('authority changed')
            response = JSONResponse(result, headers=NO_STORE)
        if check() is not True: raise ValueError('authority changed')
        return response
    except asyncio.CancelledError: raise
    except Exception:
        return refuse(503, 'unavailable')


def register_preextracted_candidates(router, *, require_human, capture_context, provider, owned):
    def parse(request, body):
        if type(body) is not dict or set(body) != {'request_key', 'items'}: raise WireError('invalid request')
        stage_request_key(body['request_key']); validate_preextracted_items(body['items'])
        return body

    async def call(base, fields, before_issue):
        return await provider._host_preextracted_stage({**base, 'request_key': fields['request_key'], 'items': fields['items']},
                                                       before_issue=before_issue)

    @router.post('/candidates/preextracted')
    async def stage_preextracted(request: Request):
        return await guarded_native_op(request, require_human=require_human, capture_context=capture_context,
                                       provider=provider, owned=owned, write=True, parse=parse, call=call, operation="preextracted_candidates_v1")

    def parse_list(request, body):
        params = urllib.parse.parse_qs(request.url.query, keep_blank_values=True)
        if not set(params) <= {'after', 'limit'} or any(len(v) != 1 for v in params.values()): raise WireError('invalid request')
        after = params['after'][0] if 'after' in params else None
        if after is not None: candidate_id(after)
        try: limit = int(params['limit'][0]) if 'limit' in params else 50
        except ValueError as exc: raise WireError('invalid limit') from exc
        if not 1 <= limit <= 50: raise WireError('invalid limit')
        return {'after': after, 'limit': limit}

    @router.get('/candidates/preextracted')
    async def list_preextracted(request: Request):
        async def call(base, fields, before_issue): return await provider._host_preextracted_list({**base, **fields})
        return await guarded_native_op(request, require_human=require_human, capture_context=capture_context,
                                       provider=provider, owned=owned, write=False, parse=parse_list, call=call, operation="preextracted_candidates_v1")

    def parse_preview(request, body):
        if type(body) is not dict or set(body) != {'candidate_id'}: raise WireError('invalid request')
        candidate_id(body['candidate_id']); return body

    @router.post('/candidates/preextracted/review-preview')
    async def preview_preextracted(request: Request):
        async def call(base, fields, before_issue): return await provider._host_preextracted_review_preview({**base, **fields})
        return await guarded_native_op(request, require_human=require_human, capture_context=capture_context,
                                       provider=provider, owned=owned, write=False, parse=parse_preview, call=call, operation="preextracted_candidates_v1")

    def parse_review(request, body):
        if type(body) is not dict or set(body) != {'candidate_id', 'candidate_digest', 'action', 'request_key', 'reason'}: raise WireError('invalid request')
        candidate_id(body['candidate_id']); stage_request_key(body['request_key'])
        if (type(body['candidate_digest']) is not str or len(body['candidate_digest']) != 64
                or any(c not in '0123456789abcdef' for c in body['candidate_digest']) or body['action'] not in ('accept', 'reject')
                or type(body['reason']) is not str or not 1 <= len(body['reason']) <= 500):
            raise WireError('invalid review')
        return body

    @router.post('/candidates/preextracted/review')
    async def review_preextracted(request: Request):
        async def call(base, fields, before_issue): return await provider._host_preextracted_review({**base, **fields}, before_issue=before_issue)
        return await guarded_native_op(request, require_human=require_human, capture_context=capture_context,
                                       provider=provider, owned=owned, write=True, parse=parse_review, call=call, operation="preextracted_candidates_v1")

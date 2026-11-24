"""Named-human routes for the atomic guarded update batch (preview, apply, status).

Each item carries the same binding the single panel edit sends (mirror-context values); the
named-human authority, revision CAS and approved paths are checked per item inside Native's one transaction.
"""
import urllib.parse

from fastapi import Request
from src.openclank.memory_batch_wire import WireError, preview_sha, request_key, validate_update_items

from routes.memory.preextracted_candidates import guarded_native_op


def register_update_batch(router, *, require_human, capture_context, provider, owned):
    def run(request, **kw):
        return guarded_native_op(request, require_human=require_human, capture_context=capture_context,
                                 provider=provider, owned=owned, operation="knowledge_update_batch_v1", **kw)

    def parse_preview(request, body):
        if type(body) is not dict or set(body) != {'items'}: raise WireError('invalid request')
        validate_update_items(body['items']); return body

    def parse_apply(request, body):
        if type(body) is not dict or set(body) != {'items', 'request_key', 'preview_sha256'}: raise WireError('invalid request')
        validate_update_items(body['items']); request_key(body['request_key']); preview_sha(body['preview_sha256']); return body

    def parse_status(request, body):
        params = urllib.parse.parse_qs(request.url.query, keep_blank_values=True, strict_parsing=bool(request.url.query))
        if set(params) != {'request_key', 'preview_sha256'} or any(len(v) != 1 for v in params.values()):
            raise WireError('invalid request')
        return {'request_key': request_key(params['request_key'][0]), 'preview_sha256': preview_sha(params['preview_sha256'][0])}

    @router.post('/update-batch/preview')
    async def preview(request: Request):
        async def call(base, fields, before_issue): return await provider._host_update_batch_preview({**base, 'items': fields['items']})
        return await run(request, write=False, parse=parse_preview, call=call)

    @router.post('/update-batch/apply')
    async def apply(request: Request):
        async def call(base, fields, before_issue):
            return await provider._host_update_batch_apply({**base, **{k: fields[k] for k in ('request_key', 'preview_sha256', 'items')}},
                                                           before_issue=before_issue)
        return await run(request, write=True, parse=parse_apply, call=call)

    @router.get('/update-batch/status')
    async def status(request: Request):
        async def call(base, fields, before_issue): return await provider._host_update_batch_status({**base, **fields})
        return await run(request, write=False, parse=parse_status, call=call)

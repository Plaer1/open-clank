"""Named-human entry points for the existing guarded atomic Add owner."""
import asyncio
import json
import re

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from src.openclank.memory_policy_projection import policy_coordinator


def register_explicit_add_batch(router, *, require_human, capture_context, provider, owned):
    async def execute(request: Request, phase: str):
        try:
            user = require_human(request)
            context = capture_context(request, user)  # original credential before body await
        except Exception:
            return JSONResponse({'state':'unavailable'},status_code=403,headers={'Cache-Control':'private, no-store'})
        try:
            chunks=[]; size=0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 100 * 1024: raise ValueError('body too large')
                chunks.append(chunk)
            def pairs(values):
                result={}
                for key,value in values:
                    if key in result: raise ValueError('duplicate key')
                    result[key]=value
                return result
            body=json.loads(b''.join(chunks).decode('utf-8','strict'),object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError())) if chunks else {}
            fields={'capability':set(),'preview':{'candidate_ids'},'apply':{'candidate_ids','request_key','preview_sha256'},'status':{'request_key','preview_sha256'}}
            if type(body) is not dict or set(body)!=fields[phase]: raise ValueError('invalid request')
            ids=body.get('candidate_ids')
            if ids is not None and (type(ids) is not list or not 1<=len(ids)<=100 or len(set(ids))!=len(ids) or any(type(x) is not str or re.fullmatch(r'manualadd_[0-9a-f]{64}',x) is None for x in ids)): raise ValueError('invalid selection')
            if phase in ('apply','status') and (type(body['request_key']) is not str or re.fullmatch(r'[A-Za-z0-9_-]{22,128}',body['request_key']) is None or type(body['preview_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',body['preview_sha256']) is None): raise ValueError('invalid original')
            coordinator=policy_coordinator()
            if coordinator is None or coordinator.projection is not getattr(request.app.state,'memory_policy_projection',None): raise ValueError('policy unavailable')
            async with context['barrier'].admitted(user):
                check=context['current_write'] if phase=='apply' else context['current_live']
                async def current():
                    if not await owned(check): raise ValueError('authority changed')
                    refs=await owned(coordinator.checked_refs,user,context['account'],context['workspace'])
                    if coordinator is not policy_coordinator() or not await owned(check): raise ValueError('authority changed')
                    return refs
                refs=await current()
                available=await provider._host_negotiate_native_memory_ops(owner=context['owner'],workspace_id=context['workspace']) is True
                if await current()!=refs: raise ValueError('authority changed')
                if phase=='capability':
                    result={'version':1,'available':available,'operations':['explicit_add_review_batch_v1'] if available else []}
                else:
                    if not available: raise ValueError('capability unavailable')
                    base={'version':1,'owner':context['owner'],'workspace_id':context['workspace'],'account_id':context['account'],'policy_projection':refs}
                    if phase=='preview': result=await provider._host_native_memory_ops_batch_preview({**base,'candidate_ids':ids})
                    elif phase=='status': result=await provider._host_native_memory_ops_batch_status({**base,'request_key':body['request_key'],'preview_sha256':body['preview_sha256']})
                    else:
                        def before_issue():
                            try:
                                return (check() is True and coordinator is policy_coordinator()
                                        and coordinator.checked_refs(user,context['account'],context['workspace']) == refs
                                        and check() is True)
                            except Exception: return False
                        task=asyncio.create_task(provider._host_native_memory_ops_batch_apply({'selector':{**base,'candidate_ids':ids},'request_key':body['request_key'],'preview_sha256':body['preview_sha256']},before_issue=before_issue))
                        try: result=await asyncio.shield(task)
                        except asyncio.CancelledError:
                            while not task.done():
                                try: await asyncio.shield(task)
                                except asyncio.CancelledError: continue
                            task.result(); raise
                    if await current()!=refs: raise ValueError('authority changed')
                response=JSONResponse(result,headers={'Cache-Control':'private, no-store'})
                if await current()!=refs: raise ValueError('authority changed')
            if check() is not True: raise ValueError('authority changed')
            return response
        except asyncio.CancelledError: raise
        except Exception:
            return JSONResponse({'state':'unavailable'},status_code=503,headers={'Cache-Control':'private, no-store'})
    def endpoint_for(phase):
        async def endpoint(request: Request): return await execute(request, phase)
        return endpoint
    for phase in ('capability','preview','apply','status'):
        router.add_api_route('/candidates/batch/'+phase,endpoint_for(phase),methods=['POST'])

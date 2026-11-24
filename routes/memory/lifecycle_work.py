"""Explicit, named-human trigger for the existing S08 journal owner."""
import json

from fastapi import Request
from fastapi.responses import JSONResponse

from src.openclank.memory_lifecycle_work import consolidate_chat_now

_HEADERS = {'Cache-Control': 'private, no-store'}


def register_lifecycle_work(router, *, require_human, capture_context, owned):
    @router.post('/consolidate-now')
    async def consolidate_now(request: Request):
        try:
            user = require_human(request)
            context = capture_context(request, user)
        except Exception:
            return JSONResponse({'state':'unavailable','lastActualRun':{'state':'unavailable'}}, status_code=403, headers=_HEADERS)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw)>1024: raise ValueError('body too large')
            def pairs(values):
                result = {}
                for key,value in values:
                    if key in result: raise ValueError('duplicate field')
                    result[key]=value
                return result
            body=json.loads(bytes(raw).decode('utf-8','strict'),object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON')))
            chat=body.get('chat_id') if type(body) is dict and set(body)=={'chat_id'} else None
            if type(chat) is not str or not chat or chat!=chat.strip() or len(chat.encode('utf-8'))>256:
                raise ValueError('invalid chat')
        except (ValueError,UnicodeError):
            return JSONResponse({'state':'unavailable','lastActualRun':{'state':'unavailable'}}, status_code=400, headers=_HEADERS)
        try:
            async with context['barrier'].admitted(user):
                if not await owned(context['current_write']): raise ValueError('authority changed')
                scheduler=getattr(request.app.state,'task_scheduler',None)
                if scheduler is None: raise ValueError('scheduler unavailable')
                result=await consolidate_chat_now(scheduler=scheduler,state=request.app.state,owner=user,
                    account_id=context['account'],chat_id=chat,memory_owner=context['owner'],workspace_id=context['workspace'],
                    current=context['current_write'])
                if not await owned(context['current_write']) or getattr(request.app.state,'task_scheduler',None) is not scheduler:
                    raise ValueError('authority changed')
                return JSONResponse(result,headers=_HEADERS)
        except Exception:
            return JSONResponse({'state':'unavailable','lastActualRun':{'state':'unavailable'}}, status_code=503, headers=_HEADERS)

    @router.post('/distill-selected')
    async def distill_selected_route(request: Request):
        from src.openclank.chat_source_bridge import canonical_source_provider
        from src.openclank.memory_distill_work import distill_selected
        try:
            user=require_human(request)
            context=capture_context(request,user)
            raw=bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw)>32768: raise ValueError('body too large')
            def pairs(values):
                result={}
                for key,value in values:
                    if key in result: raise ValueError('duplicate field')
                    result[key]=value
                return result
            body=json.loads(bytes(raw).decode('utf-8','strict'),object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON')))
            if type(body) is not dict or set(body)!={'occurrences'}: raise ValueError('invalid selection')
        except Exception:
            return JSONResponse({'state':'unavailable'},status_code=400,headers=_HEADERS)
        try:
            async with context['barrier'].admitted(user):
                if not await owned(context['current_write']): raise ValueError('authority changed')
                provider=canonical_source_provider(request.app.state)
                scheduler=getattr(request.app.state,'task_scheduler',None)
                if provider is None or scheduler is None: raise ValueError('owner unavailable')
                async def action(*,owner):
                    if owner!=user: return {'state':'unavailable'}
                    return await distill_selected(request=request,context=context,provider=provider,occurrences=body['occurrences'])
                result=await scheduler._run_owner_aux_action(user,action)
                if not await owned(context['current_write']): raise ValueError('authority changed')
                return JSONResponse(result if type(result) is dict else {'state':'unavailable'},headers=_HEADERS)
        except Exception:
            return JSONResponse({'state':'unavailable'},status_code=503,headers=_HEADERS)

    @router.post('/distill-preview')
    async def distill_preview_route(request: Request):
        from src.openclank.chat_source_bridge import canonical_source_provider
        from src.openclank.memory_distill_work import preview_selected_chats
        try:
            user=require_human(request); context=capture_context(request,user)
            raw=bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw)>4096: raise ValueError('body too large')
            def pairs(values):
                result={}
                for key,value in values:
                    if key in result: raise ValueError('duplicate field')
                    result[key]=value
                return result
            body=json.loads(bytes(raw).decode('utf-8','strict'),object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON')))
            if type(body) is not dict or set(body)!={'chat_ids'}: raise ValueError('invalid selection')
            async with context['barrier'].admitted(user):
                if not await owned(context['current_write']): raise ValueError('authority changed')
                provider=canonical_source_provider(request.app.state)
                if provider is None: raise ValueError('owner unavailable')
                result=await preview_selected_chats(request=request,context=context,provider=provider,chat_ids=body['chat_ids'])
                if not await owned(context['current_write']): raise ValueError('authority changed')
                return JSONResponse(result,headers=_HEADERS)
        except Exception:
            return JSONResponse({'state':'unavailable','reason':'source_unavailable'},status_code=503,headers=_HEADERS)

    @router.get('/consolidation-retained-run')
    async def consolidation_retained_run(request: Request, output_kind: str = 'summary'):
        from src.openclank.chat_source_bridge import canonical_source_provider
        from src.openclank.memory_lifecycle_work import retained_history
        try:
            if output_kind not in ('summary','journal_synthesis','connection'): raise ValueError('unsupported output')
            user=require_human(request);context=capture_context(request,user)
            async with context['barrier'].admitted(user):
                if not await owned(context['current_write']): raise ValueError('authority changed')
                provider=canonical_source_provider(request.app.state)
                if provider is None: raise ValueError('owner unavailable')
                result=await retained_history(context=context,provider=provider,output_kind=output_kind)
                if not await owned(context['current_write']): raise ValueError('authority changed')
                return JSONResponse(result,headers=_HEADERS)
        except Exception:
            return JSONResponse({'version':1,'state':'unavailable'},status_code=503,headers=_HEADERS)

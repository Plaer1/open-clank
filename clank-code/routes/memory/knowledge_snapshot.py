"""Named-human review of one independently portable knowledge snapshot."""
import base64, json, re
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from services.memory import knowledge_snapshot_export
from src.openclank.memory_policy_projection import policy_coordinator


def register_knowledge_snapshot(router, *, require_human, capture_context, provider, owned, batch_store=None):
    async def execute(request: Request, phase: str):
        try:
            user=require_human(request); context=capture_context(request,user)
        except Exception:
            return JSONResponse({'state':'unavailable'},status_code=403,headers={'Cache-Control':'private, no-store'})
        try:
            chunks=[]; size=0
            async for chunk in request.stream():
                size+=len(chunk)
                if size>100*1024: raise ValueError('body unavailable')
                chunks.append(chunk)
            def pairs(values):
                result={}
                for key,value in values:
                    if key in result: raise ValueError('duplicate')
                    result[key]=value
                return result
            body=json.loads(b''.join(chunks).decode('utf-8','strict'),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            importing=phase.startswith('import/')
            if importing:
                expected={'action_id','package_b64'}|(set() if phase=='import/preview' else {'review_sha256'})
                if type(body) is not dict or set(body)!=expected: raise ValueError('invalid request')
                if type(body['action_id']) is not str or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',body['action_id']): raise ValueError('invalid request')
                if type(body['package_b64']) is not str: raise ValueError('invalid request')
                raw=base64.b64decode(body['package_b64'],validate=True)
                if not 1<=len(raw)<=65536: raise ValueError('invalid request')
                if 'review_sha256' in body and (type(body['review_sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}',body['review_sha256'])): raise ValueError('invalid request')
            else:
                expected={'curated_id'} if phase=='preview' else {'curated_id','expected','review_sha256'}
                if type(body) is not dict or set(body)!=expected or type(body['curated_id']) is not str or not body['curated_id'] or len(body['curated_id'])>512: raise ValueError('invalid request')
            check=context['current_write'] if phase=='import/confirm' else context['current_live']
            coordinator=policy_coordinator()
            if coordinator is None or coordinator.projection is not getattr(request.app.state,'memory_policy_projection',None): raise ValueError('policy unavailable')
            async with context['barrier'].admitted(user):
                async def refs_current():
                    if not await owned(check): raise ValueError('authority changed')
                    refs=await owned(coordinator.checked_refs,user,context['account'],context['workspace'])
                    if coordinator is not policy_coordinator() or not await owned(check): raise ValueError('authority changed')
                    return refs
                refs=await refs_current()
                async def current():
                    try:return await refs_current()==refs
                    except Exception:return False
                if importing:
                    from services.memory import knowledge_snapshot_import
                    if batch_store is None: raise ValueError('import unavailable')
                    common={'owner':context['owner'],'account_id':context['account'],'workspace_id':context['workspace'],'action_id':body['action_id'],'raw':raw}
                    store=batch_store(request)
                    if phase=='import/preview':
                        value=await knowledge_snapshot_import.preview(store=store,**common)
                    else:
                        call=knowledge_snapshot_import.confirm if phase=='import/confirm' else knowledge_snapshot_import.status
                        value=await call(provider=provider,store=store,policy_projection=refs,current=current,review_sha256=body['review_sha256'],**common,**({'write_current':lambda: context['current_write']() is True} if phase=='import/confirm' else {}))
                    response=JSONResponse(value,headers={'Cache-Control':'private, no-store'})
                    if await current() is not True: raise ValueError('authority changed')
                    if check() is not True: raise ValueError('authority changed')
                    return response
                args={'provider':provider,'owner':context['owner'],'account_id':context['account'],'workspace_id':context['workspace'],'policy_projection':refs,'current':current,'curated_id':body['curated_id']}
                if phase=='preview':
                    value=await knowledge_snapshot_export.preview(**args)
                    response=JSONResponse(value,headers={'Cache-Control':'private, no-store'})
                else:
                    raw=await knowledge_snapshot_export.confirm(**args,expected=body['expected'],review_sha256=body['review_sha256'])
                    response=Response(raw,media_type='application/json',headers={'Cache-Control':'private, no-store'})
                if await current() is not True: raise ValueError('authority changed')
            if check() is not True: raise ValueError('authority changed')
            return response
        except Exception:
            return JSONResponse({'state':'unavailable'},status_code=503,headers={'Cache-Control':'private, no-store'})
    def endpoint_for(phase):
        async def endpoint(request: Request):return await execute(request,phase)
        return endpoint
    for phase in ('preview','confirm'):
        router.add_api_route('/knowledge-snapshot/export/'+phase,endpoint_for(phase),methods=['POST'])
    for phase in ('preview','confirm','status'):
        router.add_api_route('/knowledge-snapshot/import/'+phase,endpoint_for('import/'+phase),methods=['POST'])

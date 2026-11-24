import asyncio
from types import SimpleNamespace

from src.openclank.memory_lifecycle_work import consolidate_chat_now


def test_manual_consolidation_uses_current_s08_owner_and_never_claims_a_run(monkeypatch):
    from src.openclank import fm_journal_delta
    calls = []
    class Profile:
        def assert_current(self): calls.append('profile')
    profile = Profile()
    binding = SimpleNamespace(memory_owner='memory-alice', memory_workspace='workspace')
    lifecycle = SimpleNamespace(owner='alice', account_id='account', binding=binding,
        target_authority=SimpleNamespace(resolve=lambda account: binding if account == 'account' else None))
    assignment = SimpleNamespace(owner='alice')
    router = object()
    async def callback(state, got, selection, origins, actual_router, *, output_kind, journal):
        assert got is lifecycle and actual_router is router and output_kind == 'journal_synthesis'
        assert selection is None and origins == () and journal == 'qualified-journal'
        calls.append('existing-s08'); return True
    state = SimpleNamespace(s05_profile=profile, s08_rules_callback=callback)
    entry = (lifecycle, assignment, router)
    class Scheduler:
        _s05_controller = (profile, {'account': entry})
        _s05_registry_state = state
        def _owner_lifecycle_fenced(self, owner): return False
        async def _run_owner_aux_action(self, owner, action): return await action(owner=owner)
    scheduler = Scheduler()
    async def run_chat_pass(**kw):
        assert kw['profile'] is profile and kw['lifecycle'] is lifecycle and kw['account_id'] == 'account'
        assert await kw['run_job']('qualified-journal') is True
        return {'state':'acked'}
    monkeypatch.setattr(fm_journal_delta, 'run_chat_pass', run_chat_pass)
    async def run():
        result = await consolidate_chat_now(scheduler=scheduler,state=state,owner='alice',account_id='account',chat_id='chat',memory_owner='memory-alice',workspace_id='workspace',current=lambda:True)
        assert result == {'state':'acked','lastActualRun':{'state':'unavailable'}}
        assert calls == ['profile','existing-s08']
        calls.clear()
        assert (await consolidate_chat_now(scheduler=scheduler,state=state,owner='mallory',account_id='account',chat_id='chat',memory_owner='memory-alice',workspace_id='workspace',current=lambda:True))['state'] == 'unavailable'
        assert (await consolidate_chat_now(scheduler=scheduler,state=state,owner='alice',account_id='account',chat_id='chat',memory_owner='memory-alice',workspace_id='workspace',current=lambda:False))['state'] == 'unavailable'
        assert calls == []
    asyncio.run(run())


def test_manual_route_preserves_named_owner_and_s08_scope(monkeypatch):
    import httpx
    from fastapi import APIRouter, FastAPI
    from routes.memory.lifecycle_work import register_lifecycle_work
    from src.openclank import fm_journal_delta
    calls=[]
    class Barrier:
        def admitted(self,owner):
            class Scope:
                async def __aenter__(self): return None
                async def __aexit__(self,*args): return None
            assert owner == 'alice'; return Scope()
    profile=SimpleNamespace(assert_current=lambda: None)
    binding=SimpleNamespace(memory_owner='memory-alice',memory_workspace='workspace')
    lifecycle=SimpleNamespace(owner='alice',account_id='account',binding=binding,target_authority=SimpleNamespace(resolve=lambda _:binding))
    assignment=SimpleNamespace(owner='alice');router_value=object()
    async def callback(state,got,selection,origins,router,*,output_kind,journal): calls.append((got,journal)); return True
    app=FastAPI(); state=app.state; state.s05_profile=profile;state.s08_rules_callback=callback
    class Scheduler:
        _s05_controller=(profile,{'account':(lifecycle,assignment,router_value)})
        _s05_registry_state=state
        def _owner_lifecycle_fenced(self,owner): return False
        async def _run_owner_aux_action(self,owner,action): return await action(owner=owner)
    state.task_scheduler=Scheduler()
    context={'barrier':Barrier(),'current_write':lambda:True,'account':'account','owner':'memory-alice','workspace':'workspace'}
    async def owned(fn,*args): return fn(*args)
    async def pass_(**kw):
        assert kw['chat_id']=='chat'; assert await kw['run_job']('journal') is True
        return {'state':'acked'}
    monkeypatch.setattr(fm_journal_delta,'run_chat_pass',pass_)
    router=APIRouter();register_lifecycle_work(router,require_human=lambda _: 'alice',capture_context=lambda *_:context,owned=owned);app.include_router(router)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.post('/consolidate-now',json={'chat_id':'chat'})
            assert response.status_code==200 and response.json()=={'state':'acked','lastActualRun':{'state':'unavailable'}}
            assert response.headers['cache-control']=='private, no-store' and calls==[(lifecycle,'journal')]
            response=await client.post('/consolidate-now',json={'chat_id':'chat','owner':'mallory'})
            assert response.status_code==400 and len(calls)==1
    asyncio.run(run())


def test_retained_run_route_keeps_retained_scope_and_unknown(monkeypatch):
    import httpx
    from fastapi import APIRouter, FastAPI
    from routes.memory.lifecycle_work import register_lifecycle_work
    from src.openclank import chat_source_bridge, memory_policy_projection
    live={'value':True};seen=[]
    class Barrier:
        def admitted(self,owner):
            class Scope:
                async def __aenter__(self): return None
                async def __aexit__(self,*args): return None
            return Scope()
    class Provider:
        def _host_s08_policy_ready(self,kind): return kind=='summary'
        async def _host_s08_last_retained_run(self,request,*,current):
            seen.append(request)
            if not current(): return {'version':1,'state':'unavailable'}
            return {'version':1,'state':'available','coverage':'retained_history','lastVerifiedRetainedRun':{'settledAtMs':123,'outcome':'failed'}}
    class Coordinator:
        def checked_refs(self,user,account,workspace):
            assert (user,account,workspace)==('alice','account','workspace');return {'projection':'scoped'}
    provider=Provider();monkeypatch.setattr(chat_source_bridge,'canonical_source_provider',lambda _:provider)
    monkeypatch.setattr(memory_policy_projection,'policy_coordinator',lambda:Coordinator())
    context={'user':'alice','barrier':Barrier(),'current_write':lambda:live['value'],'account':'account','owner':'memory-alice','workspace':'workspace'}
    async def owned(fn,*args):return fn(*args)
    app=FastAPI();router=APIRouter();register_lifecycle_work(router,require_human=lambda _:'alice',capture_context=lambda *_:context,owned=owned);app.include_router(router)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.get('/consolidation-retained-run')
            assert response.json()['coverage']=='retained_history' and response.json()['lastVerifiedRetainedRun']['outcome']=='failed'
            assert seen[0]['account_id']=='account' and seen[0]['owner']=='memory-alice'
            live['value']=False
            response=await client.get('/consolidation-retained-run')
            assert response.status_code==503 and response.json()=={'version':1,'state':'unavailable'}
    asyncio.run(run())

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import pytest
from src.openclank.acp_bridge import ACPBridge
from src.openclank.operation_router import ManagedOperationRouter, PreparedManagedOperation


def test_injected_actor_lifetime_preserves_child_after_parent_finish(tmp_path, monkeypatch):
    from src.openclank.memory_job_admission import create_single_host_profile, qualify_single_host
    from src.openclank.server_manager import acquire_server_lifetime_guard, acquire_database_lifetime_guard
    from src.openclank.acp_supervisor_adapter import AcpSupervisorAdapter
    from src.openclank.mimo_supervisor import MimoSupervisorPool
    import src.model_dispatch as dispatch
    path = str(tmp_path / 'actor-occupancy.db')
    with sqlite3.connect(path) as db:
        db.executescript(Path('native/memery/crates/memery-core/src/store/current-schema.sql').read_text())
        db.executescript(Path('native/memery/crates/memery-core/src/store/schema-v17-s05-additions.sql').read_text())
    provider = SimpleNamespace(_local_db_path=lambda:path)
    guard = acquire_server_lifetime_guard(str(tmp_path),exclusive=True)
    database_guard = acquire_database_lifetime_guard(path,exclusive=True)
    profile = create_single_host_profile(guard,provider,database_guard)
    adapter = AcpSupervisorAdapter(MimoSupervisorPool(memory_provider=provider,data_dir=tmp_path,grant_store=object()))
    adapter._s05_profile=profile
    monkeypatch.setattr(dispatch,'_agent_supervisor',adapter)
    runtime = qualify_single_host(profile,adapter)
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._owner='alice'; bridge._memory_provider=None; bridge._managed_execution_lock=asyncio.Lock()
    bridge._s05_foreground_runtime=runtime
    bridge._managed_actor_foreground_connection=lambda identity,resolved:'conn'
    bridge._managed_actor_turns={}; bridge._managed_actor_executions={}; bridge._managed_finished_turns={}; bridge._managed_finished_executions=set(); bridge._prepared_managed_handoffs={}
    bridge._session_context={'session':{'root_operation_id':'op'}}; bridge._turns={'session':SimpleNamespace(raw_policy_ceiling=None)}
    async def resolve(session,root,actor,revision):
        return {'owner':'alice','holderID':'holder','sessionID':session,'rootSessionID':'session','rootOperationID':root,'actorID':actor,'parentActorID':'main' if actor=='child' else None,'executionRevision':revision,'lineage':[{'sessionID':'session','actorID':'main','executionRevision':1,'status':'running'}] if actor=='child' else [],'stableChatID':'chat','mappingRevision':1,'workspaceRevision':1}
    bridge.resolve_managed_actor_execution=resolve
    async def run():
        parent={'sessionID':'session','rootOperationID':'op','actorID':'main','executionRevision':1,'innerTurnID':'p'}
        child={**parent,'actorID':'child','innerTurnID':'c'}
        await bridge._handle_managed_turn_start(parent)
        await bridge._handle_managed_turn_start(child)
        bridge._managed_actor_turns.pop(('session','op','main',1,'p'))
        await bridge._handle_managed_execution_finish({k:v for k,v in parent.items() if k!='innerTurnID'})
        with sqlite3.connect(path) as db:
            assert sorted(row[0] for row in db.execute('SELECT state FROM fm_v2_s05_foreground')) == ['active','settled']
        bridge._managed_actor_turns.pop(('session','op','child',1,'c'))
        child2={**child,'innerTurnID':'c2'}
        await bridge._handle_managed_turn_start(child2)
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT count(*) FROM fm_v2_s05_foreground').fetchone()[0] == 2
        bridge._managed_actor_turns.pop(('session','op','child',1,'c2'))
        await bridge._handle_managed_execution_finish({k:v for k,v in child.items() if k!='innerTurnID'})
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM fm_v2_s05_foreground WHERE state='active'").fetchone()[0] == 0
    asyncio.run(run())
    profile.database_guard.close(); guard.close()


def test_shadow_router_and_actor_terminal_and_lost_result(tmp_path, monkeypatch):
    path = str(tmp_path / 'shadow.db')
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE fm_v2_s05_schema(version INTEGER); INSERT INTO fm_v2_s05_schema VALUES (1);
CREATE TABLE fm_v2_s05_occupancy(singleton INTEGER, verified INTEGER); INSERT INTO fm_v2_s05_occupancy VALUES(1,0);
CREATE TABLE fm_v2_s05_foreground(operation_id TEXT PRIMARY KEY,owner_id TEXT,connection_id TEXT,coverage_generation TEXT,epoch INTEGER,state TEXT);''')
    provider = SimpleNamespace(_local_db_path=lambda: path)
    import src.model_dispatch as dispatch
    monkeypatch.setattr(dispatch, 'get_agent_supervisor', lambda: SimpleNamespace(_memory_provider=provider))
    router = ManagedOperationRouter.__new__(ManagedOperationRouter)
    request = SimpleNamespace(root_operation_id='root',idempotency_key='idempotency-key-123')
    prepared = PreparedManagedOperation(request=request,owner='alice',operation='chat.complete',routes=(SimpleNamespace(connection_id='conn'),),payload={},router_token=id(router))
    async def good(*_): return {'value':1}
    router._execute_engine = good
    router.finalize = lambda prepared, raw: raw
    assert asyncio.run(router.execute_prepared(prepared)) == {'value':1}
    async def lost(*_): raise RuntimeError('lost')
    router._execute_engine = lost
    other = PreparedManagedOperation(request=SimpleNamespace(root_operation_id='other',idempotency_key='idempotency-key-456'),owner='alice',operation='chat.complete',routes=(),payload={},router_token=id(router))
    with pytest.raises(RuntimeError): asyncio.run(router.execute_prepared(other))
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._owner='alice'; bridge._memory_provider=provider; bridge._managed_execution_lock=asyncio.Lock()
    bridge._managed_actor_turns={}; bridge._managed_actor_executions={}; bridge._managed_finished_turns={}; bridge._managed_finished_executions=set(); bridge._prepared_managed_handoffs={}
    bridge._session_context={'session':{'root_operation_id':'op'}}; bridge._turns={'session':SimpleNamespace(raw_policy_ceiling=None)}
    async def resolve(*_):
        return {'owner':'alice','holderID':'holder','sessionID':'session','rootSessionID':'session','rootOperationID':'op','actorID':'main','parentActorID':None,'executionRevision':1,'lineage':[],'stableChatID':'chat','mappingRevision':1,'workspaceRevision':1}
    bridge.resolve_managed_actor_execution=resolve
    identity={'sessionID':'session','rootOperationID':'op','actorID':'main','executionRevision':1,'innerTurnID':'turn'}
    assert asyncio.run(bridge._handle_managed_turn_start(identity))['accepted']
    key=('session','op','main',1,'turn')
    bridge._managed_actor_turns.pop(key)
    assert asyncio.run(bridge._handle_managed_execution_finish({k:v for k,v in identity.items() if k!='innerTurnID'}))['accepted']
    with sqlite3.connect(path) as db:
        assert sorted(row[0] for row in db.execute('SELECT state FROM fm_v2_s05_foreground')) == ['settled','settled','unknown']
        assert db.execute('SELECT verified FROM fm_v2_s05_occupancy').fetchone()[0] == 0

    from src.openclank.memory_job_admission import observe_shadow
    with sqlite3.connect(path) as db:
        db.execute('UPDATE fm_v2_s05_occupancy SET verified=1')
    assert not observe_shadow(provider, operation_id='unverified', owner='alice', connection_id='unknown', generation='shadow', event='start')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT verified FROM fm_v2_s05_occupancy').fetchone()[0] == 0

import sqlite3
from src.openclank import host_source_schema
from pathlib import Path
from types import SimpleNamespace
import pytest
from src.memery_external_sources import SourceRef
from src.openclank.memory_job_admission import Evidence, Limits, TargetAuthority, enqueue, _reserve as reserve, _foreground_enter as foreground_enter, _foreground_leave as foreground_leave, _commit_dispatch as commit_dispatch, settle


def _create_s05_schema(db):
    store = Path(__file__).resolve().parents[1] / 'native/memery/crates/memery-core/src/store'
    for name in ('current-schema.sql', 'schema-v17-s05-additions.sql', 'schema-v17-s05-account-v4.sql'):
        db.executescript((store / name).read_text())


def _retained_reservation(path, job_id):
    """Reconstruct a previously persisted lease; new reservations are gated on shared quota."""
    with sqlite3.connect(path) as db:
        owner, attempts, connection = db.execute(
            "SELECT j.owner_id,j.attempt_count,r.connection_id FROM fm_v2_jobs j "
            "JOIN fm_v2_s05_job_refs r USING(owner_id,job_id) WHERE job_id=?", (job_id,)
        ).fetchone()
        coverage, clock, foreground_epoch = db.execute(
            "SELECT coverage_generation,clock_generation,foreground_epoch FROM fm_v2_s05_occupancy"
        ).fetchone()
        epoch = attempts + 1
        attempt = f's05_fixture_{epoch}'
        now = '2026-01-01T00:00:00+00:00'
        db.execute("UPDATE fm_v2_jobs SET state='queued' WHERE owner_id=? AND job_id=? AND state='retry_wait'", (owner, job_id))
        db.execute("INSERT INTO fm_v2_attempts(owner_id,attempt_id,job_id,attempt_number,state,lease_epoch,"
                   "lease_owner,heartbeat_at,created_at,updated_at) VALUES (?,?,?,?,'leased',?,'worker',?,?,?)",
                   (owner, attempt, job_id, epoch, epoch, now, now, now))
        db.execute("INSERT INTO fm_v2_s05_reservations VALUES (?,?,?,?,?,?,?,?,'reserved',100000,NULL,"
                   "1,30,10,100,1,30,10,100)", (owner, attempt, job_id, epoch, connection, coverage, clock, foreground_epoch))
        db.execute("UPDATE fm_v2_jobs SET state='active',current_attempt_id=?,attempt_count=? WHERE owner_id=? AND job_id=?",
                   (attempt, epoch, owner, job_id))
    return {'attempt_id': attempt, 'lease_epoch': epoch}


def _qualified_test_profile(path, tmp_path, monkeypatch):
    import src.model_dispatch as dispatch
    from src.openclank.server_manager import acquire_server_lifetime_guard, acquire_database_lifetime_guard
    from src.openclank.acp_supervisor_adapter import AcpSupervisorAdapter
    from src.openclank.mimo_supervisor import MimoSupervisorPool
    from src.openclank.memory_job_admission import create_single_host_profile, qualify_single_host
    provider = SimpleNamespace(_local_db_path=lambda:path)
    guard = acquire_server_lifetime_guard(str(tmp_path),exclusive=True)
    database_guard = acquire_database_lifetime_guard(path,exclusive=True)
    profile = create_single_host_profile(guard,provider,database_guard)
    pool = MimoSupervisorPool(memory_provider=provider,data_dir=tmp_path,grant_store=object())
    adapter = AcpSupervisorAdapter(pool); adapter._s05_profile = profile
    monkeypatch.setattr(dispatch,'_agent_supervisor',adapter)
    runtime = qualify_single_host(profile,adapter)
    return guard,profile,runtime


def test_completed_turn_output_is_closed_and_bounded():
    from src.openclank.memory_job_admission import parse_completed_turn_output
    assert parse_completed_turn_output('{"version":1,"kind":"no_finding"}').kind == 'no_finding'
    assert parse_completed_turn_output('{"version":1,"kind":"candidate","content":"A fact"}').content == 'A fact'
    for raw in ('{"version":1,"kind":"no_finding","content":"x"}',
                '{"version":1,"kind":"candidate","content":" x"}',
                '{"version":1,"version":1,"kind":"no_finding"}',
                '{"version":true,"kind":"no_finding"}',
                '{"version":1,"kind":"candidate","content":"' + 'x'*4097 + '"}'):
        with pytest.raises(ValueError): parse_completed_turn_output(raw)


def test_database_lifetime_guard_unifies_symlink_aliases(tmp_path):
    from src.openclank.server_manager import acquire_database_lifetime_guard, ServerManagerError
    from src.openclank.memory_job_admission import create_single_host_profile
    database = tmp_path / 'memory.db'; database.write_bytes(b'')
    alias_dir = tmp_path / 'other-install'; alias_dir.mkdir()
    alias = alias_dir / 'alias.db'; alias.symlink_to(database)
    shared = acquire_database_lifetime_guard(database,exclusive=False)
    with pytest.raises(ServerManagerError): acquire_database_lifetime_guard(alias,exclusive=True)
    shared.close()
    exclusive = acquire_database_lifetime_guard(alias,exclusive=True)
    with pytest.raises(ServerManagerError): acquire_database_lifetime_guard(database,exclusive=False)
    with pytest.raises(ValueError):
        create_single_host_profile(exclusive,SimpleNamespace(_local_db_path=lambda:str(database)),exclusive)
    exclusive.close()
    hardlink = tmp_path / 'hardlink.db'; hardlink.hardlink_to(database)
    with pytest.raises(ServerManagerError): acquire_database_lifetime_guard(database,exclusive=True)


def test_injected_foreground_runtime_keeps_interrupted_operation_busy(tmp_path, monkeypatch):
    path = str(tmp_path / 'runtime.db')
    with sqlite3.connect(path) as db:
        _create_s05_schema(db)
    guard,profile,runtime = _qualified_test_profile(path,tmp_path,monkeypatch)
    with sqlite3.connect(path) as db: baseline=db.execute('SELECT last_quiet_tick FROM fm_v2_s05_occupancy').fetchone()[0]
    interrupted = runtime.enter(owner='alice', account_id='account-123', connection_id='connection')
    finished = runtime.enter(owner='alice', account_id='account-123', connection_id='connection')
    runtime.leave(finished)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT state FROM fm_v2_s05_foreground WHERE operation_id=?',(interrupted[4],)).fetchone() == ('active',)
        assert db.execute('SELECT last_quiet_tick FROM fm_v2_s05_occupancy').fetchone() == (baseline,)
    profile.database_guard.close(); guard.close()


def test_unverified_bootstrap_retains_unknown_after_restart(tmp_path, monkeypatch):
    from src.openclank.memory_job_admission import bootstrap_unverified, create_single_host_profile, qualify_single_host
    from src.openclank.server_manager import acquire_server_lifetime_guard, acquire_database_lifetime_guard
    from src.openclank.acp_supervisor_adapter import AcpSupervisorAdapter
    from src.openclank.mimo_supervisor import MimoSupervisorPool
    import src.model_dispatch as dispatch
    path = str(tmp_path / 'bootstrap.db')
    with sqlite3.connect(path) as db:
        _create_s05_schema(db)
        db.execute("INSERT INTO fm_v2_s05_occupancy VALUES (1,'old-coverage','old-clock',1,7,30)")
        db.execute("INSERT INTO fm_v2_s05_foreground VALUES ('lost','alice','connection','old-coverage',7,'unknown',NULL)")
    provider = SimpleNamespace(_local_db_path=lambda:path)
    first = bootstrap_unverified(provider)
    second = bootstrap_unverified(provider)
    assert first['state'] == second['state'] == 'unverified'
    assert first['coverage_generation'] != second['coverage_generation'] and first['clock_generation'] != second['clock_generation']
    assert second['unsettled_foreground'] == 1
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT verified,foreground_epoch FROM fm_v2_s05_occupancy').fetchone() == (0,7)
        assert db.execute("SELECT state FROM fm_v2_s05_foreground WHERE operation_id='lost'").fetchone() == ('unknown',)
    guard = acquire_server_lifetime_guard(str(tmp_path),exclusive=True)
    database_guard = acquire_database_lifetime_guard(path,exclusive=True)
    profile = create_single_host_profile(guard,provider,database_guard)
    adapter = AcpSupervisorAdapter(MimoSupervisorPool(memory_provider=provider,data_dir=tmp_path,grant_store=object()))
    adapter._s05_profile = profile
    monkeypatch.setattr(dispatch,'_agent_supervisor',adapter)
    with pytest.raises(ValueError,match='unresolved occupancy'):
        qualify_single_host(profile,adapter)
    assert adapter._s05_foreground_runtime is None
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT verified FROM fm_v2_s05_occupancy').fetchone() == (0,)
        assert db.execute("SELECT state FROM fm_v2_s05_foreground WHERE operation_id='lost'").fetchone() == ('unknown',)
    profile.database_guard.close(); guard.close()
    clean = str(tmp_path / 'clean.db')
    with sqlite3.connect(clean) as db:
        _create_s05_schema(db)
    guard,profile,runtime = _qualified_test_profile(clean,tmp_path,monkeypatch)
    assert dispatch._agent_supervisor._s05_foreground_runtime is runtime
    handle = runtime.enter(owner='alice', account_id='account-123',connection_id='connection')
    runtime.leave(handle)
    with sqlite3.connect(clean) as db:
        assert db.execute('SELECT verified FROM fm_v2_s05_occupancy').fetchone() == (1,)
    profile.database_guard.close(); guard.close()
    with pytest.raises(Exception): runtime.enter(owner='alice', account_id='account-123',connection_id='connection')


def test_foreground_revokes_reserved_and_dispatched_stays_occupied(tmp_path):
    path = str(tmp_path / 'v2.db')
    with sqlite3.connect(path) as db:
        _create_s05_schema(db)
        db.execute("INSERT INTO fm_v2_s05_occupancy VALUES (1,'coverage','clock',1,0,0)")
    limits = Limits(5,20,60,16384,30,4096,100,500,1)
    source = SourceRef(1,'host_user_body','account-123','c','main','m','body',1,'a'*64,'files-workspace',None)
    auth = SimpleNamespace(username_for_account_id=lambda account: 'alice' if account == 'account-123' else None,
                           account_id=lambda owner: 'account-123' if owner == 'alice' else None)
    supervisor = SimpleNamespace(_owner_lifecycle_epoch=lambda owner: 4, _durable_owner_epoch=lambda owner: '4',
                                 _assert_owner_lifecycle=lambda owner, epoch: None)
    target = TargetAuthority(auth_manager=auth, supervisor=supervisor)
    evidence = Evidence(source,'admission','b'*64,'c'*64,'connection','route')
    job = enqueue(path, target_authority=target, idempotency_key='key', evidence=evidence, limits=limits, now_ms=100000)
    assert not job['replayed']
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT owner_id,workspace_key FROM fm_v2_jobs WHERE job_id=?",(job['job_id'],)).fetchone() == ('alice','global')
        assert db.execute("SELECT source_account_id,source_workspace_id,target_workspace_id FROM fm_v2_s05_job_refs WHERE job_id=?",(job['job_id'],)).fetchone() == ('account-123','files-workspace','global')
    renamed = TargetAuthority(auth_manager=SimpleNamespace(username_for_account_id=lambda _: 'bob', account_id=lambda _: 'account-123'), supervisor=supervisor)
    with pytest.raises(ValueError, match='source target binding changed'):
        enqueue(path, target_authority=renamed, idempotency_key='key', evidence=evidence, limits=limits, now_ms=100000)
    wrong = TargetAuthority(auth_manager=SimpleNamespace(username_for_account_id=lambda _: 'alice', account_id=lambda _: 'other'), supervisor=supervisor)
    with pytest.raises(ValueError, match='account authority unavailable'):
        enqueue(path, target_authority=wrong, idempotency_key='other', evidence=evidence, limits=limits, now_ms=100000)
    assert enqueue(path, target_authority=target, idempotency_key='key', evidence=evidence, limits=limits, now_ms=100000)['replayed']
    from dataclasses import replace
    with pytest.raises(ValueError):
        enqueue(path, target_authority=target, idempotency_key='key', evidence=replace(evidence, connection_id='other'), limits=limits, now_ms=100000)
    args = dict(owner='alice',job_id=job['job_id'],worker_id='worker',coverage_generation='coverage',clock_generation='clock',now_tick=6000,now_ms=100000,input_token_bound=10,policy_fingerprint='b'*64,authority_fingerprint='c'*64,limits=limits)
    from src.memery_v2 import claim_job, V2OperationError
    with pytest.raises(V2OperationError) as rejected:
        claim_job(owner='alice', job_id=job['job_id'], worker_id='legacy', db_path=path)
    assert rejected.value.code == 'unsupported_contract'
    with sqlite3.connect(path) as raw:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("UPDATE fm_v2_jobs SET state='active',current_attempt_id='fake' WHERE job_id=?",(job['job_id'],))
    missing = tmp_path / 'not-created.db'
    with pytest.raises(sqlite3.OperationalError):
        reserve(str(missing), **args)
    assert not missing.exists()
    with pytest.raises(ValueError, match='shared quota owner unavailable'):
        reserve(path, **args)
    first = _retained_reservation(path, job['job_id'])
    foreground_enter(path, owner='alice', account_id='account-123', operation_id='fg', connection_id='connection', coverage_generation='coverage')
    with pytest.raises(ValueError):
        commit_dispatch(path, owner='alice', attempt_id=first['attempt_id'], lease_epoch=first['lease_epoch'], coverage_generation='coverage',clock_generation='clock',policy_fingerprint='b'*64,authority_fingerprint='c'*64)
    foreground_leave(path, owner='alice', account_id='account-123', connection_id='connection', operation_id='fg', coverage_generation='coverage',clock_generation='clock',now_tick=7000)
    second = _retained_reservation(path, job['job_id'])
    commit_dispatch(path, owner='alice', attempt_id=second['attempt_id'], lease_epoch=second['lease_epoch'], coverage_generation='coverage',clock_generation='clock',policy_fingerprint='b'*64,authority_fingerprint='c'*64)
    foreground_enter(path, owner='alice', account_id='account-123', operation_id='fg2', connection_id='connection', coverage_generation='coverage')
    with sqlite3.connect(path) as raw:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("DELETE FROM fm_v2_s05_reservations WHERE attempt_id=?",(second['attempt_id'],))
    assert settle(path, owner='alice',attempt_id=second['attempt_id'],lease_epoch=second['lease_epoch'],outcome='unknown') == 'uncertain'
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM fm_v2_s05_reservations WHERE attempt_id=?", (second['attempt_id'],)).fetchone()[0] == 'uncertain'

    assert settle(path, owner='alice',attempt_id=second['attempt_id'],lease_epoch=second['lease_epoch'],outcome='failed_retryable',usage=(2,10,4),now_ms=102000) == 'retry_wait'
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM fm_v2_jobs WHERE job_id=?", (job['job_id'],)).fetchone()[0] == 'retry_wait'
        assert db.execute("SELECT charged_input_tokens,charged_output_tokens FROM fm_v2_s05_reservations WHERE attempt_id=?", (second['attempt_id'],)).fetchone() == (10,4)


def test_assignment_snapshot_is_exact_and_idempotent(tmp_path, monkeypatch):
    from src.memery_external_sources import CurrentHostUserSource
    from src.openclank.memory_job_admission import AssignmentAuthority
    from src.openclank.memory_policy import MemoryPolicyStore, _fingerprint
    from src.openclank import chat_routing
    path = str(tmp_path / 'inactive.db')
    with sqlite3.connect(path) as db:
        _create_s05_schema(db)
        db.execute("INSERT INTO fm_v2_s05_occupancy VALUES (1,'coverage','clock',1,0,0)")
    source = SourceRef(1,'host_user_body','account-123','c','main','m','body',1,'a'*64,'files-workspace',None)
    auth = SimpleNamespace(username_for_account_id=lambda account: 'alice' if account == 'account-123' else None,
                           account_id=lambda owner: 'account-123' if owner == 'alice' else None,
                           get_privileges=lambda owner:{'can_manage_memory':True})
    supervisor = SimpleNamespace(_owner_lifecycle_epoch=lambda owner: 4, _durable_owner_epoch=lambda owner:'4',
        _assert_owner_lifecycle=lambda owner,epoch: None)
    target = TargetAuthority(auth_manager=auth, supervisor=supervisor)
    policy = MemoryPolicyStore(graph_provider=lambda:{'state':'unavailable'})
    monkeypatch.setattr(policy, 's05_base_policy_snapshot', lambda *args,**kwargs:{'inputs':{'eligibility_state':'available','capture_allowed':True},'fingerprint':'b'*64})
    selected = {'version':1,'modelRouteID':'route','connectionID':'connection','modelID':'model','grantID':None,
                'optionRouteID':'option','catalogRevision':2,'options':{'reasoning_effort':'0'}}
    entry = {'version':1,'kind':'interpretation','revision':1,'selection':selected}
    entry['fingerprint'] = _fingerprint(entry)
    monkeypatch.setattr(policy, 'work_kind_snapshot', lambda owner,kind: dict(entry))
    monkeypatch.setattr(chat_routing, 'resolve_chat_route', lambda **kwargs: SimpleNamespace(connection_id='connection',provider_grant_id=None,
        operations=('chat.complete',),option_route_id='option',catalog_revision=2,capabilities={'reasoning_effort':['0','none']}))
    provider = CurrentHostUserSource.__new__(CurrentHostUserSource); provider._owner='alice'
    monkeypatch.setattr(CurrentHostUserSource, 'hydrate', lambda self,ref:{'state':'available'})
    authority = AssignmentAuthority(owner='alice',policy_store=policy,source_authority=provider)
    assignment = authority.resolve('interpretation',source)
    assert assignment['options'] == {'reasoning_effort':'0'}
    evidence = Evidence(source,'admission','b'*64,'c'*64,'connection','route',assignment)
    limits = Limits(5,20,60,16384,30,4096,100,500,1)
    wrong_route = Evidence(source,'admission','b'*64,'c'*64,'other','route',assignment)
    with pytest.raises(ValueError, match='contention route'):
        enqueue(path,target_authority=target,idempotency_key='wrong',evidence=wrong_route,limits=limits,now_ms=100000,assignment_authority=authority)
    job = enqueue(path,target_authority=target,idempotency_key='one',evidence=evidence,limits=limits,now_ms=100000,assignment_authority=authority)
    assert enqueue(path,target_authority=target,idempotency_key='one',evidence=evidence,limits=limits,now_ms=100000,assignment_authority=authority)['replayed']
    with sqlite3.connect(path) as db:
        import json
        assert json.loads(db.execute('SELECT assignment_json FROM fm_v2_s05_job_refs WHERE job_id=?',(job['job_id'],)).fetchone()[0]) == assignment
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE fm_v2_s05_job_refs SET connection_id=? WHERE job_id=?',('other',job['job_id']))
    from src.openclank.memory_job_admission import prepare_assignment_operation
    from src.openclank.operation_router import ManagedOperationRouter
    class FakeRouter(ManagedOperationRouter):
        def __init__(self): self.requests = []
        def prepare(self, request): self.requests.append(request); return request
    router = FakeRouter()
    lease = _retained_reservation(path, job['job_id'])
    input_value = {'messages':[{'role':'user','content':'ephemeral'}]}
    prepared = prepare_assignment_operation(path,owner='alice',job_id=job['job_id'],attempt_id=lease['attempt_id'],lease_epoch=lease['lease_epoch'],target_authority=target,assignment_authority=authority,router=router,operation_input=input_value)
    input_value['messages'][0]['content'] = 'changed'
    assert prepared.input['messages'][0]['content'] == 'ephemeral'
    assert prepared.model_route_id == 'route' and prepared.grant_id is None
    assert prepared.options['modelRequestOptions']['options'] == {'reasoning_effort':'0'}
    assert len(router.requests) == 1
    entry['revision'] += 1; entry['fingerprint'] = _fingerprint({k:v for k,v in entry.items() if k != 'fingerprint'})
    with pytest.raises(ValueError, match='assignment authority changed'):
        prepare_assignment_operation(path,owner='alice',job_id=job['job_id'],attempt_id=lease['attempt_id'],lease_epoch=lease['lease_epoch'],target_authority=target,assignment_authority=authority,router=router,operation_input={'messages':[{'role':'user','content':'x'}]})
    assert len(router.requests) == 1
    with pytest.raises(ValueError, match='assignment authority changed'):
        enqueue(path,target_authority=target,idempotency_key='one',evidence=evidence,limits=limits,now_ms=100000,assignment_authority=authority)
    entry['selection'] = None
    with pytest.raises(ValueError, match='unanchored'):
        authority.resolve('interpretation')
    receipt = {'rootOperationID':'m','actorID':'main','sessionID':'engine','executionRevision':3,
               'bindingID':'binding','bindingRevision':5,'connectionID':'connection','modelID':'model',
               'modelRouteID':'route','grantID':'grant-original',
               'modelRequestOptions':{'routeID':'route','model':{'providerID':'connection','modelID':'model'},
                   'optionRouteID':'option','catalogRevision':2,'options':{'reasoning_effort':'none'}}}
    monkeypatch.setattr(CurrentHostUserSource, 'execution_receipt', lambda self,ref:{'state':'available','receipt':receipt})
    monkeypatch.setattr(chat_routing, 'resolve_chat_route', lambda **kwargs: SimpleNamespace(connection_id='connection',provider_grant_id='grant-original',
        operations=('chat.complete',),option_route_id='option',catalog_revision=2,capabilities={'reasoning_effort':['0','none']}))
    inherited = authority.resolve('interpretation',source)
    assert inherited['grantID'] == 'grant-original' and inherited['options'] == {'reasoning_effort':'none'}
    assert inherited['source']['bindingID'] == 'binding' and inherited['source']['source']['content_hash'] == 'a'*64
    monkeypatch.setattr(chat_routing, 'resolve_chat_route', lambda **kwargs: (_ for _ in ()).throw(ValueError('grant revoked')))
    with pytest.raises(ValueError, match='unavailable'):
        authority.resolve('interpretation',source)
    # Base policy fingerprint ignores work-kind changes but not policy inputs.
    import threading
    from routes import prefs_routes
    monkeypatch.setattr(prefs_routes, 'PREFERENCE_LOCK', threading.RLock())
    base_store = MemoryPolicyStore(graph_provider=lambda:{'state':'unavailable'})
    effective = {'owner':'alice','source':'legacy_compatibility','policy_state':'accepted','choices':{},'privileges':{},
        'legacy_inputs':{},'eligibility_state':'available','eligibility_reason':None,'unsupported_inputs':[],
        'read_allowed':True,'capture_allowed':True,'capture_mode':'auto','direct_write_allowed':True}
    from src.openclank.memory_policy import SCHEMA_V2, _empty_assignments
    document = {'schema':SCHEMA_V2, 'revision':1, 'accepted_policy':None, 'workKindAssignments':_empty_assignments()}
    document['fingerprint'] = _fingerprint(document)
    monkeypatch.setattr(base_store, 'effective_snapshot', lambda *args,**kwargs:effective)
    monkeypatch.setattr(base_store, 'snapshot', lambda *args,**kwargs:{'document':document})
    fingerprint = base_store.s05_base_policy_snapshot('alice',read_privilege=True,write_privilege=True)['fingerprint']
    document['workKindAssignments']['synthesis']['revision'] = 1
    document['fingerprint'] = _fingerprint({k:v for k,v in document.items() if k != 'fingerprint'})
    assert base_store.s05_base_policy_snapshot('alice',read_privilege=True,write_privilege=True)['fingerprint'] == fingerprint
    effective['capture_allowed'] = False
    assert base_store.s05_base_policy_snapshot('alice',read_privilege=True,write_privilege=True)['fingerprint'] != fingerprint


def test_completed_turn_staging_exact_source_and_assignment(tmp_path, monkeypatch):
    import hashlib, json
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    import src.memery_external_sources as external
    from src.openclank.chat_source_bridge import HostOriginAdmission, enqueue_new, deliver, complete_user_origin
    from src.openclank.conversation_archive import ConversationArchive
    from src.openclank.memory_job_admission import AssignmentAuthority, stage_completed_turn, stage_completed_turn_page, refresh_pending_completed_turn, reserve_completed_turn, prepare_completed_turn
    from src.openclank.memory_policy import MemoryPolicyStore, _fingerprint
    from src.openclank import chat_routing
    from src.memery_v2 import claim_job, V2OperationError
    from src.openclank.server_manager import acquire_server_lifetime_guard, ServerManagerError
    import os
    if os.name == 'posix':
        shared = acquire_server_lifetime_guard(str(tmp_path),exclusive=False)
        other = acquire_server_lifetime_guard(str(tmp_path),exclusive=False)
        with pytest.raises(ServerManagerError): acquire_server_lifetime_guard(str(tmp_path),exclusive=True)
        shared.close(); other.close()
        exclusive = acquire_server_lifetime_guard(str(tmp_path),exclusive=True)
        with pytest.raises(ServerManagerError): acquire_server_lifetime_guard(str(tmp_path),exclusive=False)
        exclusive.assert_held(); exclusive.close()
    host = tmp_path / 'host.db'; jobs = str(tmp_path / 'jobs.db')
    with sqlite3.connect(host) as db:
        db.executescript('CREATE TABLE sessions(id TEXT PRIMARY KEY,owner TEXT,workspace_id TEXT); CREATE TABLE chat_messages(id TEXT PRIMARY KEY,session_id TEXT,content TEXT,role TEXT,timestamp TEXT); INSERT INTO sessions VALUES ("c","alice","w"); INSERT INTO chat_messages VALUES ("root","c","body","user","2026-01-01T00:00:00"),("answer","c","ok","assistant","2026-01-01T00:00:01"),("root2","c","body2","user","2026-01-01T00:00:02"),("answer2","c","ok","assistant","2026-01-01T00:00:03");')
    host_source_schema.convert(str(host))
    host_source_schema.upgrade_origin_producer(str(host))
    archive = ConversationArchive(str(tmp_path / 'archive.db'), enable_origin_policy_for_disposable_test=True)
    factory = sessionmaker(bind=create_engine(f'sqlite:///{host}'))
    origin = HostOriginAdmission('alice','c','root','{"incognito":false,"noMemory":false,"compareMode":false}','a'*64,True)
    with factory() as db:
        enqueue_new(db,archive=archive,owner='alice',chat_id='c',message_id='root',body='body',origin=origin); db.commit()
        deliver(db,archive,owner='alice',chat_id='c',message_id='root',revision=1)
        assert complete_user_origin(db,archive,owner='alice',chat_id='c',root_operation_id='root',assistant_message_id='answer')
        deliver(db,archive,owner='alice',chat_id='c',message_id='root',revision=1)
        origin2 = HostOriginAdmission('alice','c','root2','{"incognito":false,"noMemory":false,"compareMode":false}','a'*64,True)
        enqueue_new(db,archive=archive,owner='alice',chat_id='c',message_id='root2',body='body2',origin=origin2); db.commit()
        deliver(db,archive,owner='alice',chat_id='c',message_id='root2',revision=1)
        assert complete_user_origin(db,archive,owner='alice',chat_id='c',root_operation_id='root2',assistant_message_id='answer2')
        deliver(db,archive,owner='alice',chat_id='c',message_id='root2',revision=1)
    with sqlite3.connect(jobs) as db:
        _create_s05_schema(db)
    auth=SimpleNamespace(account_id=lambda owner:'alice' if owner=='alice' else None,username_for_account_id=lambda account:'alice' if account=='alice' else None,get_privileges=lambda owner:{'can_manage_memory':True})
    supervisor=SimpleNamespace(_owner_lifecycle_epoch=lambda owner:1,_durable_owner_epoch=lambda owner:'1',_assert_owner_lifecycle=lambda owner,epoch:None)
    target=TargetAuthority(auth_manager=auth,supervisor=supervisor)
    policy=MemoryPolicyStore(graph_provider=lambda:{'state':'unavailable'})
    monkeypatch.setattr(policy,'effective_snapshot',lambda *args,**kwargs:{'eligibility_state':'available','capture_allowed':True})
    monkeypatch.setattr(policy,'s05_base_policy_snapshot',lambda *args,**kwargs:{'inputs':{'eligibility_state':'available','capture_allowed':True},'fingerprint':'b'*64})
    selection={'version':1,'modelRouteID':'route','connectionID':'connection','modelID':'model','grantID':None,'optionRouteID':'option','catalogRevision':1,'options':{}}
    entry={'version':1,'kind':'interpretation','revision':1,'selection':selection}; entry['fingerprint']=_fingerprint(entry)
    monkeypatch.setattr(policy,'work_kind_snapshot',lambda owner,kind:dict(entry))
    monkeypatch.setattr(chat_routing,'resolve_chat_route',lambda **kwargs:SimpleNamespace(connection_id='connection',provider_grant_id=None,operations=('chat.complete',),option_route_id='option',catalog_revision=1,capabilities={}))
    monkeypatch.setattr(external,'resolve_owned_workspace',lambda *args,**kwargs:SimpleNamespace(workspace=SimpleNamespace(id='w',owner_subject_id='alice',revision=1,generation=1)))
    reader=external.CurrentHostUserSource(owner='alice',auth_manager=auth,session_factory=factory,archive=archive,workspace_repository=object(),policy_store=policy)
    authority=AssignmentAuthority(owner='alice',policy_store=policy,source_authority=reader)
    ref=external.SourceRef(1,'host_user_body','alice','c','main','root','body',1,hashlib.sha256(b'body').hexdigest(),'w',None)
    limits=Limits(5,20,60,16384,30,4096,100,500,1)
    first=stage_completed_turn(jobs,source=ref,target_authority=target,assignment_authority=authority,limits=limits,now_ms=100000)
    assert stage_completed_turn(jobs,source=ref,target_authority=target,assignment_authority=authority,limits=limits,now_ms=100000)['replayed']
    from src.memery_provider import MemeryProvider
    provider = MemeryProvider.__new__(MemeryProvider)
    provider._host_worker=True; provider._guarded_v17=True; provider._broker_url=None; provider._workspace_id='global'; provider._env={}; provider._fm_db_path=jobs
    refs, next_after = provider._host_source_refs(account_id='alice',chat_id='c',message_id='root',target_authority=target)
    assert next_after is None and len(refs) == 1 and refs[0].job_id == first['job_id'] and refs[0].source == ref
    authorize = lambda owner, chat: (owner, chat) == ('alice', 'c')
    page = archive.read_completed_turn_events(owner='alice', chat_id='c', authorize=authorize)
    assert len(page['events']) == 2 and page['blocked'] is None
    with pytest.raises(ValueError):
        archive.ack_completed_turn_events(owner='alice', chat_id='c', authorize=authorize, expected_cursor=page['cursor'], events=[page['events'][1]])
    with sqlite3.connect(archive.db_path) as db:
        db.execute("UPDATE conversation_parts SET tombstone=1 WHERE owner='alice' AND chat_id='c' AND message_id='root2'")
    blocked_page = archive.read_completed_turn_events(owner='alice',chat_id='c',authorize=authorize)
    assert blocked_page['events'] == page['events'][:1] and blocked_page['blocked']['seq'] == page['events'][1]['seq']
    delivered = stage_completed_turn_page(jobs,owner='alice',chat_id='c',target_authority=target,assignment_authority=authority,limits=limits,now_ms=100000)
    assert delivered == {'cursor':page['events'][0]['seq'], 'staged':1, 'terminal_erased':0, 'stopped':'source_unavailable'}
    assert archive.ack_completed_turn_events(owner='alice',chat_id='c',authorize=authorize,expected_cursor=page['cursor'],events=page['events'][:1])['replayed']
    with pytest.raises(ValueError):
        archive.ack_completed_turn_events(owner='alice',chat_id='c',authorize=lambda *_:False,expected_cursor=page['cursor'],events=page['events'])
    with sqlite3.connect(jobs) as db:
        kind, coverage = db.execute('SELECT j.kind,r.coverage_json FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r USING(owner_id,job_id) WHERE j.job_id=?',(first['job_id'],)).fetchone()
        assert kind == 'completed_turn_review' and json.loads(coverage) == {'version':1,'input':'host_user_body_only','assistant':'unsupported','native':'unsupported','queued':'unsupported'}
    with pytest.raises(V2OperationError): claim_job(owner='alice',job_id=first['job_id'],worker_id='legacy',db_path=jobs)
    entry['revision']=2; entry['fingerprint']=_fingerprint({k:v for k,v in entry.items() if k!='fingerprint'})
    deferred=stage_completed_turn(jobs,source=ref,target_authority=target,assignment_authority=authority,limits=limits,now_ms=100000)
    assert deferred['job_id']==first['job_id'] and deferred['reason']=='assignment_refresh_required'
    with sqlite3.connect(jobs) as db:
        old_hash, old_policy, old_authority, old_assignment = db.execute("SELECT j.input_hash,r.policy_fingerprint,r.authority_fingerprint,r.assignment_json FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r USING(owner_id,job_id) WHERE j.job_id=?",(first['job_id'],)).fetchone()
    refresh_args=dict(owner='alice',job_id=first['job_id'],source=ref,target_authority=target,assignment_authority=authority,
        expected_input_hash=old_hash,expected_policy_fingerprint=old_policy,expected_authority_fingerprint=old_authority,
        expected_assignment_fingerprint=json.loads(old_assignment)['assignmentFingerprint'])
    refreshed=refresh_pending_completed_turn(jobs,**refresh_args)
    assert refreshed['refreshed'] and refreshed['job_id']==first['job_id']
    with pytest.raises(ValueError,match='changed'):
        refresh_pending_completed_turn(jobs,**refresh_args)
    fresh_args={**refresh_args,'expected_input_hash':refreshed['input_hash'], 'expected_assignment_fingerprint':entry['fingerprint']}
    assert not refresh_pending_completed_turn(jobs,**fresh_args)['refreshed']
    with sqlite3.connect(jobs) as db: db.execute("UPDATE fm_v2_jobs SET attempt_count=1 WHERE job_id=?",(first['job_id'],))
    with pytest.raises(ValueError,match='changed'):
        refresh_pending_completed_turn(jobs,**fresh_args)
    with sqlite3.connect(jobs) as db:
        db.execute("UPDATE fm_v2_jobs SET attempt_count=0 WHERE job_id=?",(first['job_id'],))
    guard,profile,runtime = _qualified_test_profile(jobs,tmp_path,monkeypatch)
    with sqlite3.connect(jobs) as db: db.execute('UPDATE fm_v2_s05_occupancy SET last_quiet_tick=0')
    args=dict(source=ref,target_authority=target,assignment_authority=authority,owner='alice',job_id=first['job_id'],worker_id='worker',coverage_generation=profile.coverage_generation,clock_generation=profile.clock_generation,now_tick=6000,now_ms=100000,input_token_bound=10,limits=limits,profile=profile)
    entry['revision'] += 1; entry['fingerprint'] = _fingerprint({k:v for k,v in entry.items() if k != 'fingerprint'})
    assert reserve_completed_turn(jobs,**args)['reason'] == 'assignment_refresh_required'
    entry['revision'] -= 1; entry['fingerprint'] = _fingerprint({k:v for k,v in entry.items() if k != 'fingerprint'})
    with pytest.raises(ValueError, match='shared quota owner unavailable'):
        reserve_completed_turn(jobs,**args)
    lease = _retained_reservation(jobs, first['job_id'])
    from src.openclank.operation_router import ManagedOperationRouter
    class FakeRouter(ManagedOperationRouter):
        def __init__(self): pass
        def prepare(self, request): return SimpleNamespace(request=request, payload={"input": dict(request.input)})
    prepared = prepare_completed_turn(jobs,source=ref,target_authority=target,assignment_authority=authority,owner='alice',job_id=first['job_id'],attempt_id=lease['attempt_id'],lease_epoch=lease['lease_epoch'],router=FakeRouter())
    assert prepared.request.input['messages'][1]['content'] == 'body'
    assert 'host_user_body_only' in prepared.request.input['messages'][0]['content']
    with pytest.raises(ValueError,match='dispatch admission'):
        commit_dispatch(jobs,owner='alice',attempt_id=lease['attempt_id'],lease_epoch=lease['lease_epoch'],coverage_generation=profile.coverage_generation,clock_generation=profile.clock_generation,policy_fingerprint='b'*64,authority_fingerprint=old_authority)
    foreground_enter(jobs,owner='alice',account_id='account-123',operation_id='foreground',connection_id='connection',coverage_generation=profile.coverage_generation)
    with pytest.raises(ValueError,match='completed-turn output bound unavailable'):
        prepare_completed_turn(jobs,source=ref,target_authority=target,assignment_authority=authority,owner='alice',job_id=first['job_id'],attempt_id=lease['attempt_id'],lease_epoch=lease['lease_epoch'],router=FakeRouter())
    with sqlite3.connect(archive.db_path) as db:
        original = db.execute("SELECT receipt_json FROM origin_admission WHERE admission_id='root'").fetchone()[0]
        changed = json.loads(original); changed['policy_value']='f'*64
        db.execute("UPDATE origin_admission SET receipt_json=? WHERE admission_id='root'",(json.dumps(changed),))
    with pytest.raises(ValueError,match='origin evidence changed'):
        stage_completed_turn(jobs,source=ref,target_authority=target,assignment_authority=authority,limits=limits,now_ms=100000)
    with sqlite3.connect(archive.db_path) as db: db.execute("UPDATE origin_admission SET receipt_json=? WHERE admission_id='root'",(original,))
    from src.openclank.chat_source_bridge import veto_root_capture
    with factory() as db:
        veto_root_capture(db,archive,owner='alice',chat_id='c',root_operation_id='root')
    with pytest.raises(ValueError,match='source authority unavailable'):
        reserve_completed_turn(jobs,**args)
    with sqlite3.connect(host) as db: db.execute("UPDATE chat_messages SET content='changed' WHERE id='root'")
    with pytest.raises(ValueError,match='source authority unavailable'):
        stage_completed_turn(jobs,source=ref,target_authority=target,assignment_authority=authority,limits=limits,now_ms=100000)
    profile.database_guard.close(); guard.close()

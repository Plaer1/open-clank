import hashlib
import json
import sqlite3
from src.openclank import host_source_schema
from dataclasses import replace
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import src.memery_external_sources as adapter
from src.openclank.chat_source_bridge import HostOriginAdmission, enqueue_new, deliver, complete_user_origin
from src.openclank.conversation_archive import ConversationArchive


def test_current_host_user_source_requires_fresh_scope(tmp_path, monkeypatch):
    host = tmp_path / 'host.db'
    with sqlite3.connect(host) as db:
        db.executescript('CREATE TABLE sessions(id TEXT PRIMARY KEY,owner TEXT,workspace_id TEXT); CREATE TABLE chat_messages(id TEXT PRIMARY KEY,session_id TEXT,content TEXT,role TEXT,timestamp TEXT); INSERT INTO sessions VALUES ("c","alice","w"); INSERT INTO chat_messages VALUES ("root","c","body","user","2026-01-01 00:00:00"),("answer","c","ok","assistant","2026-01-01 00:00:01");')
    host_source_schema.convert(str(host))
    host_source_schema.upgrade_origin_producer(str(host))
    archive = ConversationArchive(str(tmp_path / 'archive.db'), enable_origin_policy_for_disposable_test=True)
    factory = sessionmaker(bind=create_engine(f'sqlite:///{host}'))
    origin = HostOriginAdmission('alice','c','root','{"incognito":false,"noMemory":false,"compareMode":false}','a'*64,True)
    with factory() as db:
        enqueue_new(db, archive=archive, owner='alice', chat_id='c', message_id='root', body='body', origin=origin); db.commit()
        deliver(db, archive, owner='alice', chat_id='c', message_id='root', revision=1)
        complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='root', assistant_message_id='answer')
        deliver(db, archive, owner='alice', chat_id='c', message_id='root', revision=1)
    monkeypatch.setattr(adapter, 'resolve_owned_workspace', lambda *args, **kwargs: SimpleNamespace(workspace=SimpleNamespace(id='w')))
    auth = SimpleNamespace(account_id=lambda owner: 'alice' if owner == 'alice' else None, get_privileges=lambda owner: {'can_manage_memory': True})
    policy = SimpleNamespace(effective_snapshot=lambda *args, **kwargs: {'eligibility_state':'available','capture_allowed':True})
    reader = adapter.CurrentHostUserSource(owner='alice', auth_manager=auth, session_factory=factory, archive=archive, workspace_repository=object(), policy_store=policy)
    ref = adapter.SourceRef(1,'host_user_body','alice','c','main','root','body',1,hashlib.sha256(b'body').hexdigest(),'w',None)
    assert reader.hydrate(ref)['content'] == 'body'
    reader._policy = SimpleNamespace(effective_snapshot=lambda *args, **kwargs: {'eligibility_state':'available','capture_allowed':False})
    assert reader.hydrate(ref)['state'] == 'unavailable'
    assert reader.hydrate_for_manual_review(ref)['content'] == 'body'
    reader._policy = policy
    from services.memory import memory_extractor as extractor
    import asyncio
    frozen = extractor.VerifiedHostUserBody.freeze(reader=reader, ref=ref, session_id='c', name='chat')
    assert frozen.coverage == 'host_user_body_only' and frozen.get_context_messages() == [{'role':'user','content':'body'}]
    assert 'assistant' in frozen.omitted and 'queued' in frozen.omitted
    calls = []
    async def model(**kwargs):
        calls.append(kwargs['messages'])
        return '[{"text":"A durable fact from the user", "category":"fact"}]'
    class Memory:
        def __init__(self): self.saved = []
        def load_all(self): return []
        def find_duplicates(self, *_args): return []
        def add_entry(self, text, **kwargs): return {'id':'fact', 'text':text}
        def commit_owner_snapshot(self, original, entries, owners):
            assert original == [] and owners == {'alice', None}
            self.saved.append(entries)
    monkeypatch.setattr(extractor, '_complete_text', model)
    monkeypatch.setattr(extractor, '_fallback_memory_candidates', lambda _messages: [])
    memory = Memory()
    asyncio.run(extractor.extract_and_store(frozen, memory, None, owner='alice'))
    assert len(calls) == 1 and memory.saved
    assert 'assistant:' not in calls[0][1]['content'] and 'body' in calls[0][1]['content']
    from src.openclank.chat_source_bridge import veto_root_capture
    with factory() as db:
        assert veto_root_capture(db, archive, owner='alice', chat_id='c', root_operation_id='root')
    asyncio.run(extractor.extract_and_store(frozen, memory, None, owner='alice'))
    assert len(calls) == 1 and len(memory.saved) == 1
    assert reader.hydrate(replace(ref, account_id='other'))['state'] == 'unavailable'
    with sqlite3.connect(host) as db:
        db.execute("UPDATE chat_messages SET content='changed' WHERE id='root'")
    assert reader.hydrate(ref)['state'] == 'unavailable'


def test_retirement_requires_exact_durable_host_fact(tmp_path):
    from src.openclank.chat_source_bridge import edit, veto_root_capture, withdraw_deleted_messages
    host = tmp_path / 'host-retirement.db'
    with sqlite3.connect(host) as db:
        db.executescript("CREATE TABLE sessions(id TEXT PRIMARY KEY,owner TEXT,workspace_id TEXT,transcript_revision INTEGER DEFAULT 0); CREATE TABLE chat_messages(id TEXT PRIMARY KEY,session_id TEXT,content TEXT,role TEXT,timestamp TEXT); INSERT INTO sessions VALUES ('c','alice','w',0); INSERT INTO chat_messages VALUES ('root','c','body','user','2026-01-01'),('answer','c','ok','assistant','2026-01-02'),('edited','c','old','user','2026-01-03'),('deleted','c','gone','user','2026-01-04'),('veto','c','private','user','2026-01-05'),('missing','c','unknown','user','2026-01-06');")
    host_source_schema.convert(str(host))
    host_source_schema.upgrade_origin_producer(str(host))
    archive = ConversationArchive(str(tmp_path / 'retirement-archive.db'), enable_origin_policy_for_disposable_test=True)
    factory = sessionmaker(bind=create_engine(f'sqlite:///{host}'))
    auth = SimpleNamespace(account_id=lambda owner: 'alice' if owner == 'alice' else None,
                           username_for_account_id=lambda account: 'alice' if account == 'alice' else None)
    reader = adapter.CurrentHostUserSource(owner='alice', auth_manager=auth, session_factory=factory,
        archive=archive, workspace_repository=object(), policy_store=object())
    def ref(message, body):
        return adapter.SourceRef(1,'host_user_body','alice','c','main',message,'body',1,hashlib.sha256(body.encode()).hexdigest(),'w',None)
    def admission(message):
        return HostOriginAdmission('alice','c',message,'{"incognito":false,"noMemory":false,"compareMode":false}','a'*64,True)
    with factory() as db:
        for message, body in [('root','body'),('edited','old'),('deleted','gone'),('veto','private'),('missing','unknown')]:
            enqueue_new(db, archive=archive, owner='alice', chat_id='c', message_id=message, body=body, origin=admission(message))
        db.commit()
        assert complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='root', assistant_message_id='answer')
        db.commit()
    assert reader.retirement(ref('edited','old'))['state'] == 'current'
    with factory() as db:
        edit(db, archive=archive, owner='alice', chat_id='c', message_id='edited', expected_revision=1, body='new'); db.commit()
        withdraw_deleted_messages(db, archive, owner='alice', chat_id='c', message_ids=['answer','deleted'])
        db.execute(adapter.text("DELETE FROM chat_messages WHERE id IN ('answer','deleted','missing')")); db.commit()
    with factory() as db:
        assert veto_root_capture(db, archive, owner='alice', chat_id='c', root_operation_id='veto')
    assert reader.retirement(ref('edited','old'))['fact'].reason == 'superseded'
    assert reader.retirement(ref('root','body'))['fact'].reason == 'completion_withdrawn'
    assert reader.retirement(ref('deleted','gone'))['fact'].reason == 'deleted'
    assert reader.retirement(ref('veto','private'))['fact'].reason == 'privacy_veto'
    assert reader.retirement(ref('missing','unknown'))['state'] == 'unavailable'


def test_projection_drop_requires_committed_host_marker(tmp_path, monkeypatch):
    from src.openclank.chat_source_bridge import mark_projection_drops, withdraw_deleted_messages
    host = tmp_path / 'host-drop.db'
    with sqlite3.connect(host) as db:
        db.executescript('CREATE TABLE sessions(id TEXT PRIMARY KEY,owner TEXT,workspace_id TEXT); CREATE TABLE chat_messages(id TEXT PRIMARY KEY,session_id TEXT,content TEXT,role TEXT,timestamp TEXT); INSERT INTO sessions VALUES ("c","alice","w"); INSERT INTO chat_messages VALUES ("root","c","body","user","2026-01-01 00:00:00"),("answer","c","ok","assistant","2026-01-01 00:00:01");')
    host_source_schema.convert(str(host))
    host_source_schema.upgrade_origin_producer(str(host))
    host_source_schema.upgrade_execution_snapshot(str(host))
    archive = ConversationArchive(str(tmp_path / 'drop-archive.db'), enable_origin_policy_for_disposable_test=True)
    factory = sessionmaker(bind=create_engine(f'sqlite:///{host}'))
    origin = HostOriginAdmission('alice','c','root','{"incognito":false,"noMemory":false,"compareMode":false}','a'*64,True)
    with factory() as db:
        enqueue_new(db, archive=archive, owner='alice', chat_id='c', message_id='root', body='body', origin=origin); db.commit()
        deliver(db, archive, owner='alice', chat_id='c', message_id='root', revision=1)
        assert complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='root', assistant_message_id='answer')
        deliver(db, archive, owner='alice', chat_id='c', message_id='root', revision=1)
    monkeypatch.setattr(adapter, 'resolve_owned_workspace', lambda *a, **k: SimpleNamespace(workspace=SimpleNamespace(id='w')))
    auth = SimpleNamespace(account_id=lambda owner: 'alice' if owner == 'alice' else None, get_privileges=lambda owner: {'can_manage_memory':True})
    policy = SimpleNamespace(effective_snapshot=lambda *a, **k: {'eligibility_state':'available','capture_allowed':True})
    reader = adapter.CurrentHostUserSource(owner='alice',auth_manager=auth,session_factory=factory,archive=archive,workspace_repository=object(),policy_store=policy)
    ref = adapter.SourceRef(1,'host_user_body','alice','c','main','root','body',1,hashlib.sha256(b'body').hexdigest(),'w',None)
    assert reader.hydrate(ref)['state'] == 'available'
    assert archive.tombstone_part(owner='alice',chat_id='c',actor_id='main',message_id='root',part_id='body',revision=1,reason='projection_drop')['ok']
    assert reader.hydrate(ref)['state'] == 'unavailable'
    with factory() as db:
        mark_projection_drops(db, archive, owner='alice',chat_id='c',rows=[SimpleNamespace(id='root',role='user',content='body',source_body_revision=1,source_body_hash=ref.content_hash)])
        db.execute(adapter.text("DELETE FROM chat_messages WHERE id='root'")); db.commit()
    assert reader.hydrate(ref)['content'] == 'body'
    from src.openclank.conversation_archive import SourcePart
    from src.openclank.chat_source_bridge import read_execution_snapshot, source_component_state
    proof = {'version':1, 'sessionID':'engine', 'rootOperationID':'root', 'actorID':'main', 'executionRevision':1, 'bindingID':'binding', 'bindingRevision':1, 'connectionID':'connection', 'providerID':'provider', 'billingLane':'local', 'modelID':'model', 'modelRouteID':'route', 'modelRequestOptions':{'options':{}}}
    archive.append_parts([SourcePart(owner='alice',chat_id='c',actor_id='main',message_id='answer',part_id='body',role='assistant',content='ok')])
    assert archive.tombstone_part(owner='alice',chat_id='c',actor_id='main',message_id='answer',part_id='body',revision=1,reason='projection_drop')['ok']
    with factory() as db:
        db.execute(adapter.text("UPDATE host_source_delivery_intent SET execution_snapshot_json=:snapshot WHERE message_id='root'"), {'snapshot':json.dumps(proof)})
        mark_projection_drops(db, archive, owner='alice',chat_id='c',rows=[SimpleNamespace(id='answer',role='assistant')])
        db.execute(adapter.text("DELETE FROM chat_messages WHERE id='answer'")); db.commit()
    with factory() as db:
        assert source_component_state(db,archive,owner='alice',chat_id='c',message_id='root',content_hash=ref.content_hash,component='assistant') == 'projection_dropped'
        assert read_execution_snapshot(db,archive,owner='alice',chat_id='c',message_id='root') == proof
    from src.openclank.chat_source_bridge import veto_root_capture
    with factory() as db:
        assert veto_root_capture(db,archive,owner='alice',chat_id='c',root_operation_id='root')
    assert reader.hydrate(ref)['state'] == 'unavailable'
    with factory() as db:
        withdraw_deleted_messages(db,archive,owner='alice',chat_id='c',message_ids=['root']); db.commit()
        assert db.execute(adapter.text("SELECT origin_state FROM host_source_delivery_intent WHERE message_id='root'")).scalar() == 'vetoed'
    assert reader.hydrate(ref)['state'] == 'unavailable'


def test_bulk_source_fence_keeps_one_chat_guard(monkeypatch, tmp_path):
    import asyncio
    from contextlib import asynccontextmanager
    from src.openclank import chat_source_bridge as bridge
    from src.openclank import acp_supervisor_adapter as supervisor_module
    from src.openclank import memory_job_admission as admission
    from src.openclank import session_map as map_module
    calls, entries = [], []
    guard = object(); binding = object(); provider = object()
    class Pool:
        def _runtime_home(self, _owner): return tmp_path
    class Supervisor:
        def owner_lifecycle_pool(self): return Pool()
    class Authority:
        def __init__(self, **_kwargs): pass
        def resolve(self, _account): return binding
    class Mapping:
        def __init__(self, _path, _owner): pass
        @asynccontextmanager
        async def async_chat_admission_lock(self, chat):
            entries.append(chat)
            yield guard
        def validate_chat_guard(self, _chat, value): assert value is guard
    auth = SimpleNamespace(account_id=lambda _: 'account', username_for_account_id=lambda _: 'alice')
    state = SimpleNamespace(mimo_supervisor=Supervisor(), auth_manager=auth)
    monkeypatch.setattr(bridge,'canonical_source_provider',lambda _: provider)
    monkeypatch.setattr(supervisor_module,'AcpSupervisorAdapter',Supervisor)
    monkeypatch.setattr(admission,'TargetAuthority',Authority)
    monkeypatch.setattr(map_module,'OwnerSessionMap',Mapping)
    async def fence(**kwargs):
        assert kwargs['guard'] is guard
        calls.append(kwargs['message_id'])
    monkeypatch.setattr(bridge,'fence_projected_source',fence)
    ids = tuple(f'id-{i:03}' for i in range(129))
    async def run():
        with __import__('pytest').raises(bridge.HostSourceFenceUnavailable):
            async with bridge.guarded_source_mutation(state=state,owner='alice',chat_id='chat',message_ids=ids):
                pass
        async with bridge.guarded_source_mutation(state=state,owner='alice',chat_id='chat',message_ids=ids,_bulk=True) as cap:
            assert cap.fenced_ids == ids
    asyncio.run(run())
    assert entries == ['chat'] and calls == list(ids)

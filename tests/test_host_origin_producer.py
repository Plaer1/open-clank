import json
import sqlite3
from src.openclank import host_source_schema
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from src.openclank.chat_source_bridge import HostOriginAdmission, enqueue_new, deliver, complete_user_origin, recover_chat
from src.openclank.conversation_archive import ConversationArchive


def test_host_origin_is_durable_and_only_ready_after_success(tmp_path):
    host = tmp_path / 'host.db'
    with sqlite3.connect(host) as db:
        db.executescript('CREATE TABLE sessions(id TEXT PRIMARY KEY, owner TEXT, workspace_id TEXT); CREATE TABLE chat_messages(id TEXT PRIMARY KEY, session_id TEXT, content TEXT, role TEXT, timestamp TEXT); INSERT INTO sessions VALUES ("c","alice","w"); INSERT INTO chat_messages VALUES ("root","c","body","user","2026-01-01 00:00:00");')
    host_source_schema.convert(str(host))
    host_source_schema.upgrade_origin_producer(str(host))
    archive = ConversationArchive(str(tmp_path / 'archive.db'), enable_origin_policy_for_disposable_test=True)
    origin = HostOriginAdmission('alice', 'c', 'root', json.dumps({'incognito':False,'noMemory':False,'compareMode':False}), 'a'*64, True)
    with Session(create_engine(f'sqlite:///{host}')) as db:
        enqueue_new(db, archive=archive, owner='alice', chat_id='c', message_id='root', body='body', origin=origin)
        db.commit()
        deliver(db, archive, owner='alice', chat_id='c', message_id='root', revision=1)
        with sqlite3.connect(archive.db_path) as check:
            assert check.execute('SELECT count(*) FROM origin_completion').fetchone()[0] == 0
        db.execute(text("INSERT INTO chat_messages(id,session_id,content,role,timestamp) VALUES ('assistant','c','answer','assistant','2026-01-01 00:00:01')")); db.commit()
        assert complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='root', assistant_message_id='assistant')
        assert complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='root', assistant_message_id='assistant')
        with pytest.raises(ValueError):
            complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='root', assistant_message_id='different')
        assert db.execute(text("SELECT origin_assistant_id FROM host_source_delivery_intent WHERE message_id='root'")).scalar() == 'assistant' 
        assert recover_chat(db, archive, owner='alice', chat_id='c') == 1
        with sqlite3.connect(archive.db_path) as check:
            assert check.execute("SELECT count(*) FROM conversation_archive_outbox WHERE event_kind='origin_ready'").fetchone()[0] == 1

        db.execute(text("INSERT INTO chat_messages(id,session_id,content,role,timestamp) VALUES ('stale','c','old','user','2026-01-01 00:00:02')"))
        stale = HostOriginAdmission('alice', 'c', 'stale', origin.choices_json, origin.policy_fingerprint, True)
        enqueue_new(db, archive=archive, owner='alice', chat_id='c', message_id='stale', body='old', origin=stale)
        db.commit()
        deliver(db, archive, owner='alice', chat_id='c', message_id='stale', revision=1)
        assert complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='stale', assistant_message_id='assistant')
        db.execute(text("UPDATE chat_messages SET content='changed',source_body_revision=2 WHERE id='stale'")); db.commit()
        recover_chat(db, archive, owner='alice', chat_id='c')
        assert db.execute(text("SELECT origin_state FROM host_source_delivery_intent WHERE message_id='stale'")).scalar() == 'superseded'


def test_execution_snapshot_exact_completion_and_current_source(tmp_path):
    from src.openclank.chat_source_bridge import read_execution_snapshot
    host = tmp_path / 'source.db'
    with sqlite3.connect(host) as db:
        db.executescript('CREATE TABLE sessions(id TEXT PRIMARY KEY, owner TEXT, workspace_id TEXT); CREATE TABLE chat_messages(id TEXT PRIMARY KEY, session_id TEXT, content TEXT, role TEXT, timestamp TEXT); INSERT INTO sessions VALUES ("c","alice","w"); INSERT INTO chat_messages VALUES ("root","c","body","user","t"),("assistant","c","answer","assistant","t");')
    host_source_schema.convert(str(host))
    host_source_schema.upgrade_origin_producer(str(host))
    host_source_schema.upgrade_execution_snapshot(str(host))
    class Archive:
        def resolve_owner(self, owner): return owner
    archive = Archive()
    origin = HostOriginAdmission('alice', 'c', 'root', json.dumps({'incognito':False,'noMemory':False,'compareMode':False}), 'a'*64, True)
    proof = {'version':1, 'sessionID':'engine', 'rootOperationID':'root', 'actorID':'main', 'executionRevision':1, 'bindingID':'binding', 'bindingRevision':1, 'connectionID':'connection', 'providerID':'provider', 'billingLane':'local', 'modelID':'model', 'modelRouteID':'route', 'modelRequestOptions':{'options':{}}}
    from core.provider_models import ProviderBase, ProviderOperationBinding
    engine = create_engine(f'sqlite:///{host}')
    ProviderBase.metadata.create_all(engine)
    with Session(engine) as db:
        db.connection().exec_driver_sql("PRAGMA foreign_keys=OFF")
        enqueue_new(db, archive=archive, owner='alice', chat_id='c', message_id='root', body='body', origin=origin)
        db.add(ProviderOperationBinding(id='binding', owner='alice', credential_owner='alice', root_operation_id='root',
            engine_session_id='engine', actor_id='main', execution_revision=1, connection_id='connection',
            provider_id='provider', billing_lane='local', model_id='model', model_route_id='route',
            state='committed', revision=1, policy_snapshot={'main_model_options': proof['modelRequestOptions']})); db.commit()
        assert complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='root', assistant_message_id='assistant', execution_snapshot=proof, provider_owner='alice')
        assert complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='root', assistant_message_id='assistant', execution_snapshot=proof, provider_owner='alice')
        assert read_execution_snapshot(db, archive, owner='alice', chat_id='c', message_id='root') == proof
        with pytest.raises(ValueError):
            read_execution_snapshot(db, archive, owner='bob', chat_id='c', message_id='root')
        with pytest.raises(ValueError):
            complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='root', assistant_message_id='assistant', execution_snapshot={**proof, 'actorID':'other'})
        # Historical receipt is not a current provider grant; privacy changes deny reads.
        db.execute(text("UPDATE host_source_delivery_intent SET origin_json=:j WHERE message_id='root'"), {'j':json.dumps({'choices_json':json.dumps({'incognito':False,'noMemory':True,'compareMode':False}), 'capture_eligible':True})}); db.commit()
        assert read_execution_snapshot(db, archive, owner='alice', chat_id='c', message_id='root') is None
        db.execute(text("UPDATE host_source_delivery_intent SET origin_json=:j WHERE message_id='root'"), {'j':json.dumps({'choices_json':origin.choices_json,'capture_eligible':True})}); db.commit()
        db.execute(text("UPDATE chat_messages SET content='changed' WHERE id='root'")); db.commit()
        assert read_execution_snapshot(db, archive, owner='alice', chat_id='c', message_id='root') is None
        db.rollback()
        assert not complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='root', assistant_message_id='assistant', execution_snapshot=proof, provider_owner='alice')
        db.rollback()
        db.execute(text("INSERT INTO chat_messages(id,session_id,content,role,timestamp) VALUES ('other','c','body','user','t')")); db.commit()
        other = HostOriginAdmission('alice','c','other',origin.choices_json,origin.policy_fingerprint,True)
        enqueue_new(db, archive=archive, owner='alice', chat_id='c', message_id='other', body='body', origin=other)
        binding = db.query(ProviderOperationBinding).filter_by(id='binding').one()
        binding.root_operation_id = 'other'; binding.revision = 2; db.commit()
        stale = {**proof, 'rootOperationID':'other'}
        assert complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='other', assistant_message_id='assistant', execution_snapshot=stale, provider_owner='alice')
        assert read_execution_snapshot(db, archive, owner='alice', chat_id='c', message_id='other') is None


def test_root_privacy_veto_blocks_delivered_origin_and_replay(tmp_path):
    from src.openclank.chat_source_bridge import veto_root_capture, verify_current
    from types import SimpleNamespace
    host = tmp_path / 'host-veto.db'
    with sqlite3.connect(host) as db:
        db.executescript("CREATE TABLE sessions(id TEXT PRIMARY KEY, owner TEXT, workspace_id TEXT); CREATE TABLE chat_messages(id TEXT PRIMARY KEY, session_id TEXT, content TEXT, role TEXT, timestamp TEXT); INSERT INTO sessions VALUES ('c','alice','w'); INSERT INTO chat_messages VALUES ('root','c','body','user','2026-01-01'); INSERT INTO chat_messages VALUES ('assistant','c','answer','assistant','2026-01-01');")
    host_source_schema.convert(str(host))
    host_source_schema.upgrade_origin_producer(str(host))
    archive = ConversationArchive(str(tmp_path / 'veto-archive.db'), enable_origin_policy_for_disposable_test=True)
    origin = HostOriginAdmission('alice', 'c', 'root', json.dumps({'incognito':False,'noMemory':False,'compareMode':False}), 'a'*64, True)
    engine = create_engine(f'sqlite:///{host}')
    with Session(engine) as db:
        enqueue_new(db, archive=archive, owner='alice', chat_id='c', message_id='root', body='body', origin=origin); db.commit()
        deliver(db, archive, owner='alice', chat_id='c', message_id='root', revision=1)
        assert complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='root', assistant_message_id='assistant')
        deliver(db, archive, owner='alice', chat_id='c', message_id='root', revision=1)
    with Session(engine) as db:
        assert veto_root_capture(db, archive, owner='alice', chat_id='c', root_operation_id='root')
    with Session(engine) as db:
        part = SimpleNamespace(message_id='root', part_id='body', revision=1, content_hash=__import__('hashlib').sha256(b'body').hexdigest())
        assert not verify_current(db, archive, 'alice', 'c', 'main', part)
        db.rollback()
        assert not complete_user_origin(db, archive, owner='alice', chat_id='c', root_operation_id='root', assistant_message_id='assistant')

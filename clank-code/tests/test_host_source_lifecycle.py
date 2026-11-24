import sqlite3
from src.openclank import host_source_schema
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from src.openclank.account_lifecycle import AccountOwnerLifecycle
from src.openclank.conversation_archive import ConversationArchive, SourcePart


def test_offline_baseline_and_account_intent_purge(tmp_path):
    host = tmp_path / 'host.db'
    with sqlite3.connect(host) as db:
        db.executescript('CREATE TABLE sessions(id TEXT PRIMARY KEY, owner TEXT); CREATE TABLE chat_messages(id TEXT PRIMARY KEY, session_id TEXT, content TEXT); INSERT INTO sessions VALUES ("c","alice"); INSERT INTO chat_messages VALUES ("m","c","body");')
    host_source_schema.convert(str(host))
    archive = ConversationArchive(str(tmp_path / 'archive.db'))
    archive.owner_inventory('alice')
    archive.append_parts([SourcePart(owner='alice', chat_id='c', actor_id='main', message_id='m', part_id='body', content='body', aliases=(('host_message', 'm'),))])
    assert host_source_schema.baseline_body(str(host), archive.db_path, owner_account_id='alice', chat_id='c', message_id='m') == {'revision': 1, 'privacy': 'unknown'}
    factory = sessionmaker(bind=create_engine(f'sqlite:///{host}'))
    with factory() as db:
        db.execute(text("INSERT INTO host_source_delivery_intent VALUES ('alice','orphan','x','body',1,'h','pending'),('other','orphan','y','body',1,'h','pending')")); db.commit()
    lifecycle = AccountOwnerLifecycle.__new__(AccountOwnerLifecycle)
    lifecycle._session_factory = factory; lifecycle._conversation_archive = archive
    assert lifecycle._host_intent_inventory('alice')['count'] == 1
    assert lifecycle._purge_host_intents('alice')['residual'] == 0
    assert lifecycle._host_intent_inventory('other')['count'] == 1

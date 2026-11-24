import sqlite3
from src.openclank import host_source_schema
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
import pytest
from src.openclank.chat_source_bridge import available, enqueue_new, edit, deliver, verify_current, digest
from src.openclank.conversation_archive import ConversationArchive, TrustedOriginPart


def test_host_transaction_and_exact_delivery(tmp_path):
    path = tmp_path / 'host.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE sessions(id TEXT PRIMARY KEY, owner TEXT, workspace_id TEXT, transcript_revision INTEGER); CREATE TABLE chat_messages(id TEXT PRIMARY KEY, session_id TEXT, content TEXT, role TEXT, timestamp TEXT); INSERT INTO sessions VALUES ("c","alice","w",0); INSERT INTO chat_messages VALUES ("m","c","first","user","2026-01-01 00:00:00");')
    host_source_schema.convert(str(path))
    archive = ConversationArchive(str(tmp_path / 'archive.db'))
    engine = create_engine(f'sqlite:///{path}')
    with Session(engine) as db:
        assert available(db)
        enqueue_new(db, archive=archive, owner='alice', chat_id='c', message_id='m', body='first')
        db.commit()
        assert deliver(db, archive, owner='alice', chat_id='c', message_id='m', revision=1) == 'delivered'
        assert verify_current(db, archive, 'alice', 'c', 'main', TrustedOriginPart('m','body',1,digest('first')))
        assert edit(db, archive=archive, owner='alice', chat_id='c', message_id='m', expected_revision=1, body='second') == 2
        db.rollback()
        assert verify_current(db, archive, 'alice', 'c', 'main', TrustedOriginPart('m','body',1,digest('first')))
        assert edit(db, archive=archive, owner='alice', chat_id='c', message_id='m', expected_revision=1, body='second') == 2
        db.commit()
        assert db.execute(text('SELECT transcript_revision FROM sessions WHERE id="c"')).scalar() == 1
        with pytest.raises(ValueError):
            edit(db, archive=archive, owner='alice', chat_id='c', message_id='m', expected_revision=1, body='bad')
        assert not verify_current(db, archive, 'alice', 'c', 'main', TrustedOriginPart('m','body',1,digest('first')))
        assert deliver(db, archive, owner='alice', chat_id='c', message_id='m', revision=2) == 'delivered'
        assert deliver(db, archive, owner='alice', chat_id='c', message_id='m', revision=2) == 'delivered'

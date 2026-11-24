import json
import sqlite3
from types import SimpleNamespace

import pytest

from services.logging.semantic import ARCHIVE_INDEX_SCOPE
from src.memery_rag import MemeryRAG


def test_archive_absence_keeps_guarded_database_read_only(tmp_path):
    path = tmp_path / 'index.db'
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE fm_v2_sources(owner_id,workspace_key,project_key,source_id,source_revision,source_type);
            CREATE TABLE fm_v2_documents(owner_id,document_id,source_id,source_revision);
            CREATE TABLE fm_v2_chunks(owner_id,document_id,locator_json);
        """)
        db.execute("INSERT INTO fm_v2_sources VALUES(?,?,?,?,?,?)", ('alice',ARCHIVE_INDEX_SCOPE,'','s',1,'external_reference'))
        db.execute("INSERT INTO fm_v2_documents VALUES('alice','d','s',1)")
        db.execute("INSERT INTO fm_v2_chunks VALUES('alice','d',?)", (json.dumps({'type':'conversation_archive_part','chat_id':'old'}),))
    rag = object.__new__(MemeryRAG)
    rag.db_path = str(path)
    rag._healthy = True
    rag._guarded_preparation = SimpleNamespace(assert_valid=lambda: None)
    def erase(owner, chat):
        return rag.erase_external_chat_references(owner=owner, workspace_id=ARCHIVE_INDEX_SCOPE, chat_id=chat)
    assert erase('alice', 'new')['removed_documents'] == 0
    assert erase('bob', 'old')['residual'] == 0
    with pytest.raises(RuntimeError, match='retained or unverifiable'):
        erase('alice', 'old')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM fm_v2_chunks').fetchone() == (1,)
        db.execute("UPDATE fm_v2_chunks SET locator_json='broken'")
    with pytest.raises(RuntimeError, match='retained or unverifiable'):
        erase('alice', 'new')


def test_skill_attachment_refresh_respects_absent_or_damaged_index(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.database import FilesImageResource
    from src.upload_handler import UploadHandler
    from src.openclank.attachment_admission import refresh_domain_references

    engine = create_engine('sqlite://')
    FilesImageResource.__table__.create(engine)
    store = SimpleNamespace(session_factory=sessionmaker(bind=engine))
    directory = tmp_path / 'uploads'
    handler = UploadHandler(str(tmp_path), str(directory))
    inventory = lambda: {'complete':True, 'references':[]}
    refresh_domain_references('skills', inventory, upload_handler=handler, image_store=store)
    assert not (directory / 'uploads.json').exists()
    (directory / 'orphan.bin').write_bytes(b'x')
    with pytest.raises(ValueError, match='missing'):
        refresh_domain_references('skills', inventory, upload_handler=handler, image_store=store)


@pytest.mark.parametrize('has_documents', [False, True])
@pytest.mark.asyncio
async def test_account_purge_consumes_exact_moved_copal_and_agent_receipts(tmp_path, has_documents):
    from routes.auth_routes import _tombstone_inventory
    from src.openclank.copal_loose import LooseCopalBridge
    from src.openclank.copal_errors import CopalBridgeError
    from src.openclank.mimo_supervisor import MimoSupervisorPool
    original, tombstone = 'fixture', 'deleted:fixture'
    copal = LooseCopalBridge(tmp_path/'copal')
    pool = MimoSupervisorPool(data_dir=tmp_path/'mimo')
    args = {'owner':original,'workspace_id':'default','kind':'markdown','name':'test','content':'body'}
    if has_documents:
        await copal.call('create', args)
        runtime = pool._runtime_home(original)
        runtime.mkdir(parents=True)
        (runtime/'data.txt').write_text('fixture')
    cm = await copal.call('rename_owner',{'old_owner':original,'new_owner':tombstone})
    mm = await pool.reconcile_owner_rename(original,tombstone,await pool.preview_owner_rename(original,tombstone))
    steps = {'copal':{'state':'applied','receipt':cm},'mimo':{'state':'applied','receipt':mm}}
    ci = _tombstone_inventory(steps,'copal','target_after',tombstone)
    mi = _tombstone_inventory(steps,'mimo','target',tombstone)
    with pytest.raises(RuntimeError,match='inventory unavailable'):
        _tombstone_inventory(steps,'copal','source_before',tombstone)
    with pytest.raises(CopalBridgeError):
        await copal.call('purge_owner',{'owner':tombstone,'expected':cm['source_before']})
    await copal.call('purge_owner',{'owner':tombstone,'expected':ci})
    await pool.purge_owner_lifecycle(tombstone, expected=mi)
    assert pool.owner_lifecycle_inventory(tombstone)['count'] == 0


def test_deleted_observation_requires_completed_scoped_chat_erasure(tmp_path):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from src.openclank.chat_source_bridge import _deleted_observation_chat, HostSourceFenceUnavailable
    from src.openclank.conversation_archive import ConversationArchive, SourcePart

    engine = create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE sessions(id TEXT PRIMARY KEY)'))
    sessions = sessionmaker(bind=engine)
    archive = ConversationArchive(str(tmp_path / 'archive.db'))
    archive.append_parts([SourcePart('a','chat','main','message','body',content='fixture')])
    def deleted(owner):
        return _deleted_observation_chat(archive, sessions, account_id=owner, chat='chat')
    archive.begin_chat_erasure(owner='a', chat_id='chat')
    with pytest.raises(HostSourceFenceUnavailable):
        deleted('a')
    archive.erase_chat(owner='a', chat_id='chat')
    assert deleted('a')
    assert not deleted('b')
    with engine.begin() as db:
        db.execute(text("INSERT INTO sessions VALUES ('chat')"))
    with pytest.raises(HostSourceFenceUnavailable):
        deleted('a')

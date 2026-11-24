import asyncio, json
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import core.database as database
import src.openclank.session_operations as operations
import src.openclank.session_fork as forks
import src.openclank.transcript_projection as projection
from src.openclank.acp_bridge import ACPBridge
from core.database import Base, Session, ChatMessage

@pytest.fixture
def context(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'fork.db'}")
    Base.metadata.create_all(engine); sessions = sessionmaker(bind=engine)
    for module in (database, operations, forks, projection): monkeypatch.setattr(module, "SessionLocal", sessions)
    import src.openclank.conversation_archive as archive
    monkeypatch.setattr(archive, "get_conversation_archive", lambda: SimpleNamespace())
    class Client:
        def register_callback(self, *args): pass
        def on_session_update(self, callback): pass
        async def reserve_session(self): return {"sessionID": "ses_child"}
    bridge = ACPBridge(Client(), str(tmp_path), owner="alice", session_map_path=tmp_path / "map.json")
    bridge._durable_session_map.bind("chat", "ses_source")
    binding = {"owner": "alice", "stableChatID": "chat", "engineSessionID": "ses_source", "physicalCwd": str(tmp_path), "authorityWorkspaceID": "", "memoryWorkspaceID": "global", "copalWorkspace": "default", "engineAliases": [], "workspaceRevision": 0, "mappingRevision": 1, "mapRevision": 1, "memoryEnabled": True, "transition": None}
    with sessions() as db:
        db.add(Session(id="chat", owner="alice", name="Source", endpoint_url="openclank://engine", model="model", mimo_state={"managed_binding": binding}))
        for index, (mid, role) in enumerate([("root_one", "user"), ("answer_one", "assistant"), ("root_two", "user"), ("answer_two", "assistant")]):
            metadata = {"attachments": [{"attachment_id": "a" * 32 + ".png", "name": "source.png", "mime": "image/png", "size": 5}], "root_operation_id": mid}
            db.add(ChatMessage(id=mid, session_id="chat", role=role, content="exact source text", meta_data=json.dumps(metadata), timestamp=datetime(2026, 10, 13) + timedelta(seconds=index)))
        db.commit(); row = db.query(Session).filter_by(id="chat").one(); row.transcript_revision = 0; db.commit()
    bridge._bind_canonical_session("ses_source", "chat", "alice", physical_cwd=str(tmp_path), map_revision=1, mapping_revision=1)
    async def resume(*args, **kwargs): pass
    bridge.resume_session = resume
    manager = SimpleNamespace(get_session=lambda sid: SimpleNamespace(name="Source"), upload_handler=None, _archive_message_parts=lambda *args, **kwargs: None)
    class Worker:
        _bridge = bridge
        installed_generation = 3
        fail = False
        source_revision_change = False
        receipt = None
        calls = []
        async def managed_engine_call(self, method, params):
            self.calls.append((method, params))
            if method.endswith("source-map"):
                if params["sessionID"] == "ses_child":
                    return {"anchors": [], "sourceMessageCount": 4, "forkReceipt": self.receipt, "revert": None}
                return {"anchors": [{"hostSourceMessageID": "root_one", "nativeMessageID": "msg_one"}, {"hostSourceMessageID": "root_two", "nativeMessageID": "msg_two"}], "sourceMessageCount": 2 if params.get("messageID") else 4, "revert": None}
            assert method.endswith("fork/create")
            # Actual bridge callback proves target authority existed before writes.
            owner, child_id = bridge._managed_history_binding("ses_child")
            assert owner == "alice" and child_id != "chat"
            current = await bridge._handle_session_binding_read({"sessionID": "ses_child"})
            assert current["canonicalCwd"] == str(tmp_path)
            self.receipt = {"sourceSessionID": "ses_source", "targetSessionID": "ses_child", "fingerprint": forks.fingerprint(params), "sourceMessageCount": 2 if params.get("messageID") else 4}
            if self.source_revision_change:
                with sessions() as db:
                    row = db.query(Session).filter_by(id="chat").one(); row.transcript_revision += 1; db.commit()
            if self.fail: raise RuntimeError("interrupted after complete clone")
            return {"created": True, "fingerprint": self.receipt["fingerprint"], "forkReceipt": self.receipt}
    return sessions, Worker(), manager

def invoke(worker, manager, operation="fork", **payload):
    return asyncio.run(forks.execute(worker, manager, sid="chat", owner="alice", operation=operation, payload={"expected_transcript_revision": 0, **payload}, check_current=lambda: None))

def test_reserved_fork_prebinds_and_preserves_canonical_attachment_identity(context):
    sessions, worker, manager = context
    result = invoke(worker, manager)
    with sessions() as db:
        child = db.query(Session).filter_by(id=result["id"]).one()
        assert child.archived is False
        rows = db.query(ChatMessage).filter_by(session_id=child.id).order_by(ChatMessage.timestamp).all()
        assert len(rows) == 4 and [row.role for row in rows] == ["user", "assistant", "user", "assistant"]
        assert json.loads(rows[0].meta_data)["attachments"][0]["attachment_id"] == "a" * 32 + ".png"
        assert json.loads(rows[0].meta_data)["root_operation_id"] == rows[0].id
        assert db.query(ChatMessage).filter_by(session_id="chat").count() == 4
    request = next(params for method, params in worker.calls if method.endswith("fork/create"))
    assert request["hostSourceMap"]["root_one"] != "root_one"
    assert worker._bridge.mapped_session_id(result["id"]) == "ses_child"

def test_cutoff_is_exact_native_and_host_boundary(context):
    sessions, worker, manager = context
    result = invoke(worker, manager, host_message_id="root_two")
    with sessions() as db: assert db.query(ChatMessage).filter_by(session_id=result["id"]).count() == 2
    request = next(params for method, params in worker.calls if method.endswith("fork/create"))
    assert request["messageID"] == "msg_two" and list(request["hostSourceMap"]) == ["root_one"]

def test_interrupted_fork_stays_hidden_until_final_receipt_reconciliation(context):
    sessions, worker, manager = context; worker.fail = True
    with pytest.raises(forks.ForkOperationError) as error: invoke(worker, manager)
    child_id = error.value.candidate_id
    with sessions() as db: assert db.query(Session).filter_by(id=child_id).one().archived is True
    with pytest.raises(operations.HistoryOperationError): projection.canonical_snapshot(child_id, owner="alice")
    result = invoke(worker, manager, "fork-reconcile", child_session_id=child_id)
    assert result["id"] == child_id
    assert len(projection.canonical_snapshot(child_id, owner="alice").message_ids) == 4

def test_incomplete_receipt_and_stale_source_never_publish_candidate(context):
    sessions, worker, manager = context; worker.fail = True
    with pytest.raises(forks.ForkOperationError) as error: invoke(worker, manager)
    worker.receipt = None
    with pytest.raises(forks.ForkOperationError, match="complete-clone receipt"): invoke(worker, manager, "fork-reconcile", child_session_id=error.value.candidate_id)
    with sessions() as db: assert db.query(Session).filter_by(id=error.value.candidate_id).one().archived is True


def test_resume_failure_keeps_candidate_archived_until_explicit_reconcile(context):
    sessions, worker, manager = context
    async def broken(*args, **kwargs): raise RuntimeError("MCP handshake interrupted")
    worker._bridge.resume_session = broken
    with pytest.raises(forks.ForkOperationError) as error: invoke(worker, manager)
    child_id = error.value.candidate_id
    with sessions() as db: assert db.query(Session).filter_by(id=child_id).one().archived is True
    async def repaired(*args, **kwargs): pass
    worker._bridge.resume_session = repaired
    assert invoke(worker, manager, "fork-reconcile", child_session_id=child_id)["id"] == child_id

import asyncio
from types import SimpleNamespace
from datetime import datetime, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import core.database as database
import src.openclank.session_operations as operations
import src.openclank.transcript_projection as projection
from core.database import Base, Session, ChatMessage

@pytest.fixture
def history(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'history.db'}")
    Base.metadata.create_all(engine); sessions = sessionmaker(bind=engine)
    for module in (operations, projection, database): monkeypatch.setattr(module, "SessionLocal", sessions)
    binding = {"owner": "alice", "stableChatID": "chat", "engineSessionID": "ses_native", "physicalCwd": str(tmp_path), "workspaceRevision": 0, "mappingRevision": 1, "mapRevision": 1}
    with sessions() as db:
        db.add(Session(id="chat", owner="alice", name="Chat", endpoint_url="openclank://engine", model="model", transcript_revision=0, mimo_state={"managed_binding": binding}))
        for index, (message_id, role) in enumerate([("root_one", "user"), ("answer_one", "assistant"), ("root_two", "user"), ("answer_two", "assistant")]):
            db.add(ChatMessage(id=message_id, session_id="chat", role=role, content='[{"type":"image_url","image_url":{"url":"attachment://preserved"}}]', meta_data='{}', timestamp=datetime(2026, 10, 13) + timedelta(seconds=index)))
        db.commit()
        # SQLite transcript triggers advanced the revision during seeding.
        row = db.query(Session).filter_by(id="chat").one()
        row.transcript_revision = 0
        db.commit()
    bridge = SimpleNamespace(mapped_sessions=lambda owner: {"chat": "ses_native"}, mapped_session_id=lambda sid: "ses_native", mapped_session_workspace=lambda sid: str(tmp_path))
    class Worker:
        installed_generation = 3
        _bridge = bridge
        pointer = None
        fail = False
        mutate_host = False
        calls = []
        async def managed_engine_call(self, method, params):
            return {"anchors": [{"hostSourceMessageID": "root_one", "nativeMessageID": "msg_one"}, {"hostSourceMessageID": "root_two", "nativeMessageID": "msg_two"}], "revert": self.pointer}
        async def session_http_request(self, sid, method, suffix, **kwargs):
            self.calls.append(suffix)
            self.pointer = kwargs["payload"] if suffix == "revert" else None
            if self.mutate_host:
                with sessions() as db:
                    row = db.query(Session).filter_by(id="chat").one(); row.transcript_revision += 1; db.commit()
            if self.fail: raise RuntimeError("interruption after native mutation")
            value = {"revert": {**self.pointer, "snapshot": "native-owned-snapshot"} if self.pointer else None}
            return SimpleNamespace(status_code=200, json=lambda: value)
    return sessions, Worker()

def call(worker, operation, revision=0, **payload):
    return asyncio.run(operations.execute(worker, sid="chat", owner="alice", operation=operation, payload={"expected_transcript_revision": revision, **payload}, check_current=lambda: None))

def test_native_undo_redo_preserves_archived_attachments_and_bootstrap_view(history):
    sessions, worker = history
    receipt = call(worker, "undo")
    assert worker.pointer == {"messageID": "msg_two"}
    assert receipt["history_operation"]["file_snapshot_available"] is True
    assert "binding" not in receipt["history_operation"]
    assert projection.canonical_snapshot("chat", owner="alice").message_ids == ("root_one", "answer_one")
    # Full canonical rows/attachment content stay recoverable; native owns files.
    with sessions() as db:
        assert db.query(ChatMessage).count() == 4
        assert "attachment://preserved" in db.query(ChatMessage).filter_by(id="root_two").one().content
    call(worker, "redo", revision=1)
    assert len(projection.canonical_snapshot("chat", owner="alice").message_ids) == 4
    assert worker.calls == ["revert", "unrevert"]

def test_interruption_fences_rebootstrap_and_explicit_reconciliation(history):
    _, worker = history; worker.fail = True
    with pytest.raises(RuntimeError, match="interruption"): call(worker, "undo")
    with pytest.raises(operations.HistoryOperationError, match="reconciliation"): projection.canonical_snapshot("chat", owner="alice")
    receipt = call(worker, "history-reconcile")
    assert receipt["history_operation"]["outcome"] == "confirmed"
    assert projection.canonical_snapshot("chat", owner="alice").message_ids == ("root_one", "answer_one")

def test_new_generation_bootstrap_uses_settled_view_and_stale_redo_fails(history):
    _, worker = history; call(worker, "undo")
    before = projection.canonical_snapshot("chat", owner="alice")
    worker.installed_generation = 4
    worker.pointer = None  # native new-turn cleanup or replacement has no redo
    assert projection.canonical_snapshot("chat", owner="alice").message_ids == before.message_ids
    with pytest.raises(operations.HistoryOperationError, match="invalidated redo"): call(worker, "redo", revision=1)
    assert worker.calls == ["revert"]

def test_source_boundary_revision_and_owner_are_checked_before_mutation(history):
    _, worker = history
    with pytest.raises(operations.HistoryOperationError, match="expected_transcript_revision"): call(worker, "undo", revision=9)
    with pytest.raises(operations.HistoryOperationError, match="source-mapped"): call(worker, "undo", host_message_id="answer_one")
    with pytest.raises(KeyError): operations._read("chat", "bob")
    assert worker.calls == []

def test_concurrent_host_revision_leaves_recoverable_pending_intent(history):
    _, worker = history; worker.mutate_host = True
    with pytest.raises(operations.HistoryOperationError, match="host history changed"): call(worker, "undo")
    with pytest.raises(operations.HistoryOperationError): projection.canonical_snapshot("chat", owner="alice")
    result = call(worker, "history-reconcile", revision=1)
    assert result["transcript_revision"] == 2 and result["history_operation"]["outcome"] == "confirmed"

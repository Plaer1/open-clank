from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.models import ChatMessage
from core.session_manager import SessionManager
import core.session_manager as SM


def _manager_with(sessions):
    manager = SessionManager.__new__(SessionManager)
    manager.sessions = dict(sessions)
    return manager


def _session_local(parent_row):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = parent_row
    return MagicMock(return_value=db), db


def test_persist_message_drops_write_when_parent_session_is_gone(monkeypatch):
    session_local, db = _session_local(None)
    monkeypatch.setattr(SM, "SessionLocal", session_local)

    manager = _manager_with({"deleted": SimpleNamespace(history=[])})
    message = ChatMessage("assistant", "late token")

    manager._persist_message("deleted", message)

    assert "deleted" not in manager.sessions
    db.add.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


def test_persist_message_still_writes_when_parent_session_exists(monkeypatch):
    parent = SimpleNamespace(message_count=0, last_accessed=None, last_message_at=None)
    session_local, db = _session_local(parent)
    monkeypatch.setattr(SM, "SessionLocal", session_local)

    message = ChatMessage("user", "hello")
    manager = _manager_with({"sid": SimpleNamespace(history=[message])})

    manager._persist_message("sid", message)

    db.add.assert_called_once()
    db.commit.assert_called_once()
    assert parent.message_count == 1
    assert parent.last_accessed is not None
    assert parent.last_message_at is not None
    assert message.metadata["_db_id"]
    assert message.metadata["timestamp"].endswith("Z")


def test_persist_message_propagates_commit_failure(monkeypatch):
    parent = SimpleNamespace(message_count=0, last_accessed=None, last_message_at=None)
    session_local, db = _session_local(parent)
    db.commit.side_effect = RuntimeError("disk full")
    monkeypatch.setattr(SM, "SessionLocal", session_local)

    message = ChatMessage("assistant", "must be durable")
    manager = _manager_with({"sid": SimpleNamespace(history=[message])})

    with pytest.raises(RuntimeError, match="disk full"):
        manager._persist_message("sid", message)

    db.rollback.assert_called_once()
    assert "_db_id" not in message.metadata


def test_add_message_rolls_back_memory_after_persistence_failure(monkeypatch):
    session = SimpleNamespace(history=[], message_count=0)
    manager = _manager_with({"sid": session})
    monkeypatch.setattr(manager, "_persist_message", MagicMock(side_effect=RuntimeError("write failed")))

    with pytest.raises(RuntimeError, match="write failed"):
        manager.add_message("sid", ChatMessage("assistant", "not committed"))

    assert session.history == []
    assert session.message_count == 0

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, Session as DbSession
from src.openclank.chat_lifecycle import ChatLifecycleError, ChatLifecycleService


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'chat-lifecycle.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.mark.asyncio
async def test_archive_restore_coordinates_projection_owner_and_process_cache(tmp_path, monkeypatch):
    factory = _factory(tmp_path)
    db = factory()
    try:
        db.add(DbSession(
            id="chat-a",
            name="Chat A",
            endpoint_url="http://model.invalid",
            model="model-a",
            owner="alice",
            archived=False,
        ))
        db.commit()
    finally:
        db.close()

    purges = []

    async def purge(supervisor, session_id, *, owner=None):
        purges.append((supervisor, session_id, owner))
        return True

    monkeypatch.setattr("src.openclank.transcript_projection.purge_execution_projection", purge)
    monkeypatch.setattr("src.agent_runs.is_active", lambda _session_id: False)
    cached = SimpleNamespace(archived=False)
    manager = SimpleNamespace(sessions={"chat-a": cached})
    supervisor = object()
    service = ChatLifecycleService(
        session_factory=factory,
        session_manager=manager,
        mimo_supervisor=supervisor,
    )

    await service.set_archived(owner="alice", session_id="chat-a", archived=True)
    assert cached.archived is True
    await service.set_archived(owner="alice", session_id="chat-a", archived=False)
    assert cached.archived is False
    assert purges == [
        (supervisor, "chat-a", "alice"),
        (supervisor, "chat-a", "alice"),
    ]
    db = factory()
    try:
        assert db.query(DbSession.archived).filter(DbSession.id == "chat-a").scalar() is False
    finally:
        db.close()

    with pytest.raises(ChatLifecycleError) as denied:
        await service.set_archived(owner="bob", session_id="chat-a", archived=True)
    assert denied.value.code == "chat_unavailable"


@pytest.mark.asyncio
async def test_active_run_rejects_before_projection_or_database_mutation(tmp_path, monkeypatch):
    factory = _factory(tmp_path)
    db = factory()
    try:
        db.add(DbSession(
            id="chat-active",
            name="Active",
            endpoint_url="http://model.invalid",
            model="model-a",
            owner="alice",
            archived=False,
        ))
        db.commit()
    finally:
        db.close()
    purged = []

    async def purge(*_args, **_kwargs):
        purged.append(True)

    monkeypatch.setattr("src.openclank.transcript_projection.purge_execution_projection", purge)
    monkeypatch.setattr("src.agent_runs.is_active", lambda session_id: session_id == "chat-active")
    service = ChatLifecycleService(session_factory=factory)
    with pytest.raises(ChatLifecycleError) as blocked:
        await service.set_archived(owner="alice", session_id="chat-active", archived=True)
    assert blocked.value.code == "active_run"
    assert purged == []
    db = factory()
    try:
        assert db.query(DbSession.archived).filter(DbSession.id == "chat-active").scalar() is False
    finally:
        db.close()

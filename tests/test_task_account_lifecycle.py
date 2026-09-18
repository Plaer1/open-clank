"""Scheduled task admission and runtime cleanup during account lifecycle."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from sqlalchemy import Column, DateTime, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


def _database(tmp_path, monkeypatch):
    import core.database as database

    base = declarative_base()

    class ScheduledTask(base):
        __tablename__ = "scheduled_tasks"

        id = Column(String, primary_key=True)
        owner = Column(String, nullable=False)
        name = Column(String)
        status = Column(String, default="active")

    class TaskRun(base):
        __tablename__ = "task_runs"

        id = Column(String, primary_key=True)
        task_id = Column(String)
        started_at = Column(DateTime)
        finished_at = Column(DateTime)
        status = Column(String)
        result = Column(Text)
        error = Column(Text)
        model = Column(String)

    engine = create_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(database, "ScheduledTask", ScheduledTask)
    monkeypatch.setattr(database, "TaskRun", TaskRun)
    return factory, ScheduledTask, TaskRun, engine


def _scheduler(fenced):
    from src.task_scheduler import TaskScheduler

    auth = type(
        "Auth",
        (),
        {"is_account_lifecycle_fenced": lambda _self, owner: owner in fenced},
    )()
    return TaskScheduler(session_manager=None, auth_manager=auth)


def test_fenced_owner_cannot_create_manual_queued_run(tmp_path, monkeypatch):
    factory, ScheduledTask, TaskRun, engine = _database(tmp_path, monkeypatch)
    with factory() as db:
        db.add(ScheduledTask(id="alice-task", owner="alice", name="A", status="active"))
        db.commit()
    scheduler = _scheduler({"alice"})

    assert asyncio.run(scheduler.run_task_now("alice-task")) is False
    asyncio.run(scheduler._execute_task("alice-task"))

    with factory() as db:
        assert db.query(TaskRun).count() == 0
    engine.dispose()


def test_runtime_manifest_allows_restart_expiry_but_rejects_new_state():
    scheduler = _scheduler({"alice"})
    scheduler._pending_notifications = [{"owner": "alice", "task_id": "old-task"}]
    manifest = scheduler.preview_owner_runtime_rename("alice", "carol")
    restarted = _scheduler({"alice", "carol"})
    receipt = restarted.reconcile_owner_runtime("alice", "carol", manifest)
    assert receipt["state"] == "expired_on_restart"
    restarted._pending_notifications = [{"owner": "alice", "task_id": "late-task"}]
    with pytest.raises(RuntimeError, match="after restart"):
        restarted.reconcile_owner_runtime("alice", "carol", manifest)


def test_quiesce_joins_owner_handles_and_preserves_other_owner(tmp_path, monkeypatch):
    factory, ScheduledTask, TaskRun, engine = _database(tmp_path, monkeypatch)
    now = datetime(2026, 8, 31)
    with factory() as db:
        db.add_all(
            [
                ScheduledTask(id="alice-task", owner="alice", name="A", status="active"),
                ScheduledTask(id="bob-task", owner="bob", name="B", status="active"),
                TaskRun(id="alice-run", task_id="alice-task", started_at=now, status="running"),
                TaskRun(id="bob-run", task_id="bob-task", started_at=now, status="running"),
            ]
        )
        db.commit()

    async def drive():
        scheduler = _scheduler({"alice"})
        scheduler._pending_notifications = [
            {"owner": "alice", "kind": "task", "body": "private-a"},
            {"owner": "bob", "kind": "task", "body": "private-b"},
        ]
        entered = asyncio.Event()

        async def running():
            entered.set()
            await asyncio.Event().wait()

        handle = asyncio.create_task(running())
        scheduler._register_task_handle("alice-task", handle)
        scheduler._executing.update({"alice-task", "bob-task"})
        await entered.wait()
        receipt = await scheduler.quiesce_owner_lifecycle("alice")
        assert handle.done()
        assert receipt["executions_cancelled"] == 1
        assert "alice-task" not in scheduler._executing
        assert "bob-task" in scheduler._executing

        bob_before = scheduler.owner_runtime_inventory("bob")
        moved = scheduler.reconcile_owner_runtime("alice", "deleted:alice-id")
        assert moved["moved"] == 1
        assert scheduler.owner_runtime_inventory("alice")["count"] == 0
        assert scheduler.owner_runtime_inventory("bob") == bob_before
        restored = scheduler.compensate_owner_runtime("alice", "deleted:alice-id")
        assert restored["state"] == "restored"
        scheduler.reconcile_owner_runtime("alice", "deleted:alice-id")
        purged = scheduler.purge_owner_runtime("deleted:alice-id")
        assert purged["removed"] == 1
        assert scheduler.owner_runtime_inventory("bob") == bob_before

    asyncio.run(drive())
    with factory() as db:
        assert db.get(TaskRun, "alice-run").status == "aborted"
        assert db.get(TaskRun, "bob-run").status == "running"
    engine.dispose()

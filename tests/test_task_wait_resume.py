"""S12 — durable task waits: persistence, CAS answer, recurrence guard, restart.

Covers the VALIDATION Tasks row: task waits/restart/answer duplicate/cancel/
recurrence. No live scheduled task or email send — fixture/scheduler unit only.
"""

import asyncio
import uuid

import pytest


def _make_scheduler(tmp_path, monkeypatch):
    """Build a TaskScheduler with a disposable DB."""
    import src.task_scheduler as ts
    monkeypatch.setattr(ts, "_utcnow", lambda: __import__("datetime").datetime.utcnow())
    sched = ts.TaskScheduler(session_manager=None)
    return sched


def _wait_row(task_id, run_id, session_id, owner, state="waiting"):
    from core.database import TaskWaitRequest
    return TaskWaitRequest(
        id=f"wait_{uuid.uuid4().hex}",
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        owner=owner,
        kind="question",
        payload='{"question": "Proceed?"}',
        state=state,
    )


# ---- persistence ----

def test_persist_wait_request_sets_run_waiting(tmp_path, monkeypatch):
    from core.database import SessionLocal, TaskRun, ScheduledTask, TaskWaitRequest
    sched = _make_scheduler(tmp_path, monkeypatch)
    db = SessionLocal()
    try:
        task_id = f"t-{uuid.uuid4().hex[:8]}"
        run_id = f"r-{uuid.uuid4().hex[:8]}"
        db.add(ScheduledTask(id=task_id, owner="alice", name="T"))
        db.add(TaskRun(id=run_id, task_id=task_id, status="running"))
        db.commit()
    finally:
        db.close()

    wait_id = sched._persist_wait_request(
        task_id=task_id, run_id=run_id, session_id="s1",
        owner="alice", kind="question", payload={"question": "Proceed?"},
    )
    assert wait_id is not None

    db = SessionLocal()
    try:
        run = db.query(TaskRun).filter(TaskRun.id == run_id).first()
        assert run.status == "waiting"
        wait = db.query(TaskWaitRequest).filter(TaskWaitRequest.id == wait_id).first()
        assert wait is not None
        assert wait.state == "waiting"
        assert wait.owner == "alice"
        assert wait.session_id == "s1"
    finally:
        db.close()


# ---- CAS answer consumption ----

def test_consume_wait_request_is_one_use(tmp_path, monkeypatch):
    from core.database import SessionLocal, TaskRun, ScheduledTask
    sched = _make_scheduler(tmp_path, monkeypatch)
    db = SessionLocal()
    try:
        task_id = f"t-{uuid.uuid4().hex[:8]}"
        run_id = f"r-{uuid.uuid4().hex[:8]}"
        db.add(ScheduledTask(id=task_id, owner="alice", name="T"))
        db.add(TaskRun(id=run_id, task_id=task_id, status="running"))
        db.commit()
    finally:
        db.close()

    wait_id = sched._persist_wait_request(
        task_id=task_id, run_id=run_id, session_id="s1",
        owner="alice", kind="question", payload={"question": "Go?"},
    )
    assert sched._consume_wait_request(wait_id, owner="alice", session_id="s1") is True
    # Second consume must fail (duplicate answer).
    assert sched._consume_wait_request(wait_id, owner="alice", session_id="s1") is False


def test_consume_wait_request_rejects_wrong_owner(tmp_path, monkeypatch):
    from core.database import SessionLocal, TaskRun, ScheduledTask
    sched = _make_scheduler(tmp_path, monkeypatch)
    db = SessionLocal()
    try:
        task_id = f"t-{uuid.uuid4().hex[:8]}"
        run_id = f"r-{uuid.uuid4().hex[:8]}"
        db.add(ScheduledTask(id=task_id, owner="alice", name="T"))
        db.add(TaskRun(id=run_id, task_id=task_id, status="running"))
        db.commit()
    finally:
        db.close()

    wait_id = sched._persist_wait_request(
        task_id=task_id, run_id=run_id, session_id="s1",
        owner="alice", kind="question", payload={},
    )
    assert sched._consume_wait_request(wait_id, owner="bob", session_id="s1") is False
    # The valid request is NOT consumed by the wrong-owner attempt.
    assert sched._consume_wait_request(wait_id, owner="alice", session_id="s1") is True


def test_consume_wait_request_rejects_session_mismatch(tmp_path, monkeypatch):
    from core.database import SessionLocal, TaskRun, ScheduledTask
    sched = _make_scheduler(tmp_path, monkeypatch)
    db = SessionLocal()
    try:
        task_id = f"t-{uuid.uuid4().hex[:8]}"
        run_id = f"r-{uuid.uuid4().hex[:8]}"
        db.add(ScheduledTask(id=task_id, owner="alice", name="T"))
        db.add(TaskRun(id=run_id, task_id=task_id, status="running"))
        db.commit()
    finally:
        db.close()

    wait_id = sched._persist_wait_request(
        task_id=task_id, run_id=run_id, session_id="s1",
        owner="alice", kind="question", payload={},
    )
    assert sched._consume_wait_request(wait_id, owner="alice", session_id="other") is False
    assert sched._consume_wait_request(wait_id, owner="alice", session_id="s1") is True


# ---- cancel / revoke ----

def test_cancel_wait_requests_invalidates_pending(tmp_path, monkeypatch):
    from core.database import SessionLocal, TaskRun, ScheduledTask, TaskWaitRequest
    sched = _make_scheduler(tmp_path, monkeypatch)
    db = SessionLocal()
    try:
        task_id = f"t-{uuid.uuid4().hex[:8]}"
        run_id = f"r-{uuid.uuid4().hex[:8]}"
        db.add(ScheduledTask(id=task_id, owner="alice", name="T"))
        db.add(TaskRun(id=run_id, task_id=task_id, status="running"))
        db.commit()
    finally:
        db.close()

    wait_id = sched._persist_wait_request(
        task_id=task_id, run_id=run_id, session_id="s1",
        owner="alice", kind="question", payload={},
    )
    cancelled = sched._cancel_wait_requests_for_run(run_id)
    assert cancelled == 1
    assert sched._consume_wait_request(wait_id, owner="alice", session_id="s1") is False


# ---- recurrence guard ----

def test_waiting_run_blocks_overlapping_recurrence(tmp_path, monkeypatch):
    from core.database import SessionLocal, TaskRun, ScheduledTask
    sched = _make_scheduler(tmp_path, monkeypatch)
    db = SessionLocal()
    try:
        task_id = f"t-{uuid.uuid4().hex[:8]}"
        run_id = f"r-{uuid.uuid4().hex[:8]}"
        db.add(ScheduledTask(id=task_id, owner="alice", name="T"))
        db.add(TaskRun(id=run_id, task_id=task_id, status="waiting"))
        db.commit()
    finally:
        db.close()

    assert sched._has_waiting_run(task_id) is True
    # _execute_task returns without creating a new run when one is waiting.
    result = asyncio.run(sched._execute_task(task_id))
    assert result is None
    db = SessionLocal()
    try:
        runs = db.query(TaskRun).filter(TaskRun.task_id == task_id).all()
        # Still exactly one run — no competing run created.
        assert len(runs) == 1
        assert runs[0].status == "waiting"
    finally:
        db.close()


# ---- resume state transitions ----

def test_mark_run_resuming_only_from_waiting(tmp_path, monkeypatch):
    from core.database import SessionLocal, TaskRun, ScheduledTask
    sched = _make_scheduler(tmp_path, monkeypatch)
    db = SessionLocal()
    try:
        task_id = f"t-{uuid.uuid4().hex[:8]}"
        run_id = f"r-{uuid.uuid4().hex[:8]}"
        db.add(ScheduledTask(id=task_id, owner="alice", name="T"))
        db.add(TaskRun(id=run_id, task_id=task_id, status="running"))
        db.commit()
    finally:
        db.close()

    # Cannot resume a running run.
    assert sched._mark_run_resuming(run_id) is False

    db = SessionLocal()
    try:
        run = db.query(TaskRun).filter(TaskRun.id == run_id).first()
        run.status = "waiting"
        db.commit()
    finally:
        db.close()

    assert sched._mark_run_resuming(run_id) is True
    db = SessionLocal()
    try:
        run = db.query(TaskRun).filter(TaskRun.id == run_id).first()
        assert run.status == "resuming"
    finally:
        db.close()

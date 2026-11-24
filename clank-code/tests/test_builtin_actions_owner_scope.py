"""Regression tests for owner-scoped model resolution in scheduled actions."""

import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest


class _Column:
    def __eq__(self, _other):
        return True

    def __ne__(self, _other):
        return True

    def __ge__(self, _other):
        return True

    def __le__(self, _other):
        return True


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def limit(self, _limit):
        return self

    def all(self):
        return list(self._rows)


class _Db:
    def __init__(self, rows_by_model):
        self._rows_by_model = rows_by_model
        self.commits = 0
        self.closed = False

    def query(self, model):
        return _Query(self._rows_by_model.get(model, []))

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_classify_events_resolves_llm_for_task_owner(monkeypatch):
    from core import database
    from src.openclank import modality_facade
    from src.builtin_actions import action_classify_events

    class FakeCalendarEvent:
        dtstart = _Column()
        status = _Column()

    event = SimpleNamespace(
        summary="Demo presentation",
        event_type="work",
        importance="high",
        color=None,
        dtstart=datetime(2026, 1, 1, 9, 0, 0),
        location="",
    )
    db = _Db({FakeCalendarEvent: [event]})
    calls = []

    def managed_route_summary(**kwargs):
        calls.append(kwargs)
        return {"model_route_id": "pmr-tasks"}

    monkeypatch.setattr(database, "CalendarEvent", FakeCalendarEvent)
    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(modality_facade, "managed_route_summary", managed_route_summary)

    message, ok = await action_classify_events("alice")

    assert ok is True
    assert "Scanned 1 upcoming event" in message
    assert calls == [{
        "owner": "alice",
        "purpose": "tasks",
        "operation": "chat.complete",
    }]
    assert db.closed is True


@pytest.mark.asyncio
async def test_classify_events_managed_completion_keeps_task_root(monkeypatch):
    from core import database
    from src import builtin_actions
    from src.openclank import modality_facade

    class FakeCalendarEvent:
        dtstart = _Column()
        status = _Column()

    event = SimpleNamespace(
        summary="Project sync",
        event_type=None,
        importance="normal",
        color=None,
        dtstart=datetime(2026, 1, 1, 9, 0, 0),
        location="",
    )
    db = _Db({FakeCalendarEvent: [event]})
    captured = {}

    async def complete_text(**kwargs):
        captured.update(kwargs)
        return '[{"i":0,"type":"work","importance":"normal"}]'

    async def ready(_label):
        return None

    monkeypatch.setattr(database, "CalendarEvent", FakeCalendarEvent)
    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        modality_facade,
        "managed_route_summary",
        lambda **_kwargs: {"model_route_id": "pmr-tasks"},
    )
    monkeypatch.setattr(modality_facade, "complete_text", complete_text)
    monkeypatch.setattr(builtin_actions, "wait_for_interactive_quiet", ready)

    message, ok = await builtin_actions.action_classify_events(
        "alice",
        model_route_id="pmr-tasks",
        grant_id="grant-tasks",
        root_operation_id="turn-task-run:calendar",
    )

    assert ok is True
    assert "1 via LLM" in message
    assert captured["owner"] == "alice"
    assert captured["purpose"] == "tasks"
    assert captured["model_route_id"] == "pmr-tasks"
    assert captured["grant_id"] == "grant-tasks"
    assert captured["root_operation_id"] == "turn-task-run:calendar"


@pytest.mark.asyncio
async def test_scheduled_skill_test_keeps_utility_route_and_root(monkeypatch):
    from routes import skills_routes
    from services.memory import skills as skills_module
    from src.builtin_actions import action_test_skills

    route = SimpleNamespace(provider_model_id="worker-model")
    captured = {}

    class FakeSkillsManager:
        def __init__(self, _data_dir):
            pass

        def load(self, *, owner):
            assert owner == "alice"
            return [{"name": "safe-skill", "description": "A safe skill"}]

        def read_skill_md(self, name, *, owner):
            assert (name, owner) == ("safe-skill", "alice")
            return "---\nname: safe-skill\n---\nUse the fixture."

        def set_audit(self, name, verdict, **kwargs):
            captured["audit"] = (name, verdict, kwargs)

    async def run_once(md, task, selected_route, owner, **kwargs):
        captured.update({
            "md": md,
            "task": task,
            "route": selected_route,
            "owner": owner,
            **kwargs,
        })
        return "completed fixture", {
            "verdict": "pass",
            "summary": "worked",
        }

    monkeypatch.setattr(skills_module, "SkillsManager", FakeSkillsManager)
    monkeypatch.setattr(skills_routes, "_bound_skill_route", lambda owner, purpose: route)
    monkeypatch.setattr(skills_routes, "_skill_test_task", lambda _skill: "test task")
    monkeypatch.setattr(skills_routes, "_run_skill_test_once", run_once)

    message, ok = await action_test_skills(
        "alice",
        root_operation_id="turn-task-run:skill-test",
    )

    assert ok is True
    assert "model=worker-model" in message
    assert captured["route"] is route
    assert captured["owner"] == "alice"
    assert captured["root_operation_id"] == "turn-task-run:skill-test"


@pytest.mark.asyncio
async def test_scheduled_skill_audit_keeps_normalized_routes_and_root(monkeypatch):
    from routes import skills_routes
    from services.memory import skills as skills_module
    from src.builtin_actions import action_audit_skills

    worker = SimpleNamespace(provider_model_id="worker-model")
    teacher = SimpleNamespace(provider_model_id="teacher-model")
    captured = {}

    class FakeSkillsManager:
        def __init__(self, _data_dir):
            pass

        def load(self, *, owner):
            assert owner == "alice"
            return [{"name": "needs-audit", "audit_verdict": None}]

    jobs = {}

    async def run_job(key, manager, names, worker_route, teacher_route, owner, **kwargs):
        captured.update({
            "key": key,
            "manager": manager,
            "names": names,
            "worker_route": worker_route,
            "teacher_route": teacher_route,
            "owner": owner,
            **kwargs,
        })
        jobs[key].update({
            "status": "done",
            "done": 1,
            "results": [{"result": "pass"}],
        })

    monkeypatch.setattr(skills_module, "SkillsManager", FakeSkillsManager)
    monkeypatch.setattr(skills_routes, "_skill_audit_jobs", jobs)
    monkeypatch.setattr(
        skills_routes,
        "_resolve_audit_models",
        lambda owner: (worker, teacher),
    )
    monkeypatch.setattr(skills_routes, "_run_audit_all_job", run_job)

    message, ok = await action_audit_skills(
        "alice",
        root_operation_id="turn-task-run:skill-audit",
    )

    assert ok is True
    assert "Audited 1/1" in message
    assert captured["worker_route"] is worker
    assert captured["teacher_route"] is teacher
    assert captured["root_operation_id"] == "turn-task-run:skill-audit"


@pytest.mark.asyncio
async def test_learn_sender_signatures_resolves_llm_for_task_owner(monkeypatch):
    from routes import email_helpers
    from src.openclank import modality_facade
    from src.builtin_actions import action_learn_sender_signatures

    class FakeImap:
        def __init__(self, owner=""):
            self.owner = owner

        def select(self, *_args, **_kwargs):
            return "OK", []

        def uid(self, command, *_args):
            if command == "SEARCH":
                return "OK", [b"1 2 3"]
            return "OK", [(None, b"From: Writer <writer@example.com>\r\n\r\n")]

        def logout(self):
            return None

    calls = []
    imap_owners = []

    def fake_imap_connect(_account_id=None, owner=""):
        imap_owners.append(owner)
        return FakeImap(owner)

    monkeypatch.setattr(email_helpers, "_imap_connect", fake_imap_connect)
    monkeypatch.setattr(
        modality_facade,
        "managed_route_summary",
        lambda **kwargs: calls.append(kwargs),
    )

    message, ok = await action_learn_sender_signatures("alice")

    assert ok is False
    assert message == "No managed utility route available"
    assert calls == [{
        "owner": "alice",
        "purpose": "utility",
        "operation": "chat.complete",
    }]
    assert imap_owners == ["alice"]


@pytest.mark.asyncio
async def test_learn_sender_signatures_writes_owner_scoped_cache(monkeypatch, tmp_path):
    from routes import email_helpers
    from src.openclank import modality_facade
    from src.builtin_actions import action_learn_sender_signatures

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO sender_signatures
            (from_address, owner, signature_text, sample_count, last_built_at, model_used, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "writer@example.com",
                "bob",
                "bob cached signature",
                3,
                "2999-01-01T00:00:00",
                "old-model",
                "llm",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    class FakeImap:
        def select(self, *_args, **_kwargs):
            return "OK", []

        def uid(self, command, uid=None, query=None):
            if command == "SEARCH":
                return "OK", [b"1 2 3"]
            if query and "HEADER.FIELDS" in query:
                return "OK", [(None, b"From: Writer <writer@example.com>\r\n\r\n")]
            return "OK", [
                (
                    None,
                    (
                        b"Thanks for the update.\r\n\r\n"
                        b"Regards,\r\n"
                        b"Writer Example\r\n"
                        b"Example Co.\r\n"
                        + str(uid).encode()
                    ),
                )
            ]

        def logout(self):
            return None

    imap_owners = []

    def fake_imap_connect(_account_id=None, owner=""):
        imap_owners.append(owner)
        return FakeImap()

    monkeypatch.setattr(email_helpers, "_imap_connect", fake_imap_connect)
    monkeypatch.setattr(
        modality_facade,
        "managed_route_summary",
        lambda **_kwargs: {"model_route_id": "route-1", "model_id": "alice-model"},
    )

    async def fake_complete_text(**_kwargs):
        return "Writer Example\nExample Co.\nwriter@example.com"

    monkeypatch.setattr(modality_facade, "complete_text", fake_complete_text)

    message, ok = await action_learn_sender_signatures("alice")

    assert ok is True
    assert message.startswith("Learned sigs: 1 found")
    assert imap_owners == ["alice", "alice"]

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT owner, signature_text, model_used
            FROM sender_signatures
            WHERE from_address = ?
            ORDER BY owner
            """,
            ("writer@example.com",),
        ).fetchall()
    finally:
        conn.close()

    assert rows == [
        ("alice", "Writer Example\nExample Co.\nwriter@example.com", "alice-model"),
        ("bob", "bob cached signature", "old-model"),
    ]


@pytest.mark.asyncio
async def test_check_email_urgency_is_owner_scoped_without_provider_resolution(monkeypatch, tmp_path):
    from core import database
    from src.builtin_actions import TaskNoop, action_check_email_urgency

    class FakeEmailAccount:
        enabled = _Column()
        owner = _Column()
        imap_user = _Column()
        from_address = _Column()

    db = _Db({FakeEmailAccount: []})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(database, "EmailAccount", FakeEmailAccount)
    monkeypatch.setattr(database, "SessionLocal", lambda: db)

    with pytest.raises(TaskNoop, match="no email accounts configured"):
        await action_check_email_urgency("alice")

    assert db.closed is True

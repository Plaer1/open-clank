"""Stable file-policy Workspace identity for scheduled/background tasks."""

from __future__ import annotations

import inspect
import json
import sqlite3
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core import database
from core.database import ScheduledTask
from routes import file_policy_routes, task_routes
from src.openclank.file_policy import FilePolicyRepository
from src.task_scheduler import _ManagedTaskRoute, TaskScheduler
from src.tools.system import do_manage_tasks


class _Auth:
    def account_id(self, username):
        return {"alice": "alice-id", "bob": "bob-id"}.get(str(username or ""))

    def is_admin(self, username):
        return username == "admin"


def _request(username: str = "alice"):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=username, api_token=False),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=_Auth())),
    )


def _workspace_policy(tmp_path):
    repository = FilePolicyRepository(tmp_path / "file-policy.db")
    root = tmp_path / "assigned"
    root.mkdir()
    project = root / "project"
    project.mkdir()
    location = repository.create_location(
        actor_subject_id="admin-id",
        path=str(root),
        kind="directory",
        capabilities=("read", "write"),
    )
    repository.create_binding(
        actor_subject_id="admin-id",
        binding_class="people",
        subject_id="alice-id",
        location_id=location.id,
        capabilities=("read",),
    )
    agent = repository.create_binding(
        actor_subject_id="admin-id",
        binding_class="agent",
        subject_id="alice-id",
        location_id=location.id,
        capabilities=("read",),
    )
    workspace = repository.create_workspace(
        actor_subject_id="alice-id",
        owner_subject_id="alice-id",
        location_id=location.id,
        name="Project",
        relative_folder="project",
    )
    return repository, workspace, agent, project


def _endpoint(router, method: str, path: str):
    return next(
        route.endpoint
        for route in router.routes
        if route.path == path and method in route.methods
    )


@pytest.mark.parametrize("with_model_endpoints", [True, False])
def test_task_workspace_migration_survives_legacy_rebuild_and_is_idempotent(
    monkeypatch,
    tmp_path,
    with_model_endpoints,
):
    path = tmp_path / f"legacy-tasks-{int(with_model_endpoints)}.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if with_model_endpoints:
            connection.execute(
                "CREATE TABLE model_endpoints (id VARCHAR PRIMARY KEY)"
            )
        connection.executescript(
            """
            CREATE TABLE sessions (id VARCHAR PRIMARY KEY);
            CREATE TABLE scheduled_tasks (
                id VARCHAR PRIMARY KEY,
                owner VARCHAR,
                name VARCHAR NOT NULL,
                prompt TEXT NOT NULL,
                schedule VARCHAR NOT NULL,
                scheduled_time VARCHAR NOT NULL,
                scheduled_day INTEGER,
                scheduled_date DATETIME,
                next_run DATETIME,
                last_run DATETIME,
                status VARCHAR,
                output_target VARCHAR,
                session_id VARCHAR,
                model VARCHAR,
                endpoint_url VARCHAR,
                workspace VARCHAR,
                workspace_id VARCHAR,
                run_count INTEGER,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                task_type VARCHAR DEFAULT 'llm',
                action VARCHAR,
                trigger_type VARCHAR DEFAULT 'schedule',
                trigger_event VARCHAR,
                trigger_count INTEGER,
                trigger_counter INTEGER DEFAULT 0
            );
            INSERT INTO scheduled_tasks VALUES (
                'task-1', 'alice', 'Legacy', 'work', 'daily', '09:00',
                NULL, NULL, NULL, NULL, 'active', 'session', NULL, NULL, NULL,
                '/legacy/project', 'workspace-stable', 0,
                '2026-01-01', '2026-01-01', 'llm', NULL, 'schedule', NULL,
                NULL, 0
            );
            CREATE TABLE task_runs (
                id VARCHAR PRIMARY KEY,
                task_id VARCHAR NOT NULL,
                started_at DATETIME NOT NULL,
                FOREIGN KEY(task_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE
            );
            INSERT INTO task_runs VALUES ('run-1', 'task-1', '2026-01-01');
            """
        )

    migration_engine = create_engine(f"sqlite:///{path}")
    monkeypatch.setattr(database, "engine", migration_engine)
    try:
        database._migrate_add_task_automation_columns()
        database._migrate_add_task_automation_columns()
        database._migrate_add_task_workspace_id_column()
        database._migrate_add_task_workspace_id_column()
        with migration_engine.connect() as connection:
            restored_foreign_keys = connection.exec_driver_sql(
                "PRAGMA foreign_keys"
            ).scalar_one()
    finally:
        migration_engine.dispose()

    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(scheduled_tasks)"
            ).fetchall()
        }
        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(scheduled_tasks)"
            ).fetchall()
        }
        row = connection.execute(
            "SELECT id, workspace, workspace_id FROM scheduled_tasks"
        ).fetchone()
        run = connection.execute(
            "SELECT id, task_id FROM task_runs"
        ).fetchone()
        task_run_fk_target = connection.execute(
            "PRAGMA foreign_key_list(task_runs)"
        ).fetchone()[2]
        task_foreign_keys = {
            (foreign_key[3], foreign_key[2])
            for foreign_key in connection.execute(
                "PRAGMA foreign_key_list(scheduled_tasks)"
            ).fetchall()
        }
        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert {"workspace", "workspace_id"} <= columns
    expected_indexes = {
        index.name
        for index in ScheduledTask.__table__.indexes
        if index.name
    }
    assert expected_indexes <= indexes
    assert row == ("task-1", "/legacy/project", "workspace-stable")
    assert run == ("run-1", "task-1")
    assert task_run_fk_target == "scheduled_tasks"
    assert {
        ("session_id", "sessions"),
        ("then_task_id", "scheduled_tasks"),
    } <= task_foreign_keys
    if with_model_endpoints:
        assert ("endpoint_id", "model_endpoints") in task_foreign_keys
    else:
        assert ("endpoint_id", "model_endpoints") not in task_foreign_keys
    assert foreign_key_violations == []
    assert restored_foreign_keys == 1

    source = inspect.getsource(database.init_db)
    automation = source.index("_migrate_add_task_automation_columns()")
    contract = source.index("_migrate_add_background_agent_contract_columns()")
    stable = source.index("_migrate_add_task_workspace_id_column()")
    assert automation < contract < stable


@pytest.mark.asyncio
async def test_task_api_accepts_owned_stable_id_and_revalidates_on_update(
    monkeypatch,
    tmp_path,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tasks.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    repository, workspace, agent, project = _workspace_policy(tmp_path)
    monkeypatch.setattr(task_routes, "SessionLocal", sessions)
    monkeypatch.setattr(task_routes, "FilePolicyRepository", lambda: repository)
    monkeypatch.setattr(
        task_routes,
        "owner_has_admin_task_privileges",
        lambda owner: False,
    )

    router = task_routes.setup_task_routes(MagicMock())
    create = _endpoint(router, "POST", "/api/tasks")
    update = _endpoint(router, "PUT", "/api/tasks/{task_id}")

    created = await create(
        _request(),
        task_routes.TaskCreate(
            name="Workspace task",
            prompt="Inspect the project",
            trigger_type="webhook",
            workspace_id=workspace.id,
            allowed_tools=[],
        ),
    )
    assert created["workspace_id"] == workspace.id
    assert created["workspace"] is None

    db = sessions()
    try:
        stored = db.query(ScheduledTask).filter(ScheduledTask.id == created["id"]).one()
        assert stored.workspace_id == workspace.id
        assert stored.workspace is None
    finally:
        db.close()

    with pytest.raises(HTTPException) as raw_denied:
        await create(
            _request(),
            task_routes.TaskCreate(
                name="Raw path task",
                prompt="Inspect it",
                trigger_type="webhook",
                workspace=str(project),
                allowed_tools=[],
            ),
        )
    assert raw_denied.value.status_code == 403

    repository.revoke_binding(agent.id, actor_subject_id="admin-id")
    with pytest.raises(HTTPException) as revoked:
        await update(
            _request(),
            created["id"],
            task_routes.TaskUpdate(name="Must revalidate"),
        )
    assert revoked.value.status_code == 403


@pytest.mark.asyncio
async def test_scheduler_re_resolves_before_spawn_and_sends_stable_authority(
    monkeypatch,
    tmp_path,
):
    repository, workspace, agent, project = _workspace_policy(tmp_path)
    import src.openclank.file_policy as file_policy

    monkeypatch.setattr(file_policy, "FilePolicyRepository", lambda: repository)
    captured = {"calls": 0}
    revoke_during_wait = {"enabled": False}

    async def stream(_target, **kwargs):
        captured["calls"] += 1
        captured.update(kwargs)
        yield 'data: {"delta":"done"}\n\ndata: [DONE]\n\n'

    async def ready(_label):
        if revoke_during_wait["enabled"]:
            # FilePolicyRepository's mutation helpers refer to their class for
            # shared generation state, so briefly restore it for the revoke.
            monkeypatch.setattr(
                file_policy,
                "FilePolicyRepository",
                FilePolicyRepository,
            )
            repository.revoke_binding(agent.id, actor_subject_id="admin-id")
            monkeypatch.setattr(
                file_policy,
                "FilePolicyRepository",
                lambda: repository,
            )
        return None

    monkeypatch.setattr("src.model_dispatch.stream_agent_target", stream)
    monkeypatch.setattr("src.interactive_gate.wait_for_interactive_quiet", ready)
    scheduler = TaskScheduler(session_manager=None, auth_manager=_Auth())
    task = SimpleNamespace(
        id="task-stable",
        name="Stable task",
        prompt="inspect",
        owner="alice",
        workspace=None,
        workspace_id=workspace.id,
        copal_workspace="default",
        allowed_tools="[]",
        max_steps=2,
        max_tool_calls=2,
    )
    route = _ManagedTaskRoute(
        model_route_id="pmr-task",
        connection_id="pcn-task",
        provider_model_id="model",
        public_endpoint_id="pcn-task",
        capabilities={"tools": True},
    )

    result = await scheduler._run_agent_loop(
        "ignored",
        "ignored",
        task,
        "task-session",
        managed_route=route,
        root_operation_id="turn-task-run:stable",
    )
    assert result == "done"
    assert captured["cwd"] == str(project)
    assert captured["turn_envelope"]["workspace"] == str(project)
    assert (
        captured["turn_envelope"]["authority_workspace_id"]
        == workspace.id
    )

    # The second run passes its setup-time resolution, then loses Agent/read
    # authority while blocked at the final interactive gate. It must fail at
    # the structural pre-spawn recheck and never call the stream worker.
    revoke_during_wait["enabled"] = True
    with pytest.raises(RuntimeError, match="TASK_WORKSPACE_UNAVAILABLE"):
        await scheduler._run_agent_loop(
            "ignored",
            "ignored",
            task,
            "task-session",
            managed_route=route,
            root_operation_id="turn-task-run:revoked",
        )
    assert captured["calls"] == 1


@pytest.mark.asyncio
async def test_task_output_session_is_stamped_without_duplicate_insert(
    monkeypatch,
    tmp_path,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'outputs.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import core.session_manager as session_manager_module
    from core.session_manager import SessionManager

    monkeypatch.setattr(session_manager_module, "SessionLocal", sessions)
    manager = SessionManager.__new__(SessionManager)
    manager.sessions = {}
    manager.upload_handler = None
    manager.ensure_task_session = MagicMock(
        side_effect=AssertionError(
            "must not re-insert an already-created DB session"
        )
    )
    scheduler = TaskScheduler(session_manager=manager)
    scheduler._mint_session_id = AsyncMock(return_value="task-output")
    task = SimpleNamespace(
        id="task-1",
        name="Output",
        prompt="Do work",
        task_type="action",
        action="visible-action",
        output_target="session",
        session_id=None,
        endpoint_id=None,
        provider_model_route_id=None,
        model=None,
        owner="alice",
        workspace_id="workspace-stable",
        crew_member_id=None,
    )
    db = sessions()
    try:
        await scheduler._deliver_task_result(task, "done", db)
        rows = db.query(database.Session).all()
        messages = db.query(database.ChatMessage).filter(
            database.ChatMessage.session_id == "task-output"
        ).all()
    finally:
        db.close()

    assert len(rows) == 1
    assert rows[0].id == "task-output"
    assert rows[0].workspace_id == "workspace-stable"
    assert manager.sessions["task-output"].workspace_id == "workspace-stable"
    assert len(manager.sessions["task-output"].history) == 2
    assert len(messages) == 2
    manager.ensure_task_session.assert_not_called()


@pytest.mark.asyncio
async def test_force_runs_serialize_the_same_task_output_claim(monkeypatch, tmp_path):
    """Forced runs may bypass the global slot, never the per-task fence."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'force-runs.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    db = sessions()
    try:
        db.add(ScheduledTask(
            id="same-task",
            owner="alice",
            name="Same task",
            status="active",
        ))
        db.commit()
    finally:
        db.close()

    scheduler = TaskScheduler(session_manager=None)
    active = 0
    maximum_active = 0
    entered_first = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    monkeypatch.setattr(scheduler, "_task_needs_model_slot", lambda _task_id: True)

    async def execute_locked(
        _task_id,
        _run_id,
        *,
        release_executing,
        gate_foreground,
    ):
        nonlocal active, maximum_active, calls
        calls += 1
        active += 1
        maximum_active = max(maximum_active, active)
        if calls == 1:
            entered_first.set()
            await release_first.wait()
        active -= 1

    monkeypatch.setattr(scheduler, "_execute_task_locked", execute_locked)
    first = asyncio.create_task(
        scheduler._execute_task(
            "same-task",
            bypass_model_slot=True,
            release_executing=False,
        )
    )
    await entered_first.wait()
    second = asyncio.create_task(
        scheduler._execute_task(
            "same-task",
            bypass_model_slot=True,
            release_executing=False,
        )
    )
    await asyncio.sleep(0)
    assert calls == 1
    release_first.set()
    await asyncio.gather(first, second)

    assert calls == 2
    assert maximum_active == 1
    db = sessions()
    try:
        assert db.query(database.TaskRun).count() == 2
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_workspace_reset_pauses_db_first_and_cancels_all_task_handles(
    monkeypatch,
    tmp_path,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'task-reset.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    next_run = database.utcnow_naive() + timedelta(hours=1)
    db = sessions()
    try:
        db.add_all([
            ScheduledTask(
                id="running-target", owner="alice", name="Running",
                task_type="llm", status="active", workspace_id="workspace-1",
                next_run=next_run,
            ),
            ScheduledTask(
                id="queued-target", owner="alice", name="Queued",
                task_type="research", status="active", workspace_id="workspace-1",
                next_run=next_run,
            ),
            ScheduledTask(
                id="action-target", owner="alice", name="Action",
                task_type="action", action="tidy_sessions", status="active",
                workspace_id="workspace-1", next_run=next_run,
            ),
            ScheduledTask(
                id="already-paused", owner="alice", name="Paused",
                task_type="llm", status="paused", workspace_id="workspace-1",
                next_run=next_run,
            ),
            ScheduledTask(
                id="other-workspace", owner="alice", name="Other workspace",
                task_type="llm", status="active", workspace_id="workspace-2",
                next_run=next_run,
            ),
            ScheduledTask(
                id="other-owner", owner="bob", name="Other owner",
                task_type="llm", status="active", workspace_id="workspace-1",
                next_run=next_run,
            ),
        ])
        db.add_all([
            database.TaskRun(
                id="run-running", task_id="running-target",
                started_at=database.utcnow_naive(), status="running",
            ),
            database.TaskRun(
                id="run-queued", task_id="queued-target",
                started_at=database.utcnow_naive(), status="queued",
            ),
            database.TaskRun(
                id="run-unrelated", task_id="other-workspace",
                started_at=database.utcnow_naive(), status="running",
            ),
        ])
        db.commit()
    finally:
        db.close()

    scheduler = TaskScheduler(session_manager=None)
    running_ready = asyncio.Event()
    forced_ready = asyncio.Event()
    queued_ready = asyncio.Event()

    async def stale_running_worker():
        worker_db = sessions()
        task = worker_db.query(ScheduledTask).filter(
            ScheduledTask.id == "running-target"
        ).one()
        running_ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Emulate the real cancellation handler computing a future run from
            # stale ORM state. The reset's post-join DB fence must clear it.
            task.next_run = database.utcnow_naive() + timedelta(days=1)
            worker_db.commit()
            raise
        finally:
            worker_db.close()

    async def queued_worker():
        queued_ready.set()
        await asyncio.Event().wait()

    async def forced_same_task_worker():
        forced_ready.set()
        await asyncio.Event().wait()

    running_handle = asyncio.create_task(stale_running_worker())
    forced_handle = asyncio.create_task(forced_same_task_worker())
    queued_handle = asyncio.create_task(queued_worker())
    scheduler._register_task_handle("running-target", running_handle)
    scheduler._register_task_handle("running-target", forced_handle)
    scheduler._register_task_handle("queued-target", queued_handle)
    scheduler._executing.update({
        "running-target", "queued-target", "other-workspace",
    })
    await asyncio.gather(
        running_ready.wait(), forced_ready.wait(), queued_ready.wait()
    )

    counts = await scheduler.reset_file_authority(
        "alice",
        scope="workspace",
        workspace_id="workspace-1",
    )
    assert counts == {
        "selected": 4,
        "paused": 3,
        "runs_aborted": 2,
        "executions_cancelled": 3,
        "executing_cleared": 2,
        "chat_mapped": 0,
    }
    assert running_handle.cancelled()
    assert forced_handle.cancelled()
    assert queued_handle.cancelled()
    assert scheduler._executing == {"other-workspace"}

    db = sessions()
    try:
        tasks = {
            task.id: task
            for task in db.query(ScheduledTask).all()
        }
        runs = {
            run.id: run
            for run in db.query(database.TaskRun).all()
        }
    finally:
        db.close()
    for task_id in (
        "running-target", "queued-target", "action-target", "already-paused",
    ):
        assert tasks[task_id].status == "paused"
        assert tasks[task_id].next_run is None
    assert tasks["other-workspace"].status == "active"
    assert tasks["other-workspace"].next_run == next_run
    assert tasks["other-owner"].status == "active"
    assert runs["run-running"].status == "aborted"
    assert runs["run-queued"].status == "aborted"
    assert runs["run-unrelated"].status == "running"

    assert await scheduler.reset_file_authority(
        "alice",
        scope="workspace",
        workspace_id="workspace-1",
    ) == {
        "selected": 4,
        "paused": 0,
        "runs_aborted": 0,
        "executions_cancelled": 0,
        "executing_cleared": 0,
        "chat_mapped": 0,
    }
    engine.dispose()


@pytest.mark.asyncio
async def test_chat_and_all_agent_task_resets_require_owned_mapping(
    monkeypatch,
    tmp_path,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'task-reset-scopes.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    db = sessions()
    try:
        db.add_all([
            database.Session(
                id="owned-chat", name="Owned", endpoint_url="", model="",
                owner="alice",
            ),
            database.Session(
                id="foreign-chat", name="Foreign", endpoint_url="", model="",
                owner="bob",
            ),
        ])
        db.add_all([
            ScheduledTask(
                id="owned-chat-task", owner="alice", name="Owned output",
                task_type="action", action="tidy_sessions", status="active",
                session_id="owned-chat",
            ),
            ScheduledTask(
                id="foreign-chat-task", owner="alice", name="Foreign output",
                task_type="action", action="tidy_sessions", status="active",
                session_id="foreign-chat",
            ),
            ScheduledTask(
                id="agent-unbound", owner="alice", name="Agent",
                task_type="llm", status="active",
            ),
            ScheduledTask(
                id="shell-action", owner="alice", name="Shell",
                task_type="action", action="run_local", status="active",
            ),
            ScheduledTask(
                id="stable-action", owner="alice", name="Stable action",
                task_type="action", action="tidy_sessions", status="active",
                workspace_id="workspace-1",
            ),
            ScheduledTask(
                id="ordinary-action", owner="alice", name="Ordinary",
                task_type="action", action="tidy_sessions", status="active",
            ),
            ScheduledTask(
                id="bob-agent", owner="bob", name="Bob",
                task_type="llm", status="active",
            ),
        ])
        db.commit()
    finally:
        db.close()

    scheduler = TaskScheduler(session_manager=None)
    foreign = await scheduler.reset_file_authority(
        "alice", scope="chat", chat_id="foreign-chat"
    )
    assert foreign["chat_mapped"] == 0
    assert foreign["selected"] == 0

    owned = await scheduler.reset_file_authority(
        "alice", scope="chat", chat_id="owned-chat"
    )
    assert owned["chat_mapped"] == 1
    assert owned["selected"] == 1
    assert owned["paused"] == 1

    all_agent = await scheduler.reset_file_authority(
        "alice", scope="all_agent"
    )
    assert all_agent["selected"] == 3
    assert all_agent["paused"] == 3

    db = sessions()
    try:
        statuses = {
            task.id: task.status
            for task in db.query(ScheduledTask).all()
        }
    finally:
        db.close()
        engine.dispose()
    assert statuses["owned-chat-task"] == "paused"
    assert statuses["foreign-chat-task"] == "active"
    assert statuses["agent-unbound"] == "paused"
    assert statuses["shell-action"] == "paused"
    assert statuses["stable-action"] == "paused"
    assert statuses["ordinary-action"] == "active"
    assert statuses["bob-agent"] == "active"


@pytest.mark.asyncio
async def test_file_policy_location_reset_passes_only_owned_workspace_ids_to_tasks(
    monkeypatch,
    tmp_path,
):
    repository, workspace, agent, project = _workspace_policy(tmp_path)
    sibling = project.parent / "sibling"
    sibling.mkdir()
    sibling_workspace = repository.create_workspace(
        actor_subject_id="alice-id",
        owner_subject_id="alice-id",
        location_id=workspace.location_id,
        name="Sibling",
        relative_folder="sibling",
    )

    class CompatibilityStore:
        def reset_agent_permissions(self, **_scope):
            return 0

    class Scheduler:
        def __init__(self):
            self.calls = []

        async def reset_file_authority(self, owner, **scope):
            self.calls.append((owner, scope))
            return {
                "selected": 2,
                "paused": 2,
                "runs_aborted": 1,
                "executions_cancelled": 1,
                "executing_cleared": 1,
                "chat_mapped": 0,
            }

    scheduler = Scheduler()
    request = _request()
    request.app.state.task_scheduler = scheduler
    monkeypatch.setattr(
        file_policy_routes,
        "GrantStore",
        lambda _path: CompatibilityStore(),
    )
    monkeypatch.setattr(file_policy_routes, "close_all_clients", lambda: None)
    import src.agent_tools.filesystem_tools as filesystem_tools
    import src.shell_policy as shell_policy

    monkeypatch.setattr(
        filesystem_tools, "reject_file_approval_scope", lambda **_scope: 0
    )
    monkeypatch.setattr(
        shell_policy, "reject_shell_approval_scope", lambda **_scope: 0
    )

    router = file_policy_routes.setup_file_policy_routes(repository=repository)
    reset = _endpoint(router, "POST", "/api/file-policy/resets")
    result = await reset(
        file_policy_routes.ResetRequest(
            scope="location",
            location_id=workspace.location_id,
        ),
        request,
    )

    assert result["task_cascade"]["paused"] == 2
    assert scheduler.calls == [(
        "alice",
        {
            "scope": "location",
            "chat_id": None,
            "workspace_id": None,
            "location_workspace_ids": (
                workspace.id,
                sibling_workspace.id,
            ),
        },
    )]
    assert "legacy_location_path" not in scheduler.calls[0][1]
    assert repository.get_binding(agent.id).status == "revoked"
    assert any(
        binding.binding_class == "people" and binding.status == "active"
        for binding in repository.list_bindings(
            subject_id="alice-id",
            include_inactive=True,
        )
    )


@pytest.mark.asyncio
async def test_manage_tasks_copies_source_chat_stable_id_not_derived_path(
    monkeypatch,
    tmp_path,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'manage-tasks.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    db = sessions()
    try:
        db.add(database.ModelEndpoint(
            id="pcn-task",
            name="Test",
            base_url="http://localhost/v1",
            owner="alice",
        ))
        db.add(database.Session(
            id="source-chat",
            name="Source",
            endpoint_url="openclank://engine",
            endpoint_id="pcn-task",
            provider_model_route_id="pmr-task",
            model="model",
            owner="alice",
            workspace_id="workspace-stable",
        ))
        db.add(database.Session(
            id="legacy-source-chat",
            name="Legacy source",
            endpoint_url="openclank://engine",
            endpoint_id="pcn-task",
            provider_model_route_id="pmr-task",
            model="model",
            owner="alice",
            workspace_id=None,
        ))
        db.commit()
    finally:
        db.close()

    legacy_result = await do_manage_tasks(
        json.dumps({
            "action": "create",
            "name": "Unbound legacy scope",
            "prompt": "Inspect later",
            "task_type": "llm",
            "trigger_type": "schedule",
            "schedule": "daily",
            "scheduled_time": "09:00",
            "allowed_tools": [],
        }),
        owner="alice",
        session_id="legacy-source-chat",
    )
    assert legacy_result["exit_code"] == 0

    db = sessions()
    try:
        legacy_task = db.query(ScheduledTask).filter(
            ScheduledTask.id == legacy_result["task_id"]
        ).one()
        assert legacy_task.workspace_id is None
        assert legacy_task.workspace is None
    finally:
        db.close()

    result = await do_manage_tasks(
        json.dumps({
            "action": "create",
            "name": "Copied scope",
            "prompt": "Inspect later",
            "task_type": "llm",
            "trigger_type": "schedule",
            "schedule": "daily",
            "scheduled_time": "09:00",
            "allowed_tools": [],
        }),
        owner="alice",
        session_id="source-chat",
    )
    assert result["exit_code"] == 0

    db = sessions()
    try:
        task = db.query(ScheduledTask).filter(
            ScheduledTask.id == result["task_id"]
        ).one()
        assert task.workspace_id == "workspace-stable"
        assert task.workspace is None
    finally:
        db.close()

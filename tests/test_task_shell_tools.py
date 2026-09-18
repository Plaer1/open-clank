"""Scheduled tasks must be offered shell/file tools by default.

Regression for #4163: the task runner built `relevant_tools` from RAG output
plus ASSISTANT_ALWAYS_AVAILABLE, neither of which includes bash/python. On a
host with an empty/degraded tool-embedding index, RAG returns nothing, so a
task agent never received the shell — even for an admin owner. The fix offers
the shell/file group by default and lets stream_agent_loop's owner gate decide
who actually keeps it.
"""

import inspect
import sqlite3

import pytest

from types import SimpleNamespace

from src.task_scheduler import (
    _ManagedTaskRoute,
    TASK_DEFAULT_SHELL_TOOLS,
    TaskScheduler,
    compose_task_relevant_tools,
)
from src.tool_index import ASSISTANT_ALWAYS_AVAILABLE


def test_background_contract_migration_keeps_agent_connection_ids(
    monkeypatch,
    tmp_path,
):
    from core import database

    path = tmp_path / "migration.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, endpoint_id TEXT);
            CREATE TABLE model_endpoints (
                id TEXT PRIMARY KEY,
                base_url TEXT,
                owner TEXT,
                is_enabled INTEGER
            );
            CREATE TABLE scheduled_tasks (
                id TEXT PRIMARY KEY,
                owner TEXT,
                session_id TEXT,
                endpoint_url TEXT,
                endpoint_id TEXT
            );
            CREATE TABLE crew_members (
                id TEXT PRIMARY KEY,
                owner TEXT,
                session_id TEXT,
                endpoint_url TEXT,
                endpoint_id TEXT
            );
            """
        )
        for table in ("scheduled_tasks", "crew_members"):
            conn.executemany(
                f"INSERT INTO {table} "
                "(id, owner, endpoint_url, endpoint_id) VALUES (?, ?, ?, ?)",
                [
                    ("native", "alice", "mimo://acp", "mimo:xiaomi"),
                    ("shared", "alice", "mimo://acp", "shared:grant-1"),
                    ("missing", "alice", "https://gone.test/v1", "gone"),
                ],
            )
    monkeypatch.setattr(
        database,
        "DATABASE_URL",
        f"sqlite:///{path}",
    )

    database._migrate_add_background_agent_contract_columns()

    with sqlite3.connect(path) as conn:
        for table in ("scheduled_tasks", "crew_members"):
            rows = dict(conn.execute(
                f"SELECT id, endpoint_id FROM {table}"
            ).fetchall())
            assert rows == {
                "native": "mimo:xiaomi",
                "shared": "shared:grant-1",
                "missing": None,
            }


def test_background_contract_migration_survives_retired_endpoint_table(
    monkeypatch,
    tmp_path,
    caplog,
):
    """Post-cutover startup has route IDs but deliberately no model_endpoints."""
    from core import database

    path = tmp_path / "cutover.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                endpoint_id TEXT,
                provider_model_route_id TEXT
            );
            CREATE TABLE scheduled_tasks (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                endpoint_id TEXT,
                provider_model_route_id TEXT
            );
            CREATE TABLE crew_members (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                endpoint_id TEXT,
                provider_model_route_id TEXT
            );
            INSERT INTO sessions VALUES (
                'session-1', 'retired-endpoint', 'pmr-session'
            );
            INSERT INTO scheduled_tasks VALUES (
                'task-1', 'session-1', NULL, 'pmr-task'
            );
            INSERT INTO crew_members VALUES (
                'crew-1', 'session-1', NULL, 'pmr-crew'
            );
            """
        )
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{path}")

    database._migrate_add_background_agent_contract_columns()
    # Repeat startup to cover both the missing-table branch and ALTER idempotence.
    database._migrate_add_background_agent_contract_columns()

    assert "Background Agent contract migration failed" not in caplog.text
    with sqlite3.connect(path) as connection:
        task_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(scheduled_tasks)")
        }
        assert {
            "endpoint_id",
            "workspace",
            "copal_workspace",
            "allowed_tools",
            "interaction_policy",
            "max_tool_calls",
            "provider_model_route_id",
        } <= task_columns
        assert connection.execute(
            "SELECT endpoint_id, provider_model_route_id FROM scheduled_tasks"
        ).fetchone() == ("retired-endpoint", "pmr-task")
        assert connection.execute(
            "SELECT endpoint_id, provider_model_route_id FROM crew_members"
        ).fetchone() == ("retired-endpoint", "pmr-crew")
        assert connection.execute(
            "SELECT provider_model_route_id FROM sessions"
        ).fetchone() == ("pmr-session",)


def test_existing_database_gains_every_provider_route_reference_idempotently(
    tmp_path,
    monkeypatch,
):
    """create_all skips existing tables; startup must ALTER every route owner."""
    from sqlalchemy import create_engine

    from core import database as core_database

    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, legacy_value TEXT);
            CREATE TABLE scheduled_tasks (id TEXT PRIMARY KEY, legacy_value TEXT);
            CREATE TABLE crew_members (id TEXT PRIMARY KEY, legacy_value TEXT);
            CREATE TABLE comparisons (id TEXT PRIMARY KEY, legacy_value TEXT);
            INSERT INTO sessions VALUES ('session-1', 'keep-session');
            INSERT INTO scheduled_tasks VALUES ('task-1', 'keep-task');
            INSERT INTO crew_members VALUES ('crew-1', 'keep-crew');
            INSERT INTO comparisons VALUES ('comparison-1', 'keep-comparison');
            """
        )

    migration_engine = create_engine(f"sqlite:///{path}")
    monkeypatch.setattr(core_database, "engine", migration_engine)
    try:
        core_database._migrate_add_provider_route_reference_columns()
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE sessions SET provider_model_route_id='pmr-session'"
            )
            connection.execute(
                "UPDATE scheduled_tasks SET provider_model_route_id='pmr-task'"
            )
            connection.execute(
                "UPDATE crew_members SET provider_model_route_id='pmr-crew'"
            )
            connection.execute(
                "UPDATE comparisons SET provider_model_route_a_id='pmr-a', "
                "provider_model_route_b_id='pmr-b'"
            )
            connection.commit()

        # A second startup must neither duplicate schema nor erase cutover IDs.
        core_database._migrate_add_provider_route_reference_columns()
    finally:
        migration_engine.dispose()

    expected = {
        "sessions": {
            "provider_model_route_id": "ix_sessions_provider_model_route_id",
        },
        "scheduled_tasks": {
            "provider_model_route_id": "ix_scheduled_tasks_provider_model_route_id",
        },
        "crew_members": {
            "provider_model_route_id": "ix_crew_members_provider_model_route_id",
        },
        "comparisons": {
            "provider_model_route_a_id": "ix_comparisons_provider_model_route_a_id",
            "provider_model_route_b_id": "ix_comparisons_provider_model_route_b_id",
        },
    }
    legacy_values = {
        "sessions": "keep-session",
        "scheduled_tasks": "keep-task",
        "crew_members": "keep-crew",
        "comparisons": "keep-comparison",
    }
    with sqlite3.connect(path) as connection:
        for table, additions in expected.items():
            columns = {
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            indexes = {
                row[1] for row in connection.execute(f'PRAGMA index_list("{table}")')
            }
            assert set(additions) <= columns
            assert set(additions.values()) <= indexes
            assert connection.execute(
                f'SELECT legacy_value FROM "{table}"'
            ).fetchall() == [(legacy_values[table],)]
        assert connection.execute(
            "SELECT provider_model_route_id FROM sessions"
        ).fetchone() == ("pmr-session",)
        assert connection.execute(
            "SELECT provider_model_route_id FROM scheduled_tasks"
        ).fetchone() == ("pmr-task",)
        assert connection.execute(
            "SELECT provider_model_route_id FROM crew_members"
        ).fetchone() == ("pmr-crew",)
        assert connection.execute(
            "SELECT provider_model_route_a_id, provider_model_route_b_id "
            "FROM comparisons"
        ).fetchone() == ("pmr-a", "pmr-b")


def test_provider_route_schema_runs_after_legacy_task_rebuilds():
    from core import database as core_database

    declared = {
        table.name: {
            column.name
            for column in table.columns
            if column.name.startswith("provider_model_route")
        }
        for table in core_database.Base.metadata.sorted_tables
        if any(
            column.name.startswith("provider_model_route")
            for column in table.columns
        )
    }
    configured = {
        table: set(additions)
        for table, additions in (
            core_database._PROVIDER_ROUTE_REFERENCE_COLUMNS.items()
        )
    }
    assert configured == declared

    source = inspect.getsource(core_database.init_db)
    migration = source.index("_migrate_add_provider_route_reference_columns()")
    task_rebuild = source.index("_migrate_add_task_automation_columns()")
    final_query_migration = source.index("_migrate_backfill_task_folders()")
    assert task_rebuild < migration < final_query_migration


def test_assistant_always_available_lacks_shell():
    # Pins the precondition that made the bug possible: the assistant set the
    # task runner relied on does not contain the shell/Python tools.
    assert "bash" not in ASSISTANT_ALWAYS_AVAILABLE
    assert "python" not in ASSISTANT_ALWAYS_AVAILABLE


def test_shell_offered_when_rag_returns_nothing():
    # Degraded/empty embedding index -> rag_tools is empty (the #4163 case).
    tools = compose_task_relevant_tools(set(), ASSISTANT_ALWAYS_AVAILABLE, None)
    assert "bash" in tools
    assert "python" in tools
    assert TASK_DEFAULT_SHELL_TOOLS <= tools


def test_assistant_and_rag_tools_preserved():
    tools = compose_task_relevant_tools(
        {"web_fetch"}, ASSISTANT_ALWAYS_AVAILABLE, None
    )
    assert "web_fetch" in tools          # RAG-selected tool kept
    assert "manage_calendar" in tools    # assistant-always member kept
    assert "bash" in tools               # shell default added


def test_crew_allowlist_restriction_still_honored():
    # A crew that defines enabled_tools yields a `disabled_tools` set
    # (all_tools - enabled). Anything it disables must stay disabled, including
    # the shell defaults — the task owner explicitly scoped the tools.
    disabled = {"bash", "python", "edit_file"}
    tools = compose_task_relevant_tools(set(), ASSISTANT_ALWAYS_AVAILABLE, disabled)
    assert "bash" not in tools
    assert "python" not in tools
    assert "edit_file" not in tools
    # Shell tools the crew did NOT disable remain available.
    assert "read_file" in tools


def test_offered_shell_maps_to_real_schemas_for_admin():
    # End-to-end with the real schema list: the names we add are actual
    # function schemas, so an admin/single-user task (nothing in disabled_tools)
    # really does get bash/python offered to the model — not just named in prose.
    from src.agent_loop import FUNCTION_TOOL_SCHEMAS

    schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    offered = compose_task_relevant_tools(set(), ASSISTANT_ALWAYS_AVAILABLE, None)
    admin_schemas = offered & schema_names  # mirrors agent_loop's relevant∩schemas
    assert "bash" in admin_schemas
    assert "python" in admin_schemas


def test_non_admin_owner_block_strips_shell_end_to_end():
    # Defense check: the runner now OFFERS shell tools, but stream_agent_loop
    # subtracts blocked_tools_for_owner() (== NON_ADMIN_BLOCKED_TOOLS for a
    # non-admin multi-user owner) from both the prompt and the schemas. Reusing
    # that exact block set proves a non-admin task's model never sees the shell.
    from src.agent_loop import FUNCTION_TOOL_SCHEMAS
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS

    schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    offered = compose_task_relevant_tools(set(), ASSISTANT_ALWAYS_AVAILABLE, None)
    non_admin_schemas = (offered - set(NON_ADMIN_BLOCKED_TOOLS)) & schema_names
    assert "bash" not in non_admin_schemas
    assert "python" not in non_admin_schemas


@pytest.mark.asyncio
async def test_scheduled_agent_uses_normalized_managed_route(monkeypatch):
    captured = {}

    async def stream(target, **kwargs):
        captured["target"] = target
        captured["turn_envelope"] = kwargs["turn_envelope"]
        yield 'data: {"delta":"done"}\n\ndata: [DONE]\n\n'

    async def ready(_label):
        return None

    monkeypatch.setattr("src.model_dispatch.stream_agent_target", stream)
    monkeypatch.setattr("src.interactive_gate.wait_for_interactive_quiet", ready)
    scheduler = TaskScheduler(session_manager=None)
    task = SimpleNamespace(
        id="task-1",
        name="Managed task",
        prompt="do it",
        owner="alice",
        allowed_tools="[]",
        max_steps=2,
        max_tool_calls=2,
        workspace=None,
    )

    result = await scheduler._run_agent_loop(
        "mimo://acp",
        "xiaomi/mimo-v2.5-pro",
        task,
        "task-session",
        managed_route=_ManagedTaskRoute(
            model_route_id="pmr-task",
            connection_id="pcn-task",
            provider_model_id="mimo-v2.5-pro",
            public_endpoint_id="pcn-task",
            capabilities={"tools": True},
        ),
        root_operation_id="turn-task-run:run-1",
    )

    assert result == "done"
    assert captured["target"].endpoint_id == "pcn-task"
    assert captured["target"].provider_id == "pcn-task"
    assert captured["target"].model_id == "pcn-task/mimo-v2.5-pro"


async def test_scheduled_agent_keeps_shared_connection_identity(monkeypatch):
    captured = {}

    async def stream(target, **kwargs):
        captured["target"] = target
        captured["turn_envelope"] = kwargs["turn_envelope"]
        yield 'data: {"delta":"done"}\n\ndata: [DONE]\n\n'

    async def ready(_label):
        return None

    monkeypatch.setattr("src.model_dispatch.stream_agent_target", stream)
    monkeypatch.setattr("src.interactive_gate.wait_for_interactive_quiet", ready)
    scheduler = TaskScheduler(session_manager=None)
    task = SimpleNamespace(
        id="task-shared",
        name="Shared task",
        prompt="do it",
        endpoint_id="shared:grant-1",
        owner="alice",
        allowed_tools="[]",
        max_steps=2,
        max_tool_calls=2,
        workspace=None,
    )

    result = await scheduler._run_agent_loop(
        "mimo://acp",
        "shared-model",
        task,
        "task-session",
        managed_route=_ManagedTaskRoute(
            model_route_id="pmr-shared",
            connection_id="pcn-source",
            provider_model_id="shared-model",
            public_endpoint_id="share:grant-1",
            capabilities={"tools": True},
            grant_id="grant-1",
        ),
        root_operation_id="turn-task-run:run-shared",
    )

    assert result == "done"
    assert captured["target"].endpoint_id == "pcn-source"
    assert captured["target"].provider_id == "pcn-source"
    assert captured["target"].transport == "acp"
    assert captured["turn_envelope"]["provider_grant_id"] == "grant-1"
    assert captured["turn_envelope"]["root_operation_id"] == "turn-task-run:run-shared"


async def test_tool_result_summary_keeps_task_route_and_root(monkeypatch):
    captured = {}

    async def stream(_target, **_kwargs):
        yield 'data: {"type":"tool_output","tool":"bash","stdout":"finished"}\n\n'
        yield "data: [DONE]\n\n"

    async def complete_text(**kwargs):
        captured.update(kwargs)
        return "finished"

    async def ready(_label):
        return None

    monkeypatch.setattr("src.model_dispatch.stream_agent_target", stream)
    monkeypatch.setattr("src.openclank.modality_facade.complete_text", complete_text)
    monkeypatch.setattr("src.interactive_gate.wait_for_interactive_quiet", ready)
    scheduler = TaskScheduler(session_manager=None)
    task = SimpleNamespace(
        id="task-summary",
        name="Summary task",
        prompt="do it",
        owner="alice",
        allowed_tools="[]",
        max_steps=2,
        max_tool_calls=2,
        workspace=None,
    )
    route = _ManagedTaskRoute(
        model_route_id="pmr-shared",
        connection_id="pcn-source",
        provider_model_id="shared-model",
        public_endpoint_id="share:grant-1",
        capabilities={"tools": True},
        grant_id="grant-1",
    )

    result = await scheduler._run_agent_loop(
        "ignored://legacy",
        "ignored-model",
        task,
        "task-session",
        managed_route=route,
        root_operation_id="turn-task-run:summary",
    )

    assert result == "finished"
    assert captured["owner"] == "alice"
    assert captured["purpose"] == "tasks"
    assert captured["model_route_id"] == "pmr-shared"
    assert captured["grant_id"] == "grant-1"
    assert captured["root_operation_id"] == "turn-task-run:summary"


async def test_classify_action_receives_normalized_task_route(monkeypatch):
    from src import builtin_actions

    captured = {}
    route = _ManagedTaskRoute(
        model_route_id="pmr-classify",
        connection_id="pcn-classify",
        provider_model_id="classify-model",
        public_endpoint_id="share:grant-classify",
        capabilities={"chat": True},
        grant_id="grant-classify",
    )

    async def classify(**kwargs):
        captured.update(kwargs)
        return "classified", True

    monkeypatch.setitem(builtin_actions.BUILTIN_ACTIONS, "classify_events", classify)
    monkeypatch.setattr(
        "src.task_scheduler._resolve_managed_task_route",
        lambda *args, **kwargs: route,
    )
    scheduler = TaskScheduler(session_manager=None)
    task = SimpleNamespace(
        id="task-classify",
        action="classify_events",
        owner="alice",
        name="Classify calendar",
        prompt=None,
    )

    result, success = await scheduler._execute_action(task, run_id="run-classify")

    assert (result, success) == ("classified", True)
    assert captured["model_route_id"] == "pmr-classify"
    assert captured["grant_id"] == "grant-classify"
    assert captured["root_operation_id"] == "turn-task-run:run-classify"
    assert task.provider_model_route_id == "pmr-classify"
    assert task.endpoint_url == "openclank://engine"
    assert scheduler._last_run_model == "classify-model"


async def test_scheduled_task_honors_global_disabled_tools(monkeypatch):
    # RaresKeY review on #4398: the runner offers the shell/file group by
    # default, but the scheduled-task path only built disabled_tools from the
    # crew allowlist — it never merged the operator's global disabled_tools
    # setting. So an admin / AUTH_ENABLED=false task could still see and call
    # bash/python after the operator turned them off globally, because the
    # downstream prompt/schema/execution gates only enforce what is passed in.
    #
    # Drive the real _execute_llm_task and assert the global list reaches BOTH
    # sides: it is stripped from relevant_tools AND passed into the agent loop.
    global_off = ["bash", "python", "read_file"]

    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: list(global_off) if key == "disabled_tools" else default,
    )

    # Degraded-index stand-in that still returns one RAG hit, so we can prove
    # non-disabled tools survive the merge.
    class _FakeIndex:
        def get_tools_for_query(self, query, k=8):
            return {"web_fetch"}

    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: _FakeIndex())

    captured = {}

    async def _capture(endpoint_url, model, task, session_id, *,
                       system_prompt=None, disabled_tools=None, relevant_tools=None,
                       datetime_context_msg=None, managed_route=None,
                       root_operation_id=None):
        captured["disabled_tools"] = disabled_tools
        captured["relevant_tools"] = relevant_tools
        return "done"

    scheduler = TaskScheduler(session_manager=None)
    scheduler._run_agent_loop = _capture
    managed_route = _ManagedTaskRoute(
        model_route_id="pmr-task",
        connection_id="pcn-task",
        provider_model_id="util-model",
        public_endpoint_id="pcn-task",
        capabilities={"tools": True},
    )
    monkeypatch.setattr(
        "src.task_scheduler._resolve_managed_task_route",
        lambda *args, **kwargs: managed_route,
    )

    # No crew_member_id + a preset session/endpoint means the DB is never
    # touched on this path, so a bare task object is enough to exercise it.
    task = SimpleNamespace(
        crew_member_id=None,
        endpoint_id="endpoint-1",
        endpoint_url="http://endpoint",
        model="util-model",
        session_id="sess-1",
        owner="admin",
        prompt="back up the logs",
        name="Nightly job",
        max_steps=5,
        character_id=None,
    )

    class _Db:
        class _Query:
            def filter(self, *args): return self
            def first(self): return None
        def query(self, *args): return self._Query()
        def commit(self): pass

    result = await scheduler._execute_llm_task(task, db=_Db())
    assert result == "done"

    # Enforcement side: the global list reached the agent loop, so the
    # prompt/schema/execution gates will strip these even for an admin owner.
    passed_disabled = captured["disabled_tools"]
    assert passed_disabled is not None
    assert set(global_off) <= set(passed_disabled)

    # Offer side: globally-disabled tools are gone from relevant_tools, but the
    # rest of the shell/file defaults and the RAG hit survive.
    offered = captured["relevant_tools"]
    assert "bash" not in offered
    assert "python" not in offered
    assert "read_file" not in offered
    assert "edit_file" in offered   # shell default NOT globally disabled
    assert "web_fetch" in offered   # RAG-selected tool preserved

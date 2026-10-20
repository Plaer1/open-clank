from __future__ import annotations

import json
import os
import sqlite3

import pytest

import src.openclank.provider_cutover as cutover_module
from src.openclank.provider_cutover import (
    CutoverPaths,
    cutover_complete,
    freeze_mapping_plan,
    load_frozen_mapping_plan,
    run_provider_cutover,
)
from src.openclank.provider_migration import MigrationJournal, ProviderMigrationError
from src.openclank.provider_migration_plan import build_provider_mapping_plan
from src.secret_storage import encrypt


def _legacy_install(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    database = data / "app.db"
    db = sqlite3.connect(database)
    db.executescript(
        """
        CREATE TABLE model_endpoints (
          id TEXT PRIMARY KEY, name TEXT, base_url TEXT, api_key TEXT,
          is_enabled INTEGER, hidden_models TEXT, cached_models TEXT,
          pinned_models TEXT, model_type TEXT, endpoint_kind TEXT,
          model_refresh_mode TEXT, model_refresh_interval INTEGER,
          model_refresh_timeout INTEGER, owner TEXT, provider_auth_id TEXT
        );
        CREATE TABLE provider_auth_sessions (
          id TEXT PRIMARY KEY, provider TEXT, owner TEXT, label TEXT,
          access_token TEXT, refresh_token TEXT, account_id TEXT
        );
        CREATE TABLE mimo_auth_store (
          owner TEXT PRIMARY KEY, payload TEXT, updated_at TEXT
        );
        CREATE TABLE mimo_model_prefs (owner TEXT, provider_id TEXT, model_id TEXT);
        CREATE TABLE model_capabilities (endpoint_id TEXT, model_id TEXT);
        CREATE TABLE mimo_projection_states (owner_id TEXT PRIMARY KEY);
        CREATE TABLE sessions (
          id TEXT PRIMARY KEY, owner TEXT, endpoint_id TEXT, endpoint_url TEXT,
          model TEXT, headers TEXT, mimo_state TEXT
        );
        CREATE TABLE chat_messages (
          id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
          metadata TEXT
        );
        CREATE TABLE scheduled_tasks (
          id TEXT PRIMARY KEY, owner TEXT, status TEXT, task_type TEXT,
          endpoint_id TEXT REFERENCES model_endpoints(id), endpoint_url TEXT,
          model TEXT
        );
        CREATE INDEX ix_task_endpoint ON scheduled_tasks(endpoint_id);
        CREATE TABLE crew_members (
          id TEXT PRIMARY KEY, owner TEXT, is_active INTEGER,
          endpoint_id TEXT REFERENCES model_endpoints(id), endpoint_url TEXT,
          model TEXT
        );
        CREATE TABLE comparisons (
          id TEXT PRIMARY KEY, owner TEXT, endpoint_a TEXT, endpoint_b TEXT,
          model_a TEXT, model_b TEXT
        );
        CREATE TABLE model_shares (
          id TEXT PRIMARY KEY, owner TEXT, source_kind TEXT, source_id TEXT,
          model_id TEXT, active INTEGER
        );
        CREATE TABLE model_share_subscriptions (
          share_id TEXT, subscriber TEXT, enabled INTEGER
        );
        """
    )
    db.execute(
        "INSERT INTO model_endpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "endpoint-1",
            "Official API",
            "https://api.openai.com/v1/chat/completions?api_key=must-remove",
            encrypt("migration-key-secret"),
            1,
            "[]",
            '["gpt-test"]',
            "[]",
            "llm",
            "api",
            "auto",
            None,
            None,
            "alice",
            None,
        ),
    )
    db.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
        (
            "session-1",
            "alice",
            "endpoint-1",
            "https://user:password@api.openai.com/v1?token=must-remove",
            "gpt-test",
            json.dumps({"Authorization": "Bearer session-secret", "safe": True}),
            json.dumps({"access_token": "state-secret", "safe": True}),
        ),
    )
    db.execute(
        "INSERT INTO chat_messages VALUES (?,?,?,?,?)",
        (
            "message-1",
            "session-1",
            "user",
            "/setup provider plaintext-chat-secret",
            json.dumps({"api_key": "metadata-secret", "safe": True}),
        ),
    )
    db.execute(
        "INSERT INTO scheduled_tasks VALUES (?,?,?,?,?,?,?)",
        ("task-1", "alice", "active", "llm", "endpoint-1", "https://api.openai.com/v1", "gpt-test"),
    )
    db.execute(
        "INSERT INTO crew_members VALUES (?,?,?,?,?,?)",
        ("crew-1", "alice", 1, "endpoint-1", "https://api.openai.com/v1", "gpt-test"),
    )
    db.commit()
    db.close()
    (data / "auth.json").write_text(
        json.dumps({"users": {"alice": {"is_admin": True}}}),
        encoding="utf-8",
    )
    (data / "settings.json").write_text("{}", encoding="utf-8")
    (data / "user_prefs.json").write_text("{}", encoding="utf-8")
    (data / "embedding_endpoint.json").write_text("{}", encoding="utf-8")
    return data


def test_frozen_plan_is_authenticated_and_contains_no_plaintext(tmp_path):
    data = _legacy_install(tmp_path)
    plan = build_provider_mapping_plan(
        database_path=data / "app.db",
        owner_assignment=None,
    )
    path = tmp_path / "plan.ocplan"
    digest = freeze_mapping_plan(path, plan)
    assert len(digest) == 64
    assert path.read_bytes().startswith(b"OPENCLANK-PROVIDER-PLAN-V1\n")
    assert b"migration-key-secret" not in path.read_bytes()
    restored = load_frozen_mapping_plan(path)
    assert restored.safe_report() == plan.safe_report()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_activation_contract_accepts_deepseek_and_xiaomi_metered_api():
    db = sqlite3.connect(":memory:")
    try:
        db.execute(
            "CREATE TABLE provider_connections ("
            "id TEXT, family_id TEXT, adapter_id TEXT, kind TEXT, "
            "billing_lane TEXT, normalized_url TEXT, enabled INTEGER, deleted_at TEXT)"
        )
        db.executemany(
            "INSERT INTO provider_connections VALUES (?,?,?,?,?,?,1,NULL)",
            [
                (
                    "deepseek-api",
                    "deepseek",
                    "openai-chat",
                    "official",
                    "metered_api",
                    "https://api.deepseek.com",
                ),
                (
                    "xiaomi-api",
                    "xiaomi",
                    "mimo-native",
                    "official",
                    "metered_api",
                    "https://api.xiaomi.example",
                ),
            ],
        )

        cutover_module._verify_activation_connections(db)
    finally:
        db.close()


@pytest.mark.parametrize(
    ("family", "adapter", "kind", "lane", "url", "message"),
    [
        (
            "groq",
            "models-dev-openai-compatible",
            "official",
            "metered_api",
            "https://api.groq.com/openai/v1",
            None,
        ),
        (
            "custom-anthropic-host",
            "models-dev-anthropic",
            "official",
            "metered_api",
            "https://anthropic.example.test/v1",
            None,
        ),
        (
            "Groq!",
            "models-dev-openai-compatible",
            "official",
            "metered_api",
            "https://api.groq.com/openai/v1",
            "invalid ModelsDev family",
        ),
        (
            "groq",
            "models-dev-openai-compatible",
            "custom_gateway",
            "metered_api",
            "https://api.groq.com/openai/v1",
            "requires official connection kind",
        ),
        (
            "groq",
            "models-dev-openai-compatible",
            "official",
            "custom",
            "https://api.groq.com/openai/v1",
            "requires metered API billing lane",
        ),
        (
            "groq",
            "models-dev-openai-compatible",
            "official",
            "metered_api",
            "http://api.groq.com/openai/v1",
            "requires a safe HTTPS URL",
        ),
    ],
)
def test_activation_contract_bounds_dynamic_models_dev_connections(
    family,
    adapter,
    kind,
    lane,
    url,
    message,
):
    db = sqlite3.connect(":memory:")
    try:
        db.execute(
            "CREATE TABLE provider_connections ("
            "id TEXT, family_id TEXT, adapter_id TEXT, kind TEXT, "
            "billing_lane TEXT, normalized_url TEXT, enabled INTEGER, deleted_at TEXT)"
        )
        db.execute(
            "INSERT INTO provider_connections VALUES (?,?,?,?,?,?,1,NULL)",
            ("dynamic-provider", family, adapter, kind, lane, url),
        )
        if message is None:
            cutover_module._verify_activation_connections(db)
        else:
            with pytest.raises(ProviderMigrationError, match=message):
                cutover_module._verify_activation_connections(db)
    finally:
        db.close()


def test_cutover_is_atomic_resumable_and_scrubs_legacy_authorities(tmp_path):
    data = _legacy_install(tmp_path)
    result = run_provider_cutover(data_dir=data, verify_engine=False)
    assert result.complete is True
    assert result.phase == "complete"
    assert result.counts == {
        "connections": 2,
        "accounts": 1,
        "model_routes": 2,
        "share_grants": 0,
    }
    assert cutover_complete(data) is True
    assert not (data / "embedding_endpoint.json").exists()

    db = sqlite3.connect(data / "app.db")
    try:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "model_endpoints" not in tables
        assert "mimo_auth_store" not in tables
        assert "provider_connections" in tables
        embedding_binding = db.execute(
            "SELECT route.provider_model_id,connection.family_id,connection.billing_lane "
            "FROM provider_route_bindings binding "
            "JOIN provider_model_routes route ON route.id=binding.model_route_id "
            "JOIN provider_connections connection ON connection.id=route.connection_id "
            "WHERE binding.owner='alice' AND binding.purpose='embeddings'"
        ).fetchone()
        assert embedding_binding == (
            "fastembed/default",
            "local-executor",
            "local",
        )
        session_columns = {
            row[1] for row in db.execute("PRAGMA table_info(sessions)")
        }
        assert "endpoint_id" in session_columns
        assert "endpoint_url" in session_columns
        assert "provider_model_route_id" in session_columns
        headers, state, old_id, old_url = db.execute(
            "SELECT headers, mimo_state, endpoint_id, endpoint_url "
            "FROM sessions WHERE id='session-1'"
        ).fetchone()
        assert json.loads(headers) == {"safe": True}
        assert json.loads(state) == {"safe": True}
        assert old_id is None and old_url is None
        content, metadata = db.execute(
            "SELECT content, metadata FROM chat_messages WHERE id='message-1'"
        ).fetchone()
        assert "plaintext-chat-secret" not in content
        assert json.loads(metadata) == {"safe": True}
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()

    raw = (data / "app.db").read_bytes()
    for secret in (
        b"migration-key-secret",
        b"plaintext-chat-secret",
        b"session-secret",
        b"metadata-secret",
        b"state-secret",
        b"must-remove",
    ):
        assert secret not in raw
    journal_text = CutoverPaths.for_data_dir(data).journal.read_text(encoding="utf-8")
    for secret in (
        "migration-key-secret",
        "plaintext-chat-secret",
        "session-secret",
        "metadata-secret",
        "state-secret",
        "must-remove",
    ):
        assert secret not in journal_text

    # A completed journal is an idempotent no-op with the same verified counts.
    repeated = run_provider_cutover(data_dir=data, verify_engine=False)
    assert repeated.complete is True
    assert repeated.counts == result.counts


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("sessions", "provider_model_route_id"),
        ("comparisons", "provider_model_route_b_id"),
    ],
)
def test_completed_cutover_rejects_missing_consumer_route_column(
    tmp_path,
    table,
    column,
):
    data = _legacy_install(tmp_path)
    assert run_provider_cutover(data_dir=data, verify_engine=False).complete is True

    db = sqlite3.connect(data / "app.db")
    try:
        for index_row in db.execute(f'PRAGMA index_list("{table}")').fetchall():
            index_name = str(index_row[1])
            indexed_columns = {
                str(row[2])
                for row in db.execute(f'PRAGMA index_info("{index_name}")').fetchall()
            }
            if column in indexed_columns:
                escaped = index_name.replace('"', '""')
                db.execute(f'DROP INDEX "{escaped}"')
        db.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        db.commit()
    finally:
        db.close()

    with pytest.raises(
        ProviderMigrationError,
        match=rf"{table}\.{column}",
    ):
        run_provider_cutover(data_dir=data, verify_engine=False)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("family_id", "legacy-openai", "unknown family"),
        ("adapter_id", "openai", "unsupported adapter"),
        ("kind", "official_api", "unsupported connection kind"),
        ("billing_lane", "legacy", "unsupported billing lane"),
    ],
)
def test_completed_cutover_rejects_connection_outside_projection_contract(
    tmp_path,
    column,
    value,
    message,
):
    data = _legacy_install(tmp_path)
    assert run_provider_cutover(data_dir=data, verify_engine=False).complete is True

    db = sqlite3.connect(data / "app.db")
    try:
        connection_id = db.execute(
            "SELECT id FROM provider_connections WHERE family_id = 'openai'"
        ).fetchone()[0]
        db.execute(
            f'UPDATE provider_connections SET "{column}" = ? WHERE id = ?',
            (value, connection_id),
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(ProviderMigrationError, match=message):
        run_provider_cutover(data_dir=data, verify_engine=False)


def test_fresh_install_needs_no_legacy_cutover(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    result = run_provider_cutover(data_dir=data, verify_engine=False)
    assert result.needed is False
    assert result.phase == "not_required"
    assert not CutoverPaths.for_data_dir(data).journal.exists()


def test_auth_disabled_cutover_assigns_ownerless_state_to_local_installation(tmp_path):
    data = _legacy_install(tmp_path)
    db = sqlite3.connect(data / "app.db")
    for table in ("model_endpoints", "sessions", "scheduled_tasks", "crew_members"):
        db.execute(f'UPDATE "{table}" SET owner = NULL')
    db.commit()
    db.close()
    (data / "auth.json").write_text('{"users":{}}', encoding="utf-8")

    result = run_provider_cutover(
        data_dir=data,
        auth_enabled=False,
        verify_engine=False,
    )
    assert result.complete is True
    db = sqlite3.connect(data / "app.db")
    try:
        for table in (
            "provider_connections",
            "provider_accounts",
            "provider_model_routes",
            "provider_route_bindings",
        ):
            owners = {
                row[0]
                for row in db.execute(f'SELECT DISTINCT owner FROM "{table}"')
            }
            assert owners == {"local-installation"}
    finally:
        db.close()


def test_cutover_persists_global_defaults_and_active_task_route(tmp_path):
    data = _legacy_install(tmp_path)
    (data / "settings.json").write_text(
        json.dumps(
            {
                "default_endpoint_id": "endpoint-1",
                "default_model": "gpt-test",
                "task_endpoint_id": "endpoint-1",
                "task_model": "gpt-test",
            }
        ),
        encoding="utf-8",
    )

    result = run_provider_cutover(data_dir=data, verify_engine=False)
    assert result.complete is True
    db = sqlite3.connect(data / "app.db")
    try:
        bindings = db.execute(
            "SELECT owner, purpose, ordinal FROM provider_route_bindings "
            "ORDER BY purpose, ordinal"
        ).fetchall()
        assert ("alice", "chat", 0) in bindings
        assert ("alice", "tasks", 0) in bindings
        assert ("alice", "embeddings", 0) in bindings
        route_id = db.execute(
            "SELECT provider_model_route_id FROM scheduled_tasks WHERE id='task-1'"
        ).fetchone()[0]
        assert route_id
    finally:
        db.close()


@pytest.mark.parametrize(
    ("operation", "durable_phase"),
    [
        ("create_rollback_archive", "workers_drained"),
        ("freeze_mapping_plan", "snapshot_verified"),
        ("apply_provider_mapping_plan", "plan_frozen"),
        ("scrub_legacy_provider_state", "transaction_committed"),
        ("verify_cutover_state", "legacy_scrubbed"),
    ],
)
def test_cutover_resumes_after_crash_between_durable_journal_boundaries(
    tmp_path,
    monkeypatch,
    operation,
    durable_phase,
):
    data = _legacy_install(tmp_path)
    paths = CutoverPaths.for_data_dir(data)
    original = getattr(cutover_module, operation)
    injected = False

    def crash_after_side_effect(*args, **kwargs):
        nonlocal injected
        result = original(*args, **kwargs)
        if not injected:
            injected = True
            raise RuntimeError(f"simulated crash after {operation}")
        return result

    monkeypatch.setattr(cutover_module, operation, crash_after_side_effect)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_provider_cutover(data_dir=data, verify_engine=False)

    assert MigrationJournal(paths.journal).read()["phase"] == durable_phase
    assert paths.active.exists()
    monkeypatch.setattr(cutover_module, operation, original)

    resumed = run_provider_cutover(data_dir=data, verify_engine=False)
    assert resumed.complete is True
    assert resumed.phase == "complete"
    assert not paths.active.exists()
    assert cutover_complete(data) is True

    # A crash after the snapshot write but before its journal update retains
    # that valid artifact and creates a distinct recovery archive on resume.
    if operation == "create_rollback_archive":
        assert len(list((data / "backups").glob("provider-cutover-*.ocbak"))) == 2

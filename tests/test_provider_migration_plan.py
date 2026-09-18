from __future__ import annotations

import json
import sqlite3

import pytest

from src.openclank.provider_migration_plan import (
    _billing_lane,
    _family_adapter,
    apply_provider_mapping_plan,
    build_provider_mapping_plan,
)
from src.secret_storage import encrypt


def _insert_endpoint(
    db,
    *,
    endpoint_id,
    name,
    base_url,
    api_key="",
    models=("model-test",),
    endpoint_kind="api",
    owner="alice",
    provider_auth_id=None,
):
    db.execute(
        "INSERT INTO model_endpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            endpoint_id,
            name,
            base_url,
            encrypt(api_key) if api_key else "",
            1,
            "[]",
            json.dumps(list(models)),
            "[]",
            "llm",
            endpoint_kind,
            "auto",
            None,
            None,
            owner,
            provider_auth_id,
        ),
    )


def _legacy_database(tmp_path):
    path = tmp_path / "app.db"
    db = sqlite3.connect(path)
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
        CREATE TABLE mimo_auth_store (owner TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE mimo_model_prefs (owner TEXT, provider_id TEXT, model_id TEXT);
        CREATE TABLE sessions (id TEXT, owner TEXT, endpoint_id TEXT, model TEXT);
        CREATE TABLE model_shares (
          id TEXT PRIMARY KEY, owner TEXT, source_kind TEXT, source_id TEXT,
          model_id TEXT, active INTEGER
        );
        CREATE TABLE model_share_subscriptions (
          share_id TEXT, subscriber TEXT, enabled INTEGER
        );
        CREATE TABLE scheduled_tasks (
          id TEXT PRIMARY KEY, owner TEXT, status TEXT, task_type TEXT,
          endpoint_id TEXT, endpoint_url TEXT, model TEXT
        );
        CREATE TABLE crew_members (
          id TEXT PRIMARY KEY, owner TEXT, endpoint_id TEXT, model TEXT
        );
        CREATE TABLE comparisons (
          id TEXT PRIMARY KEY, owner TEXT,
          endpoint_a TEXT, model_a TEXT, endpoint_b TEXT, model_b TEXT
        );
        """
    )
    db.execute(
        "INSERT INTO model_endpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "ep-openai",
            "OpenAI",
            "https://api.openai.com/v1/chat/completions",
            encrypt("sk-migration-secret"),
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
        "INSERT INTO sessions VALUES ('s1','alice','ep-openai','gpt-test')"
    )
    db.execute(
        "INSERT INTO model_shares VALUES ('share-1','alice','endpoint','ep-openai','gpt-test',1)"
    )
    db.execute(
        "INSERT INTO model_share_subscriptions VALUES ('share-1','bob',1)"
    )
    db.execute(
        "INSERT INTO scheduled_tasks VALUES (?,?,?,?,?,?,?)",
        (
            "shared-task",
            "bob",
            "active",
            "llm",
            "shared:share-1",
            "",
            "gpt-test",
        ),
    )
    native = {
        "version": 2,
        "revision": 1,
        "pools": {
            "openai-sub": {
                "connectionID": "openai-sub",
                "providerID": "openai",
                "billingLane": "subscription",
                "revision": 1,
                "cursor": 0,
                "accounts": {
                    "first": {
                        "id": "first",
                        "label": "First subscription",
                        "enabled": True,
                        "order": 0,
                        "revision": 1,
                        "credentialRevision": 1,
                        "credential": {
                            "type": "oauth",
                            "access": "native-access",
                            "refresh": "native-refresh",
                            "expires": 123,
                        },
                    },
                    "second": {
                        "id": "second",
                        "label": "Second subscription",
                        "enabled": True,
                        "order": 1,
                        "revision": 1,
                        "credentialRevision": 1,
                        "credential": {
                            "type": "oauth",
                            "access": "native-access-2",
                            "refresh": "native-refresh-2",
                            "expires": 123,
                        },
                    },
                },
            }
        },
    }
    db.execute(
        "INSERT INTO mimo_auth_store VALUES (?,?)",
        ("alice", encrypt(json.dumps(native))),
    )
    db.execute("INSERT INTO sessions VALUES ('s2','alice','mimo:openai','gpt-native')")
    db.commit()
    db.close()
    return path


@pytest.mark.parametrize(
    ("base_url", "name", "provider_hint", "endpoint_kind", "expected"),
    [
        ("https://api.anthropic.com/v1", "", "", "api", ("anthropic", "anthropic-messages", "official")),
        ("https://openrouter.ai/api/v1", "", "", "api", ("openrouter", "openai-chat", "official")),
        (None, "", "github-copilot", "api", ("github-copilot", "copilot-chat", "subscription")),
        ("https://chatgpt.com/backend-api/codex", "", "", "api", ("openai", "openai-responses", "subscription")),
        ("https://api.openai.com/v1", "", "", "api", ("openai", "openai-responses", "official")),
        ("https://generativelanguage.googleapis.com", "", "", "api", ("google", "google-generative-ai", "official")),
        ("https://api.x.ai/v1", "", "", "api", ("xai", "xai-responses", "official")),
        ("https://api.deepseek.com/v1", "", "", "api", ("deepseek", "openai-chat", "official")),
        (None, "", "xiaomi", "api", ("xiaomi", "mimo-native", "official")),
        ("http://localhost:11434", "", "", "local", ("ollama", "ollama", "local")),
        ("http://localhost:8080", "Local gateway", "", "local", ("openai-compatible", "openai-chat", "local")),
        ("https://gateway.example.test/v1", "Gateway", "", "api", ("openai-compatible", "openai-chat", "custom_gateway")),
    ],
)
def test_family_adapter_matrix_is_managed_engine_canonical(
    base_url,
    name,
    provider_hint,
    endpoint_kind,
    expected,
):
    assert _family_adapter(
        base_url=base_url,
        name=name,
        provider_hint=provider_hint,
        endpoint_kind=endpoint_kind,
    ) == expected


@pytest.mark.parametrize(
    ("kind", "credential", "expected"),
    [
        ("official", {"type": "api", "key": "secret"}, "metered_api"),
        ("subscription", {"type": "oauth", "access": "secret"}, "subscription"),
        ("custom_gateway", {"type": "api", "key": "secret"}, "custom"),
        ("local", {"type": "api", "key": "secret"}, "local"),
    ],
)
def test_billing_lane_follows_canonical_connection_kind(kind, credential, expected):
    assert _billing_lane(
        family="openai-compatible",
        kind=kind,
        credential=credential,
    ) == expected


def test_mapping_preserves_connections_lanes_accounts_models_and_share(tmp_path):
    path = _legacy_database(tmp_path)
    plan = build_provider_mapping_plan(database_path=path, owner_assignment=None)
    assert plan.blockers == []
    assert {row.billing_lane for row in plan.connections} == {
        "metered_api",
        "subscription",
    }
    assert len(plan.accounts) == 3
    assert len([row for row in plan.accounts if row.auth_class == "subscription"]) == 2
    assert {row.provider_model_id for row in plan.routes} == {"gpt-test", "gpt-native"}
    assert len(plan.shares) == 1
    assert plan.shares[0].account_ids and len(plan.shares[0].account_ids) == 1
    assert plan.shares[0].model_route_ids and plan.shares[0].accepted is True
    assert plan.shares[0].recipient == "bob"
    assert plan.shares[0].state == "active"
    shared_reference = next(
        row
        for row in plan.references
        if row.table == "scheduled_tasks" and row.row_id == "shared-task"
    )
    assert shared_reference.model_route_id == plan.shares[0].model_route_ids[0]
    serialized = json.dumps(plan.safe_report())
    for secret in ("sk-migration-secret", "native-access", "native-refresh"):
        assert secret not in serialized


def test_frozen_native_openai_xiaomi_and_deepseek_topology_is_canonical(tmp_path):
    path = _legacy_database(tmp_path)
    native = {
        "version": 2,
        "revision": 1,
        "pools": {
            "openai-sub": {
                "providerID": "openai",
                "billingLane": "subscription",
                "accounts": {
                    "openai": {
                        "label": "OpenAI",
                        "credential": {
                            "type": "oauth",
                            "access": "openai-access",
                            "refresh": "openai-refresh",
                            "expires": 123,
                        },
                    }
                },
            },
            "xiaomi-api": {
                "providerID": "xiaomi",
                "billingLane": "metered_api",
                "accounts": {
                    "xiaomi": {
                        "label": "Xiaomi",
                        "credential": {"type": "api", "key": "xiaomi-key"},
                    }
                },
            },
            "deepseek-api": {
                "providerID": "deepseek",
                "billingLane": "metered_api",
                "accounts": {
                    "deepseek": {
                        "label": "DeepSeek",
                        "credential": {"type": "api", "key": "deepseek-key"},
                    }
                },
            },
        },
    }
    db = sqlite3.connect(path)
    db.execute(
        "UPDATE mimo_auth_store SET payload=? WHERE owner='alice'",
        (encrypt(json.dumps(native)),),
    )
    db.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?)",
        [
            ("s-xiaomi", "alice", "mimo:xiaomi", "mimo-v2.5-pro"),
            ("s-deepseek", "alice", "mimo:deepseek", "deepseek-chat"),
        ],
    )
    _insert_endpoint(
        db,
        endpoint_id="ep-deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        api_key="deepseek-endpoint-key",
        models=("deepseek-chat",),
    )
    db.commit()
    db.close()

    plan = build_provider_mapping_plan(database_path=path, owner_assignment=None)

    assert plan.blockers == []
    identities = {
        (
            row.family_id,
            row.adapter_id,
            row.kind,
            row.billing_lane,
            row.normalized_url,
        )
        for row in plan.connections
    }
    assert ("openai", "openai-responses", "subscription", "subscription", None) in identities
    assert ("xiaomi", "mimo-native", "official", "metered_api", None) in identities
    assert ("deepseek", "openai-chat", "official", "metered_api", None) in identities
    assert (
        "deepseek",
        "openai-chat",
        "official",
        "metered_api",
        "https://api.deepseek.com/v1",
    ) in identities
    assert {row.auth_method for row in plan.accounts} <= {"api_key", "oauth"}


def test_mapping_apply_is_idempotent_and_secrets_are_enveloped(tmp_path):
    path = _legacy_database(tmp_path)
    plan = build_provider_mapping_plan(database_path=path, owner_assignment=None)
    url = f"sqlite:///{path}"
    first = apply_provider_mapping_plan(database_url=url, plan=plan)
    second = apply_provider_mapping_plan(database_url=url, plan=plan)
    assert first == second
    db = sqlite3.connect(path)
    try:
        rows = db.execute(
            "SELECT credential_envelope, credential_fingerprint FROM provider_accounts"
        ).fetchall()
        assert len(rows) == 3
        assert all(envelope.startswith('{"alg":"AES-256-GCM"') for envelope, _ in rows)
        assert all(len(fingerprint) == 64 for _, fingerprint in rows)
        raw = "\n".join(str(value) for row in rows for value in row)
        assert "sk-migration-secret" not in raw
        assert "native-access" not in raw
        assert db.execute("SELECT COUNT(*) FROM provider_share_grants").fetchone()[0] == 1
        selectors = db.execute(
            "SELECT account_selector, model_selector, accepted_revision "
            "FROM provider_share_grants"
        ).fetchone()
        assert json.loads(selectors[0])["mode"] == "explicit_accounts"
        assert json.loads(selectors[1])["mode"] == "explicit_models"
        assert selectors[2] == 1
        assert db.execute("SELECT COUNT(*) FROM provider_legacy_aliases").fetchone()[0] >= 3
        assert {
            row[0]
            for row in db.execute("SELECT DISTINCT auth_method FROM provider_accounts")
        } == {"api_key", "oauth"}
        expected_columns = {
            "sessions": {"provider_model_route_id"},
            "scheduled_tasks": {"provider_model_route_id"},
            "crew_members": {"provider_model_route_id"},
            "comparisons": {
                "provider_model_route_a_id",
                "provider_model_route_b_id",
            },
        }
        for table, expected in expected_columns.items():
            actual = {
                row[1]
                for row in db.execute(f'PRAGMA table_info("{table}")')
            }
            assert expected <= actual
        indexes = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert {
            "ix_sessions_provider_model_route_id",
            "ix_scheduled_tasks_provider_model_route_id",
            "ix_crew_members_provider_model_route_id",
            "ix_comparisons_provider_model_route_a_id",
            "ix_comparisons_provider_model_route_b_id",
        } <= indexes
    finally:
        db.close()


def test_duplicate_official_urls_and_exact_credentials_dedupe_within_connection(tmp_path):
    path = _legacy_database(tmp_path)
    db = sqlite3.connect(path)
    _insert_endpoint(
        db,
        endpoint_id="ep-openai-copy",
        name="OpenAI duplicate",
        base_url=(
            "https://url-user:url-secret@API.OPENAI.COM/v1/"
            "?api_key=query-secret"
        ),
        api_key="sk-migration-secret",
        models=("gpt-test",),
    )
    _insert_endpoint(
        db,
        endpoint_id="ep-openai-other-route",
        name="OpenAI distinct route",
        base_url="https://api.openai.com/path-secret/v2",
        api_key="sk-migration-secret",
        models=("gpt-test",),
    )
    db.commit()
    db.close()

    plan = build_provider_mapping_plan(database_path=path, owner_assignment=None)
    metered = [row for row in plan.connections if row.billing_lane == "metered_api"]
    assert len(metered) == 2
    assert all(
        len([row for row in plan.accounts if row.connection_id == connection.id]) == 1
        for connection in metered
    )
    endpoint_aliases = {
        row.legacy_id: row.connection_id
        for row in plan.aliases
        if row.legacy_kind == "endpoint"
    }
    assert endpoint_aliases["ep-openai"] == endpoint_aliases["ep-openai-copy"]
    assert (
        endpoint_aliases["ep-openai"]
        != endpoint_aliases["ep-openai-other-route"]
    )

    safe = json.dumps(plan.safe_report())
    for secret in (
        "sk-migration-secret",
        "url-secret",
        "query-secret",
        "path-secret",
    ):
        assert secret not in safe

    counts = apply_provider_mapping_plan(database_url=f"sqlite:///{path}", plan=plan)
    assert counts["accounts"] == 4


def test_keyless_local_route_has_no_synthetic_account(tmp_path):
    path = _legacy_database(tmp_path)
    db = sqlite3.connect(path)
    _insert_endpoint(
        db,
        endpoint_id="ep-ollama",
        name="Ollama local",
        base_url="http://localhost:11434/v1",
        models=("llama-local",),
        endpoint_kind="local",
    )
    db.commit()
    db.close()

    plan = build_provider_mapping_plan(database_path=path, owner_assignment=None)
    local = next(row for row in plan.connections if row.family_id == "ollama")
    assert local.kind == "local"
    assert local.billing_lane == "local"
    assert [row for row in plan.accounts if row.connection_id == local.id] == []
    route = next(row for row in plan.routes if row.provider_model_id == "llama-local")
    assert route.connection_id == local.id
    assert route.operations == ["chat.stream", "chat.complete"]


def test_chatgpt_copilot_and_native_auth_map_to_subscription_accounts(tmp_path):
    path = _legacy_database(tmp_path)
    db = sqlite3.connect(path)
    db.executemany(
        "INSERT INTO provider_auth_sessions VALUES (?,?,?,?,?,?,?)",
        [
            (
                "auth-chatgpt",
                "chatgpt-subscription",
                "alice",
                "ChatGPT account",
                encrypt("chatgpt-access-secret"),
                encrypt("chatgpt-refresh-secret"),
                "chatgpt-account",
            ),
            (
                "auth-copilot",
                "github-copilot",
                "alice",
                "Copilot account",
                encrypt("copilot-access-secret"),
                encrypt("copilot-refresh-secret"),
                "copilot-account",
            ),
        ],
    )
    _insert_endpoint(
        db,
        endpoint_id="ep-chatgpt",
        name="ChatGPT subscription",
        base_url="https://chatgpt.com/backend-api/codex",
        models=("gpt-codex",),
        provider_auth_id="auth-chatgpt",
    )
    _insert_endpoint(
        db,
        endpoint_id="ep-copilot",
        name="GitHub Copilot",
        base_url="https://copilot-api.enterprise.example",
        models=("copilot-chat",),
        provider_auth_id="auth-copilot",
    )
    db.commit()
    db.close()

    plan = build_provider_mapping_plan(database_path=path, owner_assignment=None)
    chatgpt = next(
        row
        for row in plan.connections
        if row.adapter_id == "openai-responses"
        and row.billing_lane == "subscription"
        and row.normalized_url is not None
    )
    copilot = next(row for row in plan.connections if row.adapter_id == "copilot-chat")
    native = next(
        row
        for row in plan.connections
        if row.adapter_id == "openai-responses"
        and row.billing_lane == "subscription"
        and row.normalized_url is None
    )
    for connection in (chatgpt, copilot, native):
        assert connection.kind == "subscription"
        mapped = [row for row in plan.accounts if row.connection_id == connection.id]
        assert mapped and all(row.auth_class == "subscription" for row in mapped)
        assert all(row.auth_method == "oauth" for row in mapped)
    assert chatgpt.family_id == "openai"
    assert copilot.family_id == "github-copilot"

    safe = json.dumps(plan.safe_report())
    for secret in (
        "chatgpt-access-secret",
        "chatgpt-refresh-secret",
        "copilot-access-secret",
        "copilot-refresh-secret",
        "native-access",
        "native-refresh",
    ):
        assert secret not in safe


def test_active_task_and_all_global_default_modalities_get_stable_routes(tmp_path):
    path = _legacy_database(tmp_path)
    db = sqlite3.connect(path)
    db.execute(
        "UPDATE model_endpoints SET pinned_models=? WHERE id='ep-openai'",
        (json.dumps(["gpt-fallback"]),),
    )
    db.execute(
        "INSERT INTO scheduled_tasks VALUES (?,?,?,?,?,?,?)",
        (
            "task-active",
            "alice",
            "active",
            "llm",
            "ep-openai",
            "https://api.openai.com/v1",
            "gpt-test",
        ),
    )
    db.commit()
    db.close()
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "default_endpoint_id": "ep-openai",
                "default_model": "gpt-test",
                "default_model_fallbacks": ["gpt-fallback"],
                "utility_endpoint_id": "ep-openai",
                "utility_model": "gpt-test",
                "research_endpoint_id": "ep-openai",
                "research_model": "gpt-test",
                "task_endpoint_id": "ep-openai",
                "task_model": "gpt-test",
                "vision_endpoint_id": "ep-openai",
                "vision_model": "gpt-test",
                "image_endpoint_id": "ep-openai",
                "image_model": "gpt-test",
                "tts_endpoint_id": "ep-openai",
                "tts_model": "gpt-test",
                "stt_endpoint_id": "ep-openai",
                "stt_model": "gpt-test",
            }
        ),
        encoding="utf-8",
    )

    plan = build_provider_mapping_plan(
        database_path=path,
        owner_assignment="alice",
        settings_path=settings,
    )
    assert plan.blockers == []
    task_reference = next(
        row
        for row in plan.references
        if row.table == "scheduled_tasks" and row.row_id == "task-active"
    )
    assert task_reference.active is True
    assert {row.purpose for row in plan.route_bindings} == {
        "chat",
        "utility",
        "memory",
        "research",
        "tasks",
        "vision",
        "images",
        "tts",
        "stt",
    }
    assert [
        row.ordinal
        for row in plan.route_bindings
        if row.purpose == "chat"
    ] == [0, 1]
    utility_routes = [
        (row.ordinal, row.model_route_id)
        for row in plan.route_bindings
        if row.purpose == "utility"
    ]
    memory_routes = [
        (row.ordinal, row.model_route_id)
        for row in plan.route_bindings
        if row.purpose == "memory"
    ]
    # First migration clones Utility so existing owners see no behavior
    # change, but the purpose IDs remain independent for later edits.
    assert memory_routes == utility_routes


def test_active_task_with_unresolved_route_remains_a_hard_blocker(tmp_path):
    path = _legacy_database(tmp_path)
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO scheduled_tasks VALUES (?,?,?,?,?,?,?)",
        (
            "task-unresolved",
            "alice",
            "active",
            "llm",
            "missing-endpoint",
            "https://missing.invalid/v1",
            "missing-model",
        ),
    )
    db.commit()
    db.close()

    plan = build_provider_mapping_plan(database_path=path, owner_assignment=None)
    assert any(
        "active scheduled_tasks record task-unresolved" in item
        for item in plan.blockers
    )


def test_malformed_provider_json_blocks_without_echoing_contents(tmp_path):
    path = _legacy_database(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text('{"api_key":"plaintext-setting-secret",', encoding="utf-8")
    plan = build_provider_mapping_plan(
        database_path=path,
        owner_assignment="alice",
        settings_path=settings,
    )
    assert any("settings.json is malformed" in item for item in plan.blockers)
    assert "plaintext-setting-secret" not in json.dumps(plan.safe_report())

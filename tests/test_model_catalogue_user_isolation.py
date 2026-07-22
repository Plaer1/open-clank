"""Cross-user proof for the model catalogue ownership boundary."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as database
import routes.model_routes as model_routes
import routes.prefs_routes as prefs_routes
import src.database as src_database
from core.database import Base, ModelEndpoint
from services.stt.stt_service import STTService
from services.tts.tts_service import TTSService
from src.agent_tools.admin_tools import do_manage_endpoints


def _route(router, path: str, method: str = "GET"):
    for item in router.routes:
        if item.path == path and method in item.methods:
            return item.endpoint
    raise AssertionError(f"missing {method} {path}")


def _request(owner: str, *, body=None):
    class Request:
        state = SimpleNamespace(current_user=owner, api_token=False)
        app = SimpleNamespace(
            state=SimpleNamespace(auth_manager=SimpleNamespace(is_configured=True))
        )
        headers = {"content-length": "1" if body is not None else "0"}

        async def json(self):
            return body or {}

    return Request()


@pytest.fixture
def endpoint_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(model_routes, "SessionLocal", session_factory)
    monkeypatch.setattr(model_routes, "require_admin", lambda _request: None)
    monkeypatch.setattr(model_routes, "_load_settings", lambda: {})
    monkeypatch.setattr(model_routes, "_save_settings", lambda _settings: None)
    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda _owner: {})
    monkeypatch.setattr(prefs_routes, "_save_for_user", lambda _owner, _prefs: None)
    monkeypatch.setattr(database, "SessionLocal", session_factory)
    monkeypatch.setattr(src_database, "SessionLocal", session_factory)
    monkeypatch.setattr(src_database, "ModelEndpoint", ModelEndpoint)
    return session_factory


def _seed_endpoints(session_factory):
    db = session_factory()
    try:
        db.add_all([
            ModelEndpoint(
                id="alice-ep", name="Alice", base_url="https://alice.invalid/v1",
                cached_models=json.dumps(["alice-model"]), owner="alice",
            ),
            ModelEndpoint(
                id="bob-ep", name="Bob", base_url="https://bob.invalid/v1",
                cached_models=json.dumps(["bob-model"]), owner="bob",
            ),
            ModelEndpoint(
                id="legacy-ep", name="Legacy", base_url="https://legacy.invalid/v1",
                cached_models=json.dumps(["legacy-model"]), owner=None,
            ),
        ])
        db.commit()
    finally:
        db.close()


def test_catalogue_and_admin_endpoint_list_are_exact_owner(endpoint_db):
    _seed_endpoints(endpoint_db)
    router = model_routes.setup_model_routes(model_discovery=None)

    alice_catalogue = _route(router, "/api/models")(_request("alice"))
    bob_catalogue = _route(router, "/api/models")(_request("bob"))
    assert [item["endpoint_id"] for item in alice_catalogue["items"]] == ["alice-ep"]
    assert [item["endpoint_id"] for item in bob_catalogue["items"]] == ["bob-ep"]

    alice_admin = _route(router, "/api/model-endpoints")(_request("alice"))
    bob_admin = _route(router, "/api/model-endpoints")(_request("bob"))
    assert [item["id"] for item in alice_admin] == ["alice-ep"]
    assert [item["id"] for item in bob_admin] == ["bob-ep"]


def test_cookbook_stale_cleanup_only_mutates_callers_catalogue(
    endpoint_db, monkeypatch
):
    db = endpoint_db()
    try:
        db.add_all(
            [
                ModelEndpoint(
                    id="local-alice",
                    name="Alice local",
                    base_url="http://127.0.0.1:8101/v1",
                    owner="alice",
                ),
                ModelEndpoint(
                    id="local-bob",
                    name="Bob local",
                    base_url="http://127.0.0.1:8102/v1",
                    owner="bob",
                ),
            ]
        )
        db.commit()

        prefs = {
            "alice": {"default_endpoint_id": "local-alice", "default_model": "a"},
            "bob": {"default_endpoint_id": "local-bob", "default_model": "b"},
        }
        monkeypatch.setattr(
            model_routes, "_active_cookbook_endpoint_ids", lambda: {"local-live"}
        )
        monkeypatch.setattr(
            prefs_routes, "_load_for_user", lambda owner: dict(prefs[owner])
        )
        monkeypatch.setattr(
            prefs_routes,
            "_save_for_user",
            lambda owner, value: prefs.__setitem__(owner, dict(value)),
        )

        assert model_routes._disable_stale_cookbook_local_endpoints(db, "alice") == 1
        assert db.get(ModelEndpoint, "local-alice").is_enabled is False
        assert db.get(ModelEndpoint, "local-bob").is_enabled is True
        assert prefs["alice"]["default_endpoint_id"] == ""
        assert prefs["bob"]["default_endpoint_id"] == "local-bob"
    finally:
        db.close()


def test_raw_endpoint_id_cannot_cross_owner(endpoint_db):
    _seed_endpoints(endpoint_db)
    router = model_routes.setup_model_routes(model_discovery=None)
    list_models = _route(router, "/api/model-endpoints/{ep_id}/models")
    toggle = _route(router, "/api/model-endpoints/{ep_id}", "PATCH")
    delete = _route(router, "/api/model-endpoints/{ep_id}", "DELETE")

    with pytest.raises(HTTPException) as list_exc:
        list_models("bob-ep", _request("alice"), Response())
    assert list_exc.value.status_code == 404

    with pytest.raises(HTTPException) as patch_exc:
        asyncio.run(toggle("bob-ep", _request("alice", body={"is_enabled": False})))
    assert patch_exc.value.status_code == 404

    with pytest.raises(HTTPException) as delete_exc:
        delete("bob-ep", _request("alice"))
    assert delete_exc.value.status_code == 404

    db = endpoint_db()
    try:
        assert db.get(ModelEndpoint, "bob-ep") is not None
        assert db.get(ModelEndpoint, "bob-ep").is_enabled is True
    finally:
        db.close()


def test_same_url_creates_independent_rows_per_owner(endpoint_db, monkeypatch):
    router = model_routes.setup_model_routes(model_discovery=None)
    create = _route(router, "/api/model-endpoints", "POST")
    monkeypatch.setattr("src.endpoint_resolver.resolve_url", lambda url: url)

    kwargs = {
        "name": "Same provider",
        "base_url": "https://same.invalid/v1",
        "api_key": "",
        "skip_probe": "true",
        "require_models": "false",
        "model_type": "llm",
        "endpoint_kind": "auto",
        "model_refresh_mode": "",
        "model_refresh_interval": "",
        "model_refresh_timeout": "",
        "supports_tools": "",
        "pinned_models": "model-1",
        "container_local": "false",
    }
    alice = create(_request("alice"), **kwargs)
    bob = create(_request("bob"), **kwargs)

    assert alice["id"] != bob["id"]
    db = endpoint_db()
    try:
        rows = db.query(ModelEndpoint).filter(
            ModelEndpoint.base_url == "https://same.invalid/v1"
        ).all()
        assert {row.owner for row in rows} == {"alice", "bob"}
    finally:
        db.close()


def test_first_endpoint_default_is_saved_to_callers_preferences(endpoint_db, monkeypatch):
    saved = {}
    monkeypatch.setattr("src.endpoint_resolver.resolve_url", lambda url: url)
    monkeypatch.setattr(
        model_routes,
        "_probe_endpoint_result",
        lambda *_args, **_kwargs: (["alice-model"], {"status": "ok"}),
    )
    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda owner: dict(saved.get(owner, {})))
    monkeypatch.setattr(
        prefs_routes,
        "_save_for_user",
        lambda owner, prefs: saved.__setitem__(owner, dict(prefs)),
    )
    router = model_routes.setup_model_routes(model_discovery=None)
    create = _route(router, "/api/model-endpoints", "POST")

    create(
        _request("alice"),
        name="Alice provider",
        base_url="https://alice-new.invalid/v1",
        api_key="",
        skip_probe="false",
        require_models="false",
        model_type="llm",
        endpoint_kind="auto",
        model_refresh_mode="",
        model_refresh_interval="",
        model_refresh_timeout="",
        supports_tools="",
        pinned_models="",
        container_local="false",
    )

    assert saved["alice"]["default_model"] == "alice-model"
    assert saved["alice"]["default_endpoint_id"]
    assert "bob" not in saved


def test_agent_endpoint_tool_is_owner_scoped(endpoint_db):
    _seed_endpoints(endpoint_db)

    alice = asyncio.run(do_manage_endpoints('{"action":"list"}', owner="alice"))
    assert [item["id"] for item in alice["endpoints"]] == ["alice-ep"]

    denied = asyncio.run(do_manage_endpoints(
        '{"action":"delete","endpoint_id":"bob-ep"}', owner="alice"
    ))
    assert denied["exit_code"] == 1

    added = asyncio.run(do_manage_endpoints(
        '{"action":"add","name":"Alice second","base_url":"https://second.invalid/v1"}',
        owner="alice",
    ))
    assert added["exit_code"] == 0
    db = endpoint_db()
    try:
        assert db.query(ModelEndpoint).filter(
            ModelEndpoint.name == "Alice second", ModelEndpoint.owner == "alice"
        ).count() == 1
        assert db.get(ModelEndpoint, "bob-ep") is not None
    finally:
        db.close()


def test_speech_services_refuse_foreign_endpoint_credentials(endpoint_db, tmp_path, monkeypatch):
    _seed_endpoints(endpoint_db)
    tts = TTSService(cache_dir=str(tmp_path / "tts"))
    stt = STTService()
    monkeypatch.setattr(tts, "_load_settings", lambda owner=None: {
        "tts_enabled": True,
        "tts_provider": "endpoint:bob-ep",
        "tts_model": "tts-1",
        "tts_voice": "alloy",
        "tts_speed": "1",
    })
    monkeypatch.setattr(stt, "_load_settings", lambda owner=None: {
        "stt_enabled": True,
        "stt_provider": "endpoint:bob-ep",
        "stt_model": "whisper-1",
        "stt_language": "",
    })
    monkeypatch.setattr(
        "services.tts.tts_service.httpx.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("foreign TTS called")),
    )
    monkeypatch.setattr(
        "services.stt.stt_service.httpx.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("foreign STT called")),
    )

    assert tts.is_available("alice") is False
    assert tts.synthesize("private", use_cache=False, owner="alice") is None
    assert stt.is_available("alice") is False
    assert stt.transcribe(b"private", owner="alice") is None


def test_legacy_endpoint_and_provider_auth_are_claimed_before_backfill(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"users": {"e": {"is_admin": True}, "sam": {"is_admin": False}}}),
        encoding="utf-8",
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE model_endpoints (
                id TEXT PRIMARY KEY, base_url TEXT, owner TEXT, is_enabled INTEGER
            );
            CREATE TABLE provider_auth_sessions (id TEXT PRIMARY KEY, owner TEXT);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, endpoint_url TEXT, endpoint_id TEXT, owner TEXT
            );
            INSERT INTO model_endpoints VALUES ('legacy-ep', 'https://legacy.invalid/v1', NULL, 1);
            INSERT INTO provider_auth_sessions VALUES ('legacy-auth', NULL);
            INSERT INTO sessions VALUES ('legacy-session', 'https://legacy.invalid/v1/chat/completions', NULL, NULL);
            """
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "MEMORY_FILE", str(tmp_path / "missing-memory.json"))
    monkeypatch.setattr(database, "USER_PREFS_FILE", str(tmp_path / "missing-prefs.json"))

    database._migrate_assign_legacy_owner()
    database._migrate_add_session_endpoint_id_column()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT owner FROM model_endpoints WHERE id='legacy-ep'"
        ).fetchone() == ("e",)
        assert conn.execute(
            "SELECT owner FROM provider_auth_sessions WHERE id='legacy-auth'"
        ).fetchone() == ("e",)
        assert conn.execute(
            "SELECT owner, endpoint_id FROM sessions WHERE id='legacy-session'"
        ).fetchone() == ("e", "legacy-ep")
    finally:
        conn.close()


def test_auth_disabled_migration_preserves_ownerless_model_catalogue(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"users": {"e": {"is_admin": True}}}),
        encoding="utf-8",
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE model_endpoints (id TEXT PRIMARY KEY, owner TEXT);
            CREATE TABLE provider_auth_sessions (id TEXT PRIMARY KEY, owner TEXT);
            INSERT INTO model_endpoints VALUES ('single-user-ep', NULL);
            INSERT INTO provider_auth_sessions VALUES ('single-user-auth', NULL);
            """
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "MEMORY_FILE", str(tmp_path / "missing-memory.json"))
    monkeypatch.setattr(database, "USER_PREFS_FILE", str(tmp_path / "missing-prefs.json"))

    database._migrate_assign_legacy_owner()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT owner FROM model_endpoints WHERE id='single-user-ep'"
        ).fetchone() == (None,)
        assert conn.execute(
            "SELECT owner FROM provider_auth_sessions WHERE id='single-user-auth'"
        ).fetchone() == (None,)
    finally:
        conn.close()

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.auth_routes as auth_routes
import routes.prefs_routes as prefs_routes
import src.builtin_actions as builtin_actions
import src.settings as settings_module
from services.stt.stt_service import STTService
from services.tts.tts_service import TTSService


class _AuthManager:
    is_configured = True

    def get_username_for_token(self, token):
        return {"alice-token": "alice", "bob-token": "bob"}.get(token)

    def is_admin(self, user):
        return user == "alice"


class _Request:
    def __init__(self, token=None, body=None):
        self.cookies = {auth_routes.SESSION_COOKIE: token} if token else {}
        self._body = body or {}
        self.app = SimpleNamespace(state=SimpleNamespace(mimo_supervisor=None))

    async def json(self):
        return self._body


def _route(router, path, method):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"missing {method} {path}")


@pytest.fixture
def settings_routes(monkeypatch):
    global_settings = dict(settings_module.DEFAULT_SETTINGS)
    global_settings.update({
        "default_endpoint_id": "global-endpoint",
        "default_model": "global-model",
        "tts_provider": "endpoint:global-speech",
        "search_provider": "searxng",
    })
    prefs = {
        "alice": {"default_endpoint_id": "alice-endpoint", "default_model": ""},
        "bob": {},
    }
    saved_global = []
    validated = []

    monkeypatch.setattr(auth_routes, "migrate_from_settings", lambda: None)
    monkeypatch.setattr(auth_routes, "_load_settings", lambda: dict(global_settings))
    monkeypatch.setattr(auth_routes, "_save_settings", lambda value: saved_global.append(dict(value)))
    monkeypatch.setattr(auth_routes, "_load_for_user", lambda user: dict(prefs.get(user, {})))
    monkeypatch.setattr(
        auth_routes,
        "_save_for_user",
        lambda user, value: prefs.__setitem__(user, dict(value)),
    )
    monkeypatch.setattr(
        auth_routes,
        "_validate_model_settings_update",
        lambda body, current, request, owner: validated.append((dict(body), owner)),
    )

    router = auth_routes.setup_auth_routes(_AuthManager())
    return {
        "get": _route(router, "/api/auth/settings", "GET"),
        "post": _route(router, "/api/auth/settings", "POST"),
        "global": global_settings,
        "saved_global": saved_global,
        "prefs": prefs,
        "validated": validated,
    }


def test_settings_reads_are_user_scoped_and_preserve_explicit_empty(settings_routes):
    alice = asyncio.run(settings_routes["get"](_Request("alice-token")))
    bob = asyncio.run(settings_routes["get"](_Request("bob-token")))

    assert alice["default_endpoint_id"] == "alice-endpoint"
    assert alice["default_model"] == ""
    assert bob["default_endpoint_id"] == settings_module.DEFAULT_SETTINGS["default_endpoint_id"]
    assert bob["default_model"] == settings_module.DEFAULT_SETTINGS["default_model"]
    assert bob["tts_provider"] == settings_module.DEFAULT_SETTINGS["tts_provider"]
    assert bob["search_provider"] == "searxng"


def test_regular_user_saves_only_personal_model_settings(settings_routes):
    result = asyncio.run(settings_routes["post"](_Request(
        "bob-token",
        {"default_endpoint_id": "bob-endpoint", "default_model": ""},
    )))

    assert settings_routes["prefs"]["bob"]["default_endpoint_id"] == "bob-endpoint"
    assert settings_routes["prefs"]["bob"]["default_model"] == ""
    assert settings_routes["saved_global"] == []
    assert settings_routes["validated"][-1][1] == "bob"
    assert result["default_model"] == ""

    with pytest.raises(HTTPException) as exc:
        asyncio.run(settings_routes["post"](_Request(
            "bob-token", {"search_provider": "duckduckgo"}
        )))
    assert exc.value.status_code == 403


def test_admin_model_choice_is_personal_while_policy_remains_global(settings_routes):
    asyncio.run(settings_routes["post"](_Request(
        "alice-token",
        {
            "default_endpoint_id": "alice-next",
            "default_model": "alice-model",
            "search_provider": "duckduckgo",
        },
    )))

    assert settings_routes["prefs"]["alice"]["default_endpoint_id"] == "alice-next"
    assert settings_routes["prefs"]["alice"]["default_model"] == "alice-model"
    assert settings_routes["saved_global"][-1]["search_provider"] == "duckduckgo"
    assert settings_routes["saved_global"][-1]["default_endpoint_id"] == "global-endpoint"


def test_auth_disabled_keeps_single_user_global_storage(settings_routes, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    visible = asyncio.run(settings_routes["get"](_Request()))
    assert visible["default_endpoint_id"] == "global-endpoint"

    asyncio.run(settings_routes["post"](_Request(body={
        "default_endpoint_id": "local-endpoint",
        "default_model": "local-model",
    })))
    assert settings_routes["saved_global"][-1]["default_endpoint_id"] == "local-endpoint"


def test_get_user_setting_does_not_fall_back_to_global_or_drop_empty(monkeypatch):
    monkeypatch.setattr(
        prefs_routes,
        "_load_for_user",
        lambda owner: {"default_model": ""} if owner == "alice" else {},
    )
    monkeypatch.setattr(settings_module, "get_setting", lambda key, default=None: "global-secret")

    assert settings_module.get_user_setting("default_model", "alice") == ""
    assert settings_module.get_user_setting("tts_provider", "bob") == "disabled"
    assert settings_module.get_user_setting("tts_provider", "") == "global-secret"


def test_speech_services_resolve_settings_for_the_supplied_owner(monkeypatch, tmp_path):
    calls = []

    def get_user_setting(key, owner, default=None):
        calls.append((key, owner))
        return default

    monkeypatch.setattr(settings_module, "get_user_setting", get_user_setting)
    TTSService(cache_dir=str(tmp_path / "tts"))._load_settings("alice")
    STTService()._load_settings("bob")

    assert {owner for _, owner in calls[:5]} == {"alice"}
    assert {owner for _, owner in calls[5:]} == {"bob"}


def test_scheduled_cookbook_defaults_update_only_the_task_owner(monkeypatch):
    prefs = {"alice": {}, "bob": {"default_endpoint_id": "bob-endpoint"}}
    global_writes = []
    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda owner: dict(prefs[owner]))
    monkeypatch.setattr(
        prefs_routes,
        "_save_for_user",
        lambda owner, value: prefs.__setitem__(owner, dict(value)),
    )
    monkeypatch.setattr(settings_module, "save_settings", global_writes.append)

    builtin_actions._save_cookbook_model_defaults("alice", "alice-endpoint", "alice-model")

    assert prefs["alice"]["default_endpoint_id"] == "alice-endpoint"
    assert prefs["alice"]["task_endpoint_id"] == "alice-endpoint"
    assert prefs["alice"]["utility_endpoint_id"] == "alice-endpoint"
    assert prefs["bob"] == {"default_endpoint_id": "bob-endpoint"}
    assert global_writes == []

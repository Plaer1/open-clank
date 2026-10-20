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


def test_agent_settings_override_reset_and_owner_isolation(settings_routes):
    saved = asyncio.run(settings_routes["post"](_Request(
        "bob-token",
        {"agent_settings": {"compaction": {"tail_turns": 0, "max_context": "50%"}}},
    )))
    assert saved["agent_settings_override"] is True
    assert settings_routes["prefs"]["bob"]["agent_settings"]["compaction"]["tail_turns"] == 0
    alice = asyncio.run(settings_routes["get"](_Request("alice-token")))
    assert alice["agent_settings_override"] is False
    reset = asyncio.run(settings_routes["post"](_Request("bob-token", {"agent_settings": None})))
    assert reset["agent_settings_override"] is False
    assert "agent_settings" not in settings_routes["prefs"]["bob"]


def test_agent_settings_rejects_invalid_native_quantities(settings_routes):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(settings_routes["post"](_Request(
            "bob-token", {"agent_settings": {"compaction": {"max_context": "soon"}}}
        )))
    assert exc.value.status_code == 400


def _owned_settings_target(request, monkeypatch):
    import routes.session_routes as sessions
    from src.openclank import chat_routing
    monkeypatch.setattr(sessions, "_verify_session_owner", lambda _request, sid: "bob" if sid == "chat-bob" else (_ for _ in ()).throw(HTTPException(404, "Session is unavailable")))
    route = SimpleNamespace(connection_id="bound-connection", provider_model_id="bound-model")
    monkeypatch.setattr(chat_routing, "resolve_chat_route", lambda **_kwargs: route)
    session = SimpleNamespace(endpoint_id="bound-connection", provider_model_route_id="route-bob", workspace_id="workspace-bob")
    request.app.state.session_manager = SimpleNamespace(get_session=lambda sid: session)


def test_settings_readback_uses_bound_chat_route_without_cold_admission(settings_routes, monkeypatch):
    from src.openclank import chat_routing, modality_facade

    route = SimpleNamespace(connection_id="bound-connection", provider_model_id="bound-model")
    monkeypatch.setattr(modality_facade, "_configured_text_route", lambda **_kwargs: route)
    monkeypatch.setattr(chat_routing, "resolve_chat_model_spec", lambda **_kwargs: pytest.fail("catalog fallback should not run"))

    class ReadyWorker:
        async def ready_effective_agent_settings(self, owner, provider_id, model_id, *, session_id):
            raise AssertionError("supervisor seam should be called directly")

    class ReadySupervisor:
        def __init__(self):
            self.calls = []
            self.admit_calls = 0

        async def ready_effective_agent_settings(self, owner, provider_id, model_id, *, session_id):
            self.calls.append((owner, provider_id, model_id, session_id))
            return {"context": {"hard": 1000, "usable": 800}, "compaction": {"tailTurns": 2}}

        async def admit_agent(self, *_args):
            self.admit_calls += 1
            raise AssertionError("readback must not cold-admit a worker")

    request = _Request("bob-token")
    supervisor = ReadySupervisor()
    request.app.state.mimo_supervisor = supervisor
    _owned_settings_target(request, monkeypatch)
    visible = asyncio.run(settings_routes["get"](request, session_id="chat-bob"))
    assert visible["agent_settings_effective"]["status"] == "available"
    assert visible["agent_settings_effective"]["target"]["session_id"] == "chat-bob"
    assert supervisor.calls == [("bob", "bound-connection", "bound-model", "chat-bob")]
    assert supervisor.admit_calls == 0


def test_settings_readback_preserves_stable_unavailable_reason(settings_routes, monkeypatch):
    from src.openclank import modality_facade

    monkeypatch.setattr(modality_facade, "_configured_text_route", lambda **_kwargs: SimpleNamespace(connection_id="bound", provider_model_id="model"))

    class Supervisor:
        async def ready_effective_agent_settings(self, *_args, **_kwargs):
            error = RuntimeError("provider secret must never leak")
            error.code = "MODEL_NOT_PROJECTED"
            raise error

    request = _Request("bob-token")
    request.app.state.mimo_supervisor = Supervisor()
    _owned_settings_target(request, monkeypatch)
    visible = asyncio.run(settings_routes["get"](request, session_id="chat-bob"))
    assert visible["agent_settings_effective"]["status"] == "unavailable"
    assert visible["agent_settings_effective"]["reason"] == "MODEL_NOT_PROJECTED"
    assert "provider secret" not in repr(visible)


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


def test_settings_require_authenticated_owner_even_when_env_auth_disabled(settings_routes, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    assert asyncio.run(settings_routes["get"](_Request())) == {}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(settings_routes["post"](_Request(body={"default_model": "local-model"})))
    assert exc.value.status_code == 403
    assert settings_routes["saved_global"] == []


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


def test_agent_settings_sparse_save_reset_conflict_and_lossless_values(settings_routes):
    first = asyncio.run(settings_routes["post"](_Request("bob-token", {"agent_settings": {
        "compaction": {"preserve_recent_tokens": 12000, "max_context": {"provider/model*": "50%"}},
        "checkpoint": {"thresholds": [], "push_caps": {"recent_user": 0, "notes": 4500}},
    }})))
    revision = first["agent_settings_revision"]
    saved = asyncio.run(settings_routes["post"](_Request("bob-token", {
        "agent_settings_expected_revision": revision,
        "agent_settings": {"compaction": {"auto": False}},
        "agent_settings_reset": ["checkpoint.push_caps.notes"],
    })))
    raw = saved["agent_settings_saved"]
    assert raw["compaction"] == {"auto": False, "preserve_recent_tokens": 12000, "max_context": {"provider/model*": "50%"}}
    assert raw["checkpoint"] == {"thresholds": [], "push_caps": {"recent_user": 0}}
    assert saved["agent_settings_revision"] != revision
    with pytest.raises(HTTPException) as error:
        asyncio.run(settings_routes["post"](_Request("bob-token", {"agent_settings_expected_revision": revision, "agent_settings": {"compaction": {"auto": True}}})))
    assert error.value.status_code == 409
    assert settings_routes["prefs"]["bob"]["agent_settings"]["compaction"]["auto"] is False


def test_settings_no_target_is_observational(settings_routes):
    request = _Request("bob-token")
    request.app.state.mimo_supervisor = SimpleNamespace(ready_effective_agent_settings=lambda *args: pytest.fail("unscoped readback"))
    result = asyncio.run(settings_routes["get"](request))
    assert result["agent_settings_effective"] == {"status": "pending", "reason": "TARGET_REQUIRED"}
    assert result["agent_settings_schema"]["version"] == 1


def test_settings_target_cannot_read_another_owner(settings_routes, monkeypatch):
    request = _Request("bob-token")
    _owned_settings_target(request, monkeypatch)
    with pytest.raises(HTTPException) as error:
        asyncio.run(settings_routes["get"](request, session_id="chat-alice"))
    assert error.value.status_code == 404


def test_whole_legacy_null_is_observational_inherit_and_field_null_resets(settings_routes):
    from src.agent_settings import agent_settings_revision
    settings_routes["prefs"]["bob"]["agent_settings"] = None
    result = asyncio.run(settings_routes["get"](_Request("bob-token")))
    assert result["agent_settings_saved"] == {} and result["agent_settings_override"] is False
    assert settings_routes["prefs"]["bob"]["agent_settings"] is None
    assert result["agent_settings_revision"] == agent_settings_revision(None)
    settings_routes["prefs"]["bob"]["agent_settings"] = {"compaction": {"max_context": "300K", "auto": False}}
    result = asyncio.run(settings_routes["post"](_Request("bob-token", {"agent_settings": {"compaction": {"max_context": None}}})))
    assert result["agent_settings_saved"] == {"compaction": {"auto": False}}

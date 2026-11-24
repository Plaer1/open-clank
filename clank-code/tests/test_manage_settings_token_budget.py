"""Regression: agent_input_token_budget must be settable from chat (not flagged secret)."""
import asyncio
import json

import src.settings as settings_mod
import routes.prefs_routes as prefs_routes
from src.agent_tools.admin_tools import do_manage_settings


def test_set_token_budget_is_not_refused_as_secret(monkeypatch):
    store = {}
    monkeypatch.setattr(settings_mod, "load_settings", lambda: dict(store))
    monkeypatch.setattr(settings_mod, "save_settings", lambda s: store.update(s))

    result = asyncio.run(do_manage_settings(json.dumps({
        "action": "set", "key": "agent_input_token_budget", "value": 8000,
    })))

    # The "token" substring used to flag this int setting as a credential and
    # refuse to set it (even though there's a deliberate "token budget" alias).
    assert "credential" not in result.get("response", "").lower(), result
    assert result.get("exit_code") == 0, result
    assert store.get("agent_input_token_budget") == 8000


def test_model_setting_tool_reads_and_writes_only_callers_preferences(monkeypatch):
    global_store = dict(settings_mod.DEFAULT_SETTINGS)
    global_store["tts_provider"] = "endpoint:operators-speech"
    prefs = {"alice": {"tts_voice": "alice-voice"}, "bob": {}}
    monkeypatch.setattr(settings_mod, "load_settings", lambda: dict(global_store))
    monkeypatch.setattr(settings_mod, "save_settings", lambda value: global_store.update(value))
    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda owner: dict(prefs.get(owner, {})))
    monkeypatch.setattr(
        prefs_routes,
        "_save_for_user",
        lambda owner, value: prefs.__setitem__(owner, dict(value)),
    )

    listed = asyncio.run(do_manage_settings(json.dumps({"action": "list"}), owner="bob"))
    assert listed["settings"]["tts_provider"] == "disabled"

    result = asyncio.run(do_manage_settings(json.dumps({
        "action": "set", "key": "tts voice", "value": "bob-voice",
    }), owner="bob"))
    assert result["exit_code"] == 0
    assert prefs["bob"]["tts_voice"] == "bob-voice"
    assert prefs["alice"]["tts_voice"] == "alice-voice"
    assert global_store["tts_provider"] == "endpoint:operators-speech"

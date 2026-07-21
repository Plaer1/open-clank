"""Identity metaplan Slice 03 — the synced editable default persona.

Rulings under test (2026-07-16):
- R10: the default persona is real — factory name "Odysseus" + an editable
  prompt; chat turns without an explicit persona speak as it.
- R13: ONE state across the three stores — preset record, assistant
  CrewMember row, reminder default voice — synced in both directions.
- R15: background/utility voices resolve the default persona; mechanical
  task framing stays separate from the voice.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src import default_persona
from src.default_persona import (
    FACTORY_NAME,
    FACTORY_PROMPT,
    get_default_persona,
    reset_default_persona,
    set_default_persona,
    sync_from_assistant,
)
from src.preset_manager import PresetManager
from routes.preset_routes import setup_preset_routes


@pytest.fixture
def manager(tmp_path, monkeypatch):
    mgr = PresetManager(str(tmp_path))
    monkeypatch.setattr(default_persona, "_preset_manager", mgr)
    # Keep the DB out of unit scope; the push seam is covered separately.
    pushed = []
    monkeypatch.setattr(
        default_persona, "_push_to_assistant", lambda owner, record: pushed.append((owner, record))
    )
    mgr._pushed = pushed
    return mgr


def test_factory_default_when_never_edited(manager):
    record = get_default_persona("alice")
    assert record["name"] == FACTORY_NAME == "Odysseus"
    assert record["system_prompt"] == FACTORY_PROMPT
    assert record["is_factory"] is True


def test_set_roundtrip_and_owner_isolation(manager):
    set_default_persona("alice", name="Nyx", system_prompt="Speak in riddles.")
    alice = get_default_persona("alice")
    bob = get_default_persona("bob")
    assert alice["name"] == "Nyx"
    assert alice["system_prompt"] == "Speak in riddles."
    assert alice["is_factory"] is False
    assert bob["name"] == FACTORY_NAME
    assert bob["is_factory"] is True


def test_set_persists_through_reload(manager, tmp_path):
    set_default_persona("alice", name="Nyx", system_prompt="Speak in riddles.")
    reloaded = PresetManager(str(tmp_path))
    record = get_default_persona("alice", preset_manager=reloaded)
    assert record["name"] == "Nyx"


def test_edit_pushes_to_assistant_store(manager):
    set_default_persona("alice", name="Nyx", system_prompt="Speak in riddles.")
    assert manager._pushed == [("alice", {"name": "Nyx", "system_prompt": "Speak in riddles."})]


def test_assistant_edit_syncs_back_without_push_loop(manager):
    sync_from_assistant("alice", name="Muse", personality="Sing, goddess.")
    record = get_default_persona("alice")
    assert record["name"] == "Muse"
    assert record["system_prompt"] == "Sing, goddess."
    assert manager._pushed == []  # source of the edit — no push back


def test_reset_restores_factory_and_syncs(manager):
    set_default_persona("alice", name="Nyx", system_prompt="Speak in riddles.")
    reset_default_persona("alice")
    record = get_default_persona("alice")
    assert record["name"] == FACTORY_NAME
    assert record["system_prompt"] == FACTORY_PROMPT
    assert manager._pushed[-1] == (
        "alice",
        {"name": FACTORY_NAME, "system_prompt": FACTORY_PROMPT},
    )


def test_partial_update_keeps_other_field(manager):
    set_default_persona("alice", name="Nyx", system_prompt="Speak in riddles.")
    set_default_persona("alice", system_prompt="Speak plainly.")
    record = get_default_persona("alice")
    assert record["name"] == "Nyx"
    assert record["system_prompt"] == "Speak plainly."


def test_chat_turn_without_persona_speaks_as_default(manager):
    from src.chat_handler import ChatHandler

    set_default_persona("alice", name="Nyx", system_prompt="Speak in riddles.")
    handler = ChatHandler.__new__(ChatHandler)
    handler.preset_manager = manager
    temp, tokens, prompt, character = handler.validate_and_extract_preset(None, owner="alice")
    assert character == "Nyx"
    assert prompt == "Your name is Nyx. Speak in riddles."


def test_chat_turn_with_explicit_preset_keeps_that_persona(manager):
    from src.chat_handler import ChatHandler

    set_default_persona("alice", name="Nyx", system_prompt="Speak in riddles.")
    handler = ChatHandler.__new__(ChatHandler)
    handler.preset_manager = manager
    _, _, prompt, character = handler.validate_and_extract_preset("reason", owner="alice")
    assert character == ""
    assert "systematic reasoning assistant" in prompt
    assert "Nyx" not in prompt


def test_reminder_default_voice_is_default_persona(manager):
    from src.reminder_personas import synthesis_system_prompt

    set_default_persona("alice", name="Nyx", system_prompt="Speak in riddles.")
    prompt = synthesis_system_prompt("", owner="alice")
    assert prompt.startswith("You are Nyx. Speak in riddles.")
    assert "one-line reminder" in prompt


def test_reminder_novelty_persona_keeps_its_own_voice(manager):
    from src.reminder_personas import PERSONAS, synthesis_system_prompt

    set_default_persona("alice", name="Nyx", system_prompt="Speak in riddles.")
    prompt = synthesis_system_prompt("spark", owner="alice")
    assert prompt.startswith(PERSONAS["spark"])
    assert "Nyx" not in prompt


def test_assistant_prompt_composes_voice_plus_mechanical_framing():
    from src.task_scheduler import ASSISTANT_TASK_FRAMING, _compose_assistant_prompt

    composed = _compose_assistant_prompt("Speak in riddles.")
    assert composed.startswith("Speak in riddles.")
    assert composed.endswith(ASSISTANT_TASK_FRAMING)


def test_assistant_prompt_passes_legacy_blob_through_unchanged():
    from src.task_scheduler import _compose_assistant_prompt

    legacy = "You are the assistant.\n\nCORE RULE: use tools.\n\nEMAIL HANDLING: etc."
    assert _compose_assistant_prompt(legacy) == legacy


def test_assistant_prompt_empty_voice_falls_back_to_framing_only():
    from src.task_scheduler import ASSISTANT_TASK_FRAMING, _compose_assistant_prompt

    assert _compose_assistant_prompt("") == ASSISTANT_TASK_FRAMING


def _default_persona_endpoint():
    router = setup_preset_routes(MagicMock())
    return next(route.endpoint for route in router.routes if route.path == "/api/presets/default-persona")


def _persona_request(**state):
    auth_manager = SimpleNamespace(is_configured=True)
    return SimpleNamespace(
        state=SimpleNamespace(**state),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_manager)),
        client=SimpleNamespace(host="203.0.113.10"),
    )


def test_default_persona_route_allows_explicit_single_user_mode(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(
        default_persona,
        "get_default_persona",
        lambda owner, preset_manager=None: {"owner": owner},
    )

    assert asyncio.run(_default_persona_endpoint()(_persona_request())) == {"owner": ""}


def test_default_persona_route_keeps_configured_auth_fail_closed(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")

    with pytest.raises(HTTPException) as raised:
        asyncio.run(_default_persona_endpoint()(_persona_request()))

    assert raised.value.status_code == 401

import asyncio

import pytest

from src.openclank.acp_bridge import ACPBridge, _TurnState


def _bridge():
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._owner = "alice"
    bridge._managed_provider_generation = "generation-a"
    bridge._session_context = {
        "engine-a": {
            "root_operation_id": "root-a",
            "trusted_caller": "chat",
        }
    }
    bridge._turns = {"engine-a": _TurnState()}

    async def binding(params):
        if params["sessionID"] != "engine-a":
            raise ValueError("stale managed engine session")
        return {
            "engineSessionID": "engine-a",
            "stableChatID": "chat-a",
            "owner": "alice",
        }

    bridge._handle_session_binding_read = binding
    return bridge


def test_search_context_returns_active_owner_generation_turn_and_caller():
    result = asyncio.run(_bridge().resolve_managed_search_context("engine-a", "root-a"))
    assert result == {
        "owner": "alice",
        "holderID": "generation-a",
        "sessionID": "engine-a",
        "stableChatID": "chat-a",
        "rootOperationID": "root-a",
        "caller": "chat",
    }


@pytest.mark.parametrize(
    ("session_id", "root", "message"),
    [
        ("engine-foreign", "root-a", "stale managed engine session"),
        ("engine-a", "root-foreign", "managed search root operation is not active"),
    ],
)
def test_search_context_rejects_foreign_session_or_root(session_id, root, message):
    with pytest.raises(ValueError, match=message):
        asyncio.run(_bridge().resolve_managed_search_context(session_id, root))


def test_search_context_rejects_missing_generation_and_untrusted_caller():
    bridge = _bridge()
    bridge._managed_provider_generation = None
    with pytest.raises(ValueError, match="generation"):
        asyncio.run(bridge.resolve_managed_search_context("engine-a", "root-a"))

    bridge = _bridge()
    bridge._session_context["engine-a"]["trusted_caller"] = "model-option"
    with pytest.raises(ValueError, match="caller"):
        asyncio.run(bridge.resolve_managed_search_context("engine-a", "root-a"))

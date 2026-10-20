import inspect

import pytest

from src import ai_interaction
from src.agent_tools import model_interaction_tools
from src.agent_tools import session_tools


def _source(fn) -> str:
    return inspect.getsource(fn)


def test_legacy_model_resolver_is_retired():
    body = _source(session_tools.create_session)

    assert not hasattr(ai_interaction, "_resolve_model_target")
    assert not hasattr(ai_interaction, "_resolve_model")
    assert "resolve_chat_model_spec" in body
    assert "MANAGED_ENGINE_PUBLIC_URL" in body
    assert "provider_model_route_id=route.model_route_id" in body
    assert "ModelEndpoint" not in body
    assert "api_key" not in body


def test_model_listing_and_managed_image_generation_are_owner_scoped():
    # list_models moved to agent_tools.model_interaction_tools (#3629).
    list_body = _source(model_interaction_tools.list_models)
    image_body = _source(ai_interaction.do_generate_image)

    assert "owner: Optional[str] = None" in list_body
    assert "list_chat_routes(owner)" in list_body
    assert "ModelEndpoint" not in list_body
    assert "owner=owner or \"\"" in image_body
    assert "model_route_id=model_route_id" in image_body
    assert "root_operation_id=root_operation_id" in image_body
    assert "grant_id=grant_id" in image_body
    assert "ModelEndpoint" not in image_body
    assert "_resolve_model" not in image_body
    assert "httpx" not in image_body


# chat_with_model, list_models and ask_teacher moved to the registry (#3629)
# and no longer route through dispatch_ai_tool; their owner threading is covered
# by tests/test_model_interaction_registry.py. The remaining model-ish tools
# still dispatched here:
@pytest.mark.parametrize("tool,content", [
    ("pipeline", "gpt-test | summarize this"),
    ("ui_control", "switch_model gpt-test"),
])
async def test_dispatch_passes_owner_to_model_tools(monkeypatch, tool, content):
    seen = {}

    async def capture(name, content, session_id=None, owner=None):
        seen[name] = {"content": content, "session_id": session_id, "owner": owner}
        return {"ok": True}

    monkeypatch.setattr(
        ai_interaction,
        "do_pipeline",
        lambda content, session_id=None, owner=None: capture("pipeline", content, session_id, owner),
    )
    monkeypatch.setattr(
        ai_interaction,
        "do_ui_control",
        lambda content, session_id=None, owner=None: capture("ui_control", content, session_id, owner),
    )

    _desc, result = await ai_interaction.dispatch_ai_tool(tool, content, session_id="sid1", owner="alice")

    assert result == {"ok": True}
    assert seen[tool]["owner"] == "alice"
    assert seen[tool]["session_id"] == "sid1"

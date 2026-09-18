"""Tests for the model-interaction tools after their move to the agent_tools
registry (#3629): chat_with_model, ask_teacher, list_models.

The implementations now live in src/agent_tools/model_interaction_tools.py
(moved out of src/ai_interaction.py). These assert (1) the handlers are
registered in TOOL_HANDLERS, (2) each handler runs the moved logic and threads
session_id/owner from the ctx, and (3) tool_execution.py dispatches them
through the registry rather than the legacy dispatch_ai_tool elif.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from src.agent_tools import TOOL_HANDLERS
from src.agent_tools import model_interaction_tools as mit

_MODEL_TOOLS = ("chat_with_model", "ask_teacher", "list_models")


def test_model_interaction_tools_registered():
    for name in _MODEL_TOOLS:
        assert name in TOOL_HANDLERS, f"{name} missing from TOOL_HANDLERS"


def test_chat_with_model_threads_owner_and_returns(monkeypatch):
    seen = {}

    def fake_resolve(*, model_spec, owner=None):
        seen["spec"] = model_spec
        seen["owner"] = owner
        return SimpleNamespace(
            model_route_id="route-x",
            provider_grant_id="grant-x",
            provider_model_id="model-x",
        )

    async def fake_call(**kwargs):
        messages = kwargs["messages"]
        seen["message"] = messages[-1]["content"]
        seen["route"] = kwargs["model_route_id"]
        seen["grant"] = kwargs["grant_id"]
        return "hi back"

    monkeypatch.setattr("src.openclank.chat_routing.resolve_chat_model_spec", fake_resolve)
    monkeypatch.setattr("src.openclank.modality_facade.complete_text", fake_call)

    res = asyncio.run(mit.ChatWithModelTool().execute(
        "model-x\nhello there", {"owner": "alice", "session_id": "s1"}))

    assert res == {"model": "model-x", "response": "hi back"}
    assert seen["owner"] == "alice"
    assert seen["spec"] == "model-x"
    assert seen["message"] == "hello there"
    assert seen["route"] == "route-x"
    assert seen["grant"] == "grant-x"


def test_ask_teacher_threads_owner_and_marks_teacher(monkeypatch):
    seen = {}

    def fake_resolve(*, model_spec, owner=None):
        seen["owner"] = owner
        return SimpleNamespace(
            model_route_id="teacher-route",
            provider_grant_id=None,
            provider_model_id="teacher-x",
        )

    async def fake_call(**kwargs):
        return "do this and that"

    monkeypatch.setattr("src.openclank.chat_routing.resolve_chat_model_spec", fake_resolve)
    monkeypatch.setattr("src.openclank.modality_facade.complete_text", fake_call)

    res = asyncio.run(mit.AskTeacherTool().execute(
        "teacher-x\nI am stuck", {"owner": "bob"}))

    assert res["teacher"] is True
    assert res["response"] == "do this and that"
    assert seen["owner"] == "bob"


def test_list_models_no_endpoints(monkeypatch):
    monkeypatch.setattr("src.openclank.chat_routing.list_chat_routes", lambda owner: ([], []))

    res = asyncio.run(mit.ListModelsTool().execute("", {}))
    assert res == {"results": "No models found."}


def test_dispatched_via_registry_not_dispatch_ai_tool():
    """The model tools route through the registry (_document_tool_dispatch), and
    are no longer in the dispatch_ai_tool elif tuple."""
    source = (Path(__file__).resolve().parent.parent / "src" / "tool_execution.py").read_text(encoding="utf-8")
    assert 'elif tool in ("chat_with_model", "ask_teacher", "list_models"):' in source

    marker = "from src.ai_interaction import dispatch_ai_tool"
    idx = source.index(marker)
    branch_head = source.rfind("elif tool in (", 0, idx)
    legacy_tuple = source[branch_head:idx]
    for name in _MODEL_TOOLS:
        assert f'"{name}"' not in legacy_tuple, f"{name} still routed via dispatch_ai_tool"

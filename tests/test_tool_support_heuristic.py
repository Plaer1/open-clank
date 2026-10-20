"""Managed-route capability regressions for the compatibility agent loop."""

import asyncio
import json
from types import SimpleNamespace

import src.agent_loop as agent_loop
from src.agent_loop import _managed_agent_target, _normalized_context_length
from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL


def _route(**capabilities):
    return SimpleNamespace(
        runtime_model="connection-1/model-1",
        connection_id="connection-1",
        capabilities=capabilities,
    )


def test_managed_agent_target_retains_normalized_connection_identity():
    target = _managed_agent_target(_route(tools=False, vision=True))

    assert target.transport == "acp"
    assert target.endpoint_url == MANAGED_ENGINE_PUBLIC_URL
    assert target.model_id == "connection-1/model-1"
    assert target.endpoint_id == "connection-1"
    assert target.provider_id == "connection-1"
    assert target.headers == {}
    assert target.capabilities["tools"] is False
    assert target.capabilities["vision"] is True


def test_managed_agent_target_defaults_unspecified_tools_on():
    assert _managed_agent_target(_route()).capabilities["tools"] is True


def test_normalized_context_length_prefers_catalog_metadata():
    assert _normalized_context_length(_route(context_window="65536"), 8192) == 65536


def test_normalized_context_length_uses_supplied_value_when_catalog_is_silent():
    assert _normalized_context_length(_route(), 8192) == 8192


def test_normalized_context_length_rejects_malformed_or_negative_values():
    assert _normalized_context_length(_route(context_length="unknown"), -1) == 0


def test_agent_loop_has_no_legacy_provider_dispatch_source():
    from pathlib import Path

    source = Path("src/agent_loop.py").read_text()
    forbidden = (
        "stream_llm_with_fallback",
        "from src.llm_core",
        "ModelEndpoint",
        "httpx.post",
        "requests.post",
    )
    assert not any(token in source for token in forbidden)
    assert "resolve_chat_route(" in source
    assert "stream_agent_target(" in source


def test_low_signal_stream_propagates_only_normalized_authority(monkeypatch):
    captured = {}

    def resolve(**kwargs):
        captured["resolve"] = kwargs
        return SimpleNamespace(
            provider_model_id="model-1",
            model_route_id="route-1",
            provider_grant_id="grant-1",
            connection_id="connection-1",
            runtime_model="connection-1/model-1",
            capabilities={"tools": True},
        )

    async def stream(target, messages, **kwargs):
        captured["target"] = target
        captured["messages"] = messages
        captured["stream"] = kwargs
        yield f'data: {json.dumps({"delta": "Hey."})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "resolve_chat_route", resolve)
    monkeypatch.setattr(agent_loop, "stream_agent_target", stream)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "get_setting", lambda _key, default=None: default)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda _owner: set())
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *_args, **_kwargs: 1)

    async def collect():
        return [
            event
            async for event in agent_loop.stream_agent_loop(
                "https://legacy.invalid/v1",
                "legacy-model",
                [{"role": "user", "content": "yo"}],
                headers={"Authorization": "must-not-cross"},
                fallbacks=[("https://fallback.invalid", "other", {"X-Key": "secret"})],
                owner="Alice",
                session_id="session-1",
                provider_model_route_id="route-1",
                provider_grant_id="grant-1",
                root_operation_id="root-1",
            )
        ]

    events = asyncio.run(collect())

    assert any('"delta": "Hey."' in event for event in events)
    assert captured["resolve"] == {
        "owner": "alice",
        "endpoint_id": "share:grant-1",
        "model_id": None,
        "model_route_id": "route-1",
    }
    assert captured["target"].headers == {}
    assert captured["target"].endpoint_url == MANAGED_ENGINE_PUBLIC_URL
    assert captured["stream"]["owner"] == "alice"
    assert captured["stream"]["turn_envelope"] == {
        "allowed_tools": [],
        "provider_model_route_id": "route-1",
        "provider_grant_id": "grant-1",
        "root_operation_id": "root-1",
    }

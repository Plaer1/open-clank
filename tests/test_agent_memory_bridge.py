"""Focused contracts for strict Agent's scoped Frankenmemory bridge."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _tool_result_text(result) -> dict:
    return json.loads(result[0].text)


class _StubMemoryProvider:
    provider_id = "frankenmemory"

    def __init__(self):
        self.records = [
            SimpleNamespace(
                id="q_1",
                text="What is the launch code?",
                category="unknown",
                kind="unknown",
                metadata={},
                pinned=False,
            )
        ]
        self.remembered = []
        self.resolved = []

    async def list_memories(self, *, owner=None, limit=100):
        return self.records[:limit]

    async def remember(
        self,
        text,
        *,
        owner=None,
        session_id=None,
        category="fact",
        source="user",
        capture_mode="manual",
    ):
        record = SimpleNamespace(
            id="m_answer",
            text=text,
            category=category,
            kind=category,
            metadata={},
            pinned=False,
        )
        self.records.append(record)
        self.remembered.append(
            {
                "text": text,
                "owner": owner,
                "session_id": session_id,
                "category": category,
                "source": source,
                "capture_mode": capture_mode,
            }
        )
        return record

    async def resolve_id(self, display_id, *, owner=None):
        return display_id

    async def resolve_question(self, memory_id, *, resolved_by=None, owner=None):
        self.resolved.append(
            {"memory_id": memory_id, "resolved_by": resolved_by, "owner": owner}
        )
        return True

    async def recall(self, query, *, owner=None, top_k=5):
        return []

    async def get(self, memory_id, *, owner=None):
        return None


@pytest.mark.asyncio
async def test_lifetools_structured_memory_list_add_resolve_and_recall(monkeypatch):
    import routes.prefs_routes as prefs_routes
    import src.ai_interaction as ai_interaction
    from src.openclank import lifetools_server

    provider = _StubMemoryProvider()
    monkeypatch.setattr(lifetools_server, "_OWNER", "alice")
    monkeypatch.setattr(lifetools_server, "_SESSION_ID", "chat-1")
    monkeypatch.setattr(lifetools_server, "_MEMORY_PROVIDER", provider)
    monkeypatch.setattr(ai_interaction, "_memory_provider", provider)
    monkeypatch.setattr(
        prefs_routes,
        "_load_for_user",
        lambda _owner: {"memory_mode": "automatic"},
    )

    listed = _tool_result_text(
        await lifetools_server.call_tool("manage_memory", {"action": "list"})
    )
    assert "What is the launch code?" in listed["results"]

    added = _tool_result_text(
        await lifetools_server.call_tool(
            "manage_memory",
            {
                "action": "add",
                "text": "The launch code is cobalt.",
                "category": "fact",
            },
        )
    )
    assert "error" not in added, added
    assert added["memory_id"] == "m_answer"
    assert provider.remembered == [
        {
            "text": "The launch code is cobalt.",
            "owner": "alice",
            "session_id": "chat-1",
            "category": "fact",
            "source": "ai_agent",
            "capture_mode": "manual",
        }
    ]

    resolved = _tool_result_text(
        await lifetools_server.call_tool(
            "manage_memory",
            {
                "action": "resolve",
                "memory_id": "q_1",
                "answer_memory_id": "m_answer",
            },
        )
    )
    assert resolved["action"] == "resolve"
    assert provider.resolved == [
        {"memory_id": "q_1", "resolved_by": "m_answer", "owner": "alice"}
    ]

    recalled = _tool_result_text(
        await lifetools_server.call_tool(
            "recall_memory",
            {"query": "launch code"},
        )
    )
    assert recalled == {"results": "No matching memories."}


def test_agent_descriptors_scope_lifetools_and_mark_raw_bridge(monkeypatch, tmp_path):
    from src.openclank import acp_bridge

    db_path = str(tmp_path / "frankenmemory.db")
    monkeypatch.setenv("FM_DB_PATH", db_path)
    monkeypatch.setenv("FM_DB_ID", "db-test")
    monkeypatch.setenv("FM_EMBED_API_BASE", "https://embed.example/v1")
    monkeypatch.setenv("FM_EMBED_API_KEY", "embed-secret")
    monkeypatch.setenv("FM_EMBED_MODEL", "embedding-model")
    monkeypatch.setenv("FM_EMBED_DIMENSIONS", "3072")
    monkeypatch.setenv("FM_EMBED_TIMEOUT_MS", "45000")
    monkeypatch.setenv("FM_EMBED_FUTURE_SETTING", "future-value")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-secret")
    monkeypatch.setattr(acp_bridge, "_FM_MCP_COMMAND", "/opt/fm-mcp")
    canonical_env = {
        "FM_DB_PATH": db_path,
        "FM_DB_ID": "db-test",
        "FM_EMBED_API_BASE": "https://embed.example/v1",
        "FM_EMBED_API_KEY": "embed-secret",
        "FM_EMBED_MODEL": "embedding-model",
        "FM_EMBED_DIMENSIONS": "3072",
        "FM_EMBED_TIMEOUT_MS": "45000",
        "FM_EMBED_FUTURE_SETTING": "future-value",
        "FM_MCP_COMMAND": "/opt/fm-mcp",
    }

    life = acp_bridge.lifetools_mcp_descriptor(
        owner="alice",
        session_id="chat-1",
        workspace="/workspace",
    )
    life_env = {item["name"]: item["value"] for item in life["env"]}
    assert life_env["FM_SCOPE_AUTHORITY"] == "trusted-caller"
    assert life_env["FM_OWNER"] == "alice"
    assert life_env["FM_WORKSPACE_ID"] == "global"
    assert life_env["FM_MEMORY_ENABLED"] == "1"
    assert life_env["FM_DB_PATH"] == db_path
    assert life_env["FM_DB_ID"] == "db-test"
    assert life_env["FM_MCP_COMMAND"] == "/opt/fm-mcp"
    assert {
        name: life_env[name]
        for name in canonical_env
    } == canonical_env
    assert "OPENAI_API_KEY" not in life_env

    raw = acp_bridge.frankenmemory_mcp_descriptor(
        owner="alice",
        session_id="chat-1",
    )
    raw_env = {item["name"]: item["value"] for item in raw["env"]}
    assert raw_env["FM_AGENT_BRIDGE"] == "1"
    assert {
        name: raw_env[name]
        for name in canonical_env
    } == canonical_env
    assert "OPENAI_API_KEY" not in raw_env

    local_life = acp_bridge.lifetools_mcp_descriptor(owner=" ")
    local_raw = acp_bridge.frankenmemory_mcp_descriptor(owner=" ")
    assert {item["name"]: item["value"] for item in local_life["env"]}[
        "FM_OWNER"
    ] == "local"
    assert {item["name"]: item["value"] for item in local_raw["env"]}[
        "FM_OWNER"
    ] == "local"


@pytest.mark.asyncio
async def test_agent_session_uses_lifetools_as_memory_scope_carrier(tmp_path):
    from src.openclank.acp_bridge import ACPBridge

    class _Client:
        def __init__(self):
            self.new_servers = None
            self.resumed_servers = None

        def on_session_update(self, _callback):
            return None

        def register_callback(self, _name, _callback):
            return None

        async def new_session(self, _cwd, *, mcp_servers):
            self.new_servers = mcp_servers
            return {"sessionId": "mimo-1", "models": {}}

        async def resume_session(self, _session_id, _cwd, *, mcp_servers):
            self.resumed_servers = mcp_servers
            return {"sessionId": "mimo-1", "models": {}}

    client = _Client()
    bridge = ACPBridge(
        client,
        cwd="/workspace",
        owner="alice",
        session_map_path=tmp_path / "session-map.json",
    )

    await bridge.ensure_session(
        "chat-1",
        owner="alice",
        with_memory=False,
    )
    assert any(item["name"].startswith("lifetools_") for item in client.new_servers)
    assert not any(item["name"].startswith("frankenmemory_") for item in client.new_servers)
    life_env = {
        entry["name"]: entry["value"]
        for item in client.new_servers
        if item["name"].startswith("lifetools_")
        for entry in item["env"]
    }
    assert life_env["FM_MEMORY_ENABLED"] == "0"

    await bridge.ensure_session(
        "chat-1",
        owner="alice",
        with_memory=True,
    )
    assert not any(item["name"].startswith("frankenmemory_") for item in client.resumed_servers)
    life_env = {
        entry["name"]: entry["value"]
        for item in client.resumed_servers
        if item["name"].startswith("lifetools_")
        for entry in item["env"]
    }
    assert life_env["FM_MEMORY_ENABLED"] == "1"

    await bridge.ensure_session(
        "chat-1",
        owner="alice",
        with_memory=False,
    )
    assert not any(item["name"].startswith("frankenmemory_") for item in client.resumed_servers)
    life_env = {
        entry["name"]: entry["value"]
        for item in client.resumed_servers
        if item["name"].startswith("lifetools_")
        for entry in item["env"]
    }
    assert life_env["FM_MEMORY_ENABLED"] == "0"


def test_mimo_policy_always_denies_raw_frankenmemory_tools():
    from src.openclank.acp_bridge import _mimo_tool_policy

    assert _mimo_tool_policy({})["frankenmemory_*"] is False

    policy = _mimo_tool_policy(
        {
            "allowed_tools": [
                "manage_memory",
                "frankenmemory_owner_lifecycle",
            ],
            "forced_tools": ["frankenmemory_code_index"],
        }
    )
    assert policy["frankenmemory_*"] is False
    assert "frankenmemory_owner_lifecycle" not in policy
    assert "frankenmemory_code_index" not in policy
    assert policy["memory"] is True
    assert policy["lifetools_*_manage_memory"] is True


@pytest.mark.parametrize(
    ("prefs", "incognito", "no_memory", "expected"),
    [
        ({}, False, False, True),
        ({"memory_enabled": False}, False, False, False),
        ({"memory_mode": "off"}, False, False, False),
        ({}, False, True, False),
        ({}, True, False, False),
    ],
)
def test_agent_memory_read_authority(prefs, incognito, no_memory, expected):
    from routes.chat_routes import _agent_memory_read_allowed

    assert (
        _agent_memory_read_allowed(
            prefs,
            incognito=incognito,
            no_memory=no_memory,
        )
        is expected
    )


def test_turn_envelope_carries_resolved_memory_authority():
    from routes.chat_routes import _turn_envelope

    envelope = _turn_envelope(
        session_id="chat-1",
        owner="alice",
        workspace="/workspace",
        model="provider/model",
        mode="agent",
        incognito=True,
        no_memory=True,
        memory_read_allowed=False,
    )
    assert envelope["no_memory"] is True
    assert envelope["memory_read_allowed"] is False


@pytest.mark.asyncio
async def test_bridge_skips_digest_when_memory_read_is_disabled():
    from src.openclank.acp_bridge import ACPBridge

    class _DigestProvider:
        def __init__(self):
            self.calls = 0

        async def digest(self, *, owner=None):
            self.calls += 1
            return {"counts": {"by_tier": {"curated": 1}}}

    provider = _DigestProvider()
    bridge = ACPBridge(
        MagicMock(),
        cwd="/workspace",
        owner="alice",
        memory_provider=provider,
    )
    messages = [{"role": "user", "content": "hello"}]
    out, trusted = await bridge._maybe_inject_digest(
        messages,
        owner="alice",
        incognito=False,
        memory_read_allowed=False,
    )
    assert out is messages
    assert trusted == ""
    assert provider.calls == 0


def test_manage_memory_schema_and_adapter_expose_open_question_resolution():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block

    schema = next(
        item["function"]
        for item in FUNCTION_TOOL_SCHEMAS
        if item["function"]["name"] == "manage_memory"
    )
    properties = schema["parameters"]["properties"]
    assert "resolve" in properties["action"]["enum"]
    assert {"unknown", "question"} <= set(properties["category"]["enum"])
    assert "answer_memory_id" in properties

    block = function_call_to_tool_block(
        "manage_memory",
        json.dumps(
            {
                "action": "resolve",
                "memory_id": "q_1",
                "answer_memory_id": "m_answer",
            }
        ),
    )
    assert block is not None
    assert block.content == "resolve\nq_1\nm_answer"


def test_manage_memory_is_owner_privileged_not_admin_only():
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS

    assert "manage_memory" not in NON_ADMIN_BLOCKED_TOOLS


_FM_DEBUG_BIN = (
    Path(__file__).resolve().parents[1]
    / "mcp_servers"
    / "frankenmemory"
    / "target"
    / "debug"
    / "fm-mcp"
)


@pytest.mark.skipif(not _FM_DEBUG_BIN.exists(), reason="fm-mcp debug binary not built")
@pytest.mark.asyncio
async def test_fm_agent_bridge_rejects_internal_tools_server_side(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.shared.exceptions import McpError

    env = dict(os.environ)
    env.update(
        {
            "FM_DB_PATH": str(tmp_path / "frankenmemory.db"),
            "FM_OWNER": "alice",
            "FM_WORKSPACE_ID": "global",
            "FM_AGENT_BRIDGE": "1",
        }
    )
    env.pop("FM_DB_ID", None)
    params = StdioServerParameters(
        command=str(_FM_DEBUG_BIN),
        args=[],
        env=env,
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            with pytest.raises(McpError, match="not available through an Agent memory bridge"):
                await session.call_tool("owner_lifecycle", {"action": "stats"})
            with pytest.raises(McpError, match="not available through an Agent memory bridge"):
                await session.call_tool(
                    "memory_quality",
                    {"rebuild_graph_fts": True},
                )
            healthy = await session.call_tool(
                "memory_quality",
                {"rebuild_graph_fts": False},
            )
            assert healthy.isError is not True

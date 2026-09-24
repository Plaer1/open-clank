"""Focused contracts for strict Agent's scoped Frankenmemory bridge."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _tool_result_text(result) -> dict:
    return json.loads(result[0].text)


def test_lifetools_direct_script_bootstraps_repository_imports(tmp_path):
    """The per-session MCP launches this file directly, outside repo cwd."""
    script = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "openclank"
        / "lifetools_server.py"
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    process = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    stderr = process.stderr.decode("utf-8", errors="replace")
    assert "No module named 'src'" not in stderr
    assert process.returncode == 0, stderr


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

    async def resolve_question(
        self,
        memory_id,
        *,
        resolved_by=None,
        answer=None,
        expected_revision=None,
        owner=None,
    ):
        self.resolved.append(
            {
                "memory_id": memory_id,
                "resolved_by": resolved_by,
                "answer": answer,
                "expected_revision": expected_revision,
                "owner": owner,
            }
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
        {
            "memory_id": "q_1",
            "resolved_by": "m_answer",
            "answer": None,
            "expected_revision": None,
            "owner": "alice",
        }
    ]

    recalled = _tool_result_text(
        await lifetools_server.call_tool(
            "recall_memory",
            {"query": "launch code"},
        )
    )
    assert recalled == {"results": "No matching memories."}


@pytest.mark.asyncio
async def test_lifetools_search_renders_reviewed_handler_identity(monkeypatch):
    from services.memory import principal_context
    from src.openclank import lifetools_server

    class Provider:
        async def invoke_tool(self, name, arguments):
            assert name == "search"
            assert arguments["owner"] == "allie"
            return {
                "results": [
                    {
                        "record": {
                            "id": "name-memory",
                            "content": "%USER%'s name is Allie.",
                        },
                        "score": 1.0,
                    }
                ]
            }

    lifecycle = SimpleNamespace()
    monkeypatch.setattr(lifetools_server, "_OWNER", "allie")
    monkeypatch.setattr(lifetools_server, "_MEMORY_PROVIDER", Provider())
    monkeypatch.setattr(lifetools_server, "_MEMORY_LIFECYCLE", lifecycle)
    monkeypatch.setattr(lifetools_server, "_MEMORY_RECONCILED_LIFECYCLE", lifecycle)
    monkeypatch.setattr(
        principal_context,
        "resolve_handler_display_label",
        lambda *_args, **_kwargs: "Allie",
    )
    monkeypatch.setenv("FM_WORKSPACE_ID", "global")

    result = _tool_result_text(
        await lifetools_server.call_tool("search", {"query": "user name"})
    )
    record = result["results"][0]["record"]
    assert record["content"] == "Allie's name is Allie."
    assert record["raw_content"] == "%USER%'s name is Allie."


@pytest.mark.asyncio
async def test_agent_delete_uses_the_shared_memory_lifecycle(monkeypatch):
    import routes.prefs_routes as prefs_routes
    import src.ai_interaction as ai_interaction

    provider = _StubMemoryProvider()
    deleted = []

    class Lifecycle:
        async def delete(self, memory_id, *, owner, workspace_id=None):
            deleted.append((memory_id, owner, workspace_id))
            return {"tombstone_id": "coordinated"}

    monkeypatch.setattr(ai_interaction, "_memory_provider", provider)
    monkeypatch.setattr(ai_interaction, "_memory_lifecycle", Lifecycle())
    monkeypatch.setattr(
        prefs_routes,
        "_load_for_user",
        lambda _owner: {"memory_mode": "automatic"},
    )

    result = await ai_interaction.do_manage_memory(
        "delete\nq_1",
        session_id="chat-1",
        owner="alice",
    )
    assert result["action"] == "delete"
    assert deleted == [("q_1", "alice", None)]


@pytest.mark.asyncio
async def test_lifetools_raw_delete_is_intercepted_by_shared_lifecycle(monkeypatch):
    from src.openclank import lifetools_server

    provider_calls = []
    lifecycle_calls = []

    class Provider:
        async def invoke_tool(self, name, arguments):
            provider_calls.append((name, arguments))
            raise AssertionError("raw delete must not reach the provider")

    class Lifecycle:
        async def delete(self, memory_id, *, owner, workspace_id=None):
            lifecycle_calls.append((memory_id, owner, workspace_id))
            return {"tombstone_id": "coordinated"}

    monkeypatch.setattr(lifetools_server, "_OWNER", "alice")
    monkeypatch.setattr(lifetools_server, "_MEMORY_PROVIDER", Provider())
    monkeypatch.setattr(lifetools_server, "_MEMORY_LIFECYCLE", Lifecycle())
    monkeypatch.setenv("FM_WORKSPACE_ID", "global")

    result = _tool_result_text(await lifetools_server.call_tool(
        "delete_memory",
        {"id": "memory-1"},
    ))
    assert result["deleted"] is True
    assert lifecycle_calls == [("memory-1", "alice", "global")]
    assert provider_calls == []


@pytest.mark.asyncio
async def test_lifetools_typed_mutations_use_provider_lifecycle_not_raw_mcp(monkeypatch):
    from src.openclank import lifetools_server

    calls = []

    class Provider:
        async def invoke_tool(self, name, arguments):
            raise AssertionError(f"raw {name} bypassed the provider lifecycle: {arguments}")

        async def update_candidate(self, candidate_id, **kwargs):
            calls.append(("update", candidate_id, kwargs))
            return {"id": candidate_id, "content": kwargs["text"]}

        async def review_candidate(self, candidate_id, **kwargs):
            calls.append(("review", candidate_id, kwargs))
            return {"reviewed": True, "accepted": kwargs["accept"]}

        async def resolve_question(self, memory_id, **kwargs):
            calls.append(("resolve", memory_id, kwargs))
            return True

        async def reopen_question(self, memory_id, **kwargs):
            calls.append(("reopen", memory_id, kwargs))
            return {"id": memory_id, "current_revision": 4}

    lifecycle = SimpleNamespace()
    monkeypatch.setattr(lifetools_server, "_OWNER", "alice")
    monkeypatch.setattr(lifetools_server, "_MEMORY_PROVIDER", Provider())
    monkeypatch.setattr(lifetools_server, "_MEMORY_LIFECYCLE", lifecycle)
    monkeypatch.setattr(lifetools_server, "_MEMORY_RECONCILED_LIFECYCLE", lifecycle)
    monkeypatch.setenv("FM_WORKSPACE_ID", "global")

    assert _tool_result_text(await lifetools_server.call_tool(
        "update_candidate",
        {"id": "candidate-1", "content": "edited", "category": "fact"},
    ))["updated"] is True
    assert _tool_result_text(await lifetools_server.call_tool(
        "review_candidate",
        {"id": "candidate-1", "accept": True, "reason": "approved"},
    ))["accepted"] is True
    assert _tool_result_text(await lifetools_server.call_tool(
        "resolve_memory",
        {"id": "question-1", "answer": "E", "expected_revision": 2},
    ))["resolved"] is True
    assert _tool_result_text(await lifetools_server.call_tool(
        "reopen_memory",
        {"id": "question-1", "expected_revision": 3},
    ))["memory"]["current_revision"] == 4

    assert calls == [
        (
            "update",
            "candidate-1",
            {
                "text": "edited",
                "category": "fact",
                "reason": "edited_by_agent",
                "owner": "alice",
                "workspace_id": "global",
            },
        ),
        (
            "review",
            "candidate-1",
            {
                "accept": True,
                "reason": "approved",
                "owner": "alice",
                "workspace_id": "global",
            },
        ),
        (
            "resolve",
            "question-1",
            {
                "answer": "E",
                "resolved_by": None,
                "expected_revision": 2,
                "owner": "alice",
            },
        ),
        (
            "reopen",
            "question-1",
            {"expected_revision": 3, "owner": "alice"},
        ),
    ]


@pytest.mark.asyncio
async def test_lifetools_local_memory_scope_keeps_blank_artifact_owner(
    monkeypatch,
    tmp_path,
):
    from services.memory.forget_coordinator import (
        unpack_forget_token,
    )
    from services.memory.skills import SkillsManager
    from src.openclank import lifetools_server

    class Provider:
        def __init__(self):
            self.calls = []
            self.operations = {}

        async def forget(self, action, **kwargs):
            self.calls.append((action, kwargs))
            operation_id = kwargs.get("operation_id")
            closure = {
                "raw_ids": [],
                "candidate_ids": [],
                "curated_ids": ["memory-1"],
                "graph_node_ids": [],
            }
            if action == "preview":
                return {"token": "preview-1", "closure": closure}
            if action == "status":
                return self.operations.get(operation_id, {"state": "absent"})
            if action == "commit":
                result = {
                    "state": "committed",
                    "tombstone_id": "provider-tombstone",
                    "recover_until": "2099-01-01T00:00:00+00:00",
                    "closure": closure,
                }
                self.operations[operation_id] = result
                return result
            raise AssertionError(action)

    provider = Provider()
    skills = SkillsManager(str(tmp_path))
    created = skills.add_skill(
        name="ownerless-derived-skill",
        description="local-install artifact",
        source="memory-promotion",
        source_memory_ids=["memory-1"],
        owner="",
    )
    monkeypatch.setattr(lifetools_server, "_OWNER", "local")
    monkeypatch.setattr(lifetools_server, "_SKILL_OWNER", "")
    monkeypatch.setattr(lifetools_server, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(lifetools_server, "_MEMORY_PROVIDER", provider)
    monkeypatch.setattr(lifetools_server, "_MEMORY_LIFECYCLE", None)
    monkeypatch.setattr(lifetools_server, "_MEMORY_RECONCILED_LIFECYCLE", None)
    monkeypatch.setattr(lifetools_server, "_MEMORY_RECONCILE_LOCK", None)
    monkeypatch.setenv("FM_WORKSPACE_ID", "global")

    result = _tool_result_text(
        await lifetools_server.call_tool(
            "delete_memory",
            {"id": "memory-1"},
        )
    )
    assert result["deleted"] is True
    assert lifetools_server._MEMORY_LIFECYCLE.skill_owner == ""
    assert skills.load(owner="") == []
    assert all(
        call[1]["owner"] == "local"
        and call[1]["workspace_id"] == "global"
        for call in provider.calls
    )
    operation_id = unpack_forget_token(
        result["result"]["tombstone_id"],
        "tombstone",
    )["operation"]
    info = lifetools_server._MEMORY_LIFECYCLE.skill_forget.operation_info(
        operation_id,
        owner="",
        workspace_id="global",
    )
    assert info["state"] == "committed"
    assert info["skill_ids"] == [created["skill_id"]]
    assert [
        call[0]
        for call in provider.calls
    ] == [
        "preview",
        "status",
        "preview",
        "commit",
    ]


@pytest.mark.asyncio
async def test_lifetools_reconciles_memory_lifecycle_once(monkeypatch):
    import src.ai_interaction as ai_interaction
    from src.openclank import lifetools_server

    class Lifecycle:
        def __init__(self):
            self.reconciliations = 0

        async def reconcile(self):
            self.reconciliations += 1
            return {
                "rolled_back": 0,
                "committed": 0,
                "restored": 0,
                "errors": 0,
            }

    provider = object()
    lifecycle = Lifecycle()
    monkeypatch.setattr(lifetools_server, "_OWNER", "alice")
    monkeypatch.setattr(lifetools_server, "_MEMORY_PROVIDER", provider)
    monkeypatch.setattr(lifetools_server, "_MEMORY_LIFECYCLE", lifecycle)
    monkeypatch.setattr(lifetools_server, "_MEMORY_RECONCILED_LIFECYCLE", None)
    monkeypatch.setattr(lifetools_server, "_MEMORY_RECONCILE_LOCK", None)
    monkeypatch.setattr(ai_interaction, "set_memory_manager", lambda *a, **k: None)

    assert await lifetools_server._ensure_memory_provider() is provider
    assert await lifetools_server._ensure_memory_provider() is provider
    assert lifecycle.reconciliations == 1


@pytest.mark.asyncio
async def test_lifetools_retention_preserves_preview_and_operation_binding(
    monkeypatch,
):
    from src.openclank import lifetools_server

    calls = []

    class Provider:
        async def invoke_tool(self, name, arguments):
            raise AssertionError(f"{name} must use the shared lifecycle")

    class Lifecycle:
        async def reconcile(self):
            return {}

        async def retention(
            self,
            action,
            *,
            owner,
            workspace_id=None,
            preview_token=None,
            operation_id=None,
        ):
            calls.append(
                {
                    "action": action,
                    "owner": owner,
                    "workspace_id": workspace_id,
                    "preview_token": preview_token,
                    "operation_id": operation_id,
                }
            )
            if action == "preview_expire":
                return {
                    "token": "composite-retention-token",
                    "operation_id": "retention-operation-1",
                    "closure": {},
                }
            if action == "expire":
                return {"curated_ids": ["memory-1"]}
            if action == "status":
                return {
                    "state": "committed",
                    "operation_id": operation_id,
                }
            raise AssertionError(action)

        async def expire_retention(self, **kwargs):
            raise AssertionError("tokened expiry must not use the one-shot wrapper")

    monkeypatch.setattr(lifetools_server, "_OWNER", "alice")
    monkeypatch.setattr(lifetools_server, "_SKILL_OWNER", "alice")
    monkeypatch.setattr(lifetools_server, "_MEMORY_PROVIDER", Provider())
    monkeypatch.setattr(lifetools_server, "_MEMORY_LIFECYCLE", Lifecycle())
    monkeypatch.setattr(
        lifetools_server,
        "_MEMORY_RECONCILED_LIFECYCLE",
        lifetools_server._MEMORY_LIFECYCLE,
    )
    monkeypatch.setenv("FM_WORKSPACE_ID", "global")

    preview = _tool_result_text(
        await lifetools_server.call_tool(
            "memory_retention",
            {"action": "preview_expire"},
        )
    )
    assert preview["token"] == "composite-retention-token"
    operation_id = preview["operation_id"]
    expired = _tool_result_text(
        await lifetools_server.call_tool(
            "memory_retention",
            {
                "action": "expire",
                "preview_token": preview["token"],
                "operation_id": operation_id,
            },
        )
    )
    assert expired["curated_ids"] == ["memory-1"]
    status = _tool_result_text(
        await lifetools_server.call_tool(
            "memory_retention",
            {
                "action": "status",
                "operation_id": operation_id,
            },
        )
    )
    assert status["state"] == "committed"
    assert calls == [
        {
            "action": "preview_expire",
            "owner": "alice",
            "workspace_id": "global",
            "preview_token": None,
            "operation_id": None,
        },
        {
            "action": "expire",
            "owner": "alice",
            "workspace_id": "global",
            "preview_token": "composite-retention-token",
            "operation_id": "retention-operation-1",
        },
        {
            "action": "status",
            "owner": "alice",
            "workspace_id": "global",
            "preview_token": None,
            "operation_id": "retention-operation-1",
        },
    ]


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
    assert life_env["OPEN_CLANK_SKILL_OWNER"] == "alice"
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
    assert not any(name.startswith("FM_EMBED_") for name in life_env)

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
    assert not any(name.startswith("FM_EMBED_") for name in raw_env)

    local_life = acp_bridge.lifetools_mcp_descriptor(owner=" ")
    local_raw = acp_bridge.frankenmemory_mcp_descriptor(owner=" ")
    assert {item["name"]: item["value"] for item in local_life["env"]}[
        "FM_OWNER"
    ] == "local"
    assert {item["name"]: item["value"] for item in local_life["env"]}[
        "OPEN_CLANK_SKILL_OWNER"
    ] == ""
    assert {item["name"]: item["value"] for item in local_raw["env"]}[
        "FM_OWNER"
    ] == "local"


@pytest.mark.asyncio
async def test_agent_session_uses_lifetools_as_memory_scope_carrier(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import core.database as database
    import src.openclank.transcript_projection as projection
    from src.openclank.acp_bridge import ACPBridge

    engine = create_engine(f"sqlite:///{tmp_path / 'managed-binding.db'}")
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(projection, "SessionLocal", sessions)
    monkeypatch.setattr("src.openclank.acp_bridge.chat_workspace", lambda: "memory:chat-1")
    db = sessions()
    db.add(database.Session(
        id="chat-1",
        name="chat",
        endpoint_url="mimo://acp",
        model="mimo",
        owner="alice",
        mimo_state={},
    ))
    db.commit()
    db.close()

    class _Client:
        def __init__(self):
            self.new_servers = None
            self.resumed_servers = None
            self.discarded = []

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

        async def discard_session(self, session_id, cwd):
            self.discarded.append((session_id, cwd))

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
        authority_workspace_id="workspace:chat-1",
    )
    assert any(item["name"].startswith("lifetools_") for item in client.resumed_servers)
    assert not any(item["name"].startswith("frankenmemory_") for item in client.resumed_servers)
    life_env = {
        entry["name"]: entry["value"]
        for item in client.resumed_servers
        if item["name"].startswith("lifetools_")
        for entry in item["env"]
    }
    assert life_env["FM_MEMORY_ENABLED"] == "0"

    await bridge.ensure_session(
        "chat-1",
        owner="alice",
        with_memory=True,
        authority_workspace_id="workspace:chat-1",
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
        authority_workspace_id="workspace:chat-1",
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
    assert block.content == "resolve\nq_1\nmemory_id:m_answer"


def test_qwen_memory_guard_allows_answer_lifecycle_without_user_prompting():
    from src.agent_loop import (
        _manage_memory_action_from_block_content,
        _qwen_memory_action_allowed,
    )
    from src.tool_schemas import function_call_to_tool_block

    for action, arguments in (
        ("add", {"action": "add", "text": "The answer is cobalt."}),
        (
            "resolve",
            {
                "action": "resolve",
                "memory_id": "q_1",
                "answer_memory_id": "m_answer",
            },
        ),
    ):
        block = function_call_to_tool_block("manage_memory", json.dumps(arguments))
        assert block is not None
        parsed_action = _manage_memory_action_from_block_content(block.content)
        assert parsed_action == action
        assert _qwen_memory_action_allowed(
            parsed_action, "I found the answer in the file"
        )
    assert not _qwen_memory_action_allowed("delete", "I found the answer in the file")
    assert not _qwen_memory_action_allowed("search", "What should we do next?")
    assert _qwen_memory_action_allowed("search", "Show me my brain memories")


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

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
    provider_id = "memery"

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
async def test_memory_is_only_available_over_the_managed_channel():
    from src.openclank import lifetools_server

    names = {tool.name for tool in await lifetools_server.list_tools()}
    assert "record_skill_usage" in names
    for name in ("recall_memory", "manage_memory", "search", "delete_memory", "memory_retention"):
        assert name not in names
        result = await lifetools_server.call_tool(name, {})
        assert "not available" in _tool_result_text(result)["error"]


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


def test_agent_descriptor_scopes_lifetools_without_a_worker_command(monkeypatch, tmp_path):
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
    monkeypatch.setenv("OPEN_CLANK_MEMERY_WORKER", "/opt/memery-worker")
    canonical_env = {
        "FM_DB_PATH": db_path,
        "FM_DB_ID": "db-test",
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
    assert "OPEN_CLANK_MEMERY_WORKER" not in life_env
    assert {
        name: life_env[name]
        for name in canonical_env
    } == canonical_env
    assert "OPENAI_API_KEY" not in life_env
    assert not any(name.startswith("FM_EMBED_") for name in life_env)

    local_life = acp_bridge.lifetools_mcp_descriptor(owner=" ")
    assert {item["name"]: item["value"] for item in local_life["env"]}[
        "FM_OWNER"
    ] == "local"
    assert {item["name"]: item["value"] for item in local_life["env"]}[
        "OPEN_CLANK_SKILL_OWNER"
    ] == ""


@pytest.mark.asyncio
async def test_agent_session_uses_lifetools_as_memory_scope_carrier(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import core.database as database
    import src.openclank.transcript_projection as projection
    from src.openclank.acp_bridge import ACPBridge

    monkeypatch.setattr("src.openclank.conversation_archive.get_conversation_archive", MagicMock)
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

        async def reserve_session(self):
            return {"sessionID": "mimo-1", "provisional": True}

        async def new_session(self, _cwd, *, mcp_servers, provisional_session_id=None):
            self.new_servers = mcp_servers
            return {"sessionId": provisional_session_id or "mimo-1", "models": {}}

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
async def test_bridge_skips_digest_when_memory_read_is_disabled(monkeypatch):
    monkeypatch.setattr("src.openclank.conversation_archive.get_conversation_archive", MagicMock)
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


@pytest.mark.asyncio
async def test_native_manage_write_replays_one_durable_receipt(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.operation_models import OperationBase
    from src.openclank.managed_memery import _perform
    from src.openclank.operation_journal import OperationJournalStore
    from unittest.mock import AsyncMock

    engine = create_engine(f"sqlite:///{tmp_path / 'operations.db'}")
    OperationBase.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    execute = AsyncMock(return_value={"action": "add", "memory_id": "mem1", "results": "Private text."})
    common = dict(identity={"actorID": "main", "rootOperationID": "root"}, account="acct", owner="alice",
        chat="chat", model_route=SimpleNamespace(connection_id="connection", billing_lane="local", model_route_id="route"),
        arguments={"action":"add", "text":"Private text."}, current=AsyncMock(return_value=True), execute=execute)
    initial = await _perform(OperationJournalStore(sessions), **common)
    replay = await _perform(OperationJournalStore(sessions), **common)
    assert initial["state"] == replay["state"] == "complete"
    assert replay["operationID"] == initial["operationID"]
    assert json.loads(replay["text"]) == {"action":"add", "memory_id":"mem1"}
    execute.assert_awaited_once()
    uncertain = AsyncMock(side_effect=RuntimeError("reply lost"))
    args = {**common, "arguments":{"action":"delete","memory_id":"mem1"}, "execute": uncertain}
    assert (await _perform(OperationJournalStore(sessions), **args))["state"] == "outcome_unknown"
    assert (await _perform(OperationJournalStore(sessions), **args))["state"] == "outcome_unknown"
    uncertain.assert_awaited_once()
    engine.dispose()


@pytest.mark.asyncio
async def test_native_manage_accepts_typed_arguments_for_all_six_verbs(monkeypatch):
    from unittest.mock import AsyncMock
    from src import ai_interaction
    from src.openclank.managed_protocol import validate_managed_method_request

    provider = _StubMemoryProvider()
    provider.update = AsyncMock(return_value=provider.records[0])
    provider.recall = AsyncMock(return_value=[])
    monkeypatch.setattr(ai_interaction, "_memory_provider", provider)
    monkeypatch.setattr(ai_interaction, "_memory_lifecycle", SimpleNamespace(delete=AsyncMock(return_value={"ok":True})))
    monkeypatch.setattr(ai_interaction, "_resolve_handler_label", AsyncMock(return_value="Allie"))
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda owner: {"memory_mode": "automatic"})
    turn = dict(sessionID="session", rootOperationID="root", actorID="main", executionRevision=1, innerTurnID="turn")
    for args in ({"action":"list"}, {"action":"search","query":"question"}, {"action":"add","text":"line one\nline two"},
            {"action":"edit","memory_id":"q_1","text":"line three\nline four"}, {"action":"resolve","memory_id":"q_1","answer":"cobalt"},
            {"action":"delete","memory_id":"q_1"}):
        request = {"identity":turn,"toolCallID":"call","messageID":"message","arguments":args}
        validate_managed_method_request("_openclank/session/v1/memory/manage",request)
        result = await ai_interaction.do_manage_memory(args["action"],"chat","alice",management_args=args)
        assert "error" not in result, result
    assert provider.remembered[0]["text"] == "line one\nline two"
    assert provider.update.await_args.kwargs["text"] == "line three\nline four"
    ai_interaction._memory_lifecycle.delete.assert_awaited_once_with("q_1", owner="alice")


@pytest.mark.asyncio
async def test_management_gateway_binds_exact_tool_and_refuses_guarded_mutation(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from src import ai_interaction
    from src.openclank.managed_memery import manage

    @asynccontextmanager
    async def barrier(owner):
        assert owner == "alice"
        yield

    identity = dict(sessionID="engine", actorID="main", rootOperationID="root", executionRevision=1, innerTurnID="turn")
    binding = dict(owner="alice", stableChatID="chat", canonicalCwd="/ws", memoryEnabled=True)
    entry = dict(owner="alice", ownerAccountID="acct", stableChatID="chat", authorityWorkspaceID="ws",
        rootSessionID="engine", policyRevision=1)
    provider = SimpleNamespace(_guarded_v17=False)
    session = SimpleNamespace(owner="alice", workspace_id="ws", incognito=False, endpoint_id="endpoint", model="m", provider_model_route_id="route")
    bridge = SimpleNamespace(_managed_turn_key=lambda identity: "key", _managed_actor_turns={"key":entry},
        _turns={"engine":SimpleNamespace(original_credential_current=lambda:True,root_operation_id="root",
            provider_root_closing=False,privacy_capture_veto=False)}, _memory_provider=provider,
        _session_context={"engine":{"memory_read_allowed":True}},
        _handle_session_binding_read=AsyncMock(return_value=binding),
        resolve_managed_control_turn=AsyncMock(return_value={"owner":"alice","ownerAccountID":"acct"}))
    async def source(chat, engine, workspace, proof, *, owner):
        assert (owner, chat, engine, workspace, proof["toolCallID"], proof["messageID"]) == ("alice","chat","engine","/ws","call","message")
        return {"state":"current","argsSHA256":proof["argsSHA256"]}
    worker = SimpleNamespace(bridge=bridge,is_alive=lambda:True,session_memory_inferred_source_request=source)
    state = SimpleNamespace(memory_provider=provider,account_request_barrier=SimpleNamespace(admitted=barrier),
        mimo_supervisor=SimpleNamespace(worker_for_owner=lambda owner: worker),
        auth_manager=SimpleNamespace(account_id=lambda owner:"acct", username_for_account_id=lambda account:"alice",
            get_privileges=lambda owner: {"can_manage_memory":True}))
    monkeypatch.setattr("core.models.get_session_manager_instance", lambda:SimpleNamespace(get_session=lambda chat:session,lifecycle_context=state))
    monkeypatch.setattr("src.openclank.chat_routing.resolve_chat_route", lambda **kwargs:SimpleNamespace())
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda owner: {"memory_mode":"automatic"})
    monkeypatch.setattr(ai_interaction, "_memory_provider", provider)
    execute = AsyncMock(return_value={"results":"nothing"})
    monkeypatch.setattr(ai_interaction,"do_manage_memory",execute)
    request = {"identity":identity,"toolCallID":"call","messageID":"message","arguments":{"action":"list"}}
    assert (await manage(bridge,request))["state"] == "complete"
    provider._guarded_v17=True
    assert (await manage(bridge,{**request,"arguments":{"action":"delete","memory_id":"m"}}))["state"] == "review_required"
    worker.session_memory_inferred_source_request=AsyncMock(return_value={"state":"unavailable"})
    assert (await manage(bridge,request))["state"] == "unavailable"
    execute.assert_awaited_once()

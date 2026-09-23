import asyncio
import copy
import json
import os
import pytest
from types import SimpleNamespace

from src.openclank.acp_bridge import ACPBridge, _TurnState


class _Client:
    def __init__(self):
        self.resumed = []
        self.configured = []
        self.prompted = []
        self.callbacks = {}

    def register_callback(self, method, handler):
        self.callbacks[method] = handler

    def on_session_update(self, *_args):
        pass

    async def resume_session(self, session_id, cwd, *, mcp_servers=None):
        self.resumed.append((session_id, cwd, mcp_servers))
        return {"models": {"availableModels": []}}

    async def set_session_config_option(self, session_id, config_id, value):
        self.configured.append((session_id, config_id, value))
        return {}

    async def prompt(self, session_id, parts, *, metadata=None):
        self.prompted.append((session_id, parts, metadata))
        return {"stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}


def _bridge(tmp_path, adapter):
    return ACPBridge(_Client(), str(tmp_path), owner="alice", session_workspace_adapter=adapter)


@pytest.fixture(autouse=True)
def _durable_projection(monkeypatch, request):
    """Give accepted callback tests a strict, durable projection seam."""
    from src.openclank import transcript_projection

    # The restart regression below uses the real canonical SQL projection;
    # every other isolated bridge test gets a small per-test durable seam.
    if request.node.name == "test_session_cwd_restores_on_bridge_restart_and_resume":
        return

    stored = {}

    def save_state(session_id, state, *, owner=None):
        stored[session_id] = dict(state)
        return {**stored[session_id], "revision": int(state.get("revision", 0)) + 1}

    def get_state(session_id, owner=None):
        if session_id not in stored:
            raise KeyError(session_id)
        return dict(stored[session_id])

    monkeypatch.setattr(transcript_projection, "save_mimo_state", save_state)
    monkeypatch.setattr(transcript_projection, "get_mimo_state", get_state)


async def _request_cwd(bridge, mimo_session_id, cwd):
    return await bridge._handle_session_cwd_change(
        {"sessionID": mimo_session_id, "requestedCwd": cwd}
    )


def test_managed_cwd_callback_is_registered_once_and_returns_canonical_ack(tmp_path):
    async def adapter(_chat_id, cwd, _context):
        return cwd

    client = _Client()
    bridge = ACPBridge(client, str(tmp_path), owner="alice", session_workspace_adapter=adapter)
    bridge._session_map["chat-a"] = "mimo-a"
    bridge._session_context["mimo-a"] = {
        "odysseus_session_id": "chat-a",
        "owner": "alice",
        "workspace": str(tmp_path),
    }
    assert list(client.callbacks).count("_openclank/session/v1/cwd/change") == 1
    result = asyncio.run(
        client.callbacks["_openclank/session/v1/cwd/change"](
            {"sessionID": "mimo-a", "requestedCwd": str(tmp_path)}
        )
    )
    assert result["canonicalCwd"] == str(tmp_path)

    # The residual ACP event is a read-only projection; it cannot re-apply a
    # rejected/stale workspace transition after the callback has committed.
    asyncio.run(
        bridge._handle_session_update(
            "mimo-a", {"sessionUpdate": "_openclank_session_cwd", "cwd": "/etc"}
        )
    )
    assert bridge.mapped_session_workspace("chat-a") == str(tmp_path)


def test_session_cwd_isolated_per_chat_and_does_not_change_process_cwd(tmp_path):
    updates = []

    async def adapter(chat_id, cwd, _context):
        updates.append((chat_id, cwd))
        return cwd

    bridge = _bridge(tmp_path, adapter)
    bridge._session_map.update({"chat-a": "mimo-a", "chat-b": "mimo-b"})
    bridge._session_context.update(
        {
            "mimo-a": {"odysseus_session_id": "chat-a", "workspace": "/work/a"},
            "mimo-b": {"odysseus_session_id": "chat-b", "workspace": "/work/b"},
        }
    )
    before = os.getcwd()

    async def run():
        await asyncio.gather(
            _request_cwd(bridge, "mimo-a", "/next/a"),
            _request_cwd(bridge, "mimo-b", "/next/b"),
        )

    asyncio.run(run())

    assert bridge.mapped_session_workspace("chat-a") == "/next/a"
    assert bridge.mapped_session_workspace("chat-b") == "/next/b"
    assert bridge.mapped_session_workspace("new-chat") == str(tmp_path)
    assert os.getcwd() == before
    assert sorted(updates) == [("chat-a", "/next/a"), ("chat-b", "/next/b")]


def test_session_cwd_update_is_rendered_as_a_private_turn_event(tmp_path):
    bridge = _bridge(tmp_path, None)
    state = _TurnState()
    [raw] = bridge._process_update(
        "mimo",
        {"sessionUpdate": "_openclank_session_cwd", "cwd": "/next/workspace"},
        state,
    )
    payload = json.loads(raw[len("data: ") :])
    assert payload == {
        "type": "session_cwd",
        "data": {"sessionUpdate": "_openclank_session_cwd", "cwd": "/next/workspace"},
    }


def test_session_cwd_restores_on_bridge_restart_and_resume(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core import database
    from src.openclank import transcript_projection

    engine = create_engine(f"sqlite:///{tmp_path / 'canonical.db'}")
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(transcript_projection, "SessionLocal", sessions)
    db = sessions()
    db.add(
        database.Session(
            id="chat-a",
            name="Chat A",
            endpoint_url="mimo://acp",
            model="mimo",
            owner="alice",
            mimo_state={
                "workspace": "/work/a",
                "file_policy_workspace": "/policy/a",
                "copal_workspace": "copal-a",
                "memory_workspace": "memory-a",
            },
        )
    )
    db.commit()
    db.close()

    map_path = tmp_path / "session-map.json"
    client = _Client()
    async def adapter(_chat_id, cwd, _context):
        return cwd

    bridge = ACPBridge(
        client, str(tmp_path), owner="alice", session_map_path=map_path, session_workspace_adapter=adapter
    )
    bridge._session_map["chat-a"] = "mimo-a"
    bridge._session_context["mimo-a"] = {
        "odysseus_session_id": "chat-a",
        "owner": "alice",
        "workspace": "/work/a",
        "file_policy_workspace": "/policy/a",
        "copal_workspace": "copal-a",
        "memory_workspace": "memory-a",
    }
    bridge._session_state["mimo-a"] = {"workspace": "/work/a"}

    asyncio.run(
        _request_cwd(bridge, "mimo-a", "/next/a")
    )
    map_path.write_text(json.dumps(bridge._session_map))

    restarted_client = _Client()
    restarted = ACPBridge(
        restarted_client,
        str(tmp_path),
        owner="alice",
        session_map_path=map_path,
        session_workspace_adapter=adapter,
    )
    assert restarted.mapped_session_workspace("chat-a") == "/next/a"
    assert restarted.mapped_session_workspace("new-chat") == str(tmp_path)
    asyncio.run(restarted.ensure_session("chat-a", owner="alice"))
    assert restarted_client.resumed[0][0:2] == ("mimo-a", "/next/a")
    assert restarted._session_context["mimo-a"]["workspace"] == "/next/a"
    assert restarted._session_state["mimo-a"]["workspace"] == "/next/a"
    assert restarted._session_context["mimo-a"]["file_policy_workspace"] == "/policy/a"
    assert restarted._session_context["mimo-a"]["copal_workspace"] == "copal-a"
    assert restarted._session_context["mimo-a"]["memory_workspace"] == "memory-a"


@pytest.mark.parametrize("failure", ["missing_projection", "commit_failed"])
def test_session_cwd_persistence_failure_rolls_back_host_state(tmp_path, monkeypatch, failure):
    from src.openclank import transcript_projection

    allowed = tmp_path / "allowed"
    allowed.mkdir()

    def save_state(*_args, **_kwargs):
        if failure == "missing_projection":
            raise KeyError("projection missing")
        raise RuntimeError("projection commit failed")

    monkeypatch.setattr(transcript_projection, "save_mimo_state", save_state)
    bridge = _bridge(tmp_path, lambda _chat_id, cwd, _context: cwd)
    bridge._session_map["chat-a"] = "mimo-a"
    bridge._session_context["mimo-a"] = {
        "odysseus_session_id": "chat-a",
        "owner": "alice",
        "workspace": str(allowed),
        "cwd": str(allowed),
        "physical_cwd": str(allowed),
    }
    bridge._session_state["mimo-a"] = {"workspace": str(allowed), "revision": 4}
    previous_context = copy.deepcopy(bridge._session_context["mimo-a"])
    previous_state = copy.deepcopy(bridge._session_state["mimo-a"])

    with pytest.raises(ValueError):
        asyncio.run(_request_cwd(bridge, "mimo-a", str(tmp_path)))

    assert bridge._session_context["mimo-a"] == previous_context
    assert bridge._session_state["mimo-a"] == previous_state
    assert bridge.mapped_session_workspace("chat-a") == str(allowed)


def test_session_cwd_requires_mapped_owner_and_authorized_root(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    updates = []

    async def adapter(chat_id, cwd, context):
        assert context["owner"] == "alice"
        if chat_id != "chat-a":
            raise ValueError("outside owner root")
        try:
            __import__("pathlib").Path(cwd).relative_to(allowed)
        except ValueError as exc:
            raise ValueError("outside owner root") from exc
        updates.append((chat_id, cwd))
        return cwd

    bridge = _bridge(tmp_path, adapter)
    bridge._session_map["chat-a"] = "mimo-a"
    bridge._session_context["mimo-a"] = {
        "odysseus_session_id": "chat-a",
        "owner": "alice",
        "workspace": str(allowed),
        "file_policy_workspace": "workspace-stable",
        "copal_workspace": "copal-a",
        "memory_workspace": "memory-a",
        "goal_id": "goal-a",
    }

    async def run():
        try:
            await _request_cwd(bridge, "mimo-a", "/etc")
        except ValueError:
            pass
        try:
            await _request_cwd(bridge, "unknown", str(allowed))
        except ValueError:
            pass
        await _request_cwd(bridge, "mimo-a", str(allowed))

    asyncio.run(run())
    assert bridge.mapped_session_workspace("chat-a") == str(allowed)
    assert bridge._session_context["mimo-a"]["file_policy_workspace"] == "workspace-stable"
    assert bridge._session_context["mimo-a"]["copal_workspace"] == "copal-a"
    assert bridge._session_context["mimo-a"]["memory_workspace"] == "memory-a"
    assert bridge._session_context["mimo-a"]["goal_id"] == "goal-a"
    assert updates == [("chat-a", str(allowed))]


def test_session_cwd_rejects_sibling_root(tmp_path):
    allowed = tmp_path / "allowed"
    sibling = tmp_path / "allowed-sibling"
    allowed.mkdir()
    sibling.mkdir()

    def adapter(_chat_id, cwd, _context):
        __import__("pathlib").Path(cwd).relative_to(allowed)
        return cwd

    bridge = _bridge(tmp_path, adapter)
    bridge._session_map["chat-a"] = "mimo-a"
    bridge._session_context["mimo-a"] = {
        "odysseus_session_id": "chat-a", "owner": "alice", "workspace": str(allowed)
    }
    try:
        asyncio.run(_request_cwd(bridge, "mimo-a", str(sibling)))
    except ValueError:
        pass
    assert bridge.mapped_session_workspace("chat-a") == str(allowed)


def test_session_cwd_rejects_stale_mapping_and_foreign_owner(tmp_path):
    accepted = []

    def adapter(chat_id, cwd, context):
        accepted.append((chat_id, cwd))
        return cwd

    bridge = _bridge(tmp_path, adapter)
    bridge._session_map["chat-a"] = "mimo-new"
    bridge._session_context["mimo-old"] = {
        "odysseus_session_id": "chat-a", "owner": "alice", "workspace": str(tmp_path)
    }
    bridge._session_context["mimo-new"] = {
        "odysseus_session_id": "chat-a", "owner": "bob", "workspace": str(tmp_path)
    }

    async def run():
        for session_id in ("mimo-old", "mimo-new"):
            try:
                await _request_cwd(bridge, session_id, str(tmp_path))
            except ValueError:
                pass

    asyncio.run(run())
    assert accepted == []


def test_mapped_chat_ignores_stale_caller_cwd_on_resume(tmp_path):
    client = _Client()
    bridge = ACPBridge(client, str(tmp_path), owner="alice")
    bridge._session_map["chat-a"] = "mimo-a"
    bridge._session_context["mimo-a"] = {
        "odysseus_session_id": "chat-a", "owner": "alice", "workspace": str(tmp_path)
    }
    asyncio.run(bridge.ensure_session("chat-a", cwd="/etc", owner="alice"))
    assert client.resumed[0][1] == str(tmp_path)


def test_normal_turn_and_config_keep_the_mapped_mimo_session(tmp_path):
    client = _Client()
    bridge = ACPBridge(client, str(tmp_path), owner="alice")
    bridge._session_map["chat-a"] = "mimo-a"
    bridge._session_context["mimo-a"] = {
        "odysseus_session_id": "chat-a", "owner": "alice", "workspace": str(tmp_path)
    }
    deleted = []

    async def delete(chat_id, **_kwargs):
        deleted.append(chat_id)

    bridge.set_session_delete_callback(delete)
    bridge._session_state["mimo-a"] = {
        "config_options": [{"id": "mode", "options": [{"value": "build"}]}],
        "workspace": str(tmp_path),
    }
    asyncio.run(bridge.set_config_option("chat-a", "mode", "build", owner="alice"))
    assert client.configured == [("mimo-a", "mode", "build")]
    assert bridge.mapped_session_id("chat-a") == "mimo-a"
    assert deleted == []
    asyncio.run(bridge.ensure_session("chat-a", cwd="/stale", owner="alice"))
    asyncio.run(bridge.ensure_session("chat-a", cwd="/also-stale", owner="alice"))
    assert [call[0] for call in client.resumed] == ["mimo-a", "mimo-a"]

    async def collect_invalid_turn():
        return [item async for item in bridge.run_turn(
            "chat-a", [{"role": "user", "content": "hello"}], turn_envelope={"authority_workspace_id": "bad space"}
        )]

    asyncio.run(collect_invalid_turn())
    assert bridge.mapped_session_id("chat-a") == "mimo-a"
    assert deleted == []


def test_two_normal_turns_resume_one_mimo_session_and_retain_chat_state(tmp_path, monkeypatch):
    from src.openclank import transcript_projection

    persisted = {}

    def save_state(_session_id, state, *, owner=None):
        persisted.update(state)
        return {**persisted, "revision": int(persisted.get("revision", 0)) + 1}

    monkeypatch.setattr(transcript_projection, "save_mimo_state", save_state)
    monkeypatch.setattr(transcript_projection, "get_mimo_state", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyError("chat-a")))
    monkeypatch.setattr(transcript_projection, "canonical_snapshot", lambda *_args, **_kwargs: SimpleNamespace(revision=1))
    monkeypatch.setattr(transcript_projection, "record_projection", lambda *_args, **_kwargs: None)

    client = _Client()
    bridge = ACPBridge(client, str(tmp_path), owner="alice")
    bridge._session_map["chat-a"] = "mimo-a"
    bridge._session_context["mimo-a"] = {
        "odysseus_session_id": "chat-a",
        "owner": "alice",
        "workspace": str(tmp_path),
        "cwd": str(tmp_path),
        "goal_id": "goal-a",
        "workspace_id": "workspace-a",
    }
    bridge._session_state["mimo-a"] = {
        "config_options": [{"id": "mode", "options": [{"value": "build"}]}],
        "workspace": str(tmp_path),
        "goal_id": "goal-a",
        "desired": {"model": "mimo/gpt-5.6-luna"},
    }
    deleted = []

    async def delete(chat_id, **_kwargs):
        deleted.append(chat_id)

    bridge.set_session_delete_callback(delete)

    async def run_turns():
        first = [item async for item in bridge.run_turn(
            "chat-a", [{"role": "user", "content": "first"}], owner="alice", cwd="/stale-a"
        )]
        second = [item async for item in bridge.run_turn(
            "chat-a", [{"role": "user", "content": "second"}], owner="alice", cwd="/stale-b"
        )]
        return first, second

    asyncio.run(run_turns())
    assert [session_id for session_id, _parts, _metadata in client.prompted] == ["mimo-a", "mimo-a"]
    assert [call[0] for call in client.resumed] == ["mimo-a", "mimo-a"]
    assert bridge.mapped_session_id("chat-a") == "mimo-a"
    assert bridge._session_context["mimo-a"]["cwd"] == str(tmp_path)
    assert bridge._session_context["mimo-a"]["goal_id"] == "goal-a"
    assert bridge._session_state["mimo-a"]["desired"]["model"] == "mimo/gpt-5.6-luna"
    assert deleted == []

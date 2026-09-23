import asyncio
import json
import os

from src.openclank.acp_bridge import ACPBridge, _TurnState


class _Client:
    def __init__(self):
        self.resumed = []

    def register_callback(self, *_args):
        pass

    def on_session_update(self, *_args):
        pass

    async def resume_session(self, session_id, cwd, *, mcp_servers=None):
        self.resumed.append((session_id, cwd, mcp_servers))
        return {"models": {"availableModels": []}}


def _bridge(tmp_path, adapter):
    return ACPBridge(_Client(), str(tmp_path), owner="alice", session_workspace_adapter=adapter)


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
            bridge._handle_session_update(
                "mimo-a", {"sessionUpdate": "_openclank_session_cwd", "cwd": "/next/a"}
            ),
            bridge._handle_session_update(
                "mimo-b", {"sessionUpdate": "_openclank_session_cwd", "cwd": "/next/b"}
            ),
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
    from src.openclank import transcript_projection

    persisted = {}

    def save_state(_session_id, state, *, owner=None):
        persisted.update(state)
        return {**persisted, "revision": 1}

    monkeypatch.setattr(transcript_projection, "save_mimo_state", save_state)
    def get_state(session_id, owner=None):
        if session_id != "chat-a":
            raise KeyError(session_id)
        return dict(persisted)

    monkeypatch.setattr(transcript_projection, "get_mimo_state", get_state)

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
        bridge._handle_session_update(
            "mimo-a", {"sessionUpdate": "_openclank_session_cwd", "cwd": "/next/a"}
        )
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
    restarted._session_state["mimo-a"] = dict(persisted)
    restarted._bind_canonical_session("mimo-a", "chat-a", "alice")
    assert restarted._session_context["mimo-a"]["file_policy_workspace"] == "/policy/a"
    assert restarted._session_context["mimo-a"]["copal_workspace"] == "copal-a"
    assert restarted._session_context["mimo-a"]["memory_workspace"] == "memory-a"
    asyncio.run(restarted.ensure_session("chat-a", owner="alice"))
    assert restarted_client.resumed[0][0:2] == ("mimo-a", "/next/a")


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
    }

    async def run():
        await bridge._handle_session_update(
            "mimo-a", {"sessionUpdate": "_openclank_session_cwd", "cwd": "/etc"}
        )
        await bridge._handle_session_update(
            "unknown", {"sessionUpdate": "_openclank_session_cwd", "cwd": str(allowed)}
        )
        await bridge._handle_session_update(
            "mimo-a", {"sessionUpdate": "_openclank_session_cwd", "cwd": str(allowed)}
        )

    asyncio.run(run())
    assert bridge.mapped_session_workspace("chat-a") == str(allowed)
    assert bridge._session_context["mimo-a"]["file_policy_workspace"] == "workspace-stable"
    assert bridge._session_context["mimo-a"]["copal_workspace"] == "copal-a"
    assert bridge._session_context["mimo-a"]["memory_workspace"] == "memory-a"
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
    asyncio.run(
        bridge._handle_session_update(
            "mimo-a", {"sessionUpdate": "_openclank_session_cwd", "cwd": str(sibling)}
        )
    )
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
        await bridge._handle_session_update(
            "mimo-old", {"sessionUpdate": "_openclank_session_cwd", "cwd": str(tmp_path)}
        )
        await bridge._handle_session_update(
            "mimo-new", {"sessionUpdate": "_openclank_session_cwd", "cwd": str(tmp_path)}
        )

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

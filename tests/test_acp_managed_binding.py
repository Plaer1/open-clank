import asyncio
import pytest
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as database
import src.openclank.transcript_projection as projection
from core.database import Base, Session
from src.openclank.session_map import OwnerSessionMap


@pytest.fixture
def sqlite_projection(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'managed-binding.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(projection, "SessionLocal", sessions)
    db = sessions()
    db.add(Session(
        id="chat-a",
        name="chat",
        endpoint_url="http://example.test",
        model="model",
        owner="alice",
        mimo_state={},
    ))
    db.commit()
    db.close()
    return sessions


def _binding(*, engine="engine-a", map_revision=1, mapping_revision=1, chat="chat-a"):
    return {
        "owner": "alice",
        "stableChatID": chat,
        "engineSessionID": engine,
        "engineAliases": [],
        "memoryWorkspaceID": "memory:chat-a",
        "authorityWorkspaceID": "workspace:chat-a",
        "copalWorkspace": "default",
        "physicalCwd": str(Path("/tmp").resolve()),
        "workspaceRevision": 0,
        "mapRevision": map_revision,
        "mappingRevision": mapping_revision,
        "memoryEnabled": True,
        "transition": None,
    }


def test_binding_cas_checks_engine_and_both_map_axes(sqlite_projection):
    projection.save_managed_binding(
        "chat-a",
        _binding(),
        owner="alice",
        expected_workspace_revision=0,
        expected_engine_session_id=None,
        expected_map_revision=0,
        expected_mapping_revision=0,
    )
    with pytest.raises(ValueError, match="engine-session conflict"):
        projection.save_managed_binding(
            "chat-a",
            _binding(engine="engine-b"),
            owner="alice",
            expected_workspace_revision=0,
            expected_engine_session_id=None,
            expected_map_revision=0,
            expected_mapping_revision=0,
        )
    with pytest.raises(ValueError, match="map revision conflict"):
        projection.save_managed_binding(
            "chat-a",
            _binding(engine="engine-b", map_revision=2),
            owner="alice",
            expected_workspace_revision=0,
            expected_engine_session_id="engine-a",
            expected_map_revision=0,
            expected_mapping_revision=1,
        )
    with pytest.raises(ValueError, match="mapping revision conflict"):
        projection.delete_managed_binding(
            "chat-a",
            owner="alice",
            expected_workspace_revision=0,
            expected_engine_session_id="engine-a",
            expected_map_revision=1,
            expected_mapping_revision=0,
        )


def test_whole_state_save_preserves_authoritative_binding(sqlite_projection):
    binding = _binding()
    projection.save_managed_binding(
        "chat-a",
        binding,
        owner="alice",
        expected_workspace_revision=0,
        expected_engine_session_id=None,
        expected_map_revision=0,
        expected_mapping_revision=0,
    )
    projection.save_mimo_state("chat-a", {"models": {"desired": "kept"}}, owner="alice")
    assert projection.get_managed_binding("chat-a", owner="alice") == binding


def test_stale_bridge_forget_does_not_adopt_or_delete_successor():
    from src.openclank.acp_bridge import ACPBridge

    class DurableMap:
        def lookup(self, _chat):
            return {"current": "engine-new", "revision": 2}

        def flat_current(self):
            return {"chat-a": "engine-new"}

        def revisions(self, _chat):
            return (3, 2)

        def forget(self, *_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("stale bridge attempted successor deletion")

    bridge = ACPBridge.__new__(ACPBridge)
    bridge._session_map = {"chat-a": "engine-old"}
    bridge._durable_session_map = DurableMap()
    bridge._session_models = {"engine-old": ["old"]}
    bridge._session_state = {"engine-old": {"old": True}}
    bridge._session_context = {"engine-old": {"old": True}}
    bridge._turns = {"engine-old": object()}
    bridge._queues = {"engine-old": object()}
    bridge._owner = "alice"

    bridge.forget_session("chat-a")

    assert bridge._session_map == {"chat-a": "engine-new"}
    assert "engine-old" in bridge._session_state


def test_rpc_error_preserves_typed_missing_session_signal():
    from src.openclank.acp_client import RPCError

    missing = RPCError(400, "session missing", {"code": "OPENCLANK_SESSION_MISSING"})
    generic = RPCError(400, "invalid params", {"code": "INVALID_PARAMS"})
    assert missing.session_missing is True
    assert missing.data["code"] == "OPENCLANK_SESSION_MISSING"
    assert generic.session_missing is False


def test_private_discard_sends_canonical_candidate_cwd():
    from src.openclank.acp_client import ACPClient

    client = ACPClient.__new__(ACPClient)
    calls = []

    async def send(method, params):
        calls.append((method, params))
        return {"deleted": True}

    client._send_request = send
    asyncio.run(client.discard_session("engine-candidate", "/tmp/canonical"))
    assert calls == [(
        "_odysseus/session/discard",
        {"sessionId": "engine-candidate", "cwd": "/tmp/canonical"},
    )]


def test_candidate_discard_uses_persisted_binding_cwd(sqlite_projection, tmp_path):
    from src.openclank.acp_bridge import ACPBridge

    binding = _binding()
    projection.save_managed_binding(
        "chat-a", binding, owner="alice", expected_workspace_revision=0,
        expected_engine_session_id=None, expected_map_revision=0, expected_mapping_revision=0,
    )

    class Client:
        def __init__(self):
            self.calls = []

        async def discard_session(self, session_id, cwd):
            self.calls.append((session_id, cwd))

    client = Client()
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._client = client
    bridge._owner = "alice"
    bridge._durable_session_map = type("Map", (), {"fence": lambda *_args: None, "flat_current": lambda *_args: {}})()
    bridge._pending_session_results = {}
    bridge._session_models = {}
    bridge._session_state = {}
    bridge._session_context = {}
    bridge._turns = {}
    bridge._queues = {}
    bridge.cleanup_session = lambda _session_id: asyncio.sleep(0)
    asyncio.run(bridge._discard_unbound_engine_session("engine-a", chat_id="chat-a"))
    assert client.calls == [("engine-a", binding["physicalCwd"])]


def test_candidate_binding_uses_per_chat_axes_after_unrelated_map_mutation(sqlite_projection, tmp_path):
    db = sqlite_projection()
    db.add(Session(
        id="chat-b", name="chat", endpoint_url="http://example.test",
        model="model", owner="alice", mimo_state={},
    ))
    db.commit()
    db.close()
    from src.openclank.acp_bridge import ACPBridge

    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    mapping.bind("chat-a", "engine-a", expected_current=None, expected_mapping_revision=0)
    projection.save_managed_binding(
        "chat-a", _binding(), owner="alice", expected_workspace_revision=0,
        expected_engine_session_id=None, expected_map_revision=0, expected_mapping_revision=0,
    )
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._durable_session_map = mapping
    bridge._owner = "alice"
    mapping.bind("chat-c", "engine-c", expected_current=None, expected_mapping_revision=0)

    fresh = bridge._stage_candidate_binding(
        "chat-b", "engine-b2", "alice", previous_binding=None,
        cwd=str(tmp_path), authority_workspace_id="workspace:chat-b",
        memory_enabled=True, copal_workspace="default", memory_workspace_id="memory:chat-b",
    )
    assert fresh["mappingRevision"] == 1
    remapped = bridge._stage_candidate_binding(
        "chat-a", "engine-a2", "alice", previous_binding=_binding(),
        cwd=str(tmp_path), authority_workspace_id="workspace:chat-a",
        memory_enabled=True, copal_workspace="default", memory_workspace_id="memory:chat-a",
    )
    assert remapped["mappingRevision"] == 2


def test_stale_engine_cwd_cannot_overwrite_durable_new_engine(sqlite_projection, tmp_path):
    from src.openclank.acp_bridge import ACPBridge

    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    mapping.bind("chat-a", "engine-new", expected_current=None, expected_mapping_revision=0)
    binding = _binding(engine="engine-new")
    projection.save_managed_binding(
        "chat-a", binding, owner="alice", expected_workspace_revision=0,
        expected_engine_session_id=None, expected_map_revision=0, expected_mapping_revision=0,
    )
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._owner = "alice"
    bridge._durable_session_map = mapping
    bridge._session_map = {"chat-a": "engine-new"}
    bridge._session_context_locks = {}
    bridge._session_state = {}
    bridge._session_workspace_adapter = lambda *_args: (_ for _ in ()).throw(AssertionError("stale cwd reached adapter"))
    bridge._session_context = {
        "engine-old": {
            "odysseus_session_id": "chat-a", "owner": "alice",
            "workspace": binding["physicalCwd"], "managed_binding": _binding(engine="engine-old"),
        }
    }
    result = asyncio.run(bridge._apply_session_cwd("engine-old", str(tmp_path)))
    assert result["code"] == "stale_engine_session"
    assert mapping.lookup("chat-a")["current"] == "engine-new"
    assert projection.get_managed_binding("chat-a", owner="alice")["engineSessionID"] == "engine-new"


def test_stale_cwd_with_auxiliary_binding_axis_change_is_rejected(sqlite_projection, tmp_path):
    from src.openclank.acp_bridge import ACPBridge

    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    mapping.bind("chat-a", "engine-new", expected_current=None, expected_mapping_revision=0)
    binding = _binding(engine="engine-new")
    projection.save_managed_binding(
        "chat-a", binding, owner="alice", expected_workspace_revision=0,
        expected_engine_session_id=None, expected_map_revision=0, expected_mapping_revision=0,
    )
    stale = dict(binding)
    stale["memoryWorkspaceID"] = "memory:stale"
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._owner = "alice"
    bridge._durable_session_map = mapping
    bridge._session_map = {"chat-a": "engine-new"}
    bridge._session_context_locks = {}
    bridge._session_state = {}
    bridge._session_workspace_adapter = lambda *_args: (_ for _ in ()).throw(AssertionError("stale cwd reached adapter"))
    bridge._session_context = {
        "engine-new": {
            "odysseus_session_id": "chat-a", "owner": "alice",
            "workspace": binding["physicalCwd"], "managed_binding": stale,
        }
    }
    result = asyncio.run(bridge._apply_session_cwd("engine-new", str(tmp_path)))
    assert result["code"] == "stale_engine_session"

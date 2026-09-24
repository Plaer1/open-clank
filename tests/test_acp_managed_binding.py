import asyncio
import pytest
from contextlib import nullcontext
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


def test_ensure_rejects_foreign_owner_before_rpc_or_authority_mutation():
    from src.openclank.acp_bridge import ACPBridge

    bridge = ACPBridge.__new__(ACPBridge)
    bridge._owner = "alice"
    bridge._session_admission_locks = {}
    bridge._client = type("Client", (), {
        "new_session": lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("RPC")),
    })()
    with pytest.raises(ValueError, match="owner does not match"):
        asyncio.run(bridge.ensure_session("chat-a", owner="bob"))


def test_real_ensure_fresh_two_bridge_race_discards_only_loser(sqlite_projection, tmp_path, monkeypatch):
    from src.openclank.acp_bridge import ACPBridge
    monkeypatch.setattr("src.openclank.acp_bridge.chat_workspace", lambda: "memory:chat-a")

    mapping_path = tmp_path / "session-map.json"
    ready = asyncio.Event()
    created = []
    discarded = []

    class Client:
        def __init__(self, session_id):
            self.session_id = session_id

        async def new_session(self, *_args, **_kwargs):
            created.append(self.session_id)
            if len(created) == 2:
                ready.set()
            await ready.wait()
            return {"sessionId": self.session_id, "models": {}}

        async def resume_session(self, session_id, *_args, **_kwargs):
            assert session_id in {"engine-c1", "engine-c2"}
            return {"models": {}}

        async def discard_session(self, session_id, cwd):
            discarded.append((session_id, cwd))
            return None

    def bridge_for(session_id):
        bridge = ACPBridge.__new__(ACPBridge)
        bridge._client = Client(session_id)
        bridge._cwd = str(tmp_path.resolve())
        bridge._owner = "alice"
        bridge._durable_session_map = OwnerSessionMap(mapping_path, "alice")
        bridge._session_map = {}
        bridge._session_admission_locks = {}
        bridge._session_context_locks = {}
        bridge._pending_session_results = {}
        bridge._session_models = {}
        bridge._session_state = {}
        bridge._session_context = {}
        bridge._turns = {}
        bridge._queues = {}
        bridge._delete_session_callback = None
        bridge.cleanup_session = lambda _session_id: asyncio.sleep(0)
        return bridge

    first = bridge_for("engine-c1")
    second = bridge_for("engine-c2")
    async def run_race():
        return await asyncio.gather(
            first.ensure_session(
                "chat-a", cwd=str(tmp_path.resolve()), authority_workspace_id="workspace:chat-a"
            ),
            second.ensure_session(
                "chat-a", cwd=str(tmp_path.resolve()), authority_workspace_id="workspace:chat-a"
            ),
        )

    results = asyncio.run(run_race())
    assert results[0] == results[1]
    assert mapping_path.exists()
    assert first._durable_session_map.lookup("chat-a")["current"] == results[0]
    assert len(discarded) == 1
    assert discarded[0][0] != results[0]


def test_real_ensure_missing_session_remap_race_returns_one_winner(sqlite_projection, tmp_path, monkeypatch):
    from src.openclank.acp_bridge import ACPBridge
    from src.openclank.acp_client import RPCError

    monkeypatch.setattr("src.openclank.acp_bridge.chat_workspace", lambda: "memory:chat-a")
    mapping_path = tmp_path / "session-map.json"
    mapping = OwnerSessionMap(mapping_path, "alice")
    mapping.bind("chat-a", "engine-old", expected_current=None, expected_mapping_revision=0)
    projection.save_managed_binding(
        "chat-a", _binding(engine="engine-old"), owner="alice",
        expected_workspace_revision=0, expected_engine_session_id=None,
        expected_map_revision=0, expected_mapping_revision=0,
    )
    ready = asyncio.Event()
    created = []
    discarded = []

    class Client:
        def __init__(self, session_id):
            self.session_id = session_id

        async def resume_session(self, session_id, *_args, **_kwargs):
            if session_id == "engine-old":
                raise RPCError(404, "missing", {"code": "OPENCLANK_SESSION_MISSING"})
            return {"models": {}}

        async def new_session(self, *_args, **_kwargs):
            created.append(self.session_id)
            if len(created) == 2:
                ready.set()
            await ready.wait()
            return {"sessionId": self.session_id, "models": {}}

        async def discard_session(self, session_id, cwd):
            discarded.append((session_id, cwd))

    def bridge_for(session_id):
        bridge = ACPBridge.__new__(ACPBridge)
        bridge._client = Client(session_id)
        bridge._cwd = str(tmp_path.resolve())
        bridge._owner = "alice"
        bridge._durable_session_map = OwnerSessionMap(mapping_path, "alice")
        bridge._session_map = {"chat-a": "engine-old"}
        bridge._session_admission_locks = {}
        bridge._session_context_locks = {}
        bridge._pending_session_results = {}
        bridge._session_models = {}
        bridge._session_state = {}
        bridge._session_context = {}
        bridge._turns = {}
        bridge._queues = {}
        bridge._delete_session_callback = None
        bridge.cleanup_session = lambda _session_id: asyncio.sleep(0)
        return bridge

    first = bridge_for("engine-r1")
    second = bridge_for("engine-r2")

    async def run_race():
        return await asyncio.gather(
            first.ensure_session("chat-a", authority_workspace_id="workspace:chat-a"),
            second.ensure_session("chat-a", authority_workspace_id="workspace:chat-a"),
        )

    results = asyncio.run(run_race())
    assert results[0] == results[1]
    assert results[0] in {"engine-r1", "engine-r2"}
    assert mapping.lookup("chat-a")["current"] == results[0]
    assert len(discarded) == 1
    assert discarded[0][0] != results[0]


def test_real_ensure_repairs_staged_orphan_before_resume(sqlite_projection, tmp_path):
    from src.openclank.acp_bridge import ACPBridge

    mapping_path = tmp_path / "session-map.json"
    mapping = OwnerSessionMap(mapping_path, "alice")
    mapping.bind("chat-a", "engine-old", expected_current=None, expected_mapping_revision=0)
    orphan = _binding(engine="engine-candidate", map_revision=1, mapping_revision=2)
    orphan["engineAliases"] = ["engine-old"]
    projection.save_managed_binding(
        "chat-a", orphan, owner="alice", expected_workspace_revision=0,
        expected_engine_session_id=None, expected_map_revision=0, expected_mapping_revision=0,
    )
    discarded = []

    class Client:
        async def resume_session(self, session_id, *_args, **_kwargs):
            assert session_id == "engine-old"
            return {"models": {}}

        async def discard_session(self, session_id, cwd):
            discarded.append((session_id, cwd))

    bridge = ACPBridge.__new__(ACPBridge)
    bridge._client = Client()
    bridge._cwd = str(tmp_path.resolve())
    bridge._owner = "alice"
    bridge._durable_session_map = mapping
    bridge._session_map = {"chat-a": "engine-old"}
    bridge._session_admission_locks = {}
    bridge._session_context_locks = {}
    bridge._pending_session_results = {}
    bridge._session_models = {}
    bridge._session_state = {}
    bridge._session_context = {}
    bridge._turns = {}
    bridge._queues = {}
    bridge.cleanup_session = lambda _session_id: asyncio.sleep(0)
    result = asyncio.run(bridge.ensure_session("chat-a"))
    assert result == "engine-old"
    assert discarded == [("engine-candidate", orphan["physicalCwd"])]
    recovered = projection.get_managed_binding("chat-a", owner="alice")
    assert recovered["engineSessionID"] == "engine-old"
    assert recovered["mappingRevision"] == 1


def test_fenced_chat_rejects_real_ensure_before_engine_rpc(tmp_path):
    from src.openclank.acp_bridge import ACPBridge
    from src.openclank.session_map import SessionMapCollision

    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    mapping.fence("chat-a")
    calls = []

    class Client:
        async def new_session(self, *_args, **_kwargs):
            calls.append("new")
            raise AssertionError("fenced routing reached the engine")

    bridge = ACPBridge.__new__(ACPBridge)
    bridge._client = Client()
    bridge._cwd = str(tmp_path.resolve())
    bridge._owner = "alice"
    bridge._durable_session_map = mapping
    bridge._session_admission_locks = {}
    with pytest.raises(SessionMapCollision, match="quarantined"):
        asyncio.run(bridge.ensure_session("chat-a"))
    assert calls == []


def test_async_chat_admission_lock_cancellation_releases_waiter(tmp_path):
    from contextlib import contextmanager
    from threading import Event

    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    entered = Event()
    allow = Event()
    released = Event()

    @contextmanager
    def blocking_guard(_chat):
        entered.set()
        allow.wait(timeout=5)
        try:
            yield
        finally:
            released.set()

    mapping.chat_admission_lock = blocking_guard

    async def exercise():
        task = asyncio.create_task(_hold_one_tick(mapping))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        allow.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def _hold_one_tick(owner_map):
        async with owner_map.async_chat_admission_lock("chat-a"):
            await asyncio.sleep(60)

    asyncio.run(exercise())
    assert released.is_set()


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

        def chat_admission_lock(self, _chat):
            return nullcontext()

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


def test_private_discard_sends_canonical_candidate_cwd(tmp_path):
    from src.openclank.acp_client import ACPClient

    client = ACPClient.__new__(ACPClient)
    calls = []

    async def send(method, params):
        calls.append((method, params))
        return {"deleted": True}

    client._send_request = send
    canonical_cwd = str(tmp_path.resolve())
    asyncio.run(client.discard_session("engine-candidate", canonical_cwd))
    assert calls == [(
        "_odysseus/session/discard",
        {"sessionId": "engine-candidate", "cwd": canonical_cwd},
    )]


@pytest.mark.parametrize("result", [{}, {"deleted": False}, {"deleted": True, "extra": 1}])
def test_private_discard_rejects_unconfirmed_result(result, tmp_path):
    from src.openclank.acp_client import ACPClient

    client = ACPClient.__new__(ACPClient)

    async def send(_method, _params):
        return result

    client._send_request = send
    with pytest.raises(RuntimeError):
        asyncio.run(client.discard_session("engine-candidate", str(tmp_path.resolve())))
    with pytest.raises(ValueError):
        asyncio.run(client.discard_session("engine-candidate", str(tmp_path / ".." / tmp_path.name)))


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


def test_discard_failure_preserves_binding_and_fences(sqlite_projection, tmp_path):
    binding = _binding()
    projection.save_managed_binding(
        "chat-a", binding, owner="alice", expected_workspace_revision=0,
        expected_engine_session_id=None, expected_map_revision=0, expected_mapping_revision=0,
    )
    from src.openclank.acp_bridge import ACPBridge

    class Client:
        async def discard_session(self, _session_id, _cwd):
            raise RuntimeError("ambiguous discard")

    fenced = []
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._client = Client()
    bridge._owner = "alice"
    bridge._durable_session_map = type("Map", (), {
        "fence": lambda _self, chat: fenced.append(chat),
        "flat_current": lambda _self: {},
    })()
    bridge._pending_session_results = {}
    bridge._session_models = {}
    bridge._session_state = {}
    bridge._session_context = {}
    bridge._turns = {}
    bridge._queues = {}
    bridge.cleanup_session = lambda _session_id: asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="discard was not confirmed"):
        asyncio.run(bridge._discard_unbound_engine_session("engine-a", chat_id="chat-a"))
    assert fenced == ["chat-a"]
    assert projection.get_managed_binding("chat-a", owner="alice")["engineSessionID"] == "engine-a"


def test_map_absent_forget_cleans_owner_binding_and_projection(sqlite_projection, tmp_path):
    binding = _binding()
    projection.save_managed_binding(
        "chat-a", binding, owner="alice", expected_workspace_revision=0,
        expected_engine_session_id=None, expected_map_revision=0, expected_mapping_revision=0,
    )
    from src.openclank.acp_bridge import ACPBridge

    bridge = ACPBridge.__new__(ACPBridge)
    bridge._owner = "alice"
    bridge._session_map = {"chat-a": "engine-a"}
    bridge._durable_session_map = type("Map", (), {
        "lookup": lambda _self, _chat: None,
        "flat_current": lambda _self: {},
        "chat_admission_lock": lambda _self, _chat: nullcontext(),
    })()
    bridge.forget_session("chat-a")
    assert projection.get_managed_binding("chat-a", owner="alice") == {}


def test_map_forget_failure_restores_binding(sqlite_projection, tmp_path):
    binding = _binding()
    projection.save_managed_binding(
        "chat-a", binding, owner="alice", expected_workspace_revision=0,
        expected_engine_session_id=None, expected_map_revision=0, expected_mapping_revision=0,
    )
    from src.openclank.acp_bridge import ACPBridge

    class Map:
        def lookup(self, _chat):
            return {"current": "engine-a", "revision": 1}

        def revisions(self, _chat):
            return (1, 1)

        def forget(self, *_args, **_kwargs):
            raise RuntimeError("map commit failed")

        def chat_admission_lock(self, _chat):
            return nullcontext()

    bridge = ACPBridge.__new__(ACPBridge)
    bridge._owner = "alice"
    bridge._session_map = {"chat-a": "engine-a"}
    bridge._durable_session_map = Map()
    with pytest.raises(RuntimeError, match="map commit failed"):
        bridge.forget_session("chat-a")
    assert projection.get_managed_binding("chat-a", owner="alice")["engineSessionID"] == "engine-a"


def test_map_first_forget_crash_prefix_is_retryable(sqlite_projection, tmp_path, monkeypatch):
    binding = _binding()
    projection.save_managed_binding(
        "chat-a", binding, owner="alice", expected_workspace_revision=0,
        expected_engine_session_id=None, expected_map_revision=0, expected_mapping_revision=0,
    )
    from src.openclank.acp_bridge import ACPBridge

    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    mapping.bind("chat-a", "engine-a", expected_current=None, expected_mapping_revision=0)
    original_delete = projection.delete_managed_binding

    def crash_after_map_delete(*_args, **_kwargs):
        raise RuntimeError("simulated process crash after map delete")

    monkeypatch.setattr(projection, "delete_managed_binding", crash_after_map_delete)
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._owner = "alice"
    bridge._session_map = {"chat-a": "engine-a"}
    bridge._durable_session_map = mapping
    with pytest.raises(RuntimeError, match="binding deletion was not confirmed"):
        bridge.forget_session("chat-a")
    assert mapping.flat_current() == {}
    assert projection.get_managed_binding("chat-a", owner="alice")["engineSessionID"] == "engine-a"

    monkeypatch.setattr(projection, "delete_managed_binding", original_delete)
    bridge.forget_session("chat-a")
    assert projection.get_managed_binding("chat-a", owner="alice") == {}


def test_forget_retries_projection_after_binding_was_already_removed(sqlite_projection, tmp_path, monkeypatch):
    binding = _binding()
    projection.save_managed_binding(
        "chat-a", binding, owner="alice", expected_workspace_revision=0,
        expected_engine_session_id=None, expected_map_revision=0, expected_mapping_revision=0,
    )
    from src.openclank.acp_bridge import ACPBridge

    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    mapping.bind("chat-a", "engine-a", expected_current=None, expected_mapping_revision=0)
    calls = []

    def crash_once(_session_id, *, owner=None):
        calls.append(("delete", _session_id, owner))
        if len(calls) == 1:
            raise RuntimeError("simulated projection crash")

    monkeypatch.setattr(projection, "delete_projection", crash_once)
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._owner = "alice"
    bridge._session_map = {"chat-a": "engine-a"}
    bridge._durable_session_map = mapping
    bridge._session_models = {}
    bridge._session_state = {}
    bridge._session_context = {}
    bridge._turns = {}
    bridge._queues = {}
    with pytest.raises(RuntimeError, match="projection deletion was not confirmed"):
        bridge.forget_session("chat-a")
    assert projection.get_managed_binding("chat-a", owner="alice") == {}

    bridge.forget_session("chat-a")
    assert len(calls) == 2


def test_mapped_sessions_refreshes_durable_authority():
    from src.openclank.acp_bridge import ACPBridge

    bridge = ACPBridge.__new__(ACPBridge)
    bridge._owner = "alice"
    bridge._session_map = {"chat-a": "engine-old"}
    bridge._durable_session_map = type("Map", (), {
        "flat_current": lambda _self: {"chat-a": "engine-new"},
    })()
    assert bridge.mapped_sessions() == {"chat-a": "engine-new"}
    assert bridge._session_map == {"chat-a": "engine-new"}


def test_resume_rejects_stale_engine_id_before_rpc(sqlite_projection, tmp_path):
    from src.openclank.acp_bridge import ACPBridge

    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    mapping.bind("chat-a", "engine-new", expected_current=None, expected_mapping_revision=0)
    projection.save_managed_binding(
        "chat-a", _binding(engine="engine-new"), owner="alice",
        expected_workspace_revision=0, expected_engine_session_id=None,
        expected_map_revision=0, expected_mapping_revision=0,
    )
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._owner = "alice"
    bridge._durable_session_map = mapping
    bridge._session_map = {"chat-a": "engine-new"}
    bridge._client = type("Client", (), {"resume_session": lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stale RPC"))})()
    with pytest.raises(RuntimeError, match="durable current mapping"):
        asyncio.run(bridge.resume_session("chat-a", "engine-old"))


def test_fence_persistence_failure_is_surfaceable():
    from src.openclank.acp_bridge import ACPBridge

    bridge = ACPBridge.__new__(ACPBridge)
    bridge._durable_session_map = type("Map", (), {
        "fence": lambda *_args: (_ for _ in ()).throw(OSError("fence fsync failed")),
    })()
    with pytest.raises(RuntimeError, match="fence could not be persisted"):
        bridge._fence_admission("chat-a", "test")


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


def test_stage_candidate_rejects_map_current_changed_before_binding_write(sqlite_projection, tmp_path):
    from src.openclank.acp_bridge import ACPBridge

    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    mapping.bind("chat-a", "engine-a", expected_current=None, expected_mapping_revision=0)
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._durable_session_map = mapping
    with pytest.raises(ValueError, match="map current changed"):
        bridge._stage_candidate_binding(
            "chat-a", "candidate", "alice", previous_binding=None,
            cwd=str(tmp_path), authority_workspace_id="workspace:chat-a",
            memory_enabled=True, copal_workspace="default", memory_workspace_id="memory:chat-a",
            expected_current=None, expected_mapping_revision=0,
        )
    assert projection.get_managed_binding("chat-a", owner="alice") == {}


def test_fresh_candidate_barrier_cannot_publish_c2_over_c1(sqlite_projection, tmp_path):
    from src.openclank.acp_bridge import ACPBridge

    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    bridge = ACPBridge.__new__(ACPBridge)
    bridge._durable_session_map = mapping
    first = bridge._stage_candidate_binding(
        "chat-a", "engine-c1", "alice", previous_binding=None,
        cwd=str(tmp_path), authority_workspace_id="workspace:chat-a",
        memory_enabled=True, copal_workspace="default", memory_workspace_id="memory:chat-a",
        expected_current=None, expected_mapping_revision=0,
    )
    mapping.bind("chat-a", "engine-c1", expected_current=None, expected_mapping_revision=0)
    with pytest.raises(ValueError, match="map current changed"):
        bridge._stage_candidate_binding(
            "chat-a", "engine-c2", "alice", previous_binding=None,
            cwd=str(tmp_path), authority_workspace_id="workspace:chat-a",
            memory_enabled=True, copal_workspace="default", memory_workspace_id="memory:chat-a",
            expected_current=None, expected_mapping_revision=0,
        )
    assert first["engineSessionID"] == "engine-c1"
    assert projection.get_managed_binding("chat-a", owner="alice")["engineSessionID"] == "engine-c1"


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

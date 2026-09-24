import asyncio
import multiprocessing
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

        async def reserve_session(self):
            sid = getattr(self, "session_id", None) or "ses_reserved_test"
            return {"sessionID": sid, "provisional": True}

        async def new_session(self, *_args, provisional_session_id=None, **_kwargs):
            created.append(self.session_id)
            if len(created) == 2:
                ready.set()
            await ready.wait()
            return {"sessionId": provisional_session_id or self.session_id, "models": {}}

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

        async def reserve_session(self):
            sid = getattr(self, "session_id", None) or "ses_reserved_test"
            return {"sessionID": sid, "provisional": True}

        async def new_session(self, *_args, provisional_session_id=None, **_kwargs):
            created.append(self.session_id)
            if len(created) == 2:
                ready.set()
            await ready.wait()
            return {"sessionId": provisional_session_id or self.session_id, "models": {}}

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
        async def reserve_session(self):
            sid = getattr(self, "session_id", None) or "ses_reserved_test"
            return {"sessionID": sid, "provisional": True}

        async def new_session(self, *_args, provisional_session_id=None, **_kwargs):
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


def _engine_discard_wire_ack():
    """Exact engine `_odysseus/session/discard` RPC result.

    Locked to ``contracts/openclank/acp-session-discard-ack-v1.json``; the
    engine extMethod test asserts the live handler returns this dict.
    """
    import json

    fixture = (
        Path(__file__).resolve().parents[1]
        / "contracts"
        / "openclank"
        / "acp-session-discard-ack-v1.json"
    )
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_engine_discard_wire_ack_satisfies_host_predicate(tmp_path):
    """Non-mocked cross-check of the engine wire result against the host predicate.

    The engine extMethod test locks the live handler to the shared fixture;
    this test feeds that exact dict through the real
    ``ACPClient.discard_session`` acknowledgement predicate (only the JSON-RPC
    transport is stubbed). Mocking ``discard_session`` itself would hide the
    wire break this proves closed.
    """
    from src.openclank.acp_client import ACPClient

    engine_wire = _engine_discard_wire_ack()
    assert engine_wire == {"deleted": True}

    client = ACPClient.__new__(ACPClient)
    calls = []

    async def send(method, params):
        calls.append((method, params))
        return engine_wire

    client._send_request = send
    # Real host predicate: raises unless the engine wire result is exactly
    # {"deleted": True}.
    asyncio.run(client.discard_session("ses_engine_candidate", str(tmp_path.resolve())))
    assert calls == [(
        "_odysseus/session/discard",
        {"sessionId": "ses_engine_candidate", "cwd": str(tmp_path.resolve())},
    )]


def test_private_discard_sends_canonical_candidate_cwd(tmp_path):
    from src.openclank.acp_client import ACPClient

    client = ACPClient.__new__(ACPClient)
    calls = []

    async def send(method, params):
        calls.append((method, params))
        return _engine_discard_wire_ack()

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


def test_ordinary_restart_recovers_staged_orphan_without_remap(sqlite_projection, tmp_path):
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
            raise AssertionError("recovery must not resume before repair")

        async def discard_session(self, session_id, cwd):
            discarded.append((session_id, cwd))

        async def reserve_session(self):
            sid = getattr(self, "session_id", None) or "ses_reserved_test"
            return {"sessionID": sid, "provisional": True}

        async def new_session(self, *_args, provisional_session_id=None, **_kwargs):
            raise AssertionError("recovery must not create a session")

    bridge = ACPBridge.__new__(ACPBridge)
    bridge._client = Client()
    bridge._cwd = str(tmp_path.resolve())
    bridge._owner = "alice"
    bridge._durable_session_map = mapping
    bridge._session_map = {"chat-a": "engine-old"}
    bridge._session_admission_locks = {}
    bridge._session_context_locks = {}
    bridge._pending_session_results = {"engine-candidate": {"sessionId": "engine-candidate"}}
    bridge._session_models = {"engine-candidate": ["stale"]}
    bridge._session_state = {"engine-candidate": {"stale": True}}
    bridge._session_context = {"engine-candidate": {"stale": True}}
    bridge._turns = {"engine-candidate": object()}
    bridge._queues = {"engine-candidate": object()}
    bridge.cleanup_session = lambda _session_id: asyncio.sleep(0)

    recovered = asyncio.run(bridge.recover_admission_orphans())
    assert recovered == ["chat-a"]
    assert discarded == [("engine-candidate", orphan["physicalCwd"])]
    # Candidate-only caches are cleaned exactly for the discarded engine.
    assert bridge._pending_session_results == {}
    assert bridge._session_models == {}
    assert bridge._session_state == {}
    assert bridge._session_context == {}
    assert bridge._turns == {}
    assert bridge._queues == {}
    binding = projection.get_managed_binding("chat-a", owner="alice")
    assert binding["engineSessionID"] == "engine-old"
    assert binding["mappingRevision"] == 1
    assert binding["engineAliases"] == []


def test_async_chat_admission_lock_double_cancel_releases_real_filesystem_lock(tmp_path):
    from src.openclank.session_map import OwnerSessionMap

    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")

    async def exercise():
        entered = asyncio.Event()
        task = asyncio.create_task(_hold_admission(mapping, entered))
        await entered.wait()
        task.cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def _hold_admission(owner_map, entered):
        async with owner_map.async_chat_admission_lock("chat-a"):
            entered.set()
            await asyncio.sleep(60)

    asyncio.run(exercise())
    # The real filesystem lock must be free: a non-blocking acquire succeeds.
    with mapping.chat_admission_lock("chat-a"):
        pass


def test_resume_bad_descriptor_does_not_discard_or_delete_mapping(sqlite_projection, tmp_path):
    from src.openclank.acp_bridge import ACPBridge
    from src.openclank.acp_client import RPCError

    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    mapping.bind("chat-a", "engine-old", expected_current=None, expected_mapping_revision=0)
    projection.save_managed_binding(
        "chat-a", _binding(engine="engine-old"), owner="alice",
        expected_workspace_revision=0, expected_engine_session_id=None,
        expected_map_revision=0, expected_mapping_revision=0,
    )
    discarded = []

    class Client:
        async def resume_session(self, session_id, *_args, **_kwargs):
            # Bad descriptor is distinct from OPENCLANK_SESSION_MISSING.
            raise RPCError(-32602, "malformed session descriptor", {"code": "INVALID_PARAMS"})

        async def discard_session(self, session_id, cwd):
            discarded.append((session_id, cwd))

        async def reserve_session(self):
            sid = getattr(self, "session_id", None) or "ses_reserved_test"
            return {"sessionID": sid, "provisional": True}

        async def new_session(self, *_args, provisional_session_id=None, **_kwargs):
            raise AssertionError("bad descriptor must not silently create")

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

    with pytest.raises(RPCError):
        asyncio.run(bridge.ensure_session("chat-a", authority_workspace_id="workspace:chat-a"))
    assert discarded == []
    assert mapping.lookup("chat-a")["current"] == "engine-old"
    assert projection.get_managed_binding("chat-a", owner="alice")["engineSessionID"] == "engine-old"


def test_create_failed_discard_never_deletes_winning_mapping(sqlite_projection, tmp_path, monkeypatch):
    from src.openclank.acp_bridge import ACPBridge

    monkeypatch.setattr("src.openclank.acp_bridge.chat_workspace", lambda: "memory:chat-a")
    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    discarded = []

    class Client:
        async def reserve_session(self):
            return {"sessionID": "engine-candidate", "provisional": True}

        async def new_session(self, *_args, provisional_session_id=None, **_kwargs):
            return {
                "sessionId": provisional_session_id or "engine-candidate",
                "models": {},
            }

        async def resume_session(self, session_id, *_args, **_kwargs):
            if session_id == "engine-candidate":
                raise RuntimeError("create failed after engine session existed")
            return {"models": {}}

        async def discard_session(self, session_id, cwd):
            discarded.append((session_id, cwd))

    bridge = ACPBridge.__new__(ACPBridge)
    bridge._client = Client()
    bridge._cwd = str(tmp_path.resolve())
    bridge._owner = "alice"
    bridge._durable_session_map = mapping
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

    with pytest.raises(RuntimeError, match="create failed"):
        asyncio.run(bridge.ensure_session("chat-a", cwd=str(tmp_path.resolve()), authority_workspace_id="workspace:chat-a"))
    # Failed create discards only the unexposed candidate.  No winning chat
    # mapping exists yet, and none may be invented or deleted as a fallback.
    assert discarded == [("engine-candidate", str(tmp_path.resolve()))]
    assert mapping.flat_current() == {}
    assert projection.get_managed_binding("chat-a", owner="alice") == {}


def _two_process_ensure_worker(
    db_path: str,
    map_path: str,
    engine_id: str,
    cwd: str,
    chat_id: str,
    barrier: "multiprocessing.synchronize.Barrier",
    start: "multiprocessing.synchronize.Event",
    result_queue: "multiprocessing.Queue",
    discard_queue: "multiprocessing.Queue",
    missing_old: bool,
):
    """Production-path ensure_session race worker (one real OS process).

    Uses the real ``ACPBridge`` constructor and ``ensure_session`` entry
    point.  Only the engine RPC surface is stubbed, because no live mimo
    child is available in the test harness.
    """
    import asyncio as _asyncio
    from pathlib import Path as _Path

    from sqlalchemy import create_engine as _create_engine
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    import core.database as _database
    import src.openclank.acp_bridge as _acp_bridge
    import src.openclank.transcript_projection as _projection
    from src.openclank.acp_client import RPCError
    from src.openclank.acp_bridge import ACPBridge

    engine = _create_engine(f"sqlite:///{db_path}")
    sessions = _sessionmaker(bind=engine)
    _database.SessionLocal = sessions
    _projection.SessionLocal = sessions
    _acp_bridge.chat_workspace = lambda: "memory:chat-a"

    db = sessions()
    row = _database.Session(
        id=chat_id,
        name=chat_id,
        endpoint_url="mimo://acp",
        model="mimo",
        owner="alice",
        mimo_state={},
    )
    db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
    db.close()

    class Client:
        def __init__(self):
            self.callbacks = {}
            self.discarded = []

        def register_callback(self, method, handler):
            self.callbacks[method] = handler

        def on_session_update(self, *_args):
            pass

        async def reserve_session(self):
            return {"sessionID": engine_id, "provisional": True}

        async def new_session(self, cwd_arg, mcp_servers=None, provisional_session_id=None):
            # True barrier: both processes create an engine session before
            # either publishes its map binding.
            barrier.wait(timeout=20)
            return {
                "sessionId": provisional_session_id or engine_id,
                "models": {"availableModels": []},
            }

        async def resume_session(self, session_id, cwd_arg, mcp_servers=None):
            if missing_old and session_id == "engine-old":
                raise RPCError(404, "missing", {"code": "OPENCLANK_SESSION_MISSING"})
            return {"models": {"availableModels": []}}

        async def discard_session(self, session_id, cwd_arg):
            self.discarded.append((session_id, cwd_arg))
            discard_queue.put((engine_id, session_id, cwd_arg))

        async def set_session_config_option(self, *_args, **_kwargs):
            return {}

        async def prompt(self, *_args, **_kwargs):
            return {"stopReason": "end_turn", "usage": {}}

    client = Client()
    bridge = ACPBridge(
        client,
        cwd,
        owner="alice",
        session_map_path=_Path(map_path),
    )
    start.wait(timeout=20)
    try:
        winner = _asyncio.run(
            bridge.ensure_session(
                chat_id,
                cwd=cwd,
                authority_workspace_id="workspace:chat-a",
            )
        )
        result_queue.put((engine_id, "ok", winner))
    except Exception as exc:
        result_queue.put((engine_id, "err", f"{type(exc).__name__}: {exc}"))


def test_two_process_ensure_session_fresh_race_has_one_winner(tmp_path):
    """Two real OS processes race production ensure_session on one chat.

    Helper-only map tests are insufficient: this drives the real
    ``ACPBridge.ensure_session`` admission path end-to-end across processes.
    """
    ctx = multiprocessing.get_context("spawn")
    db_path = str(tmp_path / "shared.db")
    map_path = str(tmp_path / "session-map.json")
    cwd = str(tmp_path.resolve())
    barrier = ctx.Barrier(2)
    start = ctx.Event()
    result_queue = ctx.Queue()
    discard_queue = ctx.Queue()

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    workers = [
        ctx.Process(
            target=_two_process_ensure_worker,
            args=(db_path, map_path, "engine-c1", cwd, "chat-a", barrier, start, result_queue, discard_queue, False),
        ),
        ctx.Process(
            target=_two_process_ensure_worker,
            args=(db_path, map_path, "engine-c2", cwd, "chat-a", barrier, start, result_queue, discard_queue, False),
        ),
    ]
    for worker in workers:
        worker.start()
    start.set()
    rows = [result_queue.get(timeout=30) for _ in range(2)]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert all(row[1] == "ok" for row in rows), rows
    winners = {row[2] for row in rows}
    assert len(winners) == 1, rows
    winner = winners.pop()
    assert winner in {"engine-c1", "engine-c2"}

    mapping = OwnerSessionMap(Path(map_path), "alice")
    assert mapping.lookup("chat-a")["current"] == winner

    # Exactly one candidate is discarded — the loser — and never the winner.
    discards = []
    while not discard_queue.empty():
        discards.append(discard_queue.get_nowait())
    assert len(discards) == 1, discards
    discarded_id = discards[0][1]
    assert discarded_id != winner
    assert discarded_id in {"engine-c1", "engine-c2"}


def test_two_process_ensure_session_missing_remap_race_has_one_winner(tmp_path):
    """Two real OS processes race the missing-session remap admission path."""
    ctx = multiprocessing.get_context("spawn")
    db_path = str(tmp_path / "shared.db")
    map_path = str(tmp_path / "session-map.json")
    cwd = str(tmp_path.resolve())
    barrier = ctx.Barrier(2)
    start = ctx.Event()
    result_queue = ctx.Queue()
    discard_queue = ctx.Queue()

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    mapping = OwnerSessionMap(Path(map_path), "alice")
    mapping.bind("chat-a", "engine-old", expected_current=None, expected_mapping_revision=0)
    projection.SessionLocal = sessionmaker(bind=create_engine(f"sqlite:///{db_path}"))
    # Seed the old binding via the projection helper on the shared database.
    from core.database import Session as _Session

    db = projection.SessionLocal()
    if db.get(_Session, "chat-a") is None:
        db.add(_Session(
            id="chat-a",
            name="chat-a",
            endpoint_url="mimo://acp",
            model="mimo",
            owner="alice",
            mimo_state={},
        ))
        db.commit()
    db.close()
    projection.save_managed_binding(
        "chat-a",
        _binding(engine="engine-old", map_revision=1, mapping_revision=1),
        owner="alice",
        expected_workspace_revision=0,
        expected_engine_session_id=None,
        expected_map_revision=0,
        expected_mapping_revision=0,
    )

    workers = [
        ctx.Process(
            target=_two_process_ensure_worker,
            args=(db_path, map_path, "engine-r1", cwd, "chat-a", barrier, start, result_queue, discard_queue, True),
        ),
        ctx.Process(
            target=_two_process_ensure_worker,
            args=(db_path, map_path, "engine-r2", cwd, "chat-a", barrier, start, result_queue, discard_queue, True),
        ),
    ]
    for worker in workers:
        worker.start()
    start.set()
    rows = [result_queue.get(timeout=30) for _ in range(2)]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert all(row[1] == "ok" for row in rows), rows
    winners = {row[2] for row in rows}
    assert len(winners) == 1, rows
    winner = winners.pop()
    assert winner in {"engine-r1", "engine-r2"}

    mapping2 = OwnerSessionMap(Path(map_path), "alice")
    entry = mapping2.lookup("chat-a")
    assert entry["current"] == winner
    assert "engine-old" in entry["aliases"]

    discards = []
    while not discard_queue.empty():
        discards.append(discard_queue.get_nowait())
    # The old engine is never the winner of the remap; the loser candidate is
    # discarded exactly once and the winning mapping is never deleted.
    discarded_ids = {row[1] for row in discards}
    assert winner not in discarded_ids
    assert len(discards) == 1, discards

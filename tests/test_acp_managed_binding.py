import pytest
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as database
import src.openclank.transcript_projection as projection
from core.database import Base, Session


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


def _binding(*, engine="engine-a", map_revision=1, mapping_revision=1):
    return {
        "owner": "alice",
        "stableChatID": "chat-a",
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

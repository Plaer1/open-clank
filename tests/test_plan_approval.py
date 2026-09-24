import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.plan_approval as plan_approval
import src.openclank.transcript_projection as projection
import core.database as database
from core.database import Base, Session


def test_draft_is_not_executable(monkeypatch):
    monkeypatch.setattr(plan_approval, "get_mimo_state", lambda *_args, **_kw: {
        "plan_state": {"plan": "draft", "revision": 3, "digest": "a" * 64,
                        "approved_revision": None, "approved_digest": None}
    })
    assert plan_approval.approved_plan_state("s", "alice") == {}


def test_approval_is_revision_and_digest_bound(monkeypatch):
    state = {"plan_state": {"plan": "draft", "revision": 3, "digest": "a" * 64}}
    saved = {}
    monkeypatch.setattr(plan_approval, "get_mimo_state", lambda *_args, **_kw: dict(state))
    monkeypatch.setattr(plan_approval, "save_mimo_state", lambda _sid, value, **_kw: saved.update(value) or value)

    with pytest.raises(ValueError):
        plan_approval.approve_plan("s", "alice", revision=2, digest="a" * 64)
    approved = plan_approval.approve_plan("s", "alice", revision=3, digest="a" * 64)
    assert approved["approved_revision"] == 3
    assert approved["approved_digest"] == "a" * 64
    assert approved["status"] == "approved"


def test_approved_plan_requires_digest_match(monkeypatch):
    monkeypatch.setattr(plan_approval, "get_mimo_state", lambda *_args, **_kw: {
        "plan_state": {"plan": "draft", "revision": 3, "digest": "a" * 64,
                        "approved_revision": 3, "approved_digest": "b" * 64}
    })
    assert plan_approval.approved_plan_state("s", "alice") == {}


def test_new_draft_revokes_previous_approval(monkeypatch):
    state = {"plan_state": {
        "plan": "old", "revision": 2, "digest": "b" * 64,
        "approved_revision": 2, "approved_digest": "b" * 64,
    }}
    monkeypatch.setattr(plan_approval, "get_mimo_state", lambda *_args, **_kw: state)
    monkeypatch.setattr(plan_approval, "save_mimo_state", lambda _sid, value, **_kw: state.update(value) or value)
    draft = plan_approval.save_plan_draft("s", "alice", "new")
    assert draft["status"] == "draft"
    assert draft["approved_revision"] is None
    assert plan_approval.approved_plan_state("s", "alice") == {}


def test_artifact_path_binds_once(monkeypatch):
    state = {"plan_state": {"plan": "x", "revision": 1, "digest": "a" * 64,
                             "approved_revision": 1, "approved_digest": "a" * 64}}
    monkeypatch.setattr(plan_approval, "get_mimo_state", lambda *_args, **_kw: state)
    monkeypatch.setattr(plan_approval, "save_mimo_state", lambda _sid, value, **_kw: state.update(value) or value)
    assert plan_approval.bind_artifact_path("s", "alice", ".futures/one.md") == ".clanker/futures/one.md"
    assert plan_approval.bind_artifact_path("s", "alice", ".futures/two.md") == ".clanker/futures/one.md"
    assert state["plan_state"]["artifact_path_events"][0]["kind"] == "legacy_path_migration"


def test_artifact_path_rejects_typo_and_traversal(monkeypatch):
    state = {"plan_state": {"plan": "x", "revision": 1, "digest": "a" * 64}}
    monkeypatch.setattr(plan_approval, "get_mimo_state", lambda *_args, **_kw: state)
    monkeypatch.setattr(plan_approval, "save_mimo_state", lambda _sid, value, **_kw: state.update(value) or value)
    with pytest.raises(ValueError):
        plan_approval.bind_artifact_path("s", "alice", ".clankers/futures/one.md")
    with pytest.raises(ValueError):
        plan_approval.bind_artifact_path("s", "alice", ".futures/../one.md")


def test_real_plan_save_preserves_newer_managed_binding(tmp_path, monkeypatch):
    """A stale plan snapshot cannot erase a binding committed by the host."""
    engine = create_engine(f"sqlite:///{tmp_path / 'plan-binding.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(projection, "SessionLocal", sessions)
    db = sessions()
    db.add(Session(
        id="chat-plan",
        name="chat",
        endpoint_url="http://example.test",
        model="model",
        owner="alice",
        mimo_state={
            "plan_state": {
                "plan": "draft",
                "revision": 1,
                "digest": "a" * 64,
                "approved_revision": None,
                "approved_digest": None,
                "status": "draft",
            }
        },
    ))
    db.commit()
    db.close()

    binding = {
        "owner": "alice",
        "stableChatID": "chat-plan",
        "engineSessionID": "engine-new",
        "engineAliases": [],
        "memoryWorkspaceID": "memory:chat-plan",
        "authorityWorkspaceID": "workspace:chat-plan",
        "copalWorkspace": "default",
        "physicalCwd": str(tmp_path.resolve()),
        "workspaceRevision": 0,
        "mapRevision": 1,
        "mappingRevision": 1,
        "memoryEnabled": True,
        "transition": None,
    }
    projection.save_managed_binding(
        "chat-plan",
        binding,
        owner="alice",
        expected_workspace_revision=0,
        expected_engine_session_id=None,
        expected_map_revision=0,
        expected_mapping_revision=0,
    )
    original_save = plan_approval.save_mimo_state
    raced = False

    def save_with_binding_race(session_id, state, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            projection.save_managed_binding(
                "chat-plan",
                {**binding, "engineSessionID": "engine-newer", "mapRevision": 2, "mappingRevision": 2},
                owner="alice",
                expected_workspace_revision=0,
                expected_engine_session_id="engine-new",
                expected_map_revision=1,
                expected_mapping_revision=1,
            )
        return original_save(session_id, state, **kwargs)

    monkeypatch.setattr(plan_approval, "save_mimo_state", save_with_binding_race)
    saved = plan_approval.save_plan_draft("chat-plan", "alice", "new draft")
    assert saved["status"] == "draft"
    assert projection.get_managed_binding("chat-plan", owner="alice")["engineSessionID"] == "engine-newer"

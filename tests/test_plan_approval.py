import pytest

import src.plan_approval as plan_approval


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

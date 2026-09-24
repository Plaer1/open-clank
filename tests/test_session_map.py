import json

import pytest

from src.openclank.session_map import OwnerSessionMap, SessionMapCollision


def test_v1_upgrade_alias_bound_and_atomic_owner_map(tmp_path):
    path = tmp_path / "session-map.json"
    path.write_text(json.dumps({"chat": "engine-0"}))
    mapping = OwnerSessionMap(path, "alice")

    assert mapping.flat_current() == {"chat": "engine-0"}
    assert mapping.bind("chat", "engine-1")["aliases"] == ["engine-0"]
    assert json.loads(path.read_text())["version"] == 2

    for index in range(2, 20):
        mapping.bind("chat", f"engine-{index}")
    entry = mapping.lookup("chat")
    assert entry is not None
    assert entry["aliases"] == [f"engine-{index}" for index in range(18, 2, -1)]
    assert len(entry["aliases"]) == 16


def test_owner_wide_inverse_collision_quarantines_all_chats(tmp_path):
    path = tmp_path / "session-map.json"
    path.write_text(json.dumps({
        "version": 2,
        "mapRevision": 1,
        "chats": {
            "one": {"owner": "alice", "current": "same", "aliases": [], "revision": 0},
            "two": {"owner": "alice", "current": "other", "aliases": ["same"], "revision": 0},
        },
    }))
    mapping = OwnerSessionMap(path, "alice")
    assert mapping.flat_current() == {}
    with pytest.raises(SessionMapCollision):
        mapping.lookup("one")
    with pytest.raises(SessionMapCollision):
        mapping.bind("three", "new")


def test_forget_removes_current_and_aliases(tmp_path):
    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    mapping.bind("chat", "engine-1")
    mapping.bind("chat", "engine-2")
    mapping.forget("chat")
    assert mapping.flat_current() == {}


def test_bind_replace_forget_are_single_locked_compare_and_swap(tmp_path):
    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")

    first = mapping.bind(
        "chat",
        "engine-1",
        expected_current=None,
        expected_mapping_revision=0,
    )
    assert first["mapRevision"] == 1
    assert first["mappingRevision"] == 1

    with pytest.raises(ValueError, match="current-session conflict"):
        mapping.bind(
            "chat",
            "engine-loser",
            expected_current="engine-stale",
            expected_mapping_revision=1,
        )
    with pytest.raises(ValueError, match="mapping revision conflict"):
        mapping.bind(
            "chat",
            "engine-loser",
            expected_current="engine-1",
            expected_mapping_revision=0,
        )

    second = mapping.bind(
        "chat",
        "engine-2",
        expected_current="engine-1",
        expected_mapping_revision=1,
    )
    assert second["mapRevision"] == 2
    assert second["mappingRevision"] == 2

    with pytest.raises(ValueError, match="current-session conflict"):
        mapping.forget(
            "chat",
            expected_current="engine-1",
            expected_mapping_revision=2,
        )
    mapping.forget(
        "chat",
        expected_current="engine-2",
        expected_mapping_revision=2,
    )
    assert mapping.flat_current() == {}


def test_post_replace_directory_fsync_ambiguity_reconciles_exact_candidate(tmp_path, monkeypatch):
    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    original_write = mapping._write

    def replace_then_report_failure(data):
        original_write(data)
        raise OSError("directory fsync outcome is ambiguous")

    monkeypatch.setattr(mapping, "_write", replace_then_report_failure)
    result = mapping.bind(
        "chat",
        "engine-1",
        expected_current=None,
        expected_mapping_revision=0,
    )
    assert result["current"] == "engine-1"
    assert mapping.lookup("chat")["current"] == "engine-1"

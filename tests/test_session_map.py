import json
import multiprocessing
import os
import queue

import pytest

from src.openclank.session_map import OwnerSessionMap, SessionMapCollision


def _admission_lock_process(path, entered, release):
    mapping = OwnerSessionMap(path, "alice")
    with mapping.chat_admission_lock("chat"):
        entered.put("entered")
        release.wait(10)


def _bind_process(path, owner, engine, start, result, chat="chat", ready=None):
    mapping = OwnerSessionMap(path, owner)
    current = mapping.lookup(chat)
    expected_map_revision = mapping.map_revision()
    expected_current = current["current"] if current else None
    expected_mapping_revision = int(current.get("revision") or 0) if current else 0
    if ready is not None:
        ready.put(engine)
    start.wait(10)
    try:
        result.put((engine, "ok", mapping.bind(
            chat,
            engine,
            expected_map_revision=expected_map_revision,
            expected_current=expected_current,
            expected_mapping_revision=expected_mapping_revision,
        )))
    except Exception as exc:  # pragma: no cover - asserted by parent
        result.put((engine, type(exc).__name__, str(exc)))


def _forget_process(path, owner, expected_engine, start, result):
    start.wait(10)
    mapping = OwnerSessionMap(path, owner)
    try:
        entry = mapping.lookup("chat")
        result.put(("forget", "ok", mapping.forget(
            "chat",
            expected_map_revision=mapping.map_revision(),
            expected_current=expected_engine,
            expected_mapping_revision=int(entry.get("revision") or 0) if entry else 0,
        )))
    except Exception as exc:  # pragma: no cover - asserted by parent
        result.put(("forget", type(exc).__name__, str(exc)))


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


def test_empty_v1_map_upgrades_on_first_mutation(tmp_path):
    path = tmp_path / "session-map.json"
    path.write_text("{}")
    mapping = OwnerSessionMap(path, "alice")
    assert mapping.flat_current() == {}
    mapping.bind("chat", "engine-1", expected_map_revision=0, expected_current=None, expected_mapping_revision=0)
    assert json.loads(path.read_text())["version"] == 2


def test_two_real_processes_have_one_fresh_bind_winner(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    path = tmp_path / "session-map.json"
    start = ctx.Event()
    result = ctx.Queue()
    ready = ctx.Queue()
    workers = [ctx.Process(target=_bind_process, args=(path, "alice", engine, start, result, "chat", ready))
               for engine in ("engine-a", "engine-b")]
    for worker in workers:
        worker.start()
    [ready.get(timeout=10) for _ in workers]
    start.set()
    rows = [result.get(timeout=10) for _ in workers]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    assert sorted(row[1] for row in rows) == ["ValueError", "ok"]
    assert OwnerSessionMap(path, "alice").lookup("chat")["current"] in {"engine-a", "engine-b"}


def test_two_real_processes_have_one_remap_winner_and_stale_forget_loses(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    path = tmp_path / "session-map.json"
    mapping = OwnerSessionMap(path, "alice")
    mapping.bind("chat", "engine-0", expected_map_revision=0, expected_current=None, expected_mapping_revision=0)
    start = ctx.Event()
    result = ctx.Queue()
    ready = ctx.Queue()
    workers = [ctx.Process(target=_bind_process, args=(path, "alice", engine, start, result, "chat", ready))
               for engine in ("engine-a", "engine-b")]
    for worker in workers:
        worker.start()
    [ready.get(timeout=10) for _ in workers]
    start.set()
    rows = [result.get(timeout=10) for _ in workers]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    assert sorted(row[1] for row in rows) == ["ValueError", "ok"]
    winner = OwnerSessionMap(path, "alice").lookup("chat")["current"]
    stale_start = ctx.Event()
    stale_result = ctx.Queue()
    stale = ctx.Process(target=_forget_process, args=(path, "alice", "engine-0", stale_start, stale_result))
    stale.start()
    stale_start.set()
    row = stale_result.get(timeout=10)
    stale.join(timeout=10)
    assert stale.exitcode == 0
    assert row[1] == "ValueError"
    assert OwnerSessionMap(path, "alice").lookup("chat")["current"] == winner


def test_two_real_processes_can_mutate_different_chats_without_global_revision_staleness(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    path = tmp_path / "session-map.json"
    start = ctx.Event()
    result = ctx.Queue()
    ready = ctx.Queue()
    workers = [
        ctx.Process(target=_bind_process, args=(path, "alice", "engine-a", start, result, "chat-a", ready)),
        ctx.Process(target=_bind_process, args=(path, "alice", "engine-b", start, result, "chat-b", ready)),
    ]
    for worker in workers:
        worker.start()
    [ready.get(timeout=10) for _ in workers]
    start.set()
    rows = [result.get(timeout=10) for _ in workers]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    assert sorted(row[1] for row in rows) == ["ValueError", "ok"]
    mapping = OwnerSessionMap(path, "alice")
    loser_chat = "chat-a" if mapping.lookup("chat-a") is None else "chat-b"
    loser_engine = "engine-a" if loser_chat == "chat-a" else "engine-b"
    mapping.bind(
        loser_chat,
        loser_engine,
        expected_map_revision=mapping.map_revision(),
        expected_current=None,
        expected_mapping_revision=0,
    )
    assert mapping.lookup("chat-a")["current"] == "engine-a"
    assert mapping.lookup("chat-b")["current"] == "engine-b"


def test_two_process_admission_lock_serializes_same_chat(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    path = tmp_path / "session-map.json"
    entered = ctx.Queue()
    release = ctx.Event()
    first = ctx.Process(target=_admission_lock_process, args=(path, entered, release))
    second = ctx.Process(target=_admission_lock_process, args=(path, entered, release))
    first.start()
    assert entered.get(timeout=10) == "entered"
    second.start()
    with pytest.raises(queue.Empty):
        entered.get(timeout=0.25)
    release.set()
    assert entered.get(timeout=10) == "entered"
    first.join(timeout=10)
    second.join(timeout=10)
    assert first.exitcode == 0
    assert second.exitcode == 0


def test_post_replace_directory_fsync_ambiguity_does_not_accept_different_candidate(tmp_path, monkeypatch):
    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    original_write = mapping._write

    def replace_different_then_report_failure(data):
        changed = dict(data)
        changed["chats"] = {}
        changed["chats"]["other"] = {
            "owner": "alice", "current": "different", "aliases": [], "revision": 1,
        }
        changed["mapRevision"] = int(data.get("mapRevision") or 0) + 1
        original_write(changed)
        raise OSError("directory fsync outcome is ambiguous")

    monkeypatch.setattr(mapping, "_write", replace_different_then_report_failure)
    with pytest.raises(OSError):
        mapping.bind("chat", "engine-1", expected_map_revision=0, expected_current=None, expected_mapping_revision=0)
    assert mapping.lookup("chat") is None
    assert mapping.lookup("other")["current"] == "different"


def test_lock_rejects_symlink(tmp_path):
    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    target = tmp_path / "target.lock"
    target.write_text("")
    mapping.lock_path.symlink_to(target)
    with pytest.raises(OSError):
        mapping.read()


@pytest.mark.parametrize("chat", ["", " chat", "chat ", 7, None])
def test_public_identity_operations_reject_noncanonical_chat_ids(tmp_path, chat):
    mapping = OwnerSessionMap(tmp_path / "session-map.json", "alice")
    with pytest.raises(ValueError):
        mapping.lookup(chat)
    with pytest.raises(ValueError):
        mapping.revisions(chat)
    with pytest.raises(ValueError):
        mapping.fence(chat)


def test_quarantine_rejects_whitespace_and_duplicate_identity_entries(tmp_path):
    path = tmp_path / "session-map.json"
    path.write_text(json.dumps({"version": 2, "mapRevision": 0, "quarantine": [" chat"], "chats": {}}))
    with pytest.raises(ValueError):
        OwnerSessionMap(path, "alice").read()
    path.write_text(json.dumps({"version": 2, "mapRevision": 0, "quarantine": ["chat", "chat"], "chats": {}}))
    with pytest.raises(ValueError):
        OwnerSessionMap(path, "alice").read()

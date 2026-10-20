import copy
import json
import os
import sqlite3

import pytest

import src.openclank.mimo_supervisor as mimo_supervisor
from src.openclank.mimo_supervisor import MimoSupervisorPool


def _pool(tmp_path):
    return MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)


def _seed_owner(pool, owner, *, body="private runtime", permission="read"):
    runtime = pool._runtime_home(owner)
    (runtime / "mimocode" / "data").mkdir(parents=True)
    (runtime / "mimocode" / "data" / "state.json").write_text(body, encoding="utf-8")
    pool._grant_store.add(permission, "/private/path", owner=owner)
    pool._grant_store.remove(permission, "/private/path", owner=owner)
    pool._grant_store.add("write", "/active/private", owner=owner)
    return runtime


@pytest.mark.asyncio
async def test_mimo_owner_rename_is_content_free_replay_safe_and_tenant_stable(tmp_path):
    pool = _pool(tmp_path)
    old_runtime = _seed_owner(pool, "alice", body="ALICE SECRET")
    _seed_owner(pool, "bob", body="BOB SECRET", permission="execute")
    bob_before = pool.owner_lifecycle_inventory("bob")
    manifest = await pool.preview_owner_rename("alice", "alice2")

    receipt = await pool.reconcile_owner_rename("alice", "alice2", manifest)

    assert receipt["state"] == "applied"
    assert receipt["source"]["count"] == 0
    assert receipt["target"]["permission_grants"]["count"] == 2
    assert not old_runtime.exists()
    assert (pool._runtime_home("alice2") / "mimocode/data/state.json").read_text() == "ALICE SECRET"
    assert pool.owner_lifecycle_inventory("bob") == bob_before
    serialized = json.dumps(receipt)
    assert "ALICE SECRET" not in serialized
    assert "/private/path" not in serialized

    replay = await pool.reconcile_owner_rename("alice", "alice2", manifest)
    assert replay["state"] == "already_applied"
    assert replay["target"]["fingerprint"] == receipt["target"]["fingerprint"]


@pytest.mark.asyncio
async def test_mimo_owner_rename_recovers_component_partial_failure(tmp_path, monkeypatch):
    pool = _pool(tmp_path)
    old_runtime = _seed_owner(pool, "alice")
    new_runtime = pool._runtime_home("alice2")
    manifest = await pool.preview_owner_rename("alice", "alice2")
    original_replace = os.replace
    failed = False

    def fail_runtime_once(source, target):
        nonlocal failed
        if not failed and str(source) == str(old_runtime) and str(target) == str(new_runtime):
            failed = True
            raise OSError("injected runtime move interruption")
        return original_replace(source, target)

    monkeypatch.setattr(mimo_supervisor.os, "replace", fail_runtime_once)
    with pytest.raises(OSError, match="injected"):
        await pool.reconcile_owner_rename("alice", "alice2", manifest)

    # The SQLite grant component committed, while the directory component did
    # not. A replay recognizes both positions rather than merging or guessing.
    assert pool._grant_store.owner_inventory("alice")["count"] == 0
    assert pool._grant_store.owner_inventory("alice2")["count"] == 2
    assert old_runtime.exists()
    assert not new_runtime.exists()

    receipt = await pool.reconcile_owner_rename("alice", "alice2", manifest)
    assert receipt["runtime"]["state"] == "applied"
    assert receipt["permission_grants"]["state"] == "already_applied"


@pytest.mark.asyncio
async def test_mimo_owner_rename_compensates_component_partial_failure(tmp_path, monkeypatch):
    pool = _pool(tmp_path)
    old_runtime = _seed_owner(pool, "alice")
    new_runtime = pool._runtime_home("alice2")
    from src.openclank.session_map import OwnerSessionMap

    OwnerSessionMap(old_runtime / "session-map.json", "alice").bind("chat-1", "ses_1")
    manifest = await pool.preview_owner_rename("alice", "alice2")
    original_replace = os.replace

    def fail_runtime(source, target):
        if str(source) == str(old_runtime) and str(target) == str(new_runtime):
            raise OSError("injected runtime move interruption")
        return original_replace(source, target)

    monkeypatch.setattr(mimo_supervisor.os, "replace", fail_runtime)
    with pytest.raises(OSError, match="injected"):
        await pool.reconcile_owner_rename("alice", "alice2", manifest)

    receipt = await pool.compensate_owner_rename("alice", "alice2", manifest)
    assert receipt["runtime"]["state"] == "already_compensated"
    assert receipt["permission_grants"]["state"] == "compensated"
    assert receipt["source"]["fingerprint"] == manifest["source"]["fingerprint"]
    assert receipt["target"]["count"] == 0
    assert OwnerSessionMap(old_runtime / "session-map.json", "alice").read()["chats"]["chat-1"]["owner"] == "alice"


@pytest.mark.asyncio
async def test_mimo_owner_rename_replays_after_move_before_map_rewrite(tmp_path, monkeypatch):
    pool = _pool(tmp_path)
    old_runtime = _seed_owner(pool, "alice")
    new_runtime = pool._runtime_home("alice2")
    from src.openclank.session_map import OwnerSessionMap

    OwnerSessionMap(old_runtime / "session-map.json", "alice").bind("chat-1", "ses_1")
    manifest = await pool.preview_owner_rename("alice", "alice2")
    original_replace = os.replace
    moved = False

    def fail_after_move(source, target):
        nonlocal moved
        if str(source) == str(old_runtime) and str(target) == str(new_runtime):
            moved = True
        elif moved and str(target) == str(new_runtime / "session-map.json"):
            raise OSError("injected map rewrite interruption")
        return original_replace(source, target)

    monkeypatch.setattr(mimo_supervisor.os, "replace", fail_after_move)
    with pytest.raises(OSError, match="map rewrite"):
        await pool.reconcile_owner_rename("alice", "alice2", manifest)
    assert new_runtime.exists()

    monkeypatch.setattr(mimo_supervisor.os, "replace", original_replace)
    receipt = await pool.reconcile_owner_rename("alice", "alice2", manifest)
    assert receipt["runtime"]["state"] == "already_applied"
    assert OwnerSessionMap(new_runtime / "session-map.json", "alice2").read()["chats"]["chat-1"]["owner"] == "alice2"


@pytest.mark.asyncio
async def test_mimo_owner_rename_rejects_tampered_runtime_same_shape(tmp_path):
    pool = _pool(tmp_path)
    old_runtime = _seed_owner(pool, "alice")
    manifest = await pool.preview_owner_rename("alice", "alice2")
    (old_runtime / "mimocode" / "data" / "state.json").write_text(
        "tampered", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="runtime state changed"):
        await pool.reconcile_owner_rename("alice", "alice2", manifest)
    assert old_runtime.exists()
    assert not pool._runtime_home("alice2").exists()


@pytest.mark.asyncio
async def test_mimo_owner_compensation_replays_after_map_rewrite_interrupt(tmp_path, monkeypatch):
    pool = _pool(tmp_path)
    old_runtime = _seed_owner(pool, "alice")
    new_runtime = pool._runtime_home("alice2")
    from src.openclank.session_map import OwnerSessionMap

    OwnerSessionMap(old_runtime / "session-map.json", "alice").bind("chat-1", "ses_1")
    manifest = await pool.preview_owner_rename("alice", "alice2")
    await pool.reconcile_owner_rename("alice", "alice2", manifest)
    original_replace = os.replace
    moved_back = False

    def fail_after_compensation_move(source, target):
        nonlocal moved_back
        if str(source) == str(new_runtime) and str(target) == str(old_runtime):
            moved_back = True
        elif moved_back and str(target) == str(old_runtime / "session-map.json"):
            raise OSError("injected compensation map interruption")
        return original_replace(source, target)

    monkeypatch.setattr(mimo_supervisor.os, "replace", fail_after_compensation_move)
    with pytest.raises(OSError, match="compensation map"):
        await pool.compensate_owner_rename("alice", "alice2", manifest)
    assert old_runtime.exists()

    monkeypatch.setattr(mimo_supervisor.os, "replace", original_replace)
    receipt = await pool.compensate_owner_rename("alice", "alice2", manifest)
    assert receipt["runtime"]["state"] == "compensated"
    assert receipt["source"]["fingerprint"] == manifest["source"]["fingerprint"]
    assert OwnerSessionMap(old_runtime / "session-map.json", "alice").read()["chats"]["chat-1"]["owner"] == "alice"


@pytest.mark.asyncio
async def test_mimo_owner_compensation_after_rewritten_map_restores_original(tmp_path):
    pool = _pool(tmp_path)
    old_runtime = _seed_owner(pool, "alice")
    new_runtime = pool._runtime_home("alice2")
    from src.openclank.session_map import OwnerSessionMap

    OwnerSessionMap(old_runtime / "session-map.json", "alice").bind("chat-1", "ses_1")
    manifest = await pool.preview_owner_rename("alice", "alice2")
    await pool.reconcile_owner_rename("alice", "alice2", manifest)
    receipt = await pool.compensate_owner_rename("alice", "alice2", manifest)
    assert receipt["runtime"]["state"] == "compensated"
    assert receipt["source"]["fingerprint"] == manifest["source"]["fingerprint"]
    assert not new_runtime.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_map", [{"chat-1": "ses_1"}, {}])
async def test_mimo_owner_rename_preserves_legacy_v1_map_bytes(tmp_path, legacy_map):
    pool = _pool(tmp_path)
    old_runtime = pool._runtime_home("alice")
    old_runtime.mkdir(parents=True)
    map_path = old_runtime / "session-map.json"
    map_path.write_text(json.dumps(legacy_map, sort_keys=True) + "\n", encoding="utf-8")
    original_bytes = map_path.read_bytes()

    manifest = await pool.preview_owner_rename("alice", "alice2")
    await pool.reconcile_owner_rename("alice", "alice2", manifest)
    new_map = pool._runtime_home("alice2") / "session-map.json"
    assert new_map.read_bytes() == original_bytes

    replay = await pool.reconcile_owner_rename("alice", "alice2", manifest)
    assert replay["runtime"]["state"] == "already_applied"
    await pool.compensate_owner_rename("alice", "alice2", manifest)
    assert map_path.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_mimo_preflight_and_reconcile_reject_target_or_stale_state(tmp_path):
    pool = _pool(tmp_path)
    _seed_owner(pool, "alice")
    _seed_owner(pool, "taken")
    alice_before = pool.owner_lifecycle_inventory("alice")
    taken_before = pool.owner_lifecycle_inventory("taken")
    with pytest.raises(RuntimeError, match="target Agent owner"):
        await pool.preview_owner_rename("alice", "taken")
    assert pool.owner_lifecycle_inventory("alice") == alice_before
    assert pool.owner_lifecycle_inventory("taken") == taken_before

    manifest = await pool.preview_owner_rename("alice", "alice2")
    pool._grant_store.add("execute", "/late", owner="alice")
    with pytest.raises(RuntimeError, match="permission-grant owner state changed"):
        await pool.reconcile_owner_rename("alice", "alice2", manifest)
    assert pool._runtime_home("alice").exists()
    assert not pool._runtime_home("alice2").exists()


@pytest.mark.asyncio
async def test_mimo_empty_runtime_partition_is_counted_and_blocks_username_reuse(tmp_path):
    pool = _pool(tmp_path)
    pool._runtime_home("taken").mkdir(parents=True)
    inventory = pool.owner_lifecycle_inventory("taken")
    assert inventory["count"] == 1
    assert inventory["runtime"]["present"] is True

    _seed_owner(pool, "alice")
    with pytest.raises(RuntimeError, match="target Agent owner"):
        await pool.preview_owner_rename("alice", "taken")


@pytest.mark.asyncio
async def test_mimo_purge_replays_after_physical_delete_interruption(tmp_path, monkeypatch):
    pool = _pool(tmp_path)
    _seed_owner(pool, "deleted:stable", body="DELETE ME")
    _seed_owner(pool, "bob", body="KEEP BOB")
    bob_before = pool.owner_lifecycle_inventory("bob")
    expected = pool.owner_lifecycle_inventory("deleted:stable")
    original_rmtree = mimo_supervisor.shutil.rmtree
    failed = False

    def fail_once(path):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected physical purge interruption")
        return original_rmtree(path)

    monkeypatch.setattr(mimo_supervisor.shutil, "rmtree", fail_once)
    with pytest.raises(OSError, match="injected"):
        await pool.purge_owner_lifecycle("deleted:stable", expected=expected)

    stage, _journal_root, journal = pool._owner_purge_paths("deleted:stable")
    assert stage.exists()
    assert journal.exists()
    assert pool._grant_store.owner_inventory("deleted:stable")["count"] == 0

    receipt = await pool.purge_owner_lifecycle("deleted:stable", expected=expected)
    assert receipt["physical_compaction"] is True
    assert not stage.exists()
    assert not journal.exists()
    assert pool.owner_lifecycle_inventory("deleted:stable")["count"] == 0
    assert pool.owner_lifecycle_inventory("bob") == bob_before
    assert "DELETE ME" not in json.dumps(receipt)


@pytest.mark.asyncio
async def test_mimo_lifecycle_fails_closed_on_symlink_without_touching_target(tmp_path):
    pool = _pool(tmp_path)
    runtime = pool._runtime_home("alice")
    runtime.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("survives", encoding="utf-8")
    (runtime / "escape").symlink_to(outside)

    with pytest.raises(RuntimeError, match="non-regular file"):
        pool.owner_lifecycle_inventory("alice")
    assert outside.read_text(encoding="utf-8") == "survives"


@pytest.mark.asyncio
async def test_mimo_and_grant_manifests_are_bound_to_exact_owners(tmp_path):
    pool = _pool(tmp_path)
    _seed_owner(pool, "alice")
    manifest = await pool.preview_owner_rename("alice", "alice2")

    foreign_agent = copy.deepcopy(manifest)
    foreign_agent["source"]["owner"] = "mallory"
    with pytest.raises(RuntimeError, match="source owner inventory"):
        await pool.reconcile_owner_rename("alice", "alice2", foreign_agent)

    grant_manifest = pool._grant_store.preview_owner_rename("alice", "alice2")
    foreign_grant = copy.deepcopy(grant_manifest)
    foreign_grant["source"]["owner"] = "mallory"
    with pytest.raises(RuntimeError, match="source permission-grant inventory"):
        pool._grant_store.reconcile_owner_rename(
            "alice",
            "alice2",
            foreign_grant,
        )

    malformed_grant = copy.deepcopy(grant_manifest)
    malformed_grant["source"]["active"] += 1
    with pytest.raises(RuntimeError, match="source permission-grant inventory"):
        pool._grant_store.reconcile_owner_rename(
            "alice",
            "alice2",
            malformed_grant,
        )

    forged_empty_target = copy.deepcopy(grant_manifest)
    forged_empty_target["target"]["fingerprint"] = "sha256:" + ("0" * 64)
    with pytest.raises(RuntimeError, match="target permission-grant inventory"):
        pool._grant_store.reconcile_owner_rename(
            "alice",
            "alice2",
            forged_empty_target,
        )

    with pytest.raises(RuntimeError, match="distinct authenticated Agent owners"):
        await pool.compensate_owner_rename("alice", "alice", manifest)

    receipt = await pool.reconcile_owner_rename("alice", "alice2", manifest)
    assert receipt["state"] == "applied"


@pytest.mark.asyncio
async def test_mimo_lifecycle_recognizes_historical_whitespace_grant_owner(tmp_path):
    pool = _pool(tmp_path)
    _seed_owner(pool, "alice")
    with sqlite3.connect(pool._grant_store._db_path) as connection:
        connection.execute(
            "UPDATE permission_grants SET owner=' ALICE ' WHERE lower(owner)='alice'"
        )

    manifest = await pool.preview_owner_rename("alice", "alice2")
    assert manifest["source"]["permission_grants"]["count"] == 2
    receipt = await pool.reconcile_owner_rename("alice", "alice2", manifest)
    assert receipt["target"]["permission_grants"]["count"] == 2


@pytest.mark.asyncio
async def test_mimo_rename_rejects_unfinished_target_purge(tmp_path):
    pool = _pool(tmp_path)
    _seed_owner(pool, "alice")
    stage, _journal_root, _journal = pool._owner_purge_paths("alice2")
    stage.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="unfinished purge"):
        await pool.preview_owner_rename("alice", "alice2")


@pytest.mark.asyncio
async def test_mimo_purge_rejects_symlink_journal_without_following_it(tmp_path):
    pool = _pool(tmp_path)
    _seed_owner(pool, "alice")
    expected = pool.owner_lifecycle_inventory("alice")
    _stage, journal_root, journal = pool._owner_purge_paths("alice")
    journal_root.mkdir(parents=True)
    outside = tmp_path / "outside-journal.json"
    outside.write_text(json.dumps({"private": "survives"}), encoding="utf-8")
    journal.symlink_to(outside)

    with pytest.raises(RuntimeError, match="regular file"):
        await pool.purge_owner_lifecycle("alice", expected=expected)

    assert outside.read_text(encoding="utf-8") == json.dumps({"private": "survives"})
    assert pool.owner_lifecycle_inventory("alice")["fingerprint"] == expected["fingerprint"]


@pytest.mark.asyncio
async def test_mimo_purge_rejects_symlink_journal_root_without_writing_outside(tmp_path):
    pool = _pool(tmp_path)
    _seed_owner(pool, "alice")
    expected = pool.owner_lifecycle_inventory("alice")
    _stage, journal_root, journal = pool._owner_purge_paths("alice")
    outside = tmp_path / "outside-lifecycle"
    outside.mkdir()
    journal_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="journal root.*real directory"):
        await pool.purge_owner_lifecycle("alice", expected=expected)

    assert list(outside.iterdir()) == []
    assert not journal.exists()
    assert pool.owner_lifecycle_inventory("alice")["fingerprint"] == expected["fingerprint"]


def test_mimo_lifecycle_rejects_symlinked_owners_root(tmp_path):
    pool = _pool(tmp_path)
    pool._agent_runtime_root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-owners"
    outside.mkdir()
    pool._owners_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="owners root.*real directory"):
        pool.owner_lifecycle_inventory("alice")

    assert list(outside.iterdir()) == []

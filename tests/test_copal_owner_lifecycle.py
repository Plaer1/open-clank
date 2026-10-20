import asyncio
import json

import pytest

from src.openclank.copal_errors import CopalBridgeError
import src.openclank.copal_loose as copal_loose
from src.openclank.copal_loose import LooseCopalBridge


def _call(bridge, operation, args):
    return asyncio.run(bridge.call(operation, args))


def _create(bridge, owner, workspace, name, body):
    return _call(
        bridge,
        "create",
        {
            "owner": owner,
            "workspace_id": workspace,
            "kind": "markdown",
            "name": name,
            "content": body,
        },
    )["doc"]


def test_loose_owner_reconcile_is_content_free_replay_safe_and_tenant_stable(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "copal")
    visible = _create(bridge, "alice", "home", "private.md", "ALICE SECRET BODY")
    deleted = _create(bridge, "alice", "work", "deleted.md", "DELETED SECRET BODY")
    _call(
        bridge,
        "delete",
        {"owner": "alice", "workspace_id": "work", "id": deleted["id"]},
    )
    _create(bridge, "bob", "home", "bob.md", "BOB SECRET BODY")
    bob_before = _call(bridge, "owner_inventory", {"owner": "bob"})

    receipt = _call(
        bridge,
        "rename_owner",
        {"old_owner": "alice", "new_owner": "alice2"},
    )

    assert receipt["state"] == "applied"
    assert receipt["documents"] == 2
    assert receipt["source_after"]["documents"] == 0
    assert receipt["target_after"]["active_documents"] == 1
    assert receipt["target_after"]["deleted_documents"] == 1
    assert "SECRET BODY" not in json.dumps(receipt)
    assert _call(bridge, "owner_inventory", {"owner": "bob"}) == bob_before
    assert _call(
        bridge,
        "get",
        {"owner": "alice2", "workspace_id": "home", "id": visible["id"]},
    )["text"] == "ALICE SECRET BODY"

    replay = _call(
        bridge,
        "rename_owner",
        {"old_owner": "alice", "new_owner": "alice2"},
    )
    assert replay["state"] == "already_applied"
    assert replay["documents"] == 2
    assert replay["changed_documents"] == 0
    assert "SECRET BODY" not in json.dumps(replay)


def test_loose_lifecycle_dispatch_preflights_and_compensates_exact_manifest(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "copal")
    _create(bridge, "alice", "home", "private.md", "PRIVATE BODY")
    manifest = _call(
        bridge,
        "preflight_rename_owner",
        {"old_owner": "alice", "new_owner": "deleted:stable"},
    )
    assert manifest["source"]["documents"] == 1
    assert manifest["target"]["documents"] == 0
    assert "PRIVATE BODY" not in json.dumps(manifest)

    moved = _call(
        bridge,
        "rename_owner",
        {
            "old_owner": "alice",
            "new_owner": "deleted:stable",
            "manifest": manifest,
        },
    )
    assert moved["state"] == "applied"

    restored = _call(
        bridge,
        "compensate_owner_rename",
        {
            "old_owner": "alice",
            "new_owner": "deleted:stable",
            "manifest": manifest,
        },
    )
    assert restored["state"] == "compensated"
    assert restored["source"]["fingerprint"] == manifest["source"]["fingerprint"]
    assert restored["target"]["documents"] == 0
    assert "PRIVATE BODY" not in json.dumps(restored)

    replay = _call(
        bridge,
        "compensate_owner_rename",
        {
            "old_owner": "alice",
            "new_owner": "deleted:stable",
            "manifest": manifest,
        },
    )
    assert replay["state"] == "already_compensated"


def test_loose_owner_reconcile_recovers_after_directory_move_fault(tmp_path, monkeypatch):
    bridge = LooseCopalBridge(tmp_path / "copal")
    document = _create(bridge, "alice", "home", "private.md", "PRIVATE")
    original = bridge._rewrite_owner_manifests
    failed = False

    def fail_once(root, old_owner, new_owner):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected post-move interruption")
        return original(root, old_owner, new_owner)

    monkeypatch.setattr(bridge, "_rewrite_owner_manifests", fail_once)
    with pytest.raises(OSError, match="injected"):
        bridge.reconcile_owner("alice", "alice2")

    assert not bridge._owner_root("alice").exists()
    assert bridge._owner_root("alice2").is_dir()
    recovered = bridge.reconcile_owner("alice", "alice2")
    assert recovered["state"] == "already_applied"
    assert recovered["target_after"]["record_owner_mismatches"] == 0
    assert _call(
        bridge,
        "get",
        {"owner": "alice2", "workspace_id": "home", "id": document["id"]},
    )["text"] == "PRIVATE"


def test_loose_owner_reconcile_rejects_unrelated_target_and_split_state(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "copal")
    _create(bridge, "alice", "home", "alice.md", "alice")
    _create(bridge, "taken", "home", "taken.md", "taken")
    alice_before = bridge.owner_inventory("alice")
    taken_before = bridge.owner_inventory("taken")

    with pytest.raises(CopalBridgeError, match="both contain"):
        bridge.reconcile_owner("alice", "taken")

    assert bridge.owner_inventory("alice") == alice_before
    assert bridge.owner_inventory("taken") == taken_before

    with pytest.raises(CopalBridgeError, match="already has"):
        bridge.reconcile_owner("missing", "taken")


def test_loose_owner_purge_physically_removes_history_and_is_idempotent(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "copal")
    document = _create(bridge, "alice", "home", "private.md", "FIRST SECRET")
    _call(
        bridge,
        "write",
        {
            "owner": "alice",
            "workspace_id": "home",
            "id": document["id"],
            "base": document["head"],
            "content": "SECOND SECRET",
        },
    )
    _create(bridge, "bob", "home", "bob.md", "BOB")
    bob_before = bridge.owner_inventory("bob")
    expected = bridge.owner_inventory("alice")

    receipt = _call(
        bridge,
        "purge_owner",
        {"owner": "alice", "expected": expected},
    )

    assert receipt["state"] == "applied"
    assert receipt["documents"] == 1
    assert receipt["physical_compaction"] is True
    assert receipt["history_retained"] is False
    assert receipt["after"]["documents"] == 0
    assert "SECRET" not in json.dumps(receipt)
    assert not bridge._owner_root("alice").exists()
    assert bridge.owner_inventory("bob") == bob_before

    replay = _call(bridge, "purge_owner", {"owner": "alice"})
    assert replay["state"] == "empty"
    assert replay["documents"] == 0


def test_loose_owner_lifecycle_fails_closed_on_symlink(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "copal")
    _create(bridge, "alice", "home", "private.md", "PRIVATE")
    outside = tmp_path / "outside"
    outside.write_text("do not touch", encoding="utf-8")
    (bridge._owner_root("alice") / "escape").symlink_to(outside)

    with pytest.raises(CopalBridgeError, match="symbolic links"):
        bridge.owner_inventory("alice")
    with pytest.raises(CopalBridgeError, match="symbolic links"):
        bridge.purge_owner("alice")
    assert outside.read_text(encoding="utf-8") == "do not touch"


def test_loose_owner_manifest_is_bound_and_same_size_edits_stale_the_cas(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "copal")
    _create(bridge, "alice", "home", "private.md", "BEFORE")
    manifest = _call(
        bridge,
        "preflight_rename_owner",
        {"old_owner": "alice", "new_owner": "alice2"},
    )

    foreign = json.loads(json.dumps(manifest))
    foreign["source"]["owner"] = "mallory"
    with pytest.raises(CopalBridgeError, match="lifecycle inventory is invalid"):
        _call(
            bridge,
            "rename_owner",
            {
                "old_owner": "alice",
                "new_owner": "alice2",
                "manifest": foreign,
            },
        )

    document = bridge._vault("alice", "home") / "private.md"
    document.write_text("AFTERS", encoding="utf-8")
    with pytest.raises(CopalBridgeError, match="inventory changed"):
        _call(
            bridge,
            "rename_owner",
            {
                "old_owner": "alice",
                "new_owner": "alice2",
                "manifest": manifest,
            },
        )
    assert document.read_text(encoding="utf-8") == "AFTERS"
    assert not bridge._owner_root("alice2").exists()


def test_loose_owner_purge_recovers_after_partial_recursive_delete(tmp_path, monkeypatch):
    bridge = LooseCopalBridge(tmp_path / "copal")
    _create(bridge, "alice", "home", "private.md", "PRIVATE")
    expected = bridge.owner_inventory("alice")
    original_rmtree = copal_loose.shutil.rmtree
    interrupted = False

    def partially_remove_once(root):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            victim = next(path for path in root.rglob("*") if path.is_file())
            victim.unlink()
            raise OSError("injected partial recursive removal")
        return original_rmtree(root)

    monkeypatch.setattr(copal_loose.shutil, "rmtree", partially_remove_once)
    with pytest.raises(OSError, match="injected"):
        bridge.purge_owner("alice", expected=expected)

    receipt = bridge.purge_owner("alice", expected=expected)
    assert receipt["state"] == "applied"
    assert bridge.owner_inventory("alice")["documents"] == 0

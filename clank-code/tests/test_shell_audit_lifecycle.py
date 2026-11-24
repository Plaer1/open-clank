import json

import pytest

from src.openclank.shell_audit_lifecycle import (
    ShellAuditLifecycleError,
    ShellAuditOwnerLifecycle,
)


def _write(path, *rows):
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_shell_audit_rename_compensate_and_purge_are_owner_exact(tmp_path):
    path = tmp_path / "shell-audit.jsonl"
    _write(
        path,
        {"owner": "Alice", "event": "start", "command": "secret-a"},
        {"owner": "bob", "event": "start", "command": "secret-b"},
        {"owner": "alice", "event": "finish", "command": "secret-c"},
    )
    lifecycle = ShellAuditOwnerLifecycle(path)
    bob_before = lifecycle.owner_inventory("bob")
    manifest = lifecycle.preview_owner_rename("alice", "deleted:stable")

    first = lifecycle.reconcile_owner_rename("alice", "deleted:stable", manifest)
    replay = lifecycle.reconcile_owner_rename("alice", "deleted:stable", manifest)
    assert first["count"] == replay["count"] == 2
    assert lifecycle.owner_inventory("alice")["count"] == 0
    assert lifecycle.owner_inventory("bob") == bob_before

    restored = lifecycle.compensate_owner_rename(
        "alice", "deleted:stable", manifest
    )
    assert restored["state"] == "restored"
    assert lifecycle.owner_inventory("alice")["count"] == 2
    lifecycle.reconcile_owner_rename("alice", "deleted:stable", manifest)
    purged = lifecycle.purge_owner("deleted:stable", expected=manifest)
    assert purged["complete"] is True
    assert purged["count"] == 2
    assert lifecycle.owner_inventory("bob") == bob_before
    assert "secret-b" in path.read_text(encoding="utf-8")


def test_shell_audit_rejects_target_conflict_stale_rows_and_symlink(tmp_path):
    path = tmp_path / "shell-audit.jsonl"
    _write(path, {"owner": "alice", "event": "start"})
    lifecycle = ShellAuditOwnerLifecycle(path)
    manifest = lifecycle.preview_owner_rename("alice", "ada")
    _write(
        path,
        {"owner": "alice", "event": "start"},
        {"owner": "alice", "event": "late"},
    )
    with pytest.raises(ShellAuditLifecycleError, match="frozen preview"):
        lifecycle.reconcile_owner_rename("alice", "ada", manifest)

    _write(path, {"owner": "alice"}, {"owner": "ada"})
    with pytest.raises(ShellAuditLifecycleError, match="target"):
        lifecycle.preview_owner_rename("alice", "ada")

    target = tmp_path / "real.jsonl"
    target.write_text("", encoding="utf-8")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ShellAuditLifecycleError, match="symlink"):
        lifecycle.owner_inventory("alice")


def test_shell_audit_rejects_symlink_present_at_construction(tmp_path):
    target = tmp_path / "external.jsonl"
    _write(target, {"owner": "alice", "event": "start"})
    path = tmp_path / "shell-audit.jsonl"
    path.symlink_to(target)

    lifecycle = ShellAuditOwnerLifecycle(path)

    with pytest.raises(ShellAuditLifecycleError, match="symlink"):
        lifecycle.owner_inventory("alice")


def test_shell_audit_rotation_keeps_complete_json_records(tmp_path, monkeypatch):
    from src import constants, shell_policy

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(shell_policy, "_AUDIT_MAX_BYTES", 2048)
    for index in range(20):
        shell_policy.append_shell_audit(
            command=f"echo item-{index} " + "x" * 300,
            owner="alice", session_id="test", workspace=str(tmp_path),
            containment="host",
        )
    path = tmp_path / "shell-audit.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert path.stat().st_size <= 2048
    assert rows and "item-19" in rows[-1]["command"]
    assert ShellAuditOwnerLifecycle(path).owner_inventory("alice")["count"] == len(rows)

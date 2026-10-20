from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from src.openclank.migration_snapshot import (
    MigrationLock,
    MigrationSnapshotError,
    create_rollback_archive,
    restore_rollback_archive,
    verify_rollback_archive,
)


def _data_dir(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    db = sqlite3.connect(root / "app.db")
    db.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO sample(value) VALUES ('before')")
    db.commit()
    db.close()
    (root / "auth.json").write_text('{"users":{"alice":{"is_admin":true}}}')
    (root / "settings.json").write_text('{"provider":"configured"}')
    runtime = root / "runtime/agent-engine/owners/abc/generations/1/home"
    runtime.mkdir(parents=True)
    (runtime / "auth.json").write_text('{"openai":{"type":"api","key":"secret"}}')
    (runtime / "large.bin").write_bytes(b"x" * 1024)
    return root


def test_encrypted_snapshot_round_trip_and_selective_runtime_scope(tmp_path, monkeypatch):
    data = _data_dir(tmp_path)
    result = create_rollback_archive(
        data,
        destination_dir=tmp_path / "backups",
        now=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )
    assert result.archive.read_bytes().startswith(b"OPENCLANK-PROVIDER-ROLLBACK-V1\n")
    assert b"secret" not in result.archive.read_bytes()
    report = verify_rollback_archive(result.archive)
    assert report["file_count"] == result.file_count

    (data / "settings.json").write_text("changed")
    with pytest.raises(MigrationSnapshotError, match="explicit acceptance"):
        restore_rollback_archive(
            result.archive,
            data,
            accept_post_cut_data_loss=False,
        )
    restore_rollback_archive(
        result.archive,
        data,
        accept_post_cut_data_loss=True,
    )
    assert json.loads((data / "settings.json").read_text())["provider"] == "configured"
    # Non-provider payloads are intentionally absent from the archive and are
    # not treated as migration rollback targets.
    assert (data / "runtime/agent-engine/owners/abc/generations/1/home/large.bin").exists()


def test_snapshot_tampering_fails_authentication(tmp_path):
    data = _data_dir(tmp_path)
    result = create_rollback_archive(data, destination_dir=tmp_path / "backups")
    raw = bytearray(result.archive.read_bytes())
    raw[-10] ^= 1
    result.archive.write_bytes(raw)
    with pytest.raises(MigrationSnapshotError, match="authentication"):
        verify_rollback_archive(result.archive)


def test_migration_lock_rejects_concurrent_owner(tmp_path):
    lock_path = tmp_path / "migration.lock"
    with MigrationLock(lock_path):
        with pytest.raises(MigrationSnapshotError, match="another"):
            with MigrationLock(lock_path):
                pass

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


MIGRATION = Path(__file__).resolve().parent / "fixtures/provider_account_policy.py"


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}


def test_policy_migration_is_offline_additive_and_requires_fresh_backup(tmp_path):
    database = tmp_path / "copied-app.db"
    db = sqlite3.connect(database)
    db.executescript(
        """
        CREATE TABLE provider_connections (id TEXT PRIMARY KEY, label TEXT, settings JSON);
        CREATE TABLE provider_operation_bindings (id TEXT PRIMARY KEY, root_operation_id TEXT, selected_account_id TEXT);
        INSERT INTO provider_connections VALUES ('pcn_synthetic', 'Primary', '{"keep":true}');
        INSERT INTO provider_operation_bindings VALUES ('pbd_synthetic', 'root_synthetic', 'pac_synthetic');
        """
    )
    db.commit()
    db.close()

    plan = subprocess.run(
        [sys.executable, str(MIGRATION), "--database", str(database)],
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(plan.stdout)["status"] == "planned"
    with sqlite3.connect(database) as db:
        assert "account_pool_policy" not in _columns(db, "provider_connections")
        assert "policy_snapshot" not in _columns(db, "provider_operation_bindings")

    backup_dir = tmp_path / "backup"
    denied = subprocess.run(
        [
            sys.executable,
            str(MIGRATION),
            "--database",
            str(database),
            "--apply",
            "--backup-dir",
            str(backup_dir),
        ],
        text=True,
        capture_output=True,
    )
    assert denied.returncode == 2
    assert not backup_dir.exists()

    applied = subprocess.run(
        [
            sys.executable,
            str(MIGRATION),
            "--database",
            str(database),
            "--apply",
            "--writers-stopped",
            "--backup-dir",
            str(backup_dir),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    result = json.loads(applied.stdout)
    assert result["status"] == "converted"
    assert Path(result["backup"]).is_file()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT id, label, settings, account_pool_policy FROM provider_connections").fetchone() == (
            "pcn_synthetic",
            "Primary",
            '{"keep":true}',
            None,
        )
        assert db.execute("SELECT id, root_operation_id, selected_account_id, policy_snapshot FROM provider_operation_bindings").fetchone() == (
            "pbd_synthetic",
            "root_synthetic",
            "pac_synthetic",
            None,
        )
    with sqlite3.connect(result["backup"]) as backup:
        assert "account_pool_policy" not in _columns(backup, "provider_connections")
        assert "policy_snapshot" not in _columns(backup, "provider_operation_bindings")

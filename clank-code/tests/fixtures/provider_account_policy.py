"""Test fixture for the historical additive provider account-policy conversion.

Exercises the original SQLite conversion against a caller-supplied synthetic database.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
import sys


REQUIRED_COLUMNS = {
    "provider_connections": {"account_pool_policy": "JSON"},
    "provider_operation_bindings": {"policy_snapshot": "JSON"},
}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}


def _inspect(path: Path) -> tuple[dict[str, int], dict[str, list[str]]]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("database must be an existing regular copied SQLite file")
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing_tables = sorted(set(REQUIRED_COLUMNS) - tables)
        if missing_tables:
            raise ValueError("database is missing provider tables: " + ", ".join(missing_tables))
        counts = {
            table: int(db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in REQUIRED_COLUMNS
        }
        missing = {
            table: sorted(set(columns) - _columns(db, table))
            for table, columns in REQUIRED_COLUMNS.items()
        }
        return counts, missing
    finally:
        db.close()


def _backup(path: Path, backup_dir: Path) -> Path:
    if backup_dir.exists():
        raise ValueError("backup directory must be new and not already exist")
    backup_dir.mkdir(parents=True)
    backup_path = backup_dir / "provider-account-policy-preimage.sqlite3"
    source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ValueError("database backup failed integrity verification")
    finally:
        target.close()
        source.close()
    return backup_path


def _apply(path: Path, backup_dir: Path) -> tuple[dict[str, int], dict[str, list[str]], Path]:
    before_counts, missing = _inspect(path)
    backup_path = _backup(path, backup_dir)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        for table, columns in REQUIRED_COLUMNS.items():
            present = _columns(db, table)
            for column, sql_type in columns.items():
                if column not in present:
                    db.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type}')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    after_counts, remaining = _inspect(path)
    if before_counts != after_counts or any(remaining.values()):
        raise ValueError("provider policy migration verification failed; restore the backup")
    return before_counts, missing, backup_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="explicit path to a copied SQLite database")
    parser.add_argument("--apply", action="store_true", help="apply the additive conversion")
    parser.add_argument("--backup-dir", help="new directory for the required preimage backup")
    parser.add_argument("--writers-stopped", action="store_true", help="acknowledge all database writers are stopped")
    args = parser.parse_args(argv)
    selected_path = Path(args.database).expanduser()
    if selected_path.is_symlink():
        print("provider account policy migration failed: database path must not be a symlink", file=sys.stderr)
        return 2
    path = selected_path.resolve()
    try:
        if args.apply:
            if not args.writers_stopped:
                raise ValueError("--apply requires --writers-stopped")
            if not args.backup_dir:
                raise ValueError("--apply requires --backup-dir naming a new directory")
            counts, missing, backup_path = _apply(path, Path(args.backup_dir).expanduser().resolve())
            result = {
                "status": "converted",
                "database": str(path),
                "tables": counts,
                "added_columns": missing,
                "backup": str(backup_path),
            }
        else:
            counts, missing = _inspect(path)
            result = {
                "status": "already_current" if not any(missing.values()) else "planned",
                "database": str(path),
                "tables": counts,
                "missing_columns": missing,
                "writes": False,
            }
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"provider account policy migration failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

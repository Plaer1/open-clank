"""Disposable-file tests for the durable history settings projection."""

from pathlib import Path

import pytest

from src.openclank.history_settings import (
    DEFAULT_TOTAL_BYTES,
    load_settings,
    measure_usage,
    scope_id,
    update_settings,
)


def test_defaults_persist_with_revision_and_scope_precedence(tmp_path: Path):
    settings = tmp_path / "history-settings.json"
    first = load_settings(settings)
    assert first["global"]["total_bytes"] == DEFAULT_TOTAL_BYTES
    second = update_settings(
        {
            "global": {"total_bytes": 2 * 1024 * 1024},
            "scopes": [
                {"kind": "workspace", "owner_account_id": "alice", "workspace_id": "default", "limit_bytes": 1024},
                {"kind": "directory", "owner_account_id": "alice", "workspace_id": "default", "root": str(tmp_path), "limit_bytes": 512},
            ],
        },
        expected_revision=first["revision"],
        path=settings,
    )
    assert second["revision"] == 2
    reopened = load_settings(settings)
    assert reopened["global"]["total_bytes"] == 2 * 1024 * 1024
    assert reopened["scopes"][0]["scope_id"] == scope_id("workspace", "default", "default", "alice")
    assert reopened["scopes"][0]["workspace_id"] == "default"
    assert reopened["scopes"][0]["owner_account_id"] == "alice"


def test_revision_conflict_does_not_overwrite(tmp_path: Path):
    settings = tmp_path / "history-settings.json"
    with pytest.raises(ValueError, match="revision"):
        update_settings({"global": {"total_bytes": 123}}, expected_revision=99, path=settings)
    assert load_settings(settings)["global"]["total_bytes"] == DEFAULT_TOTAL_BYTES


def test_measurement_reports_allocated_and_apparent_bytes(tmp_path: Path):
    root = tmp_path / "store"
    root.mkdir()
    (root / "payload").write_bytes(b"history")
    usage = measure_usage(root)
    assert usage["apparent_file_bytes"] == 7
    assert usage["physical_allocated_bytes"] >= 7
    assert usage["retained_version_count"] == 1

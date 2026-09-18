from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.clanker_layout_migration import (
    LayoutMigrationError,
    apply_manifest,
    create_manifest,
    load_manifest,
    rollback_manifest,
)


def test_dry_run_is_manifest_bound_and_non_mutating(tmp_path: Path) -> None:
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "a.md").write_text("hello\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"

    manifest = create_manifest(source, destination, manifest_path)
    before = manifest_path.read_bytes()
    result = apply_manifest(manifest_path, dry_run=True)

    assert result["status"] == "dry_run"
    assert manifest_path.read_bytes() == before
    assert (source / "nested" / "a.md").exists()
    assert not destination.exists()
    assert manifest["plan_hash"]


def test_apply_and_rollback_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir()
    (source / "a.md").write_text("a\n", encoding="utf-8")
    (source / "b.md").write_text("b\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    create_manifest(source, destination, manifest_path)

    assert apply_manifest(manifest_path)["status"] == "applied"
    assert not (source / "a.md").exists()
    assert (destination / "a.md").read_text() == "a\n"
    assert json.loads(manifest_path.read_text())["status"] == "applied"

    assert rollback_manifest(manifest_path)["status"] == "rolled_back"
    assert (source / "a.md").read_text() == "a\n"
    assert not (destination / "a.md").exists()


def test_apply_removes_all_empty_source_ancestors(tmp_path: Path) -> None:
    source = tmp_path / "old"
    destination = tmp_path / "new"
    deep = source / "one" / "two" / "three"
    deep.mkdir(parents=True)
    (deep / "a.md").write_text("a\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    create_manifest(source, destination, manifest_path)
    apply_manifest(manifest_path)
    from src.clanker_layout_migration import finalize_manifest
    finalize_manifest(manifest_path)
    assert not (source / "one").exists()


def test_reapply_reports_already_migrated_and_changed_destination_blocks_rollback(tmp_path: Path) -> None:
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir()
    (source / "a.md").write_text("a\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    create_manifest(source, destination, manifest_path)
    apply_manifest(manifest_path)
    assert apply_manifest(manifest_path)["status"] == "already_migrated"
    (destination / "a.md").write_text("new work\n", encoding="utf-8")
    with pytest.raises(LayoutMigrationError, match="destination changed"):
        rollback_manifest(manifest_path)


def test_divergent_collision_blocks_before_mutation(tmp_path: Path) -> None:
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir()
    destination.mkdir()
    (source / "same.md").write_text("source\n", encoding="utf-8")
    (destination / "same.md").write_text("different\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    create_manifest(source, destination, manifest_path)

    with pytest.raises(LayoutMigrationError, match="divergent destination collision"):
        apply_manifest(manifest_path)
    assert (source / "same.md").read_text() == "source\n"
    assert (destination / "same.md").read_text() == "different\n"


def test_identical_collision_deduplicates_and_rollback_restores_source(tmp_path: Path) -> None:
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir()
    destination.mkdir()
    (source / "same.md").write_text("same\n", encoding="utf-8")
    (destination / "same.md").write_text("same\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    create_manifest(source, destination, manifest_path)

    apply_manifest(manifest_path)
    assert (source / "same.md").exists()
    assert (destination / "same.md").exists()
    from src.clanker_layout_migration import finalize_manifest
    finalize_manifest(manifest_path)
    assert not (source / "same.md").exists()
    rollback_manifest(manifest_path)
    assert (source / "same.md").read_text() == "same\n"
    assert (destination / "same.md").read_text() == "same\n"


def test_robonotes_root_ds_store_is_recorded_but_not_migrated(tmp_path: Path) -> None:
    source = tmp_path / ".robonotes"
    destination = tmp_path / ".clankers" / "robonotes"
    source.mkdir()
    (source / ".DS_Store").write_bytes(b"junk")
    (source / "run.md").write_text("run\n")
    manifest_path = tmp_path / "manifest.json"
    manifest = create_manifest(source, destination, manifest_path)
    assert manifest["source_manifest"]["excluded_entries"][0]["relative_path"] == ".DS_Store"
    apply_manifest(manifest_path)
    assert not (destination / ".DS_Store").exists()
    assert (source / ".DS_Store").exists()


def test_manifest_tampering_and_nested_manifest_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir()
    (source / "a").write_text("a")
    with pytest.raises(LayoutMigrationError, match="manifest must not"):
        create_manifest(source, destination, source / "manifest.json")

    manifest_path = tmp_path / "manifest.json"
    create_manifest(source, destination, manifest_path)
    payload = json.loads(manifest_path.read_text())
    payload["entries"][0]["relative_path"] = "escape"
    manifest_path.write_text(json.dumps(payload))
    with pytest.raises(LayoutMigrationError, match="plan hash"):
        load_manifest(manifest_path)

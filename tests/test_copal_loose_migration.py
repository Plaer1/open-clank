import pytest

from src.openclank.copal_loose_migration import LooseCopalMigration, LooseMigrationError


def test_snapshot_migration_stages_and_cutovers_without_overwriting(tmp_path):
    migration = LooseCopalMigration(tmp_path / "vaults")
    snapshot = {"docs": [{"id": "DOC1", "kind": "markdown", "name": "Notes/One.md", "text": "one\n", "head": "redb-head"}]}

    descriptor = migration.stage(snapshot, owner="owner", workspace="home", source={"redb": "backup.redb"})
    assert descriptor["state"] == "staged"
    assert not (tmp_path / "vaults" / "owner" / "home").exists()
    cutover = migration.cutover(descriptor)

    assert cutover["state"] == "files-live"
    assert (tmp_path / "vaults" / "b3duZXI" / "aG9tZQ" / "Notes" / "One.md").read_text() == "one\n"


def test_snapshot_dry_run_rejects_collisions_and_nonempty_targets(tmp_path):
    migration = LooseCopalMigration(tmp_path / "vaults")
    snapshot = {"docs": [{"id": "DOC1", "name": "same.md", "text": "one"}, {"id": "DOC2", "name": "same.md", "text": "two"}]}

    report = migration.dry_run(snapshot, owner="owner", workspace="home")
    assert report["ok"] is False
    assert any(issue["code"] == "path_collision" for issue in report["issues"])

    target = migration.repository._vault("owner", "home")
    target.mkdir(parents=True)
    (target / "existing.md").write_text("keep")
    with pytest.raises(LooseMigrationError):
        migration.stage({"docs": [{"id": "DOC1", "name": "new.md", "text": "new"}]}, owner="owner", workspace="home")


def test_snapshot_migration_preserves_typed_hidden_system_documents(tmp_path):
    migration = LooseCopalMigration(tmp_path / "vaults")
    snapshot = {
        "docs": [
            {
                "id": "TRACKS",
                "kind": "copal-tracks",
                "name": ".copal/tracks.json",
                "text": '{"schemaVersion":2,"tracks":[]}',
                "head": "redb-tracks",
            }
        ]
    }

    descriptor = migration.stage(snapshot, owner="owner", workspace="home")
    migration.cutover(descriptor)
    document = migration.repository._call_sync(
        "get", {"owner": "owner", "workspace_id": "home", "id": "TRACKS"}
    )

    assert document["name"] == ".copal/tracks.json"
    assert document["hidden"] is True

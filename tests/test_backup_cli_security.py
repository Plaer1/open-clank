import hashlib
import io
import json
import sqlite3
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.cli_loader import load_script


def _load_backup_cli():
    return load_script("odysseus-backup")


def _patch_repo(module, monkeypatch, root: Path):
    monkeypatch.setattr(module, "_REPO_ROOT", root)
    monkeypatch.setattr(module, "_DATA_DIR", root / "data")
    monkeypatch.setattr(module, "_BACKUP_DIR", root / "backups")


def _snapshot_args(path: Path):
    return SimpleNamespace(
        out=str(path),
        include_research=False,
        include_attachments=False,
        pretty=False,
    )


def _restore_args(path: Path):
    return SimpleNamespace(path=str(path), yes=True, app_stopped=True, pretty=False)


def _verify_args(path: Path):
    return SimpleNamespace(path=str(path), pretty=False)


def _tree_bytes(root: Path):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _add_bytes(tar, name: str, payload: bytes):
    item = tarfile.TarInfo(name)
    item.size = len(payload)
    tar.addfile(item, io.BytesIO(payload))


def _add_dir(tar, name: str):
    item = tarfile.TarInfo(name)
    item.type = tarfile.DIRTYPE
    tar.addfile(item)


def _write_legacy_archive(path: Path, files: dict[str, bytes]):
    with tarfile.open(path, "w:gz") as tar:
        for name, payload in files.items():
            _add_bytes(tar, f"data/{name}", payload)


def _write_manifested_archive(
    backup,
    path: Path,
    archived_files: dict[str, bytes],
    manifest_files: dict[str, bytes] | None = None,
):
    declared = archived_files if manifest_files is None else manifest_files
    metadata = {
        name: {
            "kind": "sqlite" if payload.startswith(b"SQLite format 3\x00") else "file",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        for name, payload in declared.items()
    }
    manifest = json.dumps(
        {
            "format": backup._MANIFEST_FORMAT,
            "version": backup._MANIFEST_VERSION,
            "files": metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    with tarfile.open(path, "w:gz") as tar:
        for name, payload in archived_files.items():
            _add_bytes(tar, f"data/{name}", payload)
        _add_bytes(tar, backup._MANIFEST_NAME, manifest)


def test_backup_entry_skips_files_that_disappear():
    backup = _load_backup_cli()

    class Vanished:
        name = "gone.tar.gz"

        def is_file(self):
            return True

        def stat(self):
            raise FileNotFoundError("gone")

        def __str__(self):
            return "backups/gone.tar.gz"

    assert backup._backup_entry(Vanished()) is None


def test_backup_list_sorts_by_captured_mtime(monkeypatch):
    backup = _load_backup_cli()
    first = SimpleNamespace(name="older.tar.gz")
    second = SimpleNamespace(name="newer.tar.gz")
    monkeypatch.setattr(backup, "_BACKUP_DIR", SimpleNamespace(
        is_dir=lambda: True,
        iterdir=lambda: [first, second],
    ))
    monkeypatch.setattr(backup, "_backup_entry", lambda p: {
        "name": p.name,
        "modified": "2026-10-25T01:45:00" if p is first else "2026-10-25T01:15:00",
        "_mtime": 100 if p is first else 200,
    })
    seen = []
    monkeypatch.setattr(backup, "emit", lambda payload, args: seen.append(payload))

    backup.cmd_list(SimpleNamespace(pretty=False))

    assert [entry["name"] for entry in seen[0]] == ["newer.tar.gz", "older.tar.gz"]
    assert all("_mtime" not in entry for entry in seen[0])


def test_snapshot_rejects_output_inside_data_dir(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    _patch_repo(backup, monkeypatch, repo)

    with pytest.raises(SystemExit):
        backup._reject_output_inside_data(data / "self.tar.gz")


def test_restore_requires_explicit_stopped_app_assertion(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.txt").write_bytes(b"prior")
    _patch_repo(backup, monkeypatch, repo)
    archive = tmp_path / "valid.tar.gz"
    _write_legacy_archive(archive, {"new.txt": b"new"})

    args = _restore_args(archive)
    args.app_stopped = False
    with pytest.raises(SystemExit):
        backup.cmd_restore(args)

    assert _tree_bytes(data) == {"keep.txt": b"prior"}


def test_restore_rejects_symlink_escape(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    outside = tmp_path / "outside"
    data.mkdir(parents=True)
    outside.mkdir()
    (data / "keep.txt").write_text("still here", encoding="utf-8")
    _patch_repo(backup, monkeypatch, repo)

    tar_path = tmp_path / "malicious.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        data_dir = tarfile.TarInfo("data")
        data_dir.type = tarfile.DIRTYPE
        tar.addfile(data_dir)

        link = tarfile.TarInfo("data/link")
        link.type = tarfile.SYMTYPE
        link.linkname = str(outside)
        tar.addfile(link)

        payload = b"escaped"
        escaped = tarfile.TarInfo("data/link/pwned.txt")
        escaped.size = len(payload)
        tar.addfile(escaped, io.BytesIO(payload))

    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(tar_path))

    assert not (outside / "pwned.txt").exists()
    assert (data / "keep.txt").read_text(encoding="utf-8") == "still here"


def test_verify_rejects_symlink_escape(tmp_path):
    backup = _load_backup_cli()

    tar_path = tmp_path / "malicious.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        link = tarfile.TarInfo("data/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp"
        tar.addfile(link)

    with pytest.raises(SystemExit):
        backup.cmd_verify(_verify_args(tar_path))


def test_restore_rejects_hardlink_entries(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    _patch_repo(backup, monkeypatch, repo)

    tar_path = tmp_path / "hardlink.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        link = tarfile.TarInfo("data/hardlink")
        link.type = tarfile.LNKTYPE
        link.linkname = "../outside.txt"
        tar.addfile(link)

    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(tar_path))


def test_restore_extracts_regular_files_without_extractall(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "old.txt").write_text("old", encoding="utf-8")
    _patch_repo(backup, monkeypatch, repo)

    tar_path = tmp_path / "valid.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        folder = tarfile.TarInfo("data/nested")
        folder.type = tarfile.DIRTYPE
        tar.addfile(folder)

        payload = b"new"
        item = tarfile.TarInfo("data/nested/new.txt")
        item.size = len(payload)
        tar.addfile(item, io.BytesIO(payload))

    real_exchange = backup._exchange_paths
    exchange_observed = []

    def observe_atomic_exchange(first, second):
        assert first == data
        assert first.exists()
        assert second.exists()
        real_exchange(first, second)
        assert first.exists()
        assert second.exists()
        exchange_observed.append(True)

    monkeypatch.setattr(backup, "_exchange_paths", observe_atomic_exchange)
    backup.cmd_restore(_restore_args(tar_path))

    assert exchange_observed == [True]
    assert (repo / "data" / "nested" / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (repo / "data" / "old.txt").exists()
    assert list(repo.glob("data.before-restore-*"))


def test_snapshot_wal_database_is_coherent_and_omits_sidecars(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    _patch_repo(backup, monkeypatch, repo)

    db_path = data / "state.db"
    keeper = sqlite3.connect(db_path)
    assert keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.execute("PRAGMA foreign_keys=ON")
    keeper.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    keeper.execute(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL "
        "REFERENCES parent(id))"
    )
    keeper.execute("INSERT INTO parent(value) VALUES ('initial')")
    keeper.execute("INSERT INTO child(parent_id) VALUES (1)")
    keeper.commit()
    db_path.chmod(0o600)

    started = threading.Event()
    snapshot_started = threading.Event()
    committed_during_snapshot = threading.Event()
    stop = threading.Event()
    writer_errors = []

    def write_commits():
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            index = 0
            conn.execute("INSERT INTO parent(value) VALUES (?)", (f"writer-{index}",))
            conn.commit()
            index += 1
            started.set()
            if not snapshot_started.wait(timeout=5):
                raise TimeoutError("snapshot did not enter SQLite backup")
            conn.execute("INSERT INTO parent(value) VALUES (?)", (f"writer-{index}",))
            conn.commit()
            index += 1
            committed_during_snapshot.set()
            while not stop.is_set():
                conn.execute("INSERT INTO parent(value) VALUES (?)", (f"writer-{index}",))
                conn.commit()
                index += 1
            conn.close()
        except Exception as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)
            started.set()

    writer = threading.Thread(target=write_commits, daemon=True)
    writer.start()
    assert started.wait(timeout=5)
    real_sqlite_copy = backup._sqlite_safe_copy

    def gated_sqlite_copy(source, destination):
        snapshot_started.set()
        assert committed_during_snapshot.wait(timeout=5)
        real_sqlite_copy(source, destination)

    monkeypatch.setattr(backup, "_sqlite_safe_copy", gated_sqlite_copy)

    archive = tmp_path / "wal-snapshot.tar.gz"
    try:
        backup.cmd_snapshot(_snapshot_args(archive))
    finally:
        stop.set()
        writer.join(timeout=5)

    assert not writer.is_alive()
    assert writer_errors == []
    assert (data / "state.db-wal").exists()
    assert (data / "state.db-shm").exists()

    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert "data/state.db" in names
    assert backup._MANIFEST_NAME in names
    assert not any(name.endswith(("-wal", "-shm", "-journal")) for name in names)

    staged = tmp_path / "staged"
    staged.mkdir()
    result = backup._stage_archive(archive, staged)
    assert set(_tree_bytes(staged / "data")) == {"state.db"}
    snap = sqlite3.connect(staged / "data" / "state.db")
    assert snap.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    assert snap.execute("PRAGMA foreign_key_check").fetchall() == []
    assert snap.execute("SELECT COUNT(*) FROM parent").fetchone()[0] >= 1
    snap.close()
    assert (staged / "data" / "state.db").stat().st_mode & 0o777 == 0o600
    keeper.close()
    assert result["sqlite_files"] == 1
    assert result["consolidated_legacy_sidecars"] == 0


def test_snapshot_sqlite_backup_failure_does_not_publish_or_overwrite(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    _patch_repo(backup, monkeypatch, repo)
    db_path = data / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    archive = tmp_path / "existing.tar.gz"
    archive.write_bytes(b"prior archive bytes")

    def fail_backup(_source, _destination):
        raise backup.BackupValidationError("injected backup API failure")

    monkeypatch.setattr(backup, "_sqlite_safe_copy", fail_backup)
    with pytest.raises(SystemExit):
        backup.cmd_snapshot(_snapshot_args(archive))

    assert archive.read_bytes() == b"prior archive bytes"
    assert list(tmp_path.glob(f".{archive.name}.*.tmp")) == []


def test_snapshot_candidate_validation_failure_does_not_publish_or_overwrite(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "item.txt").write_bytes(b"new payload")
    _patch_repo(backup, monkeypatch, repo)
    archive = tmp_path / "existing.tar.gz"
    archive.write_bytes(b"prior archive bytes")

    def fail_validation(_path, _staging_root):
        raise backup.BackupValidationError("injected candidate validation failure")

    monkeypatch.setattr(backup, "_stage_archive", fail_validation)
    with pytest.raises(SystemExit):
        backup.cmd_snapshot(_snapshot_args(archive))

    assert archive.read_bytes() == b"prior archive bytes"
    assert list(tmp_path.glob(f".{archive.name}.*.tmp")) == []


def test_snapshot_classifies_non_sqlite_dot_db_as_regular_file(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    _patch_repo(backup, monkeypatch, repo)
    (data / "opaque.db").write_bytes(b"not actually sqlite")

    archive = tmp_path / "opaque.tar.gz"
    backup.cmd_snapshot(_snapshot_args(archive))

    staged = tmp_path / "staged"
    staged.mkdir()
    result = backup._stage_archive(archive, staged)
    assert (staged / "data" / "opaque.db").read_bytes() == b"not actually sqlite"
    assert result["sqlite_files"] == 0


def test_snapshot_preserves_ordinary_files_with_sidecar_suffixes(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    _patch_repo(backup, monkeypatch, repo)
    (data / "notes").write_bytes(b"ordinary primary")
    (data / "notes-wal").write_bytes(b"ordinary wal-suffixed payload")
    (data / "orphan-shm").write_bytes(b"ordinary shm-suffixed payload")

    archive = tmp_path / "ordinary-suffixes.tar.gz"
    backup.cmd_snapshot(_snapshot_args(archive))

    staged = tmp_path / "staged"
    staged.mkdir()
    result = backup._stage_archive(archive, staged)
    assert (staged / "data" / "notes-wal").read_bytes() == (
        b"ordinary wal-suffixed payload"
    )
    assert (staged / "data" / "orphan-shm").read_bytes() == (
        b"ordinary shm-suffixed payload"
    )
    assert result["consolidated_legacy_sidecars"] == 0


def test_snapshot_preserves_uploaded_non_sqlite_file_named_app_db(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    uploaded = data / "personal_uploads" / "app.db"
    uploaded.parent.mkdir(parents=True)
    uploaded.write_bytes(b"opaque uploaded content")
    _patch_repo(backup, monkeypatch, repo)

    archive = tmp_path / "uploaded-app-db.tar.gz"
    backup.cmd_snapshot(_snapshot_args(archive))

    staged = tmp_path / "staged"
    staged.mkdir()
    result = backup._stage_archive(archive, staged)
    assert (staged / "data" / "personal_uploads" / "app.db").read_bytes() == (
        b"opaque uploaded content"
    )
    assert result["sqlite_files"] == 0


def test_snapshot_materializes_internal_file_symlink_as_regular_payload(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    stores = data / "stores"
    stores.mkdir(parents=True)
    target = stores / "canonical.db"
    conn = sqlite3.connect(target)
    conn.execute("CREATE TABLE item (value TEXT NOT NULL)")
    conn.execute("INSERT INTO item(value) VALUES ('linked store')")
    conn.commit()
    conn.close()
    link = data / "app.db"
    try:
        link.symlink_to(target.relative_to(data))
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    _patch_repo(backup, monkeypatch, repo)

    archive = tmp_path / "linked-store.tar.gz"
    backup.cmd_snapshot(_snapshot_args(archive))

    staged = tmp_path / "staged"
    staged.mkdir()
    result = backup._stage_archive(archive, staged)
    restored_link = staged / "data" / "app.db"
    assert restored_link.is_file()
    assert not restored_link.is_symlink()
    restored = sqlite3.connect(restored_link)
    assert restored.execute("SELECT value FROM item").fetchall() == [
        ("linked store",)
    ]
    restored.close()
    assert result["sqlite_files"] == 2


def test_snapshot_rejects_external_symlink_without_overwriting_output(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"must not enter archive")
    try:
        (data / "external.txt").symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    _patch_repo(backup, monkeypatch, repo)
    archive = tmp_path / "existing.tar.gz"
    archive.write_bytes(b"prior archive bytes")

    with pytest.raises(SystemExit):
        backup.cmd_snapshot(_snapshot_args(archive))

    assert archive.read_bytes() == b"prior archive bytes"
    assert list(tmp_path.glob(f".{archive.name}.*.tmp")) == []


def test_snapshot_rejects_damaged_dynamic_mimocode_store(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    dynamic = data / "runtime" / "agent-engine" / "data" / "mimocode-beta.db"
    dynamic.parent.mkdir(parents=True)
    dynamic.write_bytes(b"damaged dynamic database")
    _patch_repo(backup, monkeypatch, repo)
    archive = tmp_path / "existing.tar.gz"
    archive.write_bytes(b"prior archive bytes")

    with pytest.raises(SystemExit):
        backup.cmd_snapshot(_snapshot_args(archive))

    assert archive.read_bytes() == b"prior archive bytes"


def test_snapshot_treats_zero_length_known_store_as_sqlite(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    _patch_repo(backup, monkeypatch, repo)
    (data / "openclank.db").touch()

    archive = tmp_path / "empty-known-store.tar.gz"
    backup.cmd_snapshot(_snapshot_args(archive))

    staged = tmp_path / "staged"
    staged.mkdir()
    result = backup._stage_archive(archive, staged)
    assert result["sqlite_files"] == 1
    assert (staged / "data" / "openclank.db").read_bytes().startswith(
        b"SQLite format 3\x00"
    )


def test_snapshot_rejects_damaged_known_store_without_publishing(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    _patch_repo(backup, monkeypatch, repo)
    (data / "app.db").write_bytes(b"damaged known database")
    archive = tmp_path / "existing.tar.gz"
    archive.write_bytes(b"prior archive bytes")

    with pytest.raises(SystemExit):
        backup.cmd_snapshot(_snapshot_args(archive))

    assert archive.read_bytes() == b"prior archive bytes"
    assert list(tmp_path.glob(f".{archive.name}.*.tmp")) == []


def test_snapshot_and_verify_support_an_empty_data_directory(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    _patch_repo(backup, monkeypatch, repo)

    archive = tmp_path / "empty.tar.gz"
    backup.cmd_snapshot(_snapshot_args(archive))

    staged = tmp_path / "staged"
    staged.mkdir()
    result = backup._stage_archive(archive, staged)
    assert (staged / "data").is_dir()
    assert _tree_bytes(staged / "data") == {}
    assert result["format_version"] == 1


def test_restore_rejects_manifest_missing_member_without_touching_data(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.txt").write_bytes(b"prior")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)

    archive = tmp_path / "missing-member.tar.gz"
    _write_manifested_archive(
        backup,
        archive,
        {"present.txt": b"present"},
        {"present.txt": b"present", "missing.txt": b"declared but absent"},
    )

    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert _tree_bytes(data) == before


def test_restore_rejects_manifested_undeclared_directory_without_touching_data(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.txt").write_bytes(b"prior")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)

    archive = tmp_path / "undeclared-directory.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        _add_dir(tar, "data")
        _add_dir(tar, "data/app.db")
        _add_bytes(tar, backup._MANIFEST_NAME, backup._manifest_bytes({}))

    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert _tree_bytes(data) == before
    assert not (data / "app.db").exists()
    assert list(repo.glob("data.before-restore-*")) == []


def test_restore_rejects_corrupt_sqlite_without_touching_data(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.txt").write_bytes(b"prior")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)

    archive = tmp_path / "corrupt-db.tar.gz"
    _write_legacy_archive(
        archive,
        {"broken.db": b"SQLite format 3\x00" + (b"broken" * 30)},
    )

    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert _tree_bytes(data) == before
    assert list(repo.glob("data.before-restore-*")) == []


def test_verify_rejects_truncated_archive(tmp_path):
    backup = _load_backup_cli()
    archive = tmp_path / "truncated.tar.gz"
    _write_legacy_archive(archive, {"item.txt": b"payload" * 100})
    payload = archive.read_bytes()
    archive.write_bytes(payload[: max(1, len(payload) // 2)])

    with pytest.raises(SystemExit):
        backup.cmd_verify(_verify_args(archive))


def test_restore_requires_data_directory_without_touching_prior_tree(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.txt").write_bytes(b"prior")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)

    archive = tmp_path / "no-data.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        pass

    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert _tree_bytes(data) == before


def test_restore_rejects_portable_path_collisions_without_touching_data(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.txt").write_bytes(b"prior")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)

    archive = tmp_path / "case-collision.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        _add_bytes(tar, "data/Name.txt", b"first")
        _add_bytes(tar, "data/name.txt", b"second")

    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert _tree_bytes(data) == before
    assert list(repo.glob("data.before-restore-*")) == []


def test_restore_rejects_backslash_paths_without_touching_data(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.txt").write_bytes(b"prior")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)

    archive = tmp_path / "backslash.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        _add_bytes(tar, "data\\..\\escaped.txt", b"escaped")

    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert _tree_bytes(data) == before
    assert list(repo.glob("data.before-restore-*")) == []


def test_restore_extraction_failure_never_moves_prior_tree(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.txt").write_bytes(b"prior")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)
    archive = tmp_path / "valid.tar.gz"
    _write_legacy_archive(archive, {"new.txt": b"new"})

    def fail_extract(_tar, _members, _root):
        raise backup.BackupValidationError("injected extraction failure")

    monkeypatch.setattr(backup, "_extract_restore_members", fail_extract)
    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert _tree_bytes(data) == before
    assert list(repo.glob("data.before-restore-*")) == []


def test_restore_atomic_exchange_failure_preserves_prior_tree(tmp_path, monkeypatch):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.txt").write_bytes(b"prior")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)
    archive = tmp_path / "valid.tar.gz"
    _write_legacy_archive(archive, {"new.txt": b"new"})
    def fail_exchange(_first, _second):
        raise OSError("injected atomic exchange failure")

    monkeypatch.setattr(backup, "_exchange_paths", fail_exchange)
    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert _tree_bytes(data) == before
    assert list(repo.glob("data.before-restore-*")) == []


def test_restore_initially_absent_data_publishes_exact_candidate(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    data = repo / "data"
    _patch_repo(backup, monkeypatch, repo)
    archive = tmp_path / "valid.tar.gz"
    _write_legacy_archive(archive, {"new.txt": b"new"})

    backup.cmd_restore(_restore_args(archive))

    assert _tree_bytes(data) == {"new.txt": b"new"}
    assert list(repo.glob("data.before-restore-*")) == []
    assert list(tmp_path.glob(f".{repo.name}-restore-*")) == []


def test_restore_initially_absent_validation_failure_quarantines_candidate(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    data = repo / "data"
    _patch_repo(backup, monkeypatch, repo)
    archive = tmp_path / "valid.tar.gz"
    _write_legacy_archive(archive, {"new.txt": b"new"})
    real_validate = backup._validate_data_tree

    def fail_published_tree(path):
        result = real_validate(path)
        if path == data:
            raise backup.BackupValidationError("injected validation failure")
        return result

    monkeypatch.setattr(backup, "_validate_data_tree", fail_published_tree)
    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert not data.exists()
    failed_trees = list(repo.glob("data.failed-restore-*"))
    assert len(failed_trees) == 1
    assert _tree_bytes(failed_trees[0]) == {"new.txt": b"new"}


def test_restore_initially_absent_never_quarantines_raced_unrelated_tree(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    data = repo / "data"
    _patch_repo(backup, monkeypatch, repo)
    archive = tmp_path / "valid.tar.gz"
    _write_legacy_archive(archive, {"new.txt": b"new"})
    real_rename = backup._rename_path

    def race_publish(source, destination):
        if destination == data:
            data.mkdir()
            (data / "unrelated.txt").write_bytes(b"unrelated")
            raise OSError("injected publication race")
        real_rename(source, destination)

    monkeypatch.setattr(backup, "_rename_path", race_publish)
    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert _tree_bytes(data) == {"unrelated.txt": b"unrelated"}
    assert list(repo.glob("data.failed-restore-*")) == []


def test_restore_candidate_publish_failure_rolls_back_byte_identically(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    (data / "nested").mkdir(parents=True)
    (data / "keep.bin").write_bytes(b"prior\x00bytes")
    (data / "nested" / "other.txt").write_bytes(b"other")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)
    archive = tmp_path / "valid.tar.gz"
    _write_legacy_archive(archive, {"new.txt": b"new"})
    real_exchange = backup._exchange_paths
    injected = False

    def exchange_then_fail(first, second):
        nonlocal injected
        real_exchange(first, second)
        if not injected:
            injected = True
            raise OSError("injected post-exchange publication failure")

    monkeypatch.setattr(backup, "_exchange_paths", exchange_then_fail)
    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert injected
    assert _tree_bytes(data) == before
    assert list(repo.glob("data.before-restore-*")) == []
    failed_trees = list(repo.glob("data.failed-restore-*"))
    assert len(failed_trees) == 1
    assert _tree_bytes(failed_trees[0]) == {"new.txt": b"new"}


def test_restore_post_publish_validation_failure_rolls_back_byte_identically(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    (data / "nested").mkdir(parents=True)
    (data / "keep.bin").write_bytes(b"prior\x00bytes")
    (data / "nested" / "other.txt").write_bytes(b"other")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)
    archive = tmp_path / "valid.tar.gz"
    _write_legacy_archive(archive, {"new.txt": b"new"})
    real_validate = backup._validate_data_tree

    def fail_published_tree(path):
        result = real_validate(path)
        if path == data:
            raise backup.BackupValidationError(
                "injected post-publication validation failure"
            )
        return result

    monkeypatch.setattr(backup, "_validate_data_tree", fail_published_tree)
    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert _tree_bytes(data) == before
    assert list(repo.glob("data.before-restore-*")) == []


def test_restore_interruption_after_publish_rolls_back_byte_identically(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.bin").write_bytes(b"prior\x00bytes")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)
    archive = tmp_path / "valid.tar.gz"
    _write_legacy_archive(archive, {"new.txt": b"new"})
    real_validate = backup._validate_data_tree

    def interrupt_published_tree(path):
        result = real_validate(path)
        if path == data:
            raise KeyboardInterrupt()
        return result

    monkeypatch.setattr(backup, "_validate_data_tree", interrupt_published_tree)
    with pytest.raises(KeyboardInterrupt):
        backup.cmd_restore(_restore_args(archive))

    assert _tree_bytes(data) == before
    assert list(repo.glob("data.before-restore-*")) == []


def test_restore_system_exit_after_publish_rolls_back_byte_identically(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.bin").write_bytes(b"prior\x00bytes")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)
    archive = tmp_path / "valid.tar.gz"
    _write_legacy_archive(archive, {"new.txt": b"new"})
    real_validate = backup._validate_data_tree

    def exit_published_tree(path):
        result = real_validate(path)
        if path == data:
            raise SystemExit(23)
        return result

    monkeypatch.setattr(backup, "_validate_data_tree", exit_published_tree)
    with pytest.raises(SystemExit) as exc_info:
        backup.cmd_restore(_restore_args(archive))

    assert exc_info.value.code == 23
    assert _tree_bytes(data) == before


def test_restore_interruption_during_rollback_still_restores_prior_tree(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.bin").write_bytes(b"prior\x00bytes")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)
    archive = tmp_path / "valid.tar.gz"
    _write_legacy_archive(archive, {"new.txt": b"new"})
    real_validate = backup._validate_data_tree
    real_exchange = backup._exchange_paths
    exchange_count = 0

    def interrupt_published_tree(path):
        result = real_validate(path)
        if path == data:
            raise KeyboardInterrupt()
        return result

    def interrupt_rollback(first, second):
        nonlocal exchange_count
        exchange_count += 1
        if exchange_count == 1:
            real_exchange(first, second)
            return
        raise KeyboardInterrupt()

    monkeypatch.setattr(backup, "_validate_data_tree", interrupt_published_tree)
    monkeypatch.setattr(backup, "_exchange_paths", interrupt_rollback)
    with pytest.raises(KeyboardInterrupt):
        backup.cmd_restore(_restore_args(archive))

    assert exchange_count == 2
    assert _tree_bytes(data) == before


def test_restore_transient_fallback_rename_failure_retries_prior_tree(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.bin").write_bytes(b"prior\x00bytes")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)
    archive = tmp_path / "valid.tar.gz"
    _write_legacy_archive(archive, {"new.txt": b"new"})
    real_validate = backup._validate_data_tree
    real_exchange = backup._exchange_paths
    real_rename = backup._rename_path
    exchange_count = 0
    restore_rename_count = 0

    def fail_published_tree(path):
        result = real_validate(path)
        if path == data:
            raise backup.BackupValidationError("injected validation failure")
        return result

    def fail_rollback_exchange(first, second):
        nonlocal exchange_count
        exchange_count += 1
        if exchange_count == 1:
            real_exchange(first, second)
            return
        raise OSError("injected rollback exchange failure")

    def fail_first_restore_rename(source, destination):
        nonlocal restore_rename_count
        if destination == data and source != data:
            restore_rename_count += 1
            if restore_rename_count == 1:
                raise OSError("injected fallback rename failure")
        real_rename(source, destination)

    monkeypatch.setattr(backup, "_validate_data_tree", fail_published_tree)
    monkeypatch.setattr(backup, "_exchange_paths", fail_rollback_exchange)
    monkeypatch.setattr(backup, "_rename_path", fail_first_restore_rename)
    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert restore_rename_count == 2
    assert _tree_bytes(data) == before
    assert list(tmp_path.glob(f".{repo.name}-restore-*")) == []


def test_restore_persistent_rollback_failure_retains_exact_prior_tree(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "keep.bin").write_bytes(b"prior\x00bytes")
    _patch_repo(backup, monkeypatch, repo)
    before = _tree_bytes(data)
    archive = tmp_path / "valid.tar.gz"
    _write_legacy_archive(archive, {"new.txt": b"new"})
    real_validate = backup._validate_data_tree
    real_exchange = backup._exchange_paths
    real_rename = backup._rename_path
    exchange_count = 0

    def fail_published_tree(path):
        result = real_validate(path)
        if path == data:
            raise backup.BackupValidationError("injected validation failure")
        return result

    def fail_rollback_exchange(first, second):
        nonlocal exchange_count
        exchange_count += 1
        if exchange_count == 1:
            real_exchange(first, second)
            return
        raise OSError("injected persistent rollback exchange failure")

    def fail_restore_rename(source, destination):
        if destination == data and source != data:
            raise OSError("injected persistent fallback rename failure")
        real_rename(source, destination)

    monkeypatch.setattr(backup, "_validate_data_tree", fail_published_tree)
    monkeypatch.setattr(backup, "_exchange_paths", fail_rollback_exchange)
    monkeypatch.setattr(backup, "_rename_path", fail_restore_rename)
    with pytest.raises(SystemExit):
        backup.cmd_restore(_restore_args(archive))

    assert not data.exists()
    retained_roots = list(tmp_path.glob(f".{repo.name}-restore-*"))
    assert len(retained_roots) == 1
    assert _tree_bytes(retained_roots[0] / "data") == before
    failed_trees = list(repo.glob("data.failed-restore-*"))
    assert len(failed_trees) == 1
    assert _tree_bytes(failed_trees[0]) == {"new.txt": b"new"}


def test_restore_legacy_archive_consolidates_committed_wal_state(
    tmp_path, monkeypatch
):
    backup = _load_backup_cli()
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "old.txt").write_bytes(b"old")
    _patch_repo(backup, monkeypatch, repo)

    source_db = tmp_path / "source.db"
    conn = sqlite3.connect(source_db)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO item(value) VALUES ('committed only in wal')")
    conn.commit()
    primary_bytes = source_db.read_bytes()
    wal_bytes = Path(f"{source_db}-wal").read_bytes()
    shm_bytes = Path(f"{source_db}-shm").read_bytes()

    primary_only = tmp_path / "primary-only.db"
    primary_only.write_bytes(primary_bytes)
    probe = sqlite3.connect(primary_only)
    assert probe.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='item'"
    ).fetchone() is None
    probe.close()

    archive = tmp_path / "legacy-sidecar.tar.gz"
    _write_legacy_archive(
        archive,
        {
            "state.db": primary_bytes,
            "state.db-wal": wal_bytes,
            "state.db-shm": shm_bytes,
        },
    )
    conn.close()

    staged = tmp_path / "staged-legacy"
    staged.mkdir()
    result = backup._stage_archive(archive, staged)
    assert set(_tree_bytes(staged / "data")) == {"state.db"}
    consolidated = sqlite3.connect(staged / "data" / "state.db")
    assert consolidated.execute("SELECT value FROM item").fetchall() == [
        ("committed only in wal",)
    ]
    consolidated.close()
    assert result["consolidated_legacy_sidecars"] == 2
    assert not (staged / "data" / "state.db-wal").exists()
    assert not (staged / "data" / "state.db-shm").exists()

    backup.cmd_restore(_restore_args(archive))

    assert set(_tree_bytes(data)) == {"state.db"}
    restored = sqlite3.connect(data / "state.db")
    assert restored.execute("SELECT value FROM item").fetchall() == [
        ("committed only in wal",)
    ]
    restored.close()
    assert not (data / "state.db-wal").exists()
    assert not (data / "state.db-shm").exists()
    assert list(repo.glob("data.before-restore-*"))

import io
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from PIL import Image
import pytest

from services.memory.media_assets import MediaAssetError, MemoryMediaStore, admit_photo


def _photo(rgb=(30, 60, 90)):
    buffer = io.BytesIO()
    Image.new("RGB", (3, 2), rgb).save(buffer, format="PNG")
    return admit_photo(buffer.getvalue(), "photo.png", "image/png")


def _put(store, *, owner="alice", source_id="source-1", photo=None):
    photo = photo or _photo()
    return store.put(
        owner=owner,
        source_id=source_id,
        filename="photo.png",
        photo=photo,
        associated_text="A blue notebook.",
        provenance={"test": True},
    )


def test_default_provider_store_keeps_canonical_data_root(tmp_path):
    data_dir = tmp_path / "data"
    store = MemoryMediaStore.for_provider(
        SimpleNamespace(),
        default_db_path=str(data_dir / "frankenmemory.db"),
        default_data_dir=str(data_dir),
    )

    assert store.data_dir == data_dir.resolve()
    assert Path(store.db_path) == (data_dir / "frankenmemory.db").resolve()


def test_provider_specific_db_uses_its_own_parent_as_media_root(tmp_path):
    provider_db = tmp_path / "tenant" / "fm.sqlite"
    store = MemoryMediaStore.for_provider(
        SimpleNamespace(_fm_db_path=str(provider_db)),
        default_db_path=str(tmp_path / "default" / "fm.sqlite"),
        default_data_dir=str(tmp_path / "default"),
    )

    assert store.data_dir == provider_db.parent.resolve()


def test_database_failure_leaves_no_published_blob_or_stage(tmp_path, monkeypatch):
    store = MemoryMediaStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    photo = _photo()
    blob = store._blob_path("alice", photo.canonical_sha256)

    def fail_connect():
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(store, "_connect", fail_connect)
    with pytest.raises(MediaAssetError) as caught:
        _put(store, photo=photo)

    assert caught.value.code == "asset_store_unavailable"
    assert not blob.exists()
    assert not list(blob.parent.glob(".openclank-memory-media-stage-*"))


def test_metadata_exists_before_blob_publication_and_is_retracted_on_failure(tmp_path, monkeypatch):
    store = MemoryMediaStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    photo = _photo()
    blob = store._blob_path("alice", photo.canonical_sha256)

    def fail_publish(stage, destination):
        assert destination == blob
        assert not destination.exists()
        with store._connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM fm_v2_media_assets WHERE owner_id=? AND asset_id=?",
                ("alice", photo.asset_id),
            ).fetchone()[0] == 1
        raise OSError("publish failed")

    monkeypatch.setattr(store, "_publish_stage", fail_publish)
    with pytest.raises(MediaAssetError) as caught:
        _put(store, photo=photo)

    assert caught.value.code == "asset_publish_failed"
    assert not blob.exists()
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM fm_v2_media_assets WHERE owner_id=?",
            ("alice",),
        ).fetchone()[0] == 0


def test_same_photo_uses_distinct_owner_local_blob_paths(tmp_path):
    store = MemoryMediaStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    photo = _photo()
    _put(store, owner="alice", photo=photo)
    _put(store, owner="bob", photo=photo)

    alice = store._blob_path("alice", photo.canonical_sha256)
    bob = store._blob_path("bob", photo.canonical_sha256)
    assert alice != bob
    assert alice.read_bytes() == bob.read_bytes() == photo.bytes


def test_blob_key_is_not_an_alternate_path_authority(tmp_path):
    store = MemoryMediaStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    result = _put(store)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    with store._connect() as conn:
        conn.execute(
            "UPDATE fm_v2_media_assets SET blob_key=? WHERE owner_id=? AND asset_id=?",
            (str(outside), "alice", result["asset_id"]),
        )
        conn.commit()

    with pytest.raises(MediaAssetError) as caught:
        store.read_blob("alice", result["asset_id"])

    assert caught.value.code == "asset_path_invalid"


def test_symlink_blob_and_forgotten_asset_are_not_readable(tmp_path):
    store = MemoryMediaStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    photo = _photo()
    result = _put(store, photo=photo)
    blob = store._blob_path("alice", photo.canonical_sha256)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(photo.bytes)
    blob.unlink()
    try:
        os.symlink(outside, blob)
    except (AttributeError, NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(MediaAssetError) as symlinked:
        store.read_blob("alice", result["asset_id"])
    assert symlinked.value.code == "asset_path_invalid"

    blob.unlink()
    blob.write_bytes(photo.bytes)
    with store._connect() as conn:
        conn.execute(
            "UPDATE fm_v2_media_assets SET state='forgotten' WHERE owner_id=? AND asset_id=?",
            ("alice", result["asset_id"]),
        )
        conn.commit()

    assert store.list_assets("alice") == []
    with pytest.raises(MediaAssetError) as forgotten:
        store.read_blob("alice", result["asset_id"])
    assert forgotten.value.code == "asset_not_found"


def test_owner_directory_symlink_cannot_redirect_publication(tmp_path):
    store = MemoryMediaStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    photo = _photo()
    owner_directory = store._blob_path("alice", photo.canonical_sha256).parent
    owner_directory.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, owner_directory)
    except (AttributeError, NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(MediaAssetError) as caught:
        _put(store, photo=photo)

    assert caught.value.code == "asset_path_invalid"
    assert list(outside.iterdir()) == []


def test_media_root_symlink_cannot_redirect_publication(tmp_path):
    store = MemoryMediaStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, tmp_path / "memory_media")
    except (AttributeError, NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(MediaAssetError) as caught:
        _put(store)

    assert caught.value.code == "asset_path_invalid"
    assert list(outside.iterdir()) == []


def test_owner_purge_is_preview_bound_and_never_removes_foreign_blob(tmp_path):
    store = MemoryMediaStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    shared = _photo()
    _put(store, owner="alice", photo=shared)
    bob = _put(store, owner="bob", photo=shared)
    stale = store.preview_owner_purge("alice")

    second = _photo((200, 20, 40))
    _put(store, owner="alice", source_id="source-2", photo=second)
    with pytest.raises(MediaAssetError) as conflict:
        store.purge_owner("alice", expected=stale)
    assert conflict.value.code == "asset_purge_conflict"
    assert len(store.list_assets("alice")) == 2

    current = store.preview_owner_purge("alice")
    result = store.purge_owner("alice", expected=current)
    assert result["complete"] is True
    assert store.list_assets("alice") == []
    assert not store._blob_path("alice", shared.canonical_sha256).exists()
    assert not store._blob_path("alice", second.canonical_sha256).exists()

    metadata, data = store.read_blob("bob", bob["asset_id"])
    assert metadata["asset_id"] == bob["asset_id"]
    assert data == shared.bytes

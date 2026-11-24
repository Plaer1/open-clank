"""Crash-reconcilable Memory media and import-staging owner moves."""

import os
from io import BytesIO

import pytest
from PIL import Image

from services.memory.import_batch import ImportBatchError, MemoryImportBatchStore
from services.memory.media_assets import MediaAssetError, MemoryMediaStore, admit_photo


def _photo(color: str):
    buffer = BytesIO()
    Image.new("RGB", (3, 2), color=color).save(buffer, format="PNG")
    return admit_photo(buffer.getvalue(), "photo.png", "image/png")


def _put(store: MemoryMediaStore, owner: str, color: str):
    photo = _photo(color)
    result = store.put(
        owner=owner,
        source_id=f"source-{owner}",
        filename=f"{owner}.png",
        photo=photo,
        associated_text=f"caption for {owner}",
        provenance={"source": "isolated-test"},
    )
    return result, photo.bytes


def test_media_owner_move_preserves_bytes_text_and_other_owner(tmp_path):
    store = MemoryMediaStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    alice, alice_bytes = _put(store, "alice", "red")
    bob, bob_bytes = _put(store, "bob", "blue")

    receipt = store.rename_owner("ALICE", "alice2")

    assert receipt["complete"] is True
    assert receipt["assets"] == 1
    assert receipt["representations"] == 1
    assert store.list_assets("alice") == []
    renamed = store.list_assets("alice2")
    assert renamed[0]["representations"][0]["text"] == "caption for alice"
    assert store.read_blob("alice2", alice["asset_id"])[1] == alice_bytes
    assert store.read_blob("bob", bob["asset_id"])[1] == bob_bytes


def test_media_owner_move_recovers_directory_effect_before_db_checkpoint(tmp_path):
    store = MemoryMediaStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    asset, expected = _put(store, "alice", "green")
    source = store._owner_blob_directory("alice")
    target = store._owner_blob_directory("alice2")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)

    receipt = store.rename_owner("alice", "alice2")

    assert receipt["complete"] is True
    assert store.list_assets("alice") == []
    assert store.read_blob("alice2", asset["asset_id"])[1] == expected


def test_media_owner_move_rejects_split_principals(tmp_path):
    store = MemoryMediaStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    _put(store, "alice", "red")
    _put(store, "alice2", "blue")

    with pytest.raises(MediaAssetError, match="Both photo owners"):
        store.rename_owner("alice", "alice2")

    assert len(store.list_assets("alice")) == 1
    assert len(store.list_assets("alice2")) == 1


def test_media_owner_reconcile_rejects_target_rows_without_bytes(tmp_path):
    store = MemoryMediaStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    _put(store, "alice2", "blue")
    directory = store._owner_blob_directory("alice2")
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()

    with pytest.raises(MediaAssetError, match="target photo bytes are missing"):
        store.rename_owner("alice", "alice2")


def _staged(store: MemoryImportBatchStore, owner: str, marker: str):
    batch_id = "batch_" + marker * 32
    item_id = "item_" + marker * 32
    store.stage_bytes(owner, batch_id, item_id, marker.encode("ascii"))
    return batch_id, item_id


def test_import_staging_owner_move_is_atomic_and_tenant_safe(tmp_path):
    store = MemoryImportBatchStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    alice_batch, alice_item = _staged(store, "alice", "a")
    bob_batch, bob_item = _staged(store, "bob", "b")
    expected = store.preview_owner_staging("alice")

    receipt = store.rename_owner_staging(
        "alice",
        "alice2",
        expected_source=expected,
        expected_target=store.preview_owner_staging("alice2"),
    )

    assert receipt["complete"] is True
    assert store.preview_owner_staging("alice")["count"] == 0
    assert store.read_staged("alice2", alice_batch, alice_item) == b"a"
    assert store.read_staged("bob", bob_batch, bob_item) == b"b"


def test_import_staging_owner_move_recovers_effect_before_checkpoint(tmp_path):
    store = MemoryImportBatchStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    batch_id, item_id = _staged(store, "alice", "c")
    expected_source = store.preview_owner_staging("alice")
    expected_target = store.preview_owner_staging("alice2")
    parent = tmp_path / "memory_import_staging"
    os.replace(
        parent / store._stage_root("alice", batch_id).parent.name,
        parent / store._stage_root("alice2", batch_id).parent.name,
    )

    receipt = store.rename_owner_staging(
        "alice",
        "alice2",
        expected_source=expected_source,
        expected_target=expected_target,
    )

    assert receipt["already_applied"] is True
    assert store.read_staged("alice2", batch_id, item_id) == b"c"


def test_import_staging_owner_move_rejects_split_principals(tmp_path):
    store = MemoryImportBatchStore(str(tmp_path / "fm.sqlite"), str(tmp_path))
    _staged(store, "alice", "d")
    _staged(store, "alice2", "e")

    with pytest.raises(ImportBatchError, match="Both import owners"):
        store.rename_owner_staging("alice", "alice2")

    assert store.preview_owner_staging("alice")["count"] == 1
    assert store.preview_owner_staging("alice2")["count"] == 1

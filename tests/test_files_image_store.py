from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from core.database import Base, FilesImageResource
from src.openclank.files_image_store import FilesImageError, FilesImageStore
from src.openclank.files_facade import ProviderContext
from src.openclank.files_managed_providers import GalleryFilesProvider


@pytest.fixture
def store(tmp_path):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return FilesImageStore(sessionmaker(bind=engine), blob_root=tmp_path / "blobs")


def test_nested_import_replay_isolation_and_collision(store):
    gallery = store.ensure_gallery("alice")
    nested = store.create_folder("alice", parent_id=gallery.id, name="Work")
    first = store.import_image("alice", parent_id=nested.id, name="picture.png", data=b"png", operation_key="op-1")
    replay = store.import_image("alice", parent_id=nested.id, name="picture.png", data=b"png", operation_key="op-1")
    collision = store.import_image("alice", parent_id=nested.id, name="picture.png", data=b"other", operation_key="op-2")
    assert first.id == replay.id
    assert collision.id != first.id
    assert collision.display_name == "picture (2).png"
    with pytest.raises(FilesImageError, match="unavailable"):
        store.move("bob", first.id, parent_id=None, name="stolen.png", expected_revision=1)


def test_move_preserves_identity_and_rejects_cycles_and_stale_revision(store):
    root = store.ensure_gallery("alice")
    parent = store.create_folder("alice", parent_id=root.id, name="Parent")
    child = store.create_folder("alice", parent_id=parent.id, name="Child")
    image = store.import_image("alice", parent_id=child.id, name="x.png", data=b"x", operation_key="move-1")
    moved = store.move("alice", image.id, parent_id=parent.id, name="renamed.png", expected_revision=1)
    assert moved.id == image.id and moved.revision == 2
    with pytest.raises(FilesImageError, match="cycle"):
        store.move("alice", parent.id, parent_id=child.id, name="Parent", expected_revision=1)
    with pytest.raises(FilesImageError, match="stale"):
        store.move("alice", image.id, parent_id=child.id, name="x.png", expected_revision=1)


def test_source_binding_and_private_locator_are_bounded(store):
    root = store.ensure_gallery("alice")
    image = store.import_image(
        "alice", parent_id=root.id, name="generated.webp", data=b"bytes",
        mime_type="image/webp", operation_key="generated-1",
        source_provider="gallery", source_resource_id="image:legacy",
        provenance={"prompt": "synthetic", "model": "fixture"},
    )
    assert image.source_provider == "gallery"
    assert image.source_resource_id == "image:legacy"
    assert image.locator and not Path(image.locator).is_absolute()
    assert store.ref(image) == f"image:{image.id}"


def test_operation_keys_are_owner_scoped_and_failed_commit_cleans_published_bytes(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    alice = FilesImageStore(factory, blob_root=tmp_path / "blobs")
    bob = FilesImageStore(factory, blob_root=tmp_path / "blobs")
    aroot = alice.ensure_photos("alice")
    broot = bob.ensure_photos("bob")
    a = alice.import_image("alice", parent_id=aroot.id, name="a.png", data=b"a", operation_key="same")
    b = bob.import_image("bob", parent_id=broot.id, name="b.png", data=b"b", operation_key="same")
    assert a.id != b.id

    class FailingSession:
        def __init__(self): self.inner = factory()
        def __getattr__(self, key): return getattr(self.inner, key)
        def commit(self): raise RuntimeError("injected commit failure")
        def close(self): self.inner.close()

    failed = FilesImageStore(lambda: FailingSession(), blob_root=tmp_path / "failed")
    with pytest.raises(RuntimeError, match="commit failure"):
        failed.import_image("alice", parent_id=aroot.id, name="failed.png", data=b"failed", operation_key="failed")
    assert not list((tmp_path / "failed").glob("files-image-*"))


def test_builtin_gallery_and_photos_are_singletons_under_concurrent_sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'race.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    def ensure_builtins(_):
        store = FilesImageStore(factory, blob_root=tmp_path / "blobs")
        gallery = store.ensure_gallery("alice")
        photos = store.ensure_photos("alice")
        return gallery.id, photos.id

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(ensure_builtins, range(8)))
    assert len({gallery_id for gallery_id, _ in results}) == 1
    assert len({photos_id for _, photos_id in results}) == 1
    db = factory()
    try:
        assert db.query(FilesImageResource).filter(
            FilesImageResource.owner == "alice",
            FilesImageResource.kind == "folder",
            FilesImageResource.display_name == "Gallery",
            FilesImageResource.parent_id.is_(None),
        ).count() == 1
        assert db.query(FilesImageResource).filter(
            FilesImageResource.owner == "alice",
            FilesImageResource.kind == "folder",
            FilesImageResource.display_name == "Photos",
        ).count() == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_gallery_provider_browses_files_owned_rows_and_content(store, tmp_path):
    photos = store.ensure_photos("alice")
    image = store.import_image("alice", parent_id=photos.id, name="nested.png", data=b"pixels", mime_type="image/png", operation_key="browse")
    provider = GalleryFilesProvider(session_factory=store.session_factory, image_resolver=lambda name: tmp_path / name)
    (tmp_path / image.locator).write_bytes(b"pixels")
    context = ProviderContext("alice", "alice", 1)
    page = await provider.children(context, parent_origin_id="photos", cursor=None, snapshot=None, limit=10, sort={"key":"name", "direction":"asc"}, query="")
    assert [entry.origin_id for entry in page.entries] == [f"image:{image.id}"]
    stat = await provider.stat(context, origin_id=f"image:{image.id}")
    assert stat.provenance["files_owned"] is True
    content = await provider.content(context, origin_id=f"image:{image.id}")
    assert content.media_type == "image/png"
    other = ProviderContext("bob", "bob", 1)
    with pytest.raises(Exception):
        await provider.stat(other, origin_id=f"image:{image.id}")

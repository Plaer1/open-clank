from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, FilesImageResource, Session
from src import session_image_cleanup
from src.openclank.files_image_store import FilesImageStore


def test_cleanup_detaches_one_session_and_preserves_shared_files_image(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cleanup.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    blobs = tmp_path / "blobs"
    store = FilesImageStore(factory, blob_root=blobs)
    root = store.ensure_photos("alice")
    image = store.import_image(
        "alice", parent_id=root.id, name="shared.png", data=b"shared",
        provenance={"session_ids": ["chat-1", "chat-2"]},
    )
    with factory() as db:
        db.add_all([
            Session(id="chat-1", name="one", endpoint_url="http://local", model="image", owner="alice"),
            Session(id="chat-2", name="two", endpoint_url="http://local", model="image", owner="alice"),
        ])
        db.commit()
        assert session_image_cleanup.session_image_refs(db, "chat-1", "alice") == {image.id}
    assert session_image_cleanup.retire_session_image_refs("chat-1", "alice", {image.id}) == 0
    assert store.image("alice", image.id).provenance["session_ids"] == ["chat-2"]
    assert session_image_cleanup.retire_session_image_refs("chat-2", "alice", {image.id}) == 1
    with factory() as db:
        assert db.get(FilesImageResource, image.id).is_active is False
    assert not (blobs / image.locator).exists()

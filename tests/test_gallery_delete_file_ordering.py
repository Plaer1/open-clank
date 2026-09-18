"""Regression: deleting a gallery image must not remove the file before the DB
commit succeeds.

delete_gallery_image() removed the on-disk file first and only then set
is_active=False and committed. If that commit failed and rolled back, the record
stayed active but its file was already gone — a broken, unviewable image (data
loss). The file is now removed only after the soft-delete commit succeeds, and
best-effort so a missing/locked file can't fail an otherwise-successful delete.
"""
import asyncio
import json

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.database import Base, ChatMessage, GalleryImage, Session
import routes.gallery_routes as gallery_routes


def _delete_endpoint():
    router = gallery_routes.setup_gallery_routes()
    for route in router.routes:
        if getattr(route, "path", "") == "/api/gallery/{image_id}" and "DELETE" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError("DELETE /api/gallery/{image_id} endpoint not found")


def _seed(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    db.add(GalleryImage(id="img-1", filename="x.png", owner="alice", is_active=True))
    db.commit()
    db.close()
    img_dir = tmp_path / "data" / "generated_images"
    img_dir.mkdir(parents=True)
    (img_dir / "x.png").write_bytes(b"image-bytes")
    return SessionLocal


def test_file_kept_when_commit_fails(tmp_path, monkeypatch):
    SessionLocal = _seed(tmp_path)
    # GALLERY_IMAGE_DIR is an absolute path fixed at import, so a chdir can't
    # redirect the delete; point the resolver at the seeded tmp dir directly.
    monkeypatch.setattr(gallery_routes, "GALLERY_IMAGE_DIR", tmp_path / "data" / "generated_images")
    monkeypatch.setattr(gallery_routes, "get_current_user", lambda r: "alice")

    # A session whose commit always fails, to simulate a DB error mid-delete.
    sess = SessionLocal()

    def _boom():
        raise RuntimeError("commit failed")

    monkeypatch.setattr(sess, "commit", _boom)
    monkeypatch.setattr(gallery_routes, "SessionLocal", lambda: sess)

    delete = _delete_endpoint()
    with pytest.raises(HTTPException):
        asyncio.run(delete(Request(scope={"type": "http"}), "img-1"))

    # File must survive a failed commit — the record is still active after rollback.
    assert (tmp_path / "data" / "generated_images" / "x.png").exists()
    check = SessionLocal()
    row = check.query(GalleryImage).filter(GalleryImage.id == "img-1").first()
    assert row.is_active is True
    check.close()


def test_file_removed_on_successful_delete(tmp_path, monkeypatch):
    SessionLocal = _seed(tmp_path)
    monkeypatch.setattr(gallery_routes, "GALLERY_IMAGE_DIR", tmp_path / "data" / "generated_images")
    monkeypatch.setattr(gallery_routes, "get_current_user", lambda r: "alice")
    monkeypatch.setattr(gallery_routes, "SessionLocal", SessionLocal)

    delete = _delete_endpoint()
    result = asyncio.run(delete(Request(scope={"type": "http"}), "img-1"))

    assert result["status"] == "deleted"
    assert not (tmp_path / "data" / "generated_images" / "x.png").exists()
    check = SessionLocal()
    row = check.query(GalleryImage).filter(GalleryImage.id == "img-1").first()
    assert row.is_active is False
    check.close()


def test_replacement_restores_previous_bytes_when_metadata_commit_fails(tmp_path, monkeypatch):
    SessionLocal = _seed(tmp_path)
    image_dir = tmp_path / "data" / "generated_images"
    monkeypatch.setattr(gallery_routes, "GALLERY_IMAGE_DIR", image_dir)
    session = SessionLocal()
    image = session.query(GalleryImage).filter_by(id="img-1").first()
    image.file_size = len(b"replacement")

    def _boom():
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)
    with pytest.raises(HTTPException) as caught:
        gallery_routes._commit_gallery_replacement(
            session,
            filename="x.png",
            content=b"replacement",
            error_message="Image update failed",
        )

    assert caught.value.status_code == 500
    assert (image_dir / "x.png").read_bytes() == b"image-bytes"
    assert not list(image_dir.glob(".openclank-gallery-stage-*"))
    session.close()


def test_upload_database_failure_never_publishes_routable_bytes(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'gallery.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    failing = session_factory()

    def _boom():
        raise RuntimeError("commit failed")

    monkeypatch.setattr(failing, "commit", _boom)
    image_dir = tmp_path / "generated_images"
    monkeypatch.setattr(gallery_routes, "GALLERY_IMAGE_DIR", image_dir)
    monkeypatch.setattr(gallery_routes, "SessionLocal", lambda: failing)
    monkeypatch.setattr(gallery_routes, "get_current_user", lambda request: "alice")
    app = FastAPI()
    app.include_router(gallery_routes.setup_gallery_routes())

    response = TestClient(app).post(
        "/api/gallery/upload",
        files={"file": ("photo.png", b"not-a-real-png", "image/png")},
    )

    assert response.status_code == 500
    assert not list(image_dir.glob("*.png"))
    assert not list(image_dir.glob(".openclank-gallery-stage-*"))
    with session_factory() as check:
        assert check.query(GalleryImage).count() == 0


def test_delete_chat_cleanup_never_touches_a_foreign_owner_session(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    image_dir = tmp_path / "generated_images"
    image_dir.mkdir()
    (image_dir / "same.png").write_bytes(b"alice image")
    matching = {
        "image_id": "img-alice",
        "image_url": "/api/generated-image/same.png",
    }
    other = {"type": "status", "message": "keep me"}
    with session_factory() as db:
        db.add_all(
            [
                Session(
                    id="session-alice",
                    name="Alice",
                    endpoint_url="http://local",
                    model="test",
                    owner="alice",
                ),
                Session(
                    id="session-bob",
                    name="Bob",
                    endpoint_url="http://local",
                    model="test",
                    owner="bob",
                ),
                GalleryImage(
                    id="img-alice",
                    filename="same.png",
                    owner="alice",
                    is_active=True,
                ),
                ChatMessage(
                    id="message-alice",
                    session_id="session-alice",
                    role="assistant",
                    content="generated",
                    meta_data=json.dumps({"tool_events": [matching, other]}),
                ),
                ChatMessage(
                    id="message-bob",
                    session_id="session-bob",
                    role="assistant",
                    content="foreign reference",
                    meta_data=json.dumps({"tool_events": [matching, other]}),
                ),
            ]
        )
        db.commit()

    monkeypatch.setattr(gallery_routes, "GALLERY_IMAGE_DIR", image_dir)
    monkeypatch.setattr(gallery_routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(gallery_routes, "SessionLocal", session_factory)
    result = asyncio.run(
        _delete_endpoint()(Request(scope={"type": "http"}), "img-alice")
    )
    assert result["status"] == "deleted"

    with session_factory() as db:
        alice = json.loads(db.query(ChatMessage).filter_by(id="message-alice").one().meta_data)
        bob = json.loads(db.query(ChatMessage).filter_by(id="message-bob").one().meta_data)
    assert alice["tool_events"] == [other]
    assert bob["tool_events"] == [matching, other]

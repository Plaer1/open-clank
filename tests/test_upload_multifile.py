"""Regression tests for issue #1346 — attaching more than one file at once made
the model "not even see" the attachments.

Root cause: the per-IP concurrency guard in routes/upload_routes.py summed its
condition over `files`, and the condition didn't depend on the loop variable, so
it collapsed to `len(files)` whenever the IP had any recent upload. A multi-file
batch sent right after a single upload (the reporter's exact flow) therefore
counted itself as N concurrent uploads and tripped `max_concurrent_uploads`,
returning 429. The browser swallowed the 429 (no `files` in the body) and sent
the chat message with no attachments.

The fix counts genuine recent upload *events*, independent of the current
batch's file count. save_upload still enforces the per-minute rate limit.
"""
import io
import re
import types
from pathlib import Path

import pytest
from fastapi import APIRouter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import FilesImageResource
from src import generated_images
from src.upload_handler import count_recent_uploads, UploadHandler
import routes.upload_routes as up

_REPO = Path(__file__).resolve().parent.parent


def test_count_recent_uploads_ignores_batch_size():
    now = 1_000.0
    # No prior uploads -> zero, regardless of how big the incoming batch is.
    assert count_recent_uploads([], now) == 0
    # Only events inside the window are counted.
    assert count_recent_uploads([now - 1, now - 2, now - 3], now, window=10) == 3
    assert count_recent_uploads([now - 1, now - 50], now, window=10) == 1
    assert count_recent_uploads([now - 11], now, window=10) == 0


def _fake_handler():
    h = types.SimpleNamespace()
    h.upload_rate_log = {}
    h.max_concurrent_uploads = 3

    def save_upload(u, client_ip, owner=None):
        # Mimic the real handler: every saved file logs a timestamp.
        h.upload_rate_log.setdefault(client_ip, []).append(_NOW)
        name = getattr(u, "filename", "f")
        return {
            "id": "0" * 32 + "." + "txt",
            "name": name,
            "mime": "text/plain",
            "size": 1,
            "hash": "h",
            "uploaded_at": "now",
            "width": None,
            "height": None,
            "is_duplicate": False,
        }

    h.save_upload = save_upload
    return h


_NOW = 5_000.0


def _endpoint(router):
    for r in router.routes:
        if getattr(r, "path", None) == "/api/upload" and "POST" in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError("upload endpoint not found")


def _request(ip="1.2.3.4", user="tester"):
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=ip),
        state=types.SimpleNamespace(current_user=user),
    )


def _files(n):
    return [types.SimpleNamespace(filename=f"f{i}.txt") for i in range(n)]


def _image_upload(name="photo.png", content=b"not really png but enough for route metadata"):
    return types.SimpleNamespace(filename=name, file=io.BytesIO(content))


@pytest.fixture(autouse=True)
def _reset_router(monkeypatch):
    # Module-level router accumulates routes across setup calls; reset it.
    monkeypatch.setattr(up, "router", APIRouter(prefix="/api/upload", tags=["upload"]))
    # Freeze time so the seeded "recent upload" is deterministic.
    monkeypatch.setattr(up.time, "time", lambda: _NOW)


async def test_multifile_after_a_recent_upload_is_not_rejected():
    """The bug: one prior upload + a 3-file batch -> 429. Must now succeed."""
    h = _fake_handler()
    h.upload_rate_log["1.2.3.4"] = [_NOW - 1]  # step 1: a single file moments ago
    up.setup_upload_routes(h)
    endpoint = _endpoint(up.router)

    result = await endpoint(_request(), _files(3))

    assert [f["name"] for f in result["files"]] == ["f0.txt", "f1.txt", "f2.txt"]


async def test_fresh_multifile_upload_succeeds():
    h = _fake_handler()
    up.setup_upload_routes(h)
    endpoint = _endpoint(up.router)

    result = await endpoint(_request(), _files(5))

    assert len(result["files"]) == 5


async def test_genuine_recent_volume_still_throttled():
    """The guard is preserved: enough genuine recent uploads still 429s."""
    from fastapi import HTTPException

    h = _fake_handler()
    h.upload_rate_log["1.2.3.4"] = [_NOW - 1, _NOW - 2, _NOW - 3]  # 3 recent events
    up.setup_upload_routes(h)
    endpoint = _endpoint(up.router)

    with pytest.raises(HTTPException) as ei:
        await endpoint(_request(), _files(1))
    assert ei.value.status_code == 429


# ── #1346 follow-up: the per-minute rate limit must not reject a single
# full multi-file batch. The reporter found "5 attachments work, 6 fail":
# save_upload() counts each file against upload_rate_limit, which was 5 while
# the composer allows MAX_FILES=10. ──────────────────────────────────────────

def _max_files_from_frontend() -> int:
    src = (_REPO / "static/js/fileHandler.js").read_text(encoding="utf-8")
    m = re.search(r"MAX_FILES\s*=\s*(\d+)", src)
    assert m, "MAX_FILES not found in fileHandler.js"
    return int(m.group(1))


def test_rate_limit_accommodates_a_full_batch():
    # The per-minute file cap must comfortably exceed the frontend batch cap,
    # or a single legitimate multi-file attach trips it (issue #1346).
    h = UploadHandler.__new__(UploadHandler)
    UploadHandler.__init__(h, base_dir="/tmp", upload_dir="/tmp/_odysseus_test_uploads_cfg")
    assert h.upload_rate_limit >= _max_files_from_frontend()


def test_six_file_batch_is_not_rate_limited(tmp_path):
    from fastapi import HTTPException

    h = UploadHandler(base_dir=str(tmp_path), upload_dir=str(tmp_path / "uploads"))
    saved = 0
    for i in range(6):
        u = types.SimpleNamespace(
            file=io.BytesIO(f"file number {i} unique content".encode()),
            filename=f"f{i}.txt",
        )
        try:
            meta = h.save_upload(u, client_ip="9.9.9.9", owner="tester")
        except HTTPException as e:
            raise AssertionError(f"file {i} rejected with {e.status_code}: {e.detail}")
        assert meta and meta.get("id")
        saved += 1
    assert saved == 6


async def test_chat_image_upload_is_added_to_gallery(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'gallery.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    gallery_dir = tmp_path / "generated_images"

    monkeypatch.setattr(up, "SessionLocal", TestingSession)
    monkeypatch.setattr(generated_images, "GENERATED_IMAGE_DIR", gallery_dir)

    h = UploadHandler(base_dir=str(tmp_path), upload_dir=str(tmp_path / "uploads"))
    up.setup_upload_routes(h)
    endpoint = _endpoint(up.router)

    result = await endpoint(_request(user="alice"), [_image_upload()])
    uploaded = result["files"][0]

    assert uploaded["image_id"]
    db = TestingSession()
    try:
        image = db.query(FilesImageResource).filter(FilesImageResource.id == uploaded["image_id"]).one()
        assert image.owner == "alice"
        assert image.provenance["model"] == "chat-upload"
        assert image.provenance["prompt"] == "photo.png"
        assert image.digest == uploaded["hash"]
        assert (gallery_dir / image.locator).exists()
    finally:
        db.close()


async def test_non_image_chat_upload_is_not_added_to_gallery(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'gallery.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(up, "SessionLocal", TestingSession)
    monkeypatch.setattr(
        generated_images,
        "GENERATED_IMAGE_DIR",
        tmp_path / "generated_images",
    )

    h = UploadHandler(base_dir=str(tmp_path), upload_dir=str(tmp_path / "uploads"))
    up.setup_upload_routes(h)
    endpoint = _endpoint(up.router)

    result = await endpoint(_request(user="alice"), [types.SimpleNamespace(
        filename="notes.txt",
        file=io.BytesIO(b"plain text upload"),
    )])

    assert "image_id" not in result["files"][0]
    db = TestingSession()
    try:
        assert db.query(FilesImageResource).count() == 0
    finally:
        db.close()


def _files_image(**values):
    """Build a Files-owned resource from legacy fixture metadata."""
    filename = values.pop("filename", values.pop("name", "image"))
    parent_id = values.pop("album_id", values.pop("parent_id", None))
    prompt = values.pop("prompt", None)
    model = values.pop("model", None)
    file_hash = values.pop("file_hash", values.pop("digest", None))
    size = values.pop("file_size", values.pop("size", 0))
    provenance = dict(values.pop("provenance", {}) or {})
    for key, value in (("prompt", prompt), ("model", model)):
        if value is not None:
            provenance[key] = value
    is_folder = not filename or ("name" in values and parent_id is None)
    return FilesImageResource(
        id=values.pop("id"), owner=values.pop("owner", "alice"),
        kind="folder" if is_folder else "image", parent_id=parent_id,
        display_name=filename, locator=values.pop("locator", filename),
        digest=file_hash, size=size, mime_type=values.pop("mime_type", None),
        favorite=values.pop("favorite", False), is_active=values.pop("is_active", True),
        provenance=provenance or None, **values,
    )

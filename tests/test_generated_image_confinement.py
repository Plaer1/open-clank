import os
from pathlib import Path

import pytest
from fastapi import HTTPException


def _generated_images_module():
    from src import generated_images
    return generated_images


def test_generated_image_path_allows_safe_existing_file(tmp_path, monkeypatch):
    generated_images = _generated_images_module()
    image_dir = tmp_path / "generated_images"
    image_dir.mkdir()
    filename = "a" * 12 + ".png"
    image_path = image_dir / filename
    image_path.write_bytes(b"png")
    monkeypatch.setattr(generated_images, "GENERATED_IMAGE_DIR", image_dir)

    assert generated_images.resolve_generated_image_path(filename) == image_path


@pytest.mark.parametrize("filename", ["../../secret.png", "zzzzzzzz.png", "aaaaaaa.png", None, 12345])
def test_generated_image_path_rejects_invalid_filenames(tmp_path, monkeypatch, filename):
    generated_images = _generated_images_module()
    image_dir = tmp_path / "generated_images"
    image_dir.mkdir()
    monkeypatch.setattr(generated_images, "GENERATED_IMAGE_DIR", image_dir)

    with pytest.raises(HTTPException) as exc:
        generated_images.resolve_generated_image_path(filename)

    assert exc.value.status_code == 400


def test_generated_image_path_rejects_symlink_escape(tmp_path, monkeypatch):
    generated_images = _generated_images_module()
    image_dir = tmp_path / "generated_images"
    image_dir.mkdir()
    filename = "b" * 12 + ".png"
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside image root")
    try:
        os.symlink(outside, image_dir / filename)
    except (AttributeError, NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    monkeypatch.setattr(generated_images, "GENERATED_IMAGE_DIR", image_dir)

    with pytest.raises(HTTPException) as exc:
        generated_images.resolve_generated_image_path(filename)

    assert exc.value.status_code == 400


def test_generated_image_headers_include_nosniff():
    generated_images = _generated_images_module()

    assert generated_images.GENERATED_IMAGE_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert (
        generated_images.GENERATED_IMAGE_HEADERS["Cache-Control"]
        == "private, no-store"
    )
    assert generated_images.GENERATED_IMAGE_HEADERS["Pragma"] == "no-cache"


def test_service_worker_bypasses_api_before_any_cache_handler():
    source = Path("static/sw.js").read_text(encoding="utf-8")
    fetch_handler = source.index("self.addEventListener('fetch'")
    api_guard = source.index(
        "if (url.pathname.startsWith('/api/') || e.request.method !== 'GET') return;",
        fetch_handler,
    )
    first_cache_handler = source.index("e.respondWith(", fetch_handler)

    assert api_guard < first_cache_handler


def test_generated_image_route_uses_confining_resolver():
    source = Path("app.py").read_text(encoding="utf-8")

    assert 'Path("data/generated_images") / filename' not in source
    assert "resolve_generated_image_path(filename)" in source
    assert "headers=GENERATED_IMAGE_HEADERS" in source


def test_auth_disabled_gallery_owner_is_explicit(monkeypatch):
    generated_images = _generated_images_module()
    monkeypatch.setenv("AUTH_ENABLED", "false")

    assert generated_images.gallery_owner_key(None) == "local-installation"
    assert generated_images.gallery_owner_key("") == "local-installation"
    assert generated_images.gallery_owner_key("alice") == "alice"


def test_auth_enabled_missing_gallery_owner_stays_missing(monkeypatch):
    generated_images = _generated_images_module()
    monkeypatch.setenv("AUTH_ENABLED", "true")

    assert generated_images.gallery_owner_key(None) is None


def test_staged_gallery_bytes_are_not_routable_before_publish(tmp_path, monkeypatch):
    generated_images = _generated_images_module()
    image_dir = tmp_path / "generated_images"
    monkeypatch.setattr(generated_images, "GENERATED_IMAGE_DIR", image_dir)

    staged = generated_images.stage_gallery_image_bytes(b"private image")
    assert staged.exists()
    with pytest.raises(HTTPException):
        generated_images.resolve_generated_image_path(staged.name)
    with pytest.raises(HTTPException):
        generated_images.resolve_gallery_image_path(
            staged.name,
            require_exists=True,
        )

    filename = "a" * 12 + ".png"
    published = generated_images.publish_staged_gallery_image(staged, filename)
    assert published.read_bytes() == b"private image"
    assert not staged.exists()


def test_new_gallery_publication_rolls_back_when_directory_sync_fails(
    tmp_path,
    monkeypatch,
):
    generated_images = _generated_images_module()
    image_dir = tmp_path / "generated_images"
    monkeypatch.setattr(generated_images, "GENERATED_IMAGE_DIR", image_dir)
    staged = generated_images.stage_gallery_image_bytes(b"private image")

    def fail_sync(_root):
        raise OSError("disk sync failed")

    monkeypatch.setattr(generated_images, "_fsync_gallery_root", fail_sync)
    filename = "c" * 12 + ".png"

    with pytest.raises(OSError, match="disk sync failed"):
        generated_images.publish_staged_gallery_image(staged, filename)

    assert staged.read_bytes() == b"private image"
    assert not (image_dir / filename).exists()


def test_gallery_replacement_restores_prior_bytes_when_directory_sync_fails(
    tmp_path,
    monkeypatch,
):
    generated_images = _generated_images_module()
    image_dir = tmp_path / "generated_images"
    image_dir.mkdir()
    monkeypatch.setattr(generated_images, "GENERATED_IMAGE_DIR", image_dir)
    filename = "d" * 12 + ".png"
    destination = image_dir / filename
    destination.write_bytes(b"old image")
    staged = generated_images.stage_gallery_image_bytes(b"new image")

    def fail_sync(_root):
        raise OSError("disk sync failed")

    monkeypatch.setattr(generated_images, "_fsync_gallery_root", fail_sync)

    with pytest.raises(OSError, match="disk sync failed"):
        generated_images.publish_staged_gallery_image(
            staged,
            filename,
            replace=True,
        )

    assert destination.read_bytes() == b"old image"
    assert not staged.exists()
    assert not list(image_dir.glob(".openclank-gallery-backup-*"))


class _ProvenanceQuery:
    def __init__(self, row):
        self.row = row

    def filter(self, *_conditions):
        return self

    def first(self):
        if isinstance(self.row, Exception):
            raise self.row
        return self.row


class _ProvenanceSession:
    def __init__(self, row, *, close_error=False):
        self.row = row
        self.close_error = close_error

    def query(self, *_columns):
        return _ProvenanceQuery(self.row)

    def close(self):
        if self.close_error:
            raise RuntimeError("close failed")


def test_generated_image_provenance_fails_closed_on_missing_and_store_errors():
    generated_images = _generated_images_module()
    from core.database import GalleryImage

    def proof(row, *, close_error=False):
        return generated_images.has_generated_image_provenance(
            lambda: _ProvenanceSession(row, close_error=close_error),
            GalleryImage,
            filename="a" * 12 + ".png",
            owner="alice",
        )

    assert proof(object()) is True
    assert proof(None) is False
    assert proof(RuntimeError("query failed")) is False
    assert proof(object(), close_error=True) is False
    assert generated_images.has_generated_image_provenance(
        lambda: _ProvenanceSession(object()),
        GalleryImage,
        filename="a" * 12 + ".png",
        owner=None,
    ) is False

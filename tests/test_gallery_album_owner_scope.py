"""Issue #2754 — gallery owner-scoping, and the album retirement surface.

`patch_gallery_image` must validate that the *target album* belongs to the caller
before moving an image into it (otherwise user B can file B's image into user A's
album). The gallery route handlers are closures, so — matching the AST-assertion
convention of test_gallery_image_privileges.py — we assert the guards are present
in the source.

Albums themselves are retired in S19 (Files Gallery folder + Imps). The album
CRUD endpoints no longer mutate anything; they answer 410/empty. Those tests
assert the retirement rather than the old in-function owner guards.
"""
import ast
from pathlib import Path


def _function_sources():
    source = Path("routes/gallery/gallery_routes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    return {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_patch_validates_target_album_ownership():
    fns = _function_sources()
    body = fns["patch_gallery_image"]
    assert "req.album_id" in body
    # The target album must be ownership-validated (via the same helper the
    # sibling mutators use) before the image is reassigned to it.
    assert "_get_or_404_album(db, req.album_id, user)" in body


def test_upload_validates_target_album_ownership():
    fns = _function_sources()
    body = fns["gallery_upload"]
    assert "album_id" in body
    assert "_get_or_404_album(db, album_id, user)" in body


def test_list_albums_is_retired():
    fns = _function_sources()
    body = fns["list_albums"]
    # Albums are retired: the list is honestly empty and flagged, never a
    # live owner-scoped album inventory.
    assert "retired" in body
    assert "GalleryAlbum" not in body


def test_album_mutators_are_retired():
    fns = _function_sources()
    for name in ("create_album", "update_album", "delete_album", "add_to_album", "remove_from_album"):
        body = fns[name]
        assert "410" in body, name
        assert "retired" in body, name
        # No live album mutation survives retirement.
        assert "db.commit()" not in body, name


def test_get_or_404_album_enforces_owner():
    # Guard the precedent we rely on: the helper rejects another user's album.
    # It stays in place for legacy album_id references on patch/upload.
    fns = _function_sources()
    helper = fns["_get_or_404_album"]
    assert "GalleryAlbum.owner == user" in helper

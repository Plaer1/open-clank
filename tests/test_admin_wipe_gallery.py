import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.database import Base, GalleryImage, GalleryAlbum
from routes.admin_wipe_routes import setup_admin_wipe_routes
from fastapi import Request
from fastapi import HTTPException

def test_wipe_gallery_clears_albums_and_only_proven_image_bytes(monkeypatch, tmp_path):
    # 1. Create a clean in-memory database
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    
    # 2. Create test session factory
    TestSessionLocal = sessionmaker(bind=engine)
    
    # 3. Populate test database with an album and an image linked to it
    db = TestSessionLocal()
    album = GalleryAlbum(id="album-1", name="Trip to Rome")
    image = GalleryImage(id="img-1", filename="rome1.jpg", album_id="album-1")
    db.add(album)
    db.add(image)
    db.commit()
    
    assert db.query(GalleryImage).count() == 1
    assert db.query(GalleryAlbum).count() == 1
    db.close()


    generated_dir = tmp_path / "generated_images"
    generated_dir.mkdir()
    tracked_image = generated_dir / "rome1.jpg"
    untracked_image = generated_dir / "untracked.jpg"
    tracked_image.write_bytes(b"tracked")
    untracked_image.write_bytes(b"untracked")
    
    # 4. Patch SessionLocal in routes/admin_wipe_routes.py to use our in-memory DB
    import routes.admin_wipe_routes
    monkeypatch.setattr(routes.admin_wipe_routes, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(
        routes.admin_wipe_routes,
        "GENERATED_IMAGE_DIR",
        generated_dir,
    )
    
    # Mock require_admin to bypass auth check (using standard pytest monkeypatch)
    monkeypatch.setattr(routes.admin_wipe_routes, "require_admin", lambda r: None)
    
    # Construct a real FastAPI Request object
    request = Request(scope={"type": "http"})
    
    # 5. Initialize the router and retrieve the handler
    router = setup_admin_wipe_routes(session_manager=None)
    wipe_route = next(r for r in router.routes if r.path == "/api/admin/wipe/{kind}")
    wipe_handler = wipe_route.endpoint
    
    # 6. Execute the wipe logic for gallery
    result = asyncio.run(wipe_handler(kind="gallery", request=request))
    
    # 7. Assertions
    db = TestSessionLocal()
    assert db.query(GalleryImage).count() == 0
    # This assertion will fail before the fix because GalleryAlbum rows were not deleted
    assert db.query(GalleryAlbum).count() == 0
    
    # Check returned stats
    assert result["status"] == "deleted"
    assert result["kind"] == "gallery"
    assert result["count"] == 2  # 1 image + 1 album
    assert not tracked_image.exists()
    assert untracked_image.read_bytes() == b"untracked"
    
    db.close()


@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("kind", ["memory", " MEMORY "])
def test_retired_memory_wipe_checks_admin_and_normalizes_before_database_or_provider(monkeypatch, authorized, kind):
    import routes.admin_wipe_routes

    def check_admin(_request):
        if not authorized:
            raise HTTPException(403, "admin required")
    monkeypatch.setattr(routes.admin_wipe_routes, "require_admin", check_admin)

    def fail_database_open():
        raise AssertionError("retired memory wipe must not open the database")

    class Provider:
        async def purge_owner(self, **kwargs):
            raise AssertionError("retired memory wipe must not call the provider")

    monkeypatch.setattr(routes.admin_wipe_routes, "SessionLocal", fail_database_open)
    request = Request(scope={"type": "http"})
    router = setup_admin_wipe_routes(session_manager=None, memory_provider=Provider())
    wipe_handler = next(r for r in router.routes if r.path == "/api/admin/wipe/{kind}").endpoint

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(wipe_handler(kind=kind, request=request))
    assert exc_info.value.status_code == (410 if authorized else 403)
    if authorized:
        assert "Brain" in str(exc_info.value.detail)

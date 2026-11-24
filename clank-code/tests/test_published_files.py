import hashlib
import os
import types
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, PublishedFile, PublishedFileGrant
from routes.published_file_routes import setup_published_file_routes
from src.published_files import PublishedFileError, PublishedFileService, _utcnow


@pytest.fixture()
def published(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'published.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        engine,
        tables=[PublishedFile.__table__, PublishedFileGrant.__table__],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    service = PublishedFileService(str(tmp_path / "bytes"), factory)
    yield service, factory, tmp_path
    engine.dispose()


def _token(url):
    return urlsplit(url).path.rsplit("/", 1)[-1]


def test_published_file_lifecycle_is_owner_scoped_revocable_and_persistent(published):
    service, factory, tmp_path = published
    source = tmp_path / "clänk pack.zip"
    source.write_bytes(b"PK\x03\x04real zip-ish bytes")

    made = service.publish(str(source), owner="alice", audience="owner")
    token = _token(made["download_url"])
    assert made["download_url"].startswith("/api/files/download/")
    assert service.get_owned(made["id"], owner="bob") is None

    listing = service.list(owner="alice")
    assert listing[0]["filename"] == "clänk pack.zip"
    assert "path" not in listing[0]
    assert "token" not in repr(listing[0]).lower()

    with factory() as db:
        grant = db.query(PublishedFileGrant).one()
        assert grant.token_hash == hashlib.sha256(token.encode("ascii")).hexdigest()
        assert token not in grant.token_hash

    restarted = PublishedFileService(str(tmp_path / "bytes"), factory)
    assert restarted.resolve_grant(token)["filename"] == "clänk pack.zip"
    assert restarted.revoke_all(made["id"], owner="alice") == 1
    assert restarted.resolve_grant(token) is None

    public = restarted.create_grant(
        made["id"],
        owner="alice",
        audience="public",
        expires_in_hours=1,
        public_origin="https://buildweek.openclank.dev",
    )
    assert public["download_url"].startswith("https://buildweek.openclank.dev/")
    public_token = _token(public["download_url"])
    assert restarted.resolve_grant(public_token)["audience"] == "public"

    with factory() as db:
        grant = db.query(PublishedFileGrant).filter(
            PublishedFileGrant.token_hash == hashlib.sha256(public_token.encode("ascii")).hexdigest()
        ).one()
        grant.expires_at = _utcnow() - timedelta(seconds=1)
        db.commit()
    assert restarted.resolve_grant(public_token) is None

    replacement = restarted.create_grant(made["id"], owner="alice")
    assert _token(replacement["download_url"]) not in {token, public_token}
    stored = restarted.get_owned(made["id"], owner="alice")["path"]
    assert restarted.delete(made["id"], owner="alice") is True
    assert not os.path.exists(stored)
    assert restarted.list(owner="alice") == []
    assert restarted.resolve_grant(_token(replacement["download_url"])) is None


def test_public_routes_keep_owner_links_private_and_serve_real_bytes(published):
    service, _factory, tmp_path = published
    source = tmp_path / "résumé.zip"
    source.write_bytes(b"0123456789")
    public = service.publish(
        str(source), owner="alice", audience="public", public_origin="http://testserver"
    )
    owner = service.create_grant(public["id"], owner="alice", audience="owner")

    app = FastAPI()
    app.state.auth_manager = type("Auth", (), {"is_admin": lambda self, user: user == "admin"})()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        request.state.current_user = request.headers.get("X-Test-User")
        return await call_next(request)

    app.include_router(setup_published_file_routes(service))
    client = TestClient(app)

    response = client.get(urlsplit(public["download_url"]).path)
    assert response.status_code == 200
    assert response.content == b"0123456789"
    assert "filename*=utf-8''r%C3%A9sum%C3%A9.zip" in response.headers["content-disposition"]

    ranged = client.get(urlsplit(public["download_url"]).path, headers={"Range": "bytes=2-5"})
    assert ranged.status_code == 206
    assert ranged.content == b"2345"

    owner_path = urlsplit(owner["download_url"]).path
    assert client.get(owner_path).status_code == 404
    assert client.get(owner_path, headers={"X-Test-User": "bob"}).status_code == 404
    assert client.get(owner_path, headers={"X-Test-User": "alice"}).status_code == 200
    assert client.get(owner_path, headers={"X-Test-User": "admin"}).status_code == 200
    assert client.get("/api/files/download/not-a-real-token").status_code == 404


def test_public_grants_require_a_clean_configured_origin(published):
    service, _factory, tmp_path = published
    source = tmp_path / "file.txt"
    source.write_text("hello", encoding="utf-8")
    with pytest.raises(PublishedFileError, match="public app URL"):
        service.publish(str(source), owner="alice", audience="public")
    with pytest.raises(PublishedFileError, match="credentials"):
        service.publish(
            str(source), owner="alice", audience="public", public_origin="https://user:pass@example.com"
        )
    assert not any(path.is_file() for path in (tmp_path / "bytes").rglob("*"))


def test_publication_rejects_nonfiles_symlinks_oversize_and_changed_sources(published, monkeypatch):
    service, _factory, tmp_path = published
    source = tmp_path / "source.bin"
    source.write_bytes(b"1234")
    symlink = tmp_path / "link.bin"
    symlink.symlink_to(source)

    with pytest.raises(PublishedFileError, match="regular file"):
        service.publish(str(tmp_path), owner="alice")
    with pytest.raises(PublishedFileError, match="regular file"):
        service.publish(str(symlink), owner="alice")
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        with pytest.raises(PublishedFileError, match="regular file"):
            service.publish(str(fifo), owner="alice")

    monkeypatch.setattr("src.published_files.get_chat_upload_max_bytes", lambda: 3)
    with pytest.raises(PublishedFileError, match="exceeds"):
        service.publish(str(source), owner="alice")

    monkeypatch.setattr("src.published_files.get_chat_upload_max_bytes", lambda: 1024)
    real_fstat = os.fstat
    calls = 0

    def changed_fstat(fd):
        nonlocal calls
        stat_result = real_fstat(fd)
        calls += 1
        if calls == 2:
            return types.SimpleNamespace(
                st_mode=stat_result.st_mode,
                st_dev=stat_result.st_dev,
                st_ino=stat_result.st_ino,
                st_size=stat_result.st_size,
                st_mtime_ns=stat_result.st_mtime_ns + 1,
            )
        return stat_result

    monkeypatch.setattr("src.published_files.os.fstat", changed_fstat)
    with pytest.raises(PublishedFileError, match="changed while"):
        service.publish(str(source), owner="alice")
    assert service.list(owner="alice") == []
    assert not any(path.is_file() for path in (tmp_path / "bytes").rglob("*"))


def test_tool_and_files_ui_are_registered():
    from src.agent_tools import TOOL_HANDLERS
    from src.tool_blocks import TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block

    schemas = {item["function"]["name"] for item in FUNCTION_TOOL_SCHEMAS}
    assert all("publish_file" in registry for registry in (TOOL_HANDLERS, TOOL_TAGS, schemas))
    block = function_call_to_tool_block(
        "publish_file", '{"path":"/tmp/a.zip","audience":"public","expires_in_hours":2}'
    )
    assert block.tool_type == "publish_file"
    assert '"audience": "public"' in block.content

    source = open("static/js/documentLibrary.js", encoding="utf-8").read()
    html = open("static/index.html", encoding="utf-8").read()
    assert "/api/files/library" in source
    assert "Break links" in source
    assert "Copy public link" in source
    assert 'id="rail-documents" title="Files"' in html

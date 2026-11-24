"""Bundle restore must cap asset count and reject duplicate member references.

``_restore_memory_bundle`` trusts the v3 manifest's asset list: without a
count cap a manifest can declare unbounded photo restorations per upload, and
two assets naming the same archive member would store the same bytes twice.
"""
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import zipfile
from fastapi import HTTPException, UploadFile
from PIL import Image

import routes.memory_routes as mr
from services.memory.import_batch import MAX_BATCH_FILES


def _route(router, path, method):
    for r in router.routes:
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(path)


class _StubProvider:
    provider_id = "stub"

    def __init__(self, fm_db_path):
        self._fm_db_path = fm_db_path


def _router(monkeypatch, tmp_path):
    monkeypatch.setattr(mr, "get_current_user", lambda request: "alice", raising=False)
    monkeypatch.setattr(mr, "require_user", lambda request: "alice", raising=False)
    monkeypatch.setattr(
        "src.auth_helpers.require_privilege", lambda request, privilege: "alice"
    )
    mem = MagicMock()
    mem.load = lambda owner=None: []
    provider = _StubProvider(str(tmp_path / "fm.sqlite"))
    return mr.setup_memory_routes(mem, MagicMock(), memory_provider=provider)


def _request():
    return SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )


def _png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _bundle(assets, members=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"schema_version": "openclank.memory-bundle/v3", "assets": assets}),
        )
        for name, data in (members or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _upload(content, name="bundle.zip"):
    return UploadFile(filename=name, file=io.BytesIO(content))


async def test_bundle_restore_rejects_excess_assets(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path)
    endpoint = _route(router, "/api/memory/import", "POST")
    assets = [
        {"member": f"assets/photo{i}.png", "media_type": "image/png"}
        for i in range(MAX_BATCH_FILES + 1)
    ]
    # Every declared member exists and is a valid photo, so only the asset
    # count cap itself can reject this bundle.
    members = {asset["member"]: _png_bytes() for asset in assets}
    with pytest.raises(HTTPException) as excinfo:
        await endpoint(_request(), None, _upload(_bundle(assets, members)))
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["code"] == "MEMORY_BUNDLE_INVALID"


async def test_bundle_restore_rejects_duplicate_member_references(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path)
    endpoint = _route(router, "/api/memory/import", "POST")
    assets = [
        {"member": "assets/photo.png", "media_type": "image/png", "filename": "a.png"},
        {"member": "assets/photo.png", "media_type": "image/png", "filename": "b.png"},
    ]
    content = _bundle(assets, {"assets/photo.png": _png_bytes()})
    with pytest.raises(HTTPException) as excinfo:
        await endpoint(_request(), None, _upload(content))
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["code"] == "MEMORY_BUNDLE_INVALID"


async def test_bundle_restore_restores_valid_assets(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path)
    endpoint = _route(router, "/api/memory/import", "POST")
    assets = [{"member": "assets/photo.png", "media_type": "image/png", "filename": "a.png"}]
    content = _bundle(assets, {"assets/photo.png": _png_bytes()})
    result = await endpoint(_request(), None, _upload(content))
    assert len(result["media"]) == 1
    assert result["suggestions"] == []

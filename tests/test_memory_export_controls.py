"""Filtered memory export controls and owner-scoped asset download.

``GET /api/memory/export`` used to be all-or-nothing. These tests pin the
fine-grained filters (``sections``/``kinds``/``tags``/``since``/``until``/
``q``/``include_archived``), the bundle asset controls (``assets`` and
``assets_only``), and ``GET /api/memory/assets/{asset_id}`` single-asset
downloads. All stores live under ``tmp_path``; live memory data is never
touched.
"""
import io
import json
import zipfile
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image

import routes.memory_routes as mr
from services.memory.media_assets import MemoryMediaStore, admit_photo


def _route(router, path, method):
    for r in router.routes:
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(path)


def _payload():
    return {
        "owner": "alice",
        "workspace_id": "chat",
        "retention": {"policy": "keep"},
        "raw": [
            {"id": "raw-1", "content": "hello world", "recorded_at": "2026-01-01T00:00:00+00:00"},
            {"id": "raw-2", "content": "later note", "recorded_at": "2026-03-01T00:00:00+00:00"},
        ],
        "candidates": [
            {"id": "cand-1", "kind": "fact", "content": "likes tea", "created_at": "2026-02-01T00:00:00+00:00"},
            {"id": "cand-2", "kind": "episodic", "content": "went hiking", "created_at": "2026-04-01T00:00:00+00:00"},
        ],
        "curated": [
            {
                "id": "cur-1", "kind": "fact", "tags": ["food", "health"],
                "content": "eats pasta", "created_at": "2026-02-15T00:00:00+00:00",
                "archived": False,
            },
            {
                "id": "cur-2", "kind": "persona", "tags": ["identity"],
                "content": "name is alice", "created_at": "2026-05-01T00:00:00+00:00",
                "archived": True,
            },
        ],
        "quarantine": [
            {"id": "quar-1", "content": "spam content", "quarantined_at": "2026-06-01T00:00:00+00:00"},
        ],
        "graph_nodes": [],
        "graph_edges": [],
        "graph_cues": [],
        "tombstones": [],
        "digest": {"persisted": False},
    }


class _StubProvider:
    provider_id = "stub"

    def __init__(self, fm_db_path, payload=None):
        self._fm_db_path = fm_db_path
        self._payload = payload or {}

    async def export_scope(self, owner=None, workspace_id=None):
        return json.loads(json.dumps(self._payload))


def _router(monkeypatch, tmp_path, payload=None):
    monkeypatch.setattr(mr, "get_current_user", lambda request: "alice", raising=False)
    monkeypatch.setattr(mr, "require_user", lambda request: "alice", raising=False)
    monkeypatch.setattr(
        "src.auth_helpers.require_privilege", lambda request, privilege: "alice"
    )
    mem = MagicMock()
    mem.load = lambda owner=None: []
    provider = _StubProvider(str(tmp_path / "fm.sqlite"), payload)
    return mr.setup_memory_routes(mem, MagicMock(), memory_provider=provider)


def _request():
    return SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )


def _png_bytes(rgb=(10, 20, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), rgb).save(buffer, format="PNG")
    return buffer.getvalue()


def _media_store(tmp_path):
    return MemoryMediaStore(db_path=str(tmp_path / "fm.sqlite"), data_dir=str(tmp_path))


def _put_photo(tmp_path, source_id="photo-1", owner="alice", rgb=(10, 20, 30)):
    admitted = admit_photo(_png_bytes(rgb), f"{source_id}.png", "image/png")
    result = _media_store(tmp_path).put(
        owner=owner,
        source_id=source_id,
        filename=f"{source_id}.png",
        photo=admitted,
        associated_text="",
        provenance={},
    )
    return result, admitted


def _insert_non_image_asset(tmp_path, owner="alice", asset_id="asset_doc"):
    """Insert a non-image asset row directly; ``put`` only admits photos."""
    store = _media_store(tmp_path)
    data = b"plain text export"
    digest = sha256(data).hexdigest()
    blob = store._blob_path(owner, digest)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(data)
    with store._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO fm_v2_media_assets("
            "owner_id,asset_id,source_id,filename,media_type,width,height,"
            "byte_size,original_sha256,canonical_sha256,blob_key,state,created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                owner, asset_id, "doc-1", "notes.txt", "text/plain", 0, 0,
                len(data), digest, digest, str(blob), "active",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    return asset_id


async def _export(router, **params):
    endpoint = _route(router, "/api/memory/export", "GET")
    query = dict(
        format=None, sections=None, kinds=None, tags=None, since=None,
        until=None, q=None, include_archived=None, assets=None, assets_only=None,
    )
    query.update(params)
    return await endpoint(_request(), **query)


async def _body(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    return b"".join(chunks)


async def _export_bundle_bytes(router, **params):
    response = await _export(router, format="bundle", **params)
    assert response.media_type == "application/zip"
    return await _body(response)


def _manifest(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return json.loads(archive.read("manifest.json")), archive.namelist()


async def _restore(router, content):
    endpoint = _route(router, "/api/memory/import", "POST")
    upload = UploadFile(filename="bundle.zip", file=io.BytesIO(content))
    return await endpoint(_request(), None, upload)


async def test_export_sections_keeps_only_requested_keys(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path, _payload())
    result = await _export(router, sections="curated")
    assert set(result) == {"curated"}
    assert [record["id"] for record in result["curated"]] == ["cur-1", "cur-2"]


async def test_export_sections_rejects_unknown_key(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path, _payload())
    with pytest.raises(HTTPException) as excinfo:
        await _export(router, sections="curated,bogus")
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "MEMORY_EXPORT_FILTER_INVALID"


async def test_export_kinds_filters_curated_and_candidates(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path, _payload())
    result = await _export(router, kinds="fact")
    assert [record["id"] for record in result["curated"]] == ["cur-1"]
    assert [record["id"] for record in result["candidates"]] == ["cand-1"]
    assert [record["id"] for record in result["raw"]] == ["raw-1", "raw-2"]


async def test_export_kinds_rejects_empty_values(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path, _payload())
    with pytest.raises(HTTPException) as excinfo:
        await _export(router, kinds=" , ,")
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "MEMORY_EXPORT_FILTER_INVALID"


async def test_export_tags_filters_curated_only(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path, _payload())
    result = await _export(router, tags="food")
    assert [record["id"] for record in result["curated"]] == ["cur-1"]
    assert len(result["candidates"]) == 2
    result = await _export(router, tags="identity")
    assert [record["id"] for record in result["curated"]] == ["cur-2"]


async def test_export_since_filters_record_timestamps(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path, _payload())
    result = await _export(router, since="2026-03-01T00:00:00Z")
    assert [record["id"] for record in result["raw"]] == ["raw-2"]
    assert [record["id"] for record in result["candidates"]] == ["cand-2"]
    assert [record["id"] for record in result["curated"]] == ["cur-2"]


async def test_export_until_filters_record_timestamps(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path, _payload())
    result = await _export(router, until="2026-02-01T00:00:00Z")
    assert [record["id"] for record in result["raw"]] == ["raw-1"]
    assert [record["id"] for record in result["candidates"]] == ["cand-1"]
    assert result["curated"] == []


async def test_export_since_rejects_bad_date(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path, _payload())
    with pytest.raises(HTTPException) as excinfo:
        await _export(router, since="not-a-date")
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "MEMORY_EXPORT_FILTER_INVALID"


async def test_export_q_matches_content_case_insensitive(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path, _payload())
    result = await _export(router, q="ALICE")
    assert [record["id"] for record in result["curated"]] == ["cur-2"]
    assert result["raw"] == []
    assert result["candidates"] == []
    assert result["quarantine"] == []


async def test_export_include_archived_false_drops_archived_curated(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path, _payload())
    result = await _export(router, include_archived="false")
    assert [record["id"] for record in result["curated"]] == ["cur-1"]
    with pytest.raises(HTTPException) as excinfo:
        await _export(router, include_archived="maybe")
    assert excinfo.value.status_code == 422


async def test_export_bundle_applies_filters_to_manifest_memory(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path, _payload())
    content = await _export_bundle_bytes(router, kinds="fact", include_archived="false")
    manifest, _ = _manifest(content)
    memory = manifest["memory"]
    assert [record["id"] for record in memory["curated"]] == ["cur-1"]
    assert [record["id"] for record in memory["candidates"]] == ["cand-1"]


async def test_export_bundle_assets_none_omits_members_and_restores(monkeypatch, tmp_path):
    _put_photo(tmp_path)
    router = _router(monkeypatch, tmp_path, _payload())
    content = await _export_bundle_bytes(router, assets="none")
    manifest, names = _manifest(content)
    assert names == ["manifest.json"]
    assert "assets" not in manifest
    result = await _restore(router, content)
    assert result["media"] == []


async def test_export_bundle_assets_only_keeps_empty_memory_and_restores(monkeypatch, tmp_path):
    _put_photo(tmp_path)
    router = _router(monkeypatch, tmp_path, _payload())
    content = await _export_bundle_bytes(router, assets_only="true")
    manifest, names = _manifest(content)
    assert any(name.startswith("assets/") for name in names)
    memory = manifest["memory"]
    assert memory["raw"] == []
    assert memory["candidates"] == []
    assert memory["curated"] == []
    assert memory["quarantine"] == []
    assert memory["owner"] == "alice"
    result = await _restore(router, content)
    assert len(result["media"]) == 1


async def test_export_bundle_assets_images_excludes_non_images(monkeypatch, tmp_path):
    photo, _ = _put_photo(tmp_path)
    _insert_non_image_asset(tmp_path)
    router = _router(monkeypatch, tmp_path, _payload())
    content = await _export_bundle_bytes(router, assets="images")
    manifest, names = _manifest(content)
    assert [asset["asset_id"] for asset in manifest["assets"]] == [photo["asset_id"]]
    assert not any(name.endswith(".bin") for name in names)
    content = await _export_bundle_bytes(router, assets="all")
    manifest, _ = _manifest(content)
    assert {asset["asset_id"] for asset in manifest["assets"]} == {
        photo["asset_id"], "asset_doc",
    }


async def test_export_bundle_rejects_invalid_assets_value(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path, _payload())
    with pytest.raises(HTTPException) as excinfo:
        await _export(router, format="bundle", assets="videos")
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "MEMORY_EXPORT_FILTER_INVALID"


async def test_export_assets_only_requires_bundle_format(monkeypatch, tmp_path):
    router = _router(monkeypatch, tmp_path, _payload())
    with pytest.raises(HTTPException) as excinfo:
        await _export(router, assets_only="true")
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "MEMORY_EXPORT_FILTER_INVALID"


async def test_download_asset_streams_bytes_with_media_type_and_filename(monkeypatch, tmp_path):
    stored, admitted = _put_photo(tmp_path)
    router = _router(monkeypatch, tmp_path)
    endpoint = _route(router, "/api/memory/assets/{asset_id}", "GET")
    response = await endpoint(_request(), stored["asset_id"])
    assert response.media_type == "image/png"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="{stored["asset_id"]}.png"'
    )
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert await _body(response) == admitted.bytes


async def test_download_asset_unknown_or_foreign_is_404(monkeypatch, tmp_path):
    foreign, _ = _put_photo(tmp_path, owner="bob")
    router = _router(monkeypatch, tmp_path)
    endpoint = _route(router, "/api/memory/assets/{asset_id}", "GET")
    with pytest.raises(HTTPException) as excinfo:
        await endpoint(_request(), "asset_missing")
    assert excinfo.value.status_code == 404
    with pytest.raises(HTTPException) as excinfo:
        await endpoint(_request(), foreign["asset_id"])
    assert excinfo.value.status_code == 404


async def test_download_asset_integrity_failure_is_indistinguishable_404(monkeypatch, tmp_path):
    stored, admitted = _put_photo(tmp_path)
    blob = _media_store(tmp_path)._blob_path("alice", admitted.canonical_sha256)
    blob.write_bytes(b"corrupted")
    router = _router(monkeypatch, tmp_path)
    endpoint = _route(router, "/api/memory/assets/{asset_id}", "GET")
    with pytest.raises(HTTPException) as excinfo:
        await endpoint(_request(), stored["asset_id"])
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail["code"] == "asset_not_found"

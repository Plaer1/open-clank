"""Imps managed project routes — Save with Lore recovery, Save a copy, export.

Covers the HTTP surface of the S19 managed project substrate: ownership,
optimistic concurrency, recoverable Save (Lore preimage refusal), Save-a-copy
allocation, and portable export/import including blank canvases.
"""

from __future__ import annotations

import pytest
import base64
import hashlib
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, FilesImageResource, ManagedImageProject
import src.generated_images as generated_images
import routes.image_project_routes as ipr
from src.openclank import image_projects as image_projects_module
from src.openclank.image_projects import ImageProjectRepository


@pytest.fixture()
def client(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projects.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(ipr, "SessionLocal", factory)
    # Deterministic principal for the auth-disabled path.
    monkeypatch.setenv("AUTH_ENABLED", "false")
    # Keep durable Lore preimages inside the test's tmp dir.
    monkeypatch.setenv("IMPS_PREIMAGE_DIR", str(tmp_path / "preimages"))

    app = FastAPI()
    app.include_router(ipr.setup_image_project_routes())
    return TestClient(app)


def _create(client, resource_id="image-1", **overrides):
    body = {
        "provider": "files",
        "resource_id": resource_id,
        "name": "Sunset",
        "width": 100,
        "height": 50,
        "state": {"v": 2, "layers": [{"id": 1}], "masks": {}, "text": {}},
        "expected_image_revision": "rev-a",
    }
    body.update(overrides)
    resp = client.post("/api/imps/projects", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_get_and_owner_isolation(client):
    created = _create(client)
    assert created["image"] == {"provider": "files", "resource_id": "image-1"}
    assert created["expected_image_revision"] == "rev-a"
    assert created["project_revision"] == 1

    fetched = client.get(f"/api/imps/projects/{created['id']}").json()
    assert fetched["id"] == created["id"]
    assert fetched["state"]["layers"] == [{"id": 1}]

    listed = client.get("/api/imps/projects").json()["projects"]
    assert [p["id"] for p in listed] == [created["id"]]


def test_update_requires_matching_revision(client):
    created = _create(client)
    ok = client.put(
        f"/api/imps/projects/{created['id']}",
        json={"state": {"v": 2, "layers": [{"id": 1}, {"id": 2}]}, "expected_project_revision": 1},
    )
    assert ok.status_code == 200
    assert ok.json()["project_revision"] == 2

    stale = client.put(
        f"/api/imps/projects/{created['id']}",
        json={"state": {"v": 2, "layers": []}, "expected_project_revision": 1},
    )
    assert stale.status_code == 409


def test_save_rebinds_image_revision_and_reports_refresh_receipt(client):
    created = _create(client)
    resp = client.post(
        f"/api/imps/projects/{created['id']}/save",
        json={
            "expected_project_revision": 1,
            "expected_image_revision": "rev-a",
            "new_image_revision": "rev-b",
            "state": {"v": 2, "layers": [{"id": 1}, {"id": 2}]},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["allocated"] is False
    assert body["image"] == {"provider": "files", "resource_id": "image-1"}
    assert body["refresh_receipt"]["image_revision"] == "rev-b"
    assert body["refresh_receipt"]["project_revision"] == 2

    # The project now tracks the new image revision.
    fetched = client.get(f"/api/imps/projects/{created['id']}").json()
    assert fetched["expected_image_revision"] == "rev-b"


def test_save_refuses_stale_revisions(client):
    created = _create(client)
    stale_project = client.post(
        f"/api/imps/projects/{created['id']}/save",
        json={
            "expected_project_revision": 99,
            "expected_image_revision": "rev-a",
            "new_image_revision": "rev-b",
        },
    )
    assert stale_project.status_code == 409

    stale_image = client.post(
        f"/api/imps/projects/{created['id']}/save",
        json={
            "expected_project_revision": 1,
            "expected_image_revision": "rev-wrong",
            "new_image_revision": "rev-b",
        },
    )
    assert stale_image.status_code == 409


def test_save_endpoint_writes_gallery_bytes_and_compensates_commit_failure(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gallery.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(ipr, "SessionLocal", factory)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("IMPS_PREIMAGE_DIR", str(tmp_path / "preimages"))
    monkeypatch.setattr(generated_images, "GENERATED_IMAGE_DIR", tmp_path / "generated")
    generated_images.GENERATED_IMAGE_DIR.mkdir()
    original = b"original-gallery-pixels"
    path = generated_images.GENERATED_IMAGE_DIR / "image-1.png"
    path.write_bytes(original)
    original_revision = "sha256:" + hashlib.sha256(original).hexdigest()
    db = factory()
    db.add(FilesImageResource(id="image-1", owner="local-installation", kind="image", display_name="image-1.png", locator="image-1.png", is_active=True, digest=hashlib.sha256(original).hexdigest(), size=len(original), mime_type="image/png"))
    db.commit(); db.close()
    app = FastAPI(); app.include_router(ipr.setup_image_project_routes()); http = TestClient(app, raise_server_exceptions=False)
    created = _create(http, expected_image_revision=original_revision)
    updated = b"new-gallery-pixels"
    fail = {"value": True}
    def failing_sessions():
        session = factory(); real_commit = session.commit
        def commit():
            if fail["value"]:
                fail["value"] = False
                raise RuntimeError("injected endpoint commit failure")
            return real_commit()
        session.commit = commit
        return session
    monkeypatch.setattr(ipr, "SessionLocal", failing_sessions)
    response = http.post(f"/api/imps/projects/{created['id']}/save", json={
        "expected_project_revision": 1, "expected_image_revision": original_revision,
        "new_image_revision": "ignored", "image_bytes": base64.b64encode(updated).decode(),
    })
    assert response.status_code == 500, response.text
    assert path.read_bytes() == original
    check = factory(); row = check.get(ManagedImageProject, created['id']); check.close()
    assert row.project_revision == 1
    success = http.post(f"/api/imps/projects/{created['id']}/save", json={
        "expected_project_revision": 1, "expected_image_revision": original_revision,
        "new_image_revision": "ignored", "image_bytes": base64.b64encode(updated).decode(),
    })
    assert success.status_code == 200, success.text
    check = factory(); image = check.get(FilesImageResource, "image-1"); check.close()
    assert image.digest == hashlib.sha256(updated).hexdigest()
    assert image.size == len(updated)


def test_save_refuses_when_lore_capture_fails(monkeypatch, client):
    """A Save that cannot be recovered is refused before mutation (503)."""
    created = _create(client)

    def failing_capture(**kwargs):
        raise RuntimeError("history worker unavailable")

    monkeypatch.setattr(image_projects_module, "ACTIVE_LORE_CAPTURE", failing_capture)
    resp = client.post(
        f"/api/imps/projects/{created['id']}/save",
        json={
            "expected_project_revision": 1,
            "expected_image_revision": "rev-a",
            "new_image_revision": "rev-b",
            "state": {"v": 2, "layers": []},
        },
    )
    assert resp.status_code == 503
    assert "unapplied" in resp.text or "capture" in resp.text

    # Unchanged: still revision 1, still bound to rev-a.
    fetched = client.get(f"/api/imps/projects/{created['id']}").json()
    assert fetched["project_revision"] == 1
    assert fetched["expected_image_revision"] == "rev-a"


def test_save_records_lore_preimage_before_mutation(monkeypatch, client):
    created = _create(client, state={"v": 2, "layers": [{"id": "base"}]})
    events = []

    def capture(**kwargs):
        events.append(kwargs)

    monkeypatch.setattr(image_projects_module, "ACTIVE_LORE_CAPTURE", capture)
    resp = client.post(
        f"/api/imps/projects/{created['id']}/save",
        json={
            "expected_project_revision": 1,
            "expected_image_revision": "rev-a",
            "new_image_revision": "rev-b",
            "state": {"v": 2, "layers": [{"id": "base"}, {"id": "paint"}]},
            "operation_id": "op-1",
        },
    )
    assert resp.status_code == 200
    assert len(events) == 1
    assert events[0]["operation_id"] == "op-1"
    assert events[0]["image_revision"] == "rev-a"
    assert events[0]["project_revision"] == 1


def test_save_copy_allocates_separate_resource(client):
    created = _create(client)
    resp = client.post(
        f"/api/imps/projects/{created['id']}/save-copy",
        json={
            "provider": "files",
            "resource_id": "image-2",
            "expected_image_revision": "rev-copy",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["allocated"] is True
    assert body["image"]["resource_id"] == "image-2"
    assert body["project_id"] != created["id"]

    # Source untouched.
    source = client.get(f"/api/imps/projects/{created['id']}").json()
    assert source["expected_image_revision"] == "rev-a"
    assert source["project_revision"] == 1


def test_save_copy_files_identity_replays_without_gallery_row(client):
    """Save-copy binds one Files-owned identity and replays the same project."""
    created = _create(client)
    request = {
        "provider": "files",
        "resource_id": "image:files-owned-copy-1",
        "expected_image_revision": "sha256:files-copy-revision",
        "operation_key": "imps-copy-files-owned-1",
        "state": {"v": 2, "layers": [{"id": 1, "name": "copy"}]},
    }
    first = client.post(f"/api/imps/projects/{created['id']}/save-copy", json=request)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["allocated"] is True
    assert first_body["image"] == {"provider": "files", "resource_id": "image:files-owned-copy-1"}

    replay = client.post(f"/api/imps/projects/{created['id']}/save-copy", json=request)
    assert replay.status_code == 200, replay.text
    replay_body = replay.json()
    assert replay_body["allocated"] is False
    assert replay_body["project_id"] == first_body["project_id"]
    assert replay_body["image"] == first_body["image"]

    db = ipr.SessionLocal()
    try:
        assert db.query(FilesImageResource).filter(FilesImageResource.id == "files-owned-copy-1").one_or_none() is None
        projects = db.query(ManagedImageProject).filter(ManagedImageProject.owner == "local-installation").all()
        assert len([row for row in projects if row.id == first_body["project_id"]]) == 1
    finally:
        db.close()


def test_export_import_round_trips_editable_state(client):
    created = _create(
        client,
        state={
            "v": 2,
            "layers": [{"id": 1, "name": "base"}],
            "masks": {"1": {"op": "add"}},
            "text": {"2": {"str": "hi"}},
        },
    )
    exported = client.get(f"/api/imps/projects/{created['id']}/export").json()
    assert exported["kind"] == "imps-project"
    assert exported["schema_version"] == 1
    assert exported["state"]["masks"] == {"1": {"op": "add"}}
    assert exported["state"]["text"] == {"2": {"str": "hi"}}

    imported = client.post(
        "/api/imps/projects/import",
        json={
            "provider": "files",
            "resource_id": "image-restored",
            "bundle": exported,
        },
    ).json()
    assert imported["state"]["layers"] == [{"id": 1, "name": "base"}]
    assert imported["state"]["masks"] == {"1": {"op": "add"}}
    assert imported["state"]["text"] == {"2": {"str": "hi"}}
    assert imported["image"]["resource_id"] == "image-restored"


def test_blank_canvas_project_round_trips(client):
    created = _create(
        client,
        resource_id="draft-blank",
        state={"v": 2, "layers": [], "imgWidth": 32, "imgHeight": 32},
    )
    exported = client.get(f"/api/imps/projects/{created['id']}/export").json()
    assert exported["state"]["layers"] == []
    imported = client.post(
        "/api/imps/projects/import",
        json={"provider": "files", "resource_id": "draft-2", "bundle": exported},
    ).json()
    assert imported["state"]["layers"] == []


def test_export_schema_endpoint(client):
    schema = client.get("/api/imps/export-schema").json()
    assert schema["kind"] == "imps-project"
    assert schema["schema_version"] == 1
    assert schema["lore_restore_limitation"] == "L-S19-LORE-RESTORE"


def test_missing_project_is_404(client):
    assert client.get("/api/imps/projects/nope").status_code == 404


def test_save_reports_honest_caller_owned_image_write(client):
    """Without image bytes the endpoint must not claim it wrote pixels."""
    created = _create(client)
    resp = client.post(
        f"/api/imps/projects/{created['id']}/save",
        json={
            "expected_project_revision": 1,
            "expected_image_revision": "rev-a",
            "new_image_revision": "rev-b",
            "state": {"v": 2, "layers": []},
        },
    )
    assert resp.status_code == 200, resp.text
    receipt = resp.json()["refresh_receipt"]
    assert receipt["image_write"] == "caller-owned"
    assert receipt["restore"]["limitation"] == "L-S19-LORE-RESTORE"


def test_save_without_gallery_bytes_reports_caller_owned(client):
    """Image bytes for a non-Gallery resource are honestly not written here."""
    created = _create(client)
    resp = client.post(
        f"/api/imps/projects/{created['id']}/save",
        json={
            "expected_project_revision": 1,
            "expected_image_revision": "rev-a",
            "new_image_revision": "rev-b",
            "image_bytes": "aW1hZ2UtYnl0ZXM=",
        },
    )
    assert resp.status_code == 409, resp.text
    assert "cannot be published" in resp.text


def test_restore_replays_captured_preimage(client):
    created = _create(client)
    saved = client.post(
        f"/api/imps/projects/{created['id']}/save",
        json={
            "expected_project_revision": 1,
            "expected_image_revision": "rev-a",
            "new_image_revision": "rev-b",
            "state": {"v": 2, "layers": [{"id": 1}, {"id": 2}]},
        },
    )
    assert saved.status_code == 200, saved.text
    action_id = saved.json()["action_id"]
    assert action_id

    restored = client.post(
        f"/api/imps/projects/{created['id']}/restore",
        json={"action_id": action_id},
    )
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["refresh_receipt"]["operation"] == "replay_restore"
    assert body["refresh_receipt"]["state_restored"] is True
    assert body["limitation"] == "L-S19-LORE-RESTORE"
    fetched = client.get(f"/api/imps/projects/{created['id']}").json()
    assert fetched["state"]["layers"] == [{"id": 1}]


def test_restore_unknown_action_is_an_error(client):
    created = _create(client)
    resp = client.post(
        f"/api/imps/projects/{created['id']}/restore",
        json={"action_id": "imps-save-does-not-exist"},
    )
    assert resp.status_code == 500

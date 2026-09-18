"""Shared Editor/Files equality and authority through disposable real storage."""

import json

import pytest
from fastapi.testclient import TestClient

import src.secret_storage as secret_storage
from routes.copal_routes import setup_copal_routes
from src.openclank.copal_resources import copal_resource_descriptor
from src.openclank.resource_refs import stable_resource_id
from test_files_facade_routes import _app


@pytest.fixture(autouse=True)
def isolated_app_key(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)


def test_copal_identity_survives_rename_and_revision_but_not_owner_or_workspace():
    document = {"id": "private-origin", "kind": "note", "name": "Before", "head": "h1"}
    first = copal_resource_descriptor(document, owner_account_id="account-a", workspace_id="study")
    renamed = copal_resource_descriptor(
        {**document, "name": "After", "head": "h2"}, owner_account_id="account-a", workspace_id="study",
    )
    assert first["key"] == renamed["key"]
    assert first["revision"] != renamed["revision"]
    assert "private-origin" not in json.dumps(first)
    for owner, workspace in [("account-b", "study"), ("account-a", "other")]:
        other = copal_resource_descriptor(document, owner_account_id=owner, workspace_id=workspace)
        assert first["key"]["resourceId"] != other["key"]["resourceId"]
    assert first["key"]["resourceId"] == stable_resource_id(
        owner_subject_id="account-a", provider="copal", origin_id="document:study:private-origin",
    )


def test_preserved_builtin_and_unqualified_adapters_never_advertise_writes():
    base = {"id": "origin", "kind": "note", "name": "Note", "head": "h1"}
    for patch, adapter in [({"rawPreserved": True}, True), ({"builtin": True}, True), ({}, False)]:
        resource = copal_resource_descriptor(
            {**base, **patch}, owner_account_id="account-a", workspace_id="default", writable_adapter=adapter,
        )
        assert resource["capabilities"]["read"]
        assert not resource["capabilities"]["edit"]
        assert not resource["capabilities"]["trash"]


def test_routed_copal_and_files_resolve_same_resource_without_exposing_origin(tmp_path):
    app, owner, _sessions = _app(tmp_path)
    app.include_router(setup_copal_routes())
    with TestClient(app) as client:
        created = client.post("/api/copal/documents?workspace=study", json={
            "name": "Identity.md", "kind": "note", "content": "body", "properties": {"status": "draft"},
        })
        assert created.status_code == 200, created.text
        document_id = created.json()["doc"]["id"]
        direct = client.get(f"/api/copal/documents/{document_id}?workspace=study").json()
        actor = client.get('/api/copal/status?workspace=study').json()['account_id']
        assert actor == direct['resource']['key']['accountId']
        assert actor != owner['value'], 'draft ownership uses immutable account identity'
        indexed = client.get("/api/copal/documents?workspace=study").json()["docs"]
        assert next(doc for doc in indexed if doc["id"] == document_id)["resource"] == direct["resource"]
        roots = client.get("/api/files-v1/roots?copal_workspace=study").json()["entries"]
        copal = next(row for row in roots if row["provider"] == "copal")
        folders = client.post("/api/files-v1/children", json={"parent_ref": copal["ref"]}).json()["entries"]
        folder = next(row for row in folders if row["name"] == "Documents")
        entries = client.post("/api/files-v1/children", json={"parent_ref": folder["ref"]}).json()["entries"]
        entry = next(row for row in entries if row["name"] == "Identity.md")
        exact = client.post("/api/files-v1/open-resource", json={"resource_ref": entry["ref"]})
        assert exact.status_code == 200, exact.text
        handle = exact.json()["payload"]["resource"]
        assert handle["key"] == direct["resource"]["key"]
        assert handle["key"]["resourceId"] == entry["id"]
        assert handle["revision"] == direct["resource"]["revision"]
        assert handle["capabilities"]["edit"], "managed Copal resource exposes its writable owner adapter"
        assert document_id not in exact.text
        saved = client.put(f'/api/copal/documents/{document_id}?workspace=study', json={
            'content': 'updated', 'base': direct['head'], 'properties': {'status': 'done'},
        })
        assert saved.status_code == 200, saved.text
        saved_doc = saved.json()['doc']
        assert saved_doc['resource']['key'] == direct['resource']['key']
        assert saved_doc['resource']['revision']['value'] == saved_doc['head']
        assert saved_doc['properties'] == {'status': 'done'}
        owner["value"] = "bob"
        stale_session = client.put(f'/api/copal/documents/{document_id}?workspace=study', headers={
            'X-Copal-Account': actor,
        }, json={'content': 'old actor draft', 'base': saved_doc['head']})
        assert stale_session.status_code == 409
        assert stale_session.json()['detail']['outcome'] == 'stale_session'
        denied = client.post("/api/files-v1/open-resource", json={"resource_ref": entry["ref"]})
        assert denied.status_code == 404, denied.text
        assert client.get("/api/copal/documents?workspace=study").json()["docs"] == []

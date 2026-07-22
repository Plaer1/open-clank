"""End-to-end route proof for Copal's authenticated owner boundary.

The Rust store has its own scope tests.  This matrix pins the other half of
the contract: browser-controlled JSON, query parameters, and document IDs
cannot replace the owner resolved by FastAPI authentication.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routes.copal_routes import CreateDocument, setup_copal_routes
from src.openclank.copal_bridge import CopalBridgeError


class ScopedBridge:
    """Small scope-enforcing bridge used to exercise the real HTTP routes."""

    def __init__(self, data_dir) -> None:
        self.data_dir = data_dir
        self.docs: dict[str, dict] = {}
        self.next_id = 1
        self.calls: list[tuple[str, dict]] = []
        (data_dir / "assets").mkdir()
        self.asset_path = data_dir / "assets" / "owned-asset.bin"
        self.asset_path.write_bytes(b"owned asset")

    def is_alive(self) -> bool:
        return True

    async def start(self) -> None:
        return None

    @staticmethod
    def _scope(args: dict) -> tuple[str, str]:
        owner = str(args.get("owner") or "")
        workspace = str(args.get("workspace_id") or "")
        if not owner or not workspace:
            raise CopalBridgeError("owner and workspace are required")
        return owner, workspace

    def _visible(self, args: dict) -> list[dict]:
        owner, workspace = self._scope(args)
        return [
            deepcopy(doc)
            for doc in self.docs.values()
            if doc["owner"] == owner
            and doc["workspace_id"] == workspace
            and not doc["deleted"]
        ]

    def _owned(self, args: dict, *, deleted: bool = False) -> dict:
        owner, workspace = self._scope(args)
        doc = self.docs.get(str(args.get("id") or ""))
        if (
            not doc
            or doc["owner"] != owner
            or doc["workspace_id"] != workspace
            or (args.get("corpus") and doc["corpus"] != args["corpus"])
            or doc["deleted"] is not deleted
        ):
            raise CopalBridgeError("doc not found in this scope")
        return doc

    async def call(self, operation: str, args: dict, timeout: float = 20):
        del timeout
        self.calls.append((operation, deepcopy(args)))
        if operation == "scoped_status":
            docs = self._visible(args)
            kinds: dict[str, int] = {}
            for doc in docs:
                kinds[doc["kind"]] = kinds.get(doc["kind"], 0) + 1
            return {
                "schema_version": 3,
                "documents": len(docs),
                "kinds": kinds,
                "integrity_ok": True,
            }
        if operation in {"index", "list", "search", "export_snapshot"}:
            return {"docs": self._visible(args)}
        if operation == "trash":
            owner, workspace = self._scope(args)
            return {
                "docs": [
                    deepcopy(doc)
                    for doc in self.docs.values()
                    if doc["owner"] == owner
                    and doc["workspace_id"] == workspace
                    and doc["corpus"] == str(args.get("corpus") or "notes")
                    and doc["deleted"]
                ]
            }
        if operation == "create":
            owner, workspace = self._scope(args)
            doc_id = f"DOC{self.next_id}"
            self.next_id += 1
            corpus = str(args.get("corpus") or "notes")
            doc = {
                "id": doc_id,
                "owner": owner,
                "workspace_id": workspace,
                "name": str(args["name"]),
                "kind": str(args["kind"]),
                "corpus": corpus,
                "head": f"HEAD-{doc_id}-1",
                "text": str(args.get("content") or ""),
                "deleted": False,
                "hidden": False,
            }
            self.docs[doc_id] = doc
            return {"doc": deepcopy(doc), "outcome": "committed"}
        if operation == "get":
            return deepcopy(self._owned(args))
        if operation == "write":
            doc = self._owned(args)
            doc["text"] = str(args.get("content") or "")
            doc["head"] += "-next"
            return {"doc": deepcopy(doc), "outcome": "committed"}
        if operation == "history":
            doc = self._owned(args)
            return {"doc": doc["id"], "changes": [{"commit": doc["head"]}]}
        if operation in {"checkpoint", "restore"}:
            return {"doc": deepcopy(self._owned(args)), "outcome": "committed"}
        if operation == "rename":
            doc = self._owned(args)
            doc["name"] = str(args["name"])
            return {"doc": deepcopy(doc), "outcome": "committed"}
        if operation == "delete":
            doc = self._owned(args)
            doc["deleted"] = True
            return {"deleted": True, "id": doc["id"]}
        if operation == "restore_deleted":
            doc = self._owned(args, deleted=True)
            doc["deleted"] = False
            return {"doc": deepcopy(doc), "outcome": "committed"}
        if operation == "asset_path":
            doc = self._owned(args)
            if doc["kind"] != "asset":
                raise CopalBridgeError("asset not found in this scope")
            return {"path": str(self.asset_path), "name": doc["name"], "size": 11}
        if operation == "import_vault":
            self._scope(args)
            return {"created": 0, "updated": 0, "unchanged": 1}
        raise AssertionError(f"unexpected bridge operation: {operation}")


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()

    @app.middleware("http")
    async def test_identity(request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    app.include_router(setup_copal_routes())
    app.state.copal_bridge = ScopedBridge(tmp_path)
    app.state.auth_manager = SimpleNamespace(
        is_configured=True,
        get_username_for_token={"alice-token": "alice", "bob-token": "bob"}.get,
    )
    return TestClient(app)


def _headers(owner: str) -> dict[str, str]:
    return {"x-test-user": owner}


def _create(http: TestClient, owner: str, *, workspace: str = "default") -> dict:
    response = http.post(
        f"/api/copal/documents?workspace={workspace}",
        headers=_headers(owner),
        json={"name": "Same name.md", "content": f"private to {owner}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["doc"]


def test_authenticated_users_and_workspaces_are_isolated(client):
    alice_default = _create(client, "alice")
    alice_personal = _create(client, "alice", workspace="personal")
    bob_default = _create(client, "bob")

    alice_docs = client.get("/api/copal/documents", headers=_headers("alice")).json()["docs"]
    bob_docs = client.get("/api/copal/documents", headers=_headers("bob")).json()["docs"]
    personal_docs = client.get(
        "/api/copal/documents?workspace=personal", headers=_headers("alice")
    ).json()["docs"]

    assert [doc["id"] for doc in alice_docs] == [alice_default["id"]]
    assert [doc["id"] for doc in bob_docs] == [bob_default["id"]]
    assert [doc["id"] for doc in personal_docs] == [alice_personal["id"]]

    alice_status = client.get("/api/copal/status", headers=_headers("alice")).json()
    bob_status = client.get("/api/copal/status", headers=_headers("bob")).json()
    assert alice_status["documents"] == bob_status["documents"] == 1
    assert alice_status["owner"] == "alice"
    assert bob_status["owner"] == "bob"


@pytest.mark.parametrize(
    ("method", "suffix", "json_body"),
    [
        ("get", "", None),
        ("put", "", {"content": "steal", "base": None}),
        ("get", "/history", None),
        ("post", "/checkpoint", {"message": "steal"}),
        ("post", "/rename", {"name": "Stolen.md"}),
        ("post", "/restore", {"commit": "HEAD-DOC1-1"}),
        ("delete", "", None),
    ],
)
def test_raw_document_id_cannot_cross_owner_boundary(client, method, suffix, json_body):
    alice = _create(client, "alice")
    response = client.request(
        method,
        f"/api/copal/documents/{alice['id']}{suffix}",
        headers=_headers("bob"),
        json=json_body,
    )

    assert response.status_code == 404
    still_owned = client.get(
        f"/api/copal/documents/{alice['id']}", headers=_headers("alice")
    )
    assert still_owned.status_code == 200
    assert still_owned.json()["text"] == "private to alice"


def test_client_cannot_supply_or_override_owner(client):
    rejected = client.post(
        "/api/copal/documents",
        headers=_headers("alice"),
        json={
            "name": "Spoof.md",
            "content": "mine now",
            "owner": "bob",
            "workspace_id": "default",
        },
    )
    assert rejected.status_code == 422

    created = _create(client, "alice")
    assert created["owner"] == "alice"


def test_trash_restore_and_assets_cannot_cross_owner_boundary(client):
    alice = _create(client, "alice")
    assert client.delete(
        f"/api/copal/documents/{alice['id']}", headers=_headers("alice")
    ).status_code == 200

    assert client.get("/api/copal/trash", headers=_headers("bob")).json()["docs"] == []
    assert client.post(
        f"/api/copal/trash/{alice['id']}/restore", headers=_headers("bob")
    ).status_code == 404
    assert [
        doc["id"]
        for doc in client.get("/api/copal/trash", headers=_headers("alice")).json()["docs"]
    ] == [alice["id"]]
    assert client.post(
        f"/api/copal/trash/{alice['id']}/restore", headers=_headers("alice")
    ).status_code == 200

    asset = client.post(
        "/api/copal/documents",
        headers=_headers("alice"),
        json={"name": "private.bin", "kind": "asset", "content": ""},
    ).json()["doc"]
    assert client.get(
        f"/api/copal/assets/{asset['id']}", headers=_headers("bob")
    ).status_code == 404
    owned = client.get(f"/api/copal/assets/{asset['id']}", headers=_headers("alice"))
    assert owned.status_code == 200
    assert owned.content == b"owned asset"


def test_search_export_and_import_keep_the_authenticated_scope(client):
    _create(client, "alice")
    _create(client, "bob")

    bob_search = client.get(
        "/api/copal/documents?query=private", headers=_headers("bob")
    ).json()["docs"]
    assert [doc["owner"] for doc in bob_search] == ["bob"]

    exported = client.get("/api/copal/export/obsidian", headers=_headers("bob"))
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        payload = b"\n".join(archive.read(name) for name in archive.namelist())
    assert b"private to bob" in payload
    assert b"private to alice" not in payload

    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("Imported.md", "# Mine\n")
    imported = client.post(
        "/api/copal/import/obsidian?workspace=personal",
        headers=_headers("bob"),
        files={"file": ("vault.zip", archive_bytes.getvalue(), "application/zip")},
    )
    assert imported.status_code == 200, imported.text
    import_args = next(
        args
        for operation, args in reversed(client.app.state.copal_bridge.calls)
        if operation == "import_vault"
    )
    assert import_args["owner"] == "bob"
    assert import_args["workspace_id"] == "personal"
    assert "owner" not in imported.json()

import io
import json
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.copal_routes as copal_routes

from routes.copal_routes import (
    _encode_note,
    _import_markdown_record,
    _note_blocks,
    _note_markdown,
    _note_view,
    _require_mutable_note,
    _stream_owner_is_current,
    setup_copal_routes,
)


ROOT = Path(__file__).resolve().parents[1]


def test_note_block_ids_survive_insertions_before_unchanged_content():
    original = _note_blocks("Alpha\nBeta")
    alpha_id, beta_id = [block["id"] for block in original]

    inserted = _note_blocks("New\nAlpha\nBeta", original)

    assert inserted[0]["id"] not in {alpha_id, beta_id}
    assert inserted[1]["id"] == alpha_id
    assert inserted[2]["id"] == beta_id


def test_preserved_database_note_rejects_content_mutation():
    with pytest.raises(HTTPException) as error:
        _require_mutable_note({"kind": "note", "rawPreserved": True, "note_error": "future schema"})

    assert error.value.status_code == 409


def test_imported_markdown_is_deterministic_and_exactly_exportable():
    source = "---\ntags: [alpha, beta]\ndue: 2026-07-19\n---\n\n# Welcome\n\n```dataview\nTABLE due\n```\n"
    first, diagnostics = _import_markdown_record(source, "Welcome.md")
    second, repeated_diagnostics = _import_markdown_record(source, "Welcome.md")

    assert first == second
    assert diagnostics == repeated_diagnostics == []
    document = _note_view({"id": "DOC1", "kind": "note", "name": "Welcome.md", "text": first})
    assert document["properties"] == {"tags": ["alpha", "beta"], "due": "2026-07-19"}
    assert document["extensions"]["compatibility"] == [
        {"kind": "plugin-query-block", "language": "dataview", "execution": "inert"}
    ]
    assert _note_markdown(document) == source


def test_editing_imported_markdown_marks_snapshot_modified_and_exports_projection():
    source = "---\nstatus: draft\n---\n\nOriginal\n"
    encoded, _ = _import_markdown_record(source, "Article.md")
    stored = json.loads(encoded)
    edited = _encode_note(
        "Edited",
        {"status": "final"},
        previous=stored,
    )
    document = _note_view({"id": "DOC1", "kind": "wiki", "name": "Article.md", "text": edited})

    assert document["corpus"] == "wiki"
    assert document["extensions"]["interchange"]["modified"] is True
    assert _note_markdown(document) != source
    assert "status: \"final\"" in _note_markdown(document)
    assert _note_markdown(document).endswith("Edited")


class FakeBridge:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.calls = []
        self.imported_files = {}
        self.write_result = {"outcome": "committed", "doc": {"id": "DOC1"}}

    def is_alive(self):
        return True

    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, args, timeout))
        if operation in {"status", "scoped_status"}:
            return {"schema_version": 2, "documents": 1, "integrity_ok": True, "kinds": {"markdown": 1}}
        if operation == "list":
            return {"docs": [{"id": "DOC1"}]}
        if operation == "index":
            return {"docs": []}
        if operation == "write":
            return self.write_result
        if operation == "ops":
            return {"ops": []}
        if operation == "import_vault":
            root = Path(args["path"])
            self.imported_files = {
                str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            return {
                "notes": 1,
                "assets": 0,
                "compatibility": 0,
                "unchanged": 0,
                "planning": True,
                "treehouse": True,
                "entries": [],
                "op": "IMPORT1",
            }
        if operation == "export_snapshot":
            return {
                "docs": [
                    {
                        "id": "DOC1",
                        "kind": "markdown",
                        "name": "Notes/Hello.md",
                        "text": "# Hello\n",
                        "owner": "local",
                        "head": "secret-operational-head",
                    }
                ]
            }
        if operation == "asset_path":
            return {"path": str(self.data_dir / "assets" / "asset.png"), "name": "Images/asset.png", "size": 7}
        return {"outcome": "created", "doc": {"id": "DOC1"}}


class RestartingBridge(FakeBridge):
    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.alive = False
        self.start_calls = 0

    def is_alive(self):
        return self.alive

    async def start(self):
        self.start_calls += 1
        self.alive = True


class VisibilityBridge(FakeBridge):
    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, args, timeout))
        if operation == "index":
            return {
                "docs": [
                    {"id": "VISIBLE", "kind": "note", "name": "Visible", "text": "visible", "hidden": False},
                    {"id": "HIDDEN", "kind": "note", "name": ".private/Hidden", "text": "hidden", "hidden": True},
                    {"id": "COMPAT", "kind": "compatibility", "name": ".app/config.json", "text": "", "hidden": True},
                ]
            }
        return await super().call(operation, args, timeout)


class MixedCorpusExportBridge(FakeBridge):
    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, args, timeout))
        if operation == "export_snapshot":
            note, _ = _import_markdown_record("# Notes version\n", "Same.md")
            wiki, _ = _import_markdown_record("# Wiki version\n", "Same.md")
            return {
                "docs": [
                    {"id": "NOTE", "corpus": "notes", "kind": "note", "name": "Same.md", "text": note},
                    {"id": "WIKI", "corpus": "wiki", "kind": "wiki", "name": "Same.md", "text": wiki},
                ]
            }
        return await super().call(operation, args, timeout)


class MissingAssetExportBridge(FakeBridge):
    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, args, timeout))
        if operation == "export_snapshot":
            return {
                "docs": [
                    {"id": "MISSING", "corpus": "notes", "kind": "asset", "name": "missing.bin"},
                ]
            }
        return await super().call(operation, args, timeout)


class BaseBridge(FakeBridge):
    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.docs = {
            "BASE": {
                "id": "BASE", "kind": "base", "name": "Projects.base", "head": "base-head", "text": """
version: 1
views:
  - id: table
    name: Table
    columns: [file.name, status]
    filters:
      property: status
      operator: eq
      value: active
    sorts:
      - property: file.name
        direction: asc
""", "frontmatter": {}, "tags": [], "links": [],
            },
            "A": {"id": "A", "kind": "markdown", "name": "A.md", "head": "a-head", "text": "---\nstatus: active\n---\nA\n", "frontmatter": {"status": "active"}, "tags": [], "links": []},
            "B": {"id": "B", "kind": "markdown", "name": "B.md", "head": "b-head", "text": "---\nstatus: parked\n---\nB\n", "frontmatter": {"status": "parked"}, "tags": [], "links": []},
        }

    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, args, timeout))
        if operation == "get":
            return self.docs[args["id"]]
        if operation == "index":
            docs = list(self.docs.values())
            if args.get("kind"):
                docs = [doc for doc in docs if doc["kind"] == args["kind"]]
            return {"docs": docs}
        if operation == "write":
            doc = self.docs[args["id"]]
            if args.get("base") == "stale":
                return {"outcome": "stale", "doc": doc}
            doc["text"] = args["content"]
            doc["head"] = f"{doc['head']}-next"
            if doc["kind"] == "markdown" and "status:" in doc["text"]:
                status = doc["text"].split("status:", 1)[1].splitlines()[0].strip().strip('"')
                doc["frontmatter"]["status"] = status
            return {"outcome": "committed", "doc": doc}
        return await super().call(operation, args, timeout)


class NoteBridge(FakeBridge):
    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.counter = 1
        self.docs = {
            "TARGET": {
                "id": "TARGET", "kind": "markdown", "name": "Target.md", "head": "target-head",
                "text": "# Target\n", "frontmatter": {}, "tags": [], "links": [],
            },
            "BASE": {
                "id": "BASE", "kind": "base", "name": "Notes.base", "head": "base-head",
                "text": "version: 1\nviews:\n  - id: table\n    name: Table\n    columns: [file.name, status]\n",
                "frontmatter": {}, "tags": [], "links": [],
            },
        }

    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, args, timeout))
        if operation in {"index", "export_snapshot"}:
            docs = list(self.docs.values())
            if args.get("kind"):
                docs = [doc for doc in docs if doc["kind"] == args["kind"]]
            return {"docs": docs}
        if operation == "get":
            return self.docs[args["id"]]
        if operation == "create":
            document_id = f"NOTE{self.counter}"
            self.counter += 1
            doc = {
                "id": document_id, "kind": args["kind"], "name": args["name"], "head": f"{document_id}-head",
                "text": args["content"], "frontmatter": {}, "tags": [], "links": [],
            }
            self.docs[document_id] = doc
            return {"outcome": "created", "doc": doc}
        if operation == "write":
            doc = self.docs[args["id"]]
            if args.get("base") and args["base"] != doc["head"]:
                return {"outcome": "stale", "doc": doc}
            doc["text"] = args["content"]
            doc["head"] += "x"
            return {"outcome": "committed", "doc": doc}
        return await super().call(operation, args, timeout)


class PlanningBridge(FakeBridge):
    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.counter = 1
        self.docs = {
            "LEGACY": {
                "id": "LEGACY", "kind": "planning", "name": ".copal/planning.json", "head": "h1",
                "text": json.dumps({
                    "title": "Move",
                    "tracks": [{
                        "id": "home", "name": "Home", "color": "#14b8a6", "icon": "home", "enabled": True,
                        "tasks": [{
                            "id": "task-1", "title": "Pack", "description": "boxes", "startDate": "2026-07-10",
                            "dueDate": "2026-07-12", "status": "pending", "priority": "high", "tags": ["move"],
                            "sharedTrackIds": [], "stages": [{"id": "s1", "title": "Books", "done": False}],
                            "futureField": {"preserve": True},
                        }],
                    }],
                    "floatingTodos": [],
                }),
                "frontmatter": {}, "tags": [], "links": [],
            }
        }

    def _index_doc(self, doc):
        result = dict(doc)
        result.setdefault("frontmatter", {})
        result.setdefault("tags", [])
        result.setdefault("links", [])
        return result

    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, args, timeout))
        if operation == "index":
            docs = [self._index_doc(doc) for doc in self.docs.values()]
            if args.get("kind"):
                docs = [doc for doc in docs if doc["kind"] == args["kind"]]
            return {"docs": docs}
        if operation == "get":
            if args["id"] not in self.docs:
                raise RuntimeError("not found")
            return self._index_doc(self.docs[args["id"]])
        if operation == "create":
            document_id = f"DOC{self.counter}"
            self.counter += 1
            doc = {
                "id": document_id, "kind": args["kind"], "name": args["name"], "head": f"{document_id}-h1",
                "text": args["content"], "frontmatter": {}, "tags": [], "links": [],
            }
            self.docs[document_id] = doc
            return {"outcome": "created", "doc": self._index_doc(doc)}
        if operation == "write":
            doc = self.docs[args["id"]]
            if args.get("base") and args["base"] != doc["head"]:
                return {"outcome": "stale", "doc": self._index_doc(doc)}
            doc["text"] = args["content"]
            doc["head"] += "x"
            return {"outcome": "committed", "doc": self._index_doc(doc)}
        if operation == "delete":
            doc = self.docs.pop(args["id"])
            return {"outcome": "deleted", "doc": self._index_doc(doc)}
        return await super().call(operation, args, timeout)


def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    app.state.copal_bridge = FakeBridge(tmp_path)
    return TestClient(app), app.state.copal_bridge


def test_scope_is_server_owned_and_workspace_validated(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    response = http.get("/api/copal/documents?workspace=personal")
    assert response.status_code == 200
    assert bridge.calls[-1][1]["owner"] == "local"
    assert bridge.calls[-1][1]["workspace_id"] == "personal"
    assert http.get("/api/copal/documents?workspace=../escape").status_code == 400


def test_status_storage_namespace_distinguishes_local_user_from_auth_disabled_local(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    local = http.get("/api/copal/status").json()
    assert local["owner"] == "local"
    assert local["storage_namespace"] == "local"

    monkeypatch.setattr(copal_routes, "require_user", lambda _request: "local")
    authenticated = http.get("/api/copal/status").json()
    assert authenticated["owner"] == "user:local"
    assert authenticated["storage_namespace"] == "user:local"
    assert bridge.calls[-1][1]["owner"] == "user:local"


def test_admin_operations_are_scoped_to_the_authenticated_owner(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    monkeypatch.setattr(copal_routes, "require_admin", lambda _request: None)
    monkeypatch.setattr(copal_routes, "require_user", lambda _request: "alice")

    response = http.get("/api/copal/operations?workspace=home&owner=bob&limit=7")

    assert response.status_code == 200
    assert bridge.calls[-1] == (
        "ops",
        {"owner": "alice", "workspace_id": "home", "limit": 7},
        20,
    )


def test_copal_stream_session_revalidation_closes_on_rename_or_revocation():
    sessions = {"token": "alice"}
    manager = SimpleNamespace(get_username_for_token=lambda token: sessions.get(token))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(auth_manager=manager)))

    assert _stream_owner_is_current(request, "alice", "token") is True
    sessions["token"] = "alice2"
    assert _stream_owner_is_current(request, "alice", "token") is False
    sessions.clear()
    assert _stream_owner_is_current(request, "alice", "token") is False
    assert _stream_owner_is_current(request, "", None) is True


def test_hidden_document_query_is_explicit_and_ui_requests_full_projection(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    http.app.state.copal_bridge = VisibilityBridge(tmp_path)

    visible = http.get("/api/copal/documents").json()["docs"]
    included = http.get("/api/copal/documents?hidden=include").json()["docs"]
    hidden = http.get("/api/copal/documents?hidden=only").json()["docs"]

    assert [document["id"] for document in visible] == ["VISIBLE"]
    assert [document["id"] for document in included] == ["VISIBLE", "HIDDEN"]
    assert [document["id"] for document in hidden] == ["HIDDEN"]
    assert "api('/documents?hidden=include')" in (ROOT / "static/js/copal.js").read_text()


def test_dead_bridge_restarts_before_copal_operation(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    bridge = RestartingBridge(tmp_path)
    http.app.state.copal_bridge = bridge

    response = http.get("/api/copal/status")

    assert response.status_code == 200
    assert bridge.start_calls == 1
    assert bridge.is_alive()
    operation, args, _ = bridge.calls[-1]
    assert operation == "scoped_status"
    assert args == {"owner": "local", "workspace_id": "default"}
    assert response.json()["visible_documents"] == 1
    assert response.json()["owner"] == "local"


def test_document_names_cannot_traverse(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    response = http.post(
        "/api/copal/documents",
        json={"name": "../outside.md", "kind": "markdown", "content": "no"},
    )
    assert response.status_code == 400


def test_stale_write_surfaces_authoritative_head(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    bridge.write_result = {"outcome": "stale", "doc": {"id": "DOC1", "head": "new-head"}}
    response = http.put("/api/copal/documents/DOC1", json={"content": "mine", "base": "old-head"})
    assert response.status_code == 409
    assert response.json()["detail"]["doc"]["head"] == "new-head"


def test_database_note_envelope_is_structured_lossless_and_indexed(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = NoteBridge(tmp_path)
    app.state.copal_bridge = bridge
    http = TestClient(app)
    body = "# Native\n  - [ ] nested\n* bullet\n___\n  > quote\nSee [[Target#Proof]] and #native"
    created = http.post(
        "/api/copal/documents",
        json={
            "name": "Ideas/Native",
            "content": body,
            "properties": {"status": "active", "score": 7, "settings": {"dense": True}, "tags": ["database"]},
            "relations": [{"kind": "link", "target": "Target", "targetDocumentId": "TARGET", "fragment": "Proof"}],
        },
    )
    assert created.status_code == 200
    note = created.json()["doc"]
    assert note["kind"] == "note"
    assert note["storage"] == "database"
    assert note["text"] == body
    assert note["properties"]["settings"] == {"dense": True}
    assert note["relations"][0]["targetDocumentId"] == "TARGET"
    assert note["tasks"][0]["text"] == "nested"
    assert {"native", "database"}.issubset(note["tags"])

    stored = json.loads(bridge.docs[note["id"]]["text"])
    assert stored["body"]["type"] == "doc"
    assert "\n".join(block["source"] for block in stored["body"]["blocks"]) == body
    assert all(block["id"].startswith("blk_") for block in stored["body"]["blocks"])
    assert all(property_["id"].startswith("prop_") for property_ in stored["properties"])
    assert stored["relations"][0]["id"] in stored["body"]["blocks"][-1]["relationIds"]
    first_block = stored["body"]["blocks"][0]["id"]

    updated_body = body.replace("# Native", "# Native note", 1)
    updated = http.put(
        f"/api/copal/documents/{note['id']}",
        json={
            "content": updated_body,
            "base": note["head"],
            "properties": {**note["properties"], "status": "done"},
            "relations": [
                {"kind": "link", "target": "Target", "targetDocumentId": "TARGET", "fragment": "Proof"},
                {"kind": "parent", "target": "Target", "targetDocumentId": "TARGET"},
            ],
        },
    )
    assert updated.status_code == 200
    fresh = http.get(f"/api/copal/documents/{note['id']}").json()
    assert fresh["text"] == updated_body
    assert fresh["properties"]["status"] == "done"
    rewritten = json.loads(bridge.docs[note["id"]]["text"])
    assert rewritten["body"]["blocks"][0]["id"] == first_block
    assert any(relation["kind"] == "parent" and relation["origin"] == "explicit" for relation in rewritten["relations"])

    assert [doc["id"] for doc in http.get("/api/copal/documents?query=done").json()["docs"]] == [note["id"]]
    assert http.get("/api/copal/documents?query=schemaVersion").json()["docs"] == []

    stale = http.put(
        f"/api/copal/documents/{note['id']}",
        json={"content": "mine", "base": "stale", "properties": fresh["properties"]},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["doc"]["text"] == updated_body


def test_base_edits_native_properties_and_markdown_export_is_only_an_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = NoteBridge(tmp_path)
    app.state.copal_bridge = bridge
    http = TestClient(app)
    created = http.post(
        "/api/copal/documents",
        json={"name": "Native", "content": "Body", "properties": {"status": "active"}},
    ).json()["doc"]
    block_id = created["blocks"][0]["id"]

    patched = http.patch(
        f"/api/copal/bases/BASE/rows/{created['id']}",
        json={"property": "status", "value": "parked", "base": created["head"]},
    )
    assert patched.status_code == 200
    fresh = http.get(f"/api/copal/documents/{created['id']}").json()
    assert fresh["properties"]["status"] == "parked"
    assert fresh["blocks"][0]["id"] == block_id
    assert not fresh["text"].startswith("---")

    exported = http.get("/api/copal/export/obsidian")
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        markdown = archive.read("Native.md").decode()
    assert markdown.startswith('---\nstatus: "parked"\n---\n\nBody')


def test_trash_restore_keeps_server_owned_scope(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)

    assert http.get("/api/copal/trash?workspace=personal").status_code == 200
    # trash now queries both notes and wiki stores
    notes_call = next(call for call in bridge.calls if call[1].get("corpus") == "notes")
    wiki_call = next(call for call in bridge.calls if call[1].get("corpus") == "wiki")
    assert notes_call[1] == {"owner": "local", "workspace_id": "personal", "corpus": "notes"}
    assert wiki_call[1] == {"owner": "local", "workspace_id": "personal", "corpus": "wiki"}
    assert http.post("/api/copal/trash/DOC1/restore?workspace=personal").status_code == 200
    assert bridge.calls[-1][1]["owner"] == "local"
    assert bridge.calls[-1][1]["workspace_id"] == "personal"
    assert bridge.calls[-1][1]["id"] == "DOC1"


def test_obsidian_export_is_scoped_and_scrubs_operational_metadata(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    response = http.get("/api/copal/export/obsidian?workspace=personal")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("Notes/Hello.md") == b"# Hello\n"
        manifest = archive.read(".copal/export-manifest.json").decode()
    assert "secret-operational-head" not in manifest
    assert '"workspace": "personal"' in manifest
    assert int(response.headers["content-length"]) == len(response.content)
    export_call = next(call for call in bridge.calls if call[0] == "export_snapshot")
    assert export_call[1] == {"owner": "local", "workspace_id": "personal"}


def test_mixed_note_and_wiki_export_uses_collision_free_round_trip_layout(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    bridge = MixedCorpusExportBridge(tmp_path)
    http.app.state.copal_bridge = bridge

    exported = http.get("/api/copal/export/obsidian?workspace=personal")

    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert archive.read("Same.md") == b"# Notes version\n"
        assert archive.read(".copal/wiki/Same.md") == b"# Wiki version\n"
        assert len(archive.namelist()) == len(set(archive.namelist()))
        manifest = json.loads(archive.read(".copal/export-manifest.json"))
    assert {item["corpus"] for item in manifest["documents"]} == {"notes", "wiki"}

    imported = http.post(
        "/api/copal/import/obsidian?workspace=personal",
        files={"file": ("round-trip.zip", exported.content, "application/zip")},
    )

    assert imported.status_code == 200
    assert json.loads(bridge.imported_files["Same.md"])["schemaVersion"] == 1
    assert json.loads(bridge.imported_files[".copal/wiki/Same.md"])["schemaVersion"] == 1
    import_call = next(call for call in reversed(bridge.calls) if call[0] == "import_vault")
    assert import_call[1]["note_kind"] == "note"
    assert import_call[1]["restore_ids"] == {
        "Same.md": {"id": "NOTE", "corpus": "notes", "kind": "note"},
        ".copal/wiki/Same.md": {"id": "WIKI", "corpus": "wiki", "kind": "wiki"},
    }
    assert imported.json()["restoreManifest"] == {"present": True, "identities": 2}


def test_copal_export_excludes_shared_seeds_and_manifest_tampering_fails_closed(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)

    class SharedExportBridge(FakeBridge):
        async def call(self, operation, args, timeout=20):
            if operation == "export_snapshot":
                return {
                    "docs": [
                        {
                            "id": "LOCAL",
                            "corpus": "notes",
                            "kind": "markdown",
                            "name": "Local.md",
                            "text": "local\n",
                            "readOnly": False,
                        },
                        {
                            "id": "SHARED",
                            "corpus": "notes",
                            "kind": "markdown",
                            "name": "Shared.md",
                            "text": "shared\n",
                            "readOnly": True,
                        },
                    ]
                }
            return await super().call(operation, args, timeout)

    http.app.state.copal_bridge = SharedExportBridge(tmp_path)
    exported = http.get("/api/copal/export/obsidian?workspace=personal")
    assert exported.status_code == 200
    payload = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(exported.content)) as source, zipfile.ZipFile(payload, "w") as changed:
        assert "Shared.md" not in source.namelist()
        for name in source.namelist():
            content = source.read(name)
            if name == "Local.md":
                content += b"tampered"
            changed.writestr(name, content)

    rejected = http.post(
        "/api/copal/import/obsidian?workspace=personal",
        files={"file": ("tampered.zip", payload.getvalue(), "application/zip")},
    )
    assert rejected.status_code == 400
    assert "does not match archive content" in rejected.json()["detail"]


def test_export_fails_closed_when_versioned_asset_bytes_are_missing(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    http.app.state.copal_bridge = MissingAssetExportBridge(tmp_path)

    response = http.get("/api/copal/export/obsidian")

    assert response.status_code == 409
    assert response.json()["detail"].startswith("Export integrity failure")


def test_obsidian_import_is_bounded_scoped_and_uses_temporary_vault(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("Welcome.md", "# Welcome\n")
        archive.writestr(".copal/planning.json", '{"tracks":[]}')
        archive.writestr(".copal/treehouse-state.json", '{"schemaVersion":1}')

    response = http.post(
        "/api/copal/import/obsidian?workspace=personal",
        files={"file": ("vault.zip", payload.getvalue(), "application/zip")},
    )

    assert response.status_code == 200
    assert response.json()["imported"]["treehouse"] is True
    call = next(call for call in bridge.calls if call[0] == "import_vault")
    assert call[1]["owner"] == "local"
    assert call[1]["workspace_id"] == "personal"
    assert call[1]["note_kind"] == "note"
    assert call[1]["planning_path"].endswith("/.copal/planning.json")
    imported = json.loads(bridge.imported_files["Welcome.md"])
    assert imported["schemaVersion"] == 1
    assert imported["extensions"]["interchange"]["source"] == "# Welcome\n"
    assert imported["extensions"]["interchange"]["modified"] is False
    assert not Path(call[1]["path"]).exists()


def test_obsidian_import_can_target_the_separate_wiki_corpus(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("Article.md", "# Article\n")

    response = http.post(
        "/api/copal/import/obsidian?workspace=personal&corpus=wiki",
        files={"file": ("wiki.zip", payload.getvalue(), "application/zip")},
    )

    assert response.status_code == 200
    call = next(call for call in bridge.calls if call[0] == "import_vault")
    assert call[1]["note_kind"] == "wiki"
    assert response.json()["corpus"] == "wiki"


def test_obsidian_import_rejects_zip_traversal(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../escape.md", "no")

    response = http.post(
        "/api/copal/import/obsidian",
        files={"file": ("bad.zip", payload.getvalue(), "application/zip")},
    )

    assert response.status_code == 400
    assert not any(call[0] == "import_vault" for call in bridge.calls)


def test_obsidian_import_rejects_duplicate_member_names(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("duplicate.md", "first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("duplicate.md", "second")

    response = http.post(
        "/api/copal/import/obsidian",
        files={"file": ("duplicate.zip", payload.getvalue(), "application/zip")},
    )

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Duplicate ZIP member")
    assert not any(call[0] == "import_vault" for call in bridge.calls)


def test_obsidian_import_rejects_casefold_path_collisions(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("Folder/Note.md", "first")
        archive.writestr("folder/note.md", "second")

    response = http.post(
        "/api/copal/import/obsidian",
        files={"file": ("colliding.zip", payload.getvalue(), "application/zip")},
    )

    assert response.status_code == 400
    assert "portable filesystems" in response.json()["detail"]
    assert not any(call[0] == "import_vault" for call in bridge.calls)


def test_obsidian_import_rejects_symlinks_and_zip_bombs(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    symlink_payload = io.BytesIO()
    with zipfile.ZipFile(symlink_payload, "w") as archive:
        link = zipfile.ZipInfo("linked.md")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "outside.md")
    symlink_response = http.post(
        "/api/copal/import/obsidian",
        files={"file": ("symlink.zip", symlink_payload.getvalue(), "application/zip")},
    )

    bomb_payload = io.BytesIO()
    with zipfile.ZipFile(bomb_payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb.md", b"0" * (1024 * 1024))
    bomb_response = http.post(
        "/api/copal/import/obsidian",
        files={"file": ("bomb.zip", bomb_payload.getvalue(), "application/zip")},
    )

    assert symlink_response.status_code == 400
    assert bomb_response.status_code == 413
    assert "compression ratio" in bomb_response.json()["detail"]
    assert not any(call[0] == "import_vault" for call in bridge.calls)


def test_obsidian_import_preserves_binary_and_dot_namespace_payloads(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("Note.md", "# Note\n")
        archive.writestr("Media/reference.pdf", b"PDF\x00bytes")
        archive.writestr("Media/demo.mp4", b"MP4\x00bytes")
        archive.writestr(".obsidian/community-plugins.json", b'["dataview"]')

    response = http.post(
        "/api/copal/import/obsidian",
        files={"file": ("complete.zip", payload.getvalue(), "application/zip")},
    )

    assert response.status_code == 200
    assert bridge.imported_files["Media/reference.pdf"] == b"PDF\x00bytes"
    assert bridge.imported_files["Media/demo.mp4"] == b"MP4\x00bytes"
    assert bridge.imported_files[".obsidian/community-plugins.json"] == b'["dataview"]'
    assert response.json()["preparation"]["preparedDatabaseNotes"] == 1


def test_asset_delivery_is_scoped_to_owned_asset_directory(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "asset.png").write_bytes(b"png-data")

    response = http.get("/api/copal/assets/DOC1?workspace=personal")

    assert response.status_code == 200
    assert response.content == b"png-data"
    assert response.headers["content-type"] == "image/png"
    assert bridge.calls[-1][1] == {"owner": "local", "workspace_id": "personal", "id": "DOC1"}


def test_copal_sidebar_has_all_native_submenus_in_stable_order():
    html = (ROOT / "static/index.html").read_text()
    expected = ["notes", "wiki", "timeline", "galaxy", "graph", "mind", "bases", "treehouse", "todo"]
    positions = [html.index(f'data-copal-view="{view}"') for view in expected]
    assert positions == sorted(positions)
    assert '<iframe' not in html[positions[0]:positions[-1]]
    assert 'data-copal-view="calendar"' not in html


def test_copal_frontend_uses_only_canonical_adapter():
    source = (ROOT / "static/js/copal.js").read_text()
    assert "/api/copal" in source
    assert "/api/notes" not in source
    assert "/api/note" not in source
    assert "/api/data" not in source
    assert "new EventSource" in source
    assert "Export for Obsidian" in source


def test_copal_reuses_native_window_calendar_and_sidebar_contracts():
    source = (ROOT / "static/js/copal.js").read_text()
    windows = (ROOT / "static/js/copal/windows.js").read_text()
    html = (ROOT / "static/index.html").read_text()
    assert "createCopalWindow" in source
    assert "modal-content copal-modal-content" in windows
    assert "Modals.register(id" in windows
    assert "makeWindowDraggable" in windows
    assert "resizeStorageKey: sizeKey" in windows
    assert "`copal-${view}-modal`" in source
    assert "odysseus-copal-${view}-window-size" in source
    assert "id: 'copal-modal'" not in source
    assert "openCalendar" in source
    assert "function renderCalendar()" in source  # dormant, intentionally retained
    assert "chat-container" not in source
    assert "copal-sidebar-caret" not in html
    assert html.count('id="copal-section-toggle"') == 1


def test_copal_task_identity_labels_are_unambiguous_without_api_migration():
    html = (ROOT / "static/index.html").read_text()
    tasks = (ROOT / "static/js/tasks.js").read_text()
    copal = (ROOT / "static/js/copal.js").read_text()
    assert "Clanker Tasks" in html
    assert "Clanker Tasks" in tasks
    assert "Meatbag Tasks" in html
    assert "Meatbag Tasks" in copal
    assert "/api/tasks" in tasks
    assert "tasks-modal" in tasks


def test_base_query_is_live_scoped_and_has_no_fabricated_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = BaseBridge(tmp_path)
    app.state.copal_bridge = bridge
    http = TestClient(app)

    first = http.get("/api/copal/bases/BASE/query?workspace=personal")
    assert first.status_code == 200
    assert [row["documentId"] for row in first.json()["rows"]] == ["A"]
    assert first.json()["sourceCount"] == 2
    index_call = next(call for call in bridge.calls if call[0] == "index")
    assert index_call[1] == {"owner": "local", "workspace_id": "personal"}

    bridge.docs["B"]["frontmatter"]["status"] = "active"
    second = http.get("/api/copal/bases/BASE/query?workspace=personal")
    assert [row["documentId"] for row in second.json()["rows"]] == ["A", "B"]


def test_base_migration_and_row_edit_use_optimistic_redb_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = BaseBridge(tmp_path)
    app.state.copal_bridge = bridge
    http = TestClient(app)

    dry = http.post("/api/copal/bases/BASE/migrate?workspace=personal&dry_run=true", json={})
    assert dry.status_code == 200
    assert dry.json()["canonical"].startswith('{\n  "version": 1')
    assert not any(call[0] == "write" for call in bridge.calls)

    edited = http.patch(
        "/api/copal/bases/BASE/rows/B?workspace=personal",
        json={"property": "status", "value": "active", "base": "b-head"},
    )
    assert edited.status_code == 200
    write = next(call for call in reversed(bridge.calls) if call[0] == "write")
    assert write[1]["owner"] == "local"
    assert write[1]["workspace_id"] == "personal"
    assert write[1]["base"] == "b-head"
    assert 'status: "active"' in write[1]["content"]

    stale = http.patch(
        "/api/copal/bases/BASE/rows/B?workspace=personal",
        json={"property": "status", "value": "parked", "base": "stale"},
    )
    assert stale.status_code == 409


def test_planning_migration_is_dry_runnable_idempotent_and_blocks_split_truth(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("COPAL_CALENDAR_PROJECTION_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = PlanningBridge(tmp_path)
    app.state.copal_bridge = bridge
    http = TestClient(app)

    assert http.get("/api/copal/planning").json()["migrationRequired"] is True
    dry = http.post("/api/copal/planning/migrate?dry_run=true", json={"action": "apply"})
    assert dry.status_code == 200
    assert dry.json()["report"]["events"] == 1
    assert set(bridge.docs) == {"LEGACY"}

    applied = http.post("/api/copal/planning/migrate?dry_run=false", json={"action": "apply"})
    assert applied.status_code == 200
    canonical = http.get("/api/copal/planning").json()
    assert canonical["canonical"] is True
    assert canonical["tracks"][0]["tasks"][0]["copal_extra"]["futureField"] == {"preserve": True}
    event = canonical["tracks"][0]["tasks"][0]

    repeated = http.post("/api/copal/planning/migrate?dry_run=false", json={"action": "apply"})
    assert repeated.status_code == 200
    assert repeated.json()["changed"] is False
    legacy_write = http.put("/api/copal/documents/LEGACY", json={"content": "{}", "base": "h1"})
    assert legacy_write.status_code == 409
    assert "read-only" in legacy_write.json()["detail"]

    patched = http.patch(
        f"/api/copal/planning/events/{event['id']}",
        json={"base": event["head"], "patch": {"title": "Packed"}},
    )
    assert patched.status_code == 200
    assert patched.json()["event"]["title"] == "Packed"
    unsafe_rollback = http.post("/api/copal/planning/migrate?dry_run=false", json={"action": "rollback"})
    assert unsafe_rollback.status_code == 409
    assert unsafe_rollback.json()["detail"]["conflicts"][0]["id"] == event["id"]


# ── Corpus isolation tests ────────────────────────────────────────────────

class CorpusTrackingBridge(FakeBridge):
    """Tracks create calls by corpus to verify routing."""

    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.create_calls = []

    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, args, timeout))
        if operation == "create":
            self.create_calls.append(args)
            return {"outcome": "created", "doc": {"id": f"DOC-{args.get('corpus', 'notes')}-{args.get('kind', 'note')}"}}
        return await super().call(operation, args, timeout)


def test_wiki_create_routes_to_wiki_corpus(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    bridge2 = CorpusTrackingBridge(tmp_path)
    http.app.state.copal_bridge = bridge2

    result = http.post("/api/copal/documents", json={
        "name": "Test Tiddler",
        "kind": "note",
        "content": "Hello wiki",
        "corpus": "wiki",
    })
    assert result.status_code == 200
    create_args = bridge2.create_calls[-1]
    assert create_args["corpus"] == "wiki"
    assert create_args["kind"] == "wiki"


def test_notes_create_routes_to_notes_corpus(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    bridge2 = CorpusTrackingBridge(tmp_path)
    http.app.state.copal_bridge = bridge2

    result = http.post("/api/copal/documents", json={
        "name": "My Note",
        "kind": "note",
        "content": "Hello notes",
        "corpus": "notes",
    })
    assert result.status_code == 200
    create_args = bridge2.create_calls[-1]
    assert create_args["corpus"] == "notes"
    assert create_args["kind"] == "note"


def test_wiki_list_filters_by_corpus(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)

    http.get("/api/copal/documents?corpus=wiki")
    # The bridge should receive corpus=wiki
    assert bridge.calls[-1][1].get("corpus") == "wiki"

    http.get("/api/copal/documents?corpus=notes")
    assert bridge.calls[-1][1].get("corpus") == "notes"

    http.get("/api/copal/documents?corpus=all")
    assert bridge.calls[-1][1].get("corpus") == "all"


def test_wiki_create_coerces_kind(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)

    # When corpus=wiki and kind=note, kind should be coerced to wiki
    result = http.post("/api/copal/documents", json={
        "name": "Auto Wiki",
        "kind": "note",
        "content": "",
        "corpus": "wiki",
    })
    assert result.status_code == 200
    # Find the create call (may be followed by get calls)
    create_calls = [c for c in bridge.calls if c[0] == "create"]
    assert len(create_calls) == 1
    assert create_calls[0][1]["kind"] == "wiki"


def test_wiki_create_rejects_non_note_kinds(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)

    # Wiki corpus should reject non-note kinds (e.g., markdown)
    result = http.post("/api/copal/documents", json={
        "name": "Bad Wiki",
        "kind": "markdown",
        "content": "",
        "corpus": "wiki",
    })
    assert result.status_code == 422


def test_write_route_passes_corpus_for_wiki_doc(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)

    # FakeBridge.get returns a wiki-kind doc for DOC1
    wiki_bridge = CorpusTrackingBridge(tmp_path)
    wiki_bridge.write_result = {"outcome": "committed", "doc": {"id": "DOC1"}}
    http.app.state.copal_bridge = wiki_bridge

    # Patch the get call to return a wiki doc with copal-note-v1 format
    original_call = wiki_bridge.call
    wiki_text = json.dumps({
        "schemaVersion": 1,
        "body": {"type": "doc", "blocks": [{"id": "blk1", "type": "paragraph", "text": "Hello", "source": "Hello"}]},
        "properties": [],
        "relations": [],
    })

    async def patched_call(operation, args, timeout=20):
        if operation == "get" and args.get("id") == "DOC1":
            wiki_bridge.calls.append((operation, args, timeout))
            return {"id": "DOC1", "kind": "wiki", "name": "Test Wiki", "text": wiki_text, "head": "h1", "corpus": "wiki", "format": "copal-note-v1", "blocks": [{"id": "blk1", "type": "paragraph", "text": "Hello", "source": "Hello"}], "propertyDefinitions": [], "relations": [], "extensions": {}}
        return await original_call(operation, args, timeout)

    wiki_bridge.call = patched_call
    result = http.put("/api/copal/documents/DOC1", json={"content": "updated", "base": "h1"})
    assert result.status_code == 200
    # Find the last write call and check it has corpus=wiki
    write_calls = [c for c in wiki_bridge.calls if c[0] == "write"]
    assert len(write_calls) >= 1
    assert write_calls[-1][1]["corpus"] == "wiki"

import io
import json
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.copal_routes import setup_copal_routes


ROOT = Path(__file__).resolve().parents[1]


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
        if operation == "status":
            return {"schema_version": 2, "documents": 1, "integrity_ok": True, "kinds": {"markdown": 1}}
        if operation == "list":
            return {"docs": [{"id": "DOC1"}]}
        if operation == "index":
            return {"docs": []}
        if operation == "write":
            return self.write_result
        if operation == "import_vault":
            root = Path(args["path"])
            self.imported_files = {
                str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            return {"notes": 1, "assets": 0, "planning": True, "treehouse": True, "op": "IMPORT1"}
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


def test_dead_bridge_restarts_before_copal_operation(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    bridge = RestartingBridge(tmp_path)
    http.app.state.copal_bridge = bridge

    response = http.get("/api/copal/status")

    assert response.status_code == 200
    assert bridge.start_calls == 1
    assert bridge.is_alive()


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


def test_trash_restore_keeps_server_owned_scope(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)

    assert http.get("/api/copal/trash?workspace=personal").status_code == 200
    assert bridge.calls[-1][1] == {"owner": "local", "workspace_id": "personal"}
    assert http.post("/api/copal/trash/DOC1/restore?workspace=personal").status_code == 200
    assert bridge.calls[-1][1] == {"owner": "local", "workspace_id": "personal", "id": "DOC1"}


def test_obsidian_export_is_scoped_and_scrubs_operational_metadata(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    response = http.get("/api/copal/export/obsidian?workspace=personal")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("Notes/Hello.md") == b"# Hello\n"
        manifest = archive.read(".copal/export-manifest.json").decode()
    assert "secret-operational-head" not in manifest
    assert '"workspace": "personal"' in manifest
    export_call = next(call for call in bridge.calls if call[0] == "export_snapshot")
    assert export_call[1] == {"owner": "local", "workspace_id": "personal"}


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
    assert call[1]["planning_path"].endswith("/.copal/planning.json")
    assert bridge.imported_files["Welcome.md"] == b"# Welcome\n"
    assert not Path(call[1]["path"]).exists()


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

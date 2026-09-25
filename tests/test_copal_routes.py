import asyncio
import io
import json
import stat
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.copal_routes as copal_routes
from src.openclank.copal_bases import parse_base_definition
from src.openclank.copal_planning import serialize_event
from src.openclank.copal_transfer import create_export
from src.openclank.file_policy import FilePolicyRepository

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


class _AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.anchors = []
        self._current = None
        self._stack = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self._stack.append((tag, attributes))
        if tag == "a":
            self._current = {"attrs": attributes, "text": [], "ancestors": list(self._stack[:-1])}
            self.anchors.append(self._current)

    def handle_data(self, data):
        if self._current is not None:
            self._current["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "a":
            self._current = None
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break


def _anchors(html):
    parser = _AnchorParser()
    parser.feed(html)
    return parser.anchors


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


def test_historical_wiki_markdown_remains_readable_and_source_preserved():
    document = _note_view({
        "id": "WIKI1", "kind": "wiki", "corpus": "wiki", "name": ".memes/Legacy",
        "head": "legacy-h1", "text": "# Heading\n\nUnicode — readable\n",
    })
    assert document["text"] == "# Heading\n\nUnicode — readable\n"
    assert document["recoveryState"] == "legacy-import"
    assert document["formatNotice"] == "legacy-markdown"
    assert document["readOnly"] is True
    assert document.get("note_error") is None


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
        if operation == "trash":
            docs = [
                {"id": "DOC1", "kind": "note", "name": "Deleted.md", "text": "", "head": "deleted-h1"}
            ] if args.get("corpus") == "notes" else []
            return {"docs": docs}
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


class TaskProjectionFailureBridge(FakeBridge):
    supports_task_index_lookup = True
    supports_keyed_task_index = True

    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.docs = {
            "DOC1": {
                "id": "DOC1", "kind": "markdown", "name": "Notes.md",
                "head": "h0", "text": "old", "frontmatter": {}, "tags": [], "links": [],
            }
        }

    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, args, timeout))
        if operation == "get":
            return dict(self.docs[args["id"]])
        if operation == "write":
            document = self.docs[args["id"]]
            if args.get("base") and args["base"] != document["head"]:
                return {"outcome": "stale", "doc": dict(document)}
            document["text"] = args["content"]
            document["head"] = f"h{int(document['head'][1:]) + 1}"
            return {"outcome": "committed", "doc": dict(document)}
        if operation == "task_index_generation":
            raise HTTPException(502, "task tables unavailable")
        if operation == "index":
            return {"docs": list(self.docs.values())}
        return await super().call(operation, args, timeout)


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


class CanonicalPlanningBridge(PlanningBridge):
    def __init__(self, data_dir, *, schema_version=1):
        super().__init__(data_dir)
        tracks = [
            {"id": "home", "name": "Home", "color": "#14b8a6", "icon": "home", "enabled": True},
            {"id": "car", "name": "Car", "color": "#0ea5e9", "icon": "car", "enabled": True},
        ]
        self.docs = {
            "TRACKS": {
                "id": "TRACKS",
                "kind": "copal-tracks",
                "name": ".copal/tracks.json",
                "head": "tracks-h1",
                "text": json.dumps({
                    "schemaVersion": schema_version,
                    "title": "Canonical",
                    "vendor": {"preserve": True},
                    "tracks": tracks,
                }),
                "frontmatter": {},
                "tags": [],
                "links": [],
            },
            "EVENT": {
                "id": "EVENT",
                "kind": "copal-event",
                "name": ".events/event.md",
                "head": "event-h1",
                "text": serialize_event({
                    "title": "Event",
                    "startDate": "2026-08-03",
                    "trackId": "home",
                }, tracks=tracks),
                "frontmatter": {},
                "tags": [],
                "links": [],
            },
            "LEGACY": {
                "id": "LEGACY",
                "kind": "planning",
                "name": "planning.json",
                "head": "legacy-h1",
                "text": json.dumps({"schemaVersion": 1, "tracks": [], "tasks": []}),
                "frontmatter": {},
                "tags": [],
                "links": [],
            },
        }
        self.trashed = {
            "TRASHED": {
                **self.docs["EVENT"],
                "id": "TRASHED",
                "head": "trashed-h1",
            }
        }

    async def call(self, operation, args, timeout=20):
        if operation == "trash":
            self.calls.append((operation, args, timeout))
            docs = list(self.trashed.values()) if args.get("corpus") == "notes" else []
            return {"docs": [self._index_doc(doc) for doc in docs]}
        return await super().call(operation, args, timeout)


class AsyncASGIRouteClient:
    """Small test facade over httpx's non-threaded ASGI transport."""

    def __init__(self, app):
        self.app = app

    async def request(self, method, path, **kwargs):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    async def get(self, path, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def post(self, path, **kwargs):
        return await self.request("POST", path, **kwargs)

    async def put(self, path, **kwargs):
        return await self.request("PUT", path, **kwargs)

    async def patch(self, path, **kwargs):
        return await self.request("PATCH", path, **kwargs)

    async def delete(self, path, **kwargs):
        return await self.request("DELETE", path, **kwargs)


def canonical_planning_client(tmp_path, monkeypatch, *, schema_version=1):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("COPAL_CALENDAR_PROJECTION_ENABLED", "false")

    async def no_projection(*_args, **_kwargs):
        return {"enabled": False}

    monkeypatch.setattr(copal_routes, "_project_canonical_workspace", no_projection)
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = CanonicalPlanningBridge(tmp_path, schema_version=schema_version)
    app.state.copal_bridge = bridge
    return AsyncASGIRouteClient(app), bridge


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


def test_committed_write_returns_task_projection_failure_receipt_and_head_for_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = TaskProjectionFailureBridge(tmp_path)
    app.state.copal_bridge = bridge
    http = TestClient(app)

    first = http.put("/api/copal/documents/DOC1", json={"content": "new", "base": "h0", "actionId": "g02-write-1"})
    assert first.status_code == 200
    body = first.json()
    assert body["outcome"] == "committed"
    assert body["actionId"] == "g02-write-1"
    assert body["doc"]["head"] == "h1"
    assert body["projections"]["tasks"] == {"status": "failed", "retryable": True, "code": "task_projection_failed"}

    second = http.put("/api/copal/documents/DOC1", json={"content": "newer", "base": "h1"})
    assert second.status_code == 200
    assert second.json()["doc"]["head"] == "h2"


def test_wiki_copy_create_receipt_recovers_response_loss_and_scopes_action(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    class ScopedBridge(FakeBridge):
        def __init__(self, root):
            super().__init__(root)
            self.docs = []
            self.counter = 0
            self.fail_receipt_once = True

        async def call(self, operation, args, timeout=20):
            self.calls.append((operation, args, timeout))
            workspace = args.get("workspace_id", "default")
            if operation == "index":
                docs = [doc for doc in self.docs if doc["workspace_id"] == workspace]
                if args.get("kind"):
                    docs = [doc for doc in docs if doc["kind"] == args["kind"]]
                return {"docs": [dict(doc) for doc in docs]}
            if operation == "get":
                return dict(next(doc for doc in self.docs if doc["id"] == args["id"] and doc["workspace_id"] == workspace))
            if operation == "create":
                if args["kind"] == "copal-operation" and self.fail_receipt_once:
                    self.fail_receipt_once = False
                    raise copal_routes.CopalBridgeError("response-loss after document commit")
                self.counter += 1
                doc = {
                    "id": f"DOC{self.counter}", "kind": args["kind"], "corpus": args.get("corpus", "notes"),
                    "name": args["name"], "head": f"head-{self.counter}", "text": args.get("content", ""),
                    "owner": "local", "workspace_id": workspace, "builtin": False,
                }
                self.docs.append(doc)
                return {"outcome": "created", "doc": dict(doc)}
            return await super().call(operation, args, timeout)

    bridge = ScopedBridge(tmp_path)
    app = FastAPI()
    app.include_router(setup_copal_routes())
    app.state.copal_bridge = bridge
    no_raise = TestClient(app, raise_server_exceptions=False)
    http = TestClient(app)
    payload = {
        "actionId": "wiki-copy-replay",
        "name": ".memes/Editable copy",
        "kind": "wiki",
        "corpus": "wiki",
        "content": "# Editable\n",
        "properties": {"topic": "story"},
        "relations": [{"kind": "link", "target": ".memes/Target"}],
    }

    lost = no_raise.post("/api/copal/documents?workspace=home", json=payload)
    assert lost.status_code == 400, lost.text
    retried = http.post("/api/copal/documents?workspace=home", json=payload)
    assert retried.status_code == 200
    assert retried.json()["replayed"] is True
    assert retried.json()["doc"]["properties"] == payload["properties"]
    assert [(relation["kind"], relation["target"]) for relation in retried.json()["doc"]["relations"]] == [("link", ".memes/Target")]
    assert len([doc for doc in bridge.docs if doc["kind"] == "wiki" and doc["workspace_id"] == "home"]) == 1

    replayed_again = http.post("/api/copal/documents?workspace=home", json=payload)
    assert replayed_again.status_code == 200
    assert replayed_again.json()["replayed"] is True
    assert replayed_again.json()["doc"]["id"] == retried.json()["doc"]["id"]
    assert len([doc for doc in bridge.docs if doc["kind"] == "wiki" and doc["workspace_id"] == "home"]) == 1

    foreign_scope = http.post("/api/copal/documents?workspace=other", json=payload)
    assert foreign_scope.status_code == 200
    assert foreign_scope.json()["replayed"] is False
    assert foreign_scope.json()["doc"]["id"] != retried.json()["doc"]["id"]

    conflict = http.post("/api/copal/documents?workspace=home", json={**payload, "content": "# Changed\n"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["outcome"] == "idempotency_conflict"


def test_legacy_attachment_reconciles_crash_window_and_replay_checks_asset(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    class Bridge(FakeBridge):
        def __init__(self, root):
            super().__init__(root)
            self.assets = {}
            self.document = {"id": "DOC", "kind": "markdown", "name": "Doc.md", "text": "hello", "head": "head-1"}

        async def call(self, operation, args, timeout=20):
            if operation == "get" and args.get("id") == "DOC":
                return dict(self.document)
            if operation == "get" and args.get("id", "").startswith("asset"):
                if args["id"] not in self.assets:
                    raise copal_routes.CopalBridgeError("asset missing")
                return dict(self.assets[args["id"]])
            if operation == "find_by_name":
                return next((dict(asset) for asset in self.assets.values() if asset["name"] == args["name"]), None)
            if operation == "put_asset_scoped":
                raw = __import__("base64").b64decode(args["base64"])
                digest = __import__("hashlib").sha256(raw).hexdigest()
                asset = {"id": "asset-1", "kind": "asset", "name": args["name"], "size": len(raw), "head": f"sha256:{digest}:{len(raw)}"}
                self.assets[asset["id"]] = asset
                return {"doc": asset}
            if operation == "commit_guarded":
                self.document = {**self.document, "text": args["operations"][0]["content"], "head": "head-2"}
                return {"outcome": "applied", "receipt_id": "receipt-1"}
            return await super().call(operation, args, timeout)

    repository = FilePolicyRepository(tmp_path / "policy.db")
    bridge = Bridge(tmp_path)
    app = FastAPI()
    app.include_router(setup_copal_routes(policy_repository=repository))
    app.state.copal_bridge = bridge
    http = TestClient(app)
    payload = {"actionId": "attach-crash", "documentId": "DOC", "name": "attachments/source.txt", "mime": "text/plain", "contentBase64": __import__("base64").b64encode(b"abc").decode(), "content": "hello\n![[attachments/source.txt]]", "base": "head-1", "prepareOnly": True, "sourceTextHash": __import__("hashlib").sha256(b"hello").hexdigest()}
    prepared = http.post("/api/copal/attachments", json=payload)
    assert prepared.status_code == 200
    assert prepared.json()["preparation"]["phase"] == "staged"
    original_record = repository.record_operation
    fail_consumed = True

    def fail_marker_once(**kwargs):
        nonlocal fail_consumed
        if fail_consumed and kwargs.get("phase") == "consumed":
            fail_consumed = False
            raise OSError("simulated marker crash")
        return original_record(**kwargs)

    monkeypatch.setattr(repository, "record_operation", fail_marker_once)
    no_raise = TestClient(app, raise_server_exceptions=False)
    commit = no_raise.post("/api/copal/attachments/commit", json={"actionId": "attach-crash", "documentId": "DOC", "content": payload["content"], "base": "head-1", "sourceTextHash": payload["sourceTextHash"], "assetId": "asset-1", "assetName": "attachments/source.txt"})
    assert commit.status_code == 500
    replay = http.post("/api/copal/attachments/commit", json={"actionId": "attach-crash", "documentId": "DOC", "content": payload["content"], "base": "head-1", "sourceTextHash": payload["sourceTextHash"], "assetId": "asset-1", "assetName": "attachments/source.txt"})
    assert replay.status_code == 200
    bridge.assets.clear()
    missing = http.post("/api/copal/attachments/commit", json={"actionId": "attach-crash", "documentId": "DOC", "content": payload["content"], "base": "head-1", "sourceTextHash": payload["sourceTextHash"], "assetId": "asset-1", "assetName": "attachments/source.txt"})
    assert missing.status_code == 409


def test_attachment_reaper_handles_aged_pending_and_staged_without_touching_consumed(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    class Bridge(FakeBridge):
        def __init__(self, root):
            super().__init__(root)
            self.assets = {}
            self.trashed = []

        async def call(self, operation, args, timeout=20):
            if operation == "get" and args.get("id") in self.assets:
                return dict(self.assets[args["id"]])
            if operation == "find_by_name":
                return next((dict(asset) for asset in self.assets.values() if asset["name"] == args["name"]), None)
            if operation == "delete":
                asset_id = args.get("id")
                self.trashed.append(asset_id)
                self.assets.pop(asset_id, None)
                return {"outcome": "committed"}
            return await super().call(operation, args, timeout)

    repository = FilePolicyRepository(tmp_path / "policy.db")
    bridge = Bridge(tmp_path)
    now_ms = int(__import__("time").time() * 1000) - 2 * 60 * 60 * 1000

    def marker(action, phase, asset_id, digest, *, intended=None):
        value = {"action_id": action, "phase": phase, "owner": "local", "workspace_id": "default", "document_id": "DOC", "base": "head-1", "source_text_hash": "source-hash", "asset_name": f"attachments/{action}.bin", "asset_size": 3, "asset_digest": digest, "mime": "application/octet-stream", "created_unix_ms": now_ms, "generation": 1, "asset_id": asset_id}
        if intended: value["intended_content_hash"] = intended
        return value

    import hashlib
    digest = hashlib.sha256(b"abc").hexdigest()
    for action, phase in [("aged-pending", "pending"), ("aged-staged", "staged")]:
        asset_id = f"asset-{action}"
        value = marker(action, phase, asset_id, digest)
        bridge.assets[asset_id] = {"id": asset_id, "kind": "asset", "name": value["asset_name"], "size": 3, "head": f"sha256:{digest}:3"}
        immutable = {key: value.get(key) for key in ("action_id", "owner", "workspace_id", "generation", "document_id", "base", "source_text_hash", "source_ref", "source_item_id", "source_revision", "target_ref", "target_revision", "asset_name", "asset_size", "asset_digest", "intended_content_hash", "mime")}
        request_digest = hashlib.sha256(json.dumps(immutable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        repository.record_operation(owner_subject_id="local-installation", operation_id=f"__copal_attachment_lifecycle__{action}", request_digest=request_digest, generation=1, receipt=value, phase=phase)
    consumed = marker("consumed", "consumed", "asset-consumed", digest)
    bridge.assets["asset-consumed"] = {"id": "asset-consumed", "kind": "asset", "name": consumed["asset_name"], "size": 3, "head": f"sha256:{digest}:3"}
    immutable = {key: consumed.get(key) for key in ("action_id", "owner", "workspace_id", "generation", "document_id", "base", "source_text_hash", "source_ref", "source_item_id", "source_revision", "target_ref", "target_revision", "asset_name", "asset_size", "asset_digest", "intended_content_hash", "mime")}
    repository.record_operation(owner_subject_id="local-installation", operation_id="__copal_attachment_lifecycle__consumed", request_digest=hashlib.sha256(json.dumps(immutable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(), generation=1, receipt=consumed, phase="consumed")
    for index in range(2050):
        terminal = marker(f"terminal-{index}", "reaped", "", digest)
        immutable = {key: terminal.get(key) for key in ("action_id", "owner", "workspace_id", "generation", "document_id", "base", "source_text_hash", "source_ref", "source_item_id", "source_revision", "target_ref", "target_revision", "asset_name", "asset_size", "asset_digest", "intended_content_hash", "mime")}
        repository.record_operation(owner_subject_id="local-installation", operation_id=f"__copal_attachment_lifecycle__terminal-{index}", request_digest=hashlib.sha256(json.dumps(immutable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(), generation=1, receipt=terminal, phase="reaped")
    malformed = marker("malformed-age", "pending", "asset-malformed-age", digest)
    malformed["created_unix_ms"] = "not-a-timestamp"
    foreign = marker("foreign-owner", "pending", "asset-foreign-owner", digest)
    foreign["owner"] = "user:rhythm"
    for value in (malformed, foreign):
        asset_id = value["asset_id"]
        bridge.assets[asset_id] = {"id": asset_id, "kind": "asset", "name": value["asset_name"], "size": 3, "head": f"sha256:{digest}:3"}
        immutable = {key: value.get(key) for key in ("action_id", "owner", "workspace_id", "generation", "document_id", "base", "source_text_hash", "source_ref", "source_item_id", "source_revision", "target_ref", "target_revision", "asset_name", "asset_size", "asset_digest", "intended_content_hash", "mime")}
        repository.record_operation(owner_subject_id="local-installation", operation_id=f"__copal_attachment_lifecycle__{value['action_id']}", request_digest=hashlib.sha256(json.dumps(immutable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(), generation=1, receipt=value, phase="pending")

    # Exercise the compatibility repository shape: it has no phase filter, and
    # its first pages are dominated by terminal rows. Reaping must advance
    # through bounded pages and restart after each mutation.
    phase_lister = repository.list_operations

    def compatibility_lister(*, owner_subject_id, operation_prefix="", offset=0, limit=256):
        all_rows = []
        page_offset = 0
        while True:
            page = phase_lister(owner_subject_id=owner_subject_id, operation_prefix=operation_prefix, offset=page_offset, limit=256)
            all_rows.extend(page)
            if len(page) < 256:
                break
            page_offset += len(page)
        all_rows.sort(key=lambda row: 0 if (row.get("receipt") or {}).get("phase") == "reaped" else 1)
        return all_rows[offset:offset + min(int(limit), 256)]

    monkeypatch.setattr(repository, "list_operations", compatibility_lister)
    app = FastAPI()
    app.include_router(setup_copal_routes(policy_repository=repository))
    app.state.copal_bridge = bridge
    response = TestClient(app, raise_server_exceptions=False).get("/api/copal/attachments/missing/status")
    assert response.status_code == 404
    assert set(bridge.trashed) == {"asset-aged-pending", "asset-aged-staged"}, (bridge.calls, repository.list_operations(owner_subject_id="local-installation", operation_prefix="__copal_attachment_lifecycle__", phase="pending"), repository.list_operations(owner_subject_id="local-installation", operation_prefix="__copal_attachment_lifecycle__", phase="staged"))
    assert "asset-consumed" in bridge.assets
    assert repository.get_operation(owner_subject_id="local-installation", operation_id="__copal_attachment_lifecycle__aged-pending")["receipt"]["phase"] == "reaped"
    assert repository.get_operation(owner_subject_id="local-installation", operation_id="__copal_attachment_lifecycle__aged-staged")["receipt"]["phase"] == "reaped"
    assert repository.get_operation(owner_subject_id="local-installation", operation_id="__copal_attachment_lifecycle__consumed")["receipt"]["phase"] == "consumed"
    assert bridge.assets["asset-malformed-age"]["name"] == "attachments/malformed-age.bin"
    assert bridge.assets["asset-foreign-owner"]["name"] == "attachments/foreign-owner.bin"
    assert repository.get_operation(owner_subject_id="local-installation", operation_id="__copal_attachment_lifecycle__malformed-age")["receipt"]["phase"] == "pending"
    assert repository.get_operation(owner_subject_id="local-installation", operation_id="__copal_attachment_lifecycle__foreign-owner")["receipt"]["phase"] == "pending"


def test_legacy_one_shot_marks_consumed_and_reaper_preserves_asset(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    class Bridge(FakeBridge):
        def __init__(self, root):
            super().__init__(root)
            self.document = {"id": "DOC", "kind": "markdown", "name": "Doc.md", "text": "hello", "head": "head-1"}
            self.assets = {}
            self.trashed = []

        async def call(self, operation, args, timeout=20):
            if operation == "get" and args.get("id") == "DOC":
                return dict(self.document)
            if operation == "get" and args.get("id") in self.assets:
                return dict(self.assets[args["id"]])
            if operation == "put_asset_scoped":
                raw = __import__("base64").b64decode(args["base64"])
                digest = __import__("hashlib").sha256(raw).hexdigest()
                asset = {"id": "asset-one-shot", "kind": "asset", "name": args["name"], "size": len(raw), "head": f"sha256:{digest}:{len(raw)}"}
                self.assets[asset["id"]] = asset
                return {"doc": asset}
            if operation == "commit_guarded":
                self.document = {**self.document, "text": args["operations"][0]["content"], "head": "head-2"}
                return {"outcome": "applied", "receipt_id": "receipt-one-shot"}
            if operation == "delete":
                asset_id = args.get("id")
                self.trashed.append(asset_id)
                self.assets.pop(asset_id, None)
                return {"outcome": "committed"}
            if operation == "find_by_name":
                return next((dict(asset) for asset in self.assets.values() if asset["name"] == args["name"]), None)
            return await super().call(operation, args, timeout)

    import hashlib
    repository = FilePolicyRepository(tmp_path / "policy.db")
    bridge = Bridge(tmp_path)
    app = FastAPI()
    app.include_router(setup_copal_routes(policy_repository=repository))
    app.state.copal_bridge = bridge
    http = TestClient(app)
    content = "hello\n![[attachments/one-shot.txt]]"
    payload = {
        "actionId": "one-shot", "documentId": "DOC", "name": "attachments/one-shot.txt",
        "mime": "text/plain", "contentBase64": __import__("base64").b64encode(b"abc").decode(),
        "content": content, "base": "head-1", "sourceTextHash": hashlib.sha256(b"hello").hexdigest(),
        "prepareOnly": False,
    }
    response = http.post("/api/copal/attachments", json=payload)
    assert response.status_code == 200
    marker = repository.get_operation(owner_subject_id="local-installation", operation_id="__copal_attachment_lifecycle__one-shot")
    assert marker["receipt"]["phase"] == "consumed"
    app.state.copal_attachment_last_reap = 0
    assert http.get("/api/copal/attachments/missing/status").status_code == 404
    assert bridge.assets["asset-one-shot"]["name"] == "attachments/one-shot.txt"
    assert bridge.trashed == []


def test_legacy_conflict_leaves_staged_asset_for_bounded_reaper(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    class Bridge(FakeBridge):
        def __init__(self, root):
            super().__init__(root)
            self.document = {"id": "DOC", "kind": "markdown", "name": "Doc.md", "text": "hello", "head": "head-2"}
            self.assets = {}
            self.trashed = []

        async def call(self, operation, args, timeout=20):
            if operation == "get" and args.get("id") == "DOC":
                return dict(self.document)
            if operation == "get" and args.get("id") in self.assets:
                return dict(self.assets[args["id"]])
            if operation == "put_asset_scoped":
                raw = __import__("base64").b64decode(args["base64"])
                digest = __import__("hashlib").sha256(raw).hexdigest()
                asset = {"id": "asset-conflict", "kind": "asset", "name": args["name"], "size": len(raw), "head": f"sha256:{digest}:{len(raw)}"}
                self.assets[asset["id"]] = asset
                return {"doc": asset}
            if operation == "commit_guarded":
                return {"outcome": "conflict", "message": "head changed"}
            if operation == "delete":
                asset_id = args.get("id")
                self.trashed.append(asset_id)
                self.assets.pop(asset_id, None)
                return {"outcome": "committed"}
            return await super().call(operation, args, timeout)

    import hashlib
    repository = FilePolicyRepository(tmp_path / "policy.db")
    bridge = Bridge(tmp_path)
    app = FastAPI()
    app.include_router(setup_copal_routes(policy_repository=repository))
    app.state.copal_bridge = bridge
    http = TestClient(app, raise_server_exceptions=False)
    content = "hello\n![[attachments/conflict.txt]]"
    payload = {
        "actionId": "conflict", "documentId": "DOC", "name": "attachments/conflict.txt",
        "mime": "text/plain", "contentBase64": __import__("base64").b64encode(b"abc").decode(),
        "content": content, "base": "head-1", "sourceTextHash": hashlib.sha256(b"hello").hexdigest(),
        "prepareOnly": False,
    }
    response = http.post("/api/copal/attachments", json=payload)
    assert response.status_code == 409
    operation_id = "__copal_attachment_lifecycle__conflict"
    stored = repository.get_operation(owner_subject_id="local-installation", operation_id=operation_id)
    assert stored["receipt"]["phase"] == "staged"
    aged = {**stored["receipt"], "created_unix_ms": int(__import__("time").time() * 1000) - 2 * 60 * 60 * 1000}
    repository.record_operation(owner_subject_id="local-installation", operation_id=operation_id, request_digest=stored["digest"], generation=stored["generation"], receipt=aged, phase="staged")
    app.state.copal_attachment_last_reap = 0
    assert http.get("/api/copal/attachments/missing/status").status_code == 404
    assert bridge.trashed == ["asset-conflict"]


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
    assert "api('/documents?hidden=include', {}, scope.workspace)" in (ROOT / "static/js/copal.js").read_text()


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


def test_task_index_is_scoped_paged_and_writes_markdown_once(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = NoteBridge(tmp_path)
    app.state.copal_bridge = bridge
    http = TestClient(app)
    created = http.post(
        "/api/copal/documents",
        json={"name": "Tasks", "content": "- [ ] duplicate\n* [x] duplicate\n+ [ ] plus"},
    ).json()["doc"]

    page = http.get("/api/copal/tasks?pageSize=2")
    assert page.status_code == 200
    payload = page.json()
    assert len(payload["items"]) == 2
    assert payload["nextCursor"]
    assert len({item["id"] for item in payload["items"]}) == 2
    assert all(item["resourceKey"] for item in payload["items"])
    assert page.json()["snapshotRevision"] == page.json()["queryRevision"]
    next_page = http.get(f"/api/copal/tasks?cursor={payload['nextCursor']}&pageSize=2")
    assert next_page.status_code == 200
    assert len(next_page.json()["items"]) == 1

    target = payload["items"][0]
    writes_before = len([call for call in bridge.calls if call[0] == "write" and call[1].get("id") == created["id"]])
    mutation_body = {
        "actionId": "toggle-1", "taskId": target["id"], "resourceKey": target["resourceKey"],
        "expectedRevision": {"kind": "copalHead", "value": target["sourceRevision"]},
        "anchor": target["anchor"], "checked": True,
    }
    mutation = http.patch(
        f"/api/copal/tasks/{target['id']}",
        json=mutation_body,
    )
    assert mutation.status_code == 200
    assert len([call for call in bridge.calls if call[0] == "write" and call[1].get("id") == created["id"]]) == writes_before + 1
    replay = http.patch(f"/api/copal/tasks/{target['id']}", json=mutation_body)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert len([call for call in bridge.calls if call[0] == "write" and call[1].get("id") == created["id"]]) == writes_before + 1
    changed_action = http.patch(f"/api/copal/tasks/{target['id']}", json={**mutation_body, "checked": False})
    assert changed_action.status_code == 409
    assert changed_action.json()["detail"]["outcome"] == "idempotency_conflict"
    restarted = FastAPI()
    restarted.include_router(setup_copal_routes())
    restarted.state.copal_bridge = bridge
    restarted_http = TestClient(restarted)
    persisted_replay = restarted_http.patch(f"/api/copal/tasks/{target['id']}", json=mutation_body)
    assert persisted_replay.status_code == 200
    assert persisted_replay.json()["replayed"] is True
    assert len([call for call in bridge.calls if call[0] == "write" and call[1].get("id") == created["id"]]) == writes_before + 1
    fresh = http.get(f"/api/copal/documents/{created['id']}").json()
    assert "* [x] duplicate" in fresh["text"] or "- [x] duplicate" in fresh["text"]
    assert {line.split(" ", 1)[0] for line in fresh["text"].splitlines() if "[" in line} == {"-", "*", "+"}

    old_cursor = payload["nextCursor"]
    created_again = http.post("/api/copal/documents", json={"name": "Inserted", "content": "- [ ] inserted"})
    assert created_again.status_code == 200
    stale = http.get(f"/api/copal/tasks?cursor={old_cursor}&pageSize=2")
    assert stale.status_code == 409
    assert stale.json()["detail"]["outcome"] == "stale_cursor"


def test_task_index_rejects_deleted_readonly_and_dirty_revision_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = NoteBridge(tmp_path)
    app.state.copal_bridge = bridge
    http = TestClient(app)
    created = http.post("/api/copal/documents", json={"name": "Target", "content": "- [ ] change"}).json()["doc"]
    item = http.get("/api/copal/tasks").json()["items"][0]
    body = {"actionId": "dirty", "taskId": item["id"], "resourceKey": item["resourceKey"], "expectedRevision": item["sourceRevision"], "anchor": item["anchor"], "checked": True}
    bridge.docs[created["id"]]["head"] += "external"
    dirty = http.patch(f"/api/copal/tasks/{item['id']}", json=body)
    assert dirty.status_code == 409
    bridge.docs[created["id"]]["head"] = item["sourceRevision"]
    bridge.docs[created["id"]]["readOnly"] = True
    readonly = http.patch(f"/api/copal/tasks/{item['id']}", json={**body, "actionId": "readonly"})
    assert readonly.status_code == 403
    bridge.docs.pop(created["id"])
    deleted = http.patch(f"/api/copal/tasks/{item['id']}", json={**body, "actionId": "deleted"})
    assert deleted.status_code == 404


def test_task_create_in_selected_note_and_resource_owner_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = NoteBridge(tmp_path)
    app.state.copal_bridge = bridge
    actor = {"value": "alice"}
    monkeypatch.setattr(copal_routes, "require_user", lambda _request: actor["value"])
    app.state.auth_manager = SimpleNamespace(account_id=lambda username: f"account-{username}")
    http = TestClient(app)
    created = http.post("/api/copal/documents", json={"name": "Selected", "content": "Intro"}).json()["doc"]
    item = http.get("/api/copal/tasks").json()["items"]
    assert item == []
    selected = http.get("/api/copal/documents").json()["docs"]
    resource_key = next(document["resource"]["key"] for document in selected if document["id"] == created["id"])
    inserted = http.post(
        "/api/copal/tasks/create",
        json={"actionId": "create-1", "resourceKey": resource_key, "expectedRevision": created["head"], "text": "created in place"},
    )
    assert inserted.status_code == 200
    assert inserted.json()["task"]["text"] == "created in place"
    actor["value"] = "bob"
    denied = http.patch(
        f"/api/copal/tasks/{inserted.json()['task']['id']}",
        json={
            "actionId": "bob-toggle", "taskId": inserted.json()["task"]["id"], "resourceKey": resource_key,
            "expectedRevision": inserted.json()["revision"], "anchor": inserted.json()["task"]["anchor"], "checked": True,
        },
    )
    assert denied.status_code == 404


def test_persisted_task_index_pages_over_500_and_rejects_stale_restart_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = NoteBridge(tmp_path)
    bridge.docs = {
        f"N{index}": {
            "id": f"N{index}", "kind": "note", "name": f"Large/{index}.md", "head": f"head-{index}",
            "text": _encode_note(f"- [ ] Task {index}"), "frontmatter": {}, "tags": [], "links": [],
        }
        for index in range(601)
    }
    app.state.copal_bridge = bridge
    http = TestClient(app)
    first = http.get("/api/copal/tasks?pageSize=100")
    assert first.status_code == 200
    assert first.json()["total"] == 601
    assert len(first.json()["items"]) == 100
    cursor = first.json()["nextCursor"]
    restarted = FastAPI()
    restarted.include_router(setup_copal_routes())
    restarted.state.copal_bridge = bridge
    restarted_http = TestClient(restarted)
    page = restarted_http.get(f"/api/copal/tasks?pageSize=100&cursor={cursor}")
    assert page.status_code == 200
    assert len(page.json()["items"]) == 100
    bridge.docs["N0"]["head"] = "head-external"
    stale = restarted_http.get(f"/api/copal/tasks?pageSize=100&cursor={cursor}")
    assert stale.status_code == 409
    assert stale.json()["detail"]["outcome"] == "stale_cursor"


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

    # A source property transaction keeps the native envelope's unrelated
    # extension and supports explicit Clear separately from a real null.
    native_record = json.loads(bridge.docs[created["id"]]["text"])
    native_record["extensions"] = {"pluginState": {"keep": True}}
    bridge.docs[created["id"]]["text"] = json.dumps(native_record, ensure_ascii=False, separators=(",", ":"))
    cleared = http.patch(
        f"/api/copal/bases/BASE/rows/{created['id']}",
        json={"property": "status", "clear": True, "base": bridge.docs[created["id"]]["head"], "actionId": "row-clear-1"},
    )
    assert cleared.status_code == 200
    fresh = http.get(f"/api/copal/documents/{created['id']}").json()
    assert "status" not in fresh["properties"]
    assert fresh["extensions"]["pluginState"] == {"keep": True}

    null_value = http.patch(
        f"/api/copal/bases/BASE/rows/{created['id']}",
        json={"property": "status", "value": None, "base": bridge.docs[created["id"]]["head"]},
    )
    assert null_value.status_code == 200
    assert http.get(f"/api/copal/documents/{created['id']}").json()["properties"]["status"] is None

    exported = http.get("/api/copal/export/obsidian")
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        markdown = archive.read("Native.md").decode()
    assert markdown.startswith('---\nstatus: null\n---\n\nBody')


def test_base_transform_route_previews_then_applies_source_command_with_cas(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = BaseBridge(tmp_path)
    app.state.copal_bridge = bridge
    http = TestClient(app)
    source = bridge.docs["BASE"]["text"]

    preview = http.post(
        "/api/copal/bases/BASE/transform",
        json={"command": {"type": "set-sort", "view_id": "table", "property": "status", "direction": "desc"}, "base": "base-head"},
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["applied"] is False and body["revision"] == "base-head"
    assert body["source"] != source
    assert body["definition"]["views"][0]["sorts"] == [
        {"property": "file.name", "direction": "asc"},
        {"property": "status", "direction": "desc"},
    ]
    assert [call[0] for call in bridge.calls].count("write") == 0
    unverified_apply = http.post(
        "/api/copal/bases/BASE/transform",
        json={"source": body["source"], "command": {"action": "set-limit", "view_id": "table", "limit": 20}, "base": "base-head", "apply": True},
    )
    assert unverified_apply.status_code == 400

    applied = http.post(
        "/api/copal/bases/BASE/command",
        json={"actionId": "base-sort-1", "command": {"action": "set-sort", "view_id": "table", "property": "status", "direction": "desc"}, "base": "base-head", "apply": True},
    )
    assert applied.status_code == 200
    assert applied.json()["applied"] is True
    assert "direction: desc" in bridge.docs["BASE"]["text"]
    assert parse_base_definition(bridge.docs["BASE"]["text"])[0]["views"][0]["sorts"] == [
        {"property": "file.name", "direction": "asc"},
        {"property": "status", "direction": "desc"},
    ]

    bridge.docs["A"]["text"] = "---\r\nstatus: active # keep\ntags: [one]\r\n---\r\nDirty prose\r\n"
    markdown_patch = http.patch(
        "/api/copal/bases/BASE/rows/A",
        json={"property": "status", "value": "ready", "base": bridge.docs["A"]["head"], "actionId": "row-status-1"},
    )
    assert markdown_patch.status_code == 200
    assert bridge.docs["A"]["text"] == "---\r\nstatus: \"ready\" # keep\ntags: [one]\r\n---\r\nDirty prose\r\n"
    markdown_clear = http.patch(
        "/api/copal/bases/BASE/rows/A",
        json={"property": "status", "clear": True, "base": bridge.docs["A"]["head"]},
    )
    assert markdown_clear.status_code == 200
    assert bridge.docs["A"]["text"] == "---\r\ntags: [one]\r\n---\r\nDirty prose\r\n"

    stale = http.post(
        "/api/copal/bases/BASE/transform",
        json={"command": {"action": "set-limit", "view_id": "table", "limit": 20}, "base": "old-head"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["diagnostics"][0]["code"] == "stale_revision"


def test_base_row_patch_preserves_extensions_from_production_native_projection(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = NoteBridge(tmp_path)
    encoded = _encode_note("Imported body\n", {"status": "active"}, import_source="---\nstatus: active\n---\n\nImported body\n")
    record = json.loads(encoded)
    bridge.docs["NATIVE"] = {
        "id": "NATIVE", "kind": "note", "name": "Imported.md", "head": "native-head",
        # This mirrors the production bridge response.  The route must use
        # these typed fields, rather than depending on reparsing raw JSON.
        "text": encoded, "format": "copal-note-v1", "storage": "database",
        "blocks": record["body"]["blocks"], "properties": {"status": "active"},
        "propertyDefinitions": record["properties"], "relations": record["relations"],
        "extensions": record["extensions"],
    }
    app.state.copal_bridge = bridge
    http = TestClient(app)
    patched = http.patch(
        "/api/copal/bases/BASE/rows/NATIVE",
        json={"property": "note.status", "value": "ready", "base": "native-head", "actionId": "native-property-1"},
    )
    assert patched.status_code == 200
    persisted = json.loads(bridge.docs["NATIVE"]["text"])
    assert {item["key"]: item["value"] for item in persisted["properties"]}["status"] == "ready"
    assert persisted["extensions"]["interchange"]["source"].startswith("---\nstatus: active")
    assert persisted["extensions"]["interchange"]["modified"] is True


def test_sheet_command_and_property_adapter_use_source_transform_and_shared_queue():
    source = (ROOT / "static/js/copal.js").read_text()
    command = source[source.index("async function transformBaseCommand"):source.index("async function applyBaseCells")]
    helper = source[source.index("function applyBaseCellsToEnvelope"):source.index("function pasteFailureKind")]
    retry = source[source.index("async function retryBaseCellFailures"):source.index("async function applyBaseCells")]
    cells = source[source.index("async function applyBaseCells"):source.index("function createBaseSheetAdapter")]
    assert "/bases/${encodeURIComponent(currentBase.id)}/transform" in command
    assert "getAuthoritativeSnapshot?.(currentBase.id)" in command
    assert "newest.localRevision !== localRevision" in command
    assert "queueDocumentSave" not in command
    assert "setFrontmatterProperty" in helper
    assert "cell.clear === true" in helper
    assert "applyBaseCellsToEnvelope(source, rowCells, snapshot?.envelope" in cells
    assert "notesFeature?.queueSave" in cells
    assert "applyBaseCellsToEnvelope(source, cells, remoteEnvelope)" in retry
    assert "rebaseDocumentAtRevision" in retry
    adapter = source[source.index("function createBaseSheetAdapter"):source.index("planningFeature = createPlanningFeature")]
    assert "transformBaseCommand(doc, request" in adapter
    assert "serializeBase(next)" not in adapter[adapter.index("command:async"):adapter.index("toolbar:")]


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


def test_native_export_download_rechecks_owner_workspace_and_integrity(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    artifact = asyncio.run(create_export(
        bridge,
        snapshot={"docs": [{"id": "DOC", "kind": "markdown", "name": "Doc.md", "text": "# Doc\n"}]},
        owner="local",
        workspace="personal",
        options={},
    ))
    downloaded = http.get(f"{artifact['downloadUrl']}?workspace=personal")
    assert downloaded.status_code == 200
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        assert archive.read("Doc.md") == b"# Doc\n"

    wrong_workspace = http.get(f"{artifact['downloadUrl']}?workspace=other")
    assert wrong_workspace.status_code == 403


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
    expected = ["notes", "wiki", "timeline", "graph", "treehouse", "todo"]
    positions = [html.index(f'data-copal-view="{view}"') for view in expected]
    assert positions == sorted(positions)
    assert '<iframe' not in html[positions[0]:positions[-1]]
    assert 'data-copal-view="calendar"' not in html
    assert 'data-copal-view="galaxy"' not in html
    # Bases are a document type rendered inside the shared Editor leaf.  The
    # legacy route remains an alias, but the shell must not expose a second
    # launcher that would split the Editor workspace.
    assert 'data-copal-view="bases"' not in html


def test_code_editor_launcher_lives_in_copal_submenu_only():
    html = (ROOT / "static/index.html").read_text()
    anchors = _anchors(html)
    launchers = [item for item in anchors if item["attrs"].get("id") == "tool-code-btn"]
    assert len(launchers) == 1
    launcher = launchers[0]
    assert launcher["attrs"].get("href") == "/code"
    assert "".join(launcher["text"]).strip() == "Editor"
    assert launcher["attrs"].get("data-copal-compat") == "true"
    assert any(attrs.get("id") == "copal-submenu" for _, attrs in launcher["ancestors"])
    visible_editor = [item for item in anchors if item["attrs"].get("href") == "/editor" and "".join(item["text"]).strip() == "Editor" and "display:none" not in item["attrs"].get("style", "").replace(" ", "")]
    assert len(visible_editor) == 1
    assert "display:none" in launcher["attrs"].get("style", "").replace(" ", "")
    # The legacy rail-notes node remains for compatibility wiring, but the
    # canonical rail-copal-launchers Editor button is the only visible rail
    # launcher in the shipped static shell.
    rail_notes = html[html.index('id="rail-notes"') - 80:html.index('id="rail-notes"') + 80]
    assert "display:none" in rail_notes.replace(" ", "")


def test_files_launcher_lives_in_copal_submenu_and_uses_shared_open_clank_window():
    html = (ROOT / "static/index.html").read_text()
    source = (ROOT / "static/js/files.js").read_text()
    app = (ROOT / "static/app.js").read_text()
    submenu_start = html.index('<div id="copal-submenu"')
    submenu_end = html.index('</div>', submenu_start)
    launcher = next(item for item in _anchors(html) if item["attrs"].get("id") == "tool-files-btn")
    assert launcher["attrs"].get("href") == "/files"
    assert "".join(launcher["text"]).strip() == "Files"
    assert any(attrs.get("id") == "copal-submenu" for _, attrs in launcher["ancestors"])
    assert "createOpenClankWindow" in source
    assert "id: 'files-window'" in source
    assert "/api/odysseus-files/navigation-roots" in source
    assert "listDirectory" in source
    assert "startBrowserDownload" in source
    assert "filesFacadeClient.roots" in source
    assert "filesFacadeClient.children" in source
    assert "filesFacadeClient.contentUrl" in source
    assert "/api/copal/documents/" not in source
    assert "/api/gallery/library" not in source
    assert "/api/documents/library" not in source
    assert "/api/files/library" not in source
    assert "gallery" in source
    assert "All sources" in source
    assert "Download selected" in source
    assert "Available worktree" in source
    assert "data-files-favorites" in source
    assert "data-files-tree" in source
    assert "role: 'tree'" in source
    assert "createExplorerTree" in source
    assert "fileIconKey" in source
    assert "entryVisual(entry, directory)" in source
    assert "data.next_cursor" in source
    assert "directory ? '▱' : '·'" not in source
    open_directory = source[source.index("async function openDirectory"):source.index("async function loadMoreEntries")]
    assert "renderTree(" not in open_directory
    assert "updateTreeHighlight()" in open_directory
    assert "filesModule.open()" in app


def test_code_editor_uses_native_open_clank_window_contract():
    source = (ROOT / "static/js/codeEditor.js").read_text()
    windows = (ROOT / "static/js/copal/windows.js").read_text()
    app = (ROOT / "static/app.js").read_text()
    assert "createOpenClankWindow" in source
    assert "id: 'code-editor-window'" in source
    assert "nativeWindow.body.replaceChildren(shell)" in source
    assert "document.body.append(shell)" not in source
    assert "export const createCopalWindow = createOpenClankWindow" in windows
    launcher_block = app[app.index("const toolCodeBtn"):app.index("// Refresh notes due-reminder badge")]
    assert "_collapseSidebarToRail()" not in launcher_block
    # S13 registry openers: editor-family targets must not collapse the sidebar
    # (only the /email fullscreen opener does).
    route_block = app[app.index("const _targetOpen = {"):app.index("const _openResolvedTarget")]
    assert "_collapseSidebarToRail()" not in route_block.split("email:")[0]
    assert "copalModule.open" in route_block


def test_code_editor_folder_picker_is_separate_from_agent_workspace_state():
    source = (ROOT / "static/js/codeEditor.js").read_text()
    workspace = (ROOT / "static/js/workspace.js").read_text()

    assert "const CODE_WORKSPACE_KEY = 'odysseus-code-workspace'" in source
    assert "odysseus-workspace" not in source
    assert "window._isAdmin" not in source
    assert "selection_kind: 'app_folder'" in source
    assert "selectionKind: 'app_folder'" in source
    assert "initialPath: state.root" in source
    assert "params.set('selection_kind', _browserContext.selectionKind)" in workspace

    selection = workspace[
        workspace.index("if (_browserContext.selectionKind === 'app_folder'"):
        workspace.index("closeWorkspaceBrowser();", workspace.index("if (_browserContext.selectionKind === 'app_folder'"))
    ]
    assert "await _browserContext.onSelect(_curPath)" in selection
    assert "bindWorkspacePath(_curPath, 'agent_workspace')" in selection
    assert "setWorkspace(workspace.path, workspace.id);" in selection


def test_code_editor_folder_load_failures_remain_visible_and_empty_roots_are_not_writable():
    source = (ROOT / "static/js/codeEditor.js").read_text()

    # Workspace root loading now enters through the authority-neutral lazy tree
    # adapter. A failed first page remains an honest false result and is never
    # persisted as the next workspace; successful roots mount only that page
    # and leave continuation cursors to explorerTree.
    display = source.split("async function displayWorkspaceRoot(root", 1)[1].split(
        "async function selectWorkspaceRoot", 1
    )[0]
    # The root loader is an implementation detail of the transactional open
    # path; assert the post-load guard and commit seam instead of its old call
    # spelling.
    assert "if (!page || generation !== state.requestGeneration || !state.nativeWindow.visible) return false;" in display
    assert "mountCodeExplorer(page.items, page.nextCursor, root);" in display
    assert "if (error?.name === 'AbortError'" in display
    # Explorer errors are rendered by the transactional tree adapter after
    # the candidate root is committed; the prior direct tree replacement was
    # removed so a failed root load cannot erase the visible workspace.
    assert "onError: error => setStatus(error.message || 'Folder unavailable', true)," in source
    assert "return false;" in display
    assert "createExplorerTree({" in source
    assert "rootNextCursor: nextCursor" in source
    assert "loadRootPage:" in source
    selection = source.split("async function selectWorkspaceRoot(path)", 1)[1].split(
        "function openWorkspaceFolder", 1
    )[0]
    assert selection.index("const loaded = await displayWorkspaceRoot(path);") < selection.index(
        "bindWorkspacePath(path, 'app_folder')"
    )
    assert selection.index("bindWorkspacePath(path, 'app_folder')") < selection.index(
        "saveWorkspaceRoot(workspace.path, state.workspaceOwner, workspace.id);"
    )
    assert "if (!loaded) throw new Error" in selection
    assert source.count("if (!state.root) { setStatus('Open a workspace folder first', true); return; }") == 2


def test_code_editor_save_refresh_and_account_lifecycles_are_separate():
    source = (ROOT / "static/js/codeEditor.js").read_text()
    init = (ROOT / "static/js/init.js").read_text()

    save = source.split("async function saveBuffer(path)", 1)[1].split(
        "function saveActive", 1
    )[0]
    assert "state.requestGeneration" not in save
    assert "const saveWorkspaceEpoch = state.workspaceEpoch;" in save
    assert "state.buffers.get(path) === buffer" in save
    assert "state.workspaceOwner === saveOwner" in save
    assert "state.root === saveRoot" in save

    assert "document.dispatchEvent(new CustomEvent('openclank:auth-user-ready'" in init
    assert "document.addEventListener('openclank:auth-user-ready'" in source
    assert "window.addEventListener('openclank:auth-user-ready'" not in source
    clear = source.split("function clearWorkspaceOwnerState", 1)[1].split(
        "async function handleAuthenticatedOwnerReady", 1
    )[0]
    assert "buffer.saveController?.abort?.();" in clear
    assert "state.bufferReconcileController?.abort?.();" in clear
    assert "state.closedBuffers.length = 0;" in clear
    reload = source.split("async function handleAuthenticatedOwnerReady", 1)[1].split(
        "async function resolveWorkspaceRoot", 1
    )[0]
    assert "const root = await workspaceRoot();" in reload
    assert "return displayWorkspaceRoot(root, generation);" in reload


def test_code_editor_file_reads_reload_and_identity_outages_are_exactly_scoped():
    source = (ROOT / "static/js/codeEditor.js").read_text()

    owner = source.split("async function authenticatedOwner()", 1)[1].split(
        "function abortPendingFileReads", 1
    )[0]
    assert "{ status: 'unavailable', owner: null }" in owner
    assert "{ status: 'confirmed', owner:" in owner

    workspace = source.split("async function workspaceRoot()", 1)[1].split(
        "function displayName", 1
    )[0]
    assert "const identityUnavailable" in workspace
    assert "if (identityUnavailable) return saved;" in workspace
    assert "validated.status === 'unavailable'" in workspace

    opened = source.split("async function openFile(path", 1)[1].split(
        "function closeTabMenu", 1
    )[0]
    assert "state.pendingFileReads.get(path)" in opened
    assert "pending.activationSequence = activationSequence" in opened
    assert "entry.activationSequence === state.fileActivationSequence" in opened
    assert "state.buffers.get(entry.path) === buffer" in opened
    # A rename rekeys an in-flight request's path; the completion guard uses
    # the rekeyed path and buffer identity rather than rejecting that request
    # because its original requested path is now stale.
    assert "requestedPath: path" in opened

    mutation = source.split("function beginTreeMutation", 1)[1].split(
        "function mountCodeExplorer", 1
    )[0]
    assert "subtreeBufferEntries(reservation.source)" in mutation
    assert "pending.path = nextPath" in mutation
    assert "treeMutationBuffersChanged(reservation)" in mutation

    reload = source.split("async function reloadBufferFromDisk(path)", 1)[1].split(
        "async function closeBuffers", 1
    )[0]
    assert "const reloadWorkspaceEpoch = state.workspaceEpoch;" in reload
    assert "state.buffers.get(path) === buffer" in reload
    assert "buffer.reloadController = controller" in reload
    assert "signal: controller.signal" in reload
    assert "(buffer.revision || 0) === reloadRevision" in reload

    editor = source.split("function renderEditor()", 1)[1].split(
        "async function reloadBufferFromDisk", 1
    )[0]
    assert "saveBuffer(buffer.path)" in editor


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


def test_base_query_preview_uses_dirty_definition_without_write_and_rejects_stale_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = BaseBridge(tmp_path)
    app.state.copal_bridge = bridge
    http = TestClient(app)
    dirty_source = bridge.docs["BASE"]["text"].replace("value: active", "value: parked")

    preview = http.post(
        "/api/copal/bases/BASE/query/preview?workspace=personal&page=1&page_size=100",
        json={"source": dirty_source, "base": "base-head", "definitionRevision": 7},
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["draft"] is True and body["definitionRevision"] == 7
    assert [row["documentId"] for row in body["rows"]] == ["B"]
    preview_index = [call for call in bridge.calls if call[0] == "index"][-1]
    assert preview_index[1] == {"owner": "local", "workspace_id": "personal"}
    assert not any(call[0] == "write" for call in bridge.calls)

    stale = http.post(
        "/api/copal/bases/BASE/query/preview?workspace=personal",
        json={"source": dirty_source, "base": "old-head", "definitionRevision": 8},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["outcome"] == "stale"


def test_base_migration_and_row_edit_use_optimistic_redb_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = BaseBridge(tmp_path)
    app.state.copal_bridge = bridge
    http = TestClient(app)

    dry = http.post("/api/copal/bases/BASE/migrate?workspace=personal&dry_run=true", json={})
    assert dry.status_code == 200
    canonical = dry.json()["canonical"]
    assert canonical.lstrip().startswith("version: 1\n")
    definition, diagnostics = parse_base_definition(canonical)
    assert not diagnostics
    assert definition["sourceVersion"] == 1
    assert definition["views"][0]["id"] == "table"
    assert definition["views"][0]["localFilters"] == {
        "property": "status", "operator": "eq", "value": "active",
    }
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


async def test_track_registry_get_is_nonmutating_and_put_upgrades_with_cas(tmp_path, monkeypatch):
    http, bridge = canonical_planning_client(tmp_path, monkeypatch, schema_version=1)
    original = dict(bridge.docs["TRACKS"])

    projected = await http.get("/api/copal/planning?workspace=personal")

    assert projected.status_code == 200
    assert projected.json()["schemaVersion"] == 2
    assert [track["parentTrackId"] for track in projected.json()["tracks"]] == [None, None]
    assert bridge.docs["TRACKS"] == original
    assert not any(operation in {"create", "write", "delete"} for operation, _, _ in bridge.calls)

    tracks = [
        {"id": "child", "name": "Child", "color": "#123456", "icon": "c", "enabled": True, "parentTrackId": "home", "extension": {"keep": True}},
        {"id": "car", "name": "Car", "color": "#0ea5e9", "icon": "car", "enabled": True},
        {"id": "home", "name": "Home", "color": "#14b8a6", "icon": "home", "enabled": True},
    ]
    upgraded = await http.put(
        "/api/copal/planning/tracks?workspace=personal",
        json={"tracks": tracks, "base": "tracks-h1"},
    )

    assert upgraded.status_code == 200
    assert [track["id"] for track in upgraded.json()["tracks"]] == ["car", "home", "child"]
    stored = json.loads(bridge.docs["TRACKS"]["text"])
    assert stored["schemaVersion"] == 2
    assert stored["vendor"] == {"preserve": True}
    assert stored["tracks"][2]["extension"] == {"keep": True}
    write = next(call for call in reversed(bridge.calls) if call[0] == "write")
    assert write[1]["owner"] == "local"
    assert write[1]["workspace_id"] == "personal"
    assert write[1]["base"] == "tracks-h1"

    stale = await http.put(
        "/api/copal/planning/tracks?workspace=personal",
        json={"tracks": tracks, "base": "stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["doc"]["head"] == bridge.docs["TRACKS"]["head"]


async def test_invalid_track_tree_fails_before_any_mutation(tmp_path, monkeypatch):
    http, bridge = canonical_planning_client(tmp_path, monkeypatch)
    bridge.calls.clear()
    invalid = [
        {"id": "a", "name": "A", "color": "#123456", "icon": "a", "enabled": True, "parentTrackId": "b"},
        {"id": "b", "name": "B", "color": "#654321", "icon": "b", "enabled": True, "parentTrackId": "a"},
    ]

    response = await http.put("/api/copal/planning/tracks", json={"tracks": invalid})

    assert response.status_code == 422
    assert not any(operation in {"create", "write", "delete"} for operation, _, _ in bridge.calls)


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/api/copal/planning/migrate?dry_run=false", {"action": "apply"}),
        ("post", "/api/copal/planning/events", {"event": {"title": "New", "trackId": "home"}}),
        ("patch", "/api/copal/planning/events/EVENT", {"patch": {"title": "Changed"}}),
        ("delete", "/api/copal/planning/events/EVENT", None),
        ("post", "/api/copal/calendar/reconcile", {}),
    ],
)
async def test_future_track_schema_preflights_every_planning_mutation(
    tmp_path, monkeypatch, method, path, payload
):
    http, bridge = canonical_planning_client(tmp_path, monkeypatch, schema_version=3)
    bridge.calls.clear()

    response = await (
        getattr(http, method)(path, json=payload)
        if payload is not None
        else getattr(http, method)(path)
    )

    assert response.status_code == 422
    assert not any(operation in {"create", "write", "delete"} for operation, _, _ in bridge.calls)


async def test_malformed_falsey_tracks_preflight_event_mutation(tmp_path, monkeypatch):
    http, bridge = canonical_planning_client(tmp_path, monkeypatch, schema_version=2)
    bridge.docs["TRACKS"]["text"] = json.dumps({"schemaVersion": 2, "tracks": False})
    bridge.calls.clear()

    response = await http.post(
        "/api/copal/planning/events",
        json={"event": {"title": "New", "trackId": "arbitrary"}},
    )

    assert response.status_code == 422
    assert not any(operation in {"create", "write", "delete"} for operation, _, _ in bridge.calls)


async def test_targeted_calendar_reconcile_preflights_future_registry(tmp_path, monkeypatch):
    http, bridge = canonical_planning_client(tmp_path, monkeypatch, schema_version=3)
    bridge.calls.clear()

    response = await http.post(
        "/api/copal/calendar/reconcile",
        json={"document_id": "LEGACY"},
    )

    assert response.status_code == 422
    assert not any(operation in {"create", "write", "delete"} for operation, _, _ in bridge.calls)


async def test_generic_document_mutations_cannot_bypass_planning_domain_routes(tmp_path, monkeypatch):
    http, bridge = canonical_planning_client(tmp_path, monkeypatch)
    canonical_event = bridge.docs["EVENT"]["text"]
    bridge.docs.update({
        "TREEHOUSE": {
            "id": "TREEHOUSE", "kind": "treehouse-state", "name": ".copal/treehouse-state.json",
            "head": "treehouse-h1", "text": "{}", "frontmatter": {}, "tags": [], "links": [],
        },
        "CALENDAR": {
            "id": "CALENDAR", "kind": "calendar-projection", "name": ".copal/calendar-projection-test.json",
            "head": "calendar-h1", "text": "{}", "frontmatter": {}, "tags": [], "links": [],
        },
        "OPERATION": {
            "id": "OPERATION", "kind": "copal-operation", "name": ".copal/operations/test.json",
            "head": "operation-h1", "text": "{}", "frontmatter": {}, "tags": [], "links": [],
        },
    })
    requests = [
        ("post", "/api/copal/documents", {"name": ".copal/tracks.json", "kind": "copal-tracks", "content": "{}"}),
        ("post", "/api/copal/documents", {"name": ".copal/treehouse-state.json", "kind": "treehouse-state", "content": "{}"}),
        ("post", "/api/copal/documents", {"name": ".copal/calendar-projection-test.json", "kind": "calendar-projection", "content": "{}"}),
        ("post", "/api/copal/documents", {"name": ".copal/operations/test.json", "kind": "copal-operation", "content": "{}"}),
        ("post", "/api/copal/documents", {"name": "event.md", "kind": "markdown", "content": canonical_event}),
        ("put", "/api/copal/documents/TRACKS", {"content": "{}"}),
        ("put", "/api/copal/documents/TREEHOUSE", {"content": "{}"}),
        ("delete", "/api/copal/documents/CALENDAR", None),
        ("post", "/api/copal/documents/OPERATION/rename", {"name": "renamed.json"}),
        ("delete", "/api/copal/documents/EVENT", None),
        ("post", "/api/copal/documents/EVENT/checkpoint", {}),
        ("post", "/api/copal/documents/TRACKS/rename", {"name": "renamed.json"}),
        ("post", "/api/copal/documents/EVENT/restore", {"commit": "old-head"}),
    ]

    for method, path, payload in requests:
        bridge.calls.clear()
        response = await (
            getattr(http, method)(path, json=payload)
            if payload is not None
            else getattr(http, method)(path)
        )
        assert response.status_code == 409, path
        assert not any(
            operation in {"create", "write", "delete", "checkpoint", "rename", "restore"}
            for operation, _, _ in bridge.calls
        ), path

    bridge.calls.clear()
    trashed = await http.post("/api/copal/trash/TRASHED/restore")
    assert trashed.status_code == 409
    assert any(operation == "trash" for operation, _, _ in bridge.calls)
    assert not any(operation == "restore_deleted" for operation, _, _ in bridge.calls)


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
        "name": "Test Meme",
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


class FacetsBridge(FakeBridge):
    """Corpus that exercises official-root binding and facet discovery."""

    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.docs = {
            "p-a": {
                "id": "p-a", "kind": "note", "name": "Notes/Personal A.md", "head": "p-a-h",
                "text": "a", "format": "copal-note-v1", "storage": "database",
                "tags": ["personal", "keep"], "properties": {"status": "open"}, "frontmatter": {}, "links": [],
            },
            "p-b": {
                "id": "p-b", "kind": "note", "name": "Notes/Personal B.md", "head": "p-b-h",
                "text": "b", "format": "copal-note-v1", "storage": "database",
                "tags": ["personal"], "properties": {"status": "done"}, "frontmatter": {}, "links": [],
            },
            "o-mix": {
                "id": "o-mix", "kind": "note", "name": "Notes/Official Guide", "head": "o-mix-h",
                "text": "guide", "format": "copal-note-v1", "storage": "database",
                "tags": ["builtin"], "properties": {"product": "open-clank", "builtin": True},
                "builtin": True, "frontmatter": {}, "links": [],
            },
            "o-out": {
                "id": "o-out", "kind": "note", "name": "Other/Moved Official", "head": "o-out-h",
                "text": "moved", "format": "copal-note-v1", "storage": "database",
                "tags": ["builtin"], "properties": {"product": "open-clank"},
                "builtin": True, "frontmatter": {}, "links": [],
            },
        }

    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, args, timeout))
        if operation == "index":
            docs = list(self.docs.values())
            if args.get("kind"):
                docs = [doc for doc in docs if doc["kind"] == args["kind"]]
            return {"docs": docs}
        return await super().call(operation, args, timeout)


def test_graph_facets_bind_official_root_by_identity_and_paginate(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    app.state.copal_bridge = FacetsBridge(tmp_path)
    http = TestClient(app)

    response = http.get("/api/copal/graph/facets?workspace=personal")
    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == 1
    assert payload["totalDocuments"] == 4
    # A top folder shared with personal notes is never claimed as the official
    # root, and provisioned docs outside any root cannot invent one.
    assert payload["officialRoot"] is None
    facets = payload["facets"]
    kinds = {item["value"]: item["count"] for item in facets["kinds"]["values"]}
    assert kinds == {"note": 4}
    folders = {item["value"]: item["count"] for item in facets["folders"]["values"]}
    assert folders == {"Notes": 3, "Other": 1}
    tags = {item["value"]: item["count"] for item in facets["tags"]["values"]}
    assert tags == {"personal": 2, "keep": 1, "builtin": 2}
    assert facets["tags"]["hasMore"] is False
    props = {item["value"]: item["count"] for item in facets["properties"]["status"]["values"]}
    assert props == {"open": 1, "done": 1}

    # Pagination: a small window reports hasMore and offset advances.
    page = http.get("/api/copal/graph/facets?workspace=personal&category=tags&limit=1&offset=0").json()
    assert page["facets"]["tags"]["limit"] == 1
    assert page["facets"]["tags"]["offset"] == 0
    assert page["facets"]["tags"]["total"] == 3
    assert page["facets"]["tags"]["hasMore"] is True
    assert len(page["facets"]["tags"]["values"]) == 1
    page2 = http.get("/api/copal/graph/facets?workspace=personal&category=tags&limit=1&offset=1").json()
    assert page2["facets"]["tags"]["offset"] == 1
    assert page2["facets"]["tags"]["hasMore"] is True
    assert page2["facets"]["tags"]["values"][0]["value"] != page["facets"]["tags"]["values"][0]["value"]

    # Query narrows facet values so off-page entries stay discoverable.
    searched = http.get("/api/copal/graph/facets?workspace=personal&category=tags&query=keep").json()
    assert searched["facets"]["tags"]["total"] == 1
    assert searched["facets"]["tags"]["values"] == [{"value": "keep", "count": 1}]

    # Generation is stable for the same corpus and changes when it changes.
    again = http.get("/api/copal/graph/facets?workspace=personal").json()
    assert again["generation"] == payload["generation"]
    app.state.copal_bridge.docs["p-a"]["head"] = "p-a-h-next"
    changed = http.get("/api/copal/graph/facets?workspace=personal").json()
    assert changed["generation"] != payload["generation"]


def test_graph_facets_bind_clean_official_folder_as_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())

    class CleanOfficialBridge(FacetsBridge):
        def __init__(self, data_dir):
            super().__init__(data_dir)
            self.docs = {
                "p1": {
                    "id": "p1", "kind": "note", "name": "Notes/Personal.md", "head": "p1-h",
                    "text": "p", "format": "copal-note-v1", "storage": "database",
                    "tags": [], "properties": {}, "frontmatter": {}, "links": [],
                },
                "o1": {
                    "id": "o1", "kind": "note", "name": "OpenClank/Start Here", "head": "o1-h",
                    "text": "o", "format": "copal-note-v1", "storage": "database",
                    "tags": [], "properties": {"product": "open-clank"},
                    "builtin": True, "frontmatter": {}, "links": [],
                },
                "o2": {
                    "id": "o2", "kind": "note", "name": "OpenClank/Guide.md", "head": "o2-h",
                    "text": "g", "format": "copal-note-v1", "storage": "database",
                    "tags": [], "properties": {"product": "open-clank"},
                    "builtin": True, "frontmatter": {}, "links": [],
                },
            }

    app.state.copal_bridge = CleanOfficialBridge(tmp_path)
    http = TestClient(app)
    payload = http.get("/api/copal/graph/facets?workspace=personal").json()
    assert payload["officialRoot"] == "OpenClank"
    assert payload["totalDocuments"] == 3

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, Document, FilesImageResource
from src.openclank.copal_loose import LooseCopalBridge
from src.openclank.file_policy import FilePolicyRepository
from src.openclank.filesystem_registry import FilesystemRootRegistry
from src.openclank.files_host_provider import _origin
from src.openclank.resource_refs import issue_resource_ref


class Auth:
    def account_id(self, username):
        return {"alice": "account-alice", "bob": "account-bob"}.get(username)

    def is_admin(self, username):
        return username == "alice"


class HostClient:
    def __init__(self, owner, scope):
        self.owner = owner
        self.scope = scope
        self.calls = []
        self.stages = {}

    async def stage_begin(self, path):
        stage_id = f"stage-{len(self.stages) + 1}"
        self.stages[stage_id] = bytearray()
        return {"data": {"stage_id": stage_id, "max_chunk_bytes": 512 * 1024, "max_total_bytes": 64 * 1024 * 1024}}

    async def stage_chunk(self, stage_id, chunk, *, offset):
        assert offset == len(self.stages[stage_id])
        self.stages[stage_id].extend(chunk)
        return {"data": {"stage_id": stage_id, "offset": len(self.stages[stage_id])}}

    async def stage_finish(self, stage_id, target, *, length, digest):
        payload = bytes(self.stages.pop(stage_id))
        assert len(payload) == length
        return await self.request("create", target, {"content": payload})

    async def stage_abort(self, stage_id):
        self.stages.pop(stage_id, None)
        return {"data": {"aborted": True}}

    async def request(self, operation, path, payload=None):
        self.calls.append((operation, path, dict(payload or {})))
        if operation == "filename_search":
            assert payload["max_results"] <= 200
            return {
                "data": {
                    "matches": [f"{path.rstrip('/')}/Folder", f"{path.rstrip('/')}/note.txt"],
                    "complete": True,
                    "work": {"entries_visited": 3, "bytes_read": 0},
                }
            }
        if operation == "list_directory":
            return {
                "data": {
                    "path": path,
                    "entries": [
                        {
                            "name": "Folder",
                            "kind": "Directory",
                            "size": 0,
                            "modified_unix_ms": 10,
                        },
                        {
                            "name": "note.txt",
                            "kind": "File",
                            "size": 4,
                            "modified_unix_ms": 20,
                        },
                        {
                            "name": "link",
                            "kind": "Symlink",
                            "size": 0,
                            "modified_unix_ms": 30,
                        },
                    ],
                    "next_cursor": None,
                    "generation": 7,
                }
            }
        if operation == "stat":
            is_folder = str(path).endswith("/Folder") or Path(path).is_dir()
            data = {
                "data": {
                    "path": path,
                    "kind": "Directory" if is_folder else "File",
                    "size": 0 if is_folder else 4,
                    "modified_unix_ms": 20,
                }
            }
            if payload and payload.get("include_fingerprint"):
                data["data"]["fingerprint"] = {"kind": "hostFingerprint", "value": "fp-host-v1"}
            return data
        if operation == "create":
            return {"data": {"path": path, "fingerprint": {"kind": "hostFingerprint", "value": "fp-created-v1"}}}
        if operation == "move":
            return {"data": {"path": payload["destination"], "fingerprint": {"kind": "hostFingerprint", "value": "fp-moved-v1"}}}
        if operation == "read_lines":
            return {
                "data": {
                    "path": path,
                    "text": "note\n",
                    "fingerprint": {"kind": "hostFingerprint", "value": "fp-note-v1"},
                    "encoding": "utf-8",
                    "newline": "\\n",
                    "bom_bytes": 0,
                    "mode": "text",
                    "language": "plaintext",
                }
            }
        raise AssertionError(f"unexpected Host operation: {operation}")

    async def open_handle(self, path):
        self.calls.append(("open_handle", path, {}))
        return {
            "handle": {
                "token": "opaque-test-handle",
                "root_id": "host",
                "relative_components": ["note.txt"],
                "generation": 7,
            },
            "size": 4,
            "modified_unix_ms": 20,
            "object_tag": "object-v1",
        }

    async def stream_read_handle(self, handle, *, offset, length, chunk_bytes=256 * 1024):
        self.calls.append((
            "stream_read_handle",
            "<opaque-handle>",
            {"offset": offset, "length": length, "chunk_bytes": chunk_bytes},
        ))
        data = b"note"[offset:offset + length]
        if data:
            yield data

    async def thumbnail_handle(self, handle, *, width, height, scale=1.0, deadline_ms=2_000):
        self.calls.append((
            "thumbnail_handle",
            "<opaque-handle>",
            {"width": width, "height": height, "scale": scale, "deadline_ms": deadline_ms},
        ))
        return b"\x89PNG\r\n\x1a\nopaque-native-content-pixels"

    async def watch_path(self, path):
        self.calls.append(("watch_path", path, {}))
        yield {
            "sequence": 0,
            "kind": "modified",
            "rescan_required": False,
            "observed_unix_ms": 123,
        }


def _app(tmp_path, host_client_factory_override=None):
    from routes.files_facade_routes import setup_files_facade_routes

    app = FastAPI()
    app.state.auth_manager = Auth()
    app.state.copal_bridge = LooseCopalBridge(tmp_path / "copal")
    engine = create_engine(f"sqlite:///{tmp_path / 'managed.db'}")
    Base.metadata.create_all(engine)
    managed_sessions = sessionmaker(bind=engine)
    owner = {"value": "alice"}
    host_registry = FilesystemRootRegistry(tmp_path / "host-roots.json")
    host_clients = []

    def host_client_factory(username, *, app_scope=None):
        factory = host_client_factory_override or HostClient
        client = factory(username, app_scope or {"host": True})
        host_clients.append(client)
        return client

    app.state.host_registry = host_registry
    app.state.host_clients = host_clients

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        request.state.current_user = owner["value"]
        return await call_next(request)

    policy_repository = FilePolicyRepository(tmp_path / "app.db")
    app.state.file_policy_repository = policy_repository
    app.include_router(setup_files_facade_routes(
        policy_repository=policy_repository,
        session_factory=managed_sessions,
        filesystem_registry=host_registry,
        host_client_factory=host_client_factory,
    ))
    return app, owner, managed_sessions


def test_roots_and_children_return_only_opaque_owner_bound_refs(tmp_path):
    app, owner, _sessions = _app(tmp_path)
    with TestClient(app) as client:
        roots = client.get("/api/files-v1/roots")
        assert roots.status_code == 200
        assert {row["provider"] for row in roots.json()["entries"]} == {"host", "copal", "gallery", "library"}
        copal = next(row for row in roots.json()["entries"] if row["provider"] == "copal")
        assert "account-alice" not in copal["ref"]

        assert "workspace:default" not in copal["ref"]
        page = client.post("/api/files-v1/children", json={"parent_ref": copal["ref"]})
        assert page.status_code == 200
        assert {row["name"] for row in page.json()["entries"]} == {"Documents", "Notes", "Wiki", "System", "Trash"}

        owner["value"] = "bob"
        forged = client.post("/api/files-v1/children", json={"parent_ref": copal["ref"]})
        assert forged.status_code == 404
        assert forged.json()["detail"]["code"] == "resource_unavailable"


def test_create_directory_request_is_strictly_typed(tmp_path):
    app, _owner, _sessions = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/files-v1/create-directory", json={
            "parent_ref": "rr1.parent", "name": "Nested", "operation_id": "mkdir-1",
            "generation": "7",
        })
    assert response.status_code == 422


def test_auth_disabled_uses_stable_local_files_principal(tmp_path, monkeypatch):
    app, owner, _sessions = _app(tmp_path)
    owner["value"] = ""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    with TestClient(app) as client:
        response = client.get("/api/files-v1/roots")
    assert response.status_code == 200
    assert {row["provider"] for row in response.json()["entries"]} == {"host", "copal", "gallery", "library"}


def test_host_namespace_is_opaque_rust_backed_and_capability_truthful(tmp_path):
    app, owner, _sessions = _app(tmp_path)
    with TestClient(app) as client:
        roots = client.get("/api/files-v1/roots").json()["entries"]
        host = next(row for row in roots if row["provider"] == "host")
        assert host["name"] == "Host locations"
        assert host["capabilities"] == ["children", "stat"]

        anchors_response = client.post("/api/files-v1/children", json={
            "parent_ref": host["ref"],
            "limit": 1,
        })
        assert anchors_response.status_code == 200
        anchors = anchors_response.json()
        assert len(anchors["entries"]) == 1
        assert anchors["next_cursor"]
        assert "/Users/" not in anchors_response.text

        second = client.post("/api/files-v1/children", json={
            "parent_ref": host["ref"],
            "cursor": anchors["next_cursor"],
            "limit": 1,
        })
        assert second.status_code == 200
        all_anchors = anchors["entries"] + second.json()["entries"]
        directory = next(row for row in all_anchors if row["kind"] == "folder")

        page_response = client.post("/api/files-v1/children", json={
            "parent_ref": directory["ref"],
            "limit": 37,
            "sort": {"key": "modified", "direction": "desc", "directories_first": True},
        })
        assert page_response.status_code == 200
        page = page_response.json()["entries"]
        assert [(row["name"], row["kind"]) for row in page] == [
            ("Folder", "folder"),
            ("note.txt", "file"),
            ("link", "symlink"),
        ]
        assert next(row for row in page if row["kind"] == "file")["capabilities"] == [
            "download", "move", "open", "preview", "rename", "stat", "write",
        ]
        file_entry = next(row for row in page if row["kind"] == "file")
        opened = client.post("/api/files-v1/action", json={"resource_ref": file_entry["ref"], "action": "open", "args": {}})
        assert opened.status_code == 200
        assert opened.json()["target"] == {"app": "editor"}
        assert opened.json()["exact"] is True
        payload = client.post("/api/files-v1/open-resource", json={"resource_ref": file_entry["ref"]})
        assert payload.status_code == 200
        body = payload.json()
        assert body["target"] == {"app": "editor"}
        assert body["payload"]["text"] == "note\n"
        assert body["payload"]["representation"] == "text"
        assert body["payload"]["encoding"] == "utf-8"
        assert body["payload"]["newline"] == "\\n"
        assert body["payload"]["mode"] == "text"
        assert body["payload"]["language"] == "plaintext"
        assert body["payload"]["resource"]["metadata"] == {
            "encoding": "utf-8",
            "newline": "\\n",
            "language": "plaintext",
            "mode": "text",
            "bomBytes": 0,
        }
        assert body["payload"]["resource"]["locator"]["opaqueRef"] == file_entry["ref"]
        expected_folder_caps = ["children", "search", "stat"]
        if sys.platform == "darwin":
            expected_folder_caps.append("watch")
        expected_folder_caps.append("write")
        assert next(row for row in page if row["kind"] == "folder")["capabilities"] == expected_folder_caps
        assert all("path" not in row["provenance"] for row in page)
        list_call = next(call for host_client in app.state.host_clients for call in host_client.calls if call[0] == "list_directory" and call[2].get("limit") == 37)
        assert list_call[2]["limit"] == 37
        assert list_call[2]["sort"] == {
            "key": "modified",
            "direction": "desc",
            "directories_first": True,
            "collation": "open-clank-v1",
        }

        search_response = client.post("/api/files-v1/children", json={
            "parent_ref": directory["ref"],
            "query": "note",
            "limit": 10,
            "sort": {"key": "name", "direction": "asc", "directories_first": True},
        })
        assert search_response.status_code == 200
        assert [(row["name"], row["kind"]) for row in search_response.json()["entries"]] == [
            ("Folder", "folder"),
            ("note.txt", "file"),
        ]
        assert "/Users/" not in search_response.text
        search_calls = [
            call
            for host_client in app.state.host_clients
            for call in host_client.calls
            if call[0] == "filename_search"
        ]
        assert len(search_calls) == 1
        assert search_calls[0][2]["query"] == "note"

        global_search = client.post("/api/files-v1/search", json={
            "query": "note",
            "limit": 20,
            "sort": {"key": "name", "direction": "asc", "directories_first": True},
        })
        assert global_search.status_code == 200
        assert {row["name"] for row in global_search.json()["entries"]} >= {"Folder", "note.txt"}
        assert "/Users/" not in global_search.text
        assert global_search.json()["providers"]["host"]["available"] is True

        note = next(row for row in page if row["name"] == "note.txt")
        downloaded = client.get(f"/api/files-v1/content/{note['ref']}")
        assert downloaded.status_code == 200
        assert downloaded.content == b"note"
        assert downloaded.headers["content-disposition"].startswith("attachment;")
        assert "/Users/" not in downloaded.text

        ranged = client.get(
            f"/api/files-v1/content/{note['ref']}",
            headers={"Range": "bytes=1-2"},
        )
        assert ranged.status_code == 206
        assert ranged.content == b"ot"
        assert ranged.headers["content-range"] == "bytes 1-2/4"

        stream_count = sum(
            1
            for host_client in app.state.host_clients
            for call in host_client.calls
            if call[0] == "stream_read_handle"
        )
        headed = client.head(f"/api/files-v1/content/{note['ref']}")
        assert headed.status_code == 200
        assert headed.content == b""
        assert sum(
            1
            for host_client in app.state.host_clients
            for call in host_client.calls
            if call[0] == "stream_read_handle"
        ) == stream_count

        thumbnail = client.get(
            f"/api/files-v1/thumbnail/{note['ref']}?width=96&height=64&scale=2",
        )
        assert thumbnail.status_code == 200
        assert thumbnail.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert thumbnail.headers["content-type"] == "image/png"
        assert thumbnail.headers["cache-control"] == "private, no-store"
        assert thumbnail.headers["cross-origin-resource-policy"] == "same-origin"
        thumbnail_call = next(
            call
            for host_client in app.state.host_clients
            for call in host_client.calls
            if call[0] == "thumbnail_handle"
        )
        assert thumbnail_call[2] == {
            "width": 96, "height": 64, "scale": 2.0, "deadline_ms": 2_000,
        }

        folder = next(row for row in page if row["kind"] == "folder")
        if sys.platform == "darwin":
            with client.stream("POST", "/api/files-v1/watch", json={"resource_ref": folder["ref"]}) as watched:
                assert watched.status_code == 200
                body = "".join(watched.iter_text())
            assert "event: files-change" in body
            assert '"kind":"modified"' in body
            assert "path" not in body
            assert any(
                call[0] == "watch_path"
                for host_client in app.state.host_clients
                for call in host_client.calls
            )

        # A fresh non-admin ref projects only explicitly readable assignments.
        assigned = tmp_path / "assigned"
        assigned.mkdir()
        exact = assigned / "only.txt"
        exact.write_text("x", encoding="utf-8")
        hidden = tmp_path / "write-only"
        hidden.mkdir()
        registry = app.state.host_registry
        assigned_root = registry.add("alice", str(assigned), "recursive_directory", ["read", "write"])
        exact_root = registry.add("alice", str(exact), "exact_file", ["read"])
        hidden_root = registry.add("alice", str(hidden), "recursive_directory", ["read", "write"])
        registry.assign_visibility("alice", "bob", assigned_root["id"], ["read"])
        registry.assign_visibility("alice", "bob", exact_root["id"], ["read"])
        registry.assign_visibility("alice", "bob", hidden_root["id"], ["write"])
        owner["value"] = "bob"
        bob_host = next(row for row in client.get("/api/files-v1/roots").json()["entries"] if row["provider"] == "host")
        bob_page = client.post("/api/files-v1/children", json={"parent_ref": bob_host["ref"]})
        assert bob_page.status_code == 200
        assert {row["name"] for row in bob_page.json()["entries"]} == {"assigned", "only.txt"}
        assert "write-only" not in bob_page.text
        exact_entry = next(row for row in bob_page.json()["entries"] if row["name"] == "only.txt")
        denied_children = client.post("/api/files-v1/children", json={"parent_ref": exact_entry["ref"]})
        assert denied_children.status_code == 404


def test_host_editor_save_is_opaque_cas_and_preserves_utf16_bom_newline(tmp_path):
    import hashlib

    target = tmp_path / "README.md"
    initial_text = "one\r\ntwo\r\n"
    target.write_bytes(b"\xff\xfe" + initial_text.encode("utf-16-le"))

    class Utf16HostClient(HostClient):
        def _snapshot(self):
            raw = target.read_bytes()
            text = raw[2:].decode("utf-16-le")
            return {
                "path": str(target),
                "text": text,
                "fingerprint": {"kind": "hostFingerprint", "value": hashlib.sha256(raw).hexdigest()},
                "encoding": "utf-16-le",
                "newline": "\r\n",
                "bom_bytes": 2,
                "mode": "text",
                "language": "markdown",
                "representation": "markdown",
            }

        async def request(self, operation, path, payload=None):
            self.calls.append((operation, path, dict(payload or {})))
            if operation == "stat":
                return {"data": {"path": str(target), "kind": "File", "size": target.stat().st_size, "modified_unix_ms": 20}}
            if operation == "read_lines":
                return {"data": self._snapshot()}
            if operation == "patch":
                current = self._snapshot()
                expected = (payload.get("expected_fingerprint") or {}).get("value")
                assert expected == current["fingerprint"]["value"]
                assert payload["old"] == current["text"]
                normalized = str(payload["new"]).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
                target.write_bytes(b"\xff\xfe" + normalized.encode("utf-16-le"))
                return {"data": {"new_fingerprint": {"kind": "hostFingerprint", "value": self._snapshot()["fingerprint"]["value"]}}}
            raise AssertionError(f"unexpected UTF-16 Host operation: {operation}")

    app, owner, _sessions = _app(
        tmp_path,
        host_client_factory_override=lambda username, scope: Utf16HostClient(username, scope),
    )
    generation = app.state.file_policy_repository.generation()
    resource_ref = issue_resource_ref(
        owner_subject_id="account-alice",
        provider="host",
        origin_id=_origin(str(target)),
        kind="file",
        capabilities=["stat", "open"],
        policy_generation=generation,
    ).token
    with TestClient(app) as client:
        opened = client.post("/api/files-v1/open-resource", json={"resource_ref": resource_ref})
        assert opened.status_code == 200
        payload = opened.json()["payload"]
        assert payload["representation"] == "markdown"
        assert payload["resource"]["metadata"] == {
            "encoding": "utf-16-le",
            "newline": "\r\n",
            "language": "markdown",
            "mode": "text",
            "bomBytes": 2,
        }
        fingerprint = payload["resource"]["revision"]["value"]
        saved = client.post("/api/files-v1/save-resource", json={
            "resource_ref": resource_ref,
            "expected_revision": {"kind": "hostFingerprint", "value": fingerprint},
            "text": "edited\nthird\n",
        })
        assert saved.status_code == 200
        saved_payload = saved.json()
        assert saved_payload["outcome"] == "applied"
        assert str(target) not in saved.text
        assert saved_payload["snapshot"]["envelope"]["metadata"] == payload["resource"]["metadata"]
        raw = target.read_bytes()
        assert raw.startswith(b"\xff\xfe")
        assert raw[2:].decode("utf-16-le") == "edited\r\nthird\r\n"

        target.write_bytes(b"\xff\xfe" + "remote\r\n".encode("utf-16-le"))
        stale = client.post("/api/files-v1/save-resource", json={
            "resource_ref": resource_ref,
            "expected_revision": {"kind": "hostFingerprint", "value": fingerprint},
            "text": "local\n",
        })
        assert stale.status_code == 200
        stale_payload = stale.json()
        assert stale_payload["outcome"] == "conflict"
        assert str(target) not in stale.text
        assert stale_payload["remote"]["envelope"]["text"] == "remote\r\n"
        assert stale_payload["remote"]["envelope"]["metadata"]["encoding"] == "utf-16-le"

        owner["value"] = "bob"
        revoked = client.post("/api/files-v1/save-resource", json={
            "resource_ref": resource_ref,
            "expected_revision": {"kind": "hostFingerprint", "value": fingerprint},
            "text": "still local",
        })
        assert revoked.status_code == 404


def test_host_editor_unchanged_save_is_a_cas_noop(tmp_path):
    """Rust rejects empty text edits; an unchanged save must remain successful."""
    app, _owner, _sessions = _app(tmp_path)
    with TestClient(app) as client:
        host = next(row for row in client.get("/api/files-v1/roots").json()["entries"] if row["provider"] == "host")
        anchors = client.post("/api/files-v1/children", json={"parent_ref": host["ref"], "limit": 10}).json()["entries"]
        folder = next(row for row in anchors if row["kind"] == "folder")
        created = client.post("/api/files-v1/create", json={
            "parent_ref": folder["ref"], "name": "Noop.md", "text": "same\n",
        })
        assert created.status_code == 200, created.text
        resource_ref = created.json()["resource"]["ref"]
        opened = client.post("/api/files-v1/open-resource", json={"resource_ref": resource_ref})
        assert opened.status_code == 200, opened.text
        fingerprint = opened.json()["payload"]["resource"]["revision"]["value"]
        saved = client.post("/api/files-v1/save-resource", json={
            "resource_ref": resource_ref,
            "expected_revision": {"kind": "hostFingerprint", "value": fingerprint},
            "text": "note\n",
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["outcome"] == "applied"
        assert not any(call[0] == "patch" for host_client in app.state.host_clients for call in host_client.calls)


def test_host_markdown_template_create_and_move_use_opaque_parent_and_rotate_ref(tmp_path):
    app, owner, _sessions = _app(tmp_path)
    with TestClient(app) as client:
        host = next(row for row in client.get("/api/files-v1/roots").json()["entries"] if row["provider"] == "host")
        anchors = client.post("/api/files-v1/children", json={"parent_ref": host["ref"], "limit": 10}).json()["entries"]
        folder = next(row for row in anchors if row["kind"] == "folder")
        created = client.post("/api/files-v1/create", json={
            "parent_ref": folder["ref"], "name": "Meeting.md", "text": "# {{title}}\n---\nowner: keep\n",
            "action_id": "template-create-1",
        })
        assert created.status_code == 200, created.text
        resource = created.json()["resource"]
        assert created.json()["history"]["receipt"]["outcome"] == "created"
        assert "/Users/" not in created.text

        opened = client.post("/api/files-v1/open-resource", json={"resource_ref": resource["ref"]})
        assert opened.status_code == 200, opened.text
        assert opened.json()["payload"]["parent_resource_ref"]
        moved = client.post("/api/files-v1/action", json={
            "resource_ref": resource["ref"], "action": "move", "args": {"name": "Meeting-renamed.md"},
            "action_id": "template-move-1",
        })
        assert moved.status_code == 200, moved.text
        assert moved.json()["resource"]["name"] == "Meeting-renamed.md"
        assert moved.json()["resource"]["ref"] != resource["ref"]


def test_host_template_names_are_single_path_components(tmp_path):
    app, _owner, _sessions = _app(tmp_path)
    with TestClient(app) as client:
        host = next(row for row in client.get("/api/files-v1/roots").json()["entries"] if row["provider"] == "host")
        anchors = client.post("/api/files-v1/children", json={"parent_ref": host["ref"], "limit": 10}).json()["entries"]
        folder = next(row for row in anchors if row["kind"] == "folder")
        for name in ("nested/template.md", r"nested\template.md", "../escape.md", "/absolute.md"):
            response = client.post("/api/files-v1/create", json={
                "parent_ref": folder["ref"], "name": name, "text": "template\n",
            })
            assert response.status_code == 400, (name, response.text)


def test_host_move_restat_failure_keeps_typed_provider_error(tmp_path):
    from src.openclank.files_service_client import FilesServiceError

    class FailingRestatHostClient(HostClient):
        total_stat_count = 0

        def __init__(self, owner, scope):
            super().__init__(owner, scope)

        async def request(self, operation, path, payload=None):
            if operation == "stat":
                type(self).total_stat_count += 1
                if type(self).total_stat_count == 2:
                    raise FilesServiceError("scope changed", code="policy_generation_changed")
            return await super().request(operation, path, payload)

    app, _owner, _sessions = _app(
        tmp_path,
        host_client_factory_override=lambda username, scope: FailingRestatHostClient(username, scope),
    )
    generation = app.state.file_policy_repository.generation()
    resource_ref = issue_resource_ref(
        owner_subject_id="account-alice",
        provider="host",
        origin_id=_origin(str(tmp_path / "note.md")),
        kind="file",
        capabilities=["stat", "rename"],
        policy_generation=generation,
    ).token
    with TestClient(app) as client:
        moved = client.post("/api/files-v1/action", json={
            "resource_ref": resource_ref, "action": "rename", "args": {"name": "renamed.md"},
        })
        assert moved.status_code == 409
        assert moved.json()["detail"]["code"] == "resource_ref_stale"
        assert str(tmp_path) not in moved.text


def test_host_resource_workspace_handoff_is_opaque_and_only_narrows(tmp_path):
    app, owner, _sessions = _app(tmp_path)
    assigned = tmp_path / "assigned"
    folder = assigned / "Folder"
    note = assigned / "note.txt"
    folder.mkdir(parents=True)
    note.write_text("note", encoding="utf-8")

    registry = app.state.host_registry
    legacy = registry.add("alice", str(assigned), "recursive_directory", ["read", "write"])
    registry.assign_visibility("alice", "bob", legacy["id"], ["read"])
    repository = app.state.file_policy_repository
    location = repository.create_location(
        actor_subject_id="account-alice",
        path=str(assigned),
        kind="directory",
        capabilities=("read", "write"),
    )
    repository.create_binding(
        actor_subject_id="account-alice",
        binding_class="people",
        subject_id="account-bob",
        location_id=location.id,
        capabilities=("read",),
    )
    owner["value"] = "bob"

    def host_entries(client):
        host = next(
            row for row in client.get("/api/files-v1/roots").json()["entries"]
            if row["provider"] == "host"
        )
        anchor = client.post("/api/files-v1/children", json={"parent_ref": host["ref"]}).json()["entries"][0]
        return client.post("/api/files-v1/children", json={"parent_ref": anchor["ref"]}).json()["entries"]

    with TestClient(app) as client:
        entries = host_entries(client)
        file_ref = next(row["ref"] for row in entries if row["name"] == "note.txt")
        app_binding = client.post("/api/files-v1/workspace", json={
            "resource_ref": file_ref,
            "purpose": "app_folder",
        })
        assert app_binding.status_code == 200, app_binding.text
        assert app_binding.json()["open_relative"] == "note.txt"
        assert "path" not in app_binding.json()["workspace"]
        assert str(assigned) not in app_binding.text
        assert repository.list_bindings(subject_id="account-bob", binding_class="agent") == []
        workspace_id = app_binding.json()["workspace"]["id"]
        workspace_revision = app_binding.json()["workspace"]["revision"]
        catalog = client.get("/api/files-v1/workspaces")
        assert catalog.status_code == 200, catalog.text
        assert [row["workspace"]["id"] for row in catalog.json()["entries"]] == [workspace_id]
        assert catalog.json()["entries"][0]["resource"]["provider"] == "host"
        assert catalog.json()["entries"][0]["availability"] == "available"
        assert str(assigned) not in catalog.text

        renamed = client.patch(f"/api/files-v1/workspaces/{workspace_id}", json={
            "name": "Bob project",
            "expected_revision": workspace_revision,
        })
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["workspace"]["name"] == "Bob project"
        assert str(assigned) not in renamed.text
        stale = client.patch(f"/api/files-v1/workspaces/{workspace_id}", json={
            "name": "Stale rename",
            "expected_revision": workspace_revision,
        })
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "revision_conflict"

        revealed = client.post("/api/files-v1/workspace-resource", json={
            "workspace_id": workspace_id,
            "relative_path": "note.txt",
        })
        assert revealed.status_code == 200, revealed.text
        reveal_body = revealed.json()
        assert reveal_body["workspace"]["id"] == workspace_id
        assert reveal_body["resource"]["provider"] == "host"
        assert reveal_body["resource"]["kind"] == "file"
        assert reveal_body["resource"]["name"] == "note.txt"
        assert reveal_body["parent"]["kind"] == "folder"
        assert str(assigned) not in revealed.text
        assert "path" not in reveal_body["workspace"]

        revealed_folder = client.post("/api/files-v1/workspace-resource", json={
            "workspace_id": workspace_id,
            "relative_path": "Folder",
        })
        assert revealed_folder.status_code == 200, revealed_folder.text
        assert revealed_folder.json()["parent"]["id"] == revealed_folder.json()["resource"]["id"]
        traversal = client.post("/api/files-v1/workspace-resource", json={
            "workspace_id": workspace_id,
            "relative_path": "../outside.txt",
        })
        assert traversal.status_code == 422

        outside = tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        escape = assigned / "escape.txt"
        try:
            escape.symlink_to(outside)
        except (OSError, NotImplementedError):
            pass
        else:
            escaped = client.post("/api/files-v1/workspace-resource", json={
                "workspace_id": workspace_id,
                "relative_path": "escape.txt",
            })
            assert escaped.status_code == 404

        entries = host_entries(client)  # Workspace creation also advances policy generation.
        folder_ref = next(row["ref"] for row in entries if row["name"] == "Folder")
        denied = client.post("/api/files-v1/workspace", json={
            "resource_ref": folder_ref,
            "purpose": "agent_workspace",
        })
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "workspace_denied"

        repository.create_binding(
            actor_subject_id="account-alice",
            binding_class="agent",
            subject_id="account-bob",
            location_id=location.id,
            capabilities=("read",),
            lifetime="always",
        )
        entries = host_entries(client)  # Fresh refs bind the new policy generation.
        folder_ref = next(row["ref"] for row in entries if row["name"] == "Folder")
        allowed = client.post("/api/files-v1/workspace", json={
            "resource_ref": folder_ref,
            "purpose": "agent_workspace",
        })
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["open_relative"] == ""
        assert "path" not in allowed.json()["workspace"]
        assert str(folder) not in allowed.text

        repository.create_binding(
            actor_subject_id="account-bob",
            binding_class="operation",
            subject_id="account-bob",
            location_id=location.id,
            workspace_id=workspace_id,
            capabilities=("read",),
            lifetime="workspace",
            resource_ref="opaque-resource",
            operation="read",
        )
        current = repository.get_workspace(workspace_id)
        archived = client.patch(f"/api/files-v1/workspaces/{workspace_id}", json={
            "archived": True,
            "expected_revision": current.revision,
        })
        assert archived.status_code == 200, archived.text
        assert archived.json()["workspace"]["archived"] is True
        assert archived.json()["resource"] is None
        active_ids = {
            row["workspace"]["id"] for row in client.get("/api/files-v1/workspaces").json()["entries"]
        }
        assert workspace_id not in active_ids
        archived_catalog = client.get("/api/files-v1/workspaces?include_archived=true").json()["entries"]
        archived_row = next(row for row in archived_catalog if row["workspace"]["id"] == workspace_id)
        assert archived_row["availability"] == "archived"
        operation = repository.list_bindings(
            subject_id="account-bob", binding_class="operation", include_inactive=True,
        )[0]
        assert operation.status == "revoked"

        owner["value"] = "alice"
        cross_owner = client.patch(f"/api/files-v1/workspaces/{workspace_id}", json={"name": "Admin steal"})
        assert cross_owner.status_code == 404
        cross_owner_reveal = client.post("/api/files-v1/workspace-resource", json={
            "workspace_id": workspace_id,
            "relative_path": "note.txt",
        })
        assert cross_owner_reveal.status_code == 404


def test_copal_workspace_root_hint_is_validated_and_direct_content_needs_no_header(tmp_path):
    app, owner, _sessions = _app(tmp_path)
    import asyncio

    created = asyncio.run(app.state.copal_bridge.call("create", {
        "owner": "alice",
        "workspace_id": "course-1",
        "name": "Course.md",
        "kind": "note",
        "content": "course body",
    }))
    with TestClient(app) as client:
        roots = client.get("/api/files-v1/roots?copal_workspace=course-1")
        assert roots.status_code == 200
        copal = next(row for row in roots.json()["entries"] if row["provider"] == "copal")
        folders = client.post("/api/files-v1/children", json={"parent_ref": copal["ref"]}).json()["entries"]
        documents = next(row for row in folders if row["name"] == "Documents")
        resource = client.post("/api/files-v1/children", json={"parent_ref": documents["ref"]}).json()["entries"][0]
        # The content URL carries only the ref. The route reconstructs a
        # default facade and must still resolve the sealed course workspace.
        content = client.get(f"/api/files-v1/content/{resource['ref']}")
        assert content.status_code == 200
        assert content.content == b"course body"
        action = client.post("/api/files-v1/action", json={
            "resource_ref": resource["ref"], "action": "open", "args": {},
        })
        assert action.status_code == 200
        assert action.json()["target"] == {"app": "copal_notes"}
        assert action.json()["exact"] is True
        exact = client.post("/api/files-v1/open-resource", json={
            "resource_ref": action.json()["resource"]["ref"],
        })
        assert exact.status_code == 200
        assert exact.json()["target"] == {"app": "copal_notes"}
        assert exact.json()["payload"]["text"] == "course body"
        assert exact.json()["payload"]["read_only"] is False
        assert exact.json()["payload"]["relations"] == []
        assert created["doc"]["id"] not in exact.text
        revealed = client.post("/api/files-v1/reveal", json={
            "resource_ref": exact.json()["resource"]["ref"],
        })
        assert revealed.status_code == 200, revealed.text
        assert revealed.json()["provider"] == "copal"
        assert [row["name"] for row in revealed.json()["ancestors"]] == ["Copal", "Documents"]
        assert revealed.json()["parent"]["name"] == "Documents"
        assert revealed.json()["resource"]["name"] == "Course.md"
        assert created["doc"]["id"] not in revealed.text
        assert "origin_id" not in revealed.text

        expired = issue_resource_ref(
            owner_subject_id="account-alice",
            provider="copal",
            origin_id=f"document:course-1:{created['doc']['id']}",
            kind="document",
            capabilities=("stat", "open", "preview", "download"),
            policy_generation=revealed.json()["resource"]["policy_generation"],
            workspace_id="course-1",
            ttl_seconds=1,
            now_unix_ms=1,
        )
        renewed = client.post("/api/files-v1/reissue", json={"resource_ref": expired.token})
        assert renewed.status_code == 200, renewed.text
        assert renewed.json()["resource"]["id"] == expired.stable_id
        assert renewed.json()["resource"]["policy_generation"] == revealed.json()["resource"]["policy_generation"]
        assert renewed.json()["resource"]["ref"] != expired.token
        assert created["doc"]["id"] not in renewed.text
        assert "origin_id" not in renewed.text

        owner["value"] = "bob"
        denied_renewal = client.post("/api/files-v1/reissue", json={"resource_ref": expired.token})
        assert denied_renewal.status_code == 404
        assert denied_renewal.json()["detail"]["code"] == "resource_unavailable"
        owner["value"] = "alice"
        invalid = client.get("/api/files-v1/roots?copal_workspace=../secret")
        assert invalid.status_code == 400


def test_policy_generation_change_stales_existing_refs(tmp_path):
    app, _owner, _sessions = _app(tmp_path)
    # Use the same path as the injected repository; a separate instance shares
    # its transactional generation.
    repository = FilePolicyRepository(tmp_path / "app.db")
    with TestClient(app) as client:
        root = client.get("/api/files-v1/roots").json()["entries"][0]
        repository.create_location(
            actor_subject_id="account-alice",
            path=str(tmp_path / "location"),
            kind="directory",
            capabilities=("read",),
        )
        stale = client.post("/api/files-v1/children", json={"parent_ref": root["ref"]})
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "resource_ref_stale"


def test_missing_immutable_identity_fails_without_username_fallback(tmp_path):
    app, owner, _sessions = _app(tmp_path)
    owner["value"] = "unknown"
    with TestClient(app) as client:
        response = client.get("/api/files-v1/roots")
    assert response.status_code == 503


def _resource(client, provider, *names):
    root = next(row for row in client.get("/api/files-v1/roots").json()["entries"] if row["provider"] == provider)
    current = root
    for name in names:
        page = client.post("/api/files-v1/children", json={"parent_ref": current["ref"]})
        assert page.status_code == 200, page.text
        current = next(row for row in page.json()["entries"] if row["name"] == name)
    return current


def test_content_get_head_range_and_stale_owner_are_opaque(tmp_path):
    app, owner, _sessions = _app(tmp_path)
    bridge = app.state.copal_bridge
    import asyncio
    asyncio.run(bridge.call("create", {
        "owner": "alice",
        "workspace_id": "default",
        "name": "Range.md",
        "kind": "note",
        "content": "0123456789",
    }))
    with TestClient(app) as client:
        resource = _resource(client, "copal", "Documents", "Range.md")
        url = f"/api/files-v1/content/{resource['ref']}"
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == b"0123456789"
        assert response.headers["content-disposition"].startswith('attachment; filename="Range.md"')
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
        preview = client.get(f"{url}?purpose=preview")
        assert preview.status_code == 200
        assert preview.headers["content-disposition"].startswith('inline; filename="Range.md"')
        assert preview.content == b"0123456789"
        head = client.head(url)
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-length"] == "10"
        partial = client.get(url, headers={"Range": "bytes=2-5"})
        assert partial.status_code == 206
        assert partial.content == b"2345"
        assert partial.headers["content-range"] == "bytes 2-5/10"
        invalid = client.get(url, headers={"Range": "bytes=40-50"})
        assert invalid.status_code == 416
        assert invalid.headers["content-range"] == "bytes */10"

        owner["value"] = "bob"
        denied_get = client.get(url)
        denied_head = client.head(url)
        assert denied_get.status_code == 404
        assert denied_head.status_code == 404
        assert "Range.md" not in denied_get.text


def test_active_document_download_is_forced_attachment_and_sanitized(tmp_path):
    app, _owner, sessions = _app(tmp_path)
    db = sessions()
    try:
        db.add(Document(
            id="active-doc",
            title='evil"; name.html',
            language="html",
            current_content="<script>parent.location='https://evil.invalid'</script>",
            owner="alice",
            is_active=True,
            archived=False,
        ))
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        resource = _resource(client, "library", "Documents", 'evil"; name.html')
        response = client.get(f"/api/files-v1/content/{resource['ref']}")
        assert response.status_code == 200
        assert not response.headers["content-type"].startswith("text/html")
        disposition = response.headers["content-disposition"]
        assert disposition.startswith('attachment; filename="evil name.html"')
        assert "filename*=UTF-8''evil%20name.html" in disposition
        assert "\r" not in disposition and "\n" not in disposition
        assert response.content.startswith(b"<script>")


def test_open_action_is_strict_opaque_and_owner_bound(tmp_path):
    app, owner, sessions = _app(tmp_path)
    db = sessions()
    try:
        db.add(Document(
            id="open-action-doc",
            title="Open me",
            language="markdown",
            current_content="# hello",
            owner="alice",
            is_active=True,
            archived=False,
        ))
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        resource = _resource(client, "library", "Documents", "Open me")
        response = client.post("/api/files-v1/action", json={
            "resource_ref": resource["ref"],
            "action": "open",
            "args": {},
        })
        assert response.status_code == 200
        payload = response.json()
        assert payload["action"] == "open"
        assert payload["target"] == {"app": "document_editor"}
        assert payload["exact"] is True
        assert payload["resource"]["id"] == resource["id"]
        assert payload["resource"]["ref"] != resource["ref"]
        assert "open-action-doc" not in response.text
        assert "/api/documents" not in response.text

        exact = client.post("/api/files-v1/open-resource", json={
            "resource_ref": payload["resource"]["ref"],
        })
        assert exact.status_code == 200
        exact_payload = exact.json()
        assert exact_payload["target"] == {"app": "document_editor"}
        assert exact_payload["payload"]["title"] == "Open me"
        assert exact_payload["payload"]["content"] == "# hello"
        assert exact_payload["payload"]["session_ref"] is None
        assert exact_payload["payload"]["read_only"] is True
        assert "open-action-doc" not in exact.text

        exact_extra = client.post("/api/files-v1/open-resource", json={
            "resource_ref": payload["resource"]["ref"],
            "provider_id": "open-action-doc",
        })
        assert exact_extra.status_code == 422

        extra = client.post("/api/files-v1/action", json={
            "resource_ref": resource["ref"],
            "action": "open",
            "args": {},
            "provider_id": "open-action-doc",
        })
        assert extra.status_code == 422
        unsupported = client.post("/api/files-v1/action", json={
            "resource_ref": resource["ref"],
            "action": "delete",
            "args": {},
        })
        assert unsupported.status_code == 422
        with_args = client.post("/api/files-v1/action", json={
            "resource_ref": resource["ref"],
            "action": "open",
            "args": {"id": "open-action-doc"},
        })
        assert with_args.status_code == 422

        owner["value"] = "bob"
        denied = client.post("/api/files-v1/action", json={
            "resource_ref": resource["ref"],
            "action": "open",
            "args": {},
        })
        assert denied.status_code == 404
        assert denied.json()["detail"]["code"] == "resource_unavailable"
        assert "Open me" not in denied.text
        denied_exact = client.post("/api/files-v1/open-resource", json={
            "resource_ref": payload["resource"]["ref"],
        })
        assert denied_exact.status_code == 404
        assert "Open me" not in denied_exact.text


def test_idempotent_managed_actions_are_strict_opaque_and_owner_bound(tmp_path):
    app, owner, sessions = _app(tmp_path)
    db = sessions()
    try:
        db.add_all([
            _files_image(
                id="favorite-action-image",
                filename="Favorite me.png",
                owner="alice",
                prompt="",
                is_active=True,
                favorite=False,
            ),
            Document(
                id="archive-action-document",
                title="Archive me",
                language="markdown",
                current_content="body",
                owner="alice",
                is_active=True,
                archived=False,
            ),
        ])
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        image = _resource(client, "gallery", "Photos", "Favorite me.png")
        document = _resource(client, "library", "Documents", "Archive me")
        favored = client.post("/api/files-v1/action", json={
            "resource_ref": image["ref"],
            "action": "favorite.set",
            "args": {"value": True},
        })
        assert favored.status_code == 200
        assert favored.json()["state"] == {"favorite": True}
        assert "favorite-action-image" not in favored.text

        archived = client.post("/api/files-v1/action", json={
            "resource_ref": document["ref"],
            "action": "archive.set",
            "args": {"value": True},
        })
        assert archived.status_code == 200
        assert archived.json()["state"] == {"archived": True}
        assert "restore" in archived.json()["resource"]["capabilities"]
        assert "archive-action-document" not in archived.text

        restored = client.post("/api/files-v1/action", json={
            "resource_ref": archived.json()["resource"]["ref"],
            "action": "archive.set",
            "args": {"value": False},
        })
        assert restored.status_code == 200
        assert restored.json()["state"] == {"archived": False}

        bad_args = client.post("/api/files-v1/action", json={
            "resource_ref": image["ref"],
            "action": "favorite.set",
            "args": {"value": "true"},
        })
        assert bad_args.status_code == 422
        extra_args = client.post("/api/files-v1/action", json={
            "resource_ref": image["ref"],
            "action": "favorite.set",
            "args": {"value": True, "provider_id": "favorite-action-image"},
        })
        assert extra_args.status_code == 422

        owner["value"] = "bob"
        denied = client.post("/api/files-v1/action", json={
            "resource_ref": favored.json()["resource"]["ref"],
            "action": "favorite.set",
            "args": {"value": False},
        })
        assert denied.status_code == 404
        assert "Favorite me" not in denied.text


def test_files_places_are_server_owned_revalidated_and_account_isolated(tmp_path):
    app, owner, sessions = _app(tmp_path)
    db = sessions()
    try:
        db.add(Document(
            id="place-document-origin",
            title="Pinned document",
            language="markdown",
            current_content="body",
            owner="alice",
            is_active=True,
            archived=False,
        ))
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        document = _resource(client, "library", "Documents", "Pinned document")
        saved = client.post("/api/files-v1/places", json={"resource_ref": document["ref"]})
        assert saved.status_code == 200
        place_id = saved.json()["resource"]["place_id"]
        assert place_id.startswith("place-")
        assert "place-document-origin" not in saved.text

        listed = client.get("/api/files-v1/places")
        assert listed.status_code == 200
        assert [row["name"] for row in listed.json()["entries"]] == ["Pinned document"]
        assert listed.json()["entries"][0]["ref"] != document["ref"]
        assert "place-document-origin" not in listed.text

        owner["value"] = "bob"
        assert client.get("/api/files-v1/places").json()["entries"] == []
        denied_remove = client.delete(f"/api/files-v1/places/{place_id}")
        assert denied_remove.status_code == 404

        owner["value"] = "alice"
        removed = client.delete(f"/api/files-v1/places/{place_id}")
        assert removed.status_code == 200
        assert removed.json()["removed"] is True
        assert client.get("/api/files-v1/places").json()["entries"] == []


def test_inflight_download_aborts_when_policy_generation_changes(tmp_path, monkeypatch):
    """A scoped reset bumps the policy generation; an in-flight download that
    resolved its ref before the reset must not keep streaming afterwards."""
    import asyncio

    import pytest

    app, _owner, _sessions = _app(tmp_path)
    bridge = app.state.copal_bridge
    asyncio.run(bridge.call("create", {
        "owner": "alice",
        "workspace_id": "default",
        "name": "Big.md",
        "kind": "note",
        "content": "x" * (300 * 1024),
    }))
    with TestClient(app) as client:
        resource = _resource(client, "copal", "Documents", "Big.md")
        url = f"/api/files-v1/content/{resource['ref']}"

        # Control: an unchanged generation streams the full body.
        control = client.get(url)
        assert control.status_code == 200
        assert len(control.content) == 300 * 1024

        repository = app.state.file_policy_repository
        real_generation = repository.generation
        calls = {"n": 0}

        def bumped():
            calls["n"] += 1
            # The first read is the request's own context; the transfer probe
            # observes the post-reset generation.
            if calls["n"] > 1:
                return real_generation() + 1
            return real_generation()

        monkeypatch.setattr(repository, "generation", bumped)
        with pytest.raises(Exception) as excinfo:
            client.get(url)
        assert calls["n"] >= 2
        assert "policy" in str(excinfo.value).lower() or "generation" in str(excinfo.value).lower()


def test_inflight_download_probe_failure_fails_closed(tmp_path, monkeypatch):
    """A store error during the mid-transfer probe aborts rather than streams."""
    import asyncio

    import pytest

    app, _owner, _sessions = _app(tmp_path)
    bridge = app.state.copal_bridge
    asyncio.run(bridge.call("create", {
        "owner": "alice",
        "workspace_id": "default",
        "name": "Guarded.md",
        "kind": "note",
        "content": "guarded content",
    }))
    with TestClient(app) as client:
        resource = _resource(client, "copal", "Documents", "Guarded.md")
        url = f"/api/files-v1/content/{resource['ref']}"
        repository = app.state.file_policy_repository
        real_generation = repository.generation
        calls = {"n": 0}

        def broken():
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("policy store locked")
            return real_generation()

        monkeypatch.setattr(repository, "generation", broken)
        with pytest.raises(Exception):
            client.get(url)
        assert calls["n"] >= 2


def test_transfer_and_stream_import_use_authorized_host_folder_and_durable_receipt(tmp_path):
    import json

    app, _owner, _sessions = _app(tmp_path)
    with TestClient(app) as client:
        roots = client.get("/api/files-v1/roots").json()["entries"]
        host_root = next(row for row in roots if row["provider"] == "host")
        home = next(row for row in client.post("/api/files-v1/children", json={"parent_ref": host_root["ref"]}).json()["entries"] if row["kind"] == "folder")
        entries = client.post("/api/files-v1/children", json={"parent_ref": home["ref"]}).json()["entries"]
        source = next(row for row in entries if row["name"] == "note.txt")
        folder = next(row for row in entries if row["name"] == "Folder")
        transfer = client.post("/api/files-v1/transfers", json={
            "operation_id": "host-transfer-1", "generation": client.get("/api/files-v1/roots").json()["policy_generation"],
            "kind": "move", "sources": [{"item_id": "i-1", "resource_ref": source["ref"]}],
            "destination_ref": folder["ref"], "collision": "fail",
        })
        assert transfer.status_code == 200
        assert transfer.json()["items"][0]["outcome"] == "committed"
        replay = client.get("/api/files-v1/operations/host-transfer-1")
        assert replay.status_code == 200
        assert replay.json() == transfer.json()
        imported = client.post(
            "/api/files-v1/imports",
            files={"file": ("upload.txt", b"hello", "text/plain")},
            data={"metadata": json.dumps({
                "operation_id": "host-import-1", "item_id": "i-1", "generation": replay.json()["generation"],
                "destination_ref": folder["ref"], "name": "upload.txt", "collision": "fail",
            })},
        )
        assert imported.status_code == 200
        assert imported.json()["items"][0]["outcome"] == "committed"


def test_s03_new_file_and_folder_are_scoped_idempotent_and_zero_byte(tmp_path):
    import json
    import uuid
    from src.openclank.files_service_client import FilesServiceError

    folder_name = f"S03-disposable-{uuid.uuid4().hex[:12]}"

    class DisposableHostClient(HostClient):
        async def request(self, operation, path, payload=None):
            if operation == "stat" and not Path(path).exists():
                raise FilesServiceError("missing disposable resource", code="denied")
            if operation == "mkdir":
                self.calls.append(("mkdir", path, dict(payload or {})))
                Path(path).mkdir()
                return {"data": {"path": path}}
            if operation == "create":
                self.calls.append(("create", path, dict(payload or {})))
                Path(path).write_bytes(bytes((payload or {}).get("content") or b""))
                return {"data": {"path": path, "fingerprint": {"kind": "hostFingerprint", "value": "s03-created"}}}
            return await super().request(operation, path, payload)

    app, owner, _sessions = _app(
        tmp_path,
        host_client_factory_override=lambda username, scope: DisposableHostClient(username, scope),
    )
    fixture_root = app.state.host_registry.add("alice", str(tmp_path), "recursive_directory", ["read", "write"])
    app.state.host_registry.assign_visibility("alice", "bob", fixture_root["id"], ["read", "write"])
    owner["value"] = "bob"
    with TestClient(app) as client:
        roots = client.get("/api/files-v1/roots").json()
        generation = roots["policy_generation"]
        host = next(row for row in roots["entries"] if row["provider"] == "host")
        home = next(row for row in client.post("/api/files-v1/children", json={"parent_ref": host["ref"]}).json()["entries"] if row["name"] == tmp_path.name)
        mkdir_body = {
            "parent_ref": home["ref"], "name": folder_name, "operation_id": "s03-mkdir-1",
            "generation": generation, "collision": "fail",
        }
        if home.get("revision"):
            mkdir_body["expected_revision"] = home["revision"]
        created_folder = client.post("/api/files-v1/create-directory", json=mkdir_body)
        assert created_folder.status_code == 200, created_folder.text
        folder = created_folder.json()["resource"]
        assert folder["kind"] == "folder"
        replay_folder = client.get("/api/files-v1/operations/s03-mkdir-1")
        assert replay_folder.status_code == 200
        assert replay_folder.json()["state"] == "complete"
        assert replay_folder.json()["items"][0]["resource_ref"] == folder["ref"]

        empty_import = client.post(
            "/api/files-v1/imports",
            files={"file": ("Résumé's.md", b"", "text/markdown")},
            data={"metadata": json.dumps({
                "operation_id": "s03-file-1", "item_id": "s03-file-item-1", "generation": generation,
                "destination_ref": folder["ref"], "name": "Résumé's.md", "relative_path": "Résumé's.md", "collision": "fail",
            })},
        )
        assert empty_import.status_code == 200, empty_import.text
        receipt = empty_import.json()
        assert receipt["items"][0]["outcome"] == "committed"
        assert receipt["items"][0]["resource_ref"]
        replay_file = client.get("/api/files-v1/operations/s03-file-1").json()
        assert replay_file["operation_id"] == receipt["operation_id"]
        assert replay_file["generation"] == receipt["generation"]
        assert replay_file["items"] == receipt["items"]
        assert any(call[0] == "create" and call[2].get("content") == b"" for host_client in app.state.host_clients for call in host_client.calls)

        owner["value"] = "alice"
        assert client.get("/api/files-v1/operations/s03-file-1").status_code == 404
        assert client.post("/api/files-v1/children", json={"parent_ref": folder["ref"]}).status_code == 404


def test_multipart_import_validation_returns_typed_client_errors(tmp_path):
    import json

    app, _owner, _sessions = _app(tmp_path)
    with TestClient(app) as client:
        roots = client.get("/api/files-v1/roots").json()
        generation = roots["policy_generation"]
        host_root = next(row for row in roots["entries"] if row["provider"] == "host")
        home = next(
            row for row in client.post(
                "/api/files-v1/children", json={"parent_ref": host_root["ref"]}
            ).json()["entries"] if row["kind"] == "folder"
        )
        folder = next(
            row for row in client.post(
                "/api/files-v1/children", json={"parent_ref": home["ref"]}
            ).json()["entries"] if row["name"] == "Folder"
        )

        cases = [
            ("malformed", "bad", 400, "invalid_resource_request"),
            ("missing", None, 409, "resource_ref_stale"),
            ("negative", -1, 400, "invalid_resource_request"),
            ("future", generation + 1, 409, "resource_ref_stale"),
            ("boolean", True, 400, "invalid_resource_request"),
            ("boolean-string", "true", 400, "invalid_resource_request"),
        ]
        for operation_id, value, expected_status, expected_code in cases:
            metadata = {
                "operation_id": f"import-{operation_id}",
                "item_id": "item",
                "destination_ref": folder["ref"],
                "name": "upload.txt",
                "collision": "fail",
            }
            if value is not None:
                metadata["generation"] = value
            response = client.post(
                "/api/files-v1/imports",
                files={"file": ("upload.txt", b"hello", "text/plain")},
                data={"metadata": json.dumps(metadata)},
            )
            assert response.status_code == expected_status, response.text
            assert response.json()["detail"]["code"] == expected_code

        for field, value in (("operation_id", True), ("item_id", 42), ("destination_ref", [folder["ref"]]), ("name", 7), ("collision", False)):
            metadata = {
                "operation_id": f"import-bad-{field}",
                "item_id": "item",
                "generation": generation,
                "destination_ref": folder["ref"],
                "name": "upload.txt",
                "collision": "fail",
            }
            metadata[field] = value
            response = client.post(
                "/api/files-v1/imports",
                files={"file": ("upload.txt", b"hello", "text/plain")},
                data={"metadata": json.dumps(metadata)},
            )
            assert response.status_code == 400, response.text
            assert response.json()["detail"]["code"] == "invalid_resource_request"


def _files_image(**values):
    """Build a Files-owned resource from legacy fixture metadata."""
    filename = values.pop("filename", values.pop("name", "image"))
    parent_id = values.pop("album_id", values.pop("parent_id", None))
    prompt = values.pop("prompt", None)
    model = values.pop("model", None)
    file_hash = values.pop("file_hash", values.pop("digest", None))
    size = values.pop("file_size", values.pop("size", 0))
    provenance = dict(values.pop("provenance", {}) or {})
    for key, value in (("prompt", prompt), ("model", model)):
        if value is not None:
            provenance[key] = value
    is_folder = not filename or ("name" in values and parent_id is None)
    return FilesImageResource(
        id=values.pop("id"), owner=values.pop("owner", "alice"),
        kind="folder" if is_folder else "image", parent_id=parent_id,
        display_name=filename, locator=values.pop("locator", filename),
        digest=file_hash, size=size, mime_type=values.pop("mime_type", None),
        favorite=values.pop("favorite", False), is_active=values.pop("is_active", True),
        provenance=provenance or None, **values,
    )

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import odysseus_files_routes as routes
from src.openclank.files_service_client import FilesServiceError
from src.openclank.files_service_client import FilesServiceError
from src.openclank.filesystem_registry import FilesystemRootRegistry


class StubClient:
    def __init__(self):
        self.calls = []
        self.scopes = []
        self.contents = b"hello"
        self.fingerprint = "fp-1"
        self.modified_unix_ms = 1
        self.errors = {}

    async def request(self, operation, path, payload=None):
        self.calls.append((operation, path, payload))
        if operation in self.errors:
            raise self.errors[operation]
        if operation == "stat":
            return {
                "protocol": {"major": 1, "minor": 0},
                "data": {
                    "path": path,
                    "kind": "file",
                    "size": len(self.contents),
                    "fingerprint": {"algorithm": "sha256", "value": self.fingerprint},
                    "modified_unix_ms": self.modified_unix_ms,
                },
            }
        if operation == "read_range":
            offset = int((payload or {}).get("offset") or 0)
            length = int((payload or {}).get("length") or len(self.contents))
            chunk = self.contents[offset:offset + length]
            next_offset = offset + len(chunk)
            eof = next_offset >= len(self.contents)
            return {
                "protocol": {"major": 1, "minor": 0},
                "data": {
                    "bytes": list(chunk),
                    "next_offset": None if eof else next_offset,
                    "eof": eof,
                    "fingerprint": {"algorithm": "sha256", "value": self.fingerprint},
                },
            }
        return {"protocol": {"major": 1, "minor": 0}, "data": {"ok": True}}


def _client(monkeypatch, *, admin=True, registry=None, owner="owner-1"):
    stub = StubClient()
    app = FastAPI()
    if registry is not None:
        monkeypatch.setattr(routes, "FilesystemRootRegistry", lambda: registry)
    app.include_router(routes.setup_odysseus_files_routes())
    monkeypatch.setattr(routes, "get_current_user", lambda request: owner)
    monkeypatch.setattr(routes, "owner_is_admin_or_single_user", lambda owner: admin)
    def _client_for(owner_id, app_scope=None):
        stub.scopes.append(app_scope)
        return stub
    monkeypatch.setattr(routes, "client_for_owner", _client_for)
    return TestClient(app), stub


def test_browse_and_read_use_rust_service_without_client_lane_fields(monkeypatch):
    client, stub = _client(monkeypatch)
    browse = client.get("/api/odysseus-files/browse", params={"path": "/workspace"})
    assert browse.status_code == 200
    assert stub.calls[0] == ("list_directory", "/workspace", {})

    read = client.get(
        "/api/odysseus-files/read",
        params={"path": "/workspace/main.rs", "offset": 4, "length": 12, "fingerprint": "true"},
    )
    assert read.status_code == 200
    assert stub.calls[1] == ("read_range", "/workspace/main.rs", {"offset": 4, "length": 12, "include_fingerprint": True})

    text = client.get("/api/odysseus-files/read-text", params={"path": "/workspace/main.rs"})
    assert text.status_code == 200
    assert stub.calls[2] == ("read_lines", "/workspace/main.rs", {})

    stat = client.get("/api/odysseus-files/stat", params={"path": "/workspace/main.rs", "fingerprint": "true"})
    assert stat.status_code == 200
    assert stub.calls[3] == ("stat", "/workspace/main.rs", {"include_fingerprint": True})

    write = client.post("/api/odysseus-files/write", json={"path": "/workspace/main.rs", "text": "fn main() {}", "expected_fingerprint": "sha256:old:12"})
    assert write.status_code == 200
    assert stub.calls[4] == ("replace", "/workspace/main.rs", {"text": "fn main() {}", "expected_fingerprint": {"algorithm": "sha256", "value": "sha256:old:12"}})

    edit = client.post("/api/odysseus-files/edit", json={"path": "/workspace/main.rs", "old": "main", "new": "start"})
    assert edit.status_code == 200
    assert stub.calls[5] == ("patch", "/workspace/main.rs", {"old": "main", "new": "start", "replace_all": False})

    copied = client.post("/api/odysseus-files/copy", json={"path": "/workspace/main.rs", "destination": "/workspace/copy.rs"})
    assert copied.status_code == 200
    assert stub.calls[6] == ("copy", "/workspace/main.rs", {"destination": "/workspace/copy.rs"})

    moved = client.post("/api/odysseus-files/move", json={"path": "/workspace/copy.rs", "to": "/workspace/moved.rs"})
    assert moved.status_code == 200
    assert stub.calls[7] == ("move", "/workspace/copy.rs", {"destination": "/workspace/moved.rs"})

    renamed = client.post("/api/odysseus-files/rename", json={"path": "/workspace/moved.rs", "destination": "/workspace/main.rs"})
    assert renamed.status_code == 200
    assert stub.calls[8] == ("rename", "/workspace/moved.rs", {"destination": "/workspace/main.rs"})

    created = client.post("/api/odysseus-files/create", json={"path": "/workspace/new.rs", "text": "fn main() {}"})
    assert created.status_code == 200
    assert stub.calls[9] == ("create", "/workspace/new.rs", {"text": "fn main() {}"})

    made = client.post("/api/odysseus-files/mkdir", json={"path": "/workspace/src"})
    assert made.status_code == 200
    assert stub.calls[10] == ("mkdir", "/workspace/src", {})

    trashed = client.post("/api/odysseus-files/trash", json={"path": "/workspace/new.rs"})
    assert trashed.status_code == 200
    assert stub.calls[11] == ("trash", "/workspace/new.rs", {})

    entry = {"id": "trash-1", "root_id": "root-1", "original_path": "/workspace/new.rs", "trashed_path": "/workspace/.odysseus-trash/trash-1"}
    restored = client.post("/api/odysseus-files/restore", json={"entry": entry})
    assert restored.status_code == 200
    assert stub.calls[12] == ("restore", "/workspace/new.rs", {"entry": entry})


def test_stale_directory_cursor_is_a_restartable_conflict(monkeypatch):
    client, stub = _client(monkeypatch)
    stub.errors["list_directory"] = FilesServiceError(
        "directory cursor is stale",
        code="stale_cursor",
    )

    response = client.get(
        "/api/odysseus-files/browse",
        params={"path": "/workspace", "cursor": json.dumps({"token": "old", "generation": 1, "position": 2})},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_cursor"


def test_text_preview_uses_dedicated_rust_operation_and_fixed_budget(monkeypatch):
    client, stub = _client(monkeypatch)

    response = client.get(
        "/api/odysseus-files/preview-text",
        params={"path": "/workspace/large.log", "max_bytes": 8 * 1024 * 1024},
    )

    assert response.status_code == 200
    assert stub.calls == [
        ("read_text_preview", "/workspace/large.log", {"max_bytes": 320_000}),
    ]


def test_text_preview_returns_typed_unsupported_media_response(monkeypatch):
    client, stub = _client(monkeypatch)

    async def unsupported(operation, path, payload=None):
        raise FilesServiceError("filesystem operation failed", code="invalid_path")

    stub.request = unsupported
    response = client.get(
        "/api/odysseus-files/preview-text",
        params={"path": "/workspace/archive.bin"},
    )

    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "unsupported_text_preview"


def test_download_streams_rust_authorized_bytes_to_viewing_device(monkeypatch):
    client, stub = _client(monkeypatch)
    response = client.get("/api/odysseus-files/download", params={"path": "/workspace/hello.txt"})
    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers["content-length"] == "5"
    assert "attachment" in response.headers["content-disposition"]
    assert [call[0] for call in stub.calls] == ["stat", "read_range", "stat"]
    assert stub.calls[1][2]["include_fingerprint"] is False

    legacy_inline = client.get("/api/odysseus-files/download", params={"path": "/workspace/hello.txt", "inline": "true"})
    assert legacy_inline.status_code == 200
    assert legacy_inline.headers["content-disposition"].startswith("attachment;")

    head = client.head("/api/odysseus-files/download", params={"path": "/workspace/hello.txt", "inline": "true"})
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == "5"
    assert head.headers["accept-ranges"] == "bytes"
    assert head.headers["content-disposition"].startswith("attachment;")

    quoted = client.get(
        "/api/odysseus-files/download",
        params={"path": '/workspace/weird"; name-雪.txt'},
    )
    disposition = quoted.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert 'filename="weird name-.txt"' in disposition
    assert "filename*=UTF-8''weird%3B%20name-%E9%9B%AA.txt" in disposition


def test_download_never_serves_script_capable_host_content_inline(monkeypatch):
    client, stub = _client(monkeypatch)

    for path in ("/workspace/attack.html", "/workspace/attack.svg", "/workspace/document.pdf"):
        response = client.get(
            "/api/odysseus-files/download",
            params={"path": path, "inline": "true"},
        )

        assert response.status_code == 200
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["content-type"].startswith("application/octet-stream")
        assert response.headers["x-content-type-options"] == "nosniff"


def test_download_hashes_once_and_never_rehashes_each_stream_page(monkeypatch):
    client, stub = _client(monkeypatch)
    stub.contents = b"a" * (2 * 1024 * 1024 + 9)

    response = client.get(
        "/api/odysseus-files/download",
        params={"path": "/workspace/large.bin"},
    )

    assert response.status_code == 200
    assert response.content == stub.contents
    stat_calls = [call for call in stub.calls if call[0] == "stat"]
    range_calls = [call for call in stub.calls if call[0] == "read_range"]
    assert stat_calls[0][2] == {"include_fingerprint": True}
    assert stat_calls[-1][2] == {"include_fingerprint": False}
    assert len(range_calls) == 3
    assert all(call[2]["include_fingerprint"] is False for call in range_calls)


def test_preview_handle_stream_is_opaque_session_bound_and_revocable(monkeypatch, tmp_path):
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    client, stub = _client(monkeypatch, registry=registry)
    client.cookies.set("odysseus_session", "session-a")
    stub.contents = b"\x89PNG\r\n\x1a\n" + b"preview-bytes"
    stub.fingerprint = "png-fingerprint"
    raw_path = "/workspace/private-image.png"

    minted = client.post("/api/odysseus-files/preview-handles", json={"path": raw_path, "kind": "image"})

    assert minted.status_code == 200
    handle = minted.json()
    assert len(handle["token"]) >= 32
    assert handle["url"] == f"/api/odysseus-files/preview/{handle['token']}"
    assert handle["kind"] == "image"
    assert handle["media_type"] == "image/png"
    assert handle["size"] == len(stub.contents)
    assert raw_path not in minted.text
    assert stub.calls[:2] == [
        ("stat", raw_path, {"include_fingerprint": True}),
        ("read_range", raw_path, {"offset": 0, "length": len(stub.contents), "include_fingerprint": False}),
    ]

    streamed = client.get(handle["url"])
    assert streamed.status_code == 200
    assert streamed.content == stub.contents
    assert streamed.headers["content-type"].startswith("image/png")
    assert streamed.headers["cache-control"] == "private, no-store"
    assert streamed.headers["x-content-type-options"] == "nosniff"
    assert raw_path not in str(streamed.headers)
    assert stub.calls[2:] == [
        ("stat", raw_path, {"include_fingerprint": False}),
        ("read_range", raw_path, {"offset": 0, "length": len(stub.contents), "include_fingerprint": False}),
        ("stat", raw_path, {"include_fingerprint": False}),
    ]

    ranged = client.get(handle["url"], headers={"Range": "bytes=2-7"})
    assert ranged.status_code == 206
    assert ranged.content == stub.contents[2:8]
    assert ranged.headers["content-range"] == f"bytes 2-7/{len(stub.contents)}"

    head = client.head(handle["url"])
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(stub.contents))

    # A second login to the same owner cannot consume or revoke the first
    # browser session's capability.
    client.cookies.set("odysseus_session", "session-b")
    stale_for_other_session = client.get(handle["url"])
    assert stale_for_other_session.status_code == 410
    assert stale_for_other_session.json()["detail"]["code"] == "preview_handle_stale"
    assert client.delete(f"/api/odysseus-files/preview-handles/{handle['token']}").status_code == 200
    client.cookies.set("odysseus_session", "session-a")
    assert client.get(handle["url"]).status_code == 200

    revoked = client.delete(f"/api/odysseus-files/preview-handles/{handle['token']}")
    assert revoked.json() == {"ok": True}
    stale = client.get(handle["url"])
    assert stale.status_code == 410
    assert stale.json()["detail"]["code"] == "preview_handle_stale"
    assert raw_path not in stale.text


def test_preview_handle_rechecks_policy_and_file_identity(monkeypatch, tmp_path):
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    client, stub = _client(monkeypatch, registry=registry)
    client.cookies.set("odysseus_session", "session-a")
    stub.contents = b"ID3" + b"\x04\x00\x00" + b"audio"
    stub.fingerprint = "audio-fingerprint"

    first = client.post("/api/odysseus-files/preview-handles", json={"path": "/private/track.mp3", "kind": "audio"}).json()
    stub.fingerprint = "changed-fingerprint"
    stub.modified_unix_ms = 2
    changed = client.get(first["url"])
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "preview_file_changed"
    assert "/private/track.mp3" not in changed.text

    stub.fingerprint = "audio-fingerprint-2"
    second = client.post("/api/odysseus-files/preview-handles", json={"path": "/private/track.mp3", "kind": "audio"}).json()
    visible = tmp_path / "visible"
    visible.mkdir()
    registry.add("admin", str(visible), "recursive_directory", ["read"])
    policy_changed = client.get(second["url"])
    assert policy_changed.status_code == 409
    assert policy_changed.json()["detail"]["code"] == "preview_policy_changed"
    assert "/private/track.mp3" not in policy_changed.text


def test_preview_magic_rejects_script_capable_and_unknown_content(monkeypatch, tmp_path):
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    client, stub = _client(monkeypatch, registry=registry)
    client.cookies.set("odysseus_session", "session-a")
    raw_path = "/private/payload.svg"
    stub.contents = b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'
    stub.fingerprint = "svg-fingerprint"

    rejected = client.post("/api/odysseus-files/preview-handles", json={"path": raw_path, "kind": "image"})

    assert rejected.status_code == 415
    assert rejected.json()["detail"]["code"] == "unsupported_preview_type"
    assert raw_path not in rejected.text

    # A closure-local table miss (including another non-sticky worker) fails
    # closed with a stable typed response and cannot solicit a raw path.
    unknown = client.get("/api/odysseus-files/preview/" + "x" * 43)
    assert unknown.status_code == 410
    assert unknown.json()["detail"]["code"] == "preview_handle_stale"


def test_preview_store_is_ttl_and_per_owner_bounded(monkeypatch):
    store = routes._PreviewHandleStore()
    monkeypatch.setattr(routes, "_PREVIEW_HANDLES_PER_OWNER", 2)
    first, _ = store.mint("owner", {"session": "session", "path": "/one"})
    second, _ = store.mint("owner", {"session": "session", "path": "/two"})
    third, _ = store.mint("owner", {"session": "session", "path": "/three"})
    assert store.get(first, "owner", "session") is None
    assert store.get(second, "owner", "session")["path"] == "/two"
    assert store.get(third, "owner", "session")["path"] == "/three"
    assert store.get(second, "another-owner", "session") is None

    monkeypatch.setattr(routes, "_PREVIEW_HANDLE_TTL_SECONDS", 0)
    expired, _ = store.mint("owner", {"session": "session", "path": "/expired"})
    assert store.get(expired, "owner", "session") is None


def test_browse_carries_display_sort_without_accepting_client_authority(monkeypatch):
    client, stub = _client(monkeypatch)
    sort = {"key": "modified", "direction": "desc", "directories_first": True, "collation": "open-clank-v1"}
    response = client.get("/api/odysseus-files/browse", params={"path": "/workspace", "sort": json.dumps(sort)})
    assert response.status_code == 200
    assert stub.calls == [("list_directory", "/workspace", {"sort": sort})]


def test_browse_rejects_bad_cursor_and_non_admin_scope_is_not_hostwide(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch)
    bad = client.get("/api/odysseus-files/browse", params={"cursor": "not-json"})
    assert bad.status_code == 400

    folder = tmp_path / "workspace"
    folder.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    root = registry.add("admin", str(folder), "recursive_directory", ["read"])
    registry.assign_visibility("admin", "user-1", root["id"], ["read"])
    scoped, scoped_stub = _client(monkeypatch, admin=False, registry=registry, owner="user-1")
    assert scoped.get("/api/odysseus-files/browse", params={"path": str(folder)}).status_code == 200
    assert scoped_stub.scopes[-1]["host"] is False
    assert scoped.get("/api/odysseus-files/app-scope").json()["scope"] == {
        "host": False,
        "visible_root_ids": [root["id"]],
        "capabilities": ["read"],
        "root_capabilities": {root["id"]: ["read"]},
        "generation": registry.app_scope("user-1", is_admin=False)["generation"],
        "active_folder": None,
    }

    empty, _ = _client(monkeypatch, admin=False, registry=registry, owner="unassigned")
    assert empty.get("/api/odysseus-files/app-scope").json()["scope"]["host"] is False
    assert empty.get("/api/odysseus-files/app-scope").json()["scope"]["visible_root_ids"] == []


def test_navigation_roots_admin_projects_host_namespace_not_agent_roots(monkeypatch, tmp_path):
    agent_folder = tmp_path / "agent-only"
    agent_folder.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    agent_root = registry.add("admin", str(agent_folder), "recursive_directory", ["read"])
    client, _ = _client(monkeypatch, admin=True, registry=registry, owner="admin")

    response = client.get("/api/odysseus-files/navigation-roots")

    assert response.status_code == 200
    data = response.json()
    home = routes.os.path.realpath(routes.os.path.expanduser("~"))
    assert data["version"] == 1
    assert data["generation"] == registry.generation()
    assert data["default_path"] == home
    assert data["favorites"] == [
        {
            "id": "home",
            "name": "Home",
            "path": home,
            "kind": "recursive_directory",
            "pinned": True,
        }
    ]
    assert data["roots"]
    assert all(root["id"].startswith("host:") for root in data["roots"])
    assert all(root["kind"] == "recursive_directory" for root in data["roots"])
    assert all(root["capabilities"] == ["read", "write"] for root in data["roots"])
    assert agent_root["id"] not in {root["id"] for root in data["roots"]}
    if routes.os.name != "nt":
        assert data["roots"] == [
            {
                "id": "host:/",
                "name": "/",
                "path": routes.os.path.realpath(routes.os.path.sep),
                "kind": "recursive_directory",
                "capabilities": ["read", "write"],
            }
        ]


def test_navigation_roots_non_admin_projects_only_effectively_readable_assignments(monkeypatch, tmp_path):
    readable_folder = tmp_path / "a-readable-folder"
    write_only_folder = tmp_path / "b-write-only-folder"
    disabled_folder = tmp_path / "c-disabled-folder"
    other_users_folder = tmp_path / "d-other-user-folder"
    for folder in (readable_folder, write_only_folder, disabled_folder, other_users_folder):
        folder.mkdir()
    readable_file = tmp_path / "z-readable-file.txt"
    readable_file.write_text("visible", encoding="utf-8")

    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    readable_root = registry.add("admin", str(readable_folder), "recursive_directory", ["read", "write"])
    readable_file_root = registry.add("admin", str(readable_file), "exact_file", ["read"])
    write_only_root = registry.add("admin", str(write_only_folder), "recursive_directory", ["read", "write"])
    disabled_root = registry.add("admin", str(disabled_folder), "recursive_directory", ["read"])
    other_users_root = registry.add("admin", str(other_users_folder), "recursive_directory", ["read"])
    registry.assign_visibility("admin", "user-1", readable_root["id"], ["read"])
    registry.assign_visibility("admin", "user-1", readable_file_root["id"], ["read"])
    registry.assign_visibility("admin", "user-1", write_only_root["id"], ["write"])
    registry.assign_visibility("admin", "user-1", disabled_root["id"], ["read"])
    registry.assign_visibility("admin", "user-2", other_users_root["id"], ["read"])
    registry.update("admin", disabled_root["id"], enabled=False)
    client, _ = _client(monkeypatch, admin=False, registry=registry, owner="user-1")

    response = client.get("/api/odysseus-files/navigation-roots")

    assert response.status_code == 200
    data = response.json()
    assert data["version"] == 1
    assert data["generation"] == registry.app_scope("user-1", is_admin=False)["generation"]
    assert data["default_path"] == readable_root["canonical_path"]
    assert data["roots"] == [
        {
            "id": readable_root["id"],
            "name": readable_folder.name,
            "path": readable_root["canonical_path"],
            "kind": "recursive_directory",
            "capabilities": ["read"],
        },
        {
            "id": readable_file_root["id"],
            "name": readable_file.name,
            "path": readable_file_root["canonical_path"],
            "kind": "exact_file",
            "capabilities": ["read"],
        },
    ]
    assert data["favorites"] == [
        {
            "id": "assigned-start",
            "name": readable_folder.name,
            "path": readable_root["canonical_path"],
            "kind": "recursive_directory",
            "pinned": True,
        }
    ]
    # A write-only assignment must not disclose even its path/name through the
    # user-directed navigation projection. Disabled and cross-user roots follow
    # the same existence-blind boundary.
    body = response.text
    for hidden in (write_only_folder, disabled_folder, other_users_folder):
        assert str(hidden) not in body
        assert hidden.name not in body


def test_root_permissions_are_server_persisted_and_owner_scoped(monkeypatch, tmp_path):
    from src.openclank.filesystem_registry import FilesystemRootRegistry

    folder = tmp_path / "workspace"
    folder.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    client, _ = _client(monkeypatch, registry=registry)
    added = client.post(
        "/api/odysseus-files/roots",
        json={"path": str(folder), "kind": "recursive_directory", "capabilities": ["read", "write"]},
    )
    assert added.status_code == 200
    root_id = added.json()["root"]["id"]
    listed = client.get("/api/odysseus-files/roots")
    assert listed.status_code == 200
    assert listed.json()["roots"][0]["id"] == root_id
    updated = client.patch(f"/api/odysseus-files/roots/{root_id}", json={"enabled": False})
    assert updated.json()["root"]["enabled"] is False
    removed = client.delete(f"/api/odysseus-files/roots/{root_id}")
    assert removed.json()["ok"] is True


def test_visibility_settings_are_admin_managed_and_subject_filtered(monkeypatch, tmp_path):
    from src.openclank.filesystem_registry import FilesystemRootRegistry

    folder = tmp_path / "workspace"
    folder.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    root = registry.add("admin", str(folder), "recursive_directory", ["read", "write"])
    admin, _ = _client(monkeypatch, registry=registry, owner="admin")
    created = admin.post(
        "/api/odysseus-files/visibility",
        json={"subject_id": "user-1", "root_id": root["id"], "capabilities": ["read"]},
    )
    assert created.status_code == 200
    assignment_id = created.json()["assignment"]["id"]
    assert admin.get("/api/odysseus-files/visibility").json()["assignments"][0]["id"] == assignment_id

    user, _ = _client(monkeypatch, admin=False, registry=registry, owner="user-1")
    visible = user.get("/api/odysseus-files/visibility")
    assert visible.status_code == 200
    assert [item["id"] for item in visible.json()["assignments"]] == [assignment_id]
    assert user.patch(f"/api/odysseus-files/visibility/{assignment_id}", json={"enabled": False}).status_code == 403
    assert user.delete(f"/api/odysseus-files/visibility/{assignment_id}").status_code == 403


def test_non_admin_agent_roots_can_only_be_added_inside_visible_root(monkeypatch, tmp_path):
    visible = tmp_path / "visible"
    nested = visible / "nested"
    outside = tmp_path / "outside"
    nested.mkdir(parents=True)
    outside.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    app_root = registry.add("admin", str(visible), "recursive_directory", ["read"])
    registry.assign_visibility("admin", "user-1", app_root["id"], ["read"])
    user, _ = _client(monkeypatch, admin=False, registry=registry, owner="user-1")
    inside = user.post(
        "/api/odysseus-files/roots",
        json={"path": str(nested), "kind": "recursive_directory", "capabilities": ["read"]},
    )
    assert inside.status_code == 200
    outside_response = user.post(
        "/api/odysseus-files/roots",
        json={"path": str(outside), "kind": "recursive_directory", "capabilities": ["read"]},
    )
    assert outside_response.status_code == 403

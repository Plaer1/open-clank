from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Base,
    ChatMessage,
    Document,
    EditorDraft,
    FilesImageResource,
    PublishedFile,
    Session as DbSession,
)
from src.openclank.copal_loose import LooseCopalBridge
from src.openclank.copal_errors import CopalBridgeError
from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContent, ProviderContext, ProviderResource
from src.openclank.resource_refs import issue_resource_ref
from src.openclank.files_managed_providers import (
    CopalFilesProvider,
    FilesImagesProvider,
    LibraryFilesProvider,
)
from src.openclank.files_host_provider import HostFilesProvider, _origin
from src.openclank.chat_lifecycle import ChatLifecycleService
from src.published_files import PublishedFileService


def _context(owner="alice", generation=1, *, workspace_id="default"):
    return ProviderContext(
        owner_subject_id=f"account-{owner}",
        owner_username=owner,
        policy_generation=generation,
        workspace_id=workspace_id,
    )


@pytest.mark.asyncio
async def test_copal_target_materializes_authorized_host_content_as_scoped_asset():
    class Bridge:
        def __init__(self): self.calls = []
        async def call(self, operation, args):
            self.calls.append((operation, args))
            if operation == "metadata_get":
                return {"id": "target", "name": "Target.md", "kind": "note", "head": "head-1", "size": 6, "updatedAt": 1}
            if operation == "put_asset_scoped":
                return {"doc": {"id": "asset-1", "name": args["name"], "kind": "asset", "size": 5, "head": "asset-head"}}
            raise AssertionError(operation)

    class Host:
        async def stat(self, context, *, origin_id):
            return ProviderResource(origin_id, "source.txt", "file", ("stat", "read", "download"), revision={"kind": "hostFingerprint", "value": "source-1"}, mime_type="text/plain", size=5)
        async def content(self, context, *, origin_id):
            async def stream(start, length):
                assert start == 0
                assert length > 5
                yield b"hello"
            return ProviderContent(origin_id=origin_id, filename="source.txt", media_type="text/plain", stream=stream, size=5)

    bridge = Bridge()
    provider = CopalFilesProvider(bridge)
    result = await provider.prepare_attachment(
        _context(), operation_id="attach-host", source={"resource_ref": "source"},
        target={"kind": "copal_document", "resource_ref": "target-ref"}, mode="embed",
        source_provider=Host(), source_origin_id="host:source", source_entry=await Host().stat(_context(), origin_id="host:source"),
        target_origin_id="document:default:target",
    )
    assert result["asset"]["resource_key"]["workspace_id"] == "default"
    assert result["source_revision"]["value"] == "source-1"
    assert result["insertion"]["media_kind"] == "text/plain"
    assert [call[0] for call in bridge.calls].count("put_asset_scoped") == 1


@pytest.mark.asyncio
async def test_copal_target_materializes_copal_content_without_document_mutation():
    class Bridge:
        def __init__(self): self.calls = []
        async def call(self, operation, args):
            self.calls.append((operation, args))
            if operation == "metadata_get":
                return {"id": args["id"], "name": f"{args['id']}.md", "kind": "note", "head": "head-1", "size": 5, "updatedAt": 1}
            if operation == "get":
                return {"id": args["id"], "name": "source.md", "kind": "note", "text": "hello"}
            if operation == "put_asset_scoped":
                return {"doc": {"id": "asset-1", "name": args["name"], "kind": "asset", "size": 5, "head": "asset-head"}}
            raise AssertionError(operation)

    bridge = Bridge()
    provider = CopalFilesProvider(bridge)
    context = _context()
    source_entry = await provider.stat(context, origin_id="document:default:source")
    result = await provider.prepare_attachment(
        context, operation_id="attach-copal", source={"resource_ref": "source"},
        target={"kind": "copal_document", "resource_ref": "target-ref"}, mode="link",
        source_provider=provider, source_origin_id="document:default:source", source_entry=source_entry,
        target_origin_id="document:default:target",
    )
    assert result["source_revision"]["value"] == "head-1"
    assert result["asset"]["resource_key"]["resource_id"] == "asset-1"
    assert all(operation not in {"write", "rename", "create"} for operation, _args in bridge.calls)


@pytest.mark.asyncio
async def test_facade_uses_production_host_and_copal_providers_for_attachment():
    class Registry:
        def app_scope(self, username, *, is_admin=False): return {"host": True, "generation": 1}
        def visibility_for_subject(self, username): return []

    class HostClient:
        async def request(self, operation, path, payload=None):
            if operation == "stat":
                return {"data": {"path": path, "kind": "File", "size": 5, "modified_unix_ms": 1, "fingerprint": {"algorithm": "sha256", "value": "host-source"}}}
            raise AssertionError(operation)
        async def open_handle(self, path): return {"handle": {"id": "h"}, "size": 5, "modified_unix_ms": 1, "object_tag": "host-source"}
        async def stream_read_handle(self, handle, *, offset, length): yield b"hello"

    class Bridge:
        def __init__(self): self.calls = []
        async def call(self, operation, args):
            self.calls.append(operation)
            if operation == "metadata_get": return {"id": args["id"], "name": "Target.md", "kind": "note", "head": "head-1", "size": 0, "updatedAt": 1}
            if operation == "put_asset_scoped": return {"doc": {"id": "asset-1", "name": args["name"], "kind": "asset", "size": 5, "head": "asset-head"}}
            raise AssertionError(operation)

    context = _context()
    host = HostFilesProvider(
        registry=Registry(), client_factory=lambda username, **kwargs: HostClient(),
    )
    copal = CopalFilesProvider(Bridge())
    facade = FilesFacade([host, copal])
    source = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="host", origin_id=_origin("/authorized/source.txt"), kind="file", capabilities=("stat", "download"), policy_generation=1)
    target = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="copal", origin_id="document:default:target", kind="document", capabilities=("stat", "write"), policy_generation=1)
    result = await facade.prepare_attachment(
        context, operation_id="production-attach", generation=1,
        source={"resource_ref": source.token}, target={"kind": "copal_document", "resource_ref": target.token}, mode="embed",
    )
    assert result["operation_id"] == "production-attach"
    assert result["asset"]["resource_key"]["provider"] == "copal"


@pytest.mark.asyncio
async def test_loose_copal_metadata_page_never_reads_document_bodies(tmp_path, monkeypatch):
    bridge = LooseCopalBridge(tmp_path / "copal")
    await bridge.start()
    scope = {"owner": "alice", "workspace_id": "default"}
    created = await bridge.call("create", {**scope, "name": "Alpha.md", "kind": "note", "content": "SECRET BODY"})
    await bridge.call("create", {**scope, "name": ".copal/planning.json", "kind": "planning", "content": "SECRET SYSTEM"})

    original = bridge._record_doc

    def reject_body(vault, record, *, include_body):
        assert include_body is False
        return original(vault, record, include_body=include_body)

    monkeypatch.setattr(bridge, "_record_doc", reject_body)
    page = await bridge.call("metadata_page", {**scope, "state": "active", "hidden": "exclude", "corpus": "all"})
    assert [row["name"] for row in page["docs"]] == ["Alpha.md"]
    assert all("text" not in row for row in page["docs"])
    assert created["doc"]["id"] == page["docs"][0]["id"]


@pytest.mark.asyncio
async def test_copal_provider_exposes_logical_folders_and_opaque_pages(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "copal")
    await bridge.start()
    await bridge.call("create", {"owner": "alice", "workspace_id": "default", "name": "Note.md", "kind": "note", "content": "body"})
    facade = FilesFacade([CopalFilesProvider(bridge)])
    roots = await facade.roots(_context())
    root = roots["entries"][0]
    folders = await facade.children(_context(), parent_ref=root["ref"])
    documents = next(row for row in folders["entries"] if row["name"] == "Documents")
    page = await facade.children(_context(), parent_ref=documents["ref"])
    assert [row["name"] for row in page["entries"]] == ["Note.md"]
    assert page["entries"][0]["preview_kind"] == "text"
    assert "document:" not in page["entries"][0]["ref"]
    content = await facade.content(_context(), resource_ref=page["entries"][0]["ref"])
    assert content.data == b"body"
    assert content.media_type.startswith("text/markdown")


@pytest.mark.asyncio
async def test_copal_files_actions_keep_opaque_identity_and_return_receipts(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "copal")
    await bridge.start()
    created = await bridge.call("create", {
        "owner": "alice", "workspace_id": "default", "name": "Lifecycle.md",
        "kind": "note", "content": "dirty draft reference",
    })
    facade = FilesFacade([CopalFilesProvider(bridge)])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    folders = await facade.children(context, parent_ref=root["ref"])
    documents = next(row for row in folders["entries"] if row["name"] == "Documents")
    note = (await facade.children(context, parent_ref=documents["ref"]))["entries"][0]
    assert {"rename", "move", "trash"}.issubset(note["capabilities"])
    renamed = await facade.action(context, resource_ref=note["ref"], action="rename", args={"name": "Renamed.md"}, action_id="files-lifecycle-rename")
    assert renamed["resource"]["name"] == "Renamed.md"
    assert renamed["history"]["action_id"] == "files-lifecycle-rename"
    assert renamed["resource"]["ref"] != note["ref"]
    trashed = await facade.action(context, resource_ref=renamed["resource"]["ref"], action="trash", args={}, action_id="files-lifecycle-trash")
    assert trashed["resource"]["name"] == "Renamed.md"
    assert "restore" in trashed["resource"]["capabilities"]
    restored = await facade.action(context, resource_ref=trashed["resource"]["ref"], action="restore", args={}, action_id="files-lifecycle-restore")
    assert restored["resource"]["name"] == "Renamed.md"
    assert restored["history"]["action_id"] == "files-lifecycle-restore"
    assert created["doc"]["id"] not in renamed["resource"]["ref"]


@pytest.mark.asyncio
async def test_copal_files_facade_uses_files_history_for_lifecycle_receipts(tmp_path):
    """The Files facade must read back the persisted version after every action.

    This runs without the optional history worker: Copal's append-only Files
    operation log remains the durable receipt authority for Files actions.
    """
    bridge = LooseCopalBridge(tmp_path / "copal-files")
    await bridge.start()
    scope = {"owner": "alice", "workspace_id": "default"}
    created = await bridge.call("create", {**scope, "name": "Files-lifecycle.md", "kind": "note", "content": "body"})
    facade = FilesFacade([CopalFilesProvider(bridge)])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    folders = await facade.children(context, parent_ref=root["ref"])
    documents = next(row for row in folders["entries"] if row["name"] == "Documents")
    row = next(row for row in (await facade.children(context, parent_ref=documents["ref"]))["entries"] if row["name"] == "Files-lifecycle.md")
    for action, args in (("rename", {"name": "Files-moved.md"}), ("move", {"name": "Files-moved-again.md"})):
        result = await facade.action(context, resource_ref=row["ref"], action=action, args=args, action_id=f"files-{action}")
        receipt = result["history"]["receipt"]
        assert result["history"]["status"] == "complete"
        assert result["history"]["durable"] is True
        assert receipt["action_id"] == f"files-{action}"
        assert receipt["revision"]
        assert created["doc"]["id"] not in json.dumps(result)
        row = result["resource"]
    trashed = await facade.action(context, resource_ref=row["ref"], action="trash", args={}, action_id="files-trash")
    assert "restore" in trashed["resource"]["capabilities"]
    assert trashed["history"]["receipt"]["revision"]
    restored = await facade.action(context, resource_ref=trashed["resource"]["ref"], action="restore", args={}, action_id="files-restore")
    assert restored["resource"]["name"] == "Files-moved-again.md"
    assert restored["history"]["receipt"]["revision"]
    await bridge.stop()


@pytest.mark.asyncio
async def test_copal_files_action_uses_account_scoped_history_binding(tmp_path, monkeypatch):
    bridge = LooseCopalBridge(tmp_path / "copal")
    await bridge.start()
    created = await bridge.call("create", {"owner": "alice", "workspace_id": "default", "name": "Bound.md", "kind": "note", "content": "body"})
    calls = []

    class FakeHistoryClient:
        def __init__(self, socket, *, actor_id, account_id, token):
            calls.append(("client", socket, actor_id, account_id, token))
        def prepare(self, envelope, *, content, fingerprint):
            calls.append(("prepare", envelope["action_id"], envelope["actor_account_id"], content is not None))
        def record_live(self, action_id, receipt):
            calls.append(("live", action_id, receipt["status"]))
        def complete(self, action_id, *, content, fingerprint):
            calls.append(("complete", action_id))
            return {"action_id": action_id, "status": "complete"}

    monkeypatch.setattr("src.openclank.files_managed_providers.history_binding_for", lambda owner, lane: {
        "socket": "history.sock", "token": "account-token", "actor_id": "alice", "account_id": "account-alice",
    })
    monkeypatch.setattr("src.openclank.files_managed_providers.HistoryClient", FakeHistoryClient)
    facade = FilesFacade([CopalFilesProvider(bridge)])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    documents = next(row for row in (await facade.children(context, parent_ref=root["ref"]))["entries"] if row["name"] == "Documents")
    note = (await facade.children(context, parent_ref=documents["ref"]))["entries"][0]
    result = await facade.action(context, resource_ref=note["ref"], action="rename", args={"name": "Bound-renamed.md"}, action_id="bound-rename")
    assert result["history"]["status"] == "complete"
    assert result["history"]["receipt"]["action_id"] == "bound-rename"
    assert calls == [
        ("client", "history.sock", "alice", "account-alice", "account-token"),
        ("prepare", "bound-rename", "account-alice", True),
        ("live", "bound-rename", "Committed"),
        ("complete", "bound-rename"),
    ]


@pytest.mark.asyncio
async def test_copal_files_implicit_actions_get_fresh_ids_on_rename_back(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "copal")
    await bridge.start()
    created = await bridge.call("create", {"owner": "alice", "workspace_id": "default", "name": "Repeat.md", "kind": "note", "content": "body"})
    facade = FilesFacade([CopalFilesProvider(bridge)])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    folders = await facade.children(context, parent_ref=root["ref"])
    documents = next(row for row in folders["entries"] if row["name"] == "Documents")
    row = next(row for row in (await facade.children(context, parent_ref=documents["ref"]))["entries"] if row["name"] == "Repeat.md")
    away = await facade.action(context, resource_ref=row["ref"], action="rename", args={"name": "Repeat-away.md"})
    back = await facade.action(context, resource_ref=away["resource"]["ref"], action="rename", args={"name": "Repeat.md"})
    assert away["history"]["action_id"] != back["history"]["action_id"]
    assert away["history"]["status"] == back["history"]["status"] == "complete"
    assert created["doc"]["id"] not in json.dumps(back)


@pytest.mark.asyncio
async def test_copal_files_history_aborts_prepared_action_when_bridge_fails(monkeypatch):
    calls = []

    class FailingBridge:
        async def call(self, operation, args):
            calls.append((operation, args))
            if operation == "get":
                return {"id": "DOC", "name": "Before.md", "head": "head-before"}
            if operation == "rename":
                raise CopalBridgeError("bridge unavailable")
            raise AssertionError(operation)

    class History:
        def __init__(self, *args, **kwargs): pass
        def prepare(self, envelope, *, content, fingerprint): calls.append(("prepare", envelope))
        def record_live(self, action_id, receipt): calls.append(("live", receipt["status"]))
        def abort(self, action_id): calls.append(("abort", action_id))

    monkeypatch.setattr("src.openclank.files_managed_providers.history_binding_for", lambda owner, lane: {
        "socket": "history.sock", "token": "account-token", "actor_id": "alice", "account_id": "account-alice",
    })
    monkeypatch.setattr("src.openclank.files_managed_providers.HistoryClient", History)
    provider = CopalFilesProvider(FailingBridge())
    with pytest.raises(CopalBridgeError, match="bridge unavailable"):
        await provider._action_with_history(
            _context(), operation="rename", args={"owner": "alice", "workspace_id": "default", "id": "DOC", "name": "After.md"}, action_id="failed-files-action"
        )
    assert [entry[0] for entry in calls] == ["get", "prepare", "rename", "live", "abort"]


@pytest.mark.asyncio
async def test_copal_files_configured_history_prepare_failure_is_visible_and_never_falls_back(monkeypatch):
    calls = []

    class Bridge:
        async def call(self, operation, args):
            calls.append(operation)
            if operation == "get": return {"id": "DOC", "name": "Before.md", "head": "head-before"}
            if operation == "rename": return {"doc": {"id": "DOC", "name": "After.md", "head": "head-after"}}
            if operation == "history": raise AssertionError("configured History failure must not use bridge fallback")
            raise AssertionError(operation)

    class BrokenHistory:
        def __init__(self, *args, **kwargs): pass
        def prepare(self, envelope, *, content, fingerprint): raise RuntimeError("history worker unavailable")

    monkeypatch.setattr("src.openclank.files_managed_providers.history_binding_for", lambda owner, lane: {
        "socket": "history.sock", "token": "account-token", "actor_id": "alice", "account_id": "account-alice",
    })
    monkeypatch.setattr("src.openclank.files_managed_providers.HistoryClient", BrokenHistory)
    provider = CopalFilesProvider(Bridge())
    result, history = await provider._action_with_history(
        _context(), operation="rename", args={"owner": "alice", "workspace_id": "default", "id": "DOC", "name": "After.md"}, action_id="prepare-failure",
    )
    assert result["doc"]["name"] == "After.md"
    assert history["status"] == "failed"
    assert history["durable"] is False
    assert calls == ["get", "rename"]


@pytest.mark.asyncio
async def test_copal_advertised_sort_modes_are_provider_ordered_and_cursor_bound(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "copal")
    await bridge.start()
    scope = {"owner": "alice", "workspace_id": "default"}
    await bridge.call("create", {**scope, "name": "Zeta.md", "kind": "wiki", "content": "z"})
    await bridge.call("create", {**scope, "name": "Alpha.md", "kind": "note", "content": "alpha body"})
    await bridge.call("create", {**scope, "name": "Middle.json", "kind": "asset", "content": "{}"})

    facade = FilesFacade([CopalFilesProvider(bridge)])
    root = (await facade.roots(_context()))["entries"][0]
    await _assert_advertised_folder_sorts(facade, root, ("name",))
    documents = next(
        row for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
        if row["name"] == "Documents"
    )
    await _assert_advertised_folder_sorts(
        facade, documents, ("name", "kind", "modified", "size"),
    )


@pytest.mark.asyncio
async def test_copal_ref_carries_non_default_workspace_into_direct_content(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "copal")
    await bridge.start()
    scope = {"owner": "alice", "workspace_id": "personal"}
    await bridge.call("create", {
        **scope,
        "name": "Personal.md",
        "kind": "note",
        "content": "personal body",
    })
    issuing = FilesFacade([CopalFilesProvider(bridge, workspace_id="personal")])
    personal_context = _context(workspace_id="personal")
    root = (await issuing.roots(personal_context))["entries"][0]
    documents = next(
        row for row in (await issuing.children(personal_context, parent_ref=root["ref"]))["entries"]
        if row["name"] == "Documents"
    )
    note = (await issuing.children(personal_context, parent_ref=documents["ref"]))["entries"][0]

    # Direct links must present the same workspace context as the sealed ref.
    resolving = FilesFacade([CopalFilesProvider(bridge)])
    content = await resolving.content(_context(workspace_id="personal"), resource_ref=note["ref"])
    assert content.data == b"personal body"


@pytest.mark.asyncio
async def test_copal_structured_note_and_asset_content_preserve_domain_bytes(tmp_path):
    class Bridge:
        def __init__(self, data_dir):
            self.data_dir = data_dir

        async def call(self, operation, args):
            assert args["workspace_id"] == "course"
            if operation == "metadata_get":
                kind = "asset" if args["id"] == "asset-id" else "note"
                return {"id": args["id"], "kind": kind, "name": "photo.png" if kind == "asset" else "Lesson"}
            if operation == "asset_path":
                return {"path": str(tmp_path / "assets" / "photo.png"), "name": "photo.png"}
            if operation == "get":
                return {
                    "id": args["id"],
                    "kind": "note",
                    "format": "copal-note-v1",
                    "storage": "database",
                    "name": "Lesson",
                    "text": "Body",
                    "properties": {"course": "Rust"},
                    "extensions": {},
                }
            raise AssertionError(operation)

    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\nreal bytes")
    provider = CopalFilesProvider(Bridge(tmp_path))
    note = await provider.content(_context(), origin_id="document:course:note-id")
    assert note.data.startswith(b"---\ncourse: \"Rust\"\n---\n\nBody")
    asset = await provider.content(_context(), origin_id="document:course:asset-id")
    assert asset.path == (tmp_path / "assets" / "photo.png").resolve()
    assert asset.media_type == "image/png"


@pytest.mark.asyncio
async def test_copal_content_reuses_canonical_interchange_and_confines_asset_paths(tmp_path):
    from routes.copal_routes import _note_projection_hash

    source = "---\ncourse: Rust # preserve this exact interchange\n---\n\nBody\n"
    provider_root = tmp_path / "copal-store"
    provider_root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside-provider-store")

    class Bridge:
        data_dir = provider_root

        async def call(self, operation, args):
            assert args["owner"] == "alice"
            assert args["workspace_id"] == "default"
            if operation == "metadata_get":
                kind = "asset" if args["id"] == "asset-id" else "note"
                return {"id": args["id"], "kind": kind, "name": "outside.png" if kind == "asset" else "Lesson"}
            if operation == "get":
                return {
                    "id": args["id"],
                    "kind": "note",
                    "format": "copal-note-v1",
                    "storage": "database",
                    "name": "Lesson",
                    "text": "Body",
                    "properties": {"course": "Rust"},
                    "extensions": {"interchange": {
                        "format": "markdown",
                        "source": source,
                        "projectionHash": _note_projection_hash("Body", {"course": "Rust"}),
                        "modified": False,
                    }},
                }
            if operation == "asset_path":
                return {"path": str(outside), "name": "outside.png"}
            raise AssertionError(operation)

    provider = CopalFilesProvider(Bridge())
    note = await provider.content(_context(), origin_id="document:default:note-id")
    assert note.data == source.encode("utf-8")
    with pytest.raises(FilesFacadeError) as denied:
        await provider.content(_context(), origin_id="document:default:asset-id")
    assert denied.value.code == "resource_unavailable"


@pytest.mark.asyncio
async def test_copal_trash_does_not_advertise_unreadable_content(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "copal")
    await bridge.start()
    scope = {"owner": "alice", "workspace_id": "default"}
    created = await bridge.call("create", {**scope, "name": "Gone.md", "kind": "note", "content": "gone"})
    await bridge.call("delete", {**scope, "id": created["doc"]["id"]})
    facade = FilesFacade([CopalFilesProvider(bridge)])
    root = (await facade.roots(_context()))["entries"][0]
    folders = await facade.children(_context(), parent_ref=root["ref"])
    trash = next(row for row in folders["entries"] if row["name"] == "Trash")
    page = await facade.children(_context(), parent_ref=trash["ref"])
    assert len(page["entries"]) == 1
    assert "download" not in page["entries"][0]["capabilities"]
    assert "preview" not in page["entries"][0]["capabilities"]
    # Trash entries expose the provider-owned restore lifecycle. The action is
    # still sealed behind the opaque ref and never bypasses Copal projections.
    assert "restore" in page["entries"][0]["capabilities"]
    current = await facade.stat(_context(), resource_ref=page["entries"][0]["ref"])
    assert current["name"] == "Gone.md"
    assert "restore" in current["capabilities"]


def _gallery_session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gallery.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _public_sort_value(entry, key):
    if key == "name":
        return str(entry["name"]).casefold()
    if key == "kind":
        return str(entry.get("sort_kind") or entry.get("mime_type") or entry.get("kind") or "").casefold()
    if key == "size":
        return int(entry["size"])
    if key == "modified":
        return int(entry["modified_unix_ms"])
    raise AssertionError(key)


async def _assert_advertised_folder_sorts(facade, folder, expected_keys):
    assert folder["sort_keys"] == list(expected_keys)
    for key in expected_keys:
        for direction in ("asc", "desc"):
            spec = {"key": key, "direction": direction, "directories_first": True}
            entries = []
            cursor = None
            first_cursor = None
            while True:
                page = await facade.children(
                    _context(),
                    parent_ref=folder["ref"],
                    cursor=cursor,
                    limit=1,
                    sort=spec,
                )
                assert page["sort"] == spec
                assert page["sort_keys"] == list(expected_keys)
                entries.extend(page["entries"])
                if first_cursor is None:
                    first_cursor = page["next_cursor"]
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            assert len({entry["id"] for entry in entries}) == len(entries)
            values = [_public_sort_value(entry, key) for entry in entries]
            assert values == sorted(values, reverse=direction == "desc")
            if first_cursor:
                with pytest.raises(FilesFacadeError) as stale:
                    await facade.children(
                        _context(),
                        parent_ref=folder["ref"],
                        cursor=first_cursor,
                        limit=1,
                        sort={**spec, "direction": "desc" if direction == "asc" else "asc"},
                    )
                assert stale.value.code == "stale_cursor"


@pytest.mark.asyncio
async def test_gallery_provider_pages_all_owned_sections_without_cross_owner_rows(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    db = factory()
    try:
        album = _files_image(id="album-a", name="Alice album", owner="alice")
        db.add(album)
        db.add_all([
            _files_image(id="alice-image", filename="alice.png", owner="alice", prompt="", is_active=True, favorite=True, album_id="album-a", file_size=12),
            _files_image(id="bob-image", filename="bob.png", owner="bob", prompt="", is_active=True, favorite=True, file_size=13),
            EditorDraft(id="draft-a", owner="alice", name="Alice project", payload="{}", is_active=True),
        ])
        db.commit()
    finally:
        db.close()

    facade = FilesFacade([FilesImagesProvider(factory)])
    root = (await facade.roots(_context()))["entries"][0]
    folders = await facade.children(_context(), parent_ref=root["ref"])
    by_name = {row["name"]: row for row in folders["entries"]}
    # Albums are retired — Gallery is a Files folder with ordinary subfolders.
    assert "Albums" not in by_name
    assert set(by_name) == {"Photos", "Favorites", "Saved Projects"}
    photos = await facade.children(_context(), parent_ref=by_name["Photos"]["ref"])
    favorites = await facade.children(_context(), parent_ref=by_name["Favorites"]["ref"])
    drafts = await facade.children(_context(), parent_ref=by_name["Saved Projects"]["ref"])
    assert [row["name"] for row in photos["entries"]] == ["alice.png"]
    assert [row["name"] for row in favorites["entries"]] == ["alice.png"]
    assert [row["name"] for row in drafts["entries"]] == ["Alice project"]


@pytest.mark.asyncio
async def test_gallery_favorite_action_is_idempotent_owner_scoped_and_refreshes_ref(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    db = factory()
    try:
        db.add_all([
            _files_image(id="alice-image", filename="alice.png", owner="alice", prompt="", is_active=True, favorite=False),
            _files_image(id="bob-image", filename="bob.png", owner="bob", prompt="", is_active=True, favorite=False),
        ])
        db.commit()
    finally:
        db.close()

    facade = FilesFacade([FilesImagesProvider(factory)])
    root = (await facade.roots(_context()))["entries"][0]
    photos = next(
        row for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
        if row["name"] == "Photos"
    )
    image = (await facade.children(_context(), parent_ref=photos["ref"]))["entries"][0]
    assert image["provenance"]["favorite"] is False

    favored = await facade.action(
        _context(), resource_ref=image["ref"], action="favorite.set", args={"value": True},
    )
    assert favored["state"] == {"favorite": True}
    assert favored["resource"]["provenance"]["favorite"] is True
    assert favored["resource"]["ref"] != image["ref"]
    # Setting the same value again is deliberately idempotent.
    favored_again = await facade.action(
        _context(), resource_ref=favored["resource"]["ref"], action="favorite.set", args={"value": True},
    )
    assert favored_again["state"] == {"favorite": True}

    db = factory()
    try:
        assert db.query(FilesImageResource.favorite).filter(FilesImageResource.id == "alice-image").scalar() is True
        assert db.query(FilesImageResource.favorite).filter(FilesImageResource.id == "bob-image").scalar() is False
    finally:
        db.close()
    with pytest.raises(FilesFacadeError) as denied:
        await facade.action(
            _context("bob"), resource_ref=favored_again["resource"]["ref"],
            action="favorite.set", args={"value": False},
        )
    assert denied.value.code in {"resource_unavailable", "invalid_resource_ref"}


@pytest.mark.asyncio
async def test_gallery_draft_sort_contract_honors_modified(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    older = datetime.fromisoformat("2025-01-01T00:00:00")
    newer = datetime.fromisoformat("2026-01-01T00:00:00")
    db = factory()
    try:
        db.add_all([
            EditorDraft(id="draft-old", owner="alice", name="A old", payload="{}", is_active=True, updated_at=older),
            EditorDraft(id="draft-new", owner="alice", name="Z new", payload="{}", is_active=True, updated_at=newer),
        ])
        db.commit()
    finally:
        db.close()

    facade = FilesFacade([FilesImagesProvider(factory)])
    root = (await facade.roots(_context()))["entries"][0]
    folders = {
        row["name"]: row
        for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
    }
    # Albums are retired; only the Saved Projects folder sorts by modified.
    assert "Albums" not in folders
    spec = {"key": "modified", "direction": "desc", "directories_first": True}
    drafts = await facade.children(_context(), parent_ref=folders["Saved Projects"]["ref"], sort=spec)
    assert [row["name"] for row in drafts["entries"]] == ["Z new", "A old"]


@pytest.mark.asyncio
async def test_gallery_advertises_only_truthful_sorts_and_pages_every_mode(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    oldest = datetime.fromisoformat("2024-01-01T00:00:00")
    middle = datetime.fromisoformat("2025-01-01T00:00:00")
    newest = datetime.fromisoformat("2026-01-01T00:00:00")
    db = factory()
    try:
        db.add_all([
            _files_image(id="media-audio", filename="a-audio.mp3", owner="alice", prompt="", is_active=True, file_size=30, updated_at=newest),
            _files_image(id="media-video", filename="m-video.mov", owner="alice", prompt="", is_active=True, file_size=20, updated_at=middle),
            _files_image(id="media-image", filename="z-image.png", owner="alice", prompt="", is_active=True, file_size=10, updated_at=oldest),
            _files_image(id="album-a", name="A album", owner="alice", updated_at=newest),
            _files_image(id="album-z", name="Z album", owner="alice", updated_at=oldest),
            EditorDraft(id="draft-a", owner="alice", name="A draft", payload="{}", is_active=True, updated_at=newest),
            EditorDraft(id="draft-z", owner="alice", name="Z draft", payload="{}", is_active=True, updated_at=oldest),
        ])
        db.commit()
    finally:
        db.close()

    facade = FilesFacade([FilesImagesProvider(factory)])
    root = (await facade.roots(_context()))["entries"][0]
    await _assert_advertised_folder_sorts(facade, root, ("name",))
    folders = {
        row["name"]: row
        for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
    }
    # Albums retired — no Albums folder, so only Photos + Saved Projects here.
    assert "Albums" not in folders
    await _assert_advertised_folder_sorts(facade, folders["Photos"], ("name", "kind", "modified", "size"))
    await _assert_advertised_folder_sorts(facade, folders["Saved Projects"], ("name", "modified"))

    for key in ("kind", "size"):
        with pytest.raises(FilesFacadeError) as unsupported:
            await facade.children(
                _context(), parent_ref=folders["Saved Projects"]["ref"], sort={"key": key},
            )
        assert unsupported.value.code == "unsupported_sort"


@pytest.mark.asyncio
async def test_gallery_video_is_downloadable_but_not_mislabeled_as_image(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    db = factory()
    try:
        db.add(_files_image(
            id="video-a",
            filename="legacy_video.mov",
            owner="alice",
            prompt="",
            is_active=True,
            file_size=42,
        ))
        db.commit()
    finally:
        db.close()
    facade = FilesFacade([FilesImagesProvider(factory)])
    root = (await facade.roots(_context()))["entries"][0]
    photos = next(
        row for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
        if row["name"] == "Photos"
    )
    video = (await facade.children(_context(), parent_ref=photos["ref"]))["entries"][0]
    assert video["kind"] == "file"
    assert video["preview_kind"] is None
    assert "preview" not in video["capabilities"]
    assert "download" in video["capabilities"]


@pytest.mark.asyncio
async def test_gallery_unsafe_legacy_filename_is_sanitized_and_not_downloadable(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    db = factory()
    try:
        db.add(_files_image(
            id="unsafe-legacy",
            filename="../../private/outside.mov",
            owner="alice",
            prompt="",
            is_active=True,
            file_size=42,
        ))
        db.commit()
    finally:
        db.close()
    resolver_calls = []
    provider = FilesImagesProvider(factory, image_resolver=lambda filename: resolver_calls.append(filename))
    facade = FilesFacade([provider])
    root = (await facade.roots(_context()))["entries"][0]
    photos = next(
        row for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
        if row["name"] == "Photos"
    )
    page = await facade.children(_context(), parent_ref=photos["ref"])
    item = page["entries"][0]
    assert item["name"] == "outside.mov"
    assert item["kind"] == "file"
    assert item["download_name"] is None
    assert "download" not in item["capabilities"]
    assert "../../private" not in json.dumps(page)
    with pytest.raises(FilesFacadeError) as denied:
        await provider.content(_context(), origin_id="image:unsafe-legacy")
    assert denied.value.code == "resource_unavailable"
    assert resolver_calls == []


@pytest.mark.asyncio
async def test_gallery_cursor_rejects_mutation_between_pages(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    db = factory()
    try:
        for index in range(3):
            db.add(_files_image(id=f"image-{index}", filename=f"{index}.png", owner="alice", prompt="", is_active=True, file_size=index))
        db.commit()
    finally:
        db.close()
    facade = FilesFacade([FilesImagesProvider(factory)])
    root = (await facade.roots(_context()))["entries"][0]
    photos = next(row for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"] if row["name"] == "Photos")
    first = await facade.children(_context(), parent_ref=photos["ref"], limit=1)
    db = factory()
    try:
        db.add(_files_image(id="image-new", filename="new.png", owner="alice", prompt="", is_active=True, file_size=5))
        db.commit()
    finally:
        db.close()
    with pytest.raises(FilesFacadeError) as stale:
        await facade.children(_context(), parent_ref=photos["ref"], cursor=first["next_cursor"], limit=1)
    assert stale.value.code == "stale_cursor"


@pytest.mark.asyncio
async def test_gallery_content_is_owner_scoped_and_uses_private_file_descriptor(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nbody")
    db = factory()
    try:
        db.add_all([
            _files_image(id="alice-image", filename="image.png", owner="alice", prompt="", is_active=True, file_size=image_path.stat().st_size),
            _files_image(id="bob-image", filename="bob.png", owner="bob", prompt="", is_active=True, file_size=1),
        ])
        db.commit()
    finally:
        db.close()
    provider = FilesImagesProvider(factory, image_resolver=lambda filename: image_path if filename == "image.png" else tmp_path / "missing")
    content = await provider.content(_context(), origin_id="image:alice-image")
    assert content.path == image_path.resolve()
    assert content.expected_identity is not None
    assert content.media_type == "image/png"
    with pytest.raises(FilesFacadeError) as denied:
        await provider.content(_context(), origin_id="image:bob-image")
    assert denied.value.code == "resource_unavailable"


@pytest.mark.asyncio
async def test_gallery_retires_albums_and_resolves_legacy_album_links(tmp_path):
    """Empty-Gallery retirement: albums are gone, legacy links still resolve.

    The Gallery applet/album model is retired and images open in Imps. A
    persisted album reference must land on the folder its images now live in
    (Photos) rather than a hard 404, and no Albums view is browsable.
    """
    factory = _gallery_session_factory(tmp_path)
    db = factory()
    try:
        db.add_all([
            _files_image(id="album-a", name="Alice album", owner="alice"),
            _files_image(id="alice-image", filename="alice.png", owner="alice", prompt="", is_active=True, album_id="album-a", file_size=12),
        ])
        db.commit()
    finally:
        db.close()

    facade = FilesFacade([FilesImagesProvider(factory)])
    root = (await facade.roots(_context()))["entries"][0]
    folders = await facade.children(_context(), parent_ref=root["ref"])
    names = {row["name"] for row in folders["entries"]}
    assert "Albums" not in names

    # Images open in Imps, not the retired Gallery applet.
    photos = next(row for row in folders["entries"] if row["name"] == "Photos")
    image = (await facade.children(_context(), parent_ref=photos["ref"]))["entries"][0]
    opened = await facade.open(_context(), resource_ref=image["ref"])
    assert opened["target"] == {"app": "imps"}

    # A legacy album origin resolves to the Photos folder (its images' owner).
    provider = FilesImagesProvider(factory)
    resolved = await provider.stat(_context(), origin_id="album:album-a")
    assert resolved.origin_id == "photos"
    assert resolved.provenance.get("retired_alias") == "album"

    # Album rows do not surface as browseable children anywhere.
    with pytest.raises(FilesFacadeError) as gone:
        await provider.children(_context(), parent_origin_id="albums", cursor=None, snapshot=None, limit=10, sort={"key": "name", "direction": "asc"}, query="")
    assert gone.value.code == "resource_unavailable"


@pytest.mark.asyncio
async def test_gallery_exact_open_exposes_managed_identity_and_owner_scope(tmp_path):
    """Exact Imps open is owner-scoped and carries the managed save identity.

    Imps Save now writes through the managed ``/api/imps`` surface, so exact
    open is no longer read-only and must expose the (provider, resource_id)
    pair that surface keys on. Filesystem paths stay sealed and another
    owner's provider identity never appears in the payload.
    """
    factory = _gallery_session_factory(tmp_path)
    image_path = tmp_path / "exact.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nbody")
    db = factory()
    try:
        db.add_all([
            _files_image(
                id="alice-exact-origin",
                filename="exact.png",
                owner="alice",
                prompt="A quiet lake",
                caption="water",
                model="imported",
                tags="calm, blue",
                ai_tags="water",
                favorite=True,
                is_active=True,
                width=640,
                height=480,
                file_size=image_path.stat().st_size,
            ),
            _files_image(
                id="bob-exact-origin",
                filename="bob.png",
                owner="bob",
                prompt="private",
                is_active=True,
            ),
        ])
        db.commit()
    finally:
        db.close()

    provider = FilesImagesProvider(
        factory,
        image_resolver=lambda filename: image_path if filename == "exact.png" else tmp_path / "missing",
    )
    facade = FilesFacade([provider])
    root = (await facade.roots(_context()))["entries"][0]
    photos = next(
        row for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
        if row["name"] == "Photos"
    )
    image = (await facade.children(_context(), parent_ref=photos["ref"]))["entries"][0]
    opened = await facade.open(_context(), resource_ref=image["ref"])
    assert opened["target"] == {"app": "imps"}
    assert opened["exact"] is True

    exact = await facade.open_payload(_context(), resource_ref=opened["resource"]["ref"])
    assert exact["target"] == {"app": "imps"}
    assert exact["payload"]["prompt"] == "A quiet lake"
    assert exact["payload"]["filename"] == "exact.png"
    assert exact["payload"]["width"] == 640
    assert exact["payload"]["favorite"] is True
    assert exact["payload"]["read_only"] is False
    # Managed Imps save keys on this pair; the caller's own provider origin is
    # intentionally present for their owner-scoped save surface.
    assert exact["payload"]["provider"] == "gallery"
    assert exact["payload"]["resource_id"] == "image:alice-exact-origin"
    serialized = json.dumps(exact)
    assert "bob-exact-origin" not in serialized
    assert str(image_path) not in serialized

    with pytest.raises(FilesFacadeError) as denied:
        await facade.open_payload(_context("bob"), resource_ref=opened["resource"]["ref"])
    assert denied.value.code in {"resource_unavailable", "invalid_resource_ref"}


@pytest.mark.asyncio
async def test_library_provider_pages_all_domain_owners_and_archive_without_ids(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    db = factory()
    try:
        db.add_all([
            DbSession(
                id="alice-chat-id",
                name="Alice chat",
                endpoint_url="http://example.invalid",
                model="test",
                owner="alice",
                archived=False,
                message_count=3,
            ),
            DbSession(
                id="alice-archived-chat-id",
                name="Old Alice chat",
                endpoint_url="http://example.invalid",
                model="test",
                owner="alice",
                archived=True,
            ),
            DbSession(
                id="bob-chat-id",
                name="Bob chat",
                endpoint_url="http://example.invalid",
                model="test",
                owner="bob",
            ),
            Document(
                id="alice-document-id",
                title="Alice document",
                language="markdown",
                current_content="SECRET DOCUMENT BODY",
                owner="alice",
                is_active=True,
                archived=False,
            ),
            Document(
                id="alice-archived-document-id",
                title="Old Alice document",
                language="text",
                current_content="SECRET ARCHIVED BODY",
                owner="alice",
                is_active=True,
                archived=True,
            ),
            Document(
                id="bob-document-id",
                title="Bob document",
                current_content="BOB SECRET",
                owner="bob",
                is_active=True,
            ),
            PublishedFile(
                id="a" * 32,
                owner="alice",
                filename="result.txt",
                mime_type="text/plain",
                size=12,
                sha256="b" * 64,
                source="agent",
            ),
            PublishedFile(
                id="c" * 32,
                owner="bob",
                filename="bob.txt",
                mime_type="text/plain",
                size=9,
                sha256="d" * 64,
                source="agent",
            ),
        ])
        db.commit()
    finally:
        db.close()

    research = tmp_path / "research"
    research.mkdir()
    (research / "alice-research-id.json").write_text(json.dumps({
        "owner": "alice",
        "query": "Alice research",
        "sources": [{"url": "https://example.invalid"}],
        "completed_at": 123,
        "archived": False,
        "report": "SECRET RESEARCH BODY",
    }), encoding="utf-8")
    (research / "bob-research-id.json").write_text(json.dumps({
        "owner": "bob",
        "query": "Bob research",
        "archived": False,
    }), encoding="utf-8")

    facade = FilesFacade([LibraryFilesProvider(factory, research_root=research)])
    root = (await facade.roots(_context()))["entries"][0]
    folders = await facade.children(_context(), parent_ref=root["ref"])
    by_name = {entry["name"]: entry for entry in folders["entries"]}
    assert set(by_name) == {"Documents", "Published Downloads", "Chats", "Research", "Archive"}

    documents = await facade.children(_context(), parent_ref=by_name["Documents"]["ref"])
    published = await facade.children(_context(), parent_ref=by_name["Published Downloads"]["ref"])
    chats = await facade.children(_context(), parent_ref=by_name["Chats"]["ref"])
    research_page = await facade.children(_context(), parent_ref=by_name["Research"]["ref"])
    assert [entry["name"] for entry in documents["entries"]] == ["Alice document"]
    assert [entry["name"] for entry in published["entries"]] == ["result.txt"]
    assert [entry["name"] for entry in chats["entries"]] == ["Alice chat"]
    assert [entry["name"] for entry in research_page["entries"]] == ["Alice research"]

    archive = await facade.children(_context(), parent_ref=by_name["Archive"]["ref"])
    archive_folders = {entry["name"]: entry for entry in archive["entries"]}
    archived_documents = await facade.children(_context(), parent_ref=archive_folders["Documents"]["ref"])
    archived_chats = await facade.children(_context(), parent_ref=archive_folders["Chats"]["ref"])
    assert [entry["name"] for entry in archived_documents["entries"]] == ["Old Alice document"]
    assert [entry["name"] for entry in archived_chats["entries"]] == ["Old Alice chat"]

    payload = json.dumps({
        "documents": documents,
        "published": published,
        "chats": chats,
        "research": research_page,
    })
    for forbidden in (
        "alice-document-id",
        "alice-chat-id",
        "alice-research-id",
        "a" * 32,
        "SECRET",
        "bob-document-id",
        "bob-chat-id",
        "bob-research-id",
    ):
        assert forbidden not in payload


@pytest.mark.asyncio
async def test_library_advertises_and_honors_each_folder_sort_contract(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    oldest = datetime.fromisoformat("2024-01-01T00:00:00")
    newest = datetime.fromisoformat("2026-01-01T00:00:00")
    db = factory()
    try:
        db.add_all([
            Document(id="sort-doc-a", title="Alpha", language="python", current_content="a" * 30, owner="alice", is_active=True, archived=False, updated_at=newest),
            Document(id="sort-doc-z", title="Zeta", language="markdown", current_content="z", owner="alice", is_active=True, archived=False, updated_at=oldest),
            PublishedFile(id="1" * 32, owner="alice", filename="alpha.zip", mime_type="application/zip", size=40, sha256="a" * 64, source="agent", created_at=newest),
            PublishedFile(id="2" * 32, owner="alice", filename="zeta.txt", mime_type="text/plain", size=2, sha256="b" * 64, source="agent", created_at=oldest),
            DbSession(id="sort-chat-a", name="Alpha chat", endpoint_url="http://example.invalid", model="z-model", owner="alice", archived=False, last_message_at=newest),
            DbSession(id="sort-chat-z", name="Zeta chat", endpoint_url="http://example.invalid", model="a-model", owner="alice", archived=False, last_message_at=oldest),
        ])
        db.commit()
    finally:
        db.close()

    research = tmp_path / "research-sort"
    research.mkdir()
    (research / "sort-research-a.json").write_text(json.dumps({
        "owner": "alice", "query": "Alpha research", "raw_report": "a" * 25,
        "category": "z-category", "completed_at": 1_767_225_600, "archived": False,
    }), encoding="utf-8")
    (research / "sort-research-z.json").write_text(json.dumps({
        "owner": "alice", "query": "Zeta research", "raw_report": "z",
        "category": "a-category", "completed_at": 1_704_067_200, "archived": False,
    }), encoding="utf-8")

    facade = FilesFacade([LibraryFilesProvider(factory, research_root=research)])
    root = (await facade.roots(_context()))["entries"][0]
    await _assert_advertised_folder_sorts(facade, root, ("name",))
    folders = {
        row["name"]: row
        for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
    }
    contracts = {
        "Documents": ("name", "kind", "modified", "size"),
        "Published Downloads": ("name", "kind", "modified", "size"),
        "Chats": ("name", "modified"),
        "Research": ("name", "modified", "size"),
    }
    for name, keys in contracts.items():
        await _assert_advertised_folder_sorts(facade, folders[name], keys)

    for name, unsupported_keys in {
        "Chats": ("kind", "size"),
        "Research": ("kind",),
    }.items():
        for key in unsupported_keys:
            with pytest.raises(FilesFacadeError) as unsupported:
                await facade.children(_context(), parent_ref=folders[name]["ref"], sort={"key": key})
            assert unsupported.value.code == "unsupported_sort"


@pytest.mark.asyncio
async def test_library_exact_chat_and_research_open_are_bounded_read_only_and_opaque(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    db = factory()
    try:
        db.add(DbSession(
            id="exact-chat-origin",
            name="Exact chat",
            endpoint_url="http://example.invalid",
            model="private/model",
            owner="alice",
            archived=False,
            message_count=52,
        ))
        db.add(ChatMessage(
            id="exact-chat-message",
            session_id="exact-chat-origin",
            role="user",
            content=json.dumps([
                {"type": "text", "text": "Visible text"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,PRIVATE"}},
            ]),
        ))
        db.commit()
    finally:
        db.close()

    research = tmp_path / "research"
    research.mkdir()
    (research / "exact-research-origin.json").write_text(json.dumps({
        "owner": "alice",
        "query": "Exact research",
        "category": "systems",
        "result": "Research body",
        "sources": [{"title": "Primary source", "url": "https://example.invalid/source"}],
        "archived": False,
    }), encoding="utf-8")
    provider = LibraryFilesProvider(factory, research_root=research)
    facade = FilesFacade([provider])
    root = (await facade.roots(_context()))["entries"][0]
    folders = {
        row["name"]: row
        for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
    }

    chat = (await facade.children(_context(), parent_ref=folders["Chats"]["ref"]))["entries"][0]
    chat_open = await facade.open(_context(), resource_ref=chat["ref"])
    assert chat_open["exact"] is True
    chat_payload = await facade.open_payload(_context(), resource_ref=chat_open["resource"]["ref"])
    assert chat_payload["target"] == {"app": "chat"}
    assert chat_payload["payload"]["read_only"] is True
    assert chat_payload["payload"]["messages"] == [{
        "role": "user",
        "text": "Visible text\n\n[Image attachment omitted from export]",
        "timestamp": chat_payload["payload"]["messages"][0]["timestamp"],
    }]
    assert chat_payload["payload"]["truncated"] is True

    report = (await facade.children(_context(), parent_ref=folders["Research"]["ref"]))["entries"][0]
    report_open = await facade.open(_context(), resource_ref=report["ref"])
    assert report_open["exact"] is True
    research_payload = await facade.open_payload(_context(), resource_ref=report_open["resource"]["ref"])
    assert research_payload["target"] == {"app": "research"}
    assert research_payload["payload"] == {
        "title": "Exact research",
        "category": "systems",
        "archived": False,
        "report": "Research body",
        "sources": [{"title": "Primary source", "url": "https://example.invalid/source"}],
        "source_count": 1,
        "truncated": False,
        "read_only": True,
    }

    serialized = json.dumps({"chat": chat_payload, "research": research_payload})
    assert "exact-chat-origin" not in serialized
    assert "exact-chat-message" not in serialized
    assert "exact-research-origin" not in serialized
    assert "data:image/png" not in serialized
    with pytest.raises(FilesFacadeError):
        await facade.open_payload(_context("bob"), resource_ref=chat_open["resource"]["ref"])


@pytest.mark.asyncio
async def test_library_document_and_research_archive_actions_round_trip_without_raw_ids(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    db = factory()
    try:
        db.add_all([
            Document(
                id="archive-document-id",
                title="Archive document",
                language="markdown",
                current_content="body",
                owner="alice",
                is_active=True,
                archived=False,
            ),
            DbSession(
                id="chat-with-domain-lifecycle",
                name="Chat stays truthful",
                endpoint_url="http://example.invalid",
                model="test",
                owner="alice",
                archived=False,
            ),
        ])
        db.commit()
    finally:
        db.close()
    research = tmp_path / "research"
    research.mkdir()
    research_path = research / "archive-research-id.json"
    research_path.write_text(json.dumps({
        "owner": "alice",
        "query": "Archive research",
        "raw_report": "report",
        "archived": False,
        "preserved": {"domain": True},
    }), encoding="utf-8")

    facade = FilesFacade([LibraryFilesProvider(factory, research_root=research)])
    root = (await facade.roots(_context()))["entries"][0]
    folders = {
        row["name"]: row
        for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
    }
    document = (await facade.children(_context(), parent_ref=folders["Documents"]["ref"]))["entries"][0]
    report = (await facade.children(_context(), parent_ref=folders["Research"]["ref"]))["entries"][0]
    chat = (await facade.children(_context(), parent_ref=folders["Chats"]["ref"]))["entries"][0]
    assert "archive" in document["capabilities"] and "restore" not in document["capabilities"]
    assert "archive" in report["capabilities"] and "restore" not in report["capabilities"]
    # Chat archive coordinates runtime projections and intentionally remains
    # absent until that existing route lifecycle is extracted as a service.
    assert "archive" not in chat["capabilities"] and "restore" not in chat["capabilities"]

    archived_document = await facade.action(
        _context(), resource_ref=document["ref"], action="archive.set", args={"value": True},
    )
    archived_report = await facade.action(
        _context(), resource_ref=report["ref"], action="archive.set", args={"value": True},
    )
    assert archived_document["state"] == {"archived": True}
    assert "restore" in archived_document["resource"]["capabilities"]
    assert archived_report["state"] == {"archived": True}
    assert "restore" in archived_report["resource"]["capabilities"]
    serialized = json.dumps({"document": archived_document, "research": archived_report})
    assert "archive-document-id" not in serialized
    assert "archive-research-id" not in serialized

    restored_document = await facade.action(
        _context(), resource_ref=archived_document["resource"]["ref"],
        action="archive.set", args={"value": False},
    )
    restored_report = await facade.action(
        _context(), resource_ref=archived_report["resource"]["ref"],
        action="archive.set", args={"value": False},
    )
    assert restored_document["state"] == {"archived": False}
    assert "archive" in restored_document["resource"]["capabilities"]
    assert restored_report["state"] == {"archived": False}
    assert json.loads(research_path.read_text(encoding="utf-8"))["preserved"] == {"domain": True}

    with pytest.raises(FilesFacadeError) as denied:
        await facade.action(
            _context("bob"), resource_ref=restored_document["resource"]["ref"],
            action="archive.set", args={"value": True},
        )
    assert denied.value.code in {"resource_unavailable", "invalid_resource_ref"}


@pytest.mark.asyncio
async def test_library_stat_is_owner_scoped_beyond_first_page(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    db = factory()
    try:
        for index in range(205):
            db.add(Document(
                id=f"alice-doc-{index:03d}",
                title=f"Document {index:03d}",
                current_content="body",
                owner="alice",
                is_active=True,
                archived=False,
            ))
        db.add(Document(
            id="bob-only-document",
            title="Bob only",
            current_content="secret",
            owner="bob",
            is_active=True,
        ))
        db.commit()
    finally:
        db.close()
    provider = LibraryFilesProvider(factory, research_root=tmp_path / "research")
    facade = FilesFacade([provider])
    root = (await facade.roots(_context()))["entries"][0]
    documents = next(
        row for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
        if row["name"] == "Documents"
    )
    cursor = None
    last = None
    while True:
        page = await facade.children(_context(), parent_ref=documents["ref"], cursor=cursor, limit=50)
        last = page["entries"][-1]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    stat = await facade.stat(_context(), resource_ref=last["ref"])
    assert stat["name"] == "Document 204"

    bob_facade = FilesFacade([provider])
    with pytest.raises(FilesFacadeError) as denied:
        await bob_facade.stat(_context("bob"), resource_ref=last["ref"])
    assert denied.value.code in {"resource_unavailable", "invalid_resource_ref"}


@pytest.mark.asyncio
async def test_library_content_reuses_domain_owners_without_raw_id_urls(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    published_root = tmp_path / "published"
    published_id = "a" * 32
    published_path = published_root / published_id[:2] / published_id
    published_path.parent.mkdir(parents=True)
    published_path.write_bytes(b"published bytes")
    db = factory()
    try:
        chat = DbSession(
            id="chat-a",
            name="Chat export",
            endpoint_url="http://example.invalid",
            model="test",
            owner="alice",
            archived=False,
        )
        db.add(chat)
        db.add_all([
            ChatMessage(id="message-1", session_id="chat-a", role="user", content="hello"),
            ChatMessage(id="message-2", session_id="chat-a", role="assistant", content="world"),
            Document(id="doc-a", title="Code", language="python", current_content="print('ok')", owner="alice", is_active=True),
            PublishedFile(id=published_id, owner="alice", filename="result.txt", mime_type="text/plain", size=15, sha256="b" * 64, source="agent"),
        ])
        db.commit()
    finally:
        db.close()
    research = tmp_path / "research"
    research.mkdir()
    (research / "research-a.json").write_text(json.dumps({
        "owner": "alice",
        "query": "Report",
        "raw_report": "# Findings\nOwner report",
        "sources": [{"url": "https://private.invalid"}],
    }), encoding="utf-8")
    service = PublishedFileService(storage_root=str(published_root), session_factory=factory)
    provider = LibraryFilesProvider(factory, research_root=research, published_service=service)

    document = await provider.content(_context(), origin_id="document:doc-a")
    assert document.data == b"print('ok')"
    assert document.filename.endswith(".py")
    chat_content = await provider.content(_context(), origin_id="chat:chat-a")
    assert b"## USER" in chat_content.data and b"## ASSISTANT" in chat_content.data
    report = await provider.content(_context(), origin_id="research:research-a")
    assert report.data == b"# Findings\nOwner report"
    assert b"private.invalid" not in report.data
    published = await provider.content(_context(), origin_id=f"published:{published_id}")
    assert published.path == published_path.resolve()
    assert published.etag == "b" * 64

    with pytest.raises(FilesFacadeError) as denied:
        await provider.content(_context("bob"), origin_id="document:doc-a")
    assert denied.value.code == "resource_unavailable"


@pytest.mark.asyncio
async def test_library_sizes_and_multimodal_exports_match_downloaded_bytes(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    db = factory()
    try:
        db.add(DbSession(
            id="chat-multimodal",
            name="Blocks",
            endpoint_url="http://example.invalid",
            model="test",
            owner="alice",
            archived=False,
        ))
        db.add(ChatMessage(
            id="message-blocks",
            session_id="chat-multimodal",
            role="user",
            content=json.dumps([
                {"type": "text", "text": "visible text"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRETPIXELS"}},
            ]),
        ))
        db.add(Document(
            id="unicode-document",
            title="Unicode.pdf",
            language="pdf",
            current_content="café ☕",
            owner="alice",
            is_active=True,
            archived=False,
        ))
        db.commit()
    finally:
        db.close()
    research = tmp_path / "research"
    research.mkdir()
    report = "évidence"
    (research / "sized.json").write_text(json.dumps({
        "owner": "alice",
        "query": "Sized",
        "raw_report": report,
        "sources": [1, 2, 3],
        "archived": False,
    }), encoding="utf-8")
    facade = FilesFacade([LibraryFilesProvider(factory, research_root=research)])
    root = (await facade.roots(_context()))["entries"][0]
    folders = {
        row["name"]: row
        for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
    }
    documents = await facade.children(_context(), parent_ref=folders["Documents"]["ref"])
    document = documents["entries"][0]
    assert document["size"] == len("café ☕".encode("utf-8"))
    assert document["download_name"].endswith(".md")
    assert not document["download_name"].endswith(".pdf")
    document_content = await LibraryFilesProvider(factory, research_root=research).content(
        _context(), origin_id="document:unicode-document"
    )
    assert document_content.filename == document["download_name"]
    assert document_content.data == "café ☕".encode("utf-8")
    assert document_content.media_type.startswith("text/markdown")
    research_page = await facade.children(_context(), parent_ref=folders["Research"]["ref"], sort={"key": "size"})
    assert research_page["entries"][0]["size"] == len(report.encode("utf-8"))
    chat = await LibraryFilesProvider(factory, research_root=research).content(
        _context(), origin_id="chat:chat-multimodal"
    )
    assert b"visible text" in chat.data
    assert b"SECRETPIXELS" not in chat.data
    assert b"image_url" not in chat.data
    assert b"[Image attachment omitted from export]" in chat.data
    assert b"[{" not in chat.data


@pytest.mark.asyncio
async def test_research_size_ties_are_stable_and_snapshot_hashes_same_size_mutations(tmp_path):
    factory = _gallery_session_factory(tmp_path)
    research = tmp_path / "research"
    research.mkdir()
    first_path = research / "a-report.json"
    second_path = research / "b-report.json"

    def encoded(query):
        return json.dumps({
            "owner": "alice",
            "query": query,
            "raw_report": "é",
            "archived": False,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    first_path.write_bytes(encoded("Zeta!"))
    second_path.write_bytes(encoded("Alpha"))
    facade = FilesFacade([LibraryFilesProvider(factory, research_root=research)])
    root = (await facade.roots(_context()))["entries"][0]
    research_folder = next(
        row for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
        if row["name"] == "Research"
    )
    first_page = await facade.children(
        _context(),
        parent_ref=research_folder["ref"],
        limit=1,
        sort={"key": "size", "direction": "desc"},
    )
    # Equal exported byte sizes retain ascending opaque provider identity as a
    # tie-breaker even when the primary direction is descending.
    assert first_page["entries"][0]["name"] == "Zeta!"
    assert first_page["entries"][0]["size"] == len("é".encode("utf-8"))
    assert first_page["entries"][0]["download_name"].endswith(".md")
    assert first_page["next_cursor"]

    previous = second_path.stat()
    replacement = encoded("Omega")
    assert len(replacement) == second_path.stat().st_size
    second_path.write_bytes(replacement)
    os.utime(second_path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    with pytest.raises(FilesFacadeError) as stale:
        await facade.children(
            _context(),
            parent_ref=research_folder["ref"],
            cursor=first_page["next_cursor"],
            limit=1,
            sort={"key": "size", "direction": "desc"},
        )
    assert stale.value.code == "stale_cursor"


@pytest.mark.asyncio
async def test_library_chat_archive_uses_shared_lifecycle_and_refreshes_capabilities(tmp_path, monkeypatch):
    factory = _gallery_session_factory(tmp_path)
    db = factory()
    try:
        db.add(DbSession(
            id="chat-action-origin",
            name="Archive this chat",
            endpoint_url="http://example.invalid",
            model="test",
            owner="alice",
            archived=False,
        ))
        db.commit()
    finally:
        db.close()

    purges = []

    async def purge(_supervisor, session_id, *, owner=None):
        purges.append((session_id, owner))
        return True

    monkeypatch.setattr("src.openclank.transcript_projection.purge_execution_projection", purge)
    monkeypatch.setattr("src.agent_runs.is_active", lambda _session_id: False)
    lifecycle = ChatLifecycleService(session_factory=factory)
    facade = FilesFacade([LibraryFilesProvider(
        factory,
        research_root=tmp_path / "research",
        chat_lifecycle=lifecycle,
    )])
    root = (await facade.roots(_context()))["entries"][0]
    chats = next(
        row for row in (await facade.children(_context(), parent_ref=root["ref"]))["entries"]
        if row["name"] == "Chats"
    )
    chat = (await facade.children(_context(), parent_ref=chats["ref"]))["entries"][0]
    assert "archive" in chat["capabilities"]

    archived = await facade.action(
        _context(), resource_ref=chat["ref"], action="archive.set", args={"value": True},
    )
    assert "restore" in archived["resource"]["capabilities"]
    assert "archive" not in archived["resource"]["capabilities"]
    restored = await facade.action(
        _context(), resource_ref=archived["resource"]["ref"], action="archive.set", args={"value": False},
    )
    assert "archive" in restored["resource"]["capabilities"]
    assert purges == [("chat-action-origin", "alice"), ("chat-action-origin", "alice")]
    assert "chat-action-origin" not in json.dumps(restored)

@pytest.mark.asyncio
async def test_copal_files_after_commit_history_transport_failure_keeps_result(monkeypatch):
    calls = []

    class Bridge:
        async def call(self, operation, args):
            calls.append(operation)
            if operation == "get":
                return {"id": "DOC", "name": "Before.md", "head": "head-before"}
            if operation == "rename":
                return {"doc": {"id": "DOC", "name": "After.md", "head": "head-after"}}
            raise AssertionError(operation)

    class AfterFailureHistory:
        def __init__(self, *args, **kwargs): pass
        def prepare(self, envelope, *, content, fingerprint): return {"Action": {"action_id": envelope["action_id"]}}
        def record_live(self, action_id, receipt): raise OSError("history worker disconnected after commit")

    monkeypatch.setattr("src.openclank.files_managed_providers.history_binding_for", lambda owner, lane: {
        "socket": "history.sock", "token": "account-token", "actor_id": "alice", "account_id": "account-alice",
    })
    monkeypatch.setattr("src.openclank.files_managed_providers.HistoryClient", AfterFailureHistory)
    provider = CopalFilesProvider(Bridge())
    result, history = await provider._action_with_history(
        _context(), operation="rename", args={"owner": "alice", "workspace_id": "default", "id": "DOC", "name": "After.md"}, action_id="after-transport-failure",
    )
    assert result["doc"]["name"] == "After.md"
    assert history["status"] == "failed"
    assert history["phase"] == "after"
    assert calls == ["get", "rename"]


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

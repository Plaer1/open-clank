"""B2-P1: Host trash/restore through the real Native Files service.

Plain functions with bare asserts. Needs OPEN_CLANK_FILES_SERVICE_BIN (or the private
devtools build) and runs the framed transport against fresh temp directories.
"""
import asyncio
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from src.openclank.files_facade import FilesFacadeError, ProviderContext
from src.openclank.files_host_provider import HostFilesProvider, _origin
from src.openclank.files_service_client import OdysseusFilesClient
from src.openclank.media_ownership import MediaProvenance

BINARY = os.environ.get("OPEN_CLANK_FILES_SERVICE_BIN") or os.path.expanduser(
    "~/.cache/openclank-devtools/terra-rust/target/debug/odysseus-files-service")
CONTEXT = ProviderContext("account-alice", "alice", 4, workspace_id="default")


class Store:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def claim_host_mutation(self, **kwargs):
        return {"operation_id": "claim", "token": "token"}

    def release_host_mutation(self, **kwargs):
        pass

    def list_host_media_operations(self, *, owner_subject_id, select):
        return [row for row in self.rows if select(row)]


class HostRegistry:
    def app_scope(self, username, *, is_admin=False):
        return {"host": True}


class RootRegistry:
    def __init__(self, root):
        self.root = str(root)

    def app_scope(self, username, *, is_admin=False):
        return {"host": False, "capabilities": ["write"], "visible_root_ids": ["r1"], "root_capabilities": {"r1": ["write"]}}

    def visibility_for_subject(self, username):
        return [{"root_id": "r1", "capabilities": ["write"], "root": {
            "enabled": True, "availability": "available", "canonical_path": self.root,
            "kind": "recursive_directory", "capabilities": ["write"]}}]


class Recorder:
    """Delegates to the real service client and remembers every operation."""

    def __init__(self, client):
        self.client, self.operations = client, []

    async def request(self, operation, path, payload=None):
        self.operations.append(operation)
        return await self.client.request(operation, path, payload)


def snapshot(root):
    result = {}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            full = os.path.join(folder, name)
            rel = os.path.relpath(full, root)
            if os.path.islink(full):
                result[rel] = ("link", os.readlink(full))
            elif os.path.isdir(full):
                result[rel] = ("dir",)
            else:
                data = Path(full).read_bytes()
                result[rel] = ("file", len(data), hashlib.sha256(data).hexdigest())
    return result


def provenance(root, document="Note.md"):
    return {"canonical_root": str(root), "origin": "workspace", "owner_subject_id": "account-alice",
            "workspace_id": "default", "document_id": "opaque", "document_path": document,
            "asset_name": "image.png", "asset_digest": "sha256:" + "a" * 64 + ":4", "layout_version": 2}


def run(body, *, scoped=False, rows_for=None, rename_ok=False):
    """Create fresh temp dirs, a real client and a provider, then await body(env)."""
    base = Path(tempfile.mkdtemp(prefix="b2p1-"))
    work = (base / "work").resolve()
    work.mkdir()
    for name in ("data", "xdg"):
        (base / name).mkdir()
    client = OdysseusFilesClient("alice", binary=BINARY, transport="framed")
    recorder = Recorder(client)
    store = Store(rows_for(work) if rows_for else ())
    registry = RootRegistry(work) if scoped else HostRegistry()
    provider = HostFilesProvider(registry=registry, client_factory=lambda username, **kwargs: recorder, operation_store=store)
    try:
        asyncio.run(body(provider, recorder, work))
    finally:
        client.close()
        shutil.rmtree(base, ignore_errors=True)


async def refused(provider, recorder, work, target, *, code=None, action="trash", args=None, expected=None):
    before = snapshot(work)
    try:
        await provider.action(CONTEXT, origin_id=_origin(str(target)), action=action, args=args or {},
                              expected_revision=expected)
    except FilesFacadeError as exc:
        assert code is None or exc.code == code, (exc.code, str(exc))
    else:
        raise AssertionError(f"not refused: {target}")
    assert snapshot(work) == before
    assert "trash" not in recorder.operations and "restore" not in recorder.operations


def test_trash_then_restore_regular_file():
    async def body(provider, recorder, work):
        file = work / "a.txt"
        file.write_bytes(b"exact \x00 bytes\n")
        revision = (await provider.stat(CONTEXT, origin_id=_origin(str(file)))).revision
        result = await provider.action(CONTEXT, origin_id=_origin(str(file)), action="trash", args={},
                                       action_id="act-1", expected_revision=revision)
        assert recorder.operations.count("trash") == 1
        assert not file.exists()
        receipt = result.action_receipt["receipt"]
        entry = receipt["trash_entry"]
        assert result.action_receipt["action_id"] == "act-1" and receipt["outcome"] == "trash"
        # No History hook is wired in this harness, so the receipt reports it honestly.
        assert receipt["history"] == {"state": "unavailable"}
        trashed = Path(entry["trashed_path"])
        assert trashed.parent == work / ".odysseus-trash" and trashed.read_bytes() == b"exact \x00 bytes\n"
        restored = await provider.action(CONTEXT, origin_id=_origin(str(file)), action="restore", args={"entry": entry})
        assert recorder.operations.count("restore") == 1
        assert file.read_bytes() == b"exact \x00 bytes\n" and not trashed.exists()
        assert restored.revision == revision
    run(body)


def test_trash_directory_is_not_listed_or_searched_but_user_trash_named_folders_are():
    async def body(provider, recorder, work):
        (work / "my-trash-notes").mkdir()
        (work / "my-trash-notes" / "keep.txt").write_text("k")
        (work / "a.txt").write_text("a")
        sort = {"key": "name", "direction": "asc", "directories_first": True}

        async def names(query=""):
            page = await provider.children(CONTEXT, parent_origin_id=_origin(str(work)), cursor=None,
                                           snapshot=None, limit=100, sort=sort, query=query)
            return [row.name for row in page.entries]
        result = await provider.action(CONTEXT, origin_id=_origin(str(work / "a.txt")), action="trash", args={})
        entry = result.action_receipt["receipt"]["trash_entry"]
        assert (work / ".odysseus-trash").is_dir()
        listed = await names()
        assert ".odysseus-trash" not in listed and "my-trash-notes" in listed
        assert await names("trash") == []  # trashed copy (trash-<id>) is not a hit
        assert await names("keep") == ["keep.txt"]  # files in a user folder named *trash* still are
        assert await names("a.txt") == []  # the trashed copy is not a search hit
        await provider.action(CONTEXT, origin_id=_origin(str(work / "a.txt")), action="restore", args={"entry": entry})
        assert (work / "a.txt").exists()
    run(body)


def test_directories_are_refused_including_empty():
    async def body(provider, recorder, work):
        (work / "empty").mkdir()
        (work / "full").mkdir()
        (work / "full" / "f.txt").write_text("x")
        await refused(provider, recorder, work, work / "empty", code="resource_unavailable")
        await refused(provider, recorder, work, work / "full", code="resource_unavailable")
    run(body)


def test_document_owning_media_and_media_assets_are_refused():
    def rows(work):
        return [{"receipt": {"provenance": provenance(work)}}]

    async def body(provider, recorder, work):
        record = MediaProvenance.from_receipt(provenance(work))
        media = work / record.resolved_media_dir()
        media.mkdir(parents=True)
        (work / "Note.md").write_text("![](x)")
        (media / "image.png").write_bytes(b"png")
        (media / "other.png").write_bytes(b"other")
        await refused(provider, recorder, work, work / "Note.md", code="managed_media_move_unsupported")
        await refused(provider, recorder, work, media / "image.png", code="managed_media_move_unsupported")
        await refused(provider, recorder, work, media / "other.png", code="managed_media_move_unsupported")
        (work / "plain.txt").write_text("ok")
        await provider.action(CONTEXT, origin_id=_origin(str(work / "plain.txt")), action="trash", args={})
        assert not (work / "plain.txt").exists()
    run(body, rows_for=rows)


def test_root_trash_folder_and_trash_contents_are_refused():
    async def body(provider, recorder, work):
        (work / "a.txt").write_text("a")
        await provider.action(CONTEXT, origin_id=_origin(str(work / "a.txt")), action="trash", args={})
        recorder.operations.clear()
        inside = next((work / ".odysseus-trash").iterdir())
        await refused(provider, recorder, work, work, code="resource_unavailable")
        await refused(provider, recorder, work, work / ".odysseus-trash", code="resource_unavailable")
        await refused(provider, recorder, work, inside, code="resource_unavailable")
    run(body, scoped=True)


def test_symlinks_and_paths_outside_the_root_are_refused():
    async def body(provider, recorder, work):
        root = work / "root"
        outside = work / "outside.txt"
        outside.write_text("outside")
        (root / "inside.txt").write_text("inside")
        os.symlink(root / "inside.txt", root / "link-in")
        os.symlink(outside, root / "link-out")
        await refused(provider, recorder, work, root / "link-in", code="resource_changed")
        await refused(provider, recorder, work, root / "link-out")  # realpath already leaves the root
        await refused(provider, recorder, work, outside, code="resource_unavailable")
        await refused(provider, recorder, work, root / ".." / "outside.txt")
    # Registry root is <work>/root: build it inside the body's work dir.
    base = Path(tempfile.mkdtemp(prefix="b2p1-"))
    work = (base / "work").resolve()
    (work / "root").mkdir(parents=True)
    client = OdysseusFilesClient("alice", binary=BINARY, transport="framed")
    recorder = Recorder(client)
    provider = HostFilesProvider(registry=RootRegistry(work / "root"), client_factory=lambda username, **kwargs: recorder,
                                 operation_store=Store())
    try:
        asyncio.run(body(provider, recorder, work))
    finally:
        client.close()
        shutil.rmtree(base, ignore_errors=True)


def test_stale_expected_revision_is_refused():
    async def body(provider, recorder, work):
        file = work / "a.txt"
        file.write_text("old")
        stale = (await provider.stat(CONTEXT, origin_id=_origin(str(file)))).revision
        file.write_text("newer bytes")
        await refused(provider, recorder, work, file, code="resource_changed", expected=stale)
    run(body)


def test_restore_refuses_a_forged_entry():
    async def body(provider, recorder, work):
        (work / "a.txt").write_text("a")
        (work / "secret.txt").write_text("secret")
        done = await provider.action(CONTEXT, origin_id=_origin(str(work / "a.txt")), action="trash", args={})
        recorder.operations.clear()
        entry = dict(done.action_receipt["receipt"]["trash_entry"])
        await refused(provider, recorder, work, work / "b.txt", action="restore", args={"entry": entry},
                      code="invalid_resource_request")
        await refused(provider, recorder, work, work / "a.txt", action="restore",
                      args={"entry": {**entry, "trashed_path": str(work / "secret.txt")}}, code="invalid_resource_request")
    run(body)


def test_ambiguous_native_reply_is_unknown_outcome_without_retry():
    class Fake:
        def __init__(self, real, behaviour):
            self.real, self.behaviour, self.operations = real, behaviour, []

        async def request(self, operation, path, payload=None):
            self.operations.append(operation)
            if operation == "trash":
                if self.behaviour == "lost":
                    raise RuntimeError("reply lost")
                return {"data": None}
            return await self.real.request(operation, path, payload)

    for behaviour in ("lost", "malformed"):
        base = Path(tempfile.mkdtemp(prefix="b2p1-"))
        work = (base / "work").resolve()
        work.mkdir()
        (work / "a.txt").write_text("a")
        client = OdysseusFilesClient("alice", binary=BINARY, transport="framed")
        fake = Fake(client, behaviour)
        provider = HostFilesProvider(registry=HostRegistry(), client_factory=lambda username, **kwargs: fake, operation_store=Store())
        try:
            try:
                asyncio.run(provider.action(CONTEXT, origin_id=_origin(str(work / "a.txt")), action="trash", args={}))
            except FilesFacadeError as exc:
                assert exc.code == "mutation_outcome_unknown", exc.code
            else:
                raise AssertionError("ambiguous reply accepted")
            assert fake.operations.count("trash") == 1
        finally:
            client.close()
            shutil.rmtree(base, ignore_errors=True)


def test_unsupported_platform_gate_refuses_before_any_call():
    async def body(provider, recorder, work):
        (work / "a.txt").write_text("a")
        HostFilesProvider._LOOSE_MANAGED_MOVE_READY = False
        try:
            await refused(provider, recorder, work, work / "a.txt", code="managed_media_move_unsupported")
        finally:
            HostFilesProvider._LOOSE_MANAGED_MOVE_READY = True
        assert recorder.operations == []
    run(body)


def test_neighbouring_rename_is_unaffected():
    async def body(provider, recorder, work):
        (work / "a.txt").write_text("a")
        await provider.action(CONTEXT, origin_id=_origin(str(work / "a.txt")), action="rename", args={"name": "b.txt"},
                              action_id="rename-1")
        assert (work / "b.txt").read_text() == "a" and not (work / "a.txt").exists()
        assert "move" in recorder.operations and "trash" not in recorder.operations
    run(body)

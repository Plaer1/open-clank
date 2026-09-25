"""Focused S14 tests: Host media attachment target and Loose Copal digest fix.

Covers the typed prepare/commit/status receipt for ``host_document`` targets,
the ``media/<stem>/<name>`` allocation, provenance persistence, and the binary
digest consistency fix in Loose Copal's ``put_asset_scoped``.
"""

from __future__ import annotations

import base64
import os

import pytest

from src.openclank.copal_loose import LooseCopalBridge
from src.openclank.files_facade import FilesFacadeError, ProviderContent, ProviderContext, ProviderResource
from src.openclank.media_attachment_targets import HostDocumentAttachmentTarget
from src.openclank.media_ownership import binary_digest, MediaProvenance, WorkspaceRootRecord


def _context(owner="alice", generation=1, *, workspace_id="default"):
    return ProviderContext(
        owner_subject_id=f"account-{owner}",
        owner_username=owner,
        policy_generation=generation,
        workspace_id=workspace_id,
    )


class _MemoryOperationStore:
    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    def get_operation(self, *, owner_subject_id, operation_id):
        row = self.rows.get((str(owner_subject_id), str(operation_id)))
        return dict(row) if row else None

    def record_operation(self, *, owner_subject_id, operation_id, request_digest, generation, receipt, phase=None):
        self.rows[(str(owner_subject_id), str(operation_id))] = {
            "digest": str(request_digest),
            "generation": int(generation),
            "receipt": dict(receipt),
        }


class _HostEntry:
    def __init__(self, origin_id, name, *, writable=True, revision=None, mime_type="text/markdown"):
        self.origin_id = origin_id
        self.name = name
        self.kind = "file"
        self.capabilities = ("stat", "read", "write", "download") if writable else ("stat", "read", "download")
        self.revision = revision or {"kind": "hostFingerprint", "value": "rev-1"}
        self.mime_type = mime_type
        self.size = 6
        self.provenance = {"domain": "host"}
        self.path = origin_id[len("host:") :] if origin_id.startswith("host:") else origin_id


class _HostProvider:
    name = "host"

    def __init__(self, entry, data=b"hello\n", *, extra_entries=()):
        self.entry = entry
        self.data = data
        self._entries = {entry.origin_id: entry}
        for item in extra_entries:
            self._entries[item.origin_id] = item

    async def stat(self, context, *, origin_id):
        entry = self._entries.get(origin_id)
        if entry is None:
            raise AssertionError(f"unexpected origin {origin_id}")
        return entry

    async def content(self, context, *, origin_id):
        data = self.data

        async def stream(start, length):
            yield data

        return ProviderContent(origin_id=origin_id, filename=self.entry.name, media_type=self.entry.mime_type, stream=stream, size=len(data))


class _SourceProvider:
    name = "source"

    def __init__(self, entry, data):
        self.entry = entry
        self.data = data

    async def stat(self, context, *, origin_id):
        return self.entry

    async def content(self, context, *, origin_id):
        data = self.data

        async def stream(start, length):
            yield data

        return ProviderContent(origin_id=origin_id, filename=self.entry.name, media_type="image/png", stream=stream, size=len(data))


def _png_entry(origin_id="host:/src/image.png", name="image.png"):
    return _HostEntry(origin_id, name, mime_type="image/png")


# ---------------------------------------------------------------------------
# Layout: media/<stem>/<collision-safe name>
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_target_writes_media_into_the_workspace_stem_layout(tmp_path):
    root = tmp_path / "foo"
    (root / "projects").mkdir(parents=True)
    document = root / "projects" / "design.md"
    document.write_text("# design\n", encoding="utf-8")

    store = _MemoryOperationStore()
    entry = _HostEntry(f"host:{document}", "design.md")
    provider = _HostProvider(entry)
    source = _SourceProvider(_png_entry(), b"\x89PNG-bytes")
    target = HostDocumentAttachmentTarget(provider=provider, operation_store=store, workspace_roots=lambda owner: [
        {"workspace_id": "workspace-1", "owner_subject_id": "account-alice", "canonical_root": str(root), "archived": False},
    ])

    result = await target.prepare_attachment(
        _context(),
        operation_id="attach-1",
        source={"resource_ref": "src"},
        target={"kind": "host_document", "resource_ref": f"host:{document}"},
        mode="embed",
        source_provider=source,
        source_origin_id="host:/src/image.png",
        source_entry=source.entry,
        target_origin_id=f"host:{document}",
    )
    assert result["target_identity"] == {"resource_ref": f"host:{document}", "kind": "host_document"}
    assert result["insertion"]["link_target"] == "media/projects/design/image.png"
    expected = root / "media" / "projects" / "design" / "image.png"
    assert expected.is_file()
    assert expected.read_bytes() == b"\x89PNG-bytes"
    assert result["provenance"]["origin"] == "workspace"
    assert result["provenance"]["canonical_root"] == str(root)
    assert result["provenance"]["asset_digest"] == binary_digest(b"\x89PNG-bytes")
    # Internal storage paths never reach the typed DTO.
    assert "asset_path" not in result
    assert "digest_hex" not in result
    assert "phase" not in result


@pytest.mark.asyncio
async def test_host_target_loose_document_uses_its_containing_folder(tmp_path):
    stuff = tmp_path / "stuff"
    stuff.mkdir()
    document = stuff / "note.md"
    document.write_text("note\n", encoding="utf-8")

    store = _MemoryOperationStore()
    entry = _HostEntry(f"host:{document}", "note.md")
    provider = _HostProvider(entry)
    source = _SourceProvider(_png_entry(), b"loose-bytes")
    target = HostDocumentAttachmentTarget(provider=provider, operation_store=store, workspace_roots=lambda owner: [])

    result = await target.prepare_attachment(
        _context(),
        operation_id="attach-loose",
        source={"resource_ref": "src"},
        target={"kind": "host_document", "resource_ref": f"host:{document}"},
        mode="embed",
        source_provider=source,
        source_origin_id="host:/src/image.png",
        source_entry=source.entry,
        target_origin_id=f"host:{document}",
    )
    assert result["insertion"]["link_target"] == "media/note/image.png"
    assert (stuff / "media" / "note" / "image.png").is_file()
    assert result["provenance"]["origin"] == "loose"
    assert result["provenance"]["canonical_root"] == str(stuff)


@pytest.mark.asyncio
async def test_host_target_allocates_collision_safe_names_for_clipboard_filenames(tmp_path):
    root = tmp_path / "foo"
    root.mkdir()
    document = root / "bar.md"
    document.write_text("bar\n", encoding="utf-8")
    media_dir = root / "media" / "bar"
    media_dir.mkdir(parents=True)
    (media_dir / "image.png").write_bytes(b"first")

    store = _MemoryOperationStore()
    entry = _HostEntry(f"host:{document}", "bar.md")
    provider = _HostProvider(entry)
    source = _SourceProvider(_png_entry(), b"second")
    target = HostDocumentAttachmentTarget(provider=provider, operation_store=store, workspace_roots=lambda owner: [
        {"workspace_id": "workspace-1", "owner_subject_id": "account-alice", "canonical_root": str(root), "archived": False},
    ])

    result = await target.prepare_attachment(
        _context(),
        operation_id="attach-collide",
        source={"resource_ref": "src"},
        target={"kind": "host_document", "resource_ref": f"host:{document}"},
        mode="embed",
        source_provider=source,
        source_origin_id="host:/src/image.png",
        source_entry=source.entry,
        target_origin_id=f"host:{document}",
    )
    assert result["asset"]["name"] == "image-1.png"
    assert result["insertion"]["link_target"] == "media/bar/image-1.png"
    assert (media_dir / "image.png").read_bytes() == b"first"
    assert (media_dir / "image-1.png").read_bytes() == b"second"


@pytest.mark.asyncio
async def test_same_stem_markdown_and_rust_share_a_directory_with_separate_ownership(tmp_path):
    root = tmp_path / "foo"
    root.mkdir()
    markdown = root / "foo.md"
    rust = root / "foo.rs"
    markdown.write_text("md\n", encoding="utf-8")
    rust.write_text("fn main() {}\n", encoding="utf-8")

    store = _MemoryOperationStore()
    markdown_entry = _HostEntry(f"host:{markdown}", "foo.md")
    rust_entry = _HostEntry(f"host:{rust}", "foo.rs")
    target = HostDocumentAttachmentTarget(
        provider=_HostProvider(markdown_entry, extra_entries=(rust_entry,)),
        operation_store=store,
        workspace_roots=lambda owner: [
            {"workspace_id": "workspace-1", "owner_subject_id": "account-alice", "canonical_root": str(root), "archived": False},
        ],
    )
    for index, (document, entry) in enumerate(((markdown, markdown_entry), (rust, rust_entry))):
        source = _SourceProvider(_png_entry(), f"bytes-{index}".encode())
        result = await target.prepare_attachment(
            _context(),
            operation_id=f"attach-stem-{index}",
            source={"resource_ref": "src"},
            target={"kind": "host_document", "resource_ref": f"host:{document}"},
            mode="embed",
            source_provider=source,
            source_origin_id="host:/src/image.png",
            source_entry=entry,
            target_origin_id=f"host:{document}",
        )
        assert result["insertion"]["link_target"].startswith("media/foo/")
        assert result["provenance"]["document_id"] == f"host:{document}"

    media_dir = root / "media" / "foo"
    assert (media_dir / "image.png").is_file()
    assert (media_dir / "image-1.png").is_file()
    documents = {row["receipt"]["provenance"]["document_id"] for row in store.rows.values() if "provenance" in row["receipt"]}
    assert len(documents) == 2


# ---------------------------------------------------------------------------
# Typed receipt, provenance persistence and recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_target_persists_provenance_and_recovers_after_crash(tmp_path):
    root = tmp_path / "foo"
    root.mkdir()
    document = root / "bar.md"
    document.write_text("bar\n", encoding="utf-8")

    store = _MemoryOperationStore()
    entry = _HostEntry(f"host:{document}", "bar.md")
    provider = _HostProvider(entry)
    source = _SourceProvider(_png_entry(), b"recover-bytes")
    target = HostDocumentAttachmentTarget(provider=provider, operation_store=store, workspace_roots=lambda owner: [
        {"workspace_id": "workspace-1", "owner_subject_id": "account-alice", "canonical_root": str(root), "archived": False},
    ])

    result = await target.prepare_attachment(
        _context(),
        operation_id="attach-recover",
        source={"resource_ref": "src"},
        target={"kind": "host_document", "resource_ref": f"host:{document}"},
        mode="embed",
        source_provider=source,
        source_origin_id="host:/src/image.png",
        source_entry=source.entry,
        target_origin_id=f"host:{document}",
    )
    assert result["action_receipt"]["durable"] is True

    # A fresh adapter instance must reconstruct the same typed preparation.
    recovered = await HostDocumentAttachmentTarget(
        provider=provider, operation_store=store, workspace_roots=lambda owner: []
    ).operation_status(_context(), operation_id="attach-recover")
    assert recovered is not None
    assert recovered["insertion"] == result["insertion"]
    assert recovered["target_identity"] == result["target_identity"]
    assert recovered["provenance"]["asset_digest"] == binary_digest(b"recover-bytes")
    # Recovery keeps the provenance-backed digest check honest: tampering the
    # asset invalidates the preparation instead of replaying a stale receipt.
    asset = root / "media" / "bar" / "image.png"
    asset.write_bytes(b"tampered")
    assert await target.operation_status(_context(), operation_id="attach-recover") is None


@pytest.mark.asyncio
async def test_host_target_is_idempotent_and_conflicts_on_reuse(tmp_path):
    root = tmp_path / "foo"
    root.mkdir()
    document = root / "bar.md"
    document.write_text("bar\n", encoding="utf-8")
    store = _MemoryOperationStore()
    entry = _HostEntry(f"host:{document}", "bar.md")
    provider = _HostProvider(entry)
    source = _SourceProvider(_png_entry(), b"same-bytes")
    target = HostDocumentAttachmentTarget(provider=provider, operation_store=store, workspace_roots=lambda owner: [])

    args = dict(
        operation_id="attach-idem",
        source={"resource_ref": "src"},
        target={"kind": "host_document", "resource_ref": f"host:{document}"},
        mode="embed",
        source_provider=source,
        source_origin_id="host:/src/image.png",
        source_entry=source.entry,
        target_origin_id=f"host:{document}",
    )
    first = await target.prepare_attachment(_context(), **args)
    second = await target.prepare_attachment(_context(), **args)
    assert second["preparation_receipt_id"] == first["preparation_receipt_id"]
    with pytest.raises(FilesFacadeError) as exc:
        await target.prepare_attachment(
            _context(),
            **{**args, "source": {"resource_ref": "other"}, "operation_id": "attach-idem"},
        )
    assert exc.value.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_host_target_never_pretends_synthetic_host_ids_are_copal_ids(tmp_path):
    root = tmp_path / "foo"
    root.mkdir()
    document = root / "bar.md"
    document.write_text("bar\n", encoding="utf-8")
    store = _MemoryOperationStore()
    entry = _HostEntry(f"host:{document}", "bar.md")
    source = _SourceProvider(_png_entry(), b"identity-bytes")
    target = HostDocumentAttachmentTarget(provider=_HostProvider(entry), operation_store=store, workspace_roots=lambda owner: [])

    result = await target.prepare_attachment(
        _context(),
        operation_id="attach-identity",
        source={"resource_ref": "src"},
        target={"kind": "host_document", "resource_ref": f"host:{document}"},
        mode="link",
        source_provider=source,
        source_origin_id="host:/src/image.png",
        source_entry=source.entry,
        target_origin_id=f"host:{document}",
    )
    assert result["target_identity"]["kind"] == "host_document"
    assert "copal" not in str(result["target_identity"]).lower()
    assert result["source_identity"]["provider"] == "source"


@pytest.mark.asyncio
async def test_host_target_rejects_stale_target_and_unwritable_targets(tmp_path):
    root = tmp_path / "foo"
    root.mkdir()
    document = root / "bar.md"
    document.write_text("bar\n", encoding="utf-8")
    store = _MemoryOperationStore()
    source = _SourceProvider(_png_entry(), b"payload")

    readonly = _HostEntry(f"host:{document}", "bar.md", writable=False)
    target = HostDocumentAttachmentTarget(provider=_HostProvider(readonly), operation_store=store, workspace_roots=lambda owner: [])
    with pytest.raises(FilesFacadeError) as exc:
        await target.prepare_attachment(
            _context(),
            operation_id="attach-ro",
            source={"resource_ref": "src"},
            target={"kind": "host_document", "resource_ref": f"host:{document}"},
            mode="embed",
            source_provider=source,
            source_origin_id="host:/src/image.png",
            source_entry=source.entry,
            target_origin_id=f"host:{document}",
        )
    assert exc.value.code == "resource_unavailable"

    stale = _HostEntry(f"host:{document}", "bar.md", revision={"kind": "hostFingerprint", "value": "rev-1"})
    target = HostDocumentAttachmentTarget(provider=_HostProvider(stale), operation_store=store, workspace_roots=lambda owner: [])
    with pytest.raises(FilesFacadeError) as exc:
        await target.prepare_attachment(
            _context(),
            operation_id="attach-stale",
            source={"resource_ref": "src"},
            target={"kind": "host_document", "resource_ref": f"host:{document}", "expected_revision": {"kind": "hostFingerprint", "value": "rev-2"}},
            mode="embed",
            source_provider=source,
            source_origin_id="host:/src/image.png",
            source_entry=source.entry,
            target_origin_id=f"host:{document}",
        )
    assert exc.value.code == "resource_changed"


@pytest.mark.asyncio
async def test_host_target_creates_no_files_on_source_failure(tmp_path):
    root = tmp_path / "foo"
    root.mkdir()
    document = root / "bar.md"
    document.write_text("bar\n", encoding="utf-8")
    store = _MemoryOperationStore()
    entry = _HostEntry(f"host:{document}", "bar.md")

    class _FailingSource:
        name = "source"

        async def stat(self, context, *, origin_id):
            return _png_entry()

        async def content(self, context, *, origin_id):
            raise RuntimeError("source unavailable")

    target = HostDocumentAttachmentTarget(provider=_HostProvider(entry), operation_store=store, workspace_roots=lambda owner: [])
    with pytest.raises(FilesFacadeError):
        await target.prepare_attachment(
            _context(),
            operation_id="attach-fail",
            source={"resource_ref": "src"},
            target={"kind": "host_document", "resource_ref": f"host:{document}"},
            mode="embed",
            source_provider=_FailingSource(),
            source_origin_id="host:/src/image.png",
            source_entry=_png_entry(),
            target_origin_id=f"host:{document}",
        )
    assert not (root / "media").exists()


# ---------------------------------------------------------------------------
# Loose Copal binary digest consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loose_copal_asset_head_hashes_original_bytes(tmp_path):
    bridge = LooseCopalBridge(tmp_path)
    payload = b"caf\xe9 \x80 binary \xff"
    result = await bridge.call("put_asset_scoped", {
        "owner": "alice",
        "workspace_id": "default",
        "name": "image.png",
        "ext": "png",
        "base64": base64.b64encode(payload).decode("ascii"),
    })
    head = result["doc"]["head"]
    assert head == binary_digest(payload)
    # The verification path (raw-byte sha256) must agree with the stored head.
    assert head.split(":", 2)[1] == __import__("hashlib").sha256(payload).hexdigest()
    # Re-encoding through Latin-1/UTF-8 must NOT be treated as the same asset.
    legacy = f"sha256:{__import__('hashlib').sha256(payload.decode('latin1').encode('utf-8')).hexdigest()}:{len(payload.decode('latin1').encode('utf-8'))}"
    assert head != legacy


@pytest.mark.asyncio
async def test_loose_copal_asset_reuse_requires_matching_byte_digest(tmp_path):
    bridge = LooseCopalBridge(tmp_path)
    payload = b"\xff\xfe non-ascii"
    args = {
        "owner": "alice",
        "workspace_id": "default",
        "name": "image.png",
        "ext": "png",
        "base64": base64.b64encode(payload).decode("ascii"),
    }
    first = await bridge.call("put_asset_scoped", dict(args))
    again = await bridge.call("put_asset_scoped", dict(args))
    assert again["doc"]["id"] == first["doc"]["id"]
    with pytest.raises(Exception):
        await bridge.call("put_asset_scoped", {**args, "base64": base64.b64encode(b"different bytes").decode("ascii")})


# ---------------------------------------------------------------------------
# Loose Copal rename repairs app-known media references
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loose_copal_rename_moves_media_dir_and_repairs_links(tmp_path):
    bridge = LooseCopalBridge(tmp_path)
    created = await bridge.call("create", {
        "owner": "alice",
        "workspace_id": "default",
        "name": "Draft.md",
        "kind": "markdown",
        "content": "Hello ![img](media/Draft/image.png) end\n",
    })
    doc_id = created["doc"]["id"]
    payload = b"png-bytes"
    await bridge.call("put_asset_scoped", {
        "owner": "alice",
        "workspace_id": "default",
        "name": "media/Draft/image.png",
        "ext": "png",
        "base64": base64.b64encode(payload).decode("ascii"),
    })
    other = await bridge.call("create", {
        "owner": "alice",
        "workspace_id": "default",
        "name": "Other.md",
        "kind": "markdown",
        "content": "see ![img](media/Draft/image.png) and ![doc](Draft.md)\n",
    })
    result = await bridge.call("rename", {"owner": "alice", "workspace_id": "default", "id": doc_id, "name": "Renamed.md"})
    assert result["outcome"] == "committed"
    assert result["doc"]["name"] == "Renamed.md"
    renamed_media = list(tmp_path.rglob("media/Renamed/image.png"))
    draft_media = list(tmp_path.rglob("media/Draft/image.png"))
    assert renamed_media and renamed_media[0].is_file()
    assert not draft_media
    other_text = next(tmp_path.rglob("Other.md")).read_text(encoding="utf-8")
    assert "media/Renamed/image.png" in other_text
    assert "Renamed.md" in other_text
    assert "media/Draft/image.png" not in other_text
    assert "Draft.md" not in other_text


@pytest.mark.asyncio
async def test_loose_copal_rename_reports_protected_reference_conflict(tmp_path):
    bridge = LooseCopalBridge(tmp_path)
    created = await bridge.call("create", {
        "owner": "alice",
        "workspace_id": "default",
        "name": "Draft.md",
        "kind": "markdown",
        "content": "Hello\n",
    })
    doc_id = created["doc"]["id"]
    await bridge.call("put_asset_scoped", {
        "owner": "alice",
        "workspace_id": "default",
        "name": "media/Draft/image.png",
        "ext": "png",
        "base64": base64.b64encode(b"png-bytes").decode("ascii"),
    })
    # A read-only document referencing the media is protected: rename must
    # refuse with a concrete conflict rather than break the link.
    locked = await bridge.call("create", {
        "owner": "alice",
        "workspace_id": "default",
        "name": "Locked.md",
        "kind": "markdown",
        "content": "see ![img](media/Draft/image.png)\n",
        "read_only": True,
    })
    with pytest.raises(Exception) as exc:
        await bridge.call("rename", {"owner": "alice", "workspace_id": "default", "id": doc_id, "name": "Renamed.md"})
    message = str(exc.value)
    assert "protected_reference" in message or "move left unapplied" in message
    # Nothing was relocated.
    assert list(tmp_path.rglob("media/Draft/image.png"))
    assert not list(tmp_path.rglob("media/Renamed"))
    assert list(tmp_path.rglob("Draft.md"))
    assert locked["doc"]["name"] == "Locked.md"

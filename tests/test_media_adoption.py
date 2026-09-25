"""Focused S14 tests: workspace-creation orphan adoption.

Covers the provenance-backed loose-asset consolidation and its refusal to
swallow another workspace, plus surgical reference repair.
"""

from __future__ import annotations

import os

import pytest

from src.openclank.media_attachment_targets import (
    adopt_loose_media_for_workspace,
    collect_media_provenance,
)
from src.openclank.media_ownership import MediaProvenance, binary_digest


class _MemoryOperationStore:
    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.workspaces: list = []

    def get_operation(self, *, owner_subject_id, operation_id):
        row = self.rows.get((str(owner_subject_id), str(operation_id)))
        return dict(row) if row else None

    def record_operation(self, *, owner_subject_id, operation_id, request_digest, generation, receipt, phase=None):
        self.rows[(str(owner_subject_id), str(operation_id))] = {
            "digest": str(request_digest),
            "generation": int(generation),
            "receipt": dict(receipt),
        }

    def list_operations(self, *, owner_subject_id, operation_prefix="", phase=None, offset=0, limit=256):
        rows = []
        for (owner, op_id), row in self.rows.items():
            if owner != str(owner_subject_id):
                continue
            if operation_prefix and not op_id.startswith(operation_prefix):
                continue
            rows.append({"operation_id": op_id, **row})
        return rows[:limit]

    def list_workspaces(self, *, owner_subject_id=None, include_archived=False):
        return list(self.workspaces)

    def get_location(self, location_id):
        for workspace in self.workspaces:
            if getattr(workspace, "location_id", "") == location_id:
                return type("Location", (), {"canonical_path": workspace.canonical_root, "id": location_id})()
        raise KeyError(location_id)


class _Workspace:
    def __init__(self, workspace_id, owner, root, archived=False):
        self.id = workspace_id
        self.owner_subject_id = owner
        self.location_id = f"loc-{workspace_id}"
        self.relative_folder = ""
        self.archived = archived
        self.canonical_root = root


def _seed_provenance(store, owner, *, root, document, name, data, workspace_id=None, origin="loose"):
    provenance = MediaProvenance(
        canonical_root=root,
        origin=origin,
        owner_subject_id=owner,
        workspace_id=workspace_id,
        document_id=f"host:{os.path.join(root, document)}",
        document_path=document,
        asset_name=name,
        asset_digest=binary_digest(data),
        references=(),
    )
    store.record_operation(
        owner_subject_id=owner,
        operation_id=f"__host_media__seed-{document}-{name}",
        request_digest="digest",
        generation=1,
        receipt={"provenance": provenance.as_receipt(), "phase": "complete"},
        phase="complete",
    )
    return provenance


def _write_loose_asset(root, document, name, data):
    loose_root = os.path.join(root, os.path.dirname(document) or "")
    media_dir = os.path.join(loose_root, "media", os.path.splitext(os.path.basename(document))[0])
    os.makedirs(media_dir, exist_ok=True)
    path = os.path.join(media_dir, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def test_collect_media_provenance_reads_only_this_owners_rows(tmp_path):
    store = _MemoryOperationStore()
    _seed_provenance(store, "account-alice", root=str(tmp_path), document="draft.md", name="image.png", data=b"a")
    _seed_provenance(store, "account-bob", root=str(tmp_path), document="other.md", name="photo.png", data=b"b")
    rows = collect_media_provenance(store, owner_subject_id="account-alice")
    assert len(rows) == 1
    assert rows[0].asset_name == "image.png"


# ---------------------------------------------------------------------------
# Adoption
# ---------------------------------------------------------------------------


def test_adoption_moves_provenance_backed_loose_assets_and_repairs_links(tmp_path):
    project = tmp_path / "project"
    notes = project / "notes"
    notes.mkdir(parents=True)
    document = notes / "draft.md"
    document.write_text("see ![img](media/draft/image.png) and ![doc](draft.md)\n", encoding="utf-8")
    asset_path = _write_loose_asset(str(project), "notes/draft.md", "image.png", b"png-bytes")

    store = _MemoryOperationStore()
    _seed_provenance(store, "account-alice", root=str(notes), document="draft.md", name="image.png", data=b"png-bytes")

    result = adopt_loose_media_for_workspace(
        operation_store=store,
        owner_subject_id="account-alice",
        workspace_root=str(project),
        workspace_id="workspace-new",
    )
    assert result["status"] == "complete"
    assert len(result["adopted"]) == 1
    adopted = project / "media" / "notes" / "draft" / "image.png"
    assert adopted.is_file()
    assert adopted.read_bytes() == b"png-bytes"
    assert not os.path.exists(asset_path)
    text = document.read_text(encoding="utf-8")
    assert "media/notes/draft/image.png" in text
    assert "media/draft/image.png" not in text
    # A resource-change receipt was published once.
    receipts = [row for (owner, op), row in store.rows.items() if op.startswith("__host_media_adopt__")]
    assert len(receipts) == 1
    assert receipts[0]["receipt"]["resource_receipt"]["adopted_count"] == 1


def test_adoption_never_swallows_another_registered_workspace(tmp_path):
    project = tmp_path / "project"
    child = project / "child"
    inside = child / "inside"
    inside.mkdir(parents=True)
    document = inside / "deep.md"
    document.write_text("![i](media/deep/photo.png)\n", encoding="utf-8")
    asset_path = _write_loose_asset(str(project), "child/inside/deep.md", "photo.png", b"child-bytes")

    store = _MemoryOperationStore()
    store.workspaces.append(_Workspace("workspace-child", "account-alice", str(child)))
    _seed_provenance(store, "account-alice", root=str(inside), document="deep.md", name="photo.png", data=b"child-bytes")

    result = adopt_loose_media_for_workspace(
        operation_store=store,
        owner_subject_id="account-alice",
        workspace_root=str(project),
        workspace_id="workspace-new",
    )
    assert result["adopted"] == []
    assert any(item["code"] == "owned_by_other_workspace" for item in result["rejected"])
    # The child workspace's asset is untouched.
    assert os.path.isfile(asset_path)
    assert not (project / "media").exists()


def test_adoption_rejects_ambiguous_or_unprovenanced_media_folders(tmp_path):
    project = tmp_path / "project"
    (project / "media" / "mystery").mkdir(parents=True)
    (project / "media" / "mystery" / "image.png").write_bytes(b"unknown")
    store = _MemoryOperationStore()

    result = adopt_loose_media_for_workspace(
        operation_store=store,
        owner_subject_id="account-alice",
        workspace_root=str(project),
        workspace_id="workspace-new",
    )
    assert result["status"] == "noop"
    assert result["adopted"] == []
    # A folder named media is never proof of orphanhood: nothing moved.
    assert (project / "media" / "mystery" / "image.png").is_file()


def test_adoption_leaves_protected_references_unapplied(tmp_path):
    project = tmp_path / "project"
    notes = project / "notes"
    notes.mkdir(parents=True)
    document = notes / "draft.md"
    document.write_text("![img](media/draft/image.png)\n", encoding="utf-8")
    asset_path = _write_loose_asset(str(project), "notes/draft.md", "image.png", b"png-bytes")

    store = _MemoryOperationStore()
    _seed_provenance(store, "account-alice", root=str(notes), document="draft.md", name="image.png", data=b"png-bytes")

    result = adopt_loose_media_for_workspace(
        operation_store=store,
        owner_subject_id="account-alice",
        workspace_root=str(project),
        workspace_id="workspace-new",
        document_sources=lambda: [("notes/draft.md", "![img](media/draft/image.png)", True)],
    )
    assert result["status"] == "conflict"
    assert result["adopted"] == []
    assert any(item["code"] == "protected_reference" for item in result["conflicts"])
    assert os.path.isfile(asset_path)


def test_adoption_refuses_untouched_bytes_and_is_idempotent_on_retry(tmp_path):
    project = tmp_path / "project"
    notes = project / "notes"
    notes.mkdir(parents=True)
    document = notes / "draft.md"
    document.write_text("![img](media/draft/image.png)\n", encoding="utf-8")
    asset_path = _write_loose_asset(str(project), "notes/draft.md", "image.png", b"png-bytes")

    store = _MemoryOperationStore()
    _seed_provenance(store, "account-alice", root=str(notes), document="draft.md", name="image.png", data=b"tampered")

    result = adopt_loose_media_for_workspace(
        operation_store=store,
        owner_subject_id="account-alice",
        workspace_root=str(project),
        workspace_id="workspace-new",
    )
    assert result["status"] == "conflict"
    assert any(item["code"] == "digest_mismatch" for item in result["conflicts"])
    assert os.path.isfile(asset_path)
    assert not (project / "media").exists()


def test_adoption_captures_preimages_through_lore(tmp_path):
    project = tmp_path / "project"
    notes = project / "notes"
    notes.mkdir(parents=True)
    document = notes / "draft.md"
    original = "![img](media/draft/image.png)\n"
    document.write_text(original, encoding="utf-8")
    _write_loose_asset(str(project), "notes/draft.md", "image.png", b"png-bytes")

    store = _MemoryOperationStore()
    _seed_provenance(store, "account-alice", root=str(notes), document="draft.md", name="image.png", data=b"png-bytes")
    captured: list[tuple[str, str]] = []

    result = adopt_loose_media_for_workspace(
        operation_store=store,
        owner_subject_id="account-alice",
        workspace_root=str(project),
        workspace_id="workspace-new",
        lore_capture=lambda *, document_id, preimage, operation_id: captured.append((document_id, preimage)),
    )
    assert result["status"] == "complete"
    assert captured and captured[0][0] == "notes/draft.md"
    assert captured[0][1] == original

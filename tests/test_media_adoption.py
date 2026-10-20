"""Focused S14 tests: workspace-creation orphan adoption.

Covers the provenance-backed loose-asset consolidation and its refusal to
swallow another workspace, plus surgical reference repair.
"""

from __future__ import annotations

import os
import threading
import multiprocessing
import queue
import time

import pytest

from src.openclank.media_attachment_targets import (
    adopt_loose_media_for_workspace,
    collect_media_provenance,
)
from src.openclank.media_ownership import MediaOwnershipError, MediaProvenance, binary_digest
from src.openclank.file_policy import FilePolicyRepository


def _production_adoption_child(db_path, root, phase, result_queue):
    repository = FilePolicyRepository(db_path)
    try:
        hook = (lambda current, *_: os._exit(0) if current == phase else None) if phase and phase != "terminal" else None
        terminal_hook = (lambda *_: os._exit(0)) if phase == "terminal" else None
        result = adopt_loose_media_for_workspace(
            operation_store=repository,
            owner_subject_id="account-alice",
            workspace_root=root,
            workspace_id="workspace-new",
            operation_id="adopt-production",
            phase_hook=hook,
            post_terminal_hook=terminal_hook,
            lease_ms=1000,
        )
        result_queue.put({"status": result.get("status"), "receipt": result.get("resource_receipt")})
    except BaseException as error:
        code = getattr(error, "code", None)
        if code == "operation_pending":
            result_queue.put({"status": "pending", "code": code})
        else:
            result_queue.put({"error": repr(error), "code": code})


def _production_adoption_fixture(tmp_path):
    root = tmp_path / "project"
    notes = root / "notes"
    source = notes / "media" / "draft" / "image.png"
    notes.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    (notes / "draft.md").write_text("![img](media/draft/image.png)\n", encoding="utf-8")
    source.write_bytes(b"production-image")
    db_path = str(tmp_path / "policy.sqlite")
    repository = FilePolicyRepository(db_path)
    provenance = MediaProvenance(canonical_root=str(notes), origin="loose", owner_subject_id="account-alice", workspace_id=None, document_id="notes/draft.md", document_path="draft.md", asset_name="image.png", asset_digest=binary_digest(b"production-image"))
    repository.record_operation(owner_subject_id="account-alice", operation_id="__host_media__production-seed", request_digest="seed", generation=0, receipt={"provenance": provenance.as_receipt()}, phase="complete")
    return root, source, db_path


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


def test_adoption_fails_closed_when_workspace_registry_read_raises(tmp_path):
    project = tmp_path / "project"
    notes = project / "notes"
    notes.mkdir(parents=True)
    _write_loose_asset(str(project), "notes/draft.md", "image.png", b"png-bytes")

    store = _MemoryOperationStore()
    _seed_provenance(store, "account-alice", root=str(notes), document="draft.md", name="image.png", data=b"png-bytes")

    def _registry_down(**kwargs):
        raise RuntimeError("workspace registry is unavailable")

    store.list_workspaces = _registry_down

    # A failed registry read is not an empty registry: refuse rather than
    # absorb a possible neighbor.
    with pytest.raises(MediaOwnershipError) as exc:
        adopt_loose_media_for_workspace(
            operation_store=store,
            owner_subject_id="account-alice",
            workspace_root=str(project),
            workspace_id="workspace-new",
        )
    assert exc.value.code == "provider_unavailable"
    # Nothing was relocated.
    assert os.path.isfile(os.path.join(str(notes), "media", "draft", "image.png"))
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


def test_adoption_distinguishes_identically_named_assets_by_document_location(tmp_path):
    from pathlib import Path
    from src.openclank.media_ownership import scan_references

    project = tmp_path / "project"
    project.mkdir()
    store = _MemoryOperationStore()
    for folder, content in (("a", b"image-a"), ("b", b"image-b")):
        child = project / folder
        child.mkdir()
        (child / "note.md").write_text("![img](media/note/image.png)\n", encoding="utf-8")
        _write_loose_asset(str(child), "note.md", "image.png", content)
        _seed_provenance(store, "account-alice", root=str(child), document="note.md", name="image.png", data=content)
        # Real attachment operations have distinct IDs even for equal names.
        key = ("account-alice", "__host_media__seed-note.md-image.png")
        store.rows[(key[0], key[1] + "-" + folder)] = store.rows.pop(key)

    result = adopt_loose_media_for_workspace(operation_store=store, owner_subject_id="account-alice",
        workspace_root=str(project), workspace_id="workspace-new")
    assert result["status"] == "complete"
    for folder, content in (("a", b"image-a"), ("b", b"image-b")):
        document = project / folder / "note.md"
        reference = scan_references(f"{folder}/note.md", document.read_text(encoding="utf-8"))[0]
        resolved = (document.parent / reference.target).resolve()
        assert resolved == (project / "media" / folder / "note" / "image.png").resolve()
        assert Path(resolved).read_bytes() == content


def test_adoption_preserves_an_existing_destination_and_original_links(tmp_path):
    project = tmp_path / "project"
    notes = project / "notes"
    notes.mkdir(parents=True)
    original = "![img](media/draft/image.png)\n"
    document = notes / "draft.md"
    document.write_text(original, encoding="utf-8")
    source = _write_loose_asset(str(notes), "draft.md", "image.png", b"source")
    destination = project / "media/notes/draft/image.png"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing-unrelated-image")
    store = _MemoryOperationStore()
    _seed_provenance(store, "account-alice", root=str(notes), document="draft.md", name="image.png", data=b"source")

    result = adopt_loose_media_for_workspace(operation_store=store, owner_subject_id="account-alice",
        workspace_root=str(project), workspace_id="workspace-new")
    assert destination.read_bytes() == b"existing-unrelated-image"
    assert os.path.isfile(source)
    assert document.read_text(encoding="utf-8") == original
    assert result["status"] == "conflict"


def test_adoption_does_not_publish_success_when_operation_storage_fails(tmp_path):
    project = tmp_path / "project"
    notes = project / "notes"
    notes.mkdir(parents=True)
    original = "![img](media/draft/image.png)\n"
    document = notes / "draft.md"
    document.write_text(original, encoding="utf-8")
    source = _write_loose_asset(str(notes), "draft.md", "image.png", b"source")
    store = _MemoryOperationStore()
    _seed_provenance(store, "account-alice", root=str(notes), document="draft.md", name="image.png", data=b"source")

    def unavailable(**kwargs):
        raise OSError("fixture operation store unavailable")

    store.record_operation = unavailable
    result = None
    try:
        result = adopt_loose_media_for_workspace(operation_store=store, owner_subject_id="account-alice",
            workspace_root=str(project), workspace_id="workspace-new")
    except (OSError, MediaOwnershipError):
        pass
    assert result is None or result["status"] != "complete"
    assert os.path.isfile(source)
    assert document.read_text(encoding="utf-8") == original
    assert not (project / "media/notes/draft/image.png").exists()


def test_adoption_retry_returns_the_same_completed_operation(tmp_path):
    project = tmp_path / "project"
    notes = project / "notes"
    notes.mkdir(parents=True)
    (notes / "draft.md").write_text("![img](media/draft/image.png)\n", encoding="utf-8")
    _write_loose_asset(str(notes), "draft.md", "image.png", b"source")
    store = _MemoryOperationStore()
    _seed_provenance(store, "account-alice", root=str(notes), document="draft.md", name="image.png", data=b"source")
    args = dict(operation_store=store, owner_subject_id="account-alice", workspace_root=str(project),
        workspace_id="workspace-new", operation_id="adopt-once")
    first = adopt_loose_media_for_workspace(**args)
    second = adopt_loose_media_for_workspace(**args)
    assert first["status"] == "complete"
    assert second == first


def test_adoption_same_operation_concurrent_publish_has_one_replay(tmp_path):
    project = tmp_path / "project"
    notes = project / "notes"
    notes.mkdir(parents=True)
    (notes / "draft.md").write_text("![img](media/draft/image.png)\n", encoding="utf-8")
    _write_loose_asset(str(notes), "draft.md", "image.png", b"source")
    store = _MemoryOperationStore()
    _seed_provenance(store, "account-alice", root=str(notes), document="draft.md", name="image.png", data=b"source")
    args = dict(operation_store=store, owner_subject_id="account-alice", workspace_root=str(project), workspace_id="workspace-new", operation_id="adopt-concurrent")
    results: list[dict] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(adopt_loose_media_for_workspace(**args))
        except BaseException as error:  # pragma: no cover - assertion below reports it
            errors.append(error)

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(results) == 2
    assert results[0] == results[1]
    assert results[0]["status"] == "complete"
    assert (project / "media/notes/draft/image.png").read_bytes() == b"source"


@pytest.mark.parametrize("phase", ["destination_published", "documents_rewritten", "source_removed", "sources_removed"])
def test_production_process_replay_after_publication_phase(tmp_path, phase):
    root, source, db_path = _production_adoption_fixture(tmp_path)
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    child = context.Process(target=_production_adoption_child, args=(db_path, str(root), phase, result_queue))
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 0
    time.sleep(1.2)
    replay = adopt_loose_media_for_workspace(
        operation_store=FilePolicyRepository(db_path), owner_subject_id="account-alice",
        workspace_root=str(root), workspace_id="workspace-new", operation_id="adopt-production", lease_ms=1000,
    )
    assert replay["status"] == "complete"
    assert (root / "media/notes/draft/image.png").read_bytes() == b"production-image"
    assert "media/notes/draft/image.png" in (root / "notes/draft.md").read_text(encoding="utf-8")
    assert not source.exists()
    with pytest.raises(queue.Empty):
        result_queue.get(timeout=0.2)


def test_production_simultaneous_processes_have_one_publisher(tmp_path):
    root, source, db_path = _production_adoption_fixture(tmp_path)
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    children = [context.Process(target=_production_adoption_child, args=(db_path, str(root), None, result_queue)) for _ in range(2)]
    for child in children:
        child.start()
    for child in children:
        child.join(timeout=10)
        assert child.exitcode == 0
    results = [result_queue.get(timeout=2) for _ in children]
    assert any(result.get("status") == "complete" for result in results), results
    assert all("error" not in result for result in results), results
    assert all(result.get("status") in {"complete", "pending"} for result in results), results
    assert any(result.get("status") == "pending" for result in results) or all(result.get("status") == "complete" for result in results), results
    published = root / "media/notes/draft/image.png"
    assert published.exists(), (results, [str(path.relative_to(root)) for path in root.rglob("*")])
    assert published.read_bytes() == b"production-image"
    assert not source.exists()


def test_file_policy_live_lease_fences_stale_terminal_write(tmp_path):
    repository = FilePolicyRepository(str(tmp_path / "policy.sqlite"))
    operation = "__host_media_adopt__fenced"
    digest = "request-fenced"
    staged = {"operation_id": "fenced", "phase": "staged", "workspace_id": "workspace-new"}
    assert repository.claim_operation(
        owner_subject_id="account-alice", operation_id=operation,
        request_digest=digest, generation=0, receipt=staged,
        lease_owner="publisher-a", lease_ms=1000,
    ) is None

    live = repository.claim_operation(
        owner_subject_id="account-alice", operation_id=operation,
        request_digest=digest, generation=0, receipt=staged,
        lease_owner="publisher-b", lease_ms=1000,
    )
    assert live is not None
    assert live["receipt"]["lease_owner"] == "publisher-a"
    with pytest.raises(Exception) as caught:
        repository.record_operation(
            owner_subject_id="account-alice", operation_id=operation,
            request_digest=digest, generation=0,
            receipt={"operation_id": "fenced", "phase": "complete", "lease_owner": "publisher-b", "resource_receipt": {"status": "complete"}},
            phase="complete",
        )
    assert getattr(caught.value, "code", None) == "operation_pending"

    # Once the lease expires, recovery owns a new lease; the stale original
    # claimant remains fenced from terminal publication.
    time.sleep(1.1)
    assert repository.claim_operation(
        owner_subject_id="account-alice", operation_id=operation,
        request_digest=digest, generation=0, receipt=staged,
        lease_owner="publisher-b", lease_ms=1000,
    ) is None
    with pytest.raises(Exception) as caught:
        repository.record_operation(
            owner_subject_id="account-alice", operation_id=operation,
            request_digest=digest, generation=0,
            receipt={"operation_id": "fenced", "phase": "complete", "lease_owner": "publisher-a", "resource_receipt": {"status": "complete"}},
            phase="complete",
        )
    assert getattr(caught.value, "code", None) == "operation_pending"


def test_production_lost_terminal_response_replays_same_receipt(tmp_path, monkeypatch):
    root, source, db_path = _production_adoption_fixture(tmp_path)
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    child = context.Process(target=_production_adoption_child, args=(db_path, str(root), "terminal", result_queue))
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 0
    with pytest.raises(queue.Empty):
        result_queue.get(timeout=0.2)
    import src.openclank.media_attachment_targets as adoption_module
    writes = {"atomic": 0, "remove": 0}
    original_atomic = adoption_module._write_atomic
    original_remove = adoption_module.os.remove
    def count_atomic(*args, **kwargs):
        writes["atomic"] += 1
        return original_atomic(*args, **kwargs)
    def count_remove(*args, **kwargs):
        writes["remove"] += 1
        return original_remove(*args, **kwargs)
    monkeypatch.setattr(adoption_module, "_write_atomic", count_atomic)
    monkeypatch.setattr(adoption_module.os, "remove", count_remove)
    replay = adopt_loose_media_for_workspace(
        operation_store=FilePolicyRepository(db_path), owner_subject_id="account-alice",
        workspace_root=str(root), workspace_id="workspace-new", operation_id="adopt-production", lease_ms=1000,
    )
    assert replay["status"] == "complete"
    assert replay["resource_receipt"]["phase"] == "complete"
    assert not source.exists()
    assert writes == {"atomic": 0, "remove": 0}


def test_production_multi_asset_manifest_and_replay_identity(tmp_path):
    root, source_a, db_path = _production_adoption_fixture(tmp_path)
    notes = root / "notes"
    source_b = notes / "second" / "media" / "note" / "image.png"
    source_b.parent.mkdir(parents=True)
    source_b.write_bytes(b"second-production-image")
    (notes / "second" / "note.md").write_text("![img](media/note/image.png)\n", encoding="utf-8")
    repository = FilePolicyRepository(db_path)
    provenance = MediaProvenance(canonical_root=str(notes / "second"), origin="loose", owner_subject_id="account-alice", workspace_id=None, document_id="second/note.md", document_path="note.md", asset_name="image.png", asset_digest=binary_digest(b"second-production-image"))
    repository.record_operation(owner_subject_id="account-alice", operation_id="__host_media__production-seed-second", request_digest="seed-second", generation=0, receipt={"provenance": provenance.as_receipt()}, phase="complete")
    result = adopt_loose_media_for_workspace(operation_store=repository, owner_subject_id="account-alice", workspace_root=str(root), workspace_id="workspace-new", operation_id="adopt-multi")
    assert result["status"] == "complete"
    assert (root / "media/notes/draft/image.png").read_bytes() == b"production-image"
    assert (root / "media/notes/second/note/image.png").read_bytes() == b"second-production-image"
    assert not source_a.exists() and not source_b.exists()
    assert "media/notes/second/note/image.png" in (notes / "second" / "note.md").read_text(encoding="utf-8")
    provenance = collect_media_provenance(repository, owner_subject_id="account-alice")
    assert any(item.origin == "workspace" and item.workspace_id == "workspace-new" for item in provenance)
    replay = adopt_loose_media_for_workspace(operation_store=FilePolicyRepository(db_path), owner_subject_id="account-alice", workspace_root=str(root), workspace_id="workspace-new", operation_id="adopt-multi")
    assert replay == result


@pytest.mark.parametrize("phase", ["destination_published", "documents_rewritten", "source_removed", "sources_removed"])
def test_production_multi_asset_crash_replay_keeps_all_effects(tmp_path, phase):
    root, source_a, db_path = _production_adoption_fixture(tmp_path)
    notes = root / "notes"
    source_b = notes / "second" / "media" / "note" / "image.png"
    source_b.parent.mkdir(parents=True)
    source_b.write_bytes(b"second-production-image")
    (notes / "second" / "note.md").write_text("![img](media/note/image.png)\n", encoding="utf-8")
    repository = FilePolicyRepository(db_path)
    provenance = MediaProvenance(canonical_root=str(notes / "second"), origin="loose", owner_subject_id="account-alice", workspace_id=None, document_id="second/note.md", document_path="note.md", asset_name="image.png", asset_digest=binary_digest(b"second-production-image"))
    repository.record_operation(owner_subject_id="account-alice", operation_id="__host_media__production-seed-second", request_digest="seed-second", generation=0, receipt={"provenance": provenance.as_receipt()}, phase="complete")
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    child = context.Process(target=_production_adoption_child, args=(db_path, str(root), phase, result_queue))
    child.start(); child.join(timeout=10)
    assert child.exitcode == 0
    time.sleep(1.2)
    replay = adopt_loose_media_for_workspace(operation_store=FilePolicyRepository(db_path), owner_subject_id="account-alice", workspace_root=str(root), workspace_id="workspace-new", operation_id="adopt-production", lease_ms=1000)
    assert replay["status"] == "complete"
    assert (root / "media/notes/draft/image.png").read_bytes() == b"production-image"
    assert (root / "media/notes/second/note/image.png").read_bytes() == b"second-production-image"
    assert not source_a.exists() and not source_b.exists()


def test_production_corrupt_receipt_cannot_traverse_or_rewrite(tmp_path):
    root, _source, db_path = _production_adoption_fixture(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    repository = FilePolicyRepository(db_path)
    repository.record_operation(owner_subject_id="account-alice", operation_id="__host_media_adopt__corrupt", request_digest="corrupt", generation=0, receipt={"operation_id": "corrupt", "workspace_id": "workspace-new", "workspace_root": str(root), "phase": "destination_published", "destination": "../../outside.txt", "source": "../../outside.txt", "digest": "sha256:bad", "documents": []}, phase="destination_published")
    with pytest.raises(Exception) as caught:
        adopt_loose_media_for_workspace(operation_store=repository, owner_subject_id="account-alice", workspace_root=str(root), workspace_id="workspace-new", operation_id="corrupt")
    assert getattr(caught.value, "code", None) == "idempotency_conflict"
    assert outside.read_text(encoding="utf-8") == "keep"


def test_terminal_replay_rejects_changed_workspace_identity(tmp_path):
    root, _source, db_path = _production_adoption_fixture(tmp_path)
    first = adopt_loose_media_for_workspace(operation_store=FilePolicyRepository(db_path), owner_subject_id="account-alice", workspace_root=str(root), workspace_id="workspace-new", operation_id="adopt-identity")
    assert first["status"] == "complete"
    with pytest.raises(Exception) as caught:
        adopt_loose_media_for_workspace(operation_store=FilePolicyRepository(db_path), owner_subject_id="account-alice", workspace_root=str(root), workspace_id="workspace-other", operation_id="adopt-identity")
    assert getattr(caught.value, "code", None) == "idempotency_conflict"


def test_unlink_failure_is_retained_duplicate_and_replay_is_stable(tmp_path, monkeypatch):
    root, source, db_path = _production_adoption_fixture(tmp_path)
    original_remove = os.remove
    def refuse_source(path):
        if os.path.realpath(path) == os.path.realpath(source):
            raise OSError("simulated unlink failure")
        return original_remove(path)
    monkeypatch.setattr(os, "remove", refuse_source)
    first = adopt_loose_media_for_workspace(operation_store=FilePolicyRepository(db_path), owner_subject_id="account-alice", workspace_root=str(root), workspace_id="workspace-new", operation_id="adopt-unlink")
    assert first["status"] == "conflict"
    assert first["resource_receipt"]["recovery"] == "retained_duplicate"
    assert source.exists()
    replay = adopt_loose_media_for_workspace(operation_store=FilePolicyRepository(db_path), owner_subject_id="account-alice", workspace_root=str(root), workspace_id="workspace-new", operation_id="adopt-unlink")
    assert replay == first


def test_history_capture_completes_only_after_terminal_adoption(tmp_path):
    from src.openclank.history_capture import HistoryContext
    class FakeHistory:
        def __init__(self):
            self.events = []
        def prepare(self, envelope, *, content, fingerprint):
            self.events.append("prepare")
        def record_live(self, action_id, payload):
            self.events.append("live")
            return {"status": "ok"}
        def complete(self, action_id, *, content, fingerprint):
            self.events.append("complete")
            return {"status": "complete"}
        def abort(self, action_id):
            self.events.append("abort")
    project = tmp_path / "project"
    notes = project / "notes"
    notes.mkdir(parents=True)
    (notes / "draft.md").write_text("![img](media/draft/image.png)\n", encoding="utf-8")
    _write_loose_asset(str(notes), "draft.md", "image.png", b"history-image")
    store = _MemoryOperationStore()
    _seed_provenance(store, "account-alice", root=str(notes), document="draft.md", name="image.png", data=b"history-image")
    client = FakeHistory()
    context = HistoryContext(actor_id="agent", account_id="account-alice", workspace_id="workspace-new", roots=(str(project),), client=client)
    result = adopt_loose_media_for_workspace(operation_store=store, owner_subject_id="account-alice", workspace_root=str(project), workspace_id="workspace-new", operation_id="history-success", history_context=context)
    assert result["status"] == "complete"
    assert client.events == ["prepare", "live", "complete"]


def test_provenance_row_failure_replays_missing_rows_before_terminal(tmp_path):
    class FailingProvenanceStore(_MemoryOperationStore):
        def __init__(self):
            super().__init__()
            self.fail = True
        def record_operation(self, **kwargs):
            if self.fail and str(kwargs.get("operation_id") or "").endswith("-1"):
                raise RuntimeError("injected provenance row failure")
            return super().record_operation(**kwargs)
    project = tmp_path / "project"
    notes = project / "notes"
    notes.mkdir(parents=True)
    (notes / "draft.md").write_text("![img](media/draft/image.png)\n", encoding="utf-8")
    _write_loose_asset(str(notes), "draft.md", "image.png", b"one")
    (notes / "second.md").write_text("![img](media/second/image.png)\n", encoding="utf-8")
    _write_loose_asset(str(notes), "second.md", "image.png", b"two")
    store = FailingProvenanceStore()
    _seed_provenance(store, "account-alice", root=str(notes), document="draft.md", name="image.png", data=b"one")
    _seed_provenance(store, "account-alice", root=str(notes), document="second.md", name="image.png", data=b"two")
    with pytest.raises(RuntimeError, match="injected provenance"):
        adopt_loose_media_for_workspace(operation_store=store, owner_subject_id="account-alice", workspace_root=str(project), workspace_id="workspace-new", operation_id="provenance-retry")
    store.fail = False
    result = adopt_loose_media_for_workspace(operation_store=store, owner_subject_id="account-alice", workspace_root=str(project), workspace_id="workspace-new", operation_id="provenance-retry")
    assert result["status"] == "complete"
    active = collect_media_provenance(store, owner_subject_id="account-alice")
    assert len([item for item in active if item.origin == "workspace" and item.workspace_id == "workspace-new"]) == 2
    assert not [item for item in active if item.origin == "loose" and item.asset_name == "image.png"]


def test_history_incomplete_recovery_result_is_exactly_stable(tmp_path):
    store = _MemoryOperationStore()
    operation_id = "history-recovery-stable"
    stored = {
        "operation_id": operation_id,
        "workspace_id": "workspace-new",
        "workspace_root": str(tmp_path),
        "phase": "source_removal_pending",
        "history_expected": True,
        "history_state": "pending",
        "adopted": [{"asset_name": "image.png", "digest": "sha256:abc:3"}],
        "conflicts": [{"code": "prior_conflict", "reason": "kept"}],
        "rejected": [{"code": "rejected", "reason": "kept"}],
        "destinations": [],
        "documents": [],
        "provenance_records": [{"canonical_root": str(tmp_path), "origin": "workspace", "owner_subject_id": "account-alice", "workspace_id": "workspace-new", "document_id": "host:doc", "document_path": "doc.md", "asset_name": "image.png", "asset_digest": "sha256:abc:3", "references": []}],
        "resource_receipt": {"action_id": "adopt-history-recovery-stable", "status": "pending", "phase": "source_removal_pending", "durable": False},
    }
    store.record_operation(owner_subject_id="account-alice", operation_id=f"__host_media_adopt__{operation_id}", request_digest="stable-digest", generation=0, receipt=stored, phase="source_removal_pending")
    first = adopt_loose_media_for_workspace(operation_store=store, owner_subject_id="account-alice", workspace_root=str(tmp_path), workspace_id="workspace-new", operation_id=operation_id)
    second = adopt_loose_media_for_workspace(operation_store=store, owner_subject_id="account-alice", workspace_root=str(tmp_path), workspace_id="workspace-new", operation_id=operation_id)
    assert first == second
    assert [item["code"] for item in first["conflicts"]].count("history_capture_failed") == 1
    persisted = store.get_operation(owner_subject_id="account-alice", operation_id=f"__host_media_adopt__{operation_id}")["receipt"]
    assert persisted["conflicts"] == first["conflicts"]
    assert persisted["provenance_records"] == stored["provenance_records"]

import json
import os
from pathlib import Path

import pytest

from routes.personal_routes import (
    PersonalRagLifecycle,
    PersonalRagLifecycleError,
    _write_personal_upload,
)
from src.frankenmemory_rag import FrankenmemoryRAG
from src.rag_manager import RAGManager


class _Manager:
    def __init__(self):
        self.renames = []
        self.removals = []

    def rename_directory(self, old, new, path_map=None):
        self.renames.append((old, new, dict(path_map or {})))

    def remove_directory(self, directory, owner=None, *, remove_rag=True):
        self.removals.append((directory, owner, remove_rag))


class _Rag:
    def __init__(self):
        self.renames = []
        self.purges = []
        self.fail_purge = False

    def rename_owner(self, old, new, path_map=None, path_prefixes=None):
        self.renames.append((old, new, dict(path_map or {}), list(path_prefixes or [])))
        return {"updated": 1}

    @staticmethod
    def owner_inventory(_owner):
        empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        return {
            "available": True, "row_count": 0, "canonical_row_count": 0,
            "derived_row_count": 0, "digest": empty,
            "canonical_semantic_digest": empty, "derived_digest": empty,
            "counts": {},
        }

    def purge_owner(self, owner):
        self.purges.append(owner)
        if self.fail_purge:
            raise RuntimeError("rag unavailable")
        return {"removed": 1}


def _lifecycle(tmp_path):
    manager = _Manager()
    rag = _Rag()
    root = tmp_path / "personal"
    lifecycle = PersonalRagLifecycle(
        upload_root=str(root),
        personal_docs_manager=manager,
        rag_manager=rag,
    )
    return lifecycle, manager, rag


def test_personal_rag_preview_is_read_only_then_reconcile_persists_and_compensates(tmp_path):
    lifecycle, manager, rag = _lifecycle(tmp_path)
    alice = Path(lifecycle._owner_dir("alice"))
    alice.mkdir()
    (alice / "private.txt").write_text("private body", encoding="utf-8")

    manifest = lifecycle.preview_owner_rename("alice", "deleted:stable")
    assert not Path(lifecycle.journal_path).exists()
    assert str(tmp_path) not in json.dumps(manifest)
    staged = lifecycle.stage_to_tombstone("alice", "deleted:stable", manifest)
    journal = json.loads(Path(lifecycle.journal_path).read_text(encoding="utf-8"))
    assert manifest["path_map_token"] in journal["operations"]
    assert staged["source"]["count"] == 0
    assert staged["target"]["count"] == 1
    assert manager.renames

    restored = lifecycle.compensate("alice", "deleted:stable", manifest)
    assert restored["source"] == manifest["source"]
    assert restored["target"]["count"] == 0
    assert (Path(lifecycle._owner_dir("alice")) / "private.txt").read_text() == "private body"


def test_personal_rag_rename_rejects_target_conflict_and_symlinks(tmp_path):
    lifecycle, _manager, _rag = _lifecycle(tmp_path)
    alice = Path(lifecycle._owner_dir("alice"))
    target = Path(lifecycle._owner_dir("target"))
    alice.mkdir()
    target.mkdir()
    (alice / "a.txt").write_text("a")
    (target / "b.txt").write_text("b")
    with pytest.raises(PersonalRagLifecycleError, match="target"):
        lifecycle.preview_owner_rename("alice", "target")

    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    os.symlink(outside, alice / "link.txt")
    with pytest.raises(PersonalRagLifecycleError, match="symlink"):
        lifecycle.owner_inventory("alice")


def test_direct_upload_writer_refuses_existing_path_and_symlink(tmp_path):
    path = tmp_path / "upload.txt"
    _write_personal_upload(str(path), b"first")
    with pytest.raises(FileExistsError):
        _write_personal_upload(str(path), b"replacement")
    assert path.read_bytes() == b"first"

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    link = tmp_path / "link.txt"
    os.symlink(outside, link)
    with pytest.raises(FileExistsError):
        _write_personal_upload(str(link), b"replacement")
    assert outside.read_bytes() == b"outside"


def test_personal_rag_purge_retains_bytes_until_rag_purge_succeeds_and_preserves_bob(tmp_path):
    lifecycle, manager, rag = _lifecycle(tmp_path)
    alice = Path(lifecycle._owner_dir("alice"))
    bob = Path(lifecycle._owner_dir("bob"))
    alice.mkdir()
    bob.mkdir()
    (alice / "a.txt").write_text("alice secret")
    (bob / "b.txt").write_text("bob secret")
    expected = lifecycle.preview_owner_purge("alice")
    token = "d" * 32
    rag.fail_purge = True

    with pytest.raises(RuntimeError, match="rag unavailable"):
        lifecycle.purge_owner("alice", expected=expected, operation_token=token)

    quarantine = Path(lifecycle.upload_root) / ".owner-lifecycle-bytes" / token / "a.txt"
    assert quarantine.read_text() == "alice secret"
    assert (bob / "b.txt").read_text() == "bob secret"

    rag.fail_purge = False
    receipt = lifecycle.purge_owner("alice", expected=expected, operation_token=token)
    assert receipt["before"] == expected
    assert receipt["after"]["count"] == 0
    assert not quarantine.exists()
    assert (bob / "b.txt").read_text() == "bob secret"
    assert manager.removals[-1][1] == "alice"
    assert str(tmp_path) not in json.dumps(receipt)


def test_personal_rag_lifecycle_uses_real_manager_wrapper_and_tracks_generations(tmp_path):
    backend = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    manager = RAGManager.__new__(RAGManager)
    manager.vector_rag = backend
    lifecycle = PersonalRagLifecycle(
        upload_root=str(tmp_path / "personal"),
        personal_docs_manager=_Manager(),
        rag_manager=manager,
    )
    alice = Path(lifecycle._owner_dir("alice"))
    bob = Path(lifecycle._owner_dir("bob"))
    alice.mkdir()
    bob.mkdir()
    alice_file = alice / "alice.txt"
    bob_file = bob / "bob.txt"
    alice_file.write_text("alice private knowledge")
    bob_file.write_text("bob private knowledge")
    assert manager.add_document("alice private knowledge", {"owner": "alice", "source": str(alice_file)})
    assert manager.add_document("bob private knowledge", {"owner": "bob", "source": str(bob_file)})

    manifest = lifecycle.preview_owner_rename("alice", "deleted:stable")
    assert manifest["source"]["rag"]["canonical_row_count"] >= 3
    assert "generations" in manifest["source"]["rag"]["counts"]
    staged = lifecycle.stage_to_tombstone("alice", "deleted:stable", manifest)
    assert staged["source"]["count"] == 0
    assert staged["target"]["rag"]["canonical_row_count"] == manifest["source"]["rag"]["canonical_row_count"]
    assert manager.owner_inventory("bob")["canonical_row_count"] >= 3

    expected = lifecycle.preview_owner_purge("deleted:stable")
    purged = lifecycle.purge_owner("deleted:stable", expected=expected, operation_token="f" * 32)
    assert purged["after"]["count"] == 0
    assert manager.owner_inventory("bob")["canonical_row_count"] >= 3
    assert bob_file.read_text() == "bob private knowledge"


def test_account_mode_uses_one_canonical_db_rename_and_purge_then_rewrites_paths(tmp_path):
    backend = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    manager = RAGManager.__new__(RAGManager)
    manager.vector_rag = backend
    docs = _Manager()
    lifecycle = PersonalRagLifecycle(
        upload_root=str(tmp_path / "personal"),
        personal_docs_manager=docs,
        rag_manager=manager,
        canonical_rag_managed_externally=True,
    )
    alice = Path(lifecycle._owner_dir("alice"))
    bob = Path(lifecycle._owner_dir("bob"))
    alice.mkdir()
    bob.mkdir()
    alice_file = alice / "alice.txt"
    bob_file = bob / "bob.txt"
    alice_file.write_text("alice private knowledge", encoding="utf-8")
    bob_file.write_text("bob private knowledge", encoding="utf-8")
    assert manager.add_document(
        "alice private knowledge", {"owner": "alice", "source": str(alice_file)}
    )
    assert manager.add_document(
        "bob private knowledge", {"owner": "bob", "source": str(bob_file)}
    )
    bob_before = manager.owner_inventory("bob")

    rename_calls = 0
    real_rename = manager.rename_owner

    def counted_rename(*args, **kwargs):
        nonlocal rename_calls
        rename_calls += 1
        return real_rename(*args, **kwargs)

    manager.rename_owner = counted_rename
    manifest = lifecycle.preview_owner_rename("alice", "deleted:stable")
    assert manifest["source"]["rag"]["managed_externally"] is True
    assert manifest["source"]["rag"]["row_count"] == 0

    # This is the canonical Memory-provider owner transaction.  The Personal
    # lifecycle that follows must move bytes and rewrite paths, never invoke a
    # second owner transition.
    manager.rename_owner("alice", "deleted:stable")
    staged = lifecycle.stage_to_tombstone("alice", "deleted:stable", manifest)
    assert rename_calls == 1
    assert staged["source"]["count"] == 0
    target_file = Path(lifecycle._owner_dir("deleted:stable")) / "alice.txt"
    assert target_file.read_text(encoding="utf-8") == "alice private knowledge"
    sources = backend.list_sources(owner="deleted:stable")
    assert [row["source_uri"] for row in sources] == [str(target_file)]
    assert manager.owner_inventory("alice")["row_count"] == 0
    assert manager.owner_inventory("bob") == bob_before

    purge_calls = 0
    real_purge = manager.purge_owner

    def counted_purge(*args, **kwargs):
        nonlocal purge_calls
        purge_calls += 1
        return real_purge(*args, **kwargs)

    manager.purge_owner = counted_purge
    expected = lifecycle.preview_owner_purge("deleted:stable")
    personal_receipt = lifecycle.purge_owner(
        "deleted:stable", expected=expected, operation_token="a" * 32
    )
    assert purge_calls == 0
    assert personal_receipt["after"]["count"] == 0
    assert docs.removals[-1][2] is False
    manager.purge_owner("deleted:stable")
    assert purge_calls == 1
    assert manager.owner_inventory("deleted:stable")["row_count"] == 0
    assert manager.owner_inventory("bob") == bob_before
    assert bob_file.read_text(encoding="utf-8") == "bob private knowledge"


def test_account_mode_compensation_retries_external_path_rewrite_after_bytes_restore(tmp_path):
    backend = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    manager = RAGManager.__new__(RAGManager)
    manager.vector_rag = backend
    docs = _Manager()
    lifecycle = PersonalRagLifecycle(
        upload_root=str(tmp_path / "personal"),
        personal_docs_manager=docs,
        rag_manager=manager,
        canonical_rag_managed_externally=True,
    )
    alice = Path(lifecycle._owner_dir("alice"))
    alice.mkdir()
    alice_file = alice / "alice.txt"
    alice_file.write_text("alice private knowledge", encoding="utf-8")
    assert manager.add_document(
        "alice private knowledge",
        {"owner": "alice", "source": str(alice_file)},
    )

    manifest = lifecycle.preview_owner_rename("alice", "deleted:stable")
    manager.rename_owner("alice", "deleted:stable")
    lifecycle.stage_to_tombstone("alice", "deleted:stable", manifest)
    tombstone_file = Path(lifecycle._owner_dir("deleted:stable")) / "alice.txt"
    assert backend.list_sources(owner="deleted:stable")[0]["source_uri"] == str(
        tombstone_file
    )

    # Whole-account compensation restores canonical Memory ownership first.
    # Personal then restores bytes and paths under that already-restored owner.
    manager.rename_owner("deleted:stable", "alice")
    real_rewrite = manager.rewrite_owner_paths
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("injected path rewrite interruption")
        return real_rewrite(*args, **kwargs)

    manager.rewrite_owner_paths = fail_once
    with pytest.raises(RuntimeError, match="injected path rewrite interruption"):
        lifecycle.compensate("alice", "deleted:stable", manifest)

    restored_file = Path(lifecycle._owner_dir("alice")) / "alice.txt"
    assert restored_file.read_text(encoding="utf-8") == "alice private knowledge"
    assert backend.list_sources(owner="alice")[0]["source_uri"] == str(tombstone_file)

    receipt = lifecycle.compensate("alice", "deleted:stable", manifest)
    assert receipt["state"] == "restored"
    assert attempts == 2
    assert backend.list_sources(owner="alice")[0]["source_uri"] == str(restored_file)
    # A completed retry is itself replay-safe and performs no extra rewrite.
    replay = lifecycle.compensate("alice", "deleted:stable", manifest)
    assert replay["state"] == "restored"
    assert attempts == 2


def test_nonempty_rag_state_fails_closed_without_rename_api(tmp_path):
    class InventoryOnly:
        def owner_inventory(self, owner):
            empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            rows = 3 if owner == "alice" else 0
            return {
                "available": True, "row_count": rows, "canonical_row_count": rows,
                "derived_row_count": 0, "digest": "nonempty" if rows else empty,
                "canonical_semantic_digest": "semantic" if rows else empty,
                "derived_digest": empty, "counts": {"documents": rows},
            }

    lifecycle = PersonalRagLifecycle(
        upload_root=str(tmp_path / "personal"), rag_manager=InventoryOnly()
    )
    alice = Path(lifecycle._owner_dir("alice"))
    alice.mkdir()
    (alice / "a.txt").write_text("a")
    manifest = lifecycle.preview_owner_rename("alice", "target")
    with pytest.raises(PersonalRagLifecycleError, match="cannot be renamed"):
        lifecycle.reconcile_owner_rename("alice", "target", manifest)
    assert (alice / "a.txt").read_text() == "a"

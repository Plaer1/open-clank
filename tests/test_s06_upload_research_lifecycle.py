import json
import os
from pathlib import Path
import asyncio
import multiprocessing
import time

import pytest

from src.research_handler import ResearchHandler
from src.upload_handler import (
    UploadHandler,
    UploadOwnerLifecycleError,
)


def _concurrent_upload_index_writer(base_dir: str, upload_dir: str, key: str, barrier) -> None:
    handler = UploadHandler(base_dir, upload_dir)
    barrier.wait()
    with handler._index_guard():
        index_path = os.path.join(upload_dir, "uploads.json")
        current = dict(handler._load_upload_index(fail_on_error=True))
        time.sleep(0.05)
        current[key] = {"owner": key, "id": (key[0] * 32) + ".txt", "hash": key}
        handler._atomic_write_json(index_path, current, sync_backup=True)


def _upload_handler(tmp_path: Path) -> UploadHandler:
    root = tmp_path / "uploads"
    root.mkdir()
    return UploadHandler(str(tmp_path), str(root))


def _row(owner: str, upload_id: str, path: Path, digest: str) -> dict:
    return {
        "owner": owner,
        "id": upload_id,
        "path": str(path),
        "hash": digest,
        "uploaded_at": "2026-08-26T00:00:00",
    }


def test_upload_lifecycle_strict_rename_is_idempotent_and_content_free(tmp_path):
    handler = _upload_handler(tmp_path)
    file_path = Path(handler.upload_dir) / ("a" * 32 + ".txt")
    file_path.write_text("secret body", encoding="utf-8")
    handler._atomic_write_json(
        os.path.join(handler.upload_dir, "uploads.json"),
        {"alice:h1": _row("alice", file_path.name, file_path, "h1")},
    )

    manifest = handler.preview_owner_rename("alice", "deleted:stable")
    receipt = handler.stage_owner_to_tombstone("alice", "deleted:stable", manifest)
    replay = handler.stage_owner_to_tombstone("alice", "deleted:stable", manifest)

    assert receipt == replay
    assert receipt["source"]["count"] == 0
    assert receipt["target"]["count"] == 1
    assert "secret" not in json.dumps(receipt)
    assert str(tmp_path) not in json.dumps(receipt)


def test_upload_lifecycle_rejects_source_target_conflict(tmp_path):
    handler = _upload_handler(tmp_path)
    first = Path(handler.upload_dir) / ("a" * 32 + ".txt")
    second = Path(handler.upload_dir) / ("b" * 32 + ".txt")
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    handler._atomic_write_json(
        os.path.join(handler.upload_dir, "uploads.json"),
        {
            "alice:h1": _row("alice", first.name, first, "h1"),
            "target:h2": _row("target", second.name, second, "h2"),
        },
    )
    with pytest.raises(UploadOwnerLifecycleError, match="target owner"):
        handler.preview_owner_rename("alice", "target")


def test_upload_lifecycle_empty_store_can_preview_and_purge(tmp_path):
    handler = _upload_handler(tmp_path)
    expected = handler.preview_owner_purge("alice")
    assert expected["count"] == 0
    receipt = handler.purge_owner("alice", expected=expected, operation_token="e" * 32)
    assert receipt["state"] == "purged"
    assert receipt["after"]["count"] == 0


def test_upload_index_guard_serializes_cross_process_writers(tmp_path):
    handler = _upload_handler(tmp_path)
    handler._atomic_write_json(os.path.join(handler.upload_dir, "uploads.json"), {})
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    processes = [
        context.Process(
            target=_concurrent_upload_index_writer,
            args=(handler.base_dir, handler.upload_dir, key, barrier),
        )
        for key in ("alice", "bob")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    with handler._index_guard():
        assert set(handler._load_upload_index(fail_on_error=True)) == {"alice", "bob"}


def test_upload_purge_reconciles_after_metadata_commit_and_preserves_shared_bytes(tmp_path, monkeypatch):
    handler = _upload_handler(tmp_path)
    unique = Path(handler.upload_dir) / ("a" * 32 + ".txt")
    shared = Path(handler.upload_dir) / ("b" * 32 + ".txt")
    unique.write_bytes(b"alice only")
    shared.write_bytes(b"shared")
    handler._atomic_write_json(
        os.path.join(handler.upload_dir, "uploads.json"),
        {
            "alice:u": _row("alice", unique.name, unique, "u"),
            "alice:s": _row("alice", shared.name, shared, "s"),
            "bob:s": _row("bob", shared.name, shared, "s"),
        },
    )
    expected = handler.owner_inventory("alice")
    token = "c" * 32
    real_remove = os.remove
    failed = False

    def fail_quarantine_once(path):
        nonlocal failed
        if ".owner-lifecycle" in str(path) and not failed:
            failed = True
            raise OSError("simulated crash window")
        return real_remove(path)

    monkeypatch.setattr(os, "remove", fail_quarantine_once)
    with pytest.raises(OSError, match="crash window"):
        handler.purge_owner_lifecycle("alice", expected=expected, operation_token=token)

    # Metadata is durably gone, but the journal still retains the staged bytes.
    assert handler.owner_inventory("alice")["count"] == 0
    assert handler._find_upload_path(unique.name) is None
    assert shared.read_bytes() == b"shared"
    monkeypatch.setattr(os, "remove", real_remove)
    receipt = handler.purge_owner_lifecycle("alice", expected=expected, operation_token=token)
    assert receipt["state"] == "purged"
    assert receipt["before"] == expected
    assert handler.owner_inventory("bob")["count"] == 1
    assert shared.exists()
    assert not unique.exists()
    assert str(tmp_path) not in json.dumps(receipt)


class _Task:
    def __init__(self):
        self.cancelled = False

    def done(self):
        return self.cancelled

    def cancel(self):
        self.cancelled = True


def _research_handler(fence_root=None) -> ResearchHandler:
    handler = ResearchHandler.__new__(ResearchHandler)
    handler._active_tasks = {}
    if fence_root is not None:
        handler._owner_lifecycle_fence_root = Path(fence_root)
    return handler


def test_research_lifecycle_fences_purge_and_preserves_other_owner():
    handler = _research_handler()
    alice_task = _Task()
    bob_task = _Task()
    handler._active_tasks = {
        "a": {"owner": "alice", "status": "running", "task": alice_task},
        "b": {"owner": "bob", "status": "running", "task": bob_task},
    }

    receipt = handler.purge_owner("alice")

    assert receipt["before"]["count"] == 1
    assert receipt["after"]["count"] == 0
    assert alice_task.cancelled is True
    assert bob_task.cancelled is False
    assert list(handler._active_tasks) == ["b"]


def test_research_rename_is_idempotent_fences_source_and_rejects_split_state():
    handler = _research_handler()
    handler._active_tasks = {"a": {"owner": "alice", "status": "running", "task": _Task()}}
    assert handler.rename_owner("alice", "alice2") == 1
    assert handler.rename_owner("alice", "alice2") == 0
    assert handler._active_tasks["a"]["owner"] == "alice2"

    conflicting = _research_handler()
    conflicting._active_tasks = {
        "a": {"owner": "alice", "status": "running", "task": _Task()},
        "b": {"owner": "alice2", "status": "running", "task": _Task()},
    }
    with pytest.raises(RuntimeError, match="both contain"):
        conflicting.rename_owner("alice", "alice2")


def test_research_preview_is_read_only_and_reconcile_replays():
    handler = _research_handler()
    handler._active_tasks = {
        "alice-task": {"owner": "alice", "status": "cancelled"},
        "bob-task": {"owner": "bob", "status": "cancelled"},
    }
    before = dict(handler._active_tasks["alice-task"])

    manifest = handler.preview_owner_rename("alice", "deleted:stable")
    assert handler._active_tasks["alice-task"] == before
    receipt = handler.reconcile_owner_rename(
        "alice",
        "deleted:stable",
        manifest,
    )
    replay = handler.reconcile_owner_rename(
        "alice",
        "deleted:stable",
        manifest,
    )

    assert receipt["state"] == replay["state"] == "staged"
    assert handler.owner_inventory("alice")["count"] == 0
    assert handler.owner_inventory("deleted:stable")["count"] == 1
    assert handler.owner_inventory("bob")["count"] == 1


def test_research_fence_rejects_new_work_before_task_creation():
    handler = _research_handler()
    handler.fence_owner("alice")

    async def attempt():
        with pytest.raises(RuntimeError, match="lifecycle operation"):
            handler.start_research("valid-session", "query", owner="alice")

    asyncio.run(attempt())
    assert handler._active_tasks == {}


def test_research_fence_is_visible_to_another_worker(tmp_path):
    first = _research_handler(tmp_path / "fences")
    second = _research_handler(tmp_path / "fences")
    first.fence_owner("alice")

    async def attempt():
        with pytest.raises(RuntimeError, match="lifecycle operation"):
            second.start_research("cross-worker", "query", owner="alice")

    asyncio.run(attempt())
    first.release_owner_fence("alice")
    assert second._owner_is_fenced("alice") is False

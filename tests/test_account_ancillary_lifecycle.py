"""Adversarial coverage for account-owned sidecars and SQL file children."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, FilesImageResource, PublishedFile, Session
from src.openclank.account_ancillary_lifecycle import (
    AccountAncillaryLifecycle,
    AccountAncillaryLifecycleError,
    AccountAncillaryPaths,
)


def _trash_key(owner: str) -> str:
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:20]


def _scheduled_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE scheduled_emails (
                id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                status TEXT NOT NULL,
                body TEXT
            );
            CREATE TABLE email_summaries (
                message_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                summary TEXT,
                PRIMARY KEY (message_id, owner)
            );
            """
        )


@pytest.fixture()
def ancillary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    paths = AccountAncillaryPaths(
        data_dir=tmp_path,
        gallery_root=tmp_path / "generated_images",
        published_root=tmp_path / "uploads" / ".published",
        background_jobs_file=tmp_path / "bg_jobs.json",
        background_jobs_dir=tmp_path / "bg_jobs",
        scheduled_email_db=tmp_path / "scheduled_emails.db",
        agent_todos_dir=tmp_path / "agent_todos",
        file_trash_dir=tmp_path / "file-trash",
        journal_path=tmp_path / ".account-lifecycle" / "ancillary.json",
    )
    _scheduled_db(paths.scheduled_email_db)
    lifecycle = AccountAncillaryLifecycle(session_factory=factory, paths=paths)
    yield lifecycle, factory, paths
    engine.dispose()


def _seed_owner(lifecycle, factory, paths, owner: str, ordinal: int) -> dict[str, Path]:
    image_id = f"gallery-{ordinal}"
    gallery_name = f"image-{ordinal}.png"
    published_id = f"{ordinal + 1:032x}"
    session_id = f"session-{ordinal}"
    with factory() as db:
        db.add(
            Session(
                id=session_id,
                name=f"chat {ordinal}",
                endpoint_url="http://localhost",
                model="test",
                owner=owner,
            )
        )
        db.add(
            _files_image(
                id=image_id,
                filename=gallery_name,
                prompt="private prompt",
                owner=owner,
            )
        )
        db.add(
            PublishedFile(
                id=published_id,
                owner=owner,
                filename="private.txt",
                mime_type="text/plain",
                size=7,
                sha256="a" * 64,
                source="agent",
            )
        )
        db.commit()

    gallery_path = paths.gallery_root / gallery_name
    gallery_path.parent.mkdir(parents=True, exist_ok=True)
    gallery_path.write_bytes(f"gallery-{owner}".encode())
    published_path = paths.published_root / published_id[:2] / published_id
    published_path.parent.mkdir(parents=True, exist_ok=True)
    published_path.write_bytes(f"published-{owner}".encode())
    todo_path = paths.agent_todos_dir / f"{session_id}.json"
    todo_path.parent.mkdir(parents=True, exist_ok=True)
    todo_path.write_text(json.dumps({"todos": [f"secret-{owner}"]}), encoding="utf-8")

    jobs = {}
    if paths.background_jobs_file.exists():
        jobs = json.loads(paths.background_jobs_file.read_text(encoding="utf-8"))
    job_id = f"job-{ordinal}"
    jobs[job_id] = {
        "owner": owner,
        "status": "done",
        "session_id": session_id,
        "command": f"private command {owner}",
    }
    paths.background_jobs_file.write_text(json.dumps(jobs), encoding="utf-8")
    paths.background_jobs_dir.mkdir(parents=True, exist_ok=True)
    job_log = paths.background_jobs_dir / f"{job_id}.log"
    job_log.write_text(f"private output {owner}", encoding="utf-8")
    job_spec = paths.background_jobs_dir / f"{job_id}.spec.json"
    job_spec.write_text(json.dumps({"owner": owner, "workspace": "/private"}), encoding="utf-8")

    note = paths.data_dir / f"note_pings_{owner}.json"
    note.write_text(json.dumps({"private": owner}), encoding="utf-8")
    email_state = paths.data_dir / f"email_urgency_state_{owner}.json"
    email_state.write_text(json.dumps({"private": owner}), encoding="utf-8")
    trash = paths.file_trash_dir / _trash_key(owner)
    trash.mkdir(parents=True, exist_ok=True)
    (trash / "recoverable.txt").write_text(f"trash-{owner}", encoding="utf-8")

    with sqlite3.connect(paths.scheduled_email_db) as connection:
        connection.execute(
            "INSERT INTO scheduled_emails(id,owner,status,body) VALUES(?,?,?,?)",
            (f"mail-{ordinal}", owner, "sent", f"secret mail {owner}"),
        )
        connection.execute(
            "INSERT INTO email_summaries(message_id,owner,summary) VALUES(?,?,?)",
            (f"message-{ordinal}", owner, f"secret summary {owner}"),
        )
        connection.commit()
    return {
        "gallery": gallery_path,
        "published": published_path,
        "todo": todo_path,
        "job_log": job_log,
        "job_spec": job_spec,
        "note": note,
        "email_state": email_state,
        "trash": trash,
    }


def _move_sql_owner(factory, source: str, target: str) -> None:
    with factory() as db:
        for model in (FilesImageResource, PublishedFile, Session):
            db.query(model).filter(model.owner == source).update(
                {model.owner: target}, synchronize_session=False
            )
        db.commit()


def _purge_sql_owner(factory, owner: str) -> None:
    with factory() as db:
        for model in (FilesImageResource, PublishedFile, Session):
            db.query(model).filter(model.owner == owner).delete(
                synchronize_session=False
            )
        db.commit()


def test_rename_moves_every_independent_owner_authority_and_is_replay_safe(ancillary):
    lifecycle, factory, paths = ancillary
    _seed_owner(lifecycle, factory, paths, "alice", 1)
    bob_paths = _seed_owner(lifecycle, factory, paths, "bob", 2)
    bob_before = lifecycle.owner_inventory("bob")

    manifest = lifecycle.preview_owner_rename("alice", "ada")
    serialized = json.dumps(manifest, sort_keys=True)
    assert "private" not in serialized
    assert str(paths) not in serialized

    first = lifecycle.reconcile_owner_rename("alice", "ada", manifest)
    second = lifecycle.reconcile_owner_rename("alice", "ada", manifest)
    assert first["state"] == second["state"] == "staged"
    _move_sql_owner(factory, "alice", "ada")

    assert lifecycle.owner_inventory("alice")["count"] == 0
    assert lifecycle.owner_inventory("ada") == manifest["source"]
    assert lifecycle.owner_inventory("bob") == bob_before
    assert all(path.exists() for path in bob_paths.values())
    jobs = json.loads(paths.background_jobs_file.read_text(encoding="utf-8"))
    assert jobs["job-1"]["owner"] == "ada"
    assert json.loads((paths.background_jobs_dir / "job-1.spec.json").read_text())["owner"] == "ada"


def test_delete_stage_compensate_then_retry_purge_erases_bytes_and_preserves_bob(ancillary):
    lifecycle, factory, paths = ancillary
    alice_paths = _seed_owner(lifecycle, factory, paths, "alice", 1)
    bob_paths = _seed_owner(lifecycle, factory, paths, "bob", 2)
    bob_before = lifecycle.owner_inventory("bob")
    tombstone = "deleted:account-alice"
    manifest = lifecycle.preview_owner_rename("alice", tombstone)
    token = "a" * 32

    staged = lifecycle.stage_owner_to_tombstone(
        "alice", tombstone, manifest, operation_token=token
    )
    assert staged["asset_count"] >= 5
    assert not alice_paths["gallery"].exists()
    restored = lifecycle.compensate(
        "alice", tombstone, manifest, operation_token=token
    )
    assert restored["restored_assets"] == staged["asset_count"]
    assert all(path.exists() for path in alice_paths.values())
    assert lifecycle.owner_inventory("alice") == manifest["source"]

    lifecycle.stage_owner_to_tombstone(
        "alice", tombstone, manifest, operation_token=token
    )
    # A legitimate owner that resembles an implementation sentinel must never
    # be broadened into the account deletion.
    jobs = json.loads(paths.background_jobs_file.read_text(encoding="utf-8"))
    jobs["sentinel-owner-job"] = {
        "owner": "__purged__",
        "status": "done",
        "session_id": "other",
        "command": "keep me",
    }
    paths.background_jobs_file.write_text(json.dumps(jobs), encoding="utf-8")
    _move_sql_owner(factory, "alice", tombstone)
    receipt = lifecycle.purge_owner(
        tombstone,
        expected=manifest["source"],
        operation_token=token,
    )
    assert receipt["deleted_assets"] == staged["asset_count"]
    _purge_sql_owner(factory, tombstone)

    assert lifecycle.owner_inventory("alice")["count"] == 0
    assert lifecycle.owner_inventory(tombstone)["count"] == 0
    assert lifecycle.owner_inventory("bob") == bob_before
    assert all(path.exists() for path in bob_paths.values())
    assert not any(path.exists() for path in alice_paths.values())
    assert "sentinel-owner-job" in json.loads(
        paths.background_jobs_file.read_text(encoding="utf-8")
    )


def test_preflight_rejects_active_jobs_and_scheduled_email_without_mutation(ancillary):
    lifecycle, factory, paths = ancillary
    _seed_owner(lifecycle, factory, paths, "alice", 1)
    before = paths.background_jobs_file.read_bytes()
    jobs = json.loads(before)
    jobs["job-1"]["status"] = "running"
    paths.background_jobs_file.write_text(json.dumps(jobs), encoding="utf-8")
    with pytest.raises(AccountAncillaryLifecycleError, match="active background jobs"):
        lifecycle.preview_owner_rename("alice", "ada")
    assert json.loads(paths.background_jobs_file.read_text())["job-1"]["owner"] == "alice"

    jobs["job-1"]["status"] = "done"
    paths.background_jobs_file.write_text(json.dumps(jobs), encoding="utf-8")
    with sqlite3.connect(paths.scheduled_email_db) as connection:
        connection.execute(
            "UPDATE scheduled_emails SET status='pending' WHERE owner='alice'"
        )
        connection.commit()
    with pytest.raises(AccountAncillaryLifecycleError, match="scheduled or sending"):
        lifecycle.preview_owner_rename("alice", "ada")
    with sqlite3.connect(paths.scheduled_email_db) as connection:
        assert connection.execute(
            "SELECT owner FROM scheduled_emails WHERE id='mail-1'"
        ).fetchone()[0] == "alice"


def test_target_conflict_and_symlink_fail_closed(ancillary):
    lifecycle, factory, paths = ancillary
    _seed_owner(lifecycle, factory, paths, "alice", 1)
    _seed_owner(lifecycle, factory, paths, "ada", 2)
    with pytest.raises(AccountAncillaryLifecycleError, match="target already contains"):
        lifecycle.preview_owner_rename("alice", "ada")

    outside = paths.data_dir / "outside"
    outside.write_text("do not touch", encoding="utf-8")
    trash = paths.file_trash_dir / _trash_key("alice")
    (trash / "link").symlink_to(outside)
    with pytest.raises(AccountAncillaryLifecycleError, match="symlink"):
        lifecycle.reconcile_owner_rename(
            "alice",
            "empty-target",
            lifecycle.preview_owner_rename("alice", "empty-target"),
        )
    assert outside.read_text(encoding="utf-8") == "do not touch"


def test_post_preview_target_insertion_fails_before_merging_owner_state(ancillary):
    lifecycle, factory, paths = ancillary
    _seed_owner(lifecycle, factory, paths, "alice", 1)
    manifest = lifecycle.preview_owner_rename("alice", "ada")
    jobs = json.loads(paths.background_jobs_file.read_text(encoding="utf-8"))
    jobs["late-job"] = {
        "owner": "ada",
        "status": "done",
        "session_id": "late",
        "command": "must not merge",
    }
    paths.background_jobs_file.write_text(json.dumps(jobs), encoding="utf-8")

    with pytest.raises(AccountAncillaryLifecycleError, match="background_jobs"):
        lifecycle.reconcile_owner_rename("alice", "ada", manifest)
    observed = json.loads(paths.background_jobs_file.read_text(encoding="utf-8"))
    assert observed["job-1"]["owner"] == "alice"
    assert observed["late-job"]["owner"] == "ada"


def test_tampered_recovery_journal_cannot_move_or_delete_an_unrelated_file(ancillary):
    lifecycle, factory, paths = ancillary
    _seed_owner(lifecycle, factory, paths, "alice", 1)
    manifest = lifecycle.preview_owner_rename("alice", "deleted:alice")
    token = "b" * 32
    lifecycle.stage_owner_to_tombstone(
        "alice", "deleted:alice", manifest, operation_token=token
    )
    outside = paths.data_dir / "outside-secret"
    outside.write_text("retain", encoding="utf-8")
    journal = json.loads(paths.journal_path.read_text(encoding="utf-8"))
    journal["operations"][token]["files"][0]["source"] = str(outside)
    paths.journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(AccountAncillaryLifecycleError, match="unsafe path"):
        lifecycle.purge_owner(
            "deleted:alice",
            expected=manifest["source"],
            operation_token=token,
        )
    assert outside.read_text(encoding="utf-8") == "retain"


def test_delete_stage_replays_after_a_partial_filesystem_move(ancillary, monkeypatch):
    lifecycle, factory, paths = ancillary
    _seed_owner(lifecycle, factory, paths, "alice", 1)
    manifest = lifecycle.preview_owner_rename("alice", "deleted:alice")
    token = "c" * 32
    original_replace = __import__(
        "src.openclank.account_ancillary_lifecycle", fromlist=["os"]
    ).os.replace
    move_calls = 0

    def fail_second(source, target):
        nonlocal move_calls
        if f"/bytes/{token}/" in str(target):
            move_calls += 1
            if move_calls == 2:
                raise OSError("injected move crash")
        return original_replace(source, target)

    monkeypatch.setattr(
        "src.openclank.account_ancillary_lifecycle.os.replace", fail_second
    )
    with pytest.raises(OSError, match="injected move crash"):
        lifecycle.stage_owner_to_tombstone(
            "alice", "deleted:alice", manifest, operation_token=token
        )
    assert move_calls == 2
    monkeypatch.setattr(
        "src.openclank.account_ancillary_lifecycle.os.replace", original_replace
    )

    receipt = lifecycle.stage_owner_to_tombstone(
        "alice", "deleted:alice", manifest, operation_token=token
    )
    assert receipt["state"] == "staged"
    assert receipt["asset_count"] >= 5


def test_rename_replays_after_one_domain_committed_before_checkpoint(ancillary, monkeypatch):
    lifecycle, factory, paths = ancillary
    _seed_owner(lifecycle, factory, paths, "alice", 1)
    manifest = lifecycle.preview_owner_rename("alice", "ada")
    original_email = lifecycle._rename_email
    calls = 0

    def fail_email_once(source, target):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected email crash")
        return original_email(source, target)

    monkeypatch.setattr(lifecycle, "_rename_email", fail_email_once)
    with pytest.raises(OSError, match="injected email crash"):
        lifecycle.reconcile_owner_rename("alice", "ada", manifest)
    jobs = json.loads(paths.background_jobs_file.read_text(encoding="utf-8"))
    assert jobs["job-1"]["owner"] == "ada"

    receipt = lifecycle.reconcile_owner_rename("alice", "ada", manifest)
    assert receipt["state"] == "staged"


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

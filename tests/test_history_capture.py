"""Provider-boundary tests for the Python history capture hook."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os

import pytest

from core.atomic_io import AtomicWriteConflict, atomic_write_batch, AtomicFileChange
from src.openclank import history_capture
from src.openclank.history_capture import HistoryContext
from src.openclank.history_client import HistoryClient, HistoryClientError


class FakeHistoryClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def prepare(self, envelope, *, content, fingerprint):
        self.calls.append(("prepare", (envelope, content, fingerprint)))
        return {"Action": {"action_id": envelope["action_id"]}}

    def record_live(self, action_id, receipt):
        self.calls.append(("record_live", (action_id, receipt)))
        return {"Action": {"action_id": action_id}}

    def complete(self, action_id, *, content, fingerprint):
        self.calls.append(("complete", (action_id, content, fingerprint)))
        return {"Action": {"action_id": action_id}}

    def abort(self, action_id):
        self.calls.append(("abort", action_id))
        return {"Action": {"action_id": action_id}}


def _context(tmp_path, client):
    return HistoryContext(
        actor_id="agent",
        account_id="account",
        workspace_id="workspace",
        roots=(str(tmp_path),),
        client=client,
    )


def test_directory_manifest_embeds_exact_recursive_subtree(tmp_path):
    root = tmp_path / "tree"
    nested = root / "nested" / "deeper"
    nested.mkdir(parents=True)
    child = nested / "payload.bin"
    content = b"\x00recursive\xff-bytes"
    child.write_bytes(content)
    os.chmod(child, 0o751)
    mtime_ns = 1_700_000_123_000_000_000
    os.utime(child, ns=(mtime_ns, mtime_ns))
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are unavailable")
    link = root / "alias"
    try:
        link.symlink_to("nested/deeper/payload.bin")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    manifest = json.loads(history_capture._directory_manifest(str(root)))
    entries = {entry["path"]: entry for entry in manifest["entries"]}
    assert entries["nested"]["type"] == "directory"
    assert entries["nested/deeper"]["type"] == "directory"
    assert entries["nested/deeper/payload.bin"]["content"] == base64.b64encode(content).decode("ascii")
    assert entries["nested/deeper/payload.bin"]["content_encoding"] == "base64"
    assert entries["nested/deeper/payload.bin"]["size"] == len(content)
    assert entries["nested/deeper/payload.bin"]["mode"] & 0o777 == 0o751
    assert entries["nested/deeper/payload.bin"]["mtime_millis"] == mtime_ns // 1_000_000
    assert entries["alias"]["type"] == "symlink"
    assert entries["alias"]["target"] == "nested/deeper/payload.bin"


def test_atomic_batch_prepares_once_and_completes_one_action(tmp_path):
    client = FakeHistoryClient()
    context = _context(tmp_path, client)
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"before")
    atomic_write_batch(
        [
            AtomicFileChange(str(first), b"after", history_context=context),
            AtomicFileChange(str(second), b"created", require_missing=True, history_context=context),
        ]
    )

    assert first.read_bytes() == b"after"
    assert second.read_bytes() == b"created"
    assert [name for name, _ in client.calls] == ["prepare", "record_live", "complete"]
    envelope = client.calls[0][1][0]
    assert envelope["action_id"] == client.calls[1][1][0]
    assert envelope["action_id"] == client.calls[2][1][0]
    assert len(envelope["modified_resource_ids"]) == 2
    assert history_capture.last_history_status(context)["history_status"] == "complete"
    assert envelope["coverage"]["kind"] == "ObservedAfterOnly"


def test_history_envelope_carries_pinned_action_context(tmp_path):
    client = FakeHistoryClient()
    context = HistoryContext(
        actor_id="actor-authenticated",
        account_id="account-authenticated",
        workspace_id="chat-authenticated",
        session_id="chat-authenticated",
        run_id="run-pinned",
        task_id="task-pinned",
        tool_id="mimo-bash",
        roots=(str(tmp_path),),
        client=client,
    )
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    handle = history_capture.begin_file_capture(
        str(target), operation="shell", context=context, action_id="action-durable"
    )
    assert handle.available
    envelope = client.calls[0][1][0]
    assert envelope["action_id"] == "action-durable"
    assert envelope["actor_id"] == "actor-authenticated"
    assert envelope["actor_account_id"] == "account-authenticated"
    assert envelope["session_id"] == "chat-authenticated"
    assert envelope["run_id"] == "run-pinned"
    assert envelope["task_id"] == "task-pinned"
    assert envelope["tool_id"] == "mimo-bash"
    target.write_text("after", encoding="utf-8")
    assert history_capture.complete_file_capture(handle, str(target))["history_status"] == "complete"


def test_context_mapping_does_not_invent_identity(tmp_path):
    context = history_capture.context_from_mapping(
        {
            "history_capture": True,
            "actor_id": "actor",
            "account_id": "account",
            "workspace_id": "chat",
            "history_roots": [str(tmp_path)],
            "session_id": "chat",
            "run_id": "run",
            "task_id": "task",
            "tool_id": "mimo-bash",
        }
    )
    assert context is not None
    assert (context.session_id, context.run_id, context.task_id, context.tool_id) == (
        "chat", "run", "task", "mimo-bash"
    )


def test_capture_finish_is_single_use_after_service_failure(tmp_path):
    class FailingClient(FakeHistoryClient):
        def complete(self, action_id, *, content, fingerprint):
            self.calls.append(("complete", (action_id, content, fingerprint)))
            raise RuntimeError("simulated complete failure")

    client = FailingClient()
    context = _context(tmp_path, client)
    target = tmp_path / "once.txt"
    target.write_text("before", encoding="utf-8")
    handle = history_capture.begin_file_capture(
        str(target), operation="replace", context=context, action_id="single-use"
    )
    target.write_text("after", encoding="utf-8")
    first = history_capture.complete_file_capture(handle, str(target))
    second = handle.finish(after=b"different")
    assert first == second
    assert [name for name, _ in client.calls].count("complete") == 1


def test_history_unavailable_does_not_block_live_write(tmp_path):
    context = _context(tmp_path, None)
    target = tmp_path / "note.md"
    atomic_write_batch([AtomicFileChange(str(target), b"live", require_missing=True, history_context=context)])
    assert target.read_bytes() == b"live"
    status = history_capture.last_history_status(context)
    assert status["history_status"] == "paused"
    assert status["capture_phase"] == "unavailable"


def test_configured_recoverable_batch_is_gated_when_prepare_fails(tmp_path):
    class FailingBatch(FakeHistoryClient):
        def prepare_batch(self, envelope, entries):
            raise RuntimeError("worker stopped before batch acknowledgement")

    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"before")
    context = _context(tmp_path, FailingBatch())
    with pytest.raises(AtomicWriteConflict, match="history_prepare_required"):
        atomic_write_batch(
            [
                AtomicFileChange(str(first), b"after", history_context=context),
                AtomicFileChange(str(second), b"created", history_context=context),
            ]
        )
    assert first.read_bytes() == b"before"
    assert not second.exists()


def test_batch_staging_cleans_every_upload_when_publish_fails():
    class FailingPublish:
        def __init__(self):
            self.staged: list[str] = []
            self.aborted: list[tuple[str, str]] = []

        def _check_content(self, content):
            return None

        def _canonical_staged_fingerprint(self, content, _fingerprint):
            return f"sha256:{len(content):064x}:{len(content)}"

        def _stage_content(self, action_id, content, _fingerprint):
            upload_id = f"upload-{len(self.staged)}"
            self.staged.append(upload_id)
            return upload_id

        def _request_envelope(self, envelope):
            return envelope

        def _call(self, _payload):
            raise HistoryClientError("worker stopped after staging the batch")

        def _abort_staged(self, action_id, upload_id):
            self.aborted.append((action_id, upload_id))

    client = FailingPublish()
    entries = [
        {"content": b"a" * (512 * 1024 + 1), "fingerprint": "first"},
        {"content": b"b" * (512 * 1024 + 1), "fingerprint": "second"},
    ]
    with pytest.raises(HistoryClientError, match="worker stopped"):
        # The method only relies on these transport hooks, so this test does
        # not start a worker or create a live service.
        HistoryClient.prepare_batch(client, {"action_id": "batch-stage"}, entries)
    assert client.staged == ["upload-0", "upload-1"]
    assert client.aborted == [
        ("batch-stage", "upload-0"),
        ("batch-stage", "upload-1"),
    ]


def test_batch_staging_obeys_aggregate_inline_cap():
    class RecordingClient:
        _wire_content = staticmethod(HistoryClient._wire_content)

        def __init__(self):
            self.staged: list[str] = []
            self.payload = None

        def _check_content(self, _content):
            return None

        def _canonical_staged_fingerprint(self, content, _fingerprint):
            return f"sha256:{len(content):064x}:{len(content)}"

        def _stage_content(self, _action_id, _content, _fingerprint):
            upload_id = f"aggregate-upload-{len(self.staged)}"
            self.staged.append(upload_id)
            return upload_id

        def _request_envelope(self, envelope):
            return envelope

        def _call(self, payload):
            self.payload = payload
            return {"Accepted": None}

    one = RecordingClient()
    HistoryClient.prepare_batch(
        one,
        {"action_id": "aggregate-one"},
        [{"content": b"x" * (600 * 1024), "fingerprint": "one"}],
    )
    assert one.staged == ["aggregate-upload-0"]
    assert one.payload["PrepareBatch"]["entries"][0]["content"] is None

    many = RecordingClient()
    HistoryClient.prepare_batch(
        many,
        {"action_id": "aggregate-many"},
        [
            {"content": b"a" * (300 * 1024), "fingerprint": "first"},
            {"content": b"b" * (300 * 1024), "fingerprint": "second"},
        ],
    )
    assert many.staged == ["aggregate-upload-0"]
    wire_entries = many.payload["PrepareBatch"]["entries"]
    assert wire_entries[0]["content"] is not None
    assert wire_entries[1]["content"] is None


def test_failed_after_capture_is_recoverable_without_rolling_back_live_write(tmp_path):
    class FailingAfter(FakeHistoryClient):
        def complete(self, action_id, *, content, fingerprint):
            raise RuntimeError("worker stopped")

    client = FailingAfter()
    context = _context(tmp_path, client)
    target = tmp_path / "note.md"
    with pytest.raises(AtomicWriteConflict, match="history reconciliation pending"):
        atomic_write_batch([AtomicFileChange(str(target), b"live", require_missing=True, history_context=context)])
    assert target.read_bytes() == b"live"
    status = history_capture.last_history_status(context)
    assert status["history_status"] == "failed"
    assert status["capture_phase"] == "after_failed"


def test_budget_pause_is_reported_at_capture_boundary_without_blocking_write(tmp_path):
    class BudgetPaused(FakeHistoryClient):
        def prepare(self, envelope, *, content, fingerprint):
            raise RuntimeError("history_paused_budget: global target reached")

    target = tmp_path / "note.md"
    context = _context(tmp_path, BudgetPaused())
    atomic_write_batch([AtomicFileChange(str(target), b"live", require_missing=True, history_context=context)])
    assert target.read_bytes() == b"live"
    status = history_capture.last_history_status(context)
    assert status["history_status"] == "paused"
    assert status["capture_phase"] == "budget"


def test_not_committed_never_calls_complete(tmp_path):
    client = FakeHistoryClient()
    context = _context(tmp_path, client)
    target = tmp_path / "note.md"
    target.write_bytes(b"before")
    handle = history_capture.begin_file_capture(str(target), operation="replace", context=context)
    result = handle.finish(committed=False, after=b"before")
    assert result["capture_phase"] == "live_not_committed"
    assert [name for name, _ in client.calls] == ["prepare", "record_live"]


def test_after_read_failure_never_writes_a_tombstone_or_completes(tmp_path):
    client = FakeHistoryClient()
    context = _context(tmp_path, client)
    target = tmp_path / "note.md"
    target.write_bytes(b"before")
    handle = history_capture.begin_file_capture(str(target), operation="replace", context=context)
    result = handle.finish(committed=True, after=None, after_read_error="read interrupted")
    assert result["capture_phase"] == "after_failed"
    live = client.calls[1][1][1]
    assert live["fingerprint"] is None
    assert live["after_unavailable"] is True
    assert [name for name, _ in client.calls] == ["prepare", "record_live"]


def test_status_isolated_between_concurrent_authenticated_actions(tmp_path):
    first_client = FakeHistoryClient()
    second_client = FakeHistoryClient()
    first_context = _context(tmp_path, first_client)
    second_context = _context(tmp_path, second_client)

    def write(context, name):
        target = tmp_path / name
        atomic_write_batch([AtomicFileChange(str(target), name.encode(), require_missing=True, history_context=context)])
        return history_capture.last_history_status(context)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(lambda args: write(*args), [(first_context, "one"), (second_context, "two")])
    assert first["history_status"] == "complete"
    assert second["history_status"] == "complete"
    assert first["action_id"] != second["action_id"]
    assert first_context.status["action_id"] == first["action_id"]
    assert second_context.status["action_id"] == second["action_id"]


def test_secret_and_unscoped_paths_are_excluded_without_provider_calls(tmp_path):
    client = FakeHistoryClient()
    context = _context(tmp_path, client)
    outside = tmp_path.parent / "outside.md"
    secret = tmp_path / "settings.json"
    for target in (outside, secret):
        handle = history_capture.begin_file_capture(str(target), operation="replace", context=context)
        assert handle.status == "paused"
        assert handle.capture_phase == "excluded"
    assert client.calls == []


def test_excluded_target_is_not_read_before_containment_check(tmp_path, monkeypatch):
    context = _context(tmp_path, FakeHistoryClient())
    secret = tmp_path / "credentials.json"

    def forbidden_read(_path):
        raise AssertionError("excluded target was read")

    monkeypatch.setattr(history_capture.pathlib.Path, "read_bytes", forbidden_read)
    handle = history_capture.begin_file_capture(str(secret), operation="replace", context=context)
    assert handle.status == "paused"
    assert handle.capture_phase == "excluded"


def test_registered_filesystem_tool_receives_trusted_context(tmp_path, monkeypatch):
    client = FakeHistoryClient()
    context = _context(tmp_path, client)
    from src.agent_tools import filesystem_tools
    from src.agent_tools import TOOL_HANDLERS

    monkeypatch.setattr(filesystem_tools, "_history_context", lambda _ctx: context)
    monkeypatch.setattr(filesystem_tools, "_validate_file_policy", lambda *_args, **_kwargs: {"warnings": []})
    monkeypatch.setenv("ODYSSEUS_FILES_AGENT_RUNTIME", "0")
    target = tmp_path / "registered.md"
    result = __import__("asyncio").run(
        TOOL_HANDLERS["write_file"](
            '{"path": "' + str(target) + '", "content": "registered"}',
            {"owner": "account", "workspace": str(tmp_path)},
        )
    )
    assert result["exit_code"] == 0
    assert result["history"]["history_status"] == "complete"
    assert [name for name, _ in client.calls] == ["prepare", "record_live", "complete"]


def test_root_journal_owns_every_granted_root_for_persistent_writer_handoff(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    owner = history_capture.WriterOwner(
        owner_id="alice",
        session_id="sess-a",
        action_id="shell:sess-a:job1",
        run_id="run-1",
        task_id="task-1",
        tool_id="mimo-bash",
    )
    journal = history_capture.RootJournal(str(tmp_path / "roots.journal.json"))
    state = journal.open(
        action_id="shell:sess-a:job1",
        writer_owner=owner,
        roots=[str(root_a), str(root_b)],
    )
    assert state["phase"] == "open"
    assert state["generation"] == 0
    assert set(state["roots"]) == {str(root_a.resolve()), str(root_b.resolve())}
    assert state["writer_owner"]["owner_id"] == "alice"
    assert state["writer_owner"]["action_id"] == "shell:sess-a:job1"


def test_root_journal_hands_off_cleanly_across_restart(tmp_path):
    path = tmp_path / "roots.journal.json"
    owner = history_capture.WriterOwner(
        owner_id="alice",
        session_id="sess-a",
        action_id="shell:sess-a:job1",
    )
    first = history_capture.RootJournal(str(path))
    opened = first.open(
        action_id="shell:sess-a:job1",
        writer_owner=owner,
        roots=[str(tmp_path)],
    )
    assert opened["generation"] == 0

    # A restarted process cannot silently steal a live in-memory batch handle,
    # even when its persistent identity is identical.
    second = history_capture.RootJournal(str(path))
    with pytest.raises(history_capture.RootJournalError, match="live lease"):
        second.open(
            action_id="shell:sess-a:job1",
            writer_owner=owner,
            roots=[str(tmp_path)],
        )
    assert second.read()["generation"] == opened["generation"]


def test_root_journal_restart_shaped_open_state_is_not_success(tmp_path):
    path = tmp_path / "roots.journal.json"
    owner = history_capture.WriterOwner(
        owner_id="alice",
        session_id="sess-a",
        action_id="shell:sess-a:job1",
    )
    journal = history_capture.RootJournal(str(path))
    journal.open(
        action_id="shell:sess-a:job1",
        writer_owner=owner,
        roots=[str(tmp_path)],
    )
    # Restart-shaped: the journal is still open after the worker vanished.
    # It must never be read as a completed capture.
    state = journal.read()
    assert state["phase"] == "open"
    assert state["result"] is None
    # Honest terminalization for the vanished worker.
    closed = journal.abandon("capture worker exited before after-state reconciliation completed")
    assert closed["phase"] == "abandoned"
    assert closed["result"]["history_status"] == "failed"
    assert closed["result"]["capture_phase"] == "after_failed"


def test_persistent_owner_handoff_requires_continuing_root_journal(tmp_path):
    owner = history_capture.WriterOwner(
        owner_id="alice",
        session_id="sess-a",
        action_id="shell:sess-a:job1",
    )
    missing = history_capture.RootJournal(str(tmp_path / "absent.journal.json"))
    with pytest.raises(history_capture.RootJournalError, match="continuing root journal"):
        missing.handoff(owner)


def test_root_journal_persistent_owner_handoff_is_recorded(tmp_path):
    path = tmp_path / "roots.journal.json"
    owner = history_capture.WriterOwner(
        owner_id="alice",
        session_id="sess-a",
        action_id="shell:sess-a:job1",
        run_id="run-1",
        task_id="task-1",
        tool_id="bash",
    )
    journal = history_capture.RootJournal(str(path))
    journal.open(
        action_id="shell:sess-a:job1",
        writer_owner=owner,
        roots=[str(tmp_path)],
    )
    continuing = history_capture.WriterOwner(
        owner_id="alice",
        session_id="sess-a",
        action_id="shell:sess-a:job1",
        run_id="run-1",
        task_id="task-1",
        tool_id="bash",
    )
    state = journal.handoff(continuing)
    assert state["generation"] == 1
    assert state["writer_owner"]["tool_id"] == "bash"
    assert state["phase"] == "open"


def test_root_journal_rejects_handoff_that_changes_run_task_or_tool_identity(tmp_path):
    path = tmp_path / "roots.journal.json"
    owner = history_capture.WriterOwner(
        owner_id="alice", session_id="sess-a", action_id="shell:sess-a:job1",
        run_id="run-1", task_id="task-1", tool_id="bash",
    )
    journal = history_capture.RootJournal(str(path))
    journal.open(action_id=owner.action_id, writer_owner=owner, roots=[str(tmp_path)])
    changed = history_capture.WriterOwner(
        owner_id="alice", session_id="sess-a", action_id=owner.action_id,
        run_id="run-2", task_id="task-1", tool_id="bash",
    )
    with pytest.raises(history_capture.RootJournalError, match="writer-owner mismatch"):
        journal.handoff(changed)


def test_root_journal_stale_generation_cannot_settle_after_continuation_claim(tmp_path):
    path = tmp_path / "roots.journal.json"
    owner = history_capture.WriterOwner(
        owner_id="alice", session_id="sess-a", action_id="shell:sess-a:job1",
        run_id="run-1", task_id="task-1", tool_id="bash",
    )
    first = history_capture.RootJournal(str(path))
    first.open(action_id=owner.action_id, writer_owner=owner, roots=[str(tmp_path)])
    continuing = history_capture.RootJournal(str(path))
    continuing.claim_recovery(owner)
    with pytest.raises(history_capture.RootJournalError, match="lease is stale"):
        first.settle({"history_status": "complete", "capture_phase": "complete"})
    settled = continuing.settle({"history_status": "complete", "capture_phase": "complete"})
    assert settled["phase"] == "settled"


def test_root_journal_abandon_strips_false_success(tmp_path):
    path = tmp_path / "roots.journal.json"
    owner = history_capture.WriterOwner(
        owner_id="alice",
        session_id="sess-a",
        action_id="shell:sess-a:job1",
    )
    journal = history_capture.RootJournal(str(path))
    journal.open(
        action_id="shell:sess-a:job1",
        writer_owner=owner,
        roots=[str(tmp_path)],
    )
    # A late writer result that claims completion must not survive abandon.
    closed = journal.abandon(
        "late_writer_unsettled",
        {"history_status": "complete", "capture_phase": "complete"},
    )
    assert closed["phase"] == "abandoned"
    assert closed["result"]["history_status"] == "failed"


def test_root_journal_settle_is_single_use_and_keeps_first_terminal(tmp_path):
    path = tmp_path / "roots.journal.json"
    owner = history_capture.WriterOwner(
        owner_id="alice",
        session_id="sess-a",
        action_id="shell:sess-a:job1",
    )
    journal = history_capture.RootJournal(str(path))
    journal.open(
        action_id="shell:sess-a:job1",
        writer_owner=owner,
        roots=[str(tmp_path)],
    )
    first = journal.settle(
        {"history_status": "complete", "capture_phase": "complete", "receipt": {"ok": True}}
    )
    assert first["phase"] == "settled"
    second = journal.abandon("too-late", {"history_status": "failed"})
    assert second["phase"] == "settled"
    assert second["result"]["history_status"] == "complete"


def test_root_journal_rejects_writer_owner_mismatch_on_restart(tmp_path):
    path = tmp_path / "roots.journal.json"
    owner = history_capture.WriterOwner(
        owner_id="alice",
        session_id="sess-a",
        action_id="shell:sess-a:job1",
    )
    journal = history_capture.RootJournal(str(path))
    journal.open(
        action_id="shell:sess-a:job1",
        writer_owner=owner,
        roots=[str(tmp_path)],
    )
    stranger = history_capture.WriterOwner(
        owner_id="bob",
        session_id="sess-a",
        action_id="shell:sess-a:job1",
    )
    with pytest.raises(history_capture.RootJournalError, match="writer-owner mismatch"):
        journal.open(
            action_id="shell:sess-a:job1",
            writer_owner=stranger,
            roots=[str(tmp_path)],
        )


def test_root_journal_handoff_rejects_foreign_writer_owner(tmp_path):
    path = tmp_path / "roots.journal.json"
    owner = history_capture.WriterOwner(
        owner_id="alice",
        session_id="sess-a",
        action_id="shell:sess-a:job1",
    )
    journal = history_capture.RootJournal(str(path))
    journal.open(
        action_id="shell:sess-a:job1",
        writer_owner=owner,
        roots=[str(tmp_path)],
    )
    # A foreign owner/session that only matches action_id must not take over.
    foreign = history_capture.WriterOwner(
        owner_id="bob",
        session_id="sess-b",
        action_id="shell:sess-a:job1",
    )
    with pytest.raises(history_capture.RootJournalError, match="writer-owner mismatch"):
        journal.handoff(foreign)
    state = journal.read()
    assert state["phase"] == "open"
    assert state["generation"] == 0
    assert state["writer_owner"]["owner_id"] == "alice"
    assert state["writer_owner"]["session_id"] == "sess-a"


def test_root_journal_open_rejects_already_terminal(tmp_path):
    path = tmp_path / "roots.journal.json"
    owner = history_capture.WriterOwner(
        owner_id="alice",
        session_id="sess-a",
        action_id="shell:sess-a:job1",
    )
    journal = history_capture.RootJournal(str(path))
    journal.open(
        action_id="shell:sess-a:job1",
        writer_owner=owner,
        roots=[str(tmp_path)],
    )
    journal.settle({"history_status": "complete", "capture_phase": "complete"})
    with pytest.raises(history_capture.RootJournalError, match="already terminal"):
        journal.open(
            action_id="shell:sess-a:job1",
            writer_owner=owner,
            roots=[str(tmp_path)],
        )


def test_root_journal_close_does_not_mint_phantom_on_missing_path(tmp_path):
    missing = history_capture.RootJournal(str(tmp_path / "absent.journal.json"))
    closed = missing.abandon("never opened")
    assert closed["phase"] == "abandoned"
    assert not (tmp_path / "absent.journal.json").exists()

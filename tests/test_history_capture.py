"""Provider-boundary tests for the Python history capture hook."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from core.atomic_io import AtomicWriteConflict, atomic_write_batch, AtomicFileChange
from src.openclank import history_capture
from src.openclank.history_capture import HistoryContext
from src.openclank.history_client import HistoryClientError


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
        {"content": b"a" * (640 * 1024 + 1), "fingerprint": "first"},
        {"content": b"b" * (640 * 1024 + 1), "fingerprint": "second"},
    ]
    with pytest.raises(HistoryClientError, match="worker stopped"):
        # The method only relies on these transport hooks, so this test does
        # not start a worker or create a live service.
        from src.openclank.history_client import HistoryClient

        HistoryClient.prepare_batch(client, {"action_id": "batch-stage"}, entries)
    assert client.staged == ["upload-0", "upload-1"]
    assert client.aborted == [
        ("batch-stage", "upload-0"),
        ("batch-stage", "upload-1"),
    ]


def test_failed_after_capture_is_reported_without_rolling_back_live_write(tmp_path):
    class FailingAfter(FakeHistoryClient):
        def complete(self, action_id, *, content, fingerprint):
            raise RuntimeError("worker stopped")

    client = FailingAfter()
    context = _context(tmp_path, client)
    target = tmp_path / "note.md"
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

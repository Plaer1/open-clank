import json
from types import SimpleNamespace

import pytest

from tests.helpers.cli_loader import load_script


@pytest.fixture
def cli():
    return load_script("odysseus-research")


def _run_list(cli, tmp_path, monkeypatch, status, records):
    cli._DATA_DIR = tmp_path
    for name, blob in records.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(blob))
    emitted = []
    monkeypatch.setattr(cli, "emit", lambda value, args: emitted.append(value))
    cli.cmd_list(SimpleNamespace(status=status, limit=50))
    assert emitted, "cmd_list emitted nothing"
    return [r["id"] for r in emitted[0]]


def test_preview_text_ignores_non_string(cli):
    assert cli._preview_text(None) == ""
    assert cli._preview_text(123) == ""
    assert cli._preview_text(["x"]) == ""
    assert cli._preview_text("q" * 250) == "q" * 200


def test_summarize_does_not_crash_on_non_string_query(cli):
    out = cli._summarize("rp1", {"query": 123, "status": "done"})
    assert out["query"] == ""
    assert out["id"] == "rp1"


def test_complete_is_a_valid_status_choice(cli):
    parser = cli._build_parser()
    ns = parser.parse_args(["list", "--status", "complete"])
    assert ns.status == "complete"


def test_filter_returns_completed_runs(cli, tmp_path, monkeypatch):
    cli._DATA_DIR = tmp_path
    (tmp_path / "r1.json").write_text(json.dumps({"query": "q1", "status": "done"}))
    (tmp_path / "r2.json").write_text(json.dumps({"query": "q2", "status": "running"}))
    emitted = []
    monkeypatch.setattr(cli, "emit", lambda value, args: emitted.append(value))
    # CLI "complete" must map to the stored "done" and match r1.
    cli.cmd_list(SimpleNamespace(status="complete", limit=50))
    ids = [r["id"] for r in emitted[0]]
    assert ids == ["r1"]


def test_verbatim_status_still_filters(cli, tmp_path, monkeypatch):
    cli._DATA_DIR = tmp_path
    (tmp_path / "r1.json").write_text(json.dumps({"query": "q1", "status": "done"}))
    (tmp_path / "r2.json").write_text(json.dumps({"query": "q2", "status": "running"}))
    emitted = []
    monkeypatch.setattr(cli, "emit", lambda value, args: emitted.append(value))
    cli.cmd_list(SimpleNamespace(status="running", limit=50))
    ids = [r["id"] for r in emitted[0]]
    assert ids == ["r2"]


def test_status_complete_matches_writer_done_records(cli, tmp_path, monkeypatch):
    """`--status complete` must return the records the writer marked `done`.
    Without the alias this filter is silently empty on any real corpus."""
    ids = _run_list(cli, tmp_path, monkeypatch, status="complete", records={
        "rp-done":      {"query": "finished one", "status": "done",      "started_at": "2026-01-02"},
        "rp-running":   {"query": "still running", "status": "running",  "started_at": "2026-01-01"},
        "rp-cancelled": {"query": "user stopped",  "status": "cancelled","started_at": "2025-12-31"},
    })
    assert ids == ["rp-done"], (
        "--status complete should alias to the writer's stored 'done' value; "
        f"got {ids}. The alias map in `_STATUS_CLI_TO_STORED` was bypassed."
    )


def test_status_running_still_matches_verbatim(cli, tmp_path, monkeypatch):
    """`running` is stored verbatim, so the alias must NOT rewrite it.
    A blanket map that turned every CLI choice into a stored variant would
    re-introduce the empty-result bug on the running/cancelled/error paths."""
    ids = _run_list(cli, tmp_path, monkeypatch, status="running", records={
        "rp-done":    {"query": "finished",     "status": "done"},
        "rp-running": {"query": "still running", "status": "running"},
    })
    assert ids == ["rp-running"], f"--status running must match verbatim; got {ids}"


def test_status_cancelled_still_matches_verbatim(cli, tmp_path, monkeypatch):
    ids = _run_list(cli, tmp_path, monkeypatch, status="cancelled", records={
        "rp-done":      {"query": "finished",  "status": "done"},
        "rp-cancelled": {"query": "user stop", "status": "cancelled"},
    })
    assert ids == ["rp-cancelled"]


def test_status_error_still_matches_verbatim(cli, tmp_path, monkeypatch):
    ids = _run_list(cli, tmp_path, monkeypatch, status="error", records={
        "rp-done":  {"query": "finished", "status": "done"},
        "rp-error": {"query": "crashed",  "status": "error"},
    })
    assert ids == ["rp-error"]


def test_status_filter_tolerates_missing_or_non_string_status(cli, tmp_path, monkeypatch):
    """A corrupt record with no `status` (or a non-string status) must not
    crash the filter and must not falsely match `--status complete`. The
    existing `_load_path` already drops non-dict blobs; this guards the
    next layer."""
    ids = _run_list(cli, tmp_path, monkeypatch, status="complete", records={
        "rp-good":  {"query": "ok",  "status": "done"},
        "rp-blank": {"query": "no status field"},
        "rp-typed": {"query": "non-string", "status": 42},
    })
    assert ids == ["rp-good"], (
        "--status complete should only match the writer's 'done' string; "
        f"got {ids}."
    )


def test_list_skips_non_object_research_records(cli, tmp_path, monkeypatch):
    cli._DATA_DIR = tmp_path
    (tmp_path / "good.json").write_text(json.dumps({"query": "hello", "status": "complete"}))
    (tmp_path / "list.json").write_text("[]")
    (tmp_path / "broken.json").write_text("{")

    emitted = []
    monkeypatch.setattr(cli, "emit", lambda value, args: emitted.append(value))

    cli.cmd_list(SimpleNamespace(status=None, limit=50))

    assert emitted == [[{
        "id": "good",
        "query": "hello",
        "category": "",
        "status": "complete",
        "started_at": "",
        "completed_at": "",
        "sources": 0,
        "stats": {},
    }]]

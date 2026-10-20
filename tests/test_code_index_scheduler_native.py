"""Opt-in actual app/provider/scheduler wire gates; never an installed fallback.

These tests prove persistence, wire validation and queued lifecycle integration.
Native deterministic barrier tests separately prove concurrent CPU accounting.
"""
import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

binary = os.environ.get("OPEN_CLANK_TEST_FM_MCP_BIN")
expected = os.environ.get("OPENCLANK_S04_NATIVE_SHA256")
if not binary or not expected:
    pytest.skip("Explicit matching S04 s03-fixtures binary and SHA required", allow_module_level=True)
with Path(binary).resolve(strict=True).open("rb") as stream:
    assert hashlib.file_digest(stream, "sha256").hexdigest() == expected

from tests.test_code_query_root_transport import _assert_isolated_startup

_assert_isolated_startup()
from tests import test_code_index_lifecycle as lifecycle
from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_provider import MemoryRequestRejectedError
from src.openclank.code_index_settings import (
    THREADS_KEY, configured, prepare_settings, readback, validate_status,
)

scope = lifecycle.scope


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    from src import settings

    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_settings_cache", None)
    yield settings
    settings._invalidate_caches()


def counts(provider, tmp_path):
    path = Path(provider._local_db_path()).resolve(strict=True)
    assert path.is_relative_to(tmp_path.resolve())
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return tuple(db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                     for table in ("code_registrations", "code_index_runs", "code_index_heads"))


def save_limit(settings, desired):
    settings.save_settings(prepare_settings(settings.load_settings(), {THREADS_KEY: desired}))
    return settings.load_settings()


async def test_actual_default_status_strict_native_requests_and_revision_conflicts(
        scope, tmp_path, monkeypatch):
    provider, gate = await lifecycle.native_provider(scope, tmp_path, monkeypatch)
    try:
        baseline = validate_status(await provider.code_index_scheduler(action="status"))
        assert (baseline["desired"], baseline["effective"], baseline["revision"],
                baseline["effective_revision"], baseline["active"], baseline["queued"]) == (2, 2, 0, 0, 0, 0)
        assert counts(provider, tmp_path) == (0, 0, 0)
        invalid = [
            {"action": "status", "origin": "agent"},
            {"action": "status", "origin": "app", "workspace_id": "not-accepted"},
            {"action": "status", "origin": "app", "max_threads": 2},
            *({"action": "configure", "origin": "app", "revision": 1, "max_threads": bad}
              for bad in (True, None, "2", 2.5, 0, -1, 33)),
            {"action": "configure", "origin": "app", "revision": 0, "max_threads": 2},
            {"action": "configure", "origin": "app", "revision": 2**53, "max_threads": 2},
        ]
        for request in invalid:
            # Bypass only Python DTO validation to exercise actual native wire rejection.
            with pytest.raises(MemoryRequestRejectedError):
                await provider._call_tool_transport("code_index_scheduler", request, _ready_only=True)
            assert await provider.code_index_scheduler(action="status") == baseline
        applied = await provider.code_index_scheduler(action="configure", revision=2, max_threads=4)
        assert applied["effective"] == 4 and applied["effective_revision"] == 2
        assert await provider.code_index_scheduler(action="configure", revision=2, max_threads=4) == applied
        for revision, maximum in ((1, 2), (2, 1)):
            with pytest.raises(MemoryRequestRejectedError):
                await provider.code_index_scheduler(action="configure", revision=revision, max_threads=maximum)
            assert await provider.code_index_scheduler(action="status") == applied
        assert counts(provider, tmp_path) == (0, 0, 0), "Settings must not create an index"
    finally:
        (gate / "release").touch()
        await provider.shutdown()


@pytest.mark.parametrize("desired", [1, 2, 4])
async def test_actual_saved_limit_restores_on_explicit_owner_restart_without_index(
        scope, tmp_path, monkeypatch, isolated_settings, desired):
    saved = save_limit(isolated_settings, desired)
    provider, gate = await lifecycle.native_provider(scope, tmp_path, monkeypatch)
    try:
        status = await readback(provider, saved)
        assert status["status"] == "applied" and status["effective"] == desired
        assert (status["desired"], status["revision"]) == configured(saved)
        assert counts(provider, tmp_path) == (0, 0, 0)
        environment = dict(provider._env)
        await provider.shutdown()
        pending = await readback(provider, saved)
        assert pending["native_status"] == "unavailable" and pending["effective"] is None
        assert provider._owner_task is None or provider._owner_task.done()
        # A deliberate test-owned restart, not Settings GET or an automatic watchdog.
        provider = FrankenmemoryProvider(command=binary, env=environment)
        await provider.initialize()
        restored = await readback(provider, isolated_settings.load_settings())
        assert restored["status"] == "applied" and restored["effective"] == desired
        assert restored["effective_revision"] == configured(saved)[1]
        assert counts(provider, tmp_path) == (0, 0, 0)
    finally:
        (gate / "release").touch()
        await provider.shutdown()


async def test_actual_two_owner_queue_reconfiguration_coalescing_and_cancellation(
        scope, tmp_path, monkeypatch, isolated_settings):
    provider, gate = await lifecycle.native_provider(scope, tmp_path, monkeypatch)
    try:
        opened = {}
        for owner in ("alice", "bob"):
            authority = lifecycle.native_request(scope, owner=owner)
            (Path(authority["root"]) / "source.py").write_text("def target():\n    return 1\n", encoding="utf8")
            opened[owner] = await provider.code_index_lifecycle(**authority)
            token = opened[owner]["task_token"]
            await lifecycle.wait_native(lambda token=token: (gate / (token + ".held")).exists(), "pre-admission hold")
        queued = await provider.code_index_scheduler(action="status")
        assert queued["queued"] == 2 and queued["active"] == 0
        assert counts(provider, tmp_path)[1:] == (0, 0)
        saved = save_limit(isolated_settings, 1)
        changed = await readback(provider, saved, apply=True)
        # The fixture holds BEFORE cohort admission; immediate application is correct.
        assert changed["status"] == "applied" and changed["effective"] == 1 and changed["queued"] == 2
        again = await provider.code_index_lifecycle(**lifecycle.native_request(scope, subscriber="view:second"))
        assert again["task_token"] == opened["alice"]["task_token"] and again["subscribers"] == 2
        assert (await provider.code_index_scheduler(action="status"))["queued"] == 2
        assert await provider.recall("empty synthetic store", owner="alice") == []
        cancelled = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="cancel"))
        assert cancelled["lifecycle"] == "cancelled"
        assert cancelled["task_token"] != opened["alice"]["task_token"]
        (gate / "release").touch()
        for receipt in opened.values():
            token = receipt["task_token"]
            await lifecycle.wait_native(lambda token=token: (gate / (token + ".finished")).exists(), "owned native exit")
        alice = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
        bob = await provider.code_index_lifecycle(**lifecycle.native_request(scope, owner="bob", action="status"))
        old_completion = gate / (opened["alice"]["task_token"] + ".finished")
        assert old_completion.read_text(encoding="utf8") == "rejected"
        # Cancellation before admission creates no synthetic index-run row.
        # The action acknowledges cancellation; later index status is missing.
        assert alice["lifecycle"] == "missing"
        assert alice.get("run_id") is None and alice.get("head_generation") is None
        assert alice["task_token"] == cancelled["task_token"]
        path = Path(provider._local_db_path()).resolve(strict=True)
        assert path.is_relative_to(tmp_path.resolve())
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            assert db.execute("SELECT lifecycle FROM code_registrations WHERE owner='alice'").fetchall() == [("cancelled",)]
            assert db.execute("SELECT count(*) FROM code_index_runs WHERE owner='alice'").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM code_index_heads WHERE owner='alice'").fetchone()[0] == 0
        assert bob["lifecycle"] == "ready" and bob.get("head_generation") is not None
        drained = await readback(provider, saved)
        assert drained["status"] == "applied" and drained["effective"] == 1
        assert drained["active"] == drained["queued"] == 0
    finally:
        (gate / "release").touch()
        await provider.shutdown()

"""Only unfinished semantic postconditions on the closed MG2acB fixture.

No new index, admission, registration, Settings change, source edit or direct
SQL mutation. Cancellation cleanup advanced epoch0 to1; old receipts stay stale.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

import pytest

if os.environ.get("OPENCLANK_S04_POSTCONDITION_ADMISSION") != "retained-MG2acB-postconditions-v1":
    pytest.skip("Explicit retained-store postcondition release required", allow_module_level=True)
from tests import test_code_index_concurrent_memory_native as original

ROOT = Path("/private/tmp/openclank-s03-concurrent-memory.MG2acB")
FIXTURE = ROOT / "pytest-01/test_two_actual_partition_coho0"
PRIOR = FIXTURE / "concurrent-memory-result.json"
PRIOR_SHA = "77320586f64b7d4fcbd14e47034b8b4b2068b8db867da0c2be6a2b760879055a"
LOG = ROOT / "concurrent-memory-01.log"
LOG_SHA = "a31e7111f57386990752ed1daf20eb60c4300332079809a377f2e77465641ca0"
HELPER_SHA = "5b9d8646f30ec039eddb94c76902f4a1782a0b54b32b6bb759c0f0b4ab56e146"
HEADS = {"alice": "codegen_c86d9a84d02f49eabf6495e858e0e98e",
         "bob": "codegen_f2b14b8906964d1fad7b4ea697665275",
         "followup": "codegen_af8c0a4de0b24f06bc1ffb5d1a4234e1"}
ROOT_BINDINGS = {"alice": "4b1fde9c81b5137285c615a7bbd35e9df3cfea5c8cf797ef4d07a1fbdd66b8a8",
                 "bob": "5e66f04692b97fb1b7a6c6fe75032b9b5541daf48d1a8e41ad64c946ef9b1cb7",
                 "followup": "6d5d8876b15b61ed5f9465f1e60a18d4de33f8cd3d0a62877b7d5898b28d098b"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def owned_file(path):
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
    assert path.resolve(strict=True) == path


def state():
    database = FIXTURE / "native-data/fm.db"
    owned_file(database)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1) as db:
        db.execute("PRAGMA query_only=ON")
        return {name: db.execute("SELECT * FROM " + name + " ORDER BY 1,2,3").fetchall()
                for name in ("code_registrations", "code_index_runs", "code_index_heads", "code_scope_epochs")}


def inputs(prior):
    for owner in ("alice", "bob"):
        root = FIXTURE / owner
        assert root.resolve(strict=True) == root and root.is_dir()
        rows = prior["source_inventories"][owner]
        assert sorted(path.name for path in root.iterdir()) == sorted(row["path"] for row in rows)
        for row in rows:
            file = root / row["path"]
            assert file.parent == root
            owned_file(file)
            assert file.stat().st_size == row["bytes"] and sha(file) == row["sha256"]
    followup = FIXTURE / "followup"
    assert sorted(path.name for path in followup.iterdir()) == ["after.py"]
    owned_file(followup / "after.py")
    assert (followup / "after.py").read_bytes() == b"def after_drain(): return 1\n"
    for file in (PRIOR, LOG, FIXTURE / "authority.db", FIXTURE / "native-data/auth.json"):
        owned_file(file)
    assert sha(PRIOR) == PRIOR_SHA and sha(LOG) == LOG_SHA
    assert sha(Path(original.__file__)) == HELPER_SHA
    assert sha(Path(original.BINARY)) == original.BINARY_SHA


def read_contexts(prior):
    with sqlite3.connect(f"file:{FIXTURE / 'native-data/fm.db'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        registrations = db.execute("SELECT * FROM code_registrations ORDER BY owner,workspace_id").fetchall()
        assert len(registrations) == 3
        contexts = {}
        for name, head in HEADS.items():
            old = prior["ready"][name]
            assert old["run_id"] == old["head_generation"] == head
            candidates = [row for row in registrations if row["checkout_id"] == old["checkout_id"]]
            assert len(candidates) == 1
            row = candidates[0]
            assert row["owner"] == ("bob" if name == "bob" else "alice")
            assert row["lifecycle"] == "cancelled" and row["admission_epoch"] == 1
            root = FIXTURE / name
            assert row["root_binding"] == ROOT_BINDINGS[name]
            info = root.stat()
            assert row["directory_identity"] == f"{info.st_dev}:{info.st_ino}"
            run = db.execute("SELECT * FROM code_index_runs WHERE workspace_id=? AND run_id=?",
                             (row["workspace_id"], head)).fetchone()
            assert run["status"] == "ready" and run["admission_epoch"] == 0
            assert run["codebase"] == row["checkout_id"]
            assert run["config_fingerprint"] == row["config_fingerprint"]
            assert db.execute("SELECT run_id FROM code_index_heads WHERE workspace_id=?",
                              (row["workspace_id"],)).fetchall()[0][0] == head
            assert len(run["source_snapshot"].encode()) <= 65536
            snapshot = json.loads(run["source_snapshot"])
            old_receipt = {"workspace_id": row["workspace_id"], "registration_id": row["registration_id"],
                           "source_snapshot_id": snapshot["file_manifest_digest"],
                           "checkout_id": row["checkout_id"], "head_generation": head,
                           "admission_epoch": 0, "policy_generation": row["policy_generation"],
                           "config_fingerprint": row["config_fingerprint"]}
            binding = {"owner": row["owner"], "owner_subject_id": row["owner_subject_id"],
                       "workspace_id": row["workspace_id"], "location_id": row["location_id"],
                       "workspace_revision": int(row["workspace_revision"]),
                       "policy_generation": row["policy_generation"], "root": str(root),
                       "subscriber_id": "view:retained-postconditions", "origin": "app"}
            contexts[name] = (binding, old_receipt, {**old_receipt, "admission_epoch": 1})
        return contexts


@pytest.mark.asyncio
async def test_closed_retained_heads_paged_postconditions_without_index(tmp_path, monkeypatch):
    from src import constants
    from src.frankenmemory_provider import FrankenmemoryProvider
    prior = json.loads(PRIOR.read_text())
    assert prior["status"] == "failed" and prior["semantics"] == {}
    assert prior["overlapping_running_workspaces"] == 2
    assert prior["queued_followup_blocked_until_effective_one"] is True
    assert prior["terminal_scheduler"]["active"] == prior["terminal_scheduler"]["queued"] == 0
    assert prior["terminal_scheduler"]["effective"] == 1
    assert prior["sampled_native_peak_kib"] <= 1024 * 1024
    assert prior["provider_shutdown_completed"] and prior["native_process_absent"]
    inputs(prior)
    before = state()
    contexts = read_contexts(prior)
    logs = tmp_path / "native-owner-logs"
    logs.mkdir(mode=0o700)
    monkeypatch.setattr(constants, "DATA_DIR", str(logs))
    environment = {"HOME": str(FIXTURE / "native-home"),
                   "OPEN_CLANK_DATA_DIR": str(FIXTURE / "native-data"),
                   "ODYSSEUS_DATA_DIR": str(FIXTURE / "native-data"),
                   "FM_DB_PATH": str(FIXTURE / "native-data/fm.db"),
                   "OPEN_CLANK_AUTHORITY_DB_PATH": str(FIXTURE / "authority.db"),
                   "OPEN_CLANK_AUTHORITY_AUTH_PATH": str(FIXTURE / "native-data/auth.json")}
    assert all(Path(value).resolve(strict=True).is_relative_to(FIXTURE) for value in environment.values())
    assert "FM_S03_FIXTURE_GATE" not in os.environ
    provider = FrankenmemoryProvider(command=original.BINARY, env=environment)
    call = provider._call_tool
    async def queries_only(tool, arguments):
        assert tool == "code_query", "Postconditions cannot invoke indexing/settings tools"
        return await call(tool, arguments)
    monkeypatch.setattr(provider, "_call_tool", queries_only)
    async def lifecycle_forbidden(**arguments):
        raise AssertionError("Postconditions cannot enter code lifecycle")
    monkeypatch.setattr(provider, "code_index_lifecycle", lifecycle_forbidden)
    report = {"schema": "openclank.s04-retained-postconditions/v1", "status": "running",
              "prior_failed_report": {"path": str(PRIOR), "sha256": PRIOR_SHA},
              "prior_failed_log": {"path": str(LOG), "sha256": LOG_SHA},
              "prior_failure_preserved": True, "new_index_requested": False,
              "new_memory_measurement": False, "native_sha256": original.BINARY_SHA,
              "helper_sha256": HELPER_SHA, "postcleanup_admission_epoch": 1,
              "publication_admission_epoch": 0, "semantics": {}, "old_receipts_rejected": []}
    identity = None
    try:
        await provider.initialize()
        identity = original.native_identity()
        report["native_process"] = identity
        for name in ("alice", "bob"):
            binding, old_receipt, current_receipt = contexts[name]
            rejected = await asyncio.wait_for(provider.code_query(binding=binding, query={
                "operation": "search", "query": "caller", "expected_receipt": old_receipt}), 60)
            assert rejected.get("error", {}).get("code") == "stale_cursor"
            assert not rejected.get("hits") and not rejected.get("edges")
            report["old_receipts_rejected"].append(name)
            report["semantics"][name] = await asyncio.wait_for(original.selected_semantics(
                provider, binding, HEADS[name], name == "alice", expected_receipt=current_receipt), 300)
        inputs(prior)
        assert state() == before, "Queries cannot change registrations, runs, heads or scope epochs"
        report["source_inventories_unchanged"] = True
        report["index_state_unchanged"] = True
        report["status"] = "passed-postcleanup-postconditions"
    except BaseException as error:
        report["status"] = "failed"
        report["error_class"] = type(error).__name__
        raise
    finally:
        try:
            await asyncio.wait_for(provider.shutdown(), 40)
            report["provider_shutdown_completed"] = True
        finally:
            report["native_process_absent"] = None if identity is None else not any(
                int(row.split()[0]) == identity["pid"] for row in original.ps("-axo", "pid=").splitlines())
            if report["native_process_absent"] is False:
                report["status"] = "failed"
            report["index_state_unchanged_after_shutdown"] = state() == before
            if not report["index_state_unchanged_after_shutdown"]:
                report["status"] = "failed"
            (tmp_path / "retained-postconditions-result.json").write_text(json.dumps(report, indent=2) + "\n")
        assert identity is None or report["native_process_absent"] is True
        assert report["index_state_unchanged_after_shutdown"]

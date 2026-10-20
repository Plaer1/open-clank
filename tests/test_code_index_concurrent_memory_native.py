"""Opt-in, isolated S04 concurrent-memory/drain qualification; no builds.

This is one new combined scenario, not another 1/2/4 measurement series.
200 ms native RSS/status observations are samples, not exact permit/RSS peaks.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

import pytest

BINARY = "/private/tmp/openclank-s07-prior-memo-build.O6HBqj/production-fm-mcp"
BINARY_SHA = "7eb0e26516b1beeb48b82e745d14c02ee1fb7d91acf82c34434e8c15af15a661"
if os.environ.get("OPENCLANK_S04_CONCURRENT_ADMISSION") != "two-partitions-memory-drain-v1":
    pytest.skip("Explicit one-shot concurrent qualification admission required", allow_module_level=True)
assert os.environ.get("OPEN_CLANK_TEST_FM_MCP_BIN") == BINARY
assert os.environ.get("OPENCLANK_S04_NATIVE_SHA256") == BINARY_SHA
assert hashlib.sha256(Path(BINARY).read_bytes()).hexdigest() == BINARY_SHA
from tests.test_code_query_root_transport import _assert_isolated_startup
_assert_isolated_startup()
from tests import test_code_index_lifecycle as lifecycle
from src.frankenmemory_provider import FrankenmemoryProvider
from src.openclank.code_index_settings import THREADS_KEY, prepare_settings, readback

scope = lifecycle.scope
INTERVAL = 0.2
MAX_RSS_KIB = 1024 * 1024
# A finite test observation/failure-cleanup bound, not a product index deadline.
OBSERVATION_SECONDS = 900


def recipe(large=False):
    files = {"lib.py": b"def target(): return 1\n",
             "caller.py": b"from lib import target\ndef caller():\n    target()\n    target()\n"}
    expression = " + ".join(f"({n % 10})" for n in range(128))
    for number in range(129):
        files[f"load_{number:03}.py"] = "".join(
            f"def load_{number:03}_{function:03}(x):\n    return {expression}\n\n"
            for function in range(64)).encode()
    if large:
        # Exact previously proven production >8 MiB evidence retry recipe.
        files["large.py"] = "".join(
            f"def large_result_{n:05}(): return {n}\n" for n in range(6000)).encode()
    assert len(files) == (132 if large else 131)
    assert all(len(body) <= 512 * 1024 for body in files.values())
    return files


def sql_rows(database, query, arguments=()):
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1) as db:
        db.execute("PRAGMA query_only=ON")
        return db.execute(query, arguments).fetchall()


def ps(*arguments):
    return subprocess.check_output(["/bin/ps", *arguments], text=True).strip()


def native_identity():
    children = ps("-axo", "pid=,ppid=").splitlines()
    matches = []
    for row in children:
        pid, parent = map(int, row.split())
        if parent != os.getpid():
            continue
        try:
            if ps("-ww", "-p", str(pid), "-o", "command=") == BINARY:
                assert int(ps("-p", str(pid), "-o", "uid=")) == os.getuid()
                matches.append({"pid": pid, "birth": ps("-p", str(pid), "-o", "lstart=")})
        except subprocess.CalledProcessError:
            continue
    assert len(matches) == 1, "Require exactly one test-owned native child, never source app native"
    return matches[0]


async def selected_semantics(provider, arguments, head, large, expected_receipt=None):
    binding = {key: value for key, value in arguments.items() if key != "action"}
    binding["origin"] = "app"
    async def query(request):
        value = await provider.code_query(binding=binding, query={"budgets": {
            "rows": 50, "bytes": 64512, "tokens": 7168, "hops": 1, "work": 4000000}, **request})
        assert not value.get("error"), "Selected semantic query rejected"
        assert value["receipt"]["head_generation"] == head
        if expected_receipt is not None:
            assert value["receipt"] == expected_receipt
        assert value["cost"]["files_bytes_read"] == value["cost"]["validation_bytes_read"] == 0
        return value
    async def declaration(name):
        value = await query({"operation": "search", "query": name,
                             "channels": ["exact"], "fields": ["label", "source"],
                             "filter": {"kinds": ["declaration"]}})
        matches = [hit for hit in value["hits"] if hit.get("label") == name]
        assert len(matches) == 1
        return matches[0], value["receipt"]
    caller, receipt = await declaration("caller")
    target, other_receipt = await declaration("target")
    assert receipt == other_receipt
    request = {"operation": "neighbors", "seed_ids": [caller["id"]],
               "direction": "outgoing", "fields": ["label", "source", "witnesses"],
               "expected_receipt": receipt}
    async def complete_neighbors():
        hits, edges, paths, cursors = [], [], [], set()
        hit_ids, occurrence_ids = set(), set()
        cursor = None
        for page in range(128):
            value = await query({**request, **({"cursor": cursor} if cursor is not None else {})})
            assert value["receipt"] == receipt
            assert "source_coverage_partial" not in value["reasons"]
            assert not value["degraded"]
            for hit in value["hits"]:
                assert hit["id"] not in hit_ids
                hit_ids.add(hit["id"])
            for edge in value["edges"]:
                assert edge["occurrence_id"] not in occurrence_ids
                occurrence_ids.add(edge["occurrence_id"])
            hits.extend(value["hits"])
            edges.extend(value["edges"])
            paths.extend(value["paths"])
            next_cursor = value["next_cursor"]
            if next_cursor is None:
                assert not value["partial"]
                return {"hits": hits, "edges": edges, "paths": paths}, page + 1
            assert value["partial"], "Native partial includes unfinished cursor units"
            assert value["hits"] or value["edges"] or value["paths"], "No advancing returned unit"
            assert isinstance(next_cursor, str) and 0 < len(next_cursor) <= 4096
            assert next_cursor not in cursors
            cursors.add(next_cursor)
            cursor = next_cursor
        raise AssertionError("Neighbor sequence unfinished at fixed 128-page ceiling")
    first, first_pages = await complete_neighbors()
    second, second_pages = await complete_neighbors()
    assert first == second
    exact = [edge for edge in first["edges"] if edge["from"] == caller["id"]
             and edge["to"] == target["id"] and edge["resolution_state"] == "exact"]
    assert len(exact) == 2 and len({edge["occurrence_id"] for edge in exact}) == 2
    assert caller["source"]["rel_path"] == "caller.py"
    assert target["source"]["rel_path"] == "lib.py"
    if large:
        for label in ("large_result_00000", "large_result_05999"):
            hit, same_receipt = await declaration(label)
            assert same_receipt == receipt and hit["source"]["rel_path"] == "large.py"
    canonical = {"caller": caller, "target": target, "edges": exact}
    return {"exact_calls": 2, "repeat_equal": True,
            "neighbor_page_counts": [first_pages, second_pages], "receipt": receipt,
            "canonical_selected_sha256": hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "large_endpoints_checked": large, "full_corpus_parity_claimed": False}


@pytest.mark.asyncio
async def test_two_actual_partition_cohorts_memory_and_active_setting_drain(scope, tmp_path, monkeypatch):
    assert "OPENCLANK_S04_POSTCONDITION_ADMISSION" not in os.environ, "Postconditions cannot schedule another index"
    from src import constants, settings
    repository, workspaces, _bindings, _provider = scope
    data, home = tmp_path / "native-data", tmp_path / "native-home"
    data.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_settings_cache", None)
    monkeypatch.setattr(constants, "DATA_DIR", str(data))
    monkeypatch.setattr(constants, "APP_DB", repository.db_path)
    monkeypatch.setattr(constants, "AUTH_FILE", str(data / "auth.json"))
    monkeypatch.setattr(constants, "FM_DB_PATH", str(data / "fm.db"))
    environment = {"HOME": str(home), "OPEN_CLANK_DATA_DIR": str(data),
                   "ODYSSEUS_DATA_DIR": str(data), "FM_DB_PATH": str(data / "fm.db"),
                   "OPEN_CLANK_AUTHORITY_DB_PATH": repository.db_path,
                   "OPEN_CLANK_AUTHORITY_AUTH_PATH": str(data / "auth.json")}
    for key, value in environment.items():
        assert Path(value).resolve().is_relative_to(tmp_path.resolve()), key
        monkeypatch.setenv(key, value)
    assert "FM_S03_FIXTURE_GATE" not in os.environ
    (data / "auth.json").write_text(json.dumps({"users": lifecycle.Auth.users}))
    sources = {}
    for owner in ("alice", "bob"):
        root = Path(lifecycle.native_request(scope, owner=owner)["root"])
        sources[owner] = recipe(large=owner == "alice")
        for name, body in sources[owner].items():
            (root / name).write_bytes(body)
    # A third scope is necessary: an ensure of either active scope coalesces.
    followup_root = tmp_path / "followup"
    followup_root.mkdir()
    (followup_root / "after.py").write_text("def after_drain(): return 1\n")
    location = repository.create_location(actor_subject_id="s03-admin", path=str(followup_root),
                                          kind="directory", capabilities=("read",))
    for kind in ("people", "agent"):
        repository.create_binding(actor_subject_id="s03-admin", binding_class=kind,
                                  subject_id=lifecycle.Auth().account_id("alice"),
                                  location_id=location.id, capabilities=("read",))
    followup = repository.create_workspace(actor_subject_id=lifecycle.Auth().account_id("alice"),
                                          owner_subject_id=lifecycle.Auth().account_id("alice"),
                                          location_id=location.id, name="followup")
    arguments = {owner: lifecycle.native_request(scope, owner=owner) for owner in ("alice", "bob")}
    arguments["followup"] = {**arguments["alice"], "workspace_id": followup.id,
        "location_id": location.id, "workspace_revision": followup.revision,
        "root": str(followup_root), "subscriber_id": "view:after-drain"}
    database = Path(environment["FM_DB_PATH"])
    report = {"schema": "openclank.s04-concurrent-memory/v1", "status": "unrun",
              "native_sha256": BINARY_SHA, "sample_interval_seconds": INTERVAL,
              "rss_limit_kib": MAX_RSS_KIB, "observation_bound_seconds": OBSERVATION_SECONDS,
              "rss_domain": "sampled sole native child; not exact high-water or whole app",
              "permit_peak_claimed": False, "samples": [], "source_inventories": {},
              "settings_transition": [], "ready": {}, "semantics": {}, "native_source_hashes": {}}
    for owner, files in sources.items():
        report["source_inventories"][owner] = [
            {"path": name, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            for name, body in sorted(files.items())]
    provider = FrankenmemoryProvider(command=BINARY, env=environment)
    identity = None
    passed = False
    started = time.monotonic()
    async def observe():
        assert time.monotonic() - started <= OBSERVATION_SECONDS, "Test observation exhausted; no product deadline changed"
        assert len(report["samples"]) < 5000
        assert ps("-p", str(identity["pid"]), "-o", "lstart=") == identity["birth"]
        raw_rss, raw_cpu = ps("-p", str(identity["pid"]), "-o", "rss=,%cpu=").split()
        rss, cpu = int(raw_rss), float(raw_cpu)
        status = await provider.code_index_scheduler(action="status")
        report["samples"].append({"seconds": time.monotonic() - started, "rss_kib": rss, "cpu_percent": cpu, **status})
        assert rss <= MAX_RSS_KIB, "Sampled native RSS exceeded declared fixture ceiling"
        assert status["active"] <= status["effective"]
        return status
    def running():
        return {row[0] for row in sql_rows(database,
            "SELECT workspace_id FROM code_index_runs WHERE status='running'")}
    try:
        report["status"] = "running"
        await provider.initialize()
        identity = native_identity()
        report["native_process"] = identity
        baseline = await observe()
        assert (baseline["desired"], baseline["effective"], baseline["revision"]) == (2, 2, 0)
        assert sql_rows(database, "SELECT count(*) FROM code_index_runs")[0][0] == 0
        assert sql_rows(database, "SELECT count(*) FROM code_index_heads")[0][0] == 0
        opened = await asyncio.gather(*(provider.code_index_lifecycle(**arguments[owner])
                                        for owner in ("alice", "bob")))
        assert len({value["task_token"] for value in opened}) == 2
        wanted = {workspaces[owner].id for owner in ("alice", "bob")}
        while True:
            status = await observe()
            if wanted <= running() and status["active"] > 0:
                break
            assert not sql_rows(database, "SELECT 1 FROM code_index_heads LIMIT 1"), "Both cold cohorts must actually overlap"
            await asyncio.sleep(INTERVAL)
        report["overlapping_running_workspaces"] = 2
        saved = prepare_settings(settings.load_settings(), {THREADS_KEY: 1})
        settings.save_settings(saved)
        pending = await readback(provider, saved, apply=True)
        report["settings_transition"].append(pending)
        assert pending["status"] == "draining" and pending["drain_pending"]
        assert (pending["desired"], pending["effective"]) == (1, 2)
        await provider.code_index_lifecycle(**arguments["followup"])
        saw_queued_pending = False
        while True:
            status = await observe()
            followup_runs = sql_rows(database, "SELECT run_id FROM code_index_runs WHERE workspace_id=?", (followup.id,))
            after_sql = await provider.code_index_scheduler(action="status")
            if status["drain_pending"] and after_sql["drain_pending"]:
                assert status["effective"] == after_sql["effective"] == 2 and not followup_runs
                saw_queued_pending |= status["queued"] > 0
            elif not status["drain_pending"] and followup_runs:
                assert saw_queued_pending and status["effective"] == 1
                assert status["effective_revision"] == saved["code_index_settings_revision"]
                break
            await asyncio.sleep(INTERVAL)
        report["queued_followup_blocked_until_effective_one"] = True
        while len(report["ready"]) < 3:
            await observe()
            for name, request in arguments.items():
                if name in report["ready"]:
                    continue
                value = await provider.code_index_lifecycle(**{**request, "action": "status"})
                assert value["lifecycle"] not in {"failed", "erased", "cancelled", "missing"}
                assert not value.get("error"), "Index worker failed"
                if value["lifecycle"] == "ready":
                    assert value["coverage_complete"] and value["head_generation"] == value["run_id"]
                    report["ready"][name] = {key: value[key] for key in ("run_id", "head_generation", "checkout_id")}
                    if name in sources:
                        actual = {row["rel_path"]: row for row in value["coverage"]}
                        assert set(actual) == set(sources[name])
                        for path, body in sources[name].items():
                            assert actual[path]["status"] == "indexed"
                            # Native source identities are BLAKE3, not SHA256.
                            # Independent SHA256 inventory is checked at exit;
                            # identical shared files must have identical native hashes.
                            assert re.fullmatch(r"[0-9a-f]{64}", actual[path]["source_hash"])
                        report["native_source_hashes"][name] = {path: actual[path]["source_hash"] for path in actual}
            await asyncio.sleep(INTERVAL)
        terminal = await observe()
        assert terminal["active"] == terminal["queued"] == 0 and not terminal["drain_pending"]
        report["terminal_scheduler"] = terminal
        for name, request in arguments.items():
            expected_files = len(sources[name]) if name in sources else 1
            rows = sql_rows(database,
                "SELECT status,files_indexed,files_unchanged,files_removed,errors FROM code_index_runs WHERE workspace_id=?",
                (request["workspace_id"],))
            assert rows == [("ready", expected_files, 0, 0, "[]")]
        assert all(report["native_source_hashes"]["alice"][path] == digest
                   for path, digest in report["native_source_hashes"]["bob"].items())
        # Post-memory semantic reads are separate from the concurrent index samples.
        for owner in ("alice", "bob"):
            report["semantics"][owner] = await selected_semantics(
                provider, arguments[owner], report["ready"][owner]["head_generation"], owner == "alice")
        for owner, files in sources.items():
            root = Path(arguments[owner]["root"])
            assert set(path.name for path in root.iterdir()) == set(files)
            assert all((root / name).read_bytes() == body for name, body in files.items())
        assert hashlib.sha256(Path(BINARY).read_bytes()).hexdigest() == BINARY_SHA
        passed = True
        report["status"] = "passed"
    finally:
        if not passed:
            report["status"] = "failed"
            for request in arguments.values():
                try:
                    await asyncio.wait_for(provider.code_index_lifecycle(**{**request, "action": "cancel", "root": None}), 5)
                except Exception:
                    pass  # Cleanup is followed by owned provider shutdown, never restart.
        try:
            await asyncio.wait_for(provider.shutdown(), 40)
            report["provider_shutdown_completed"] = True
        finally:
            report["sampled_native_peak_kib"] = max((s["rss_kib"] for s in report["samples"]), default=None)
            report["duration_seconds"] = time.monotonic() - started
            report["native_process_absent"] = None if identity is None else not any(
                int(row.split()[0]) == identity["pid"] for row in ps("-axo", "pid=").splitlines())
            if report["native_process_absent"] is False:
                report["status"] = "failed"
            (tmp_path / "concurrent-memory-result.json").write_text(json.dumps(report, indent=2) + "\n")
            settings._invalidate_caches()
        assert identity is None or report["native_process_absent"] is True, "Owned native child must be absent after shutdown"

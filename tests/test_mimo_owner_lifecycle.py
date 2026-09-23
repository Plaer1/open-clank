import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as database
import src.openclank.mimo_supervisor as mimo_supervisor
from core.database import Base
from src.openclank.mimo_supervisor import (
    MimoSupervisor,
    MimoSupervisorPool,
    SupervisorAdmissionError,
    _pick_small_model,
)


def _database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'owner-lifecycle.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    return sessions


def test_open_clank_agent_env_names_override_legacy_names(monkeypatch):
    monkeypatch.setenv("OPEN_CLANK_SMALL_MODEL", "open-clank/primary")
    monkeypatch.setenv("ODYSSEUS_SMALL_MODEL", "legacy/fallback")
    assert _pick_small_model({}) == "open-clank/primary"

    monkeypatch.setenv("OPEN_CLANK_AGENT_PORT", "43111")
    monkeypatch.setenv("ODYSSEUS_MIMO_PORT", "43112")
    assert MimoSupervisor(partitioned=False)._http_port == 43111

    monkeypatch.delenv("OPEN_CLANK_SMALL_MODEL")
    monkeypatch.delenv("OPEN_CLANK_AGENT_PORT")
    assert _pick_small_model({}) == "legacy/fallback"
    assert MimoSupervisor(partitioned=False)._http_port == 43112


def test_partitioned_workers_receive_the_central_owner_filtered_skill_catalog(
    tmp_path,
    monkeypatch,
):
    skills_root = tmp_path / "central" / "skills"
    monkeypatch.setattr(
        mimo_supervisor,
        "_OPEN_CLANK_SKILLS_DIR",
        str(skills_root),
    )
    alice = {
        "OPEN_CLANK_OWNER": "alice",
        "OPEN_CLANK_DATA_DIR": str(tmp_path / "owners" / "alice"),
    }
    bob = {
        "OPEN_CLANK_OWNER": "bob",
        "OPEN_CLANK_DATA_DIR": str(tmp_path / "owners" / "bob"),
    }

    mimo_supervisor._inject_skill_catalog(alice)
    mimo_supervisor._inject_skill_catalog(bob)

    for owner, env in (("alice", alice), ("bob", bob)):
        assert env["OPEN_CLANK_OWNER"] == owner
        assert env["OPEN_CLANK_SKILLS_DIR"] == str(skills_root)
        config = json.loads(env["MIMOCODE_CONFIG_CONTENT"])
        assert config["skills"]["paths"] == [str(skills_root)]
        assert config["memory"] == {"provider": "frankenmemory"}
    assert alice["OPEN_CLANK_DATA_DIR"] != bob["OPEN_CLANK_DATA_DIR"]


@pytest.mark.asyncio
async def test_projection_cleanup_only_touches_the_current_worker_partition():
    worker = MimoSupervisor(owner="recipient", partitioned=True)
    worker._bridge = SimpleNamespace(
        mapped_sessions=lambda: {"this-partition": "mimo-this"},
    )
    deleted = []

    async def delete(session_id, *, mimo_session_id=None):
        deleted.append((session_id, mimo_session_id))

    worker.delete_session = delete
    await worker._purge_stale_projections()

    assert deleted == [("this-partition", "mimo-this")]


@pytest.mark.asyncio
async def test_session_negotiate_and_config_reuse_the_mapped_runtime_session():
    worker = MimoSupervisor(owner="alice", partitioned=True)
    calls = []

    class Bridge:
        def negotiated_state(self, session_id):
            calls.append(("state", session_id))
            return {"commands": [{"name": "compact"}]}

        async def ensure_session(self, session_id, *, cwd=None, owner=None):
            calls.append(("ensure", session_id, cwd, owner))
            return "mimo-a"

        async def set_config_option(self, session_id, config_id, value, *, cwd=None, owner=None):
            calls.append(("config", session_id, config_id, value, cwd, owner))
            return {"current": {config_id: value}}

    worker._bridge = Bridge()
    worker.is_alive = lambda: True
    negotiated = await worker.negotiate_session("chat-a", owner="alice", cwd="/workspace/a")
    configured = await worker.set_session_config("chat-a", "mode", "build", owner="alice", cwd="/workspace/a")

    assert negotiated["commands"]
    assert configured == {"current": {"mode": "build"}}
    assert calls == [
        ("ensure", "chat-a", "/workspace/a", "alice"),
        ("state", "chat-a"),
        ("config", "chat-a", "mode", "build", "/workspace/a", "alice"),
    ]


@pytest.mark.asyncio
async def test_model_catalog_warmup_timeout_does_not_block_worker_start(
    monkeypatch,
    caplog,
):
    worker = MimoSupervisor(owner="alice", partitioned=True)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingBridge:
        available_models = []

        async def open_session(self, *, with_agent_tools):
            assert with_agent_tools is False
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    worker._bridge = BlockingBridge()
    monkeypatch.setattr(mimo_supervisor, "_MODEL_CATALOG_WARMUP_TIMEOUT", 0.01)

    await worker._warm_model_catalog()

    assert started.is_set()
    assert cancelled.is_set()
    assert "deferring to first session" in caplog.text


async def test_supervisor_rename_and_purge_cover_owner_runtime(tmp_path, monkeypatch):
    _database(tmp_path, monkeypatch)
    pool = MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)
    old_runtime = pool._runtime_home("alice")
    old_runtime.mkdir(parents=True)
    (old_runtime / "session-map.json").write_text("{}", encoding="utf-8")

    await pool.rename_owner("alice", "alice2")
    new_runtime = pool._runtime_home("alice2")
    assert not old_runtime.exists()
    assert (new_runtime / "session-map.json").read_text(encoding="utf-8") == "{}"

    await pool.purge_owner("alice2")
    assert not new_runtime.exists()


@pytest.mark.asyncio
async def test_owner_memory_reset_clears_only_authored_memory_sidecars(
    tmp_path,
    monkeypatch,
):
    _database(tmp_path, monkeypatch)
    pool = MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)

    alice_data = pool._runtime_home("alice") / "mimocode" / "data"
    memory_root = alice_data / "memory" / "projects" / "demo"
    memory_root.mkdir(parents=True)
    authored = memory_root / "MEMORY.md"
    checkpoint = memory_root / "checkpoint.md"
    authored.write_text("private preference", encoding="utf-8")
    checkpoint.write_text("unfinished task", encoding="utf-8")
    db = alice_data / "mimocode.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE memory_fts(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              path TEXT NOT NULL UNIQUE,
              scope TEXT NOT NULL,
              scope_id TEXT NOT NULL DEFAULT '',
              type TEXT NOT NULL,
              body TEXT NOT NULL,
              fingerprint TEXT NOT NULL,
              last_indexed_at INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO memory_fts(path,scope,scope_id,type,body,fingerprint,last_indexed_at) VALUES (?,?,?,?,?,?,?)",
            (str(authored), "projects", "demo", "memory", "private preference", "m1", 1),
        )
        connection.execute(
            "INSERT INTO memory_fts(path,scope,scope_id,type,body,fingerprint,last_indexed_at) VALUES (?,?,?,?,?,?,?)",
            (str(checkpoint), "projects", "demo", "checkpoint", "unfinished task", "c1", 1),
        )

    bob_data = pool._runtime_home("bob") / "mimocode" / "data" / "memory" / "global"
    bob_data.mkdir(parents=True)
    bob_memory = bob_data / "MEMORY.md"
    bob_memory.write_text("bob survives", encoding="utf-8")

    preview = await pool.preview_owner_memory("alice")
    assert preview["count"] == 2
    result = await pool.reset_owner_memory("alice", expected=preview)

    assert result == {"complete": True, "count": 2}
    assert not authored.exists()
    assert checkpoint.read_text(encoding="utf-8") == "unfinished task"
    assert bob_memory.read_text(encoding="utf-8") == "bob survives"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT type FROM memory_fts ORDER BY type"
        ).fetchall() == [("checkpoint",)]


@pytest.mark.asyncio
async def test_owner_memory_reset_rejects_stale_preview(tmp_path, monkeypatch):
    _database(tmp_path, monkeypatch)
    pool = MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)
    memory_root = (
        pool._runtime_home("alice")
        / "mimocode"
        / "data"
        / "memory"
        / "global"
    )
    memory_root.mkdir(parents=True)
    first = memory_root / "MEMORY.md"
    first.write_text("first", encoding="utf-8")
    preview = await pool.preview_owner_memory("alice")
    (memory_root / "memory-extra.md").write_text("second", encoding="utf-8")

    with pytest.raises(RuntimeError, match="stale"):
        await pool.reset_owner_memory("alice", expected=preview)

    assert first.exists()


@pytest.mark.asyncio
async def test_auth_disabled_memory_reset_covers_the_single_local_runtime(
    tmp_path, monkeypatch
):
    _database(tmp_path, monkeypatch)
    pool = MimoSupervisorPool(auth_enabled=False, data_dir=tmp_path)
    memory_root = pool._agent_runtime_root / "data" / "memory" / "global"
    memory_root.mkdir(parents=True)
    authored = memory_root / "MEMORY.md"
    authored.write_text("local authored memory", encoding="utf-8")

    preview = await pool.preview_owner_memory("")
    assert preview["count"] == 1
    result = await pool.reset_owner_memory("", expected=preview)

    assert result == {"complete": True, "count": 1}
    assert not authored.exists()


class _Worker:
    def __init__(self, name):
        self.name = name
        self.stop_calls = 0
        self._runtime_home = None

    async def stop(self):
        self.stop_calls += 1


@pytest.mark.asyncio
async def test_managed_operation_is_generation_pinned_and_released():
    pool = object.__new__(MimoSupervisorPool)
    calls = []
    releases = []

    class Worker:
        async def managed_engine_call(self, method, payload):
            calls.append((method, payload))
            return {"state": "complete"}

    class Lease:
        worker = Worker()

        async def release(self, *, successful_terminal=False):
            releases.append(successful_terminal)

    async def admit(owner):
        assert owner == "alice"
        return Lease()

    pool.admit_provider_control = admit
    payload = {"operation": "image.generate", "rootOperationID": "root-1"}

    result = await pool.execute_operation("alice", payload)

    assert result == {"state": "complete"}
    assert calls == [("_openclank/operations/v1/execute", payload)]
    assert releases == [True]


@pytest.mark.asyncio
async def test_managed_operation_failure_releases_generation_unsuccessfully():
    pool = object.__new__(MimoSupervisorPool)
    releases = []

    class Worker:
        async def managed_engine_call(self, _method, _payload):
            raise RuntimeError("engine unavailable")

    class Lease:
        worker = Worker()

        async def release(self, *, successful_terminal=False):
            releases.append(successful_terminal)

    async def admit(_owner):
        return Lease()

    pool.admit_provider_control = admit
    with pytest.raises(RuntimeError, match="engine unavailable"):
        await pool.execute_operation("alice", {"operation": "audio.transcribe"})
    assert releases == [False]


@pytest.mark.asyncio
async def test_purge_quiesces_active_inflight_and_owner_background_tasks(
    tmp_path,
    monkeypatch,
):
    _database(tmp_path, monkeypatch)
    pool = MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)
    active = _Worker("active")
    retired = _Worker("retired")
    state = pool._owner_state("alice")
    state.active = active
    state.in_flight[active] = 1
    state.in_flight[retired] = 1
    state.drain_events[active] = asyncio.Event()
    state.drain_events[retired] = asyncio.Event()
    pool._workers["alice"] = active
    old_epoch = pool._owner_lifecycle_epoch("alice")

    owner_task_drained = asyncio.Event()
    other_task_drained = asyncio.Event()

    async def wait_forever(drained):
        try:
            await asyncio.Future()
        finally:
            drained.set()

    pool._schedule_background(
        wait_forever(owner_task_drained),
        owner="alice",
    )
    pool._schedule_background(
        wait_forever(other_task_drained),
        owner="bob",
    )
    await asyncio.sleep(0)

    await pool.purge_owner("alice")

    assert active.stop_calls == 1
    assert retired.stop_calls == 1
    assert owner_task_drained.is_set()
    assert not other_task_drained.is_set()
    assert "alice" not in pool._workers
    assert "alice" not in pool._states

    # A lease unwinding after account deletion must not contaminate a newly
    # recreated account with the same username.
    replacement = pool._owner_state("alice")
    await pool._release_lease(
        "alice",
        active,
        successful_terminal=False,
        owner_epoch=old_epoch,
    )
    assert active not in replacement.in_flight

    remaining = [
        task
        for task, owner in pool._background_task_owners.items()
        if owner == "bob"
    ]
    for task in remaining:
        task.cancel()
    await asyncio.gather(*remaining, return_exceptions=True)


@pytest.mark.asyncio
async def test_policy_invalidation_stops_owner_generations_without_purging_runtime_or_grants(
    tmp_path,
    monkeypatch,
):
    _database(tmp_path, monkeypatch)
    pool = MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)
    active = _Worker("active")
    retired = _Worker("retired")
    state = pool._owner_state("alice")
    state.active = active
    state.in_flight[retired] = 1
    pool._workers["alice"] = active
    runtime = pool._runtime_home("alice")
    runtime.mkdir(parents=True)
    marker = runtime / "keep.txt"
    marker.write_text("retained", encoding="utf-8")
    pool._grant_store.add("read", "*", owner="alice")
    previous_epoch = pool._owner_lifecycle_epoch("alice")

    await pool.invalidate_owner_projection("alice")

    assert active.stop_calls == 1
    assert retired.stop_calls == 1
    assert "alice" not in pool._workers
    assert "alice" not in pool._states
    assert marker.read_text(encoding="utf-8") == "retained"
    assert pool._grant_store.match("read", owner="alice")
    assert pool._owner_lifecycle_epoch("alice") == previous_epoch + 1
    assert "alice" not in pool._owner_lifecycle_blocked


@pytest.mark.asyncio
async def test_recipient_opt_out_does_not_restart_the_owner_worker(
    tmp_path,
    monkeypatch,
):
    _database(tmp_path, monkeypatch)
    pool = MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)
    personal = _Worker("personal")
    pool._workers["recipient"] = personal

    await pool.revoke_shared_access("recipient", "grant")

    assert personal.stop_calls == 0


@pytest.mark.asyncio
async def test_rename_waits_for_owner_lock_then_stops_all_generations(
    tmp_path,
    monkeypatch,
):
    _database(tmp_path, monkeypatch)
    pool = MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)
    active = _Worker("active")
    candidate_or_retired = _Worker("candidate-or-retired")
    state = pool._owner_state("alice")
    state.active = active
    state.in_flight[candidate_or_retired] = 1
    pool._workers["alice"] = active

    lock = pool._owner_lock("alice")
    await lock.acquire()
    rename = asyncio.create_task(pool.rename_owner("alice", "alice2"))
    await asyncio.sleep(0)
    assert active.stop_calls == 0
    assert candidate_or_retired.stop_calls == 0
    lock.release()
    await rename

    assert active.stop_calls == 1
    assert candidate_or_retired.stop_calls == 1
    assert "alice" not in pool._workers
    assert "alice" not in pool._states


@pytest.mark.asyncio
async def test_rename_fences_and_stops_unpublished_startup_candidate(
    tmp_path,
    monkeypatch,
):
    _database(tmp_path, monkeypatch)
    pool = MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)
    candidate = _Worker("candidate")
    candidate_started = asyncio.Event()
    let_candidate_finish = asyncio.Event()
    snapshot = SimpleNamespace(fingerprint="candidate-fingerprint")

    import src.openclank.mimo_projection as projection

    monkeypatch.setattr(
        projection,
        "build_projection_snapshot",
        lambda _owner: snapshot,
    )
    monkeypatch.setattr(
        projection,
        "reconcile_projection",
        lambda _snapshot, *, materializing: {"generation": 1},
    )
    monkeypatch.setattr(projection, "mark_projection", lambda *_args, **_kwargs: None)

    async def start_campaign(*_args, **_kwargs):
        candidate_started.set()
        await let_candidate_finish.wait()
        return candidate

    monkeypatch.setattr(pool, "_start_campaign", start_campaign)
    admission = asyncio.create_task(pool._ensure_worker("alice"))
    await candidate_started.wait()

    rename = asyncio.create_task(pool.rename_owner("alice", "alice2"))
    await asyncio.sleep(0)
    let_candidate_finish.set()

    with pytest.raises(SupervisorAdmissionError) as caught:
        await admission
    await rename

    assert caught.value.code == "SUPERVISOR_UNAVAILABLE"
    assert candidate.stop_calls == 1
    assert "alice" not in pool._workers
    assert "alice" not in pool._states

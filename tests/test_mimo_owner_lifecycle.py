import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as database
from core.database import Base, MimoAuthStore, MimoProjectionState
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


async def test_supervisor_rename_and_purge_cover_projection_state(tmp_path, monkeypatch):
    sessions = _database(tmp_path, monkeypatch)
    with sessions() as db:
        db.add(MimoAuthStore(owner="alice", payload="{}"))
        db.add(
            MimoProjectionState(
                owner_id="alice",
                desired_fingerprint="fingerprint",
                generation=3,
                status="installed",
            )
        )
        db.commit()

    pool = MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)
    await pool.rename_owner("alice", "alice2")
    with sessions() as db:
        assert db.get(MimoAuthStore, "alice") is None
        assert db.get(MimoProjectionState, "alice") is None
        assert db.get(MimoAuthStore, "alice2") is not None
        assert db.get(MimoProjectionState, "alice2").generation == 3

    await pool.purge_owner("alice2")
    with sessions() as db:
        assert db.get(MimoAuthStore, "alice2") is None
        assert db.get(MimoProjectionState, "alice2") is None


class _Worker:
    def __init__(self, name):
        self.name = name
        self.stop_calls = 0
        self._runtime_home = None

    async def stop(self):
        self.stop_calls += 1


@pytest.mark.asyncio
async def test_purge_quiesces_active_inflight_and_owner_background_tasks(
    tmp_path,
    monkeypatch,
):
    _database(tmp_path, monkeypatch)
    pool = MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)
    active = _Worker("active")
    retired = _Worker("retired")
    shared = _Worker("shared")
    state = pool._owner_state("alice")
    state.active = active
    state.in_flight[active] = 1
    state.in_flight[retired] = 1
    state.drain_events[active] = asyncio.Event()
    state.drain_events[retired] = asyncio.Event()
    pool._workers["alice"] = active
    share_partition = pool._share_partition("bob", "alice-source")
    share_state = pool._owner_state(share_partition)
    share_state.active = shared
    share_state.credential_owner = "alice"
    pool._share_workers[share_partition] = shared
    share_runtime = pool._runtime_home(share_partition)
    share_runtime.mkdir(parents=True)
    (share_runtime / "copied-auth.json").write_text("secret", encoding="utf-8")
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
    assert shared.stop_calls == 1
    assert owner_task_drained.is_set()
    assert not other_task_drained.is_set()
    assert "alice" not in pool._workers
    assert "alice" not in pool._states
    assert share_partition not in pool._share_workers
    assert share_partition not in pool._states
    assert not share_runtime.exists()

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
async def test_recipient_opt_out_stops_only_that_shared_partition(
    tmp_path,
    monkeypatch,
):
    _database(tmp_path, monkeypatch)
    pool = MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)
    personal = _Worker("personal")
    shared = _Worker("shared")
    pool._workers["recipient"] = personal
    partition = pool._share_partition("recipient", "grant")
    state = pool._owner_state(partition)
    state.active = shared
    pool._share_workers[partition] = shared
    runtime = pool._runtime_home(partition)
    runtime.mkdir(parents=True)

    await pool.revoke_shared_access("recipient", "grant")

    assert shared.stop_calls == 1
    assert personal.stop_calls == 0
    assert partition not in pool._states
    assert partition not in pool._share_workers
    assert not runtime.exists()


@pytest.mark.asyncio
async def test_rename_waits_for_owner_lock_then_stops_all_generations(
    tmp_path,
    monkeypatch,
):
    _database(tmp_path, monkeypatch)
    pool = MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)
    active = _Worker("active")
    candidate_or_retired = _Worker("candidate-or-retired")
    shared = _Worker("shared")
    state = pool._owner_state("alice")
    state.active = active
    state.in_flight[candidate_or_retired] = 1
    pool._workers["alice"] = active
    share_partition = pool._share_partition("alice", "recipient-route")
    share_state = pool._owner_state(share_partition)
    share_state.active = shared
    share_state.credential_owner = "bob"
    pool._share_workers[share_partition] = shared

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
    assert shared.stop_calls == 1
    assert "alice" not in pool._workers
    assert "alice" not in pool._states
    assert share_partition not in pool._share_workers
    assert share_partition not in pool._states


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

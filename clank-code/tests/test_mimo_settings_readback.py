"""Ready readback uses a pinned generation without starting or admitting one."""
import asyncio
from types import SimpleNamespace
import pytest
from src.openclank.mimo_supervisor import MimoSupervisorPool, SupervisorAdmissionError
from src.agent_settings import agent_settings_revision

@pytest.fixture
def ready_pool(monkeypatch, tmp_path):
    pool = MimoSupervisorPool(data_dir=tmp_path)
    monkeypatch.setattr(pool, "_owner_lifecycle_epoch", lambda owner: 0)
    monkeypatch.setattr(pool, "_assert_owner_lifecycle", lambda owner, epoch: None)
    async def forbidden(*args): pytest.fail("readback cold admission")
    monkeypatch.setattr(pool, "_ensure_worker", forbidden)
    release = []
    async def released(*args): release.append(args)
    monkeypatch.setattr(pool, "_release_lease", released)
    import routes.prefs_routes as prefs
    monkeypatch.setattr(prefs, "_load_for_user", lambda owner: {"agent_settings": {"compaction": {"auto": False}}})
    calls = []
    bridge = SimpleNamespace(mapped_sessions=lambda owner: {"chat": "ses_native"} if owner == "alice" else {}, mapped_session_workspace=lambda sid: "/owned/workspace", mapped_session_id=lambda sid: "ses_native")
    async def call(method, params):
        calls.append((method, params)); return {"target": {"directory": "/owned/workspace", "sessionID": "ses_native"}, "compaction": {"auto": True}}
    worker = SimpleNamespace(_bridge=bridge, is_alive=lambda: True, installed_generation=3, managed_engine_call=call)
    state = pool._owner_state("alice")
    state.active = worker
    # Hashable production workers are required by lifecycle maps.
    class Worker:
        _bridge = bridge
        installed_generation = 3
        def is_alive(self): return True
        async def managed_engine_call(self, method, params): return await call(method, params)
    state.active = Worker()
    state.snapshot = SimpleNamespace(run_closure=lambda p, m: "closure" if (p, m) == ("provider", "model") else None, providers={"provider": {"options": {"_openclankAgentSettings": {}}}})
    return pool, calls, release

def test_scoped_readback_marks_old_generation_and_strips_private_target(ready_pool):
    pool, calls, release = ready_pool
    result = asyncio.run(pool.ready_effective_agent_settings("alice", "provider", "model", session_id="chat"))
    assert calls[0][1] == {"providerID": "provider", "modelID": "model", "directory": "/owned/workspace", "sessionID": "ses_native"}
    assert result["generation"] == 3 and result["projection_pending"] is True
    assert result["projected_revision"] == agent_settings_revision({})
    assert "target" not in result and len(release) == 1

def test_readback_other_owner_session_or_unprojected_route_never_calls_engine(ready_pool):
    pool, calls, release = ready_pool
    for owner, model, chat, code in [("bob", "model", "chat", "SUPERVISOR_UNAVAILABLE"), ("alice", "other", "chat", "MODEL_NOT_PROJECTED"), ("alice", "model", "other", "SESSION_NOT_READY")]:
        with pytest.raises(SupervisorAdmissionError) as exc: asyncio.run(pool.ready_effective_agent_settings(owner, "provider", model, session_id=chat))
        assert exc.value.code == code
    assert calls == [] and release == []

def test_readback_revocation_during_rpc_is_fenced_and_releases(ready_pool, monkeypatch):
    pool, calls, release = ready_pool
    checks = []
    def check(owner, epoch):
        checks.append(epoch)
        if len(checks) > 1: raise SupervisorAdmissionError("SUPERVISOR_UNAVAILABLE", "revoked")
    monkeypatch.setattr(pool, "_assert_owner_lifecycle", check)
    with pytest.raises(SupervisorAdmissionError): asyncio.run(pool.ready_effective_agent_settings("alice", "provider", "model", session_id="chat"))
    assert len(calls) == 1 and len(release) == 1


def test_ready_catalog_refresh_uses_exact_bound_target_without_boot(ready_pool):
    pool, calls, release = ready_pool
    result = asyncio.run(pool.ready_refresh_commands("alice", session_id="chat", reload_skills=True))
    assert calls == [("_openclank/session/v1/commands/refresh", {"directory": "/owned/workspace", "sessionID": "ses_native", "reloadSkills": True})]
    assert result == {"commands": [], "generation": 3} and len(release) == 1
    with pytest.raises(SupervisorAdmissionError) as exc: asyncio.run(pool.ready_refresh_commands("bob", session_id="chat"))
    assert exc.value.code == "WORKER_NOT_READY" and len(calls) == 1

def test_catalog_refresh_revocation_drops_ack_and_releases(ready_pool, monkeypatch):
    pool, calls, release = ready_pool
    checks = []
    def check(owner, epoch):
        checks.append(epoch)
        if len(checks) > 1: raise SupervisorAdmissionError("WORKER_NOT_READY", "revoked")
    monkeypatch.setattr(pool, "_assert_owner_lifecycle", check)
    with pytest.raises(SupervisorAdmissionError): asyncio.run(pool.ready_refresh_commands("alice", session_id="chat"))
    assert len(calls) == 1 and len(release) == 1

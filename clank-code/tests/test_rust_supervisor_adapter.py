from __future__ import annotations

import pytest

from src.openclank.rust_supervisor_adapter import RustSupervisorAdapter


def test_rust_adapter_is_health_only_and_fail_closed():
    adapter = RustSupervisorAdapter("/tmp/not-connected-supervisor.sock", "fixture-session")
    assert adapter.backend_name == "rust"
    assert adapter.available_models() == []
    assert adapter.readiness()["ready"] is False


@pytest.mark.asyncio
async def test_rust_adapter_rejects_turn_admission_until_lifecycle_closes():
    adapter = RustSupervisorAdapter("/tmp/not-connected-supervisor.sock", "fixture-session")
    with pytest.raises(Exception, match="health-only"):
        await adapter.admit_provider_control("alice")


@pytest.mark.asyncio
async def test_rust_adapter_keeps_declared_root_lifecycle_baseline(monkeypatch, tmp_path):
    class HealthClient:
        async def health(self):
            return {"transport": True, "protocol": True, "ready": False}

        async def close(self):
            return None

    monkeypatch.setattr("src.openclank.rust_supervisor_adapter.RustSupervisorClient", lambda *_args: HealthClient())
    target = tmp_path / "state.txt"
    target.write_text("before")
    adapter = RustSupervisorAdapter("fixture.sock", "fixture-session", declared_roots=[str(tmp_path)])
    await adapter.start()
    target.write_text("after")
    await adapter.stop()
    receipt = adapter.readiness()["history_capture"]
    assert receipt["coverage"] == "DeclaredRootsBaseline"
    assert receipt["before"]["manifest_digest"] != receipt["after"]["manifest_digest"]

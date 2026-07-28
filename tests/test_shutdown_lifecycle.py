import asyncio
import pytest

from src import agent_runs
from src.shutdown_lifecycle import run_shutdown_phase


@pytest.mark.asyncio
async def test_shutdown_phase_reports_success_and_duration(caplog):
    ran = []

    async def operation():
        ran.append(True)

    with caplog.at_level("INFO", logger="src.shutdown_lifecycle"):
        result = await run_shutdown_phase("fixture", operation, timeout=0.1)

    assert result == "ok"
    assert ran == [True]
    assert "phase=fixture event=start" in caplog.text
    assert "phase=fixture event=end" in caplog.text
    assert "result=ok" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_phase_bounds_hung_operation(caplog):
    async def operation():
        await asyncio.Event().wait()

    with caplog.at_level("INFO", logger="src.shutdown_lifecycle"):
        result = await run_shutdown_phase("hung", operation, timeout=0.01)

    assert result == "timeout"
    assert "result=timeout" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_phase_records_failure_without_aborting_later_phases(caplog):
    async def operation():
        raise RuntimeError("fixture teardown failed")

    with caplog.at_level("INFO", logger="src.shutdown_lifecycle"):
        result = await run_shutdown_phase("broken", operation, timeout=0.1)

    assert result == "error"
    assert "fixture teardown failed" in caplog.text
    assert "result=error" in caplog.text


@pytest.mark.asyncio
async def test_agent_run_shutdown_joins_replaced_drain_cleanup():
    await agent_runs.shutdown()
    first_running = asyncio.Event()
    first_cancelled = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def first_events():
        try:
            yield "data: first\n\n"
            first_running.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_cancelled.set()
            await release_cleanup.wait()
            raise

    async def replacement_events():
        yield "data: replacement\n\n"

    first = agent_runs.start("shutdown-replacement", first_events())
    await first_running.wait()
    replacement = agent_runs.start("shutdown-replacement", replacement_events())
    await first_cancelled.wait()

    stopping = asyncio.create_task(agent_runs.shutdown())
    await asyncio.sleep(0)
    assert not stopping.done()
    release_cleanup.set()
    await stopping

    assert first.task.done()
    assert replacement.task.done()
    assert not agent_runs._DRAIN_TASKS
    assert not agent_runs._RUNS


@pytest.mark.asyncio
async def test_application_joins_agent_consumers_before_mimo_teardown(monkeypatch):
    import app as app_module

    events = []
    run_started = asyncio.Event()

    async def stream():
        try:
            yield "data: partial\n\n"
            run_started.set()
            await asyncio.Event().wait()
        finally:
            events.append("run_joined")

    agent_runs.start("shutdown-order", stream())
    await run_started.wait()

    class Supervisor:
        async def stop(self):
            events.append("mimo_stopped")

    async def phase(name, operation, *, timeout):
        events.append(name)
        if name in {"agent_runs", "mimo_supervisor"}:
            await operation()
        return "ok"

    monkeypatch.setattr(app_module, "_run_shutdown_phase", phase)
    monkeypatch.setattr(app_module.app.state, "mimo_supervisor", Supervisor(), raising=False)
    monkeypatch.setattr("src.model_dispatch.set_mimo_supervisor", lambda value: None)

    await app_module._shutdown_event()

    assert events.index("startup_tasks") < events.index("mimo_supervisor")
    assert events.index("task_scheduler") < events.index("mimo_supervisor")
    assert events.index("agent_runs") < events.index("mimo_supervisor")
    assert events.index("agent_runs") < events.index("copal_bridge")
    assert events.index("run_joined") < events.index("mimo_stopped")

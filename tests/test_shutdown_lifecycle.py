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


@pytest.mark.asyncio
async def test_application_closes_cached_files_clients_off_loop_after_producers(monkeypatch):
    import threading
    import app as app_module
    from src.openclank import files_service_client

    events = []
    main_thread = threading.get_ident()

    class Client:
        def __init__(self, name):
            self.name = name

        def close(self):
            assert threading.get_ident() != main_thread
            events.append(self.name)

    host_clients = {("owner", "host", "grpc"): Client("host_closed")}
    agent_clients = {("owner", "agent"): Client("agent_closed")}
    monkeypatch.setattr(files_service_client, "_CLIENTS", host_clients)
    monkeypatch.setattr(files_service_client, "_AGENT_CLIENTS", agent_clients)

    async def phase(name, operation, *, timeout):
        events.append(name)
        if name == "files_services":
            assert timeout > 0
            await operation()
        return "ok"

    monkeypatch.setattr(app_module, "_run_shutdown_phase", phase)
    await app_module._shutdown_event()

    for producer in ("agent_runs", "task_scheduler", "copal_bridge", "mimo_supervisor", "mcp_servers"):
        assert events.index(producer) < events.index("files_services")
    assert events.index("host_closed") < events.index("defensive_child_reaper")
    assert events.index("agent_closed") < events.index("defensive_child_reaper")
    assert not host_clients and not agent_clients
    # An already-drained cache is safe during a repeated shutdown invocation.
    await app_module._shutdown_event()
    assert events.count("host_closed") == events.count("agent_closed") == 1


@pytest.mark.asyncio
async def test_application_files_shutdown_closes_actual_native_transport(tmp_path, monkeypatch):
    import os
    import sys
    from pathlib import Path

    binary = os.environ.get("OPENCLANK_TEST_FILES_BIN")
    if not binary or sys.platform != "darwin":
        pytest.skip("Explicit built macOS Files binary required; never build implicitly")
    assert Path(binary).is_file()
    import app as app_module
    from src.openclank import files_service_client, file_policy

    authority = tmp_path / "app.db"
    monkeypatch.setenv("OPEN_CLANK_AUTHORITY_DB_PATH", str(authority))
    monkeypatch.setattr(file_policy, "APP_DB", str(authority))
    monkeypatch.setenv("ODYSSEUS_FILES_SERVICE_BIN", binary)
    monkeypatch.setattr(files_service_client, "_CLIENTS", {})
    monkeypatch.setattr(files_service_client, "_AGENT_CLIENTS", {})
    monkeypatch.setattr(files_service_client, "_HISTORY_BINDING_PROVIDER", None)
    client = files_service_client.client_for_owner("s07-shutdown", transport="grpc")
    try:
        assert (await client.request("health", "."))["ready"] is True
        process = client.process
        socket_path = client._grpc_socket_path
        assert process is not None and process.poll() is None
        assert isinstance(socket_path, Path) and socket_path.is_socket()
        phases = []

        async def phase(name, operation, *, timeout):
            if name == "files_services":
                phases.append(await run_shutdown_phase(name, operation, timeout=timeout))
            return "ok"

        monkeypatch.setattr(app_module, "_run_shutdown_phase", phase)
        await app_module._shutdown_event()
        assert phases == ["ok"]
        assert process.poll() == 0, "Native SIGTERM handler must exit gracefully"
        assert client.process is None and not socket_path.exists()
        assert not files_service_client._CLIENTS and not files_service_client._AGENT_CLIENTS
    finally:
        client.close()

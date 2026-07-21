import asyncio
import pytest

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

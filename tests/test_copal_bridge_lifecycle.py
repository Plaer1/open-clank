import asyncio
import json
import os
import textwrap

import pytest

from src.openclank.copal_bridge import (
    CopalBridge,
    CopalBridgeError,
    _REQUIRED_CAPABILITIES,
    _artifact_metadata_path,
    _artifact_sha256,
)


def _bridge_program(tmp_path):
    program = tmp_path / "bridge-fixture.py"
    program.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys
            import time

            for line in sys.stdin:
                request = json.loads(line)
                if request.get("op") == "slow":
                    time.sleep(5)
                response = {
                    "id": request["id"],
                    "ok": True,
                    "result": {"operation": request.get("op")},
                }
                if request.get("op") in {"status", "scoped_status"}:
                    response["result"].update({
                        "protocol_version": 1,
                        "source_identity": "sha256:test-fixture",
                        "schema_version": 3,
                        "capabilities": sorted(%r),
                    })
                print(json.dumps(response), flush=True)
            """
        ) % _REQUIRED_CAPABILITIES,
        encoding="utf-8",
    )
    os.chmod(program, 0o700)
    _artifact_metadata_path(program).write_text(
        json.dumps({
            "schema_version": 1,
            "build_identity": "sha256:test-fixture",
            "artifact_sha256": _artifact_sha256(program),
            "protocol_version": 1,
            "storage_schema_version": 3,
            "capabilities": sorted(_REQUIRED_CAPABILITIES),
            "target": "test",
        }),
        encoding="utf-8",
    )
    return program


@pytest.mark.asyncio
async def test_timeout_retires_protocol_process_before_clean_restart(tmp_path):
    bridge = CopalBridge(command=_bridge_program(tmp_path), data_dir=tmp_path / "data")
    await bridge.start()
    first_pid = bridge.pid
    status = await bridge.call("status")
    assert status["build_identity"] == "sha256:test-fixture"
    assert status["artifact_sha256"].startswith("sha256:")
    assert status["artifact_source"] == "verified-sidecar"
    scoped_status = await bridge.call("scoped_status")
    assert scoped_status["build_identity"] == "sha256:test-fixture"
    assert scoped_status["artifact_source"] == "verified-sidecar"
    slow = asyncio.create_task(bridge.call("slow", timeout=0.01))
    await asyncio.sleep(0)
    queued = asyncio.create_task(bridge.call("queued", timeout=1))

    with pytest.raises(asyncio.TimeoutError):
        await slow

    assert (await queued)["operation"] == "queued"
    assert bridge.pid != first_pid
    await bridge.stop()


@pytest.mark.asyncio
async def test_status_rejects_artifact_tampering_after_spawn(tmp_path):
    bridge = CopalBridge(command=_bridge_program(tmp_path), data_dir=tmp_path / "data")
    await bridge.start()
    try:
        program = bridge.command
        program.write_text(program.read_text(encoding="utf-8") + "# changed after spawn\n", encoding="utf-8")
        with pytest.raises(CopalBridgeError, match="Running Copal artifact changed"):
            await bridge.call("status")
        assert not bridge.is_alive()
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_cancellation_retires_protocol_process_before_clean_restart(tmp_path):
    bridge = CopalBridge(command=_bridge_program(tmp_path), data_dir=tmp_path / "data")
    await bridge.start()
    first_pid = bridge.pid
    pending = asyncio.create_task(bridge.call("slow", timeout=10))
    await asyncio.sleep(0.02)

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert not bridge.is_alive()
    assert (await bridge.call("status", timeout=1))["operation"] == "status"
    assert bridge.pid != first_pid
    await bridge.stop()

import asyncio
import os
import textwrap

import pytest

from src.openclank.copal_bridge import CopalBridge


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
                print(json.dumps(response), flush=True)
            """
        ),
        encoding="utf-8",
    )
    os.chmod(program, 0o700)
    return program


@pytest.mark.asyncio
async def test_timeout_retires_protocol_process_before_clean_restart(tmp_path):
    bridge = CopalBridge(command=_bridge_program(tmp_path), data_dir=tmp_path / "data")
    await bridge.start()
    first_pid = bridge.pid
    slow = asyncio.create_task(bridge.call("slow", timeout=0.01))
    await asyncio.sleep(0)
    queued = asyncio.create_task(bridge.call("queued", timeout=1))

    with pytest.raises(asyncio.TimeoutError):
        await slow

    assert (await queued)["operation"] == "queued"
    assert bridge.pid != first_pid
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

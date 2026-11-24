"""Real GIO dispatch with disposable desktop associations and recording app."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time

import pytest

from src.openclank.host_apps import HostAppsError, host_apps
from src.openclank.linux_host_apps import LinuxHostApps

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="GIO Linux integration")


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    data, config = tmp_path / "xdg-data", tmp_path / "xdg-config"
    applications = data / "applications"
    applications.mkdir(parents=True); config.mkdir()
    record = tmp_path / "received.json"
    recorder = tmp_path / "recorder.py"
    recorder.write_text("import json,sys\nfrom pathlib import Path\nPath(" + repr(str(record)) + ").write_text(json.dumps(sys.argv[1:]))\n")
    entry = applications / "openclank-record.desktop"
    entry.write_text('[Desktop Entry]\nType=Application\nName=Open Clank Recorder\nExec="' + sys.executable + '" "' + str(recorder) + '" %f\nMimeType=text/plain;\nNoDisplay=false\nTerminal=false\n')
    (applications / "mimeinfo.cache").write_text('[MIME Cache]\ntext/plain=openclank-record.desktop;\n')
    (config / "mimeapps.list").write_text('[Default Applications]\ntext/plain=openclank-record.desktop;\n[Added Associations]\ntext/plain=openclank-record.desktop;\n')
    monkeypatch.setenv("XDG_DATA_HOME", str(data)); monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_DATA_DIRS", str(data)); monkeypatch.setenv("XDG_CONFIG_DIRS", str(config))
    target = tmp_path / "spaces ü ; $(touch forbidden).txt"
    target.write_text("plain text\n")
    return target, entry, record


def test_real_gio_discovery_launch_exact_arguments_and_removed_id(desktop, monkeypatch):
    target, entry, record = desktop
    monkeypatch.setenv("WAYLAND_DISPLAY", os.environ.get("WAYLAND_DISPLAY") or "fixture-session")
    adapter = host_apps()
    assert isinstance(adapter, LinuxHostApps)
    applications = asyncio.run(adapter.discover_async(str(target)))
    assert applications == [{"id": entry.name, "name": "Open Clank Recorder", "default": True}]
    result = asyncio.run(adapter.launch_async(str(target), entry.name))
    assert result == {"id": entry.name, "name": "Open Clank Recorder"}
    deadline = time.monotonic() + 5
    while not record.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert json.loads(record.read_text()) == [str(target)]
    entry.unlink()
    with pytest.raises(HostAppsError) as removed:
        asyncio.run(adapter.launch_async(str(target), entry.name))
    assert removed.value.code == "application_unavailable"


def test_real_gio_forged_id_and_no_session(desktop, monkeypatch):
    target, entry, _ = desktop
    adapter = LinuxHostApps()
    with pytest.raises(HostAppsError) as forged:
        asyncio.run(adapter.launch_async(str(target), "/bin/sh"))
    assert forged.value.code == "application_unavailable"
    monkeypatch.delenv("DISPLAY", raising=False); monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(HostAppsError) as unavailable:
        asyncio.run(adapter.launch_async(str(target), entry.name))
    assert unavailable.value.code == "gui_session_unavailable"
    with pytest.raises(HostAppsError) as required:
        asyncio.run(adapter.launch_async(str(target), ""))
    assert required.value.code == "application_required"


def test_spawn_failure_is_typed(desktop, monkeypatch):
    async def fail(*args, **kwargs):
        raise OSError("missing helper")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    with pytest.raises(HostAppsError) as error:
        asyncio.run(LinuxHostApps().discover_async(str(desktop[0])))
    assert error.value.code == "provider_unavailable"


def test_cancel_awaits_owned_worker_teardown(desktop, monkeypatch):
    real_spawn = asyncio.create_subprocess_exec
    children = []
    async def slow(*args, **kwargs):
        child = await real_spawn(sys.executable, "-c", "import time; time.sleep(30)",
                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        children.append(child)
        return child
    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow)
    async def exercise():
        task = asyncio.create_task(LinuxHostApps().discover_async(str(desktop[0])))
        while not children:
            await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None
    asyncio.run(exercise())

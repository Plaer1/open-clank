import json
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from src.openclank import macos_host_apps


def test_non_macos_is_truthfully_unsupported(monkeypatch):
    monkeypatch.setattr(macos_host_apps.sys, "platform", "linux")
    with pytest.raises(macos_host_apps.MacOSHostAppsError) as error:
        macos_host_apps.MacOSHostApps().discover("/tmp/report.txt")
    assert error.value.code == "unsupported_platform"


def test_discovery_and_launch_use_resolved_id_and_argument_vector(monkeypatch):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv == ["/cached/helper"]:
            request = json.loads(kwargs["input"])
            if request["op"] == "discover":
                return SimpleNamespace(returncode=0, stdout='[{"id":"com.example.Editor","name":"Editor"}]', stderr="")
            assert request == {"op": "resolve", "path": "/private/report.txt", "app_id": "com.example.Editor"}
            return SimpleNamespace(returncode=0, stdout='{"id":"com.example.Editor","name":"Editor","bundle_id":"com.example.Editor","path":"/Applications/Editor.app"}', stderr="")
        assert argv == ["/usr/bin/open", "-a", "/Applications/Editor.app", "--", "/private/report.txt"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(macos_host_apps.sys, "platform", "darwin")
    apps = macos_host_apps.MacOSHostApps(runner=runner)
    monkeypatch.setattr(apps, "_executable", lambda: "/cached/helper")
    assert apps.discover("/private/report.txt")[0]["id"] == "com.example.Editor"
    assert apps.launch("/private/report.txt", "com.example.Editor")["name"] == "Editor"
    assert calls[-1][0] == ["/usr/bin/open", "-a", "/Applications/Editor.app", "--", "/private/report.txt"]


def test_selected_id_must_be_present():
    with pytest.raises(macos_host_apps.MacOSHostAppsError) as error:
        macos_host_apps.MacOSHostApps().launch("/tmp/report.txt", "")
    assert error.value.code == "application_required"


def test_cache_rejects_symlinked_leaf(monkeypatch, tmp_path):
    monkeypatch.setattr(macos_host_apps, "DATA_DIR", str(tmp_path))
    target = tmp_path / "elsewhere"
    target.mkdir()
    cache = tmp_path / "cache" / "native" / "macos-host-apps"
    cache.parent.mkdir(parents=True)
    cache.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(macos_host_apps.sys, "platform", "darwin")
    with pytest.raises(macos_host_apps.MacOSHostAppsError):
        macos_host_apps.MacOSHostApps()._cache_dir()


def test_cache_rejects_foreign_helper_without_chmod_or_unlink(monkeypatch, tmp_path):
    monkeypatch.setattr(macos_host_apps, "DATA_DIR", str(tmp_path))
    cache = tmp_path / "cache" / "native" / "macos-host-apps"
    cache.mkdir(parents=True, mode=0o700)
    helper = cache / "helper-test"
    helper.write_bytes(b"foreign")
    monkeypatch.setattr(macos_host_apps, "_SOURCE", tmp_path / "source.swift")
    (tmp_path / "source.swift").write_text("source")
    monkeypatch.setattr(macos_host_apps.os, "getuid", lambda: helper.stat().st_uid + 1)
    with pytest.raises(macos_host_apps.MacOSHostAppsError):
        macos_host_apps.MacOSHostApps()._helper_path(helper)
    assert helper.read_bytes() == b"foreign"


@pytest.mark.asyncio
async def test_cancelled_launch_waits_for_worker_and_prevents_open():
    started = threading.Event()
    finished = threading.Event()
    opened = []
    apps = macos_host_apps.MacOSHostApps()

    def slow_launch(path, app_id, *, cancel_event=None):
        started.set()
        time.sleep(0.05)
        if cancel_event and cancel_event.is_set():
            finished.set()
            raise macos_host_apps.MacOSHostAppsError("cancelled", code="operation_cancelled")
        opened.append(app_id)
        finished.set()
        return {"id": app_id, "name": "Editor"}

    apps.launch = slow_launch
    task = asyncio.create_task(apps.launch_async("/tmp/file", "com.example.Editor"))
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert opened == []


@pytest.mark.asyncio
async def test_repeated_cancel_still_waits_for_worker():
    started = threading.Event()
    finished = threading.Event()
    apps = macos_host_apps.MacOSHostApps()

    def slow_launch(path, app_id, *, cancel_event=None):
        started.set()
        time.sleep(0.05)
        finished.set()
        raise macos_host_apps.MacOSHostAppsError("cancelled", code="operation_cancelled")

    apps.launch = slow_launch
    task = asyncio.create_task(apps.launch_async("/tmp/file", "com.example.Editor"))
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()

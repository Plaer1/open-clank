"""Frozen macOS command and AppKit-owned local server lifetime."""
from __future__ import annotations

import multiprocessing
import os
import signal
import sys
import threading
import webbrowser
from pathlib import Path


def local_app_port() -> int:
    """Use the established port setting without admitting a remote listener."""
    raw = os.environ.get("APP_PORT", "7777")
    if not raw or len(raw) > 5 or not raw.isascii() or not raw.isdecimal():
        raise ValueError("APP_PORT must be an ASCII decimal integer from 1 to 65535")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError("APP_PORT must be an ASCII decimal integer from 1 to 65535")
    return port


def app_owner() -> int:
    from scripts.openclank_bootstrap import verify_portable_payload
    from src.openclank.client_profiles import ClientProfile
    from src.openclank.server_manager import LocalServerManager
    from src.runtime_paths import get_app_root

    verify_portable_payload()
    profile = ClientProfile.create("macos-app", f"http://127.0.0.1:{local_app_port()}", auto_start=True)
    manager = LocalServerManager(repo_root=Path(get_app_root()))
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    import time
    # Use the same canonical start lock for the ownership decision and start;
    # another CLI cannot win a startup race and have its server stopped by Quit.
    with manager._start_lock(time.monotonic() + 120):
        before = manager.status(profile)
        host, port = manager._address(profile)
        status = manager._start_locked(profile, host=host, port=port,
                                       wait_seconds=120, allow_auth_setup=True)
        owned = status.pid != before.pid or not before.running
        generation = manager._read_state().get("instance_id")
    if owned and status.pid:
        # The owner stays alive, unlike the short CLI startup command. Reap
        # its own server child so canonical stop cannot mistake a zombie for
        # a running process. This is a one-shot wait, never a restart worker.
        def reap():
            try:
                os.waitpid(status.pid, 0)
            except ChildProcessError:
                pass
        threading.Thread(target=reap, daemon=True, name="macos-server-reap").start()
    try:
        if not stopping.is_set():
            if not webbrowser.open(status.url):
                raise RuntimeError("Open the browser at " + status.url)
            print("Open Clank browser opened", flush=True)
        while not stopping.wait(0.5):
            if not status.pid or not manager._alive(status.pid):
                raise RuntimeError("Open Clank server stopped; see its per-user server log")
    finally:
        state = manager._read_state()
        if owned and state.get("pid") == status.pid and state.get("instance_id") == generation:
            manager.stop(profile)
    return 0


def main() -> int:
    if sys.argv[1:] == ["__mac-app-owner"]:
        if not getattr(sys, "frozen", False) or sys.platform != "darwin":
            return 2
        try:
            return app_owner()
        except Exception as exc:
            print("Open Clank app: " + str(exc), file=sys.stderr)
            return 1
    from src.openclank.cli import main as cli_main
    return int(cli_main())


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())

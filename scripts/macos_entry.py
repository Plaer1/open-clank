"""Frozen macOS command and AppKit-owned local server lifetime."""
from __future__ import annotations

import multiprocessing
import json
import os
import re
import signal
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path


STARTUP_DIAGNOSTIC_PREFIX = "OPENCLANK_MAC_STARTUP "
STARTUP_PHASES = frozenset({"imports", "payload", "profile", "state-directory",
                          "start-lock", "server-start", "browser", "running",
                          "bootstrap", "uvicorn-import", "application-import", "serving", "configuration"})
_OWNER_PHASE = "imports"
_CODESIGN_REASONS = {
    "a sealed resource is missing or invalid": "sealed-resource-changed",
    "code object is not signed at all": "unsigned-code",
    "invalid signature": "invalid-signature",
    "bundle format unrecognized": "invalid-bundle-format",
    "resource envelope is obsolete": "obsolete-resource-envelope",
}


def safe_startup_record(record: dict) -> dict:
    """Admit diagnostic identifiers only, never exception messages or paths."""
    phase = record.get("phase")
    if record.get("schema_version") != 1 or not isinstance(phase, str) or phase not in STARTUP_PHASES:
        return {}
    kind = record.get("exception_type")
    if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", kind):
        return {}
    safe = {"schema_version": 1, "phase": record["phase"], "exception_type": kind}
    for field in ("errno", "child_exit_code", "codesign_returncode"):
        value = record.get(field)
        if type(value) is int and -4096 <= value <= 4096:
            safe[field] = value
    missing = record.get("missing_module")
    if isinstance(missing, str) and len(missing) <= 160 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", missing):
        safe["missing_module"] = missing
    reason = record.get("codesign_reason")
    if isinstance(reason, str) and reason in {*_CODESIGN_REASONS.values(), "unclassified-signature-failure"}:
        safe["codesign_reason"] = reason
    changes = record.get("codesign_resource_changes")
    if isinstance(changes, dict):
        safe_counts = {key: value for key, value in changes.items()
                       if key in {"added", "modified", "missing"} and type(value) is int and 0 <= value <= 1024}
        if safe_counts:
            safe["codesign_resource_changes"] = safe_counts
    if type(record.get("sitecustomize_cache_added")) is bool:
        safe["sitecustomize_cache_added"] = record["sitecustomize_cache_added"]
    return safe


def startup_failure_record(exc: Exception, phase: str) -> dict:
    record = {"schema_version": 1, "phase": phase, "exception_type": type(exc).__name__}
    if isinstance(exc, OSError):
        record["errno"] = exc.errno
    if isinstance(exc, ModuleNotFoundError):
        record["missing_module"] = exc.name
    exited = re.match(r"Open Clank exited during startup \(code (-?\d+)\)", str(exc))
    if exited:
        record["child_exit_code"] = int(exited.group(1))
    if isinstance(exc, subprocess.CalledProcessError) and isinstance(exc.cmd, (list, tuple)) and exc.cmd and str(exc.cmd[0]) == "/usr/bin/codesign":
        record["codesign_returncode"] = exc.returncode
        stderr = exc.stderr or b""
        detail = stderr[:8192].decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr)[:8192]
        record["codesign_reason"] = next((value for text, value in _CODESIGN_REASONS.items() if text in detail), "unclassified-signature-failure")
        lines = detail.splitlines()
        record["codesign_resource_changes"] = {kind: sum(line.startswith("file " + kind + ":") for line in lines) for kind in ("added", "modified", "missing")}
        record["sitecustomize_cache_added"] = any(line.startswith("file added:") and "/__pycache__/sitecustomize." in line for line in lines)
    return safe_startup_record(record)


def local_app_port() -> int:
    from scripts.macos_launch_profile import local_app_port as resolved_port
    return resolved_port()


def app_owner() -> int:
    global _OWNER_PHASE
    _OWNER_PHASE = "imports"
    from scripts.openclank_bootstrap import verify_portable_payload
    from src.openclank.client_profiles import ClientProfile
    from src.openclank.server_manager import LocalServerManager
    from src.runtime_paths import get_app_root

    _OWNER_PHASE = "payload"
    verify_portable_payload()
    _OWNER_PHASE = "profile"
    profile = ClientProfile.create("macos-app", f"http://127.0.0.1:{local_app_port()}", auto_start=True)
    manager = LocalServerManager(repo_root=Path(get_app_root()))
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    import time
    # Use the same canonical start lock for the ownership decision and start;
    # another CLI cannot win a startup race and have its server stopped by Quit.
    _OWNER_PHASE = "state-directory"
    manager.state_dir.mkdir(parents=True, exist_ok=True)
    _OWNER_PHASE = "start-lock"
    with manager._start_lock(time.monotonic() + 120):
        before = manager.status(profile)
        host, port = manager._address(profile)
        _OWNER_PHASE = "server-start"
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
            _OWNER_PHASE = "browser"
            if not webbrowser.open(status.url):
                raise RuntimeError("Open the browser at " + status.url)
            print("Open Clank browser opened", flush=True)
        _OWNER_PHASE = "running"
        while not stopping.wait(0.5):
            if not status.pid or not manager._alive(status.pid):
                raise RuntimeError("Open Clank server stopped; see its per-user server log")
    finally:
        state = manager._read_state()
        if owned and state.get("pid") == status.pid and state.get("instance_id") == generation:
            manager.stop(profile)
    return 0


def main() -> int:
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        # Resolve before importing anything that binds data roots/settings.
        from scripts.macos_launch_profile import apply_launch_profile
        try:
            port = apply_launch_profile()
        except Exception as exc:
            print(STARTUP_DIAGNOSTIC_PREFIX + json.dumps(startup_failure_record(exc, "configuration"), sort_keys=True), file=sys.stderr, flush=True)
            return 1
        if sys.argv[1:] == ["__mac-launch-configuration"]:
            print(port, flush=True)
            return 0
    if sys.argv[1:] == ["__mac-app-owner"]:
        if not getattr(sys, "frozen", False) or sys.platform != "darwin":
            return 2
        try:
            return app_owner()
        except Exception as exc:
            print(STARTUP_DIAGNOSTIC_PREFIX + json.dumps(startup_failure_record(exc, _OWNER_PHASE), sort_keys=True), file=sys.stderr, flush=True)
            return 1
    from src.openclank.cli import main as cli_main
    return int(cli_main())


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())

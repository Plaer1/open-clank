"""Small AppKit bridge for opening an authorized host file in an installed app."""

from __future__ import annotations

import hashlib
import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from src.constants import DATA_DIR


class MacOSHostAppsError(RuntimeError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


_SOURCE = Path(__file__).resolve().parents[2] / "native" / "macos_host_apps.swift"
_MAX_CONCURRENT = 2
_COMPILE_TIMEOUT = 20
_CALL_TIMEOUT = 8
_OPEN_TIMEOUT = 15
_WORK_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT)


class MacOSHostApps:
    """Discover and launch apps without accepting an app path from a client."""

    def __init__(self, *, runner: Callable[..., Any] | None = None, compiler: Callable[..., Any] | None = None):
        self._runner = runner or subprocess.run
        self._compiler = compiler or subprocess.run

    def _cache_dir(self) -> Path:
        cache = Path(DATA_DIR).resolve() / "cache" / "native" / "macos-host-apps"
        current = Path(cache.anchor)
        private_boundary = Path(DATA_DIR).resolve() / "cache"
        for part in cache.parts[1:]:
            current /= part
            try:
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise MacOSHostAppsError("Host app cache is unavailable", code="provider_unavailable")
                if current == private_boundary or current == private_boundary / "native" or current == cache:
                    if info.st_uid != os.getuid():
                        raise MacOSHostAppsError("Host app cache is unavailable", code="provider_unavailable")
                if current == cache and info.st_mode & 0o777 != 0o700:
                    raise MacOSHostAppsError("Host app cache is unavailable", code="provider_unavailable")
            except FileNotFoundError:
                current.mkdir(mode=0o700)
        return cache

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        """Remove only our own regular temporary file; never follow a swap."""
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return
        path.unlink()

    @staticmethod
    def _helper_path(path: Path) -> str | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MacOSHostAppsError("Host app cache is unavailable", code="provider_unavailable")
        if info.st_mode & 0o777 != 0o700:
            raise MacOSHostAppsError("Host app cache is unavailable", code="provider_unavailable")
        return str(path)

    def _executable(self) -> str:
        if sys.platform != "darwin":
            raise MacOSHostAppsError("Open on host is supported on macOS only", code="unsupported_platform")
        try:
            source = _SOURCE.read_bytes()
            digest = hashlib.sha256(source).hexdigest()[:20]
            cache = self._cache_dir() / f"helper-{digest}"
            existing = self._helper_path(cache)
            if existing:
                return existing
            fd, temporary = tempfile.mkstemp(prefix=f".{cache.name}-", dir=cache.parent)
            os.close(fd)
            temporary_path = Path(temporary)
            try:
                result = self._compiler(
                    ["/usr/bin/swiftc", str(_SOURCE), "-O", "-o", str(temporary_path)],
                    capture_output=True, text=True, timeout=_COMPILE_TIMEOUT,
                )
                if result.returncode != 0:
                    raise MacOSHostAppsError("Host app discovery is unavailable", code="provider_unavailable")
                temporary_info = temporary_path.lstat()
                if temporary_info.st_uid != os.getuid() or stat.S_ISLNK(temporary_info.st_mode) or not stat.S_ISREG(temporary_info.st_mode):
                    raise MacOSHostAppsError("Host app cache is unavailable", code="provider_unavailable")
                os.chmod(temporary_path, 0o700)
                try:
                    os.link(temporary_path, cache)
                except FileExistsError:
                    existing = self._helper_path(cache)
                    if not existing:
                        raise MacOSHostAppsError("Host app cache is unavailable", code="provider_unavailable")
                return str(cache)
            finally:
                self._safe_unlink(temporary_path)
        except subprocess.TimeoutExpired as error:
            raise MacOSHostAppsError("Host app discovery timed out", code="provider_unavailable") from error
        except MacOSHostAppsError:
            raise
        except OSError as error:
            raise MacOSHostAppsError("Host app discovery is unavailable", code="provider_unavailable") from error

    def _call(self, payload: dict[str, Any]) -> Any:
        try:
            result = self._runner([self._executable()], input=json.dumps(payload), capture_output=True, text=True, timeout=_CALL_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise MacOSHostAppsError("Host app operation timed out", code="provider_unavailable") from error
        except OSError as error:
            raise MacOSHostAppsError("Host app operation is unavailable", code="provider_unavailable") from error
        if result.returncode != 0:
            marker = str(result.stderr or "")
            code = "gui_session_unavailable" if "GUI_SESSION" in marker else "provider_unavailable"
            message = "The host GUI is unavailable" if code == "gui_session_unavailable" else "Host app operation failed"
            raise MacOSHostAppsError(message, code=code)
        try:
            value = json.loads(result.stdout)
        except (TypeError, ValueError) as error:
            raise MacOSHostAppsError("Host app operation returned invalid data", code="provider_unavailable") from error
        if isinstance(value, dict) and value.get("error"):
            code = str(value.get("code") or "application_unavailable")
            if code not in {"application_unavailable", "gui_session_unavailable", "provider_unavailable"}:
                code = "application_unavailable"
            messages = {
                "application_unavailable": "The selected application is unavailable",
                "gui_session_unavailable": "The host GUI is unavailable",
                "provider_unavailable": "Host app operation failed",
            }
            raise MacOSHostAppsError(messages[code], code=code)
        return value

    @staticmethod
    async def _wait_worker(task: asyncio.Task[Any]) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                return

    def discover(self, path: str) -> list[dict[str, str]]:
        value = self._call({"op": "discover", "path": path})
        if isinstance(value, dict):
            value = value.get("applications")
        if not isinstance(value, list):
            return []
        return [
            {"id": str(item.get("id") or ""), "name": str(item.get("name") or item.get("id") or ""), "default": bool(item.get("is_default"))}
            for item in value if isinstance(item, dict) and item.get("id")
        ]

    def launch(self, path: str, app_id: str, *, cancel_event: threading.Event | None = None) -> dict[str, str]:
        selected = str(app_id or "").strip()
        if not selected:
            raise MacOSHostAppsError("Choose an installed application", code="application_required")
        if cancel_event and cancel_event.is_set():
            raise MacOSHostAppsError("Host app operation was cancelled", code="operation_cancelled")
        resolved = self._call({"op": "resolve", "path": path, "app_id": selected})
        bundle_id = str(resolved.get("bundle_id") or "").strip() if isinstance(resolved, dict) else ""
        verified_path = str(resolved.get("path") or "").strip() if isinstance(resolved, dict) else ""
        if not bundle_id or not verified_path or not Path(verified_path).is_absolute():
            raise MacOSHostAppsError("The selected application is unavailable", code="application_unavailable")
        if cancel_event and cancel_event.is_set():
            raise MacOSHostAppsError("Host app operation was cancelled", code="operation_cancelled")
        try:
            result = self._runner(["/usr/bin/open", "-a", verified_path, "--", path], capture_output=True, text=True, timeout=_OPEN_TIMEOUT)
        except OSError as error:
            raise MacOSHostAppsError("The host GUI is unavailable", code="gui_session_unavailable") from error
        except subprocess.TimeoutExpired as error:
            raise MacOSHostAppsError("The host GUI did not respond", code="gui_session_unavailable") from error
        if result.returncode != 0:
            raise MacOSHostAppsError("The selected application could not be opened", code="gui_session_unavailable")
        return {"id": bundle_id, "name": str(resolved.get("name") or bundle_id)}

    async def discover_async(self, path: str) -> list[dict[str, str]]:
        await _WORK_SEMAPHORE.acquire()
        task = asyncio.create_task(asyncio.to_thread(self.discover, path))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await MacOSHostApps._wait_worker(task)
            raise
        finally:
            _WORK_SEMAPHORE.release()

    async def launch_async(self, path: str, app_id: str) -> dict[str, str]:
        await _WORK_SEMAPHORE.acquire()
        cancel_event = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(self.launch, path, app_id, cancel_event=cancel_event))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel_event.set()
            await MacOSHostApps._wait_worker(task)
            raise
        finally:
            _WORK_SEMAPHORE.release()

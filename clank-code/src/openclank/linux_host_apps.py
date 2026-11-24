"""Bounded GIO desktop application dispatch for already-authorized files."""
from __future__ import annotations

import asyncio
import ctypes as C
import ctypes.util
import json
import os
from pathlib import Path
import stat
import sys

from src.openclank.host_apps import HostAppsError

_WORK = asyncio.Semaphore(2)
_TIMEOUT = 15


class LinuxHostApps:
    async def _call(self, path: str, app_id: str | None = None):
        if sys.platform != "linux":
            raise HostAppsError("Linux host apps are unavailable", code="unsupported_platform")
        if not os.path.isabs(path) or "\0" in path:
            raise HostAppsError("Host resource is unavailable", code="resource_unavailable")
        if getattr(sys, "frozen", False):
            roots = [Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)), Path(sys.executable).parent]
            helper = next((root / "bin/openclank-linux-host-apps" for root in roots
                           if (root / "bin/openclank-linux-host-apps").is_file()), None)
            if helper is None:
                raise HostAppsError("Host app helper is unavailable", code="provider_unavailable")
            command = [str(helper)]
        else:
            command = [sys.executable, "-B", "-m", "src.openclank.linux_host_apps", "--worker"]
        async with _WORK:
            environment = os.environ.copy()
            # Frozen application search paths must not inject bundled libraries
            # into the system GIO/GLib stack used by the disposable helper.
            original = environment.get("LD_LIBRARY_PATH_ORIG")
            environment.pop("LD_LIBRARY_PATH", None)
            if original:
                environment["LD_LIBRARY_PATH"] = original
            try:
                process = await asyncio.create_subprocess_exec(
                    *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, cwd=Path(__file__).resolve().parents[2], env=environment)
            except OSError as error:
                raise HostAppsError("Host app helper is unavailable", code="provider_unavailable") from error
            try:
                output, _ = await asyncio.wait_for(process.communicate(
                    json.dumps({"path": path, "app_id": app_id}).encode()), _TIMEOUT)
                if process.returncode or len(output) > 65536:
                    raise ValueError("invalid helper response")
                value = json.loads(output)
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, asyncio.TimeoutError) as error:
                raise HostAppsError("Host app operation failed or timed out", code="provider_unavailable") from error
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            if value.get("error"):
                code = value.get("code")
                if code not in {"application_unavailable", "provider_unavailable", "gui_session_unavailable", "resource_unavailable"}:
                    code = "provider_unavailable"
                raise HostAppsError("Host application is unavailable", code=code)
            return value

    async def discover_async(self, path: str):
        return (await self._call(path))["applications"]

    async def launch_async(self, path: str, app_id: str):
        if not str(app_id or "").strip():
            raise HostAppsError("Choose an installed application", code="application_required")
        return (await self._call(path, app_id))["application"]


class _GList(C.Structure):
    pass

_GList._fields_ = [("data", C.c_void_p), ("next", C.POINTER(_GList)), ("prev", C.POINTER(_GList))]


def _native(path: str, selected: str | None):
    if not os.path.isabs(path) or "\0" in path or os.path.realpath(path) != path:
        raise HostAppsError("Invalid host resource", code="resource_unavailable")
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise HostAppsError("Invalid host resource", code="resource_unavailable")
    gio = C.CDLL(ctypes.util.find_library("gio-2.0") or "libgio-2.0.so.0")
    glib = C.CDLL(ctypes.util.find_library("glib-2.0") or "libglib-2.0.so.0")
    obj = C.CDLL(ctypes.util.find_library("gobject-2.0") or "libgobject-2.0.so.0")
    def bind(lib, name, result, args):
        function = getattr(lib, name); function.restype = result; function.argtypes = args
        return function
    ptr = C.c_void_p
    unref = bind(obj, "g_object_unref", None, [ptr])
    free_list = bind(glib, "g_list_free", None, [C.POINTER(_GList)])
    append = bind(glib, "g_list_append", C.POINTER(_GList), [C.POINTER(_GList), ptr])
    new_file = bind(gio, "g_file_new_for_path", ptr, [C.c_char_p])
    query = bind(gio, "g_file_query_info", ptr, [ptr, C.c_char_p, C.c_int, ptr, ptr])
    content_type = bind(gio, "g_file_info_get_content_type", C.c_char_p, [ptr])
    get_all = bind(gio, "g_app_info_get_all_for_type", C.POINTER(_GList), [C.c_char_p])
    get_default = bind(gio, "g_app_info_get_default_for_type", ptr, [C.c_char_p, C.c_int])
    get_id = bind(gio, "g_app_info_get_id", C.c_char_p, [ptr])
    get_name = bind(gio, "g_app_info_get_display_name", C.c_char_p, [ptr])
    launch = bind(gio, "g_app_info_launch", C.c_int, [ptr, C.POINTER(_GList), ptr, ptr])
    file = new_file(os.fsencode(path)); info = default = None; apps = files = None; owned = []
    try:
        info = query(file, b"standard::content-type", 1, None, None)  # NOFOLLOW_SYMLINKS
        mime = content_type(info) if info else None
        if not mime:
            raise HostAppsError("File type unavailable", code="resource_unavailable")
        default = get_default(mime, 0)
        default_id = get_id(default) if default else None
        apps = get_all(mime)
        cursor = apps; results = []; match = None; seen = set()
        while cursor:
            app = cursor.contents.data; owned.append(app)
            identity, name = get_id(app), get_name(app)
            if identity and name and len(identity) <= 512 and len(name) <= 1024 and identity not in seen and len(results) < 24:
                seen.add(identity)
                value = {"id": identity.decode("utf-8"), "name": name.decode("utf-8"), "default": identity == default_id}
                results.append(value)
                if value["id"] == selected:
                    match = (app, value)
            cursor = cursor.contents.next
        if selected is None:
            return {"applications": results}
        if match is None:
            raise HostAppsError("Selected application unavailable", code="application_unavailable")
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise HostAppsError("Desktop session unavailable", code="gui_session_unavailable")
        after = os.stat(path, follow_symlinks=False)
        if any(getattr(before, key) != getattr(after, key) for key in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")):
            raise HostAppsError("Host resource changed", code="resource_unavailable")
        files = append(None, file)
        # Child applications must not inherit the JSON response pipe.
        saved = os.dup(1)
        try:
            with open(os.devnull, "wb") as sink:
                os.dup2(sink.fileno(), 1)
                if not launch(match[0], files, None, None):
                    raise HostAppsError("Application dispatch failed", code="application_unavailable")
        finally:
            os.dup2(saved, 1); os.close(saved)
        return {"application": {"id": match[1]["id"], "name": match[1]["name"]}}
    finally:
        if files: free_list(files)
        for app in owned: unref(app)
        if apps: free_list(apps)
        if default: unref(default)
        if info: unref(info)
        if file: unref(file)


def worker_main():
    try:
        request = json.loads(sys.stdin.buffer.read(65536))
        result = _native(request["path"], request.get("app_id"))
    except Exception as error:
        result = {"error": True, "code": getattr(error, "code", "provider_unavailable")}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode())


if __name__ == "__main__":
    worker_main()

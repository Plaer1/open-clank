"""Windows Shell associations; native paths stay inside a bounded helper.

The provider authorizes the resource before this interface is called. Handler
IDs are hashes of Shell-returned identities, never executable command strings.
"""

from __future__ import annotations

import asyncio
import ctypes as C
import hashlib
import json
import logging
import ntpath
import os
import stat
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from src.openclank.host_apps import HostAppsError

_DEFAULT = "windows:default"
_WORK = asyncio.Semaphore(2)
_TIMEOUT = 15
_LOG = logging.getLogger(__name__)
_PHASES = frozenset({"initialize", "enumerate", "handler_name", "handler_ui_name",
                    "parse_item", "bind_data_object", "invoke_handler", "default_execute", "shell_path"})


class WindowsHostApps:
    def _call(self, path: str, app_id: str | None = None, *, cancel_event=None):
        if sys.platform != "win32":
            raise HostAppsError("Windows host apps are unavailable", code="unsupported_platform")
        if not ntpath.isabs(path) or "\0" in path:
            raise HostAppsError("Host resource is unavailable", code="resource_unavailable")
        if cancel_event and cancel_event.is_set():
            raise HostAppsError("Host app operation was cancelled", code="operation_cancelled")
        if getattr(sys, "frozen", False):
            roots = [Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)), Path(sys.executable).parent]
            candidates = [root / relative for root in roots for relative in
                          ("bin/openclank-windows-host-apps.exe", "openclank-windows-host-apps.exe")]
            helper = next((candidate for candidate in candidates if candidate.is_file()), None)
            if helper is None:
                raise HostAppsError("Host app helper is unavailable", code="provider_unavailable")
            command = [str(helper)]
        else:
            command = [sys.executable, "-B", "-m", "src.openclank.windows_host_apps", "--worker"]
        payload = {"path": path, "app_id": app_id}
        try:
            result = subprocess.run(command, input=json.dumps(payload), text=True, encoding="utf-8",
                                    capture_output=True, timeout=_TIMEOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW,
                                    cwd=Path(__file__).resolve().parents[2])
            value = json.loads(result.stdout)
            if result.returncode or not isinstance(value, dict):
                raise ValueError("Invalid helper response")
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise HostAppsError("Host app operation failed or timed out", code="provider_unavailable") from error
        if value.get("error"):
            native = value.get("native_error")
            if isinstance(native, dict) and native.get("phase") in _PHASES:
                hr, win32 = native.get("hresult"), native.get("win32")
                if (hr is None or type(hr) is int and -(1 << 31) <= hr < (1 << 31)) and (win32 is None or type(win32) is int and 0 <= win32 < (1 << 32)):
                    _LOG.warning("Windows host-app native failure phase=%s hresult=%s win32=%s", native["phase"], hr, win32)
            code = value.get("code")
            if code not in {"application_unavailable", "provider_unavailable", "gui_session_unavailable"}:
                code = "provider_unavailable"
            raise HostAppsError("The host application could not be opened" if app_id else "Host app discovery is unavailable", code=code)
        return value

    def discover(self, path: str):
        return self._call(path)["applications"]

    def launch(self, path: str, app_id: str, *, cancel_event=None):
        if not str(app_id or "").strip():
            raise HostAppsError("Choose an installed application", code="application_required")
        return self._call(path, app_id, cancel_event=cancel_event)["application"]

    async def _async(self, function, *args):
        async with _WORK:
            cancelled = threading.Event()
            kwargs = {"cancel_event": cancelled} if function == self.launch else {}
            task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled.set()
                # Keep admission until the bounded helper finishes, including on
                # repeated cancellation; never leave unowned background work.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                raise

    async def discover_async(self, path: str):
        return await self._async(self.discover, path)

    async def launch_async(self, path: str, app_id: str):
        return await self._async(self.launch, path, app_id)


class _GUID(C.Structure):
    _fields_ = [("data", C.c_ubyte * 16)]

    @classmethod
    def parse(cls, value):
        return cls((C.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))


def _shell_path_spelling(path: str) -> str:
    """Convert only conventional filesystem namespace aliases for Shell APIs."""
    if path.startswith('\\\\?\\UNC\\'):
        display = '\\\\' + path[8:]
    elif path.startswith('\\\\?\\'):
        display = path[4:]
    else:
        display = path
    drive, tail = ntpath.splitdrive(display)
    drive_path = len(drive) == 2 and drive[0].isascii() and drive[0].isalpha() and drive[1] == ":"
    unc_path = drive.startswith('\\\\') and len(drive[2:].split('\\')) == 2 and all(drive[2:].split('\\'))
    components = (drive[2:].split("\\") if unc_path else []) + tail[1:].split("\\")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"}
    if (not (drive_path or unc_path) or not tail.startswith("\\")
            or display.startswith(('\\\\?\\', '\\\\.\\'))
            or any(not part or part in {".", ".."} or part.endswith((" ", "."))
                   or any(ord(char) < 32 or char in '<>:"/|?*' for char in part)
                   or part.split(".", 1)[0].upper() in reserved for part in components)):
        raise HostAppsError("Unsupported Shell file spelling", code="application_unavailable")
    return display


def _authorized_shell_path(path: str) -> str:
    """Keep I/O authority canonical; verify any display alias names the same file."""
    try:
        display = _shell_path_spelling(path)
        if display != path:
            original = os.stat(path)
            alias = os.stat(display)
            after = os.stat(path)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if (not stat.S_ISREG(original.st_mode) or not original.st_ino
                    or any(getattr(original, field) != getattr(value, field)
                           for value in (alias, after) for field in fields)):
                raise HostAppsError("Shell file identity changed", code="application_unavailable")
        return display
    except (OSError, HostAppsError) as cause:
        error = HostAppsError("Shell file spelling unavailable", code="application_unavailable")
        error.native_error = {"phase": "shell_path", "win32": getattr(cause, "winerror", None)}
        raise error from cause


def _native(path: str, selected: str | None):
    """Run only in the disposable Windows STA helper process."""
    if sys.platform != "win32" or not ntpath.isabs(path) or "\0" in path:
        raise HostAppsError("Unsupported host resource", code="provider_unavailable")
    from ctypes import wintypes as W
    shell = C.WinDLL("shell32", use_last_error=True)
    ole = C.WinDLL("ole32")
    HRESULT = C.c_int32
    pointer = C.c_void_p
    ole.CoInitializeEx.argtypes = [pointer, W.DWORD]
    ole.CoInitializeEx.restype = HRESULT
    ole.CoUninitialize.argtypes = []
    ole.CoTaskMemFree.argtypes = [pointer]

    def check(hr, phase):
        if hr < 0:
            error = HostAppsError("Shell association operation failed", code="application_unavailable")
            error.native_error = {"phase": phase, "hresult": int(hr)}
            raise error

    def method(obj, index, *types):
        table = C.cast(obj, C.POINTER(C.POINTER(pointer))).contents
        return C.WINFUNCTYPE(HRESULT, pointer, *types)(table[index])

    def release(obj):
        if obj:
            method(obj, 2)(obj)

    def string(obj, index, phase):
        value = pointer()
        try:
            check(method(obj, index, C.POINTER(pointer))(obj, C.byref(value)), phase)
            return C.wstring_at(value) if value else ""
        finally:
            if value:
                ole.CoTaskMemFree(value)

    check(ole.CoInitializeEx(None, 2), "initialize")  # COINIT_APARTMENTTHREADED
    enumerator, item, data = pointer(), pointer(), pointer()
    handlers = []
    try:
        extension = ntpath.splitext(path)[1]
        if extension and selected != _DEFAULT:
            shell.SHAssocEnumHandlers.argtypes = [W.LPCWSTR, W.DWORD, C.POINTER(pointer)]
            shell.SHAssocEnumHandlers.restype = HRESULT
            check(shell.SHAssocEnumHandlers(extension, 0, C.byref(enumerator)), "enumerate")
            for _ in range(256):
                handler, fetched = pointer(), W.ULONG()
                hr = method(enumerator, 3, W.ULONG, C.POINTER(pointer), C.POINTER(W.ULONG))(
                    enumerator, 1, C.byref(handler), C.byref(fetched))
                check(hr, "enumerate")
                if not fetched.value:
                    break
                handlers.append((handler, None))  # own pointer even if metadata fails
                identity = string(handler, 3, "handler_name")
                name = string(handler, 4, "handler_ui_name")
                app = {"id": "windows:handler:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                       "name": name or "Installed application", "default": False}
                handlers[-1] = (handler, app if identity else None)
        default = {"id": _DEFAULT, "name": "Default associated application", "default": True}
        if selected is None:
            applications = [default]
            for _, app in handlers:
                if app and not any(row["id"] == app["id"] for row in applications):
                    applications.append(app)
            return {"applications": applications}
        shell_path = _authorized_shell_path(path)
        if selected == _DEFAULT:
            class ExecuteInfo(C.Structure):
                _fields_ = [("cbSize", W.DWORD), ("fMask", W.ULONG), ("hwnd", W.HWND),
                            ("lpVerb", W.LPCWSTR), ("lpFile", W.LPCWSTR), ("lpParameters", W.LPCWSTR),
                            ("lpDirectory", W.LPCWSTR), ("nShow", C.c_int), ("hInstApp", W.HINSTANCE),
                            ("lpIDList", pointer), ("lpClass", W.LPCWSTR), ("hkeyClass", W.HKEY),
                            ("dwHotKey", W.DWORD), ("hIcon", W.HANDLE), ("hProcess", W.HANDLE)]
            info = ExecuteInfo()
            info.cbSize = C.sizeof(info)
            info.fMask = 0x100 | 0x400  # NOASYNC | FLAG_NO_UI: finish DDE before helper exits
            info.lpVerb, info.lpFile, info.nShow = "open", shell_path, 1
            shell.ShellExecuteExW.argtypes = [C.POINTER(ExecuteInfo)]
            shell.ShellExecuteExW.restype = W.BOOL
            if not shell.ShellExecuteExW(C.byref(info)):
                error = HostAppsError("Default application unavailable", code="application_unavailable")
                error.native_error = {"phase": "default_execute", "win32": C.get_last_error()}
                raise error
            return {"application": {"id": _DEFAULT, "name": default["name"]}}
        match = next(((handler, app) for handler, app in handlers if app and app["id"] == selected), None)
        if not match:
            raise HostAppsError("Selected application unavailable", code="application_unavailable")
        iid_item = _GUID.parse("43826d1e-e718-42ee-bc55-a1e261c37bfe")
        iid_data = _GUID.parse("0000010e-0000-0000-c000-000000000046")
        bhid_data = _GUID.parse("b8c0bd9f-ed24-455c-83e6-d5390c4fe8c4")
        shell.SHCreateItemFromParsingName.argtypes = [W.LPCWSTR, pointer, C.POINTER(_GUID), C.POINTER(pointer)]
        shell.SHCreateItemFromParsingName.restype = HRESULT
        check(shell.SHCreateItemFromParsingName(shell_path, None, C.byref(iid_item), C.byref(item)), "parse_item")
        check(method(item, 3, pointer, C.POINTER(_GUID), C.POINTER(_GUID), C.POINTER(pointer))(
            item, None, C.byref(bhid_data), C.byref(iid_data), C.byref(data)), "bind_data_object")
        check(method(match[0], 8, pointer)(match[0], data), "invoke_handler")  # IAssocHandler::Invoke
        return {"application": {"id": match[1]["id"], "name": match[1]["name"]}}
    finally:
        release(data)
        release(item)
        for handler, _ in handlers:
            release(handler)
        release(enumerator)
        ole.CoUninitialize()


def worker_main():
    try:
        request = json.loads(sys.stdin.read(65536))
        result = _native(request["path"], request.get("app_id"))
    except Exception as error:
        result = {"error": True, "code": getattr(error, "code", "provider_unavailable")}
        native = getattr(error, "native_error", None)
        if isinstance(native, dict) and native.get("phase") in _PHASES:
            result["native_error"] = native
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    worker_main()

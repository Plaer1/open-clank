"""Private Event delivery to the durable native shell owner, never its children."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from .hex_windows import _Attributes, _apis, _checked, pinned_bytes


def _native():
    kernel, security, _ = _apis()
    for name, arguments, result in (
        ("CreateEventW", [C.POINTER(_Attributes), W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
        ("OpenEventW", [W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
        ("SetEvent", [W.HANDLE], W.BOOL),
        ("WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
        ("OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        ("GetProcessTimes", [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
        ("TerminateProcess", [W.HANDLE, W.UINT], W.BOOL),
    ):
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel, security


def _creation(kernel, process):
    values = [W.FILETIME() for _ in range(4)]
    _checked(kernel.GetProcessTimes(process, *(C.byref(value) for value in values)))
    return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime


def _owner_sid(kernel, security, process):
    token = W.HANDLE()
    _checked(security.OpenProcessToken(process, 8, C.byref(token)))
    try:
        size = W.DWORD()
        C.set_last_error(0)
        success = security.GetTokenInformation(token, 1, None, 0, C.byref(size))
        if success or C.get_last_error() != 122 or not size.value:
            raise OSError("cannot size durable-owner token identity")
        buffer = C.create_string_buffer(size.value)
        _checked(security.GetTokenInformation(token, 1, buffer, size, C.byref(size)))
        text = C.c_void_p()
        _checked(security.ConvertSidToStringSidW(C.cast(buffer, C.POINTER(C.c_void_p))[0], C.byref(text)))
        try:
            return C.wstring_at(text)
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


class CancelEndpoint:
    """Only the durable worker creates/retains this noninherited manual Event."""
    def __init__(self, path, *, job_id, action_id, owner):
        if not re.fullmatch(r"[0-9a-f]{12}", job_id) or not action_id or not owner:
            raise ValueError("invalid trusted native cancellation identity")
        self.path = Path(path)
        if self.path.exists():
            raise ValueError("native cancellation endpoint must be new")
        self.kernel, security = _native()
        self.handle = None
        self.published = False
        process = self.kernel.GetCurrentProcess()
        sid = _owner_sid(self.kernel, security, process)
        generation = uuid.uuid4().hex
        name = "Local\\OpenClank.Hex.Cancel." + job_id + "." + generation
        descriptor = C.c_void_p()
        _checked(security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"D:P(A;;GA;;;{sid})(A;;GA;;;SY)", 1, C.byref(descriptor), None))
        try:
            attributes = _Attributes(C.sizeof(_Attributes), descriptor, False)
            C.set_last_error(0)
            self.handle = self.kernel.CreateEventW(C.byref(attributes), True, False, name)
            error = C.get_last_error()
            _checked(self.handle)
            if error == 183:
                raise OSError("native cancellation Event name already exists")
            data = {"version": 1, "job_id": job_id, "action_id": action_id,
                    "owner": owner, "owner_sid": sid, "worker_pid": os.getpid(),
                    "worker_creation": _creation(self.kernel, process),
                    "generation": generation, "event": name}
            # Exclusive create prevents taking ownership of raced metadata.
            with self.path.open("x", encoding="utf-8") as output:
                self.published = True
                json.dump(data, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            self.close()
            raise
        finally:
            self.kernel.LocalFree(descriptor)

    def requested(self):
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result not in (0, 258):
            raise C.WinError(C.get_last_error())
        return result == 0

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.published:
            self.path.unlink(missing_ok=True)
            self.published = False


def wait_owned(process, endpoint, *, timeout):
    """One owner-thread supervisor; IO threads must drain pipes independently."""
    deadline = time.monotonic() + timeout
    while True:
        if endpoint.requested():
            return process.stop_tree(timeout=10), True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("native shell owned Job", timeout)
        try:
            receipt = process.wait_tree(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            continue
        # Cancellation never becomes natural completion even if it races exit.
        return receipt, endpoint.requested()


def cancel_owner(path, *, job_id, action_id, owner, worker_pid, timeout=15):
    """Deliver cancellation, retaining exact owner handle through bounded grace.

    Forced owner termination is an error recovery, not an empty-Job receipt.
    No direct child PID, taskkill, PID enumeration or replacement owner is used.
    """
    path = Path(path)
    if path.stat().st_size > 4096:
        raise ValueError("oversized native cancellation endpoint")
    data = json.loads(pinned_bytes(path))
    generation = data.get("generation", "")
    if (data.get("version") != 1 or data.get("job_id") != job_id
            or data.get("action_id") != action_id or data.get("owner") != owner
            or data.get("worker_pid") != worker_pid
            or not re.fullmatch(r"[0-9a-f]{12}", job_id)
            or not re.fullmatch(r"[0-9a-f]{32}", generation)
            or data.get("event") != "Local\\OpenClank.Hex.Cancel." + job_id + "." + generation):
        raise ValueError("native cancellation endpoint does not match trusted job")
    kernel, security = _native()
    # Exact PID+creation identity retained, with no child/tree access rights.
    process = kernel.OpenProcess(0x1000 | 0x100000 | 1, False, worker_pid)
    _checked(process)
    try:
        sid = _owner_sid(kernel, security, kernel.GetCurrentProcess())
        if (sid != data.get("owner_sid") or sid != _owner_sid(kernel, security, process)
                or _creation(kernel, process) != data.get("worker_creation")):
            raise ValueError("native cancellation owner identity is stale or foreign")
        state = kernel.WaitForSingleObject(process, 0)
        if state == 0:
            return {"requested": False, "owner_exited": True, "forced": False}
        if state != 258:
            raise C.WinError(C.get_last_error())
        event = kernel.OpenEventW(2, False, data["event"])
        _checked(event)
        try:
            _checked(kernel.SetEvent(event))
        finally:
            kernel.CloseHandle(event)
        result = kernel.WaitForSingleObject(process, max(0, min(15000, int(timeout * 1000))))
        if result == 0:
            return {"requested": True, "owner_exited": True, "forced": False}
        if result != 258:
            raise C.WinError(C.get_last_error())
        _checked(kernel.TerminateProcess(process, 1))
        return {"requested": True, "owner_exited": kernel.WaitForSingleObject(process, 5000) == 0,
                "forced": True, "tree_empty_verified": False}
    finally:
        kernel.CloseHandle(process)

"""Windows x64 adapter (the minimum): UI Automation + SendInput through our own PowerShell helper, same JSON Lines protocol as Linux.

The helper script (`windows_uia.ps1`) answers exactly what `--a11y-v1` (reads, element actions) and `--control-v1` (pointer click, keys, type) answer
on Linux, so protocol, leases, observations, approvals, Stop and the action log are the shared ones. What the minimum does not cover (move, scroll,
drag, paste, probe, select_text, secondary actions beyond invoke/toggle/select) answers the typed `unsupported_action`. Windows ARM64 is deferred
(`platform_ok` is False there). Not run on a real Windows desktop.
"""
from __future__ import annotations

import platform
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

SCRIPT = Path(__file__).with_name("windows_uia.ps1")


def is_windows() -> bool:
    return sys.platform == "win32"


def platform_ok() -> bool:
    """Windows x64 with an interactive desktop assumed (a service session has no UI to automate); ARM64 is deferred."""
    return is_windows() and platform.machine().lower() in {"amd64", "x86_64"}


def powershell() -> Optional[str]:
    return shutil.which("powershell.exe") or shutil.which("powershell")


def helper_argv(mode: str) -> Optional[list[str]]:
    ps = powershell()
    if mode not in ("a11y", "control") or ps is None or not SCRIPT.is_file():
        return None
    return [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-Mode", mode]


def facts() -> dict:
    """What is present, without running anything."""
    return {"platform": sys.platform, "machine": platform.machine().lower(), "powershell": powershell() is not None, "helper_script": SCRIPT.is_file()}


def spawn_kwargs() -> dict:
    """Extra subprocess arguments: a new process group, the portable way."""
    if is_windows():
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}



import ctypes
from ctypes import wintypes

# Windows handles, rather than PIDs, pin the process identity through Stop. This
# implementation is intentionally unavailable on systems lacking these APIs.
def _api():
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    a = ctypes.WinDLL('advapi32', use_last_error=True)
    n = ctypes.WinDLL('ntdll', use_last_error=True)
    sh = ctypes.WinDLL('shell32', use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]; k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]; k.CloseHandle.restype = wintypes.BOOL
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]; k.GetProcessTimes.restype = wintypes.BOOL
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]; k.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]; k.WaitForSingleObject.restype = wintypes.DWORD
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]; k.TerminateProcess.restype = wintypes.BOOL
    k.LocalFree.argtypes = [wintypes.HLOCAL]; k.LocalFree.restype = wintypes.HLOCAL
    a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]; a.OpenProcessToken.restype = wintypes.BOOL
    a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]; a.GetTokenInformation.restype = wintypes.BOOL
    a.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]; a.EqualSid.restype = wintypes.BOOL
    n.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]; n.NtQueryInformationProcess.restype = ctypes.c_long
    sh.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]; sh.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    return k, a, n, sh


def _token_user(api, process):
    k, a, _, _ = api
    token = wintypes.HANDLE()
    if not a.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
        raise OSError('OpenProcessToken failed')
    try:
        size = wintypes.DWORD()
        a.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value or size.value > 65536:
            raise OSError('invalid token information size')
        buf = ctypes.create_string_buffer(size.value)
        if not a.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
            raise OSError('GetTokenInformation failed')
        return buf
    finally:
        k.CloseHandle(token)


def _command_line(api, handle):
    k, _, n, sh = api
    size = wintypes.ULONG()
    n.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(size))
    if not size.value or size.value > 131072:
        raise OSError('command line unavailable')
    buf = ctypes.create_string_buffer(size.value)
    if n.NtQueryInformationProcess(handle, 60, buf, size, ctypes.byref(size)) != 0:
        raise OSError('command line unavailable')
    class UnicodeString(ctypes.Structure):
        _fields_ = [('length', wintypes.USHORT), ('maximum', wintypes.USHORT), ('buffer', ctypes.c_void_p)]
    u = UnicodeString.from_buffer(buf)
    start = ctypes.addressof(buf); end = start + len(buf)
    if u.length % 2 or not u.buffer or not start <= u.buffer <= end or u.buffer + u.length > end:
        raise OSError('invalid command line')
    line = ctypes.wstring_at(u.buffer, u.length // 2)
    count = ctypes.c_int()
    argv = sh.CommandLineToArgvW(line, ctypes.byref(count))
    if not argv:
        raise OSError('invalid command line')
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        k.LocalFree(ctypes.cast(argv, wintypes.HLOCAL))


def _matches_command(image, args, expected):
    if len(args) != 9 or len(expected) != 9:
        return False
    same_path = lambda x, y: os.path.normcase(os.path.realpath(x)) == os.path.normcase(os.path.realpath(y))
    return (same_path(image, expected[0]) and same_path(args[0], expected[0]) and
            same_path(args[6], expected[6]) and
            all(args[i].casefold() == expected[i].casefold() for i in (1, 2, 3, 4, 5, 7, 8)))


def open_verified_helper(pid: int, kind: str):
    """Return an owned process handle only for this user's exact configured helper command."""
    if not platform_ok() or kind not in ('control', 'a11y'):
        return None
    expected = helper_argv(kind)
    if not expected:
        return None
    try:
        api = _api(); k, a, _, _ = api
        handle = k.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, pid)
        if not handle:
            return None
        try:
            creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not k.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
                raise OSError('process times unavailable')
            image = ctypes.create_unicode_buffer(32768); size = wintypes.DWORD(len(image))
            if not k.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
                raise OSError('process image unavailable')
            args = _command_line(api, handle)
            if not _matches_command(image.value, args, expected):
                raise OSError('helper command mismatch')
            own, other = _token_user(api, k.GetCurrentProcess()), _token_user(api, handle)
            sid1 = ctypes.cast(own, ctypes.POINTER(ctypes.c_void_p))[0]
            sid2 = ctypes.cast(other, ctypes.POINTER(ctypes.c_void_p))[0]
            if not a.EqualSid(sid1, sid2) or k.WaitForSingleObject(handle, 0) != 258:
                raise OSError('helper owner or state mismatch')
            return {'handle': handle, 'api': api, 'start': (creation.dwHighDateTime << 32) | creation.dwLowDateTime,
                    'exe': image.value, 'kind': kind, 'platform': 'windows', 'pid': pid}
        except Exception:
            k.CloseHandle(handle)
            raise
    except (OSError, AttributeError, ValueError, IndexError):
        return None


def helper_alive(entry) -> bool:
    try:
        return entry['api'][0].WaitForSingleObject(entry['handle'], 0) == 258
    except (OSError, KeyError, AttributeError):
        return False


def terminate_helper(entry) -> bool:
    """Terminate the held process, never a freshly resolved PID; input release is unknown."""
    return bool(helper_alive(entry) and entry['api'][0].TerminateProcess(entry['handle'], 1))


def close_helper(entry) -> None:
    try:
        entry['api'][0].CloseHandle(entry['handle'])
    except (OSError, KeyError, AttributeError):
        pass


class LaunchPipe:
    """A bounded inbound pipe. Only a matching client process can complete it."""
    def __init__(self, secret: str, kind: str, timeout: float = 15.0):
        import secrets
        import threading
        import time
        self.secret, self.kind, self.deadline = secret, kind, time.monotonic() + timeout
        self.name = 'openclank-control-' + secrets.token_hex(24)
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.entry = None
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        import hmac
        import time
        handle = None
        descriptor = ctypes.c_void_p()
        try:
            api = _api(); k, a = api[0], api[1]
            class SecurityAttributes(ctypes.Structure):
                _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]
            a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
            a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
            # Owner rights only: the default object owner is the current token user.
            if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:P(A;;GA;;;OW)", 1, ctypes.byref(descriptor), None):
                return
            security = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            k.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
            k.CreateNamedPipeW.restype = wintypes.HANDLE
            k.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]; k.ConnectNamedPipe.restype = wintypes.BOOL
            k.GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]; k.GetNamedPipeClientProcessId.restype = wintypes.BOOL
            k.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]; k.ReadFile.restype = wintypes.BOOL
            k.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]; k.WriteFile.restype = wintypes.BOOL
            handle = k.CreateNamedPipeW('\\\\.\\pipe\\' + self.name, 0x00000003 | 0x00080000, 0x00000001 | 0x00000008, 1, 256, 256, 0, ctypes.byref(security))
            if not handle or handle == ctypes.c_void_p(-1).value:
                handle = None
                return
            while not self.cancelled.is_set() and time.monotonic() < self.deadline:
                if k.ConnectNamedPipe(handle, None) or ctypes.get_last_error() == 535:
                    break
                if ctypes.get_last_error() not in (232, 536):
                    return
                time.sleep(0.02)
            else:
                return
            pid = wintypes.ULONG()
            if not k.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
                return
            chunks = bytearray()
            while not self.cancelled.is_set() and time.monotonic() < self.deadline and len(chunks) < 128:
                buf = ctypes.create_string_buffer(128); read = wintypes.DWORD()
                if k.ReadFile(handle, buf, 128 - len(chunks), ctypes.byref(read), None):
                    chunks.extend(buf.raw[:read.value])
                    if b'\n' in chunks: break
                elif ctypes.get_last_error() not in (232, 536):
                    return
                time.sleep(0.02)
            if not hmac.compare_digest(bytes(chunks), (self.secret + '\n').encode('ascii')):
                return
            entry = open_verified_helper(pid.value, self.kind)
            if entry is None:
                return
            with self.lock:
                if self.cancelled.is_set() or time.monotonic() >= self.deadline:
                    close_helper(entry)
                else:
                    self.entry = entry
                    written = wintypes.DWORD()
                    ack = ctypes.create_string_buffer(b"1")
                    if not k.WriteFile(handle, ack, 1, ctypes.byref(written), None) or written.value != 1:
                        close_helper(entry); self.entry = None
        except (OSError, AttributeError, ValueError):
            return
        finally:
            if handle:
                k.CloseHandle(handle)
            if descriptor:
                k.LocalFree(descriptor)
            self.done.set()
            # Even if nobody consumes the proof, its process handle expires.
            self.cancelled.wait(max(0.0, self.deadline - time.monotonic()))
            self.cancel()

    def take(self):
        import time
        self.done.wait(max(0.0, self.deadline - time.monotonic()))
        with self.lock:
            if not self.done.is_set() or self.cancelled.is_set() or time.monotonic() >= self.deadline:
                return None
            entry, self.entry = self.entry, None
            return entry

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            if self.entry is not None:
                close_helper(self.entry)
                self.entry = None


async def bootstrap_helper(proc, state, reservation: dict, platform: str = "windows") -> bool:
    """Prove a reserved helper before exposing it to normal control requests."""
    import asyncio
    import json
    token = reservation.get("reservation")
    try:
        if not all(isinstance(reservation.get(k), str) for k in ("reservation", "pipe", "secret")):
            return False
        line = json.dumps({"v": 1, "op": "launch_proof", "pipe": reservation["pipe"], "secret": reservation["secret"]}, separators=(",", ":"))
        proc.stdin.write((line + "\n").encode("ascii"))
        await proc.stdin.drain()
        reply = await asyncio.wait_for(proc.stdout.readline(), 8)
        if len(reply) > 1024 or json.loads(reply) != {"ok": True, "state": "launch_proof_sent"}:
            return False
        complete = state.complete_windows_launch if platform == "windows" else state.complete_macos_launch
        await asyncio.wait_for(complete(token), 8)
        token = None
        return True
    except (OSError, ValueError, TypeError, AttributeError, asyncio.TimeoutError, RuntimeError):
        return False
    finally:
        if token is not None:
            try:
                cancel = state.cancel_windows_launch if platform == "windows" else state.cancel_macos_launch
                await cancel(token)
            except Exception: pass

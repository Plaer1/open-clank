"""Bounded History wire exchange over the host's native private endpoint."""
from __future__ import annotations
import hashlib
import os
import socket
import time
from pathlib import Path


def pipe_name(logical: str) -> str:
    return r"\\.\pipe\openclank-history-" + hashlib.sha256(logical.encode()).hexdigest()


def cleanup_endpoint(path: Path) -> None:
    if os.name != "nt":
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".lock").unlink(missing_ok=True)


def exchange(logical: str, frame: bytes, timeout: float, limit: int) -> bytes:
    if os.name == "nt":
        return _pipe_exchange(logical, frame, timeout, limit)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(timeout)
        channel.connect(logical)
        channel.sendall(frame)
        response = bytearray()
        while not response.endswith(b"\n"):
            chunk = channel.recv(min(65536, limit + 1 - len(response)))
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > limit:
                raise OSError("history response exceeds the service frame limit")
        return bytes(response)


def _pipe_exchange(logical: str, frame: bytes, timeout: float, limit: int) -> bytes:
    import ctypes as c
    from ctypes import wintypes as w
    k = c.WinDLL("kernel32", use_last_error=True)
    class Overlapped(c.Structure):
        _fields_ = [("Internal", c.c_size_t), ("InternalHigh", c.c_size_t),
                    ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    k.CreateFileW.restype = w.HANDLE
    k.CreateEventW.argtypes = [c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
    k.CreateEventW.restype = w.HANDLE
    k.WaitNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD]
    k.CloseHandle.argtypes = [w.HANDLE]
    k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k.CancelIoEx.argtypes = [w.HANDLE, c.POINTER(Overlapped)]
    k.GetOverlappedResult.argtypes = [w.HANDLE, c.POINTER(Overlapped), c.POINTER(w.DWORD), w.BOOL]
    for name in ("ReadFile", "WriteFile"):
        getattr(k, name).argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.POINTER(Overlapped)]
    deadline = time.monotonic() + timeout
    name = pipe_name(logical)
    invalid = c.c_void_p(-1).value
    while True:
        handle = k.CreateFileW(name, 0xC0000000, 0, None, 3, 0x40000000, None)
        if handle != invalid:
            break
        error = c.get_last_error()
        remaining = deadline - time.monotonic()
        if error != 231 or remaining <= 0:
            raise c.WinError(error)
        if not k.WaitNamedPipeW(name, max(1, int(remaining * 1000))):
            raise c.WinError(c.get_last_error())
    event = k.CreateEventW(None, True, False, None)
    if not event:
        k.CloseHandle(handle)
        raise c.WinError(c.get_last_error())
    try:
        def transfer(data, write=False):
            ov = Overlapped(hEvent=event)
            count = w.DWORD()
            # Separate events avoid stale signalled state between operations.
            k.ResetEvent.argtypes = [w.HANDLE]
            k.ResetEvent(event)
            ok = (k.WriteFile if write else k.ReadFile)(handle, data, len(data), c.byref(count), c.byref(ov))
            if not ok:
                error = c.get_last_error()
                if error != 997:
                    raise c.WinError(error)
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if k.WaitForSingleObject(event, remaining) != 0:
                    k.CancelIoEx(handle, c.byref(ov))
                    # OVERLAPPED and buffer must stay alive until cancellation finishes.
                    k.GetOverlappedResult(handle, c.byref(ov), c.byref(count), True)
                    raise TimeoutError("history pipe deadline expired")
                if not k.GetOverlappedResult(handle, c.byref(ov), c.byref(count), False):
                    raise c.WinError(c.get_last_error())
            return count.value
        sent = 0
        while sent < len(frame):
            buf = (c.c_char * (len(frame) - sent)).from_buffer_copy(frame[sent:])
            count = transfer(buf, True)
            if not count:
                raise OSError("history pipe write made no progress")
            sent += count
        response = bytearray()
        while not response.endswith(b"\n"):
            size = min(65536, limit + 1 - len(response))
            buf = (c.c_char * size)()
            count = transfer(buf)
            if not count:
                break
            response.extend(bytes(buf[:count]))
            if len(response) > limit:
                raise OSError("history response exceeds the service frame limit")
        return bytes(response)
    finally:
        k.CloseHandle(event)
        k.CloseHandle(handle)


def private_file_fd(path: Path) -> int:
    """Create a new authority preimage with privacy established before bytes."""
    if os.name != "nt":
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    import ctypes as c
    from ctypes import wintypes as w
    import msvcrt
    class Attributes(c.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]
    k = c.WinDLL("kernel32", use_last_error=True)
    a = c.WinDLL("advapi32", use_last_error=True)
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p]
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(Attributes), w.DWORD, w.DWORD, w.HANDLE]
    k.CreateFileW.restype = w.HANDLE
    k.LocalFree.argtypes = [c.c_void_p]
    k.CloseHandle.argtypes = [w.HANDLE]
    descriptor = c.c_void_p()
    if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(_private_sddl(), 1, c.byref(descriptor), None):
        raise c.WinError(c.get_last_error())
    try:
        attrs = Attributes(c.sizeof(Attributes), descriptor, False)
        handle = k.CreateFileW(str(path), 0x40000000, 0, c.byref(attrs), 1, 0x80, None)
        if handle == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            k.CloseHandle(handle)
            raise
    finally:
        k.LocalFree(descriptor)


def publish_private_file(source: Path, destination: Path) -> None:
    if os.name != "nt":
        os.replace(source, destination)
        return
    import ctypes as c
    from ctypes import wintypes as w
    k = c.WinDLL("kernel32", use_last_error=True)
    k.MoveFileExW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.DWORD]
    # Retain the new protected DACL rather than merging a possibly historical
    # public ACL. Same-parent staging prevents a cross-volume copy fallback.
    if not k.MoveFileExW(str(source), str(destination), 0x9):
        raise c.WinError(c.get_last_error())
    fd = os.open(destination, os.O_WRONLY | os.O_BINARY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_sddl() -> str:
    """Use TokenUser SID, including under elevated/default-owner group tokens."""
    import ctypes as c
    from ctypes import wintypes as w
    k = c.WinDLL("kernel32", use_last_error=True)
    a = c.WinDLL("advapi32", use_last_error=True)
    k.GetCurrentProcess.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.LocalFree.argtypes = [c.c_void_p]
    a.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    a.GetTokenInformation.argtypes = [w.HANDLE, w.DWORD, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    a.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(c.c_void_p)]
    token = w.HANDLE()
    if not a.OpenProcessToken(k.GetCurrentProcess(), 8, c.byref(token)):
        raise c.WinError(c.get_last_error())
    try:
        size = w.DWORD()
        a.GetTokenInformation(token, 1, None, 0, c.byref(size))
        if not size.value:
            raise c.WinError(c.get_last_error())
        buffer = c.create_string_buffer(size.value)
        if not a.GetTokenInformation(token, 1, buffer, size, c.byref(size)):
            raise c.WinError(c.get_last_error())
        sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]
        text = c.c_void_p()
        if not a.ConvertSidToStringSidW(sid, c.byref(text)):
            raise c.WinError(c.get_last_error())
        try:
            return f"D:P(A;;GA;;;SY)(A;;GA;;;{c.wstring_at(text)})"
        finally:
            k.LocalFree(text)
    finally:
        k.CloseHandle(token)

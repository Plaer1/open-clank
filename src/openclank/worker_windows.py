"""Native ownership and one-shot credential transport for Windows ACP workers.

Loaded only on Windows. Secrets never enter argv, environment or disk; no
listener outlives a single spawn. POSIX workers keep their existing fd path.
"""
from __future__ import annotations

import asyncio
import ctypes as C
import os
import subprocess
import uuid
from ctypes import wintypes as W

DWORD = C.c_uint32
HANDLE = C.c_void_p
INVALID_HANDLE = C.c_void_p(-1).value


class OVERLAPPED(C.Structure):
    _fields_ = [("Internal", C.c_size_t), ("InternalHigh", C.c_size_t),
                ("Offset", DWORD), ("OffsetHigh", DWORD), ("hEvent", HANDLE)]


class SECURITY_ATTRIBUTES(C.Structure):
    _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", HANDLE), ("bInheritHandle", C.c_int)]


class JOB_BASIC_LIMIT(C.Structure):
    _fields_ = [("PerProcessUserTimeLimit", C.c_int64), ("PerJobUserTimeLimit", C.c_int64),
                ("LimitFlags", DWORD), ("MinimumWorkingSetSize", C.c_size_t),
                ("MaximumWorkingSetSize", C.c_size_t), ("ActiveProcessLimit", DWORD),
                ("Affinity", C.c_size_t), ("PriorityClass", DWORD), ("SchedulingClass", DWORD)]


class IO_COUNTERS(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOB_EXTENDED_LIMIT(C.Structure):
    _fields_ = [("BasicLimitInformation", JOB_BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", C.c_size_t), ("JobMemoryLimit", C.c_size_t),
                ("PeakProcessMemoryUsed", C.c_size_t), ("PeakJobMemoryUsed", C.c_size_t)]


class THREADENTRY32(C.Structure):
    _fields_ = [("dwSize", DWORD), ("cntUsage", DWORD), ("th32ThreadID", DWORD),
                ("th32OwnerProcessID", DWORD), ("tpBasePri", C.c_int32),
                ("tpDeltaPri", C.c_int32), ("dwFlags", DWORD)]


class SID_AND_ATTRIBUTES(C.Structure):
    _fields_ = [("Sid", HANDLE), ("Attributes", DWORD)]


class TOKEN_GROUPS_ONE(C.Structure):
    _fields_ = [("GroupCount", DWORD), ("Groups", SID_AND_ATTRIBUTES * 1)]


_apis = None


def _api():
    global _apis
    if _apis is not None:
        return _apis
    if os.name != "nt":
        raise RuntimeError("Windows worker APIs require Windows")
    kernel = C.WinDLL("kernel32", use_last_error=True)
    advapi = C.WinDLL("advapi32", use_last_error=True)
    signatures = {
        "CloseHandle": ([HANDLE], C.c_int),
        "GetCurrentProcess": ([], HANDLE),
        "LocalFree": ([HANDLE], HANDLE),
        "CreateEventW": ([HANDLE, C.c_int, C.c_int, W.LPCWSTR], HANDLE),
        "WaitForSingleObject": ([HANDLE, DWORD], DWORD),
        "CreateNamedPipeW": ([W.LPCWSTR, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, C.POINTER(SECURITY_ATTRIBUTES)], HANDLE),
        "ConnectNamedPipe": ([HANDLE, C.POINTER(OVERLAPPED)], C.c_int),
        "CancelIoEx": ([HANDLE, C.POINTER(OVERLAPPED)], C.c_int),
        "GetOverlappedResult": ([HANDLE, C.POINTER(OVERLAPPED), C.POINTER(DWORD), C.c_int], C.c_int),
        "GetNamedPipeClientProcessId": ([HANDLE, C.POINTER(DWORD)], C.c_int),
        "ReadFile": ([HANDLE, HANDLE, DWORD, C.POINTER(DWORD), C.POINTER(OVERLAPPED)], C.c_int),
        "WriteFile": ([HANDLE, HANDLE, DWORD, C.POINTER(DWORD), C.POINTER(OVERLAPPED)], C.c_int),
        "CreateJobObjectW": ([HANDLE, W.LPCWSTR], HANDLE),
        "SetInformationJobObject": ([HANDLE, C.c_int, HANDLE, DWORD], C.c_int),
        "AssignProcessToJobObject": ([HANDLE, HANDLE], C.c_int),
        "OpenProcess": ([DWORD, C.c_int, DWORD], HANDLE),
        "CreateToolhelp32Snapshot": ([DWORD, DWORD], HANDLE),
        "Thread32First": ([HANDLE, C.POINTER(THREADENTRY32)], C.c_int),
        "Thread32Next": ([HANDLE, C.POINTER(THREADENTRY32)], C.c_int),
        "OpenThread": ([DWORD, C.c_int, DWORD], HANDLE),
        "ResumeThread": ([HANDLE], DWORD),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    for name, args in {
        "OpenProcessToken": [HANDLE, DWORD, C.POINTER(HANDLE)],
        "GetTokenInformation": [HANDLE, C.c_int, HANDLE, DWORD, C.POINTER(DWORD)],
        "ConvertSidToStringSidW": [HANDLE, C.POINTER(HANDLE)],
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": [W.LPCWSTR, DWORD, C.POINTER(HANDLE), C.POINTER(DWORD)],
    }.items():
        function = getattr(advapi, name)
        function.argtypes, function.restype = args, C.c_int
    _apis = kernel, advapi
    return _apis


def _check(success, operation):
    if not success:
        # Report only the operation and numeric OS error, never payload data.
        raise OSError(C.get_last_error(), f"Windows worker {operation} failed")
    return success


def _handle(value, operation):
    _check(value and value != INVALID_HANDLE, operation)
    return value


def _private_descriptor():
    kernel, advapi = _api()
    token, sid_text, descriptor = HANDLE(), HANDLE(), HANDLE()
    try:
        _check(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, C.byref(token)), "token open")
        needed = DWORD()
        # TokenLogonSid: restrict this transient endpoint to this logon session.
        advapi.GetTokenInformation(token, 28, None, 0, C.byref(needed))
        if not needed.value:
            raise RuntimeError("Windows worker logon identity unavailable")
        groups = C.create_string_buffer(needed.value)
        _check(advapi.GetTokenInformation(token, 28, groups, needed, C.byref(needed)), "logon identity")
        parsed = C.cast(groups, C.POINTER(TOKEN_GROUPS_ONE)).contents
        if parsed.GroupCount != 1:
            raise RuntimeError("Windows worker logon identity invalid")
        _check(advapi.ConvertSidToStringSidW(parsed.Groups[0].Sid, C.byref(sid_text)), "SID conversion")
        sid = C.wstring_at(sid_text)
        # Protected DACL. Never use the default pipe ACL (Everyone read access).
        sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;{sid})"
        _check(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, C.byref(descriptor), None), "pipe ACL")
        return descriptor
    finally:
        if token.value:
            kernel.CloseHandle(token)
        if sid_text.value:
            kernel.LocalFree(sid_text)


class WorkerAuthPipe:
    """One private connection, checked against the kernel's actual client PID."""
    def __init__(self, env: dict[str, str]):
        self.kernel, _ = _api()
        self.handle = None
        self.event = None
        self.pending = False
        self.buffer = None
        self.overlapped = OVERLAPPED()
        descriptor = _private_descriptor()
        try:
            attrs = SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), descriptor, 0)
            name = "\\\\.\\pipe\\open-clank-worker-auth-" + uuid.uuid4().hex
            # DUPLEX | OVERLAPPED | FIRST_PIPE_INSTANCE; byte mode, remote denied.
            self.handle = _handle(self.kernel.CreateNamedPipeW(name, 0x40080003, 0x8, 1, 2048, 2048, 0, C.byref(attrs)), "pipe create")
            self.event = _handle(self.kernel.CreateEventW(None, 1, 0, None), "pipe event")
            self.overlapped.hEvent = self.event
            if not self.kernel.ConnectNamedPipe(self.handle, C.byref(self.overlapped)):
                error = C.get_last_error()
                if error == 997:  # ERROR_IO_PENDING
                    self.pending = True
                elif error != 535:  # ERROR_PIPE_CONNECTED
                    raise OSError(error, "Windows worker pipe connect failed")
            env["OPEN_CLANK_WORKER_AUTH_PIPE"] = name
            env["OPEN_CLANK_WORKER_AUTH_PARENT_PID"] = str(os.getpid())
        except BaseException:
            self.close()
            raise
        finally:
            self.kernel.LocalFree(descriptor)

    async def _complete(self, deadline: float, process) -> int:
        if self.pending:
            while self.kernel.WaitForSingleObject(self.event, 0) == 258:
                if process.returncode is not None:
                    raise RuntimeError("Windows worker exited before credential handoff")
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("Windows worker credential handoff timed out")
                await asyncio.sleep(0.01)
            transferred = DWORD()
            _check(self.kernel.GetOverlappedResult(self.handle, C.byref(self.overlapped), C.byref(transferred), 0), "pipe completion")
            self.pending = False
            self.buffer = None
            return transferred.value
        return 0

    async def _transfer(self, write: bool, buffer, size: int, deadline: float, process) -> int:
        # A fresh event/OVERLAPPED for each operation; previous operation finished.
        old_event, self.event = self.event, None
        self.kernel.CloseHandle(old_event)
        self.event = _handle(self.kernel.CreateEventW(None, 1, 0, None), "pipe event")
        self.overlapped = OVERLAPPED(hEvent=self.event)
        transferred = DWORD()
        self.buffer = buffer  # Keep native I/O storage alive through cancellation.
        function = self.kernel.WriteFile if write else self.kernel.ReadFile
        if function(self.handle, buffer, size, C.byref(transferred), C.byref(self.overlapped)):
            self.buffer = None
            return transferred.value
        error = C.get_last_error()
        if error != 997:
            raise OSError(error, "Windows worker pipe transfer failed")
        self.pending = True
        return await self._complete(deadline, process)

    async def deliver(self, process, password: str):
        deadline = asyncio.get_running_loop().time() + 15.0
        await self._complete(deadline, process)
        actual = DWORD()
        _check(self.kernel.GetNamedPipeClientProcessId(self.handle, C.byref(actual)), "client identity")
        if actual.value != process.pid:
            raise PermissionError("Windows worker credential client identity mismatch")
        payload = password.encode("utf-8")
        if not 1 <= len(payload) <= 1024:
            raise RuntimeError("Windows worker credential size invalid")
        framed = C.create_string_buffer(len(payload).to_bytes(4, "little") + payload)
        written = await self._transfer(True, framed, len(payload) + 4, deadline, process)
        if written != len(payload) + 4:
            raise RuntimeError("Windows worker credential handoff incomplete")
        # Reader acknowledges consumption; only then close the buffered pipe.
        ack = C.create_string_buffer(1)
        read = await self._transfer(False, ack, 1, deadline, process)
        if read != 1 or ack.raw != b"\x01":
            raise RuntimeError("Windows worker credential acknowledgement invalid")

    def close(self):
        if self.handle:
            if self.pending:
                self.kernel.CancelIoEx(self.handle, C.byref(self.overlapped))
                # Closing cancels outstanding I/O. Preserve OVERLAPPED until complete.
                transferred = DWORD()
                self.kernel.GetOverlappedResult(self.handle, C.byref(self.overlapped), C.byref(transferred), 1)
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            self.pending = False
            self.buffer = None
        if self.event:
            self.kernel.CloseHandle(self.event)
            self.event = None


class WorkerJob:
    """Private OS process-tree lifetime; not an application policy broker."""
    def __init__(self):
        self.kernel, _ = _api()
        self.handle = _handle(self.kernel.CreateJobObjectW(None, None), "job create")
        limits = JOB_EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        try:
            _check(self.kernel.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)), "job limits")
        except BaseException:
            self.close()
            raise

    def attach_and_resume(self, pid: int):
        process = _handle(self.kernel.OpenProcess(0x0101, 0, pid), "process open")
        try:
            _check(self.kernel.AssignProcessToJobObject(self.handle, process), "job assignment")
        finally:
            self.kernel.CloseHandle(process)
        snapshot = _handle(self.kernel.CreateToolhelp32Snapshot(0x4, 0), "thread snapshot")
        threads = []
        try:
            entry = THREADENTRY32(dwSize=C.sizeof(THREADENTRY32))
            present = self.kernel.Thread32First(snapshot, C.byref(entry))
            while present:
                if entry.th32OwnerProcessID == pid:
                    threads.append(entry.th32ThreadID)
                present = self.kernel.Thread32Next(snapshot, C.byref(entry))
        finally:
            self.kernel.CloseHandle(snapshot)
        if len(threads) != 1:
            raise RuntimeError("Windows suspended worker initial thread unavailable")
        thread = _handle(self.kernel.OpenThread(0x2, 0, threads[0]), "thread open")
        try:
            if self.kernel.ResumeThread(thread) != 1:
                raise RuntimeError("Windows suspended worker resume failed")
        finally:
            self.kernel.CloseHandle(thread)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


async def spawn_owned(*argv, **options):
    """Return an asyncio process and its private Job, assigned before execution."""
    job = WorkerJob()
    process = None
    task = None
    try:
        options.pop("start_new_session", None)
        options["creationflags"] = 0x00000004 | subprocess.CREATE_NEW_PROCESS_GROUP  # CREATE_SUSPENDED
        options["close_fds"] = True
        task = asyncio.create_task(asyncio.create_subprocess_exec(*argv, **options))
        try:
            process = await asyncio.shield(task)
        except asyncio.CancelledError:
            # asyncio may already have created the suspended child. Retrieve and
            # dispose it before propagating cancellation; never lose its PID.
            process = await task
            raise
        job.attach_and_resume(process.pid)
        return process, job
    except BaseException:
        job.close()
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        raise

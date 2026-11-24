"""Private synchronous Windows LPAC launch and owned process-tree lifetime.

The caller prepares the unique profile and byte-copy stage. This module owns
native process/pipe/Job handles; it neither grants filesystem access nor
decides whether a command or candidate is admitted.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import os
import subprocess
import time


class _Security(C.Structure):
    _fields_ = [("length", W.DWORD), ("descriptor", C.c_void_p), ("inherit", W.BOOL)]


class _Startup(C.Structure):
    _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR),
               ("title", W.LPWSTR), *[(n, W.DWORD) for n in
               ("x", "y", "width", "height", "chars_x", "chars_y", "fill", "flags")],
               ("show", W.WORD), ("reserved_bytes", W.WORD),
               ("reserved_data", C.c_void_p), ("stdin", W.HANDLE),
               ("stdout", W.HANDLE), ("stderr", W.HANDLE)]


class _StartupEx(C.Structure):
    _fields_ = [("startup", _Startup), ("attributes", C.c_void_p)]


class _ProcessInfo(C.Structure):
    _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE),
               ("pid", W.DWORD), ("tid", W.DWORD)]


class _SidAttributes(C.Structure):
    _fields_ = [("sid", C.c_void_p), ("attributes", W.DWORD)]


class _Capabilities(C.Structure):
    _fields_ = [("profile", C.c_void_p), ("capabilities", C.POINTER(_SidAttributes)),
               ("count", W.DWORD), ("reserved", W.DWORD)]


class _BasicLimit(C.Structure):
    _fields_ = [("process_time", C.c_longlong), ("job_time", C.c_longlong),
               ("flags", W.DWORD), ("min_working", C.c_size_t),
               ("max_working", C.c_size_t), ("active_limit", W.DWORD),
               ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]


class _IoCounters(C.Structure):
    _fields_ = [(n, C.c_ulonglong) for n in
               ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimit(C.Structure):
    _fields_ = [("basic", _BasicLimit), ("io", _IoCounters),
               *[(n, C.c_size_t) for n in ("process_memory", "job_memory", "peak_process", "peak_job")]]


class _Accounting(C.Structure):
    _fields_ = [(n, C.c_longlong) for n in ("user", "kernel", "period_user", "period_kernel")] + [
        (n, W.DWORD) for n in ("faults", "total", "active", "terminated")]


def _api():
    if os.name != "nt":
        raise OSError("Windows confined process launch requires Windows")
    kernel = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "CreateJobObjectW": ([C.c_void_p, W.LPCWSTR], W.HANDLE),
        "SetInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        "QueryInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
        "AssignProcessToJobObject": ([W.HANDLE, W.HANDLE], W.BOOL),
        "IsProcessInJob": ([W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)], W.BOOL),
        "TerminateJobObject": ([W.HANDLE, W.UINT], W.BOOL),
        "TerminateProcess": ([W.HANDLE, W.UINT], W.BOOL),
        "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
        "GetExitCodeProcess": ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        "ResumeThread": ([W.HANDLE], W.DWORD),
        "CreatePipe": ([C.POINTER(W.HANDLE), C.POINTER(W.HANDLE), C.POINTER(_Security), W.DWORD], W.BOOL),
        "SetHandleInformation": ([W.HANDLE, W.DWORD, W.DWORD], W.BOOL),
        "InitializeProcThreadAttributeList": ([C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)], W.BOOL),
        "UpdateProcThreadAttribute": ([C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p], W.BOOL),
        "DeleteProcThreadAttributeList": ([C.c_void_p], None),
        "CreateProcessW": ([W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD,
                            C.c_void_p, W.LPCWSTR, C.POINTER(_StartupEx), C.POINTER(_ProcessInfo)], W.BOOL),
        "LocalFree": ([C.c_void_p], C.c_void_p),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    advapi = C.WinDLL("advapi32", use_last_error=True)
    advapi.ConvertStringSidToSidW.argtypes = [W.LPCWSTR, C.POINTER(C.c_void_p)]
    advapi.ConvertStringSidToSidW.restype = W.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = W.BOOL
    base = C.WinDLL("kernelbase", use_last_error=True)
    base.DeriveCapabilitySidsFromName.argtypes = [W.LPCWSTR, C.POINTER(C.POINTER(C.c_void_p)),
        C.POINTER(W.DWORD), C.POINTER(C.POINTER(C.c_void_p)), C.POINTER(W.DWORD)]
    base.DeriveCapabilitySidsFromName.restype = W.BOOL
    return kernel, advapi, base


def _check(value, operation):
    if not value:
        raise OSError(C.get_last_error(), operation)
    return value


@dataclass(frozen=True)
class TreeReceipt:
    direct_exit_code: int | None
    active_processes: int
    empty: bool
    termination_requested: bool = False


class OwnedProcess:
    def __init__(self, kernel, job, process, pid, streams, profile_sid, network,
                 capability_names, capability_sids):
        self._kernel, self._job, self._process = kernel, job, process
        self.pid = pid
        self.stdin, self.stdout, self.stderr = streams
        self._empty_receipt = None
        # Requested native launch identities; child token proof remains separate.
        self.appcontainer_sid = profile_sid
        self.network = network
        self.capability_names = capability_names
        self.capability_sids = capability_sids
        self.registry_capability_sid = capability_sids[0]

    def poll(self):
        result = self._kernel.WaitForSingleObject(self._process, 0)
        if result == 258:
            return None
        if result != 0:
            raise OSError(C.get_last_error(), "confined process wait")
        code = W.DWORD()
        _check(self._kernel.GetExitCodeProcess(self._process, C.byref(code)), "confined process exit query")
        return int(code.value)

    def wait(self, timeout=None):
        milliseconds = 0xffffffff if timeout is None else min(0xfffffffe, max(0, int(timeout * 1000)))
        result = self._kernel.WaitForSingleObject(self._process, milliseconds)
        if result == 258:
            raise subprocess.TimeoutExpired("confined process", timeout)
        if result != 0:
            raise OSError(C.get_last_error(), "confined process wait")
        return self.poll()

    def wait_tree(self, timeout=10.0):
        """Passively await full owned-tree completion; never terminate writers."""
        if self._empty_receipt is not None:
            return self._empty_receipt
        deadline = time.monotonic() + max(0, timeout)
        while True:
            accounting = _Accounting()
            _check(self._kernel.QueryInformationJobObject(self._job, 1, C.byref(accounting),
                C.sizeof(accounting), None), "confined Job accounting")
            if accounting.active == 0:
                direct_exit = self.wait(timeout=max(0, deadline - time.monotonic()))
                self._empty_receipt = TreeReceipt(direct_exit, 0, True)
                return self._empty_receipt
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("confined Job completion", timeout)
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))

    def stop_tree(self, timeout=10.0):
        """Explicit cancellation/error cleanup, distinct from passive success."""
        if self._empty_receipt is not None:
            return self._empty_receipt
        _check(self._kernel.TerminateJobObject(self._job, 1), "confined Job termination")
        receipt = self.wait_tree(timeout)
        self._empty_receipt = TreeReceipt(receipt.direct_exit_code, 0, True, True)
        return self._empty_receipt

    def close(self):
        # Last-handle close always supplies the OS kill-on-close boundary.
        # Successful stop_tree is a separate, retained-Job empty receipt.
        for name in ("_job", "_process"):
            handle = getattr(self, name)
            if handle:
                self._kernel.CloseHandle(handle)
                setattr(self, name, None)
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None:
                stream.close()


def launch_confined(argv, *, cwd, env, appcontainer_sid, inherited_handles=(), network="disabled"):
    """Launch trusted inputs with an explicit fixed OS network profile.

    Disabled retains the validator's registryRead-only profile. Enabled adds
    public/private client-server capabilities; host loopback needs separate
    native qualification and is never exempted by this launcher.
    """
    state = {"receipt": TreeReceipt(None, 0, True)}
    try:
        return _launch_confined(argv, cwd=cwd, env=env, appcontainer_sid=appcontainer_sid,
                                inherited_handles=inherited_handles, network=network, state=state)
    except BaseException as failure:
        # None means cleanup is inconclusive: callers must preserve resources.
        # A no-child receipt is safe only before the native creation boundary.
        failure.tree_receipt = state["receipt"]
        raise


def _launch_confined(argv, *, cwd, env, appcontainer_sid, inherited_handles, network, state):
    if network not in {"enabled", "disabled"}:
        raise ValueError("confined network profile must be enabled or disabled")
    capability_names = ("registryRead",) if network == "disabled" else (
        "registryRead", "internetClientServer", "privateNetworkClientServer")
    if not argv or not os.path.isabs(os.fspath(argv[0])):
        raise ValueError("confined executable must be an explicit absolute path")
    if not os.path.isabs(os.fspath(cwd)):
        raise ValueError("confined working directory must be absolute")
    environment_items = sorted(env.items(), key=lambda pair: pair[0].upper())
    if any(not key or "=" in key or "\0" in key or "\0" in value for key, value in environment_items):
        raise ValueError("invalid confined environment entry")
    kernel, advapi, base = _api()
    handles, streams = [], []
    profile = C.c_void_p()
    capability_allocations = []
    attributes = None
    attributes_initialized = False
    job, info, assigned = None, _ProcessInfo(), False
    try:
        _check(advapi.ConvertStringSidToSidW(appcontainer_sid, C.byref(profile)), "profile SID parse")
        selected_sids, capability_sids = [], []
        for name in capability_names:
            groups, capabilities = C.POINTER(C.c_void_p)(), C.POINTER(C.c_void_p)()
            group_count, capability_count = W.DWORD(), W.DWORD()
            # Keep allocation descriptors even if native derivation fails partway.
            capability_allocations.append((groups, group_count, capabilities, capability_count))
            _check(base.DeriveCapabilitySidsFromName(name, C.byref(groups), C.byref(group_count),
                C.byref(capabilities), C.byref(capability_count)), "fixed capability derive")
            if capability_count.value != 1:
                raise OSError("exact single SID required for each fixed capability")
            selected_sids.append(capabilities[0])
            capability_text = W.LPWSTR()
            _check(advapi.ConvertSidToStringSidW(capabilities[0], C.byref(capability_text)), "capability SID text")
            try:
                capability_sids.append(capability_text.value)
            finally:
                kernel.LocalFree(C.cast(capability_text, C.c_void_p))
        capability_array = (_SidAttributes * len(selected_sids))(
            *(_SidAttributes(sid, 4) for sid in selected_sids))
        security = _Capabilities(profile, capability_array, len(selected_sids), 0)
        inherited = _Security(C.sizeof(_Security), None, True)
        parents, children = [], []
        for index in range(3):
            read, write = W.HANDLE(), W.HANDLE()
            _check(kernel.CreatePipe(C.byref(read), C.byref(write), C.byref(inherited), 0), "confined pipe create")
            handles.extend((read.value, write.value))
            parent, child = (write.value, read.value) if index == 0 else (read.value, write.value)
            _check(kernel.SetHandleInformation(parent, 1, 0), "confined parent pipe noninherit")
            parents.append(parent); children.append(child)
        job = _check(kernel.CreateJobObjectW(None, None), "confined Job create")
        limits = _ExtendedLimit(); limits.basic.flags = 0x2000
        _check(kernel.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)), "confined Job kill-on-close")
        size = C.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 3, 0, C.byref(size))
        if C.get_last_error() != 122 or not size.value:
            raise OSError(C.get_last_error(), "confined attribute sizing")
        attributes = C.create_string_buffer(size.value)
        _check(kernel.InitializeProcThreadAttributeList(attributes, 3, 0, C.byref(size)), "confined attributes")
        attributes_initialized = True
        policy = W.DWORD(1)  # ALL_APPLICATION_PACKAGES_OPT_OUT
        inherit_list = (W.HANDLE * (3 + len(inherited_handles)))(*children, *inherited_handles)
        for attribute, value in ((0x20009, security), (0x2000f, policy), (0x20002, inherit_list)):
            _check(kernel.UpdateProcThreadAttribute(attributes, 0, attribute, C.byref(value),
                C.sizeof(value), None, None), "confined launch attribute")
        startup = _StartupEx(); startup.startup.cb = C.sizeof(startup)
        startup.startup.flags = 0x100; startup.attributes = C.cast(attributes, C.c_void_p)
        startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = children
        command = C.create_unicode_buffer(subprocess.list2cmdline([os.fspath(arg) for arg in argv]))
        environment = C.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in environment_items) + "\0\0")
        state["receipt"] = None
        created = kernel.CreateProcessW(os.fspath(argv[0]), command, None, None, True,
            0x80000 | 0x4 | 0x08000000 | 0x400, environment, os.fspath(cwd),
            C.byref(startup), C.byref(info))
        if not created:
            state["receipt"] = TreeReceipt(None, 0, True)
        _check(created, "confined suspended create")
        _check(kernel.AssignProcessToJobObject(job, info.process), "confined Job assign")
        assigned = True
        member = W.BOOL()
        _check(kernel.IsProcessInJob(info.process, job, C.byref(member)), "confined exact Job query")
        if not member.value:
            raise OSError("confined process not in its exact Job")
        # Transfer pipe handles to CRT ownership before allowing child code.
        import msvcrt
        for index, parent in enumerate(parents):
            mode = os.O_WRONLY if index == 0 else os.O_RDONLY
            descriptor = msvcrt.open_osfhandle(parent, mode | os.O_BINARY)
            handles.remove(parent)
            try:
                stream = os.fdopen(descriptor, "wb" if index == 0 else "rb", buffering=0)
            except BaseException:
                os.close(descriptor)
                raise
            streams.append(stream)
        if kernel.ResumeThread(info.thread) != 1:
            raise OSError(C.get_last_error(), "confined initial thread resume")
        kernel.CloseHandle(info.thread); info.thread = None
        result = OwnedProcess(kernel, job, info.process, int(info.pid), streams, appcontainer_sid,
                              network, capability_names, tuple(capability_sids))
        job, info.process = None, None
        return result
    except BaseException as failure:
        if info.process:
            cleanup_deadline = time.monotonic() + 10
            if assigned:
                terminated = kernel.TerminateJobObject(job, 1)
                empty = False
                while terminated:
                    accounting = _Accounting()
                    if not kernel.QueryInformationJobObject(job, 1, C.byref(accounting), C.sizeof(accounting), None):
                        break
                    if accounting.active == 0:
                        empty = True
                        break
                    if time.monotonic() >= cleanup_deadline:
                        break
                    time.sleep(0.01)
                if not empty:
                    failure.add_note("confined launch cleanup empty-Job receipt unavailable")
            else:
                terminated = kernel.TerminateProcess(info.process, 1)
                empty = bool(terminated)  # Still suspended: no descendant could execute.
            remaining = max(0, int((cleanup_deadline - time.monotonic()) * 1000))
            waited = kernel.WaitForSingleObject(info.process, remaining)
            if empty and waited == 0:
                code = W.DWORD()
                if kernel.GetExitCodeProcess(info.process, C.byref(code)):
                    state["receipt"] = TreeReceipt(int(code.value), 0, True, True)
        for stream in streams:
            stream.close()
        raise
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)
        for handle in (info.thread, info.process, job):
            if handle:
                kernel.CloseHandle(handle)
        if attributes_initialized:
            kernel.DeleteProcThreadAttributeList(attributes)
        if profile:
            kernel.LocalFree(profile)
        for groups, group_count, capabilities, capability_count in capability_allocations:
            for pointers, count in ((groups, group_count.value), (capabilities, capability_count.value)):
                if pointers:
                    for index in range(count):
                        kernel.LocalFree(pointers[index])
                    kernel.LocalFree(pointers)

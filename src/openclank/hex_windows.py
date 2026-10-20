"""Private native Hex fixture/profile preparation; no ordinary-command gate.

The synchronous launcher owns processes. This module owns only fresh profile
and copied resource lifetimes, and requires an OS empty-tree receipt to clean.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import uuid


class _Attributes(C.Structure):
    _fields_ = [("length", W.DWORD), ("descriptor", C.c_void_p), ("inherit", W.BOOL)]


def _apis():
    if os.name != "nt":
        raise OSError("native Windows Hex resources require Windows")
    kernel = C.WinDLL("kernel32", use_last_error=True)
    security = C.WinDLL("advapi32", use_last_error=True)
    user = C.WinDLL("userenv", use_last_error=True)
    signatures = [
        (kernel, "GetCurrentProcess", [], W.HANDLE),
        (kernel, "CloseHandle", [W.HANDLE], W.BOOL),
        (kernel, "LocalFree", [C.c_void_p], C.c_void_p),
        (kernel, "CreateDirectoryW", [W.LPCWSTR, C.POINTER(_Attributes)], W.BOOL),
        (security, "OpenProcessToken", [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL),
        (security, "GetTokenInformation", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
        (security, "ConvertSidToStringSidW", [C.c_void_p, C.POINTER(C.c_void_p)], W.BOOL),
        (security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p], W.BOOL),
        (security, "GetSecurityDescriptorDacl", [C.c_void_p, C.POINTER(W.BOOL), C.POINTER(C.c_void_p), C.POINTER(W.BOOL)], W.BOOL),
        (security, "SetNamedSecurityInfoW", [W.LPWSTR, C.c_int, W.DWORD, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p], W.DWORD),
        (security, "FreeSid", [C.c_void_p], C.c_void_p),
        (user, "CreateAppContainerProfile", [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)], C.c_long),
        (user, "DeleteAppContainerProfile", [W.LPCWSTR], C.c_long),
    ]
    for library, name, arguments, result in signatures:
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result
    return kernel, security, user


def _checked(value):
    if not value:
        raise C.WinError(C.get_last_error())
    return value


def _same(left, right):
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def pinned_bytes(path: Path) -> bytes:
    """Reuse History's read-only handle pins without changing its authority."""
    from src.openclank.windows_history_io import read_file
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400 or before.st_nlink != 1:
        raise ValueError("native Hex input must be an independent regular non-reparse file")
    return read_file(path, before, _same)


class PrivateHexProfile:
    def __init__(self, root: Path):
        self.root = Path(os.path.abspath(root))
        if self.root.exists():
            raise ValueError("native Hex profile root must be entirely new")
        self.kernel, self.security, self.user = _apis()
        self.name = "OpenClank.Hex." + uuid.uuid4().hex
        self.created = self.root_created = self.child_started = self.empty = False
        self.manifest: dict[str, str] = {}
        token = W.HANDLE()
        _checked(self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, C.byref(token)))
        try:
            size = W.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, C.byref(size))
            buffer = C.create_string_buffer(size.value)
            _checked(self.security.GetTokenInformation(token, 1, buffer, size, C.byref(size)))
            self.owner_sid = self._sid(C.cast(buffer, C.POINTER(C.c_void_p))[0])
        finally:
            self.kernel.CloseHandle(token)
        sid = C.c_void_p()
        result = self.user.CreateAppContainerProfile(self.name, "Open Clank private Hex", "Exact copied fixture resources", None, 0, C.byref(sid))
        if result < 0:
            raise OSError(f"CreateAppContainerProfile failed: 0x{result & 0xffffffff:08x}")
        self.created = True
        try:
            try:
                self.sid = self._sid(sid)
            finally:
                self.security.FreeSid(sid)
            descriptor = self._descriptor(self._sddl(None) + "S:(ML;OICI;NW;;;LW)")
            try:
                attributes = _Attributes(C.sizeof(_Attributes), descriptor, False)
                _checked(self.kernel.CreateDirectoryW(str(self.root), C.byref(attributes)))
                self.root_created = True
            finally:
                self.kernel.LocalFree(descriptor)
        except BaseException:
            self.cleanup()
            raise

    def _sid(self, value):
        text = C.c_void_p()
        _checked(self.security.ConvertSidToStringSidW(value, C.byref(text)))
        try:
            return C.wstring_at(text)
        finally:
            self.kernel.LocalFree(text)

    def _sddl(self, rights):
        value = f"D:P(A;OICI;FA;;;{self.owner_sid})(A;OICI;FA;;;SY)"
        return value if rights is None else value + f"(A;OICI;{rights};;;{self.sid})"

    def _descriptor(self, text):
        descriptor = C.c_void_p()
        _checked(self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(text, 1, C.byref(descriptor), None))
        return descriptor

    def grant(self, path: Path, *, writable=False, private=False, aap_only=False):
        path = Path(os.path.abspath(path))
        path.relative_to(self.root)
        if getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("reparse point in native Hex copied resources")
        text = self._sddl(None if private or aap_only else ("FA" if writable else "GRGX"))
        if aap_only:
            text += "(A;;GR;;;S-1-15-2-1)"
        descriptor = self._descriptor(text)
        try:
            present, defaulted, acl = W.BOOL(), W.BOOL(), C.c_void_p()
            _checked(self.security.GetSecurityDescriptorDacl(descriptor, C.byref(present), C.byref(acl), C.byref(defaulted)))
            if not present:
                raise ValueError("missing private Hex DACL")
            error = self.security.SetNamedSecurityInfoW(str(path), 1, 4 | 0x80000000, None, None, acl, None)
            if error:
                raise C.WinError(error)
        finally:
            self.kernel.LocalFree(descriptor)

    def put(self, relative: str, payload: bytes):
        path = self.root / relative
        if Path(relative).is_absolute() or Path(relative).drive or ".." in Path(relative).parts:
            raise ValueError("native Hex resource escapes its private root")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(payload)
        self.manifest[relative] = hashlib.sha256(payload).hexdigest()
        return path

    def copy(self, origin: Path, relative: str):
        return self.put(relative, pinned_bytes(origin))

    def protect(self):
        for directory, names, files in os.walk(self.root, followlinks=False):
            relative = Path(directory).relative_to(self.root)
            private = bool(relative.parts and relative.parts[0] == "private")
            self.grant(Path(directory), private=private)
            for name in names:
                if getattr((Path(directory) / name).lstat(), "st_file_attributes", 0) & 0x400:
                    raise ValueError("reparse directory in native Hex copied resources")
            for name in files:
                self.grant(Path(directory) / name, private=private)
        stage = self.root / "stage"
        stage.mkdir(exist_ok=True)
        self.grant(stage, writable=True)

    def verify(self):
        for relative, expected in self.manifest.items():
            if hashlib.sha256(pinned_bytes(self.root / relative)).hexdigest() != expected:
                raise ValueError("native Hex readonly copied resource drifted")

    def cleanup(self):
        if self.child_started and not self.empty:
            raise RuntimeError("native Hex cleanup requires confirmed empty owned Job")
        if self.created:
            result = self.user.DeleteAppContainerProfile(self.name)
            if result < 0:
                raise OSError(f"DeleteAppContainerProfile failed: 0x{result & 0xffffffff:08x}")
            self.created = False
        if self.root_created:
            shutil.rmtree(self.root)
            self.root_created = False


def runtime_sources(source_root: Path):
    """Explicit selected-service runtime inputs, never the whole site-packages."""
    import yaml
    base = Path(sys.base_prefix)
    executable = Path(sys.executable)
    sources = [(executable, "runtime/venv/Scripts/python.exe")]
    sources.append((base / "python.exe", "runtime/base/python.exe"))
    for path in sorted(base.glob("*.dll")):
        sources.append((path, "runtime/base/" + path.name))
    for directory in (base / "DLLs", base / "Lib"):
        for path in sorted(directory.rglob("*")):
            parts = path.relative_to(directory).parts
            if any(part in {"site-packages", "test", "tests", "__pycache__", "idlelib", "tkinter", "turtledemo", "ensurepip"} for part in parts):
                continue
            if path.is_file() and path.suffix.lower() in {".py", ".pyd", ".dll", ".zip"}:
                sources.append((path, "runtime/base/" + directory.name + "/" + path.relative_to(directory).as_posix()))
    yaml_root = Path(yaml.__file__).parent
    for path in sorted(yaml_root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix.lower() in {".py", ".pyd", ".dll"}:
            sources.append((path, "runtime/venv/Lib/site-packages/yaml/" + path.relative_to(yaml_root).as_posix()))
    for name in ("hex_policy_worker.py", "hex_contract.py", "clanker_paths.py"):
        sources.append((source_root / "src" / name, "runtime/code/src/" + name))
    return sources


def prepare_runtime(profile: PrivateHexProfile, sources):
    for origin, relative in sources:
        profile.copy(origin, relative)
    base = profile.root / "runtime" / "base"
    profile.put("runtime/venv/pyvenv.cfg", (f"home = {base}\ninclude-system-site-packages = false\nversion = {sys.version.split()[0]}\n").encode())
    bootstrap = "import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).parent/'code'))\nfrom src.hex_policy_worker import main\nraise SystemExit(main(sys.argv[1]))\n"
    profile.put("runtime/bootstrap.py", bootstrap.encode())
    return profile.root / "runtime" / "venv" / "Scripts" / "python.exe"


def _git_index_projection(root: Path):
    """Snapshot index names, not private Git payloads, for contained checks."""
    if not any((parent / ".git").exists() or (parent / ".git").is_symlink()
               for parent in (root, *root.parents)):
        def verify_non_git():
            if any((parent / ".git").exists() or (parent / ".git").is_symlink()
                   for parent in (root, *root.parents)):
                raise ValueError("Git authority appeared during native policy validation")
        return "non-git", (), verify_non_git
    executable = shutil.which("git")
    if not executable:
        raise ValueError("native policy Git index authority is unavailable")
    prefix = [executable, "--no-optional-locks", "-c", "core.fsmonitor=false",
              "-c", "core.untrackedCache=false"]

    def query(arguments):
        result = subprocess.run(prefix + arguments, cwd=root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=10, check=False)
        if result.returncode != 0 or len(result.stdout) > 16 * 1024 ** 2:
            raise ValueError("native policy Git index query failed or exceeded its bound")
        return result.stdout

    index = Path(os.fsdecode(query(["rev-parse", "--path-format=absolute", "--git-path", "index"]).rstrip(b"\r\n")))
    if not index.is_absolute():
        raise ValueError("native policy Git index locator was not absolute")

    def identity():
        try:
            before = index.lstat()
        except FileNotFoundError:
            return None
        payload = pinned_bytes(index)
        return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                before.st_ctime_ns, hashlib.sha256(payload).hexdigest())

    def cached_paths():
        paths = tuple(sorted({value.decode("utf-8", errors="surrogateescape")
                              for value in query(["--literal-pathspecs", "ls-files", "--cached", "-z"]).split(b"\0") if value}))
        if len(paths) > 100000:
            raise ValueError("native policy Git index exceeds the 100,000-path limit")
        return paths

    original = identity()
    paths = cached_paths()
    if len(paths) > 100000 or identity() != original:
        raise ValueError("native policy Git index changed while taking its projection")

    def verify():
        current_index = Path(os.fsdecode(query(["rev-parse", "--path-format=absolute", "--git-path", "index"]).rstrip(b"\r\n")))
        if current_index != index or identity() != original or cached_paths() != paths or identity() != original:
            raise ValueError("native policy Git index changed during validation")
    return "git", paths, verify


def validate_windows_candidates(root: Path, *, contract_relative: str, source_files,
                                candidates, stage: str, verify_authority):
    """Run approved policy over independent candidate bytes in a private Job.

    The caller retains activation/trust authority. No source changes are made;
    an unavailable lifetime receipt blocks the result and preserves resources.
    """
    from src.openclank.windows_confined_process import launch_confined
    root = Path(root)
    sources = runtime_sources(Path(__file__).resolve().parents[2])
    git_index_state, git_index_paths, verify_git_index = _git_index_projection(root)
    estimated = sum(origin.stat().st_size for origin, _ in sources)
    if len(sources) > 5000 or estimated > 128 * 1024 ** 2:
        raise ValueError("selected native policy runtime exceeds the bounded copy budget")
    project_bytes = 0
    for relative in source_files:
        info = (root / relative).lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_nlink != 1:
            raise ValueError("native policy source must contain independent regular non-reparse files")
        project_bytes += info.st_size
    candidate_bytes = sum(len(payload) for payload in candidates.values() if payload is not None)
    resource_root = Path(tempfile.gettempdir()) / ("open-clank-native-hex-" + uuid.uuid4().hex)
    if shutil.disk_usage(resource_root.parent).free < 2 * estimated + 2 * project_bytes + candidate_bytes + 64 * 1024 ** 2:
        raise OSError("insufficient free space for the private native policy runtime")
    profile = process = None
    try:
        profile = PrivateHexProfile(resource_root)
        executable = prepare_runtime(profile, sources)
        original = {}
        candidate_files = set()
        for relative in source_files:
            path = root / relative
            before = path.lstat()
            payload = pinned_bytes(path)
            original[relative] = (before, hashlib.sha256(payload).hexdigest())
            profile.put("source/" + relative, payload)
            replacement = candidates.get(relative, payload)
            if replacement is not None:
                profile.put("candidate/" + relative, replacement)
                candidate_files.add(relative)
        for relative, payload in candidates.items():
            if payload is not None and relative not in candidate_files:
                profile.put("candidate/" + relative, payload)
                candidate_files.add(relative)
        if contract_relative not in original:
            raise ValueError("activated contract missing from native source snapshot")
        changes = {
            "added": sorted(relative for relative, payload in candidates.items()
                            if payload is not None and relative not in original),
            "modified": sorted(relative for relative, payload in candidates.items()
                               if payload is not None and relative in original),
            "deleted": sorted(relative for relative, payload in candidates.items() if payload is None),
        }
        spec = {
            "candidate_root": str(profile.root / "candidate"),
            "source_root": str(profile.root / "source"),
            "policy_root": str(profile.root / "source"),
            "contract_path": str(profile.root / "source" / contract_relative),
            "files": sorted(candidate_files), "changes": changes, "stage": stage,
            "git_index_state": git_index_state, "git_index_paths": list(git_index_paths),
        }
        spec_path = profile.put("input/spec.json", json.dumps(spec, sort_keys=True).encode())
        profile.protect()
        verify_authority()
        verify_git_index()
        for relative, (before, digest) in original.items():
            path = root / relative
            if not _same(before, path.lstat()) or hashlib.sha256(pinned_bytes(path)).hexdigest() != digest:
                raise ValueError("project changed before native candidate validation")
        environment = {"SystemRoot": os.environ["SystemRoot"],
                       "LOCALAPPDATA": os.environ["LOCALAPPDATA"],
                       "TEMP": str(profile.root / "stage"), "TMP": str(profile.root / "stage")}
        profile.child_started, profile.empty = True, False
        try:
            process = launch_confined(
                [str(executable), "-I", "-B", str(profile.root / "runtime/bootstrap.py"), str(spec_path)],
                cwd=str(profile.root / "candidate"), env=environment, appcontainer_sid=profile.sid,
            )
        except BaseException as exc:
            receipt = getattr(exc, "tree_receipt", None)
            profile.empty = bool(receipt is not None and receipt.empty)
            raise
        process.stdin.close()
        buffers = [bytearray(), bytearray()]
        errors = []

        def drain(stream, target):
            try:
                while chunk := stream.read(8192):
                    remaining = max(0, 1024 * 1024 - len(target))
                    target.extend(chunk[:remaining])
                    if len(chunk) > remaining and "policy output exceeded limit" not in errors:
                        errors.append("policy output exceeded limit")
            except BaseException as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=drain, args=(stream, buffer), daemon=True)
                   for stream, buffer in zip((process.stdout, process.stderr), buffers)]
        for thread in threads:
            thread.start()
        receipt = process.wait_tree(timeout=30)
        profile.empty = receipt.empty
        for thread in threads:
            thread.join(2)
        if not receipt.empty or receipt.termination_requested or receipt.direct_exit_code != 0:
            raise RuntimeError("native policy did not complete naturally with an empty owned Job")
        if errors or any(thread.is_alive() for thread in threads):
            raise RuntimeError("native policy output capture failed or exceeded its bound")
        result = json.loads(buffers[0].decode("utf-8"))
        findings = result.get("findings") if isinstance(result, dict) else None
        if not isinstance(findings, list) or any(not isinstance(item, dict) or item.get("level") not in {"block", "warn"} for item in findings):
            raise ValueError("native policy returned malformed findings")
        profile.verify()
        verify_authority()
        verify_git_index()
        for relative, (before, digest) in original.items():
            path = root / relative
            if not _same(before, path.lstat()) or hashlib.sha256(pinned_bytes(path)).hexdigest() != digest:
                raise ValueError("project changed during native candidate validation")
        return {"allowed": not any(item["level"] == "block" for item in findings),
                "findings": findings, "warnings": [item for item in findings if item["level"] != "block"]}
    finally:
        cleanup_error = None
        if process is not None:
            try:
                if not profile.empty:
                    profile.empty = process.stop_tree(timeout=10).empty
            except BaseException as exc:
                cleanup_error = exc
            finally:
                process.close()
        if profile is not None:
            try:
                profile.cleanup()
            except BaseException as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            raise RuntimeError(f"native policy cleanup unverified; profile {profile.name if profile else 'creation-inconclusive'} and private resources retained at {resource_root}: {cleanup_error}") from cleanup_error

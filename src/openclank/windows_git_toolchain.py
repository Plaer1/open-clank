"""Exact private native Git inputs; preparation does not admit a shell profile."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path
import shutil
import stat
import uuid

from .hex_windows import _same
from .windows_history_io import ancestors, pinned, _same_native, _PathStatView

MAX_FILES = 10000
MAX_BYTES = 512 * 1024 * 1024


def _program_files():
    shell, ole = C.WinDLL("shell32"), C.WinDLL("ole32")
    shell.SHGetKnownFolderPath.argtypes = [C.c_void_p, W.DWORD, W.HANDLE, C.POINTER(C.c_void_p)]
    shell.SHGetKnownFolderPath.restype = C.c_long
    ole.CoTaskMemFree.argtypes = [C.c_void_p]
    ole.CoTaskMemFree.restype = None
    roots = []
    for value in ("905e63b6-c1bf-494e-b29c-65b732d3d21a", "7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e"):
        guid = C.create_string_buffer(uuid.UUID(value).bytes_le)
        result = C.c_void_p()
        if shell.SHGetKnownFolderPath(guid, 0, None, C.byref(result)) != 0:
            raise OSError("OS ProgramFiles directory lookup failed")
        try:
            roots.append(Path(C.wstring_at(result)))
        finally:
            ole.CoTaskMemFree(result)
    return roots


def _read(path, consume=lambda data: None):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_file_attributes & 0x400:
        raise ValueError("Git runtime input must be independent regular non-reparse bytes")
    digest, count = hashlib.sha256(), 0
    with ancestors(str(path)) as absolute, pinned(str(absolute), directory=False) as fd:
        opened = os.fstat(fd)
        if not _same_native(_PathStatView(opened), before, _same):
            raise ValueError("Git input changed before read")
        while data := os.read(fd, 1024 * 1024):
            count += len(data)
            if count > before.st_size or count > MAX_BYTES:
                raise ValueError("Git input exceeded pinned byte budget")
            digest.update(data)
            consume(data)
        if count != before.st_size or not _same_native(os.fstat(fd), opened, _same) or not _same(path.lstat(), before):
            raise ValueError("Git input changed during read")
    return before, opened, digest.hexdigest()


class PrivateGitToolchain:
    """Only native bin/libexec, never real repository or per-user configuration.

    Explicit operator root and exact manifest are authority. ProgramFiles location
    is a layout restriction, not a claim that every contained installation has
    an administrator-owned DACL. Native execution qualification remains separate.
    """
    def __init__(self, installation):
        if os.name != "nt":
            raise OSError("native Git preparation requires Windows")
        self.installation = Path(os.path.abspath(installation))
        if not any(self.installation.is_relative_to(root) and self.installation != root for root in _program_files()):
            raise ValueError("explicit ProgramFiles-rooted Git installation required")
        self.baseline = {}
        self.files, self.directories = self._scan()
        total = sum(info.st_size for info in self.files.values())
        if total > MAX_BYTES:
            raise ValueError("Git runtime exceeds 512 MiB")
        self.byte_count = total
        for relative in self.files:
            self.baseline[relative] = _read(self.installation / relative)
        if "mingw64/bin/git.exe" not in self.files:
            raise ValueError("supported native mingw64 Git executable missing")

    def _scan(self):
        files, directories, aliases = {}, {}, {}
        # Pin every ancestor before walking; resolve() must not erase junctions.
        with ancestors(str(self.installation / "mingw64" / "bin" / "git.exe")):
            pass
        for subtree in ("mingw64/bin", "mingw64/libexec/git-core"):
            root = self.installation / subtree
            if not root.is_dir():
                raise ValueError("supported native Git dependency layout missing")
            for directory, names, leaves in os.walk(root, followlinks=False):
                for path in [Path(directory), *(Path(directory) / name for name in names + leaves)]:
                    relative = path.relative_to(self.installation).as_posix()
                    info = path.lstat()
                    if info.st_file_attributes & 0x400:
                        raise ValueError("reparse Git dependency")
                    if aliases.setdefault(relative.casefold(), relative) != relative:
                        raise ValueError("case-alias Git dependency")
                    if stat.S_ISDIR(info.st_mode):
                        directories[relative] = info
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        files[relative] = info
                    else:
                        raise ValueError("unsupported or shared Git dependency")
                    if len(files) + len(directories) > MAX_FILES:
                        raise ValueError("Git dependency count exceeds 10000")
        return files, directories

    def verify(self):
        files, directories = self._scan()
        if files.keys() != self.files.keys() or directories.keys() != self.directories.keys():
            raise ValueError("Git installation dependency listing drifted")
        if any(not _same(info, self.directories[name]) for name, info in directories.items()):
            raise ValueError("Git dependency directory drifted")
        for name, (before, opened, digest) in self.baseline.items():
            current, raw, actual = _read(self.installation / name)
            if not _same(current, before) or not _same_native(raw, opened, _same) or actual != digest:
                raise ValueError("Git dependency bytes or native identity drifted")

    def copy(self, profile):
        if shutil.disk_usage(profile.root).free < self.byte_count + 64 * 1024 * 1024:
            raise ValueError("insufficient independent Git-copy capacity")
        self.verify()
        for name, (_, _, digest) in self.baseline.items():
            relative = "toolchain/git/" + name
            target = profile.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                before, raw, actual = _read(self.installation / name, output.write)
            original, opened, _ = self.baseline[name]
            if actual != digest or not _same(before, original) or not _same_native(raw, opened, _same):
                raise ValueError("Git dependency changed during independent copy")
            profile.manifest[relative] = digest
        self.verify()
        return profile.root / "toolchain/git/mingw64/bin/git.exe"

    @staticmethod
    def environment(profile, executable):
        """No source .git/user config/hooks/credentials or PATH discovery."""
        stage = profile.root / "stage"
        hooks = stage / "empty-hooks"
        hooks.mkdir(exist_ok=True)
        templates = stage / "empty-templates"
        templates.mkdir(exist_ok=True)
        return {"HOME": str(stage), "USERPROFILE": str(stage), "XDG_CONFIG_HOME": str(stage),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "NUL", "GIT_CONFIG_GLOBAL": "NUL",
                "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                "GIT_CONFIG_COUNT": "0", "GIT_TEMPLATE_DIR": str(templates),
                "GIT_EXEC_PATH": str(executable.parent.parent / "libexec/git-core")}

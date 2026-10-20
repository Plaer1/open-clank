"""Independent native shell workspace; promotion remains the caller's transaction."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat

from .hex_windows import _same, pinned_bytes
from .windows_history_io import ancestors, pinned, _same_native, _PathStatView

MAX_FILES = 100_000
MAX_CHANGE_BYTES = 256 * 1024 ** 2


def _relative(value):
    value = str(value)
    path = PureWindowsPath(value)
    if (not value or path.is_absolute() or path.drive or "\\" in value
            or ":" in value or any(part in {"", ".", ".."} or part.endswith((".", " "))
                                   for part in value.split("/"))):
        raise ValueError("native shell path is not canonical workspace-relative data")
    return value


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("native shell files must be independent regular non-reparse bytes")
    return info


def _stream(path, consume=lambda chunk: None):
    """Constant-memory copied bytes/hash through the same strict native pins."""
    before = _regular(path)
    digest = hashlib.sha256()
    with ancestors(str(path)) as absolute, pinned(str(absolute), directory=False) as fd:
        opened = os.fstat(fd)
        if not _same_native(_PathStatView(opened), before, _same):
            raise ValueError("native shell file changed before pinned stream")
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
            consume(chunk)
        if (not _same_native(os.fstat(fd), opened, _same)
                or not _same(_regular(path), before)):
            raise ValueError("native shell file changed during pinned stream")
    return before, digest.hexdigest(), opened


class NativeShellStage:
    """Prepare before launch; inspect only after passive whole-Job completion.

    The trusted listing callback retains the caller's policy/exclusion semantics.
    It must be re-evaluated before publication, alongside activation/trust checks.
    No .git projection or executable/runtime authority is invented here.
    """
    def __init__(self, profile, workspace, *, list_files, excluded):
        self.profile = profile
        self.workspace = Path(workspace).resolve(strict=True)
        self.list_files = list_files
        self.excluded = excluded
        self.root = profile.root / "stage" / "workspace"
        if self.root.exists():
            raise ValueError("native shell writable stage must be new")
        self.paths = self._listing()
        total = sum(_regular(self.workspace / name).st_size for name in self.paths)
        if shutil.disk_usage(profile.root).free < total * 2 + 64 * 1024 ** 2:
            raise OSError("insufficient free space for independent native shell copies")
        self.original = {}
        self.native_original = {}
        self.directory_original = self._directories()
        self.directories = set(self.directory_original) - {"."}
        self.root.mkdir(parents=True)
        for name in self.directories:
            (self.root / name).mkdir(parents=True, exist_ok=True)
            (profile.root / "source" / name).mkdir(parents=True, exist_ok=True)
        for name in self.paths:
            source = self.workspace / name
            readonly = profile.root / "source" / name
            readonly.parent.mkdir(parents=True, exist_ok=True)
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output, readonly.open("xb") as snapshot:
                def copy(chunk):
                    output.write(chunk)
                    snapshot.write(chunk)
                before, digest, native = _stream(source, copy)
            self.original[name] = (before, digest)
            self.native_original[name] = native
            profile.manifest["source/" + name] = digest
            os.chmod(target, stat.S_IMODE(before.st_mode))
            for parent in target.relative_to(self.root).parents:
                if str(parent) != ".":
                    self.directories.add(parent.as_posix())
        self.verify_source()

    def _directories(self):
        result, aliases = {}, {}
        for directory, names, _ in os.walk(self.workspace, followlinks=False):
            path = Path(directory)
            relative = path.relative_to(self.workspace).as_posix()
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("native shell source contains a reparse directory")
            result[relative] = info
            if len(result) > MAX_FILES:
                raise ValueError("native shell source exceeds bounded directory count")
            for name in tuple(names):
                entry = path / name
                key = _relative(entry.relative_to(self.workspace).as_posix())
                if self.excluded(key):
                    names.remove(name)
                    continue
                before = aliases.setdefault(key.casefold(), key)
                if before != key:
                    raise ValueError("native shell source directory case alias")
                info = entry.lstat()
                if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("native shell source contains a reparse directory")
        return result

    def _listing(self):
        paths = tuple(sorted({_relative(name) for name in self.list_files()}))
        if len(paths) > MAX_FILES:
            raise ValueError("native shell file set exceeds the 100,000-file limit")
        if any(self.excluded(name) for name in paths):
            raise ValueError("excluded input in native shell source listing")
        if len({name.casefold() for name in paths}) != len(paths):
            raise ValueError("case aliases in native shell source listing")
        prefixes = {}
        for name in paths:
            parts = name.split("/")
            for count in range(1, len(parts)):
                prefix = "/".join(parts[:count])
                prior = prefixes.setdefault(prefix.casefold(), prefix)
                if prior != prefix:
                    raise ValueError("directory case aliases in native shell source listing")
        return paths

    def protect(self):
        """Call after the parent's readonly profile protection, before launch."""
        for directory, names, files in os.walk(self.root, followlinks=False):
            self.profile.grant(Path(directory), writable=True)
            for name in names:
                item = Path(directory) / name
                info = item.lstat()
                if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("reparse/non-directory in native shell stage")
            for name in files:
                item = Path(directory) / name
                _regular(item)
                self.profile.grant(item, writable=True)

    def verify_source(self):
        if self._listing() != self.paths:
            raise ValueError("source file set changed while native shell was isolated")
        directories = self._directories()
        if (directories.keys() != self.directory_original.keys()
                or any(not _same(info, directories[name])
                       for name, info in self.directory_original.items())):
            raise ValueError("source directories changed while native shell was isolated")
        for name, (before, digest) in self.original.items():
            source = self.workspace / name
            current_path, current_digest, current_native = _stream(source)
            if (not _same(current_path, before) or current_digest != digest
                    or not _same_native(current_native, self.native_original[name], _same)):
                raise ValueError("source bytes/identity changed while native shell was isolated")

    def candidates(self, receipt, *, cancelled=False):
        if (cancelled or receipt is None or not receipt.empty
                or receipt.active_processes != 0 or receipt.termination_requested):
            raise RuntimeError("native shell diff requires passive confirmed empty Job")
        self.verify_source()
        files, directories, aliases = {}, set(), set()
        entry_count = 0
        for directory, names, names_files in os.walk(self.root, followlinks=False):
            current = Path(directory)
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("native shell stage directory is a reparse/alias boundary")
            for name in names:
                entry = current / name
                relative = _relative(entry.relative_to(self.root).as_posix())
                info = entry.lstat()
                if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("native shell stage contains an unsupported directory")
                key = relative.casefold()
                if key in aliases:
                    raise ValueError("native shell stage contains directory case aliases")
                aliases.add(key)
                if len(directories) >= MAX_FILES:
                    raise ValueError("native shell stage exceeds bounded directory count")
                directories.add(relative)
            for name in names_files:
                entry = current / name
                relative = _relative(entry.relative_to(self.root).as_posix())
                key = relative.casefold()
                if key in aliases:
                    raise ValueError("native shell stage contains case aliases")
                aliases.add(key)
                entry_count += 1
                _regular(entry)
                if not self.excluded(relative):
                    files[relative] = entry
                if entry_count > MAX_FILES:
                    raise ValueError("native shell stage exceeds the 100,000-file limit")
        if not self.directories.issubset(directories) or set(self.original) & directories:
            raise ValueError("native shell directory deletion/replacement cannot publish atomically")
        for name in directories - self.directories:
            if not any(path.startswith(name + "/") for path in files):
                raise ValueError("native shell empty-directory creation cannot publish atomically")
        candidates, modes, total = {}, {}, 0
        for name in self.original.keys() - files.keys():
            candidates[name] = None
        for name, entry in files.items():
            before = _regular(entry)
            digest = _stream(entry)[1]
            if not _same(before, entry.lstat()):
                raise ValueError("native shell stage changed after confirmed empty Job")
            prior = self.original.get(name)
            if (prior is None or digest != prior[1]
                    or stat.S_IMODE(before.st_mode) != stat.S_IMODE(prior[0].st_mode)):
                total += before.st_size
                if total > MAX_CHANGE_BYTES:
                    raise ValueError("native shell changes exceed the 256 MiB limit")
                payload = pinned_bytes(entry)
                if len(payload) != before.st_size or hashlib.sha256(payload).hexdigest() != digest:
                    raise ValueError("native shell candidate changed before bounded read")
                candidates[name] = payload
                modes[name] = stat.S_IMODE(before.st_mode)
        self.verify_source()
        return candidates, modes

    def release_readonly_attributes(self):
        """Only after confirmed Job empty, restore owned copied files for deletion."""
        if self.profile.child_started and not self.profile.empty:
            raise RuntimeError("native shell cleanup requires confirmed empty Job")
        for directory, names, files in os.walk(self.root, followlinks=False):
            current = Path(directory).lstat()
            if not stat.S_ISDIR(current.st_mode) or getattr(current, "st_file_attributes", 0) & 0x400:
                raise ValueError("native shell cleanup retains reparse root evidence")
            for name in names:
                info = (Path(directory) / name).lstat()
                if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("native shell cleanup retains reparse directory evidence")
            for name in files:
                path = Path(directory) / name
                _regular(path)
                os.chmod(path, 0o600)

"""Windows no-reparse, handle-pinned capture; no path-only safety fallback."""
from __future__ import annotations
import base64
import ctypes as c
from ctypes import wintypes as w
import hashlib
import os
from pathlib import Path
import stat
from contextlib import contextmanager


class _PathStatView:
    """Match Windows path-stat's legacy creation-time ctime for comparison only."""
    def __init__(self, native):
        self.native = native
        self.st_ctime_ns = getattr(native, "st_birthtime_ns", native.st_ctime_ns)
        self.st_ctime = getattr(native, "st_birthtime", native.st_ctime)

    def __getattr__(self, name):
        return getattr(self.native, name)


def _same_native(left, right, same):
    # Keep the unmodified handle ChangeTime, including when the path API reports
    # birthtime as ctime. Link/attribute changes must also invalidate capture.
    return same(left, right) and all(
        getattr(left, name, None) == getattr(right, name, None)
        for name in ("st_nlink", "st_file_attributes", "st_birthtime_ns")
    )


@contextmanager
def pinned(path: str, *, directory: bool):
    import msvcrt
    k = c.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    k.CreateFileW.restype = w.HANDLE
    k.GetFileInformationByHandleEx.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    k.CloseHandle.argtypes = [w.HANDLE]
    # Open reparse point itself. Omitting FILE_SHARE_DELETE pins the name;
    # regular snapshots also omit FILE_SHARE_WRITE to exclude concurrent writers.
    handle = k.CreateFileW(path, 0x80000000, 3 if directory else 1, None, 3, 0x02200000, None)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        info = (w.DWORD * 2)()
        if not k.GetFileInformationByHandleEx(handle, 9, c.byref(info), c.sizeof(info)):
            raise c.WinError(c.get_last_error())
        if info[0] & 0x400:
            raise ValueError("reparse boundary appeared during History capture")
        if bool(info[0] & 0x10) != directory:
            raise ValueError("resource type changed during History capture")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None  # ownership transferred to the CRT
        try:
            yield fd
        finally:
            os.close(fd)
    finally:
        if handle is not None:
            k.CloseHandle(handle)


@contextmanager
def ancestors(path: str):
    """Pin every ancestor before any leaf read or enumeration."""
    from contextlib import ExitStack
    absolute = Path(os.path.abspath(path))
    with ExitStack() as stack:
        for parent in reversed(absolute.parents):
            stack.enter_context(pinned(str(parent), directory=True))
        yield absolute


def read_file(path, expected, same):
    with ancestors(str(path)) as absolute, pinned(str(absolute), directory=False) as fd:
        opened = os.fstat(fd)
        if not _same_native(_PathStatView(opened), expected, same):
            raise ValueError("file replaced during History capture")
        chunks = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        if not _same_native(os.fstat(fd), opened, same) or not same(absolute.lstat(), expected):
            raise ValueError("file changed during History capture")
        return b"".join(chunks)


def directory_walk(root, *, expected, include_content, same, excluded):
    records = []
    def visit(path, relative, before):
        with pinned(str(path), directory=True) as fd:
            opened = os.fstat(fd)
            if not _same_native(_PathStatView(opened), before, same):
                raise ValueError("directory replaced during History capture")
            names = sorted(os.listdir(path))
            for name in names:
                item = path / name
                if excluded(str(item)):
                    raise ValueError("protected path appeared during History capture")
                info = os.lstat(item)
                if getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("reparse boundary appeared during History capture")
                rel = relative / name
                record = {"path": rel.as_posix(), "mode": info.st_mode & 0o7777,
                          "mtime_millis": info.st_mtime_ns // 1_000_000}
                if stat.S_ISDIR(info.st_mode):
                    record["type"] = "directory"
                    records.append(record)
                    visit(item, rel, info)
                elif stat.S_ISREG(info.st_mode):
                    record["type"] = "file"
                    # Pin/read even during validation-only passes; never accept
                    # a reparse point that appeared after the path metadata read.
                    with pinned(str(item), directory=False) as child:
                        child_opened = os.fstat(child)
                        if not _same_native(_PathStatView(child_opened), info, same):
                            raise ValueError("file replaced during History capture")
                        if include_content:
                            chunks = []
                            while chunk := os.read(child, 1024 * 1024):
                                chunks.append(chunk)
                            content = b"".join(chunks)
                            record.update(size=len(content), sha256=hashlib.sha256(content).hexdigest(),
                                          content=base64.b64encode(content).decode("ascii"), content_encoding="base64")
                        if not _same_native(os.fstat(child), child_opened, same):
                            raise ValueError("file changed during History capture")
                    records.append(record)
                else:
                    raise ValueError("unsupported entry during History capture")
                if not same(os.lstat(item), info):
                    raise ValueError("entry changed during History capture")
            if (sorted(os.listdir(path)) != names
                    or not _same_native(os.fstat(fd), opened, same)
                    or not same(os.lstat(path), before)):
                raise ValueError("directory changed during History capture")
    with ancestors(root) as absolute:
        visit(absolute, Path(), expected or os.lstat(absolute))
    return records

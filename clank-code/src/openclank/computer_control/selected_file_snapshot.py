"""Private bounded snapshot for an explicitly selected Files resource.

The returned descriptor is owned by the calling control session.  It is not a
model-facing Files capability, and must be closed on retirement or expiry.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import fcntl
import hashlib
import os
import re
import stat
import tempfile
import threading
from typing import Any, Awaitable, Callable, Mapping

MAX_BYTES = 16 * 1024 * 1024
MAX_SECONDS = 30
_FINGERPRINT = re.compile(r"sha256:([0-9a-f]{64}):([0-9]+)\Z")


@dataclass(frozen=True)
class SelectedSnapshot:
    fd: int
    size: int
    sha256: str
    object_tag: str
    filename: str


@dataclass
class NamedSnapshot:
    path: str
    directory: str
    dirfd: int
    name: str
    identity: tuple[int, int, int]
    size: int
    sha256: str
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def current(self) -> bool:
        fd = -1
        with self._lock:
            if self.dirfd < 0: return False
            try: dirfd = os.dup(self.dirfd)
            except OSError: return False
        try:
            held = os.fstat(dirfd)
            visible = os.lstat(self.directory)
            if not stat.S_ISDIR(visible.st_mode) or (held.st_dev, held.st_ino) != (visible.st_dev, visible.st_ino): return False
            fd = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino, info.st_ctime_ns) != self.identity
                    or info.st_size != self.size or info.st_mode & 0o222): return False
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 65536)
                if not chunk: break
                digest.update(chunk)
            after = os.fstat(fd)
            named_after = os.stat(self.name, dir_fd=dirfd, follow_symlinks=False)
            visible_after = os.lstat(self.directory)
            return (stat.S_ISDIR(visible_after.st_mode) and (held.st_dev, held.st_ino) == (visible_after.st_dev, visible_after.st_ino)
                    and (named_after.st_dev, named_after.st_ino, named_after.st_ctime_ns) == self.identity
                    and (after.st_dev, after.st_ino, after.st_ctime_ns) == self.identity
                    and after.st_size == self.size and digest.hexdigest() == self.sha256)
        except OSError:
            return False
        finally:
            if fd >= 0: os.close(fd)
            os.close(dirfd)

    def close(self) -> None:
        with self._lock:
            dirfd, self.dirfd = self.dirfd, -1
        if dirfd < 0: return
        try:
            info = os.stat(self.name, dir_fd=dirfd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == self.identity[:2]:
                os.unlink(self.name, dir_fd=dirfd)
        except OSError:
            pass
        try:
            held = os.fstat(dirfd)
            visible = os.lstat(self.directory)
            if (held.st_dev, held.st_ino) == (visible.st_dev, visible.st_ino): os.rmdir(self.directory)
        except OSError:
            pass
        try: os.close(dirfd)
        except OSError: pass


def materialize(snapshot: SelectedSnapshot) -> NamedSnapshot:
    """Create a unique short-lived read-only pathname from the sealed bytes."""
    name = snapshot.filename
    if (not name or name in (".", "..") or "/" in name or "\\" in name
            or "\0" in name or len(name.encode("utf-8")) > 240
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        raise ValueError("snapshot unavailable")
    directory = tempfile.mkdtemp(prefix="openclank-selected-")
    dirfd = -1
    out = -1
    try:
        dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400, dir_fd=dirfd)
        os.lseek(snapshot.fd, 0, os.SEEK_SET)
        digest = hashlib.sha256(); count = 0
        while True:
            chunk = os.read(snapshot.fd, 65536)
            if not chunk: break
            count += len(chunk); digest.update(chunk)
            view = memoryview(chunk)
            while view:
                n = os.write(out, view)
                if n <= 0: raise ValueError("snapshot unavailable")
                view = view[n:]
        os.fchmod(out, 0o400)
        info = os.fstat(out)
        if count != snapshot.size or digest.hexdigest() != snapshot.sha256 or not stat.S_ISREG(info.st_mode):
            raise ValueError("snapshot unavailable")
        result = NamedSnapshot(os.path.join(directory, name), directory, dirfd, name,
                               (info.st_dev, info.st_ino, info.st_ctime_ns), count, digest.hexdigest())
        os.close(out); out = -1; dirfd = -1
        if not result.current():
            result.close()
            raise ValueError("snapshot unavailable")
        return result
    finally:
        if out >= 0: os.close(out)
        if dirfd >= 0:
            try: os.unlink(name, dir_fd=dirfd)
            except OSError: pass
            os.close(dirfd)
            try: os.rmdir(directory)
            except OSError: pass


async def acquire(facade: Any, context: Any, agent_client: Any, resource_ref: str,
                  expected_revision: Mapping[str, Any],
                  current: Callable[[], Awaitable[bool]]) -> SelectedSnapshot:
    """Copy the authorized opened stream into an immutable sealed Linux memfd.

    Only a Files host fingerprint is accepted.  The caller must independently
    bind and recheck the original account, turn, helper and control epoch.
    """
    if (not isinstance(expected_revision, Mapping)
            or set(expected_revision) != {"kind", "value"}
            or expected_revision.get("kind") != "hostFingerprint"
            or not isinstance(expected_revision.get("value"), str)):
        raise ValueError("snapshot unavailable")
    match = _FINGERPRINT.fullmatch(expected_revision["value"])
    if match is None or len(match.group(2)) > 8:
        raise ValueError("snapshot unavailable")
    expected_size = int(match.group(2))
    if expected_size > MAX_BYTES:
        raise ValueError("snapshot unavailable")
    fd = -1
    async def authorized():
        if not facade._generation_current(context) or not await current():
            raise ValueError("snapshot unavailable")
        provider, ref = facade._provider_for_ref(context, resource_ref, capability="download")
        stat_provider, stat_ref = facade._provider_for_ref(context, resource_ref, capability="stat")
        if provider is not stat_provider or ref != stat_ref or ref.provider != "host":
            raise ValueError("snapshot unavailable")
        return provider, ref
    try:
        async with asyncio.timeout(MAX_SECONDS):
            _, ref = await authorized()
            # The facade only resolves the chosen opaque reference.  All bytes
            # and object observations use the independently scoped Agent client.
            from src.openclank.files_host_provider import _path
            path = _path(ref.origin_id)
            caps = await agent_client.request("capabilities", path, {})
            if "stat_object_identity" not in (caps.get("operations") or []):
                raise ValueError("snapshot unavailable")
            async def stat():
                response = await agent_client.request("stat", path, {"include_fingerprint": False, "object_identity": True})
                data = response.get("data") if isinstance(response, Mapping) else None
                if not isinstance(data, Mapping): raise ValueError("snapshot unavailable")
                tag = data.get("object_tag")
                if (not isinstance(data.get("kind"), str) or data["kind"].lower() != "file" or type(data.get("size")) is not int
                        or data["size"] != expected_size or not isinstance(tag, str)
                        or re.fullmatch(r"[0-9a-f]{64}", tag) is None):
                    raise ValueError("snapshot unavailable")
                return tag
            before_tag = await stat()
            await authorized()
            opened = await agent_client.open_handle(path)
            handle = opened.get("handle") if isinstance(opened, Mapping) else None
            if (not isinstance(handle, dict) or type(opened.get("size")) is not int
                    or opened["size"] != expected_size or opened.get("object_tag") != before_tag):
                raise ValueError("snapshot unavailable")
            fd = os.memfd_create("openclank-selected-file", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            digest = hashlib.sha256()
            count = 0
            async for chunk in agent_client.stream_read_handle(handle, offset=0, length=expected_size + 1):
                if not isinstance(chunk, bytes) or not chunk:
                    raise ValueError("snapshot unavailable")
                count += len(chunk)
                if count > expected_size:
                    raise ValueError("snapshot unavailable")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise ValueError("snapshot unavailable")
                    view = view[written:]
                await authorized()
            if count != expected_size or digest.hexdigest() != match.group(1):
                raise ValueError("snapshot unavailable")
            if await stat() != before_tag:
                raise ValueError("snapshot unavailable")
            await authorized()
            seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
            if fcntl.fcntl(fd, fcntl.F_GET_SEALS) & seals != seals or os.fstat(fd).st_size != count:
                raise ValueError("snapshot unavailable")
            os.lseek(fd, 0, os.SEEK_SET)
            verify = hashlib.sha256()
            while True:
                data = os.read(fd, 65536)
                if not data:
                    break
                verify.update(data)
            if verify.hexdigest() != match.group(1):
                raise ValueError("snapshot unavailable")
            await authorized()
            os.lseek(fd, 0, os.SEEK_SET)
            result = SelectedSnapshot(fd, count, digest.hexdigest(), before_tag, os.path.basename(path))
            fd = -1
            return result
    finally:
        if fd >= 0:
            os.close(fd)

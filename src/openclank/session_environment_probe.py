"""Bounded read-only proof for the optional non-Git environment reminder."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import stat

_MAX_ANCESTORS = 128
_NOT_REPOSITORY = b'fatal: not a git repository (or any of the parent directories): .git'


def _identity(value):
    return (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))


class _PinnedAncestry:
    """Retain exact directory handles while proving that no ancestor has .git."""

    def __init__(self, cwd):
        self.entries = []
        self.cwd = cwd
        if (not isinstance(cwd, str) or not os.path.isabs(cwd) or len(cwd) > 4096
                or not getattr(os, 'O_DIRECTORY', 0) or not getattr(os, 'O_NOFOLLOW', 0)
                or os.stat not in os.supports_dir_fd):
            raise ValueError('directory proof unavailable')
        if str(Path(cwd).resolve(strict=True)) != cwd:
            raise ValueError('directory proof unavailable')
        path = Path(cwd)
        try:
            for _ in range(_MAX_ANCESTORS):
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0))
                try:
                    identity = _identity(os.fstat(fd))
                except BaseException:
                    os.close(fd)
                    raise
                self.entries.append((str(path), fd, identity))
                if path.parent == path:
                    break
                path = path.parent
            else:
                raise ValueError('directory proof too deep')
            if not self.current():
                raise ValueError('directory proof changed')
        except BaseException:
            self.close()
            raise

    def current(self):
        try:
            if str(Path(self.cwd).resolve(strict=True)) != self.cwd:
                return False
            for index, (path, fd, expected) in enumerate(self.entries):
                if (expected[2] != stat.S_IFDIR or _identity(os.fstat(fd)) != expected
                        or _identity(os.stat(path, follow_symlinks=False)) != expected):
                    return False
                parent = self.entries[index + 1][2] if index + 1 < len(self.entries) else expected
                if _identity(os.stat('..', dir_fd=fd, follow_symlinks=False)) != parent:
                    return False
                try:
                    os.stat('.git', dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    return False
            return bool(self.entries)
        except (OSError, RuntimeError, ValueError):
            return False

    def close(self):
        for _, fd, _ in self.entries:
            try:
                os.close(fd)
            except OSError:
                pass
        self.entries.clear()


async def _read_bounded(stream, limit):
    result = bytearray()
    while True:
        chunk = await stream.read(min(1024, limit + 1 - len(result)))
        if not chunk:
            return bytes(result)
        result.extend(chunk)
        if len(result) > limit:
            raise ValueError('Git output unavailable')


async def capture_non_git(cwd: str, git: str):
    """Return pinned absence evidence, or None when it cannot be proved.

    The caller rechecks and closes it. This is not a promise about later
    filesystem state and grants no authority. Unknown errors remain unavailable.
    """
    ancestry = None
    proc = None
    try:
        ancestry = _PinnedAncestry(cwd)
        env = {'PATH': os.path.dirname(git), 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_DISCOVERY_ACROSS_FILESYSTEM': '1',
               'LC_ALL': 'C', 'LANG': 'C'}
        proc = await asyncio.create_subprocess_exec(git, '-C', cwd, 'rev-parse', '--show-toplevel',
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
        stdout, stderr, code = await asyncio.wait_for(asyncio.gather(
            _read_bounded(proc.stdout, 4096), _read_bounded(proc.stderr, 256), proc.wait()), timeout=1.0)
        if (code == 128 and stdout == b'' and stderr in (_NOT_REPOSITORY + b'\n', _NOT_REPOSITORY + b'\r\n')
                and ancestry.current()):
            captured=ancestry
            ancestry=None  # The caller retains and closes the exact directory handles.
            return captured
        return None
    except asyncio.CancelledError:
        raise
    except (OSError, RuntimeError, ValueError, asyncio.TimeoutError):
        return None
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except (OSError, asyncio.TimeoutError):
                pass
        if ancestry is not None:
            ancestry.close()

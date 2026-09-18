"""Cross-process commit exclusion for a loose Copal installation.

The lock lives outside owner/workspace trees so a rename or reset cannot move
the lock away from a concurrent writer. It is held only for backend operations,
never across history IPC. External filesystem writers do not take this lock.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import stat


@contextmanager
def copal_commit_lock(data_dir: Path):
    path = Path(data_dir) / ".copal-commit.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Copal commit lock is not a regular file")
        if os.name == "nt":
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

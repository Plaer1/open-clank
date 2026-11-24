"""Short no-follow observation of an existing workspace capture folder."""
from contextlib import contextmanager
import os
import stat

@contextmanager
def capture_layout_directory(root, selector):
    if os.name != 'posix' or selector not in ('movs', 'pics') or not all(hasattr(os, n) for n in ('O_NOFOLLOW', 'O_DIRECTORY', 'O_CLOEXEC')):
        raise OSError('capture layout unavailable')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fds = []
    try:
        fd = os.open(root, flags); fds.append(fd)
        root_identity = os.fstat(fd)
        edges = []
        for name in ('.clanker', 'captures', selector):
            child = os.open(name, flags, dir_fd=fd)
            fds.append(child)
            edges.append((fd, name, child))
            fd = child
        def current():
            try:
                def same(a, b): return stat.S_ISDIR(a.st_mode) and stat.S_ISDIR(b.st_mode) and (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
                return same(root_identity, os.stat(root, follow_symlinks=False)) and all(
                    same(os.fstat(child), os.stat(name, dir_fd=parent, follow_symlinks=False)) for parent, name, child in edges)
            except OSError:
                return False
        if not current(): raise OSError('capture layout changed')
        yield os.path.join(root, '.clanker', 'captures', selector), current
    finally:
        for fd in reversed(fds): os.close(fd)

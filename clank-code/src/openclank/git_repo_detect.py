"""Is this Files folder inside a Git repository? A filesystem fact, not a Git command.

Walks from the folder up to (and including) its registered location root looking for the nearest ``.git``: a directory
means a repository, a file whose first line is ``gitdir: <path>`` means a linked worktree (an agent worktree is one).
The walk never leaves the location root, never reads more than 4 KiB of a ``.git`` file, and returns only the
relative position of the repository root (never absolute paths)."""
from __future__ import annotations
import os
import stat

MAX_DEPTH = 64


def detect_repo(folder: str, location_root: str) -> dict:
    folder, root = os.path.normpath(folder), os.path.normpath(location_root)
    if not os.path.isabs(folder) or not os.path.isabs(root) or os.path.commonpath((folder, root)) != root:
        return {"state": "unavailable"}
    here = folder
    for _ in range(MAX_DEPTH):
        marker = os.path.join(here, ".git")
        try:
            info = os.lstat(marker)
        except FileNotFoundError:
            info = None
        except OSError:
            return {"state": "unavailable"}
        if info is not None:
            rel = os.path.relpath(here, folder)
            if stat.S_ISDIR(info.st_mode):
                return {"state": "repository", "kind": "repository", "root_relative": rel}
            if stat.S_ISREG(info.st_mode):
                try:
                    with open(marker, "rb") as handle:
                        first = handle.read(4096).split(b"\n", 1)[0]
                except OSError:
                    return {"state": "unavailable"}
                if first.startswith(b"gitdir:"):
                    return {"state": "repository", "kind": "worktree", "root_relative": rel}
            # a symlink or malformed .git is not trusted as a repository marker; keep looking upward is wrong, stop here
            return {"state": "none"}
        if here == root:
            break
        here = os.path.dirname(here)
    return {"state": "none"}

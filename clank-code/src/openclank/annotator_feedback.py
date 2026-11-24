"""Private, append-only user feedback for the server's source checkout."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


class FeedbackConflict(Exception):
    """A submission ID was already used for different feedback."""


class FeedbackUnavailable(Exception):
    """The private feedback file cannot safely be read or updated."""


_MAX_STORE_BYTES = 8 * 1024 * 1024
_STORE_NAME = "chores"


def _ordinary_path(path: Path, kind) -> bool:
    info = path.lstat()
    return kind(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def resolve_source_checkout(default: Path, configured: str | None) -> Path | None:
    """Only an explicit server setting or the actual source install may bind."""
    try:
        candidate = Path(configured) if configured else default
        if not candidate.is_absolute():
            return None
        root = candidate.resolve(strict=True)
        if (root / "clank-code").is_dir():
            source = root / "clank-code"
        elif root.name == "clank-code":
            source, root = root, root.parent
        else:
            source = root
        if source.is_symlink() or not all(
            _ordinary_path(source / relative, stat.S_ISREG)
            for relative in ("app.py", "pyproject.toml", "src/openclank/official_docs.py")
        ):
            return None
        if configured:
            if not _ordinary_path(root / ".clanker", stat.S_ISDIR):
                return None
        else:
            marker = root / ".git"
            if not (_ordinary_path(marker, stat.S_ISDIR) or _ordinary_path(marker, stat.S_ISREG)):
                return None
        return root
    except (OSError, RuntimeError, ValueError):
        return None


def _open_regular(path: Path, flags: int) -> int:
    if not _ordinary_path(path, stat.S_ISREG):
        raise FeedbackUnavailable
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        info = os.fstat(fd)
        named = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not os.path.samestat(info, named):
            raise FeedbackUnavailable
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def _submission_lock(directory: Path):
    lock_path = directory / ".chores.lock"
    try:
        created = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(created)
    with os.fdopen(_open_regular(lock_path, os.O_RDWR), "r+b") as handle:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
        # Closing the handle releases this process's lock, including on error.


def _read_feedback(path: Path) -> dict:
    try:
        fd = _open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as handle:
        content = handle.read(_MAX_STORE_BYTES + 1)
    if len(content) > _MAX_STORE_BYTES:
        raise FeedbackUnavailable
    try:
        pages = json.loads(content)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise FeedbackUnavailable from exc
    if not isinstance(pages, dict):
        raise FeedbackUnavailable
    return pages


def _write_feedback(path: Path, pages: dict) -> None:
    data = (json.dumps(pages, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > _MAX_STORE_BYTES:
        raise FeedbackUnavailable
    fd, staged = tempfile.mkstemp(prefix=".chores-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.parent.resolve(strict=True) != path.parent:
            raise FeedbackUnavailable
        os.replace(staged, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        Path(staged).unlink(missing_ok=True)


def record_feedback(root: Path, *, account: str, submission_id: str, page: str, element: str, comment: str) -> str:
    directory = root / ".clanker"
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        if not _ordinary_path(directory, stat.S_ISDIR):
            raise FeedbackUnavailable
        path = directory / _STORE_NAME
        digest = hashlib.sha256(f"{account}\0{submission_id}".encode()).digest()[:16]
        record_id = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        with _submission_lock(directory):
            pages = _read_feedback(path)
            for existing_page, elements in pages.items():
                if not isinstance(elements, dict):
                    raise FeedbackUnavailable
                for existing_element, entries in elements.items():
                    if not isinstance(entries, list):
                        raise FeedbackUnavailable
                    for entry in entries:
                        if not isinstance(entry, dict):
                            raise FeedbackUnavailable
                        if entry.get("id") != record_id:
                            continue
                        if (existing_page, existing_element, entry.get("feedback")) != (page, element, comment):
                            raise FeedbackConflict
                        return "already_recorded"
            pages.setdefault(page, {}).setdefault(element, []).append({
                "id": record_id,
                "feedback": comment,
            })
            _write_feedback(path, pages)
            return "recorded"
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise FeedbackUnavailable from exc

"""Private retained personal-file snapshot; this object grants no write authority."""
from __future__ import annotations

import hashlib
import io
import os
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from services.memory.skill_lifecycle import locked
from src.memery_rag import MAX_FILE_BYTES


def _fingerprint(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _directory_identity(st):
    return (st.st_dev, st.st_ino)


@dataclass(frozen=True)
class _PersonalDocumentSnapshot:
    """A snapshot valid only while the worker retains its descriptors and lock."""
    _fd: int
    _directory_fd: int
    _root_fd: int
    _path: Path
    _directory: Path
    _root: Path
    _fingerprint: tuple
    _directory_identity: tuple
    _root_identity: tuple
    _thread: int
    raw_sha256: str
    raw_byte_length: int
    extractor_version: str
    text: str
    page_ends: tuple[int, ...]
    _active: bool = True

    def assert_current(self):
        if not self._active:
            raise ValueError('personal document source unavailable')
        actual = os.fstat(self._fd)
        child = os.stat(self._path.name, dir_fd=self._directory_fd, follow_symlinks=False)
        directory = os.stat(self._directory, follow_symlinks=False)
        root = os.stat(self._root, follow_symlinks=False)
        if (not stat.S_ISREG(actual.st_mode) or not stat.S_ISREG(child.st_mode)
                or not stat.S_ISDIR(directory.st_mode) or not stat.S_ISDIR(root.st_mode)
                or _fingerprint(actual) != self._fingerprint or _fingerprint(child) != self._fingerprint
                or _directory_identity(os.fstat(self._directory_fd)) != self._directory_identity
                or _directory_identity(directory) != self._directory_identity
                or _directory_identity(os.fstat(self._root_fd)) != self._root_identity
                or _directory_identity(root) != self._root_identity):
            raise ValueError('personal document source changed')
        return True

    def _close(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError('personal document source owner changed')
        object.__setattr__(self, '_active', False)


def retained_document_text(raw, suffix):
    """Use the same bounded text/PDF projection for an authorized retained source."""
    page_ends = []
    if suffix.lower() == '.pdf':
        # The existing pypdf extraction owner reads the retained bytes,
        # never a reopened pathname. Bound cumulative output per page.
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            if len(reader.pages) > 1024:
                raise ValueError('personal PDF source unavailable')
            parts = []
            extracted_bytes = 0
            for page in reader.pages:
                part = page.extract_text() or ''
                if type(part) is not str:
                    raise ValueError('personal PDF source unavailable')
                extracted_bytes += len(part.encode('utf-8'))
                if extracted_bytes > 256 * 1024:
                    raise ValueError('personal PDF source unavailable')
                page_ends.append(extracted_bytes)
                parts.append(part)
            text = ''.join(parts)
        except Exception as exc:
            raise ValueError('personal PDF source unavailable') from exc
        extractor_version = 'pypdf-text.v1'
    else:
        text = raw.decode('utf-8', errors='replace')
        extractor_version = 'utf8-replacement.v1'
    if not text.strip() or len(text.encode('utf-8')) > 256 * 1024:
        raise ValueError('personal document source unavailable')
    return text, extractor_version, page_ends


@contextmanager
def _retained_personal_document(owner, path, upload_root):
    """Capture one existing text upload under the established owner file lock.

    The caller must separately hold actual named-account and route lifecycle
    authority. The snapshot itself is not a permission or publication grant.
    """
    if type(owner) is not str or not owner or owner != owner.strip():
        raise ValueError('personal document source unavailable')
    if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY'):
        raise ValueError('personal document descriptor unavailable')
    from routes.personal_routes import _personal_upload_dir_for_owner
    root = Path(upload_root).resolve(strict=True)
    directory = Path(_personal_upload_dir_for_owner(owner, create=False))
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.parent != directory or directory.parent != root:
        raise ValueError('personal document source unavailable')
    lock_target = str(root / '.owner-lifecycle.json')
    with locked(lock_target):
        flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | os.O_NOFOLLOW
        root_fd = os.open(root, flags | os.O_DIRECTORY)
        directory_fd = fd = None
        snapshot = None
        try:
            directory_fd = os.open(directory.name, flags | os.O_DIRECTORY, dir_fd=root_fd)
            fd = os.open(candidate.name, flags, dir_fd=directory_fd)
            before = os.fstat(fd)
            root_id = _directory_identity(os.fstat(root_fd))
            directory_id = _directory_identity(os.fstat(directory_fd))
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                raise ValueError('personal document source unavailable')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES or len(raw) != before.st_size:
                raise ValueError('personal document source unavailable')
            text, extractor_version, page_ends = retained_document_text(raw, candidate.suffix)
            os.lseek(fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(fd), 'rb') as stream:
                after_raw = stream.read(MAX_FILE_BYTES + 1)
            snapshot = _PersonalDocumentSnapshot(fd, directory_fd, root_fd, candidate, directory, root,
                _fingerprint(before), directory_id, root_id, threading.get_ident(),
                hashlib.sha256(raw).hexdigest(), len(raw), extractor_version, text, tuple(page_ends))
            snapshot.assert_current()
            if hashlib.sha256(after_raw).digest() != hashlib.sha256(raw).digest():
                raise ValueError('personal document source changed')
            yield snapshot
        finally:
            if snapshot is not None:
                snapshot._close()
            if fd is not None: os.close(fd)
            if directory_fd is not None: os.close(directory_fd)
            os.close(root_fd)

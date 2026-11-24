"""Explicit offline *copy* preparation for WB16 publication metadata.

No startup hook or implicit live database migration. The caller supplies an
already-stopped source copy and a new output path, and owns activation.
"""
from __future__ import annotations
import os
import sqlite3
from pathlib import Path


def prepare_published_target_copy(*, source_copy: str, destination: str) -> None:
    source_path = Path(source_copy)
    if source_path.is_symlink(): raise ValueError('offline publication copy unavailable')
    source = source_path.resolve(strict=True)
    target = Path(destination).absolute()
    if (not source.is_file() or target.exists() or target.is_symlink()
            or source == target.resolve(strict=False)):
        raise ValueError('offline publication copy unavailable')
    # A WAL/journal companion means the caller has not supplied an offline,
    # settled copy. This helper never changes or activates the source.
    if any(Path(str(source)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
        raise ValueError('source copy is not settled')
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    src = out = None
    try:
        src = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)
        out = sqlite3.connect(str(target))
        src.backup(out)
        out.execute('PRAGMA foreign_keys=ON')
        columns = {row[1] for row in out.execute('PRAGMA table_info(published_files)')}
        if not columns or 'owner_subject_id' in columns or 'target_ciphertext' in columns:
            raise ValueError('source publication schema is not the legacy profile')
        out.execute('BEGIN IMMEDIATE')
        out.execute('ALTER TABLE published_files ADD COLUMN owner_subject_id VARCHAR')
        out.execute('ALTER TABLE published_files ADD COLUMN target_ciphertext TEXT')
        out.execute('CREATE INDEX ix_published_files_owner_subject_id ON published_files(owner_subject_id)')
        out.commit()
        if out.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or out.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('publication copy validation failed')
    except BaseException:
        if out is not None: out.rollback()
        raise
    finally:
        if out is not None: out.close()
        if src is not None: src.close()

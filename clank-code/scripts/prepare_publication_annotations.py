#!/usr/bin/env python3
"""Prepare optional publication annotations on a separate, stopped SQLite copy.

Stop all writers and retain a separate backup. This never writes the source or
replaces an existing destination; applying the copy is an independent action.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sqlite3


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path); parser.add_argument('backup',type=Path); parser.add_argument('destination',type=Path)
    parser.add_argument('--writers-stopped',action='store_true',required=True)
    args=parser.parse_args()
    source=args.source.resolve(strict=True); backup=args.backup.resolve(); destination=args.destination.resolve()
    if len({source,backup,destination}) != 3 or not source.is_file() or backup.exists() or destination.exists():
        raise SystemExit('Source and two new output paths must be distinct')
    src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    try:
        before=src.execute('PRAGMA data_version').fetchone()[0]
        if src.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Source integrity unavailable')
        for name in ('published_files','published_file_grants','file_policy_meta','file_policy_bindings'):
            if not src.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",(name,)).fetchone(): raise ValueError('Source profile unavailable')
        cols={row[1] for row in src.execute('PRAGMA table_info(published_files)')}
        if {'annotations_ciphertext','annotations_revision'} & cols: raise ValueError('Annotation profile already present or partial')
        fd=os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        backup_db=sqlite3.connect(backup)
        try:src.backup(backup_db)
        finally:backup_db.close()
        if src.execute('PRAGMA data_version').fetchone()[0]!=before: raise ValueError('Source changed during copy')
        fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        saved=sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True);dst=sqlite3.connect(destination)
        try:
            saved.backup(dst)
            with dst:
                dst.execute('ALTER TABLE published_files ADD COLUMN annotations_ciphertext TEXT')
                dst.execute('ALTER TABLE published_files ADD COLUMN annotations_revision INTEGER NOT NULL DEFAULT 0')
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Prepared copy integrity unavailable')
        finally:dst.close();saved.close()
    finally:src.close()
    print('Prepared a separate annotation-profile copy; source was not changed.')


if __name__=='__main__':main()

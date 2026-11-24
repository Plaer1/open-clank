#!/usr/bin/env python3
"""Prepare a stopped-copy TUI run receipt profile; never mutate the source."""
from __future__ import annotations
import argparse, os, sqlite3
from pathlib import Path

COLUMNS=('tui_profile','tui_account_id','tui_root_id','tui_run_id')
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('backup',type=Path);parser.add_argument('destination',type=Path)
    parser.add_argument('--writers-stopped',required=True,action='store_true');args=parser.parse_args()
    source=args.source.resolve(strict=True);backup=args.backup.resolve();destination=args.destination.resolve()
    if len({source,backup,destination})!=3 or not source.is_file() or backup.exists() or destination.exists():raise SystemExit('source, new backup and new destination must be distinct')
    src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    try:
        version=src.execute('PRAGMA data_version').fetchone()[0]
        if src.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('source integrity unavailable')
        columns={r[1] for r in src.execute('PRAGMA table_info(tui_turn_submissions)')}
        if not {'id','owner','session_id','idempotency_key','request_hash','state','error_code'}<=columns or any(c in columns for c in COLUMNS):raise ValueError('source profile mismatch')
        fd=os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        save=sqlite3.connect(backup)
        try:src.backup(save)
        finally:save.close()
        if src.execute('PRAGMA data_version').fetchone()[0]!=version:raise ValueError('source changed during copy')
        fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        saved=sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True);dst=sqlite3.connect(destination)
        try:
            saved.backup(dst)
            with dst:
                for name in COLUMNS:dst.execute(f'ALTER TABLE tui_turn_submissions ADD COLUMN {name} TEXT')
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('copy integrity unavailable')
        finally:dst.close();saved.close()
    finally:src.close()
    print('Prepared a separate TUI run receipt profile copy; source was not changed.')
if __name__=='__main__':main()

#!/usr/bin/env python3
"""Make backup and shadow copies; add the optional timing column to shadow only."""
import argparse
import os
import sqlite3
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('backup',type=Path);parser.add_argument('shadow',type=Path)
    args=parser.parse_args(); paths=(args.source,args.backup,args.shadow)
    if any(not p.is_absolute() or p.is_symlink() for p in paths): raise ValueError('absolute non-symlink paths required')
    source=args.source.resolve(strict=True);backup=args.backup.resolve();shadow=args.shadow.resolve()
    if len({source,backup,shadow})!=3 or not source.is_file() or backup.exists() or shadow.exists(): raise ValueError('existing source and new distinct destinations required')
    before=source.stat()
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src:
        src.execute('PRAGMA query_only=ON')
        if src.execute('PRAGMA integrity_check').fetchone()!=('ok',): raise ValueError('archive integrity unavailable')
        cols=list(src.execute('PRAGMA table_info(conversation_parts)'))
        required={'owner','chat_id','actor_id','message_id','part_id','revision','content','content_hash'}
        if not required.issubset({r[1] for r in cols}) or any(r[1]=='qualified_tool_json' for r in cols): raise ValueError('archive profile unavailable')
        version=src.execute('PRAGMA data_version').fetchone()[0]
        for path in (backup,shadow):
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600);os.close(fd)
        with sqlite3.connect(backup) as dest: src.backup(dest)
        if src.execute('PRAGMA data_version').fetchone()[0]!=version or (source.stat().st_dev,source.stat().st_ino)!=(before.st_dev,before.st_ino): raise ValueError('archive changed during backup')
    with sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True) as saved, sqlite3.connect(shadow) as dest:
        if saved.execute('PRAGMA integrity_check').fetchone()!=('ok',) or list(saved.execute('PRAGMA table_info(conversation_parts)'))!=cols: raise ValueError('captured backup profile changed')
        saved.backup(dest)
        dest.execute('ALTER TABLE conversation_parts ADD COLUMN qualified_tool_json TEXT')
        if dest.execute('PRAGMA integrity_check').fetchone()!=('ok',): raise ValueError('shadow integrity unavailable')
        actual=list(dest.execute('PRAGMA table_info(conversation_parts)'))
        if actual[:-1]!=cols or actual[-1][1:3]!=('qualified_tool_json','TEXT'): raise ValueError('shadow profile mismatch')
        dest.commit()
    print('Prepared backup and optional timing shadow; cutover requires separate authorization.')

if __name__=='__main__': main()

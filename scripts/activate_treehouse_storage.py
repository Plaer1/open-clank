#!/usr/bin/env python3
"""Explicit offline, backed-up schema-only TreeHouse activation; never retires courses."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.openclank.treehouse_storage_activation import preview, apply, verify, restore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['preview', 'apply', 'verify', 'restore'])
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--operation-id', default='treehouse-global-schema-v1')
    parser.add_argument('--preview', type=Path)
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--writers-stopped', action='store_true', help='Explicit operator confirmation; does not stop any processes')
    args = parser.parse_args()
    # Refuse an occupied receipt destination before an activation can commit.
    if args.output and args.output.exists(): parser.error('Output already exists; never overwrite it')
    output = None
    try:
        if args.output: output = os.fdopen(os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w')
        if args.mode == 'preview': result = preview(args.db, args.operation_id)
        elif args.mode == 'verify': result = verify(args.db)
        elif args.mode == 'apply':
            if not args.preview or not args.backup: parser.error('apply requires --preview and --backup')
            result = apply(args.db, json.loads(args.preview.read_text()), args.backup, writers_stopped=args.writers_stopped)
        else:
            if not args.receipt or not args.backup: parser.error('restore requires --receipt and --backup')
            result = restore(args.db, args.backup, json.loads(args.receipt.read_text()), writers_stopped=args.writers_stopped)
        if output: json.dump(result, output, indent=2)
        else: print(json.dumps(result, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')
    finally:
        if output: output.close()


if __name__ == '__main__': main()

#!/usr/bin/env python3
"""Plan, apply, or roll back a Clanker corpus migration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.clanker_layout_migration import (
    DEFAULT_MANIFEST,
    LayoutMigrationError,
    apply_manifest,
    create_manifest,
    rollback_manifest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="migrate_clanker_layout")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="create a manifest without moving files")
    plan.add_argument("source", type=Path)
    plan.add_argument("destination", type=Path)
    plan.add_argument("--manifest", type=Path, default=Path(DEFAULT_MANIFEST))
    plan.add_argument("--json", action="store_true")
    inspect = sub.add_parser("inspect", help="inspect named roots without writes")
    inspect.add_argument("source", type=Path)
    inspect.add_argument("destination", type=Path)
    inspect.add_argument("--json", action="store_true")
    apply = sub.add_parser("apply", help="preflight and apply a manifest")
    apply.add_argument("--manifest", type=Path, default=Path(DEFAULT_MANIFEST))
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--json", action="store_true")
    rollback = sub.add_parser("rollback", help="verify and restore an applied manifest")
    rollback.add_argument("--manifest", type=Path, default=Path(DEFAULT_MANIFEST))
    rollback.add_argument("--json", action="store_true")
    verify = sub.add_parser("verify", help="verify destination files against a manifest")
    verify.add_argument("--manifest", type=Path, default=Path(DEFAULT_MANIFEST))
    verify.add_argument("--json", action="store_true")
    finalize = sub.add_parser("finalize", help="remove only verified duplicate source entries")
    finalize.add_argument("--manifest", type=Path, default=Path(DEFAULT_MANIFEST))
    finalize.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            manifest = create_manifest(args.source, args.destination, args.manifest)
            result = {
                "status": manifest["status"],
                "plan_hash": manifest["plan_hash"],
                "manifest": str(args.manifest.expanduser().resolve()),
                "file_count": len(manifest["entries"]),
            }
        elif args.command == "inspect":
            from src.clanker_layout_migration import inspect_roots
            result = inspect_roots(args.source, args.destination)
        elif args.command == "apply":
            result = apply_manifest(args.manifest, dry_run=args.dry_run)
        elif args.command == "verify":
            from src.clanker_layout_migration import verify_manifest
            result = verify_manifest(args.manifest)
        elif args.command == "finalize":
            from src.clanker_layout_migration import finalize_manifest
            result = finalize_manifest(args.manifest)
        else:
            result = rollback_manifest(args.manifest)
    except LayoutMigrationError as exc:
        print(f"migration blocked: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"{result.get('status')}: {result.get('file_count', 0)} files ({result.get('plan_hash', '')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

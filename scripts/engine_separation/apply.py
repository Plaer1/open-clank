#!/usr/bin/env python3
"""Apply rename_table.json to an engine tree.

usage: apply.py <engine-tree> [--table rename_table.json] [--dry-run]

Deterministic and idempotent: a second run changes nothing. Standard library only;
reads and writes only below <engine-tree>. Run after every upstream import.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

DEFAULT_TABLE = Path(__file__).resolve().with_name("rename_table.json")


class Table:
    def __init__(self, path: Path):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") != 1:
            raise SystemExit(f"unsupported rename table version in {path}")
        self.skip_dirs = set(raw["skip_dirs"])
        self.skip_globs = [entry["glob"] for entry in raw["skip_files"]]
        self.kept = [re.compile(entry["pattern"]) for entry in raw["protocol_kept"]]
        self.rules = [(re.compile(r["pattern"]), r["replace"]) for r in raw["rules"]]
        self.path_rules = [(re.compile(r["pattern"]), r["replace"]) for r in raw["path_rules"]]
        self.forbidden = [(r["class"], re.compile(r["pattern"])) for r in raw["forbidden"]]

    def skipped(self, rel: str) -> bool:
        return any(fnmatch.fnmatchcase(rel, glob) for glob in self.skip_globs)

    def mask(self, text: str) -> tuple[str, list[str]]:
        """Hide every protocol-kept span behind a NUL-delimited placeholder."""
        held: list[str] = []

        def hold(match: re.Match) -> str:
            held.append(match.group(0))
            return f"\x00{len(held) - 1}\x00"

        for pattern in self.kept:
            text = pattern.sub(hold, text)
        return text, held

    @staticmethod
    def unmask(text: str, held: list[str]) -> str:
        # Later placeholders can sit inside earlier held spans, so restore newest first.
        for index in range(len(held) - 1, -1, -1):
            text = text.replace(f"\x00{index}\x00", held[index])
        return text

    def rename_text(self, text: str) -> str:
        masked, held = self.mask(text)
        for pattern, replace in self.rules:
            masked = pattern.sub(replace, masked)
        return self.unmask(masked, held)

    def rename_name(self, name: str) -> str:
        for pattern, replace in self.path_rules:
            name = pattern.sub(replace, name)
        return name


def walk(root: Path, table: Table):
    """Yield (relative posix path, is_dir) bottom-up, without skipped directories."""
    for current, dirs, files in os.walk(root, topdown=True):
        dirs[:] = sorted(d for d in dirs if d not in table.skip_dirs)
        base = Path(current)
        for name in sorted(files):
            yield (base / name).relative_to(root).as_posix(), False
    for current, dirs, _files in os.walk(root, topdown=False):
        base = Path(current)
        if any(part in table.skip_dirs for part in base.relative_to(root).parts):
            continue
        for name in sorted(dirs):
            if name not in table.skip_dirs:
                yield (base / name).relative_to(root).as_posix(), True


def read_text(path: Path) -> str | None:
    """Text of a file, or None for binary / non-UTF-8 content."""
    data = path.read_bytes()
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def apply(root: Path, table: Table, dry_run: bool = False) -> tuple[int, int]:
    edited = renamed = 0
    entries = list(walk(root, table))
    for rel, is_dir in entries:
        if is_dir or table.skipped(rel):
            continue
        path = root / rel
        if path.is_symlink():
            continue
        text = read_text(path)
        if text is None:
            continue
        new = table.rename_text(text)
        if new != text:
            edited += 1
            if not dry_run:
                mode = path.stat().st_mode
                path.write_bytes(new.encode("utf-8"))
                os.chmod(path, mode)
    # Names: files first, then directories deepest first, so parents stay valid.
    files = [rel for rel, is_dir in entries if not is_dir]
    dirs = sorted((rel for rel, is_dir in entries if is_dir), key=lambda r: -r.count("/"))
    for rel in files + dirs:
        if table.skipped(rel) or table.skipped(rel + "/x"):
            continue
        path = root / rel
        new_name = table.rename_name(path.name)
        if new_name == path.name:
            continue
        target = path.with_name(new_name)
        if target.exists():
            raise SystemExit(f"rename target already exists: {target.relative_to(root)}")
        renamed += 1
        if not dry_run:
            path.rename(target)
    return edited, renamed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tree", type=Path)
    parser.add_argument("--table", type=Path, default=DEFAULT_TABLE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.tree.resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")
    edited, renamed = apply(root, Table(args.table), args.dry_run)
    print(f"{'would edit' if args.dry_run else 'edited'} {edited} files, "
          f"{'would rename' if args.dry_run else 'renamed'} {renamed} paths")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

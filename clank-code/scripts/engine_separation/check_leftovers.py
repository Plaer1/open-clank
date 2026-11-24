#!/usr/bin/env python3
"""Fail when an engine tree still holds a name shared with a real MiMo Code install.

usage: check_leftovers.py <engine-tree> [--table rename_table.json]

Prints `file:line: class: text` for every forbidden name outside the allow list
(the table's skip_files and protocol_kept entries) and exits 1; exits 0 when clean.
Standard library only; reads only below <engine-tree>.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply import DEFAULT_TABLE, Table, read_text, walk  # noqa: E402


def leftovers(root: Path, table: Table) -> list[str]:
    found: list[str] = []
    for rel, is_dir in walk(root, table):
        if table.skipped(rel) or table.skipped(rel + "/x"):
            continue
        name = rel.rsplit("/", 1)[-1]
        if table.rename_name(name) != name:
            found.append(f"{rel}:0: path name: {name}")
        if is_dir or (root / rel).is_symlink():
            continue
        text = read_text(root / rel)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            masked, _held = table.mask(line)
            for kind, pattern in table.forbidden:
                match = pattern.search(masked)
                if match:
                    found.append(f"{rel}:{number}: {kind}: {line.strip()[:160]}")
                    break
    return found


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tree", type=Path)
    parser.add_argument("--table", type=Path, default=DEFAULT_TABLE)
    args = parser.parse_args(argv)
    root = args.tree.resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")
    found = leftovers(root, Table(args.table))
    for line in found:
        print(line)
    print(f"{len(found)} leftover name(s)", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

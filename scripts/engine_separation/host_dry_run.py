#!/usr/bin/env python3
"""Review Host namespace changes without modifying the workspace.

Print table-scoped proposed diffs, manual exceptions and unclassified references.
The output is a review aid, not an approved patch or an application tool.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
TABLE = Path(__file__).resolve().with_name("host_table.json")


def read_text(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def review(root: Path, table: dict) -> tuple[list[str], list[str], list[str]]:
    rules = {r["id"]: r for r in table["rules"]}
    files = {f["path"]: f for f in table["files"]}
    reference = re.compile(table["scan"]["reference_pattern"])
    diffs, exceptions, unclassified = [], [], []
    for rel, entry in files.items():
        text = read_text(root / rel)
        if text is None:
            exceptions.append(f"{rel}: missing, binary, or symlink; review manually")
            continue
        if entry["mode"] == "regenerate":
            exceptions.append(f"{rel}: REGENERATE/OWNER REVIEW: {entry.get('why', '')}")
            continue
        lines = text.splitlines(keepends=True)
        result = []
        for number, line in enumerate(lines, 1):
            hand = [item for item in entry.get("hand", []) if item["match"] in line]
            if hand:
                for item in hand:
                    reason = item["why"]
                    if rel == "src/openclank/mimo_supervisor.py" and ("read BOTH" in reason or "must look for" in reason or "same, for generation" in reason):
                        reason = "REJECTED stale dual-read instruction; final namespace forbids fallback reads of real MiMo names; review migration separately"
                    exceptions.append(f"{rel}:{number}: MANUAL: {reason} | {line.strip()}")
                result.append(line)
                continue
            new = line
            for rule_id in entry.get("rules", []):
                rule = rules[rule_id]
                if rule.get("only_lines_containing") and not any(s in line for s in rule["only_lines_containing"]):
                    continue
                new = re.sub(rule["pattern"], lambda _m: rule["replace"], new)
            result.append(new)
        changed = "".join(result)
        if changed != text:
            diffs.extend(difflib.unified_diff(text.splitlines(keepends=True), changed.splitlines(keepends=True), fromfile="a/" + rel, tofile="b/" + rel))
        for item in entry.get("hand", []):
            if not any(item["match"] in line for line in lines):
                exceptions.append(f"{rel}: MANUAL marker absent: {item['match']} ({item['why']})")
    skip_dirs = set(table["scan"]["skip_dirs"])
    skip_files = set(table["scan"]["skip_files"])
    seen = set()
    for scope in table["scan"]["dirs"]:
        start = root / scope
        if start.is_symlink():
            continue
        if start.is_file():
            candidates = [start]
        elif start.is_dir():
            candidates = []
            for base, dirs, names in os.walk(start, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d not in skip_dirs and not (Path(base) / d).is_symlink())
                candidates.extend(Path(base) / name for name in sorted(names))
        else:
            continue
        for path in candidates:
            rel = path.relative_to(root).as_posix()
            if rel in seen or rel in skip_files:
                continue
            seen.add(rel)
            text = read_text(path)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if reference.search(line):
                    # Every residual deserves review; entries outside the table are not silently ignored.
                    category = "TABLE" if rel in files else "UNCLASSIFIED"
                    if category == "UNCLASSIFIED":
                        unclassified.append(f"{rel}:{number}: {line.strip()[:240]}")
    return diffs, exceptions, unclassified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="workspace or disposable fixture root")
    parser.add_argument("--table", type=Path, default=TABLE)
    args = parser.parse_args()
    table = json.loads(args.table.read_text(encoding="utf-8"))
    if table.get("version") != 1:
        parser.error("unsupported host table version")
    diffs, exceptions, unclassified = review(args.root.resolve(), table)
    print("=== PROPOSED TABLE DIFFS (review only) ===")
    print("".join(diffs), end="")
    print("\n=== MANUAL / REGENERATION EXCEPTIONS ===")
    print("\n".join(exceptions) or "(none)")
    print("\n=== REFERENCES OUTSIDE TABLE (classify manually) ===")
    print("\n".join(unclassified) or "(none)")
    print(f"\nSummary: {len(exceptions)} exceptions; {len(unclassified)} unclassified references. No files changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

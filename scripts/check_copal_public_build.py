#!/usr/bin/env python3
"""Refuse private Copal payloads before public builds or native embedding."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPAL = ROOT / "packages/Copal"
PRIVATE_DIRS = {".clanker", ".references", ".archive", ".obsidian"}


def check(directory: Path) -> None:
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("public Copal assets must not contain symlinks")
        if not path.is_file():
            continue
        relative = path.relative_to(directory)
        if (any(part in PRIVATE_DIRS for part in relative.parts)
                or path.name == "move-data.json"
                or path.name.startswith("history-credentials")
                or path.name.startswith(".env")):
            raise ValueError("private runtime payload present in public Copal assets")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", choices=("source", "output"))
    parser.add_argument("--copal-root", type=Path, default=COPAL)
    args = parser.parse_args()
    try:
        check(args.copal_root / ("public" if args.scope == "source" else "out"))
        # The public fallback JSON must exactly match the neutral source seed.
        text = (args.copal_root / "src/lib/seed.ts").read_text()
        seed = json.loads(text.split("export const SEED: MoveData = ", 1)[1].rsplit(";", 1)[0])
        directory = args.copal_root / ("public" if args.scope == "source" else "out")
        public_seed = json.loads((directory / "examples/planning.json").read_text())
        if seed != public_seed:
            raise ValueError("public planning fallback differs from source seed")
    except (OSError, ValueError, IndexError):
        print("Copal public build refused: private payload, missing assets or seed mismatch. Build from the reviewed clean export; preserve local runtime JSON separately.")
        return 1
    print(f"Copal public {args.scope} assets passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

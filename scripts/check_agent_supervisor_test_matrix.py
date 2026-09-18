#!/usr/bin/env python3
"""Validate the executable S00 supervisor test-oracle rows."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "contracts/openclank/agent-supervisor-test-matrix-v1.json"
LEDGER = ROOT / "contracts/openclank/mimo-runtime-delta-ledger-v1.json"


def _check_ledger() -> None:
    data = json.loads(LEDGER.read_text())
    rows = data["rows"]
    if len(rows) != data["source"]["total"]:
        raise ValueError("ledger row count does not match its recorded snapshot")
    paths = [row["path"] for row in rows]
    if len(paths) != len(set(paths)):
        raise ValueError("ledger contains duplicate paths")
    required = {
        "path",
        "git_state",
        "category",
        "behavior",
        "current_tests",
        "future_owner",
        "owning_slice",
        "dependencies",
        "retirement_action",
        "evidence",
    }
    if any(not required <= set(row) for row in rows):
        raise ValueError("ledger row is missing a required ownership field")
    import subprocess

    raw = subprocess.check_output(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            "packages/mimo-code",
        ],
        cwd=ROOT,
        text=True,
    )
    current = {
        line[3:]: "untracked" if line[:2] == "??" else "modified"
        for line in raw.splitlines()
    }
    recorded = {row["path"]: row["git_state"] for row in rows}
    if current != recorded:
        raise ValueError("ledger path/state set differs from the scoped worktree snapshot")


def main(argv: list[str] | None = None) -> int:
    args = set(argv or sys.argv[1:])
    try:
        _check_ledger()
        data = json.loads(MATRIX.read_text())
        rows = data["deliverables"]
        expected = {f"RAS-D{i:02d}" for i in range(1, 51)}
        seen = {row["id"] for row in rows}
        if seen & expected != {row["id"] for row in rows}:
            raise ValueError("matrix contains an unknown deliverable id")
        if len(seen) != len(rows):
            raise ValueError("matrix contains duplicate deliverable ids")
        for row in rows:
            if not isinstance(row.get("argv"), list) or not row["argv"]:
                raise ValueError(f"{row['id']} needs an argv array")
            if row.get("status") not in {"planned", "active", "passed"}:
                raise ValueError(f"{row['id']} has an invalid status")
            for path in row.get("paths", []):
                if not (ROOT / path).exists() and row["status"] == "passed":
                    raise ValueError(f"{row['id']} references missing passed path {path}")
        required = {f"RAS-D{i:02d}" for i in range(1, 6)}
        if not required <= seen:
            raise ValueError("RAS-D01 through RAS-D05 must be executable rows")
        if "--final" in args and seen != expected:
            raise ValueError("final validation requires all RAS-D01 through RAS-D50 rows")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"agent-supervisor test matrix invalid: {exc}", file=sys.stderr)
        return 2
    print(f"agent-supervisor test matrix valid: {len(rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

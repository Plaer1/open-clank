#!/usr/bin/env python3
"""Write the immutable D09 two-source aggregate receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


NAMES = (
    "01-external.snapshot.sqlite3",
    "02-runtime-global.snapshot.sqlite3",
    "01-external.plan.json",
    "02-runtime-global.plan.json",
    "01-external.receipt.json",
    "02-runtime-global.receipt.json",
    "01-external.replay.receipt.json",
    "02-runtime-global.replay.receipt.json",
    "asset-verification.json",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid evidence JSON: {path}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"evidence is not an object: {path}")
    return value


def write_new_json(path: Path, payload: object) -> None:
    if path.exists():
        raise SystemExit(f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_receipt(receipt: dict, *, replay: bool) -> None:
    plan = receipt.get("plan")
    if receipt.get("ok") is not True or receipt.get("owner") != "e" or not isinstance(plan, dict):
        raise SystemExit("receipt does not bind an e-owned migration plan")
    if plan.get("owner_mapping") != {"<ownerless>": "e"}:
        raise SystemExit("receipt has a different owner mapping")
    if sorted(plan.get("allowed_owners", [])) != ["allie", "e", "mom"]:
        raise SystemExit("receipt has a different admitted-account set")
    if replay:
        if receipt.get("accepted") != 0 or receipt.get("duplicate") != receipt.get("planned_parts"):
            raise SystemExit("replay is not a complete idempotent duplicate")
        for kind in ("sessions", "messages"):
            if receipt.get(f"accepted_{kind}") != 0:
                raise SystemExit("replay accepted an empty envelope")
            if receipt.get(f"duplicate_{kind}") != receipt.get(f"planned_empty_{kind}"):
                raise SystemExit("replay did not duplicate every empty envelope")
    elif receipt.get("accepted", 0) + receipt.get("duplicate", 0) != receipt.get("planned_parts"):
        raise SystemExit("first apply did not account for every planned part")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    evidence = Path(args.evidence)
    files = {name: evidence / name for name in NAMES}
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        raise SystemExit("missing required evidence: " + ", ".join(missing))
    if not Path(args.archive).is_file():
        raise SystemExit("archive is absent")
    first = [read_json(files[name]) for name in ("01-external.receipt.json", "02-runtime-global.receipt.json")]
    replay = [read_json(files[name]) for name in ("01-external.replay.receipt.json", "02-runtime-global.replay.receipt.json")]
    for receipt in first:
        verify_receipt(receipt, replay=False)
    for receipt in replay:
        verify_receipt(receipt, replay=True)
    asset = read_json(files["asset-verification.json"])
    if asset.get("owner") != "e" or asset.get("source_id_intersections") != {"session": 0, "message": 0, "part": 0}:
        raise SystemExit("asset verifier did not prove the selected-source scope")
    plans = [item["plan"] for item in first]
    if sum(int(plan["parts"]) for plan in plans) != 84281:
        raise SystemExit("planned part total does not match the approved source scope")
    write_new_json(
        Path(args.output),
        {
            "schema": "open-clank-d09-multisource-receipt/v1",
            "owner_mapping": {"<ownerless>": "e"},
            "allowed_owners": ["allie", "e", "mom"],
            "source_count": 2,
            "planned_parts_total": 84281,
            "source_fingerprints": [plan["source_fingerprint"] for plan in plans],
            "archive_sha256": sha256_file(Path(args.archive)),
            "artifacts": {name: sha256_file(path) for name, path in files.items()},
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

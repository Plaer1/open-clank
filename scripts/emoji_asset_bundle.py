#!/usr/bin/env python3
"""Split, assemble and verify the pinned offline emoji pack. No network access."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "static/vendor/google-emoji"
MAX_PART_BYTES = 512 * 1024 * 1024
BUFFER_BYTES = 4 * 1024 * 1024


class BundleError(RuntimeError):
    pass


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        while block := stream.read(BUFFER_BYTES):
            count += len(block)
            digest.update(block)
    return count, digest.hexdigest()


def expected_manifest(path: Path) -> dict:
    data = json.loads(path.read_text())
    if (type(data.get("pack_bytes")) is not int or data["pack_bytes"] <= 0
            or len(str(data.get("pack_sha256", ""))) != 64
            or not data.get("runtime_available")):
        raise BundleError("invalid pinned artwork manifest")
    return data


def verify(pack: Path, expected: dict) -> None:
    if file_digest(pack) != (expected["pack_bytes"], expected["pack_sha256"]):
        raise BundleError("artwork pack size or SHA-256 mismatch")
    with sqlite3.connect(pack.resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BundleError("artwork SQLite integrity failure")
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        catalogs = json.loads(metadata["runtime_catalogs"])
        exceptions = json.loads(metadata["upstream_exceptions"])
        counts = dict(db.execute("SELECT kind,count(*) FROM runtime_assets GROUP BY kind"))
        kitchen_ids = {row[0] for row in db.execute("SELECT identity FROM runtime_assets WHERE kind='kitchen'")}
        if (metadata.get("runtime_available") != "true"
                or counts.get("google") != expected["google_resolvable"]
                or counts.get("kitchen") != expected["available_kitchen"]
                or len(catalogs["google"]) != expected["google_sampler"]
                or set(catalogs["kitchen"]) != kitchen_ids
                or len(exceptions) + counts["kitchen"] != expected["expected_kitchen"]
                or exceptions != expected["exceptions"]
                or db.execute("SELECT count(*) FROM assets").fetchone()[0] != expected["original_records"]):
            raise BundleError("artwork catalog or exception-count mismatch")


def split(pack: Path, output: Path, expected: dict, part_bytes: int) -> int:
    if not 1024 * 1024 <= part_bytes <= MAX_PART_BYTES:
        raise BundleError("part size must be between 1 MiB and 512 MiB")
    verify(pack, expected)
    count = math.ceil(expected["pack_bytes"] / part_bytes)
    names = [f"emoji-assets.pack.part-{number:03d}" for number in range(1, count + 1)]
    if any((output / name).exists() for name in names + ["emoji-assets.parts.json"]):
        raise BundleError("part outputs already exist; choose an empty destination")
    output.mkdir(parents=True, exist_ok=True)
    parts = []
    with pack.open("rb") as source:
        for name in names:
            digest = hashlib.sha256()
            size = 0
            with (output / name).open("xb") as destination:
                while size < part_bytes and (block := source.read(min(BUFFER_BYTES, part_bytes - size))):
                    destination.write(block)
                    digest.update(block)
                    size += len(block)
            parts.append({"filename": name, "bytes": size, "sha256": digest.hexdigest()})
    manifest = {"schema_version": 1,
                "pack_bytes": expected["pack_bytes"], "pack_sha256": expected["pack_sha256"],
                "part_bytes": part_bytes, "parts": parts}
    with (output / "emoji-assets.parts.json").open("x") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
    return len(parts)


def assemble(parts_root: Path, destination: Path, expected: dict) -> None:
    manifest = json.loads((parts_root / "emoji-assets.parts.json").read_text())
    if (manifest.get("schema_version") != 1
            or manifest.get("pack_bytes") != expected["pack_bytes"]
            or manifest.get("pack_sha256") != expected["pack_sha256"]):
        raise BundleError("release parts do not match the pinned artwork manifest")
    part_bytes = manifest.get("part_bytes")
    if type(part_bytes) is not int or not 1 <= part_bytes <= MAX_PART_BYTES:
        raise BundleError("invalid release part size")
    parts = manifest.get("parts")
    if not isinstance(parts, list) or len(parts) != math.ceil(expected["pack_bytes"] / part_bytes):
        raise BundleError("missing release parts")
    for number, part in enumerate(parts, 1):
        expected_size = min(part_bytes, expected["pack_bytes"] - (number - 1) * part_bytes)
        if (not isinstance(part, dict)
                or part.get("filename") != f"emoji-assets.pack.part-{number:03d}"
                or part.get("bytes") != expected_size or len(str(part.get("sha256", ""))) != 64):
            raise BundleError("invalid release part record")
    # Existing runtime state is never overwritten, even when invalid.
    if destination.exists() or destination.is_symlink():
        verify(destination, expected)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=".emoji-assets-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as output:
            for part in parts:
                source = parts_root / part["filename"]
                digest = hashlib.sha256()
                count = 0
                with source.open("rb") as stream:
                    while block := stream.read(BUFFER_BYTES):
                        output.write(block)
                        digest.update(block)
                        count += len(block)
                if (count, digest.hexdigest()) != (part["bytes"], part["sha256"]):
                    raise BundleError("release part size or SHA-256 mismatch")
            output.flush()
            os.fsync(output.fileno())
        verify(temporary, expected)
        os.chmod(temporary, 0o644)
        # A hard-link publishes atomically and fails if another writer won.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "split", "assemble"))
    parser.add_argument("--pack", type=Path, default=ASSETS / "emoji-assets.pack")
    parser.add_argument("--manifest", type=Path, default=ASSETS / "bundle-manifest.json")
    parser.add_argument("--parts", type=Path, help="local part output/input directory")
    parser.add_argument("--part-bytes", type=int, default=MAX_PART_BYTES)
    args = parser.parse_args()
    try:
        expected = expected_manifest(args.manifest)
        if args.command == "verify":
            verify(args.pack, expected)
        elif args.parts is None:
            raise BundleError("--parts is required")
        elif args.command == "split":
            count = split(args.pack, args.parts, expected, args.part_bytes)
            print(f"offline artwork parts prepared: {count}")
        else:
            assemble(args.parts, args.pack, expected)
    except (BundleError, OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        print("offline artwork bundle failed verification or assembly; original installed pack was preserved")
        return 1
    print("offline artwork bundle verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Split, assemble and verify the pinned offline emoji pack. No network access."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.emoji_runtime_schema import (
    SCHEMA_VERSION, IDENTITY, MIME, assert_schema, decoded_blob, read_metadata,
)

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "static/vendor/google-emoji"
MAX_PART_BYTES = 512 * 1024 * 1024
BUFFER_BYTES = 4 * 1024 * 1024


class BundleError(RuntimeError):
    pass


def _report_failure(stage: str, error: BaseException, completed_parts: int = 0) -> None:
    """Emit bounded diagnostics without exception text, paths or environment."""
    if getattr(error, "_offline_artwork_reported", False):
        return
    record = {"stage": stage, "error_type": type(error).__name__, "completed_parts": completed_parts}
    for field in ("errno", "winerror"):
        value = getattr(error, field, None)
        if type(value) is int:
            record[field] = value
    print("offline artwork diagnostic: " + json.dumps(record, sort_keys=True), file=sys.stderr)
    error._offline_artwork_reported = True


def runtime_asset_paths(
    *, app_root: Path | None = None, data_dir: Path | None = None,
    frozen: bool | None = None,
) -> tuple[Path, Path]:
    """Resolve the install destination and immutable bundled pin.

    Source setup stays inside its writable checkout. Frozen executables keep
    the large install-time payload in normal application data, outside their
    checksummed bundle. No runtime download or caller-supplied manifest is used.
    """
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if app_root is None:
        if is_frozen:
            from src.runtime_paths import get_app_root
            app_root = Path(get_app_root())
        else:
            app_root = ROOT
    assets = Path(app_root).expanduser().absolute() / "static/vendor/google-emoji"
    if is_frozen:
        if data_dir is None:
            from src.runtime_paths import get_default_data_dir
            data_dir = Path(os.environ.get("OPEN_CLANK_DATA_DIR")
                            or os.environ.get("ODYSSEUS_DATA_DIR")
                            or get_default_data_dir())
        pack = Path(data_dir).expanduser().absolute() / "assets/google-emoji/emoji-assets.pack"
    else:
        pack = assets / "emoji-assets.pack"
    return pack, assets / "bundle-manifest.json"


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
            or not data.get("runtime_available")
            or data.get("runtime_schema") != SCHEMA_VERSION
            or data.get("kitchen_format") not in MIME
            or not isinstance(data.get("runtime_identity_sha256"), str)
            or len(data["runtime_identity_sha256"]) != 64):
        raise BundleError("invalid pinned artwork manifest")
    return data


def verify(pack: Path, expected: dict) -> None:
    stage = "pack-hash"
    try:
        if file_digest(pack) != (expected["pack_bytes"], expected["pack_sha256"]):
            raise BundleError("artwork pack size or SHA-256 mismatch")
        stage = "sqlite-open"
        with closing(sqlite3.connect(pack.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            stage = "sqlite-integrity"
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BundleError("artwork SQLite integrity failure")
            assert_schema(db)
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BundleError("artwork SQLite foreign key failure")
            stage = "catalog-verification"
            metadata = read_metadata(db)
            catalogs = json.loads(metadata["runtime_catalogs"])
            exceptions = json.loads(metadata["upstream_exceptions"])
            identities = [row[0] for row in db.execute("SELECT identity FROM runtime_assets ORDER BY identity")]
            if any(not IDENTITY.fullmatch(identity) for identity in identities):
                raise BundleError("artwork runtime identity is malformed")
            kitchen_ids = {identity for identity in identities if identity.startswith("k:")}
            google_ids = {identity for identity in identities if identity.startswith("g:")}
            identity_digest = hashlib.sha256(("\n".join(identities) + "\n").encode("utf-8")).hexdigest()
            if (metadata.get("runtime_available") != "true"
                    or len(google_ids) != expected["google_resolvable"]
                    or len(kitchen_ids) != expected["available_kitchen"]
                    or len(catalogs["google"]) != expected["google_sampler"]
                    or set(catalogs["kitchen"]) != kitchen_ids
                    or len(catalogs["kitchen"]) != len(kitchen_ids)
                    or len({entry["id"] for entry in catalogs["google"]}) != len(catalogs["google"])
                    or any(entry["id"] not in google_ids for entry in catalogs["google"])
                    or len(exceptions) + len(kitchen_ids) != expected["expected_kitchen"]
                    or exceptions != expected["exceptions"]
                    or identity_digest != expected["runtime_identity_sha256"]):
                raise BundleError("artwork catalog or exception-count mismatch")
            if db.execute("SELECT count(*) FROM blobs WHERE id NOT IN (SELECT blob_id FROM runtime_assets)").fetchone()[0]:
                raise BundleError("artwork contains unreferenced runtime blobs")
            if db.execute("SELECT count(*) FROM runtime_assets r JOIN blobs b ON b.id=r.blob_id WHERE (substr(r.identity,1,2)='g:' AND b.format!='svg') OR (substr(r.identity,1,2)='k:' AND b.format!=?)", (expected["kitchen_format"],)).fetchone()[0]:
                raise BundleError("artwork runtime image format differs")
            stage = "blob-verification"
            limit = expected.get("optimization", {}).get("profile", {}).get("max_dimension")
            if expected["kitchen_format"] == "webp" and (type(limit) is not int or not 0 < limit <= 256):
                raise BundleError("artwork raster dimension pin is missing or invalid")
            for row in db.execute("SELECT sha256,format,codec,decoded_bytes,data,width,height FROM blobs"):
                decoded_blob(row, max_dimension=limit)
    except (BundleError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        _report_failure(stage, error)
        raise


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
    stage = "copy-parts"
    completed_parts = 0
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
                completed_parts += 1
            stage = "flush-parts"
            output.flush()
            os.fsync(output.fileno())
        stage = "verify-assembled-pack"
        verify(temporary, expected)
        stage = "prepare-publication"
        os.chmod(temporary, 0o644)
        # A hard-link publishes atomically and fails if another writer won.
        stage = "publish-pack"
        os.link(temporary, destination)
    except (BundleError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        _report_failure(stage, error, completed_parts)
        raise
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as error:
            _report_failure("cleanup-temporary", error, completed_parts)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "split", "assemble"))
    default_pack, default_manifest = runtime_asset_paths()
    parser.add_argument("--pack", type=Path, default=default_pack)
    parser.add_argument("--manifest", type=Path, default=default_manifest)
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
    except (BundleError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        _report_failure("manifest-or-command", error)
        print("offline artwork bundle failed verification or assembly; original installed pack was preserved")
        return 1
    print("offline artwork bundle verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

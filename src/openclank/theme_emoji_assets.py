"""Verified local Google/Kitchen artwork; no runtime network or cache writes."""
from __future__ import annotations
import json
import re
import sqlite3
import sys
import threading
from functools import lru_cache
from contextlib import contextmanager
from pathlib import Path
from scripts.emoji_asset_bundle import BundleError, expected_manifest, runtime_asset_paths, file_digest
from scripts.emoji_runtime_schema import assert_schema, decoded_blob, read_metadata

_PACK, _MANIFEST = runtime_asset_paths()
_ID = re.compile(r"^[gk]:[0-9a-f]+(?:-[0-9a-f]+)*(?:_[0-9a-f]+(?:-[0-9a-f]+)*)?$")


def artwork_install_hint() -> str:
    command = "openclank assets assemble" if getattr(sys, "frozen", False) else "python3 scripts/emoji_asset_bundle.py assemble"
    return f"Install the matching offline release parts with {command} --parts <directory>."


_verification_lock = threading.Lock()


def _pack_identity() -> tuple[int, ...]:
    stat = _PACK.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@lru_cache(maxsize=1)
def _verify_frozen_pin(path: str, identity: tuple[int, ...]) -> bool:
    # Exact sealed bytes already bind the database whose full SQLite integrity
    # is checked by assets verify/assemble. Do not repeat that scan on requests.
    expected = expected_manifest(_MANIFEST)
    return (file_digest(Path(path)) == (expected["pack_bytes"], expected["pack_sha256"])
            and _pack_identity() == identity)


def _verify_frozen_pack(path: str, identity: tuple[int, ...]) -> None:
    # Lock outside the memoized function: concurrent first misses must share
    # one full hash. Cache failures too until the writable file identity changes.
    with _verification_lock:
        try:
            if _pack_identity() != identity or not _verify_frozen_pin(path, identity):
                raise BundleError("artwork pack does not match its sealed pin")
        except (BundleError, KeyError, TypeError) as exc:
            raise RuntimeError("Offline emoji artwork failed pinned verification") from exc


def _verified_identity() -> tuple[int, ...]:
    if not _PACK.is_file():
        raise RuntimeError("Offline emoji artwork is missing. " + artwork_install_hint())
    identity = _pack_identity()
    if getattr(sys, "frozen", False):
        _verify_frozen_pack(str(_PACK), identity)
    return identity


@contextmanager
def _connect():
    identity = _verified_identity()
    db = sqlite3.connect(_PACK.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        assert_schema(db)
        if _pack_identity() != identity:
            raise RuntimeError("Offline emoji artwork changed during verification")
        yield db
        if _pack_identity() != identity:
            raise RuntimeError("Offline emoji artwork changed while reading")
    except (ValueError, sqlite3.Error) as error:
        raise RuntimeError("Offline emoji artwork failed runtime verification") from error
    finally:
        db.close()


@lru_cache(maxsize=1)
def _catalogs_for_identity(identity: tuple[int, ...]) -> dict:
    with _connect() as db:
        metadata = read_metadata(db)
        runtime_count = db.execute("SELECT count(*) FROM runtime_assets WHERE identity LIKE 'k:%'").fetchone()[0]
    row, exceptions = metadata.get("runtime_catalogs"), metadata.get("upstream_exceptions")
    if not row or metadata.get("runtime_available") != "true":
        raise RuntimeError("Packaged emoji artwork is incomplete")
    catalogs = json.loads(row)
    if (not catalogs.get("google") or not exceptions or len(catalogs.get("kitchen", [])) != runtime_count
            or runtime_count + len(json.loads(exceptions)) != 147000):
        raise RuntimeError("Packaged emoji catalogue integrity failure")
    return catalogs


def get_emoji_catalogs() -> dict:
    return _catalogs_for_identity(_verified_identity())


@lru_cache(maxsize=1)
def _kitchen_text_for_identity(identity: tuple[int, ...]) -> str:
    return "\n".join(_catalogs_for_identity(identity)["kitchen"]) + "\n"


def get_kitchen_catalog_text() -> str:
    return _kitchen_text_for_identity(_verified_identity())


def get_google_catalog() -> dict:
    catalogs = get_emoji_catalogs()
    with _connect() as db:
        metadata = read_metadata(db)
        complete = metadata.get("bundle_complete") == "true"
        exceptions = json.loads(metadata["upstream_exceptions"])
    return {"entries": catalogs["google"], "local": True, "complete": complete,
            "availableKitchen": len(catalogs["kitchen"]), "expectedKitchen": 147000,
            "unavailableKitchen": len(exceptions)}


def get_packaged_asset(asset_id: str, kind: str) -> tuple[bytes, str, str]:
    if not _ID.fullmatch(asset_id) or not asset_id.startswith("k:" if kind == "kitchen" else "g:"):
        raise KeyError(asset_id)
    if any(0x1F3FB <= int(part, 16) <= 0x1F3FF for part in re.split(r"[-_:]", asset_id)[1:]):
        raise KeyError(asset_id)
    get_emoji_catalogs()
    with _connect() as db:
        row = db.execute("SELECT b.sha256,b.format,b.codec,b.decoded_bytes,b.data,b.width,b.height FROM runtime_assets r JOIN blobs b ON b.id=r.blob_id WHERE r.identity=?", (asset_id,)).fetchone()
    if row is None:
        raise KeyError(asset_id)
    try:
        return decoded_blob(row)
    except ValueError as error:
        raise RuntimeError("Offline emoji artwork failed response verification") from error

"""Immutable packaged Google/Kitchen artwork; no network or user-cache writes."""
from __future__ import annotations
import json
import re
import sqlite3
from functools import lru_cache
from contextlib import contextmanager
from pathlib import Path
from src.runtime_paths import get_app_root

_PACK = Path(get_app_root()) / "static/vendor/google-emoji/emoji-assets.pack"
_ID = re.compile(r"^[gk]:[0-9a-f]+(?:-[0-9a-f]+)*(?:_[0-9a-f]+(?:-[0-9a-f]+)*)?$")


@contextmanager
def _connect():
    if not _PACK.is_file():
        raise RuntimeError("Packaged emoji artwork is missing")
    db = sqlite3.connect(_PACK.as_uri() + "?mode=ro&immutable=1", uri=True)
    db.execute("PRAGMA query_only=ON")
    try:
        yield db
    finally:
        db.close()


@lru_cache(maxsize=1)
def get_emoji_catalogs() -> dict:
    with _connect() as db:
        row = db.execute("SELECT value FROM metadata WHERE key='runtime_catalogs'").fetchone()
        exceptions = db.execute("SELECT value FROM metadata WHERE key='upstream_exceptions'").fetchone()
        runtime_count = db.execute("SELECT count(*) FROM runtime_assets WHERE kind='kitchen'").fetchone()[0]
        complete = db.execute("SELECT value FROM metadata WHERE key='runtime_available'").fetchone()
    if not row or not complete or complete[0] != "true":
        raise RuntimeError("Packaged emoji artwork is incomplete")
    catalogs = json.loads(row[0])
    if (not catalogs.get("google") or not exceptions or len(catalogs.get("kitchen", [])) != runtime_count
            or runtime_count + len(json.loads(exceptions[0])) != 147000):
        raise RuntimeError("Packaged emoji catalogue integrity failure")
    return catalogs


@lru_cache(maxsize=1)
def get_kitchen_catalog_text() -> str:
    return "\n".join(get_emoji_catalogs()["kitchen"]) + "\n"


def get_google_catalog() -> dict:
    catalogs = get_emoji_catalogs()
    with _connect() as db:
        complete = db.execute("SELECT value FROM metadata WHERE key='bundle_complete'").fetchone()[0] == "true"
        exceptions = json.loads(db.execute("SELECT value FROM metadata WHERE key='upstream_exceptions'").fetchone()[0])
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
        row = db.execute("SELECT b.data,b.sha256,r.format FROM runtime_assets r JOIN blobs b ON b.sha256=r.sha256 WHERE r.identity=? AND r.kind=?", (asset_id, kind)).fetchone()
    if row is None:
        raise KeyError(asset_id)
    data, digest, format_name = row
    return data, "image/svg+xml" if format_name == "svg" else "image/png", digest

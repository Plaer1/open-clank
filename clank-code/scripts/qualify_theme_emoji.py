"""Actual authenticated package artwork HTTP journey; stdlib host helper."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from scripts.emoji_runtime_schema import assert_schema, decoded_blob, webp_dimensions

CHECK = "concurrent-cold-theme-emoji-catalogs-and-native-images"
_ID = re.compile(r"^[gk]:[0-9a-f]+(?:-[0-9a-f]+)*(?:_[0-9a-f]+(?:-[0-9a-f]+)*)?$")


def qualify_theme_emoji(base, cookies, manifest: Path, pack: Path) -> dict:
    expected = json.loads(manifest.read_text(encoding="utf-8"))

    def fetch(path, limit=16 * 1024 * 1024):
        # Independent HTTP handlers, shared fixture CookieJar; never print it.
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        request = urllib.request.Request(base + path, headers={"Origin": base})
        with opener.open(request, timeout=30) as response:
            data = response.read(limit + 1)
            if response.status != 200 or len(data) > limit:
                raise RuntimeError("Packaged emoji response failed or exceeded its bounded size")
            return data, response.headers

    start = time.monotonic()
    paths = ("/api/theme-emoji/google/catalog", "/api/theme-emoji/kitchen/catalog")
    with ThreadPoolExecutor(max_workers=2) as pool:
        google_response, kitchen_response = list(pool.map(fetch, paths))
    cold_seconds = time.monotonic() - start
    if cold_seconds >= 45:
        raise RuntimeError("Cold packaged emoji catalogs exceeded the ordinary request budget")
    google_bytes, google_headers = google_response
    kitchen_bytes, kitchen_headers = kitchen_response
    if google_headers.get_content_type() != "application/json" or kitchen_headers.get_content_type() != "text/plain":
        raise RuntimeError("Packaged emoji catalog MIME types differ")
    google = json.loads(google_bytes)
    kitchen = kitchen_bytes.decode("utf-8").splitlines()
    entries = google.get("entries", [])
    if (google.get("local") is not True or len(entries) != expected["google_sampler"]
            or len(kitchen) != expected["available_kitchen"] or len(set(kitchen)) != len(kitchen)
            or google.get("availableKitchen") != expected["available_kitchen"]
            or google.get("expectedKitchen") != expected["expected_kitchen"]
            or google.get("unavailableKitchen") != len(expected["exceptions"])):
        raise RuntimeError("Actual packaged emoji catalogs disagree with the sealed manifest")
    google_ids = [entry["id"] for entry in entries]
    if (len(set(google_ids)) != len(google_ids)
            or any(not _ID.fullmatch(identity) or not identity.startswith("g:") for identity in google_ids)
            or any(not _ID.fullmatch(identity) or not identity.startswith("k:") for identity in kitchen)):
        raise RuntimeError("Actual packaged emoji identities are malformed or duplicated")
    samples = {}
    # The qualifier already admitted the complete pinned pack with CLI verify.
    # Read only two expected blob identities to bind HTTP bytes to that artifact.
    with closing(sqlite3.connect(pack.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        assert_schema(db)
        for kind, identity, format_name in (("google", google_ids[0], "svg"),
                                             ("kitchen", kitchen[0], expected["kitchen_format"])):
            row = db.execute("SELECT b.sha256,b.format,b.codec,b.decoded_bytes,b.data,b.width,b.height FROM runtime_assets r JOIN blobs b ON b.id=r.blob_id WHERE r.identity=?", (identity,)).fetchone()
            if not row or row[1] != format_name:
                raise RuntimeError("Verified emoji sample is outside the sealed image profile")
            profile = expected.get("optimization", {}).get("profile", {})
            expected_data, mime, expected_digest = decoded_blob(row, max_dimension=profile.get("max_dimension"))
            data, headers = fetch("/api/theme-emoji/" + kind + "/" + urllib.parse.quote(identity, safe=""), 4 * 1024 * 1024)
            digest = hashlib.sha256(data).hexdigest()
            if (headers.get_content_type() != mime or headers.get("X-Content-Type-Options") != "nosniff"
                    or headers.get("ETag") != '"' + digest + '"' or digest != expected_digest or data != expected_data):
                raise RuntimeError("Actual emoji image headers or bytes disagree with the verified pack")
            sample = {"sha256": digest, "bytes": len(data), "mime": mime}
            if format_name == "svg":
                if ET.fromstring(data).tag.rsplit("}", 1)[-1] != "svg":
                    raise RuntimeError("Actual image sample is not an SVG document")
            else:
                width, height = webp_dimensions(data)
                if (width, height) != row[5:7] or max(width, height) > profile["max_dimension"]:
                    raise RuntimeError("Actual Kitchen WebP dimensions disagree with the sealed profile")
                sample.update(width=width, height=height)
            samples[kind] = sample
    return {"cold_catalog_seconds": round(cold_seconds, 3), "request_timeout_seconds": 30,
            "google_sampler": len(entries), "available_kitchen": len(kitchen),
            "unavailable_kitchen": len(expected["exceptions"]), "samples": samples}

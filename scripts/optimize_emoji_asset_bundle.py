#!/usr/bin/env python3
"""Derive a separate, resumable compact offline runtime and provenance sidecar."""
from __future__ import annotations
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import gzip
import hashlib
import io
import json
import os
import re
from pathlib import Path
import sqlite3
import time
import PIL
from PIL import Image, features
from scripts.emoji_asset_bundle import expected_manifest, file_digest, verify
from scripts import emoji_runtime_schema as schema


def profile(dimension):
    return {"name": "compact-lossless-webp-v2", "max_dimension": dimension,
            "resampling": "nearest", "webp_lossless": True, "webp_method": 4,
            "webp_effort": 75, "webp_exact": True, "google_svg": "exact-zlib",
            "sqlite_page_size": 1024, "runtime_schema": 2}


def encode(row, dimension):
    source_sha, kind, old_format, raw, width, height = row
    if hashlib.sha256(raw).hexdigest() != source_sha:
        raise RuntimeError("Original response digest differs from provenance")
    if (kind, old_format) == ("google", "svg"):
        response, format_name, codec = raw, "svg", 1
    elif (kind, old_format) == ("kitchen", "png"):
        with Image.open(io.BytesIO(raw)) as original:
            image = original.copy()
        image.thumbnail((dimension, dimension), Image.Resampling.NEAREST)
        reference = image.convert("RGBA")
        output = io.BytesIO()
        reference.save(output, format="WEBP", lossless=True, method=4, quality=75, exact=True)
        response, format_name, codec = output.getvalue(), "webp", 0
        with Image.open(io.BytesIO(response)) as decoded:
            if decoded.convert("RGBA").tobytes() != reference.tobytes():
                raise RuntimeError("Lossless WebP changed resized color or alpha pixels")
        width, height = image.size
        if schema.webp_dimensions(response) != (width, height):
            raise RuntimeError("Lossless WebP header dimensions differ")
    else:
        raise RuntimeError("Original payload is outside the approved SVG/PNG source profile")
    digest = hashlib.sha256(response).digest()
    stored = schema.encode_payload(response, codec)
    schema.decoded_blob((digest, format_name, codec, len(response), stored, width, height), max_dimension=dimension)
    return source_sha, len(raw), digest, format_name, codec, len(response), stored, width, height


def metadata_write(db, key, value):
    raw = value.encode("utf-8")
    db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?,1,?)",
               (key, schema.encode_payload(raw, 1, limit=schema.MAX_METADATA_BYTES), len(raw)))


def admit_original(source, pack, expected):
    # Explicit conversion input admission, not an old-schema application fallback.
    if file_digest(pack) != (expected["pack_bytes"], expected["pack_sha256"]):
        raise RuntimeError("Original pinned pack size or digest differs")
    if source.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise RuntimeError("Original SQLite integrity differs")
    if tuple(r[1] for r in source.execute("PRAGMA table_info(runtime_assets)")) != ("identity", "kind", "sha256", "format"):
        raise RuntimeError("Original conversion source schema differs")
    metadata = dict(source.execute("SELECT key,value FROM metadata"))
    catalogs = json.loads(metadata["runtime_catalogs"])
    counts = dict(source.execute("SELECT kind,count(*) FROM runtime_assets GROUP BY kind"))
    if (metadata.get("runtime_available") != "true"
            or counts != {"google": expected["google_resolvable"], "kitchen": expected["available_kitchen"]}
            or len(catalogs["google"]) != expected["google_sampler"]
            or sorted(catalogs["kitchen"]) != [r[0] for r in source.execute("SELECT identity FROM runtime_assets WHERE kind='kitchen' ORDER BY identity")]
            or json.loads(metadata["upstream_exceptions"]) != expected["exceptions"]
            or len(expected["exceptions"]) + counts["kitchen"] != expected["expected_kitchen"]
            or source.execute("SELECT count(*) FROM assets").fetchone()[0] != expected["original_records"]):
        raise RuntimeError("Original catalog or provenance counts differ")
    return metadata


def identity_digest(rows):
    digest = hashlib.sha256()
    for (name,) in rows:
        digest.update((name + "\n").encode("utf-8"))
    return digest.hexdigest()


def provenance(source, db, pending, identity, expected):
    columns = [r[1] for r in source.execute("PRAGMA table_info(assets)")]
    acquisitions = mappings = 0
    with pending.open("wb") as target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0, compresslevel=6) as compressed:
            def emit(value):
                compressed.write((json.dumps(value, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8"))
            expected_header = {"type": "header", "schema": 1, "optimization": identity, "source_manifest": expected,
                               "asset_columns": columns, "source_metadata": dict(source.execute("SELECT key,value FROM metadata ORDER BY key"))}
            emit(expected_header)
            for row in source.execute("SELECT * FROM assets ORDER BY asset_key"):
                emit({"type": "acquisition", "values": row}); acquisitions += 1
            for old_sha, blob_id, digest, count in db.execute("SELECT source_sha256,blob_id,payload_sha256,source_bytes FROM progress.derivations ORDER BY source_sha256"):
                emit({"type": "derivation", "source_sha256": old_sha, "blob_id": blob_id,
                      "payload_sha256": digest.hex(), "source_bytes": count}); mappings += 1
            emit({"type": "footer", "acquisitions": acquisitions, "derivations": mappings})
        target.flush(); os.fsync(target.fileno())
    # Independently stream the sidecar back against the original and checkpoint.
    with gzip.open(pending, "rt", encoding="utf-8") as stream:
        header = json.loads(next(stream))
        if header != expected_header:
            raise RuntimeError("Provenance header differs")
        for row in source.execute("SELECT * FROM assets ORDER BY asset_key"):
            record = json.loads(next(stream))
            if record != {"type": "acquisition", "values": list(row)}:
                raise RuntimeError("Original acquisition provenance changed")
        for old_sha, blob_id, digest, count in db.execute("SELECT source_sha256,blob_id,payload_sha256,source_bytes FROM progress.derivations ORDER BY source_sha256"):
            if json.loads(next(stream)) != {"type": "derivation", "source_sha256": old_sha, "blob_id": blob_id,
                                           "payload_sha256": digest.hex(), "source_bytes": count}:
                raise RuntimeError("Source-to-runtime provenance mapping differs")
        if json.loads(next(stream)) != {"type": "footer", "acquisitions": acquisitions, "derivations": mappings} or stream.read():
            raise RuntimeError("Provenance footer or stream differs")
    if acquisitions != expected["original_records"]:
        raise RuntimeError("Acquisition provenance count differs")
    return acquisitions, mappings


def derive(pack, manifest, output, *, workers=2, resume=False, dimension=160):
    if not 1 <= workers <= 4 or dimension not in (128, 160):
        raise RuntimeError("Use one to four workers and an approved 128/160px profile")
    # Runtime pin parsing is deliberately schema2-only; this offline converter
    # independently admits an explicit original acquisition/schema1 input.
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    if (not isinstance(expected, dict) or expected.get("runtime_schema", 1) != 1
            or type(expected.get("pack_bytes")) is not int or expected["pack_bytes"] <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(expected.get("pack_sha256", "")))
            or expected.get("runtime_available") is not True
            or any(type(expected.get(key)) is not int or expected[key] < 0 for key in
                   ("google_resolvable", "google_sampler", "available_kitchen", "expected_kitchen", "original_records"))
            or not isinstance(expected.get("exceptions"), list)):
        raise RuntimeError("Conversion requires the explicitly pinned original acquisition pack")
    if output.exists() != resume:
        raise RuntimeError("Require a fresh directory, or explicit --resume for an existing checkpoint")
    output.mkdir(parents=True, exist_ok=resume)
    destination = output / "emoji-assets.pack"; work = output / "work.pack"
    auxiliary = output / "emoji-assets.provenance.jsonl.gz"; pending_auxiliary = output / "provenance.pending.gz"
    candidate_manifest = output / "bundle-manifest.json"
    identity = {"profile": profile(dimension), "source_pack_sha256": expected["pack_sha256"],
                "source_manifest_sha256": file_digest(manifest)[1], "pillow": PIL.__version__,
                "libwebp": features.version("webp"), "pipeline_sha256": file_digest(Path(__file__))[1],
                "runtime_helper_sha256": file_digest(Path(schema.__file__))[1]}
    with closing(sqlite3.connect(pack.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as source:
        source_metadata = admit_original(source, pack, expected)
        original_identity = identity_digest(source.execute("SELECT identity FROM runtime_assets ORDER BY identity"))
        identity["source_runtime_identity_sha256"] = original_identity
        if candidate_manifest.exists():
            candidate = expected_manifest(candidate_manifest)
            if candidate.get("optimization") != identity or not destination.exists():
                raise RuntimeError("Published candidate identity differs")
            verify(destination, candidate)
            if file_digest(auxiliary) != (candidate["provenance"]["bytes"], candidate["provenance"]["sha256"]):
                raise RuntimeError("Published provenance differs")
            return candidate
        with closing(sqlite3.connect(work)) as db:
            db.execute("PRAGMA cache_size=-16384")
            db.execute("PRAGMA journal_mode=DELETE"); db.execute("PRAGMA synchronous=FULL")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            if not tables:
                with closing(sqlite3.connect(":memory:")) as blank:
                    schema.create_schema(blank); blank.backup(db)
            schema.assert_schema(db); db.execute("PRAGMA foreign_keys=ON")
            db.execute("ATTACH DATABASE ? AS progress", (str(output / "progress.sqlite"),))
            db.execute("PRAGMA progress.journal_mode=DELETE"); db.execute("PRAGMA progress.synchronous=FULL")
            db.execute("BEGIN")
            db.execute("CREATE TABLE IF NOT EXISTS progress.derivations(source_sha256 TEXT PRIMARY KEY,blob_id INTEGER NOT NULL,payload_sha256 BLOB NOT NULL,source_bytes INTEGER NOT NULL) WITHOUT ROWID")
            db.execute("CREATE TABLE IF NOT EXISTS progress.checkpoint(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            record = db.execute("SELECT value FROM progress.checkpoint WHERE key='state'").fetchone()
            if record:
                state = json.loads(record[0])
                if state["identity"] != identity:
                    raise RuntimeError("Checkpoint source/profile/dimensions/tool/helper/codec identity differs")
            else:
                if db.execute("SELECT count(*) FROM blobs").fetchone()[0] or db.execute("SELECT count(*) FROM runtime_assets").fetchone()[0]:
                    raise RuntimeError("Runtime payloads exist without their atomic progress checkpoint")
                state = {"identity": identity, "last_sha256": "", "payloads": 0}
                for key in ("runtime_available", "runtime_catalogs", "upstream_exceptions", "bundle_complete", "expected_kitchen_count"):
                    metadata_write(db, key, source_metadata[key])
                db.execute("INSERT INTO progress.checkpoint VALUES ('state',?)", (json.dumps(state, sort_keys=True),))
            db.commit()
            identities = {}
            for name, kind, old_sha, old_format in source.execute("SELECT * FROM runtime_assets"):
                if not schema.IDENTITY.fullmatch(name) or not name.startswith("k:" if kind == "kitchen" else "g:"):
                    raise RuntimeError("Original runtime identity differs")
                item = identities.setdefault(old_sha, {"kind": kind, "format": old_format, "names": []})
                if (item["kind"], item["format"]) != (kind, old_format):
                    raise RuntimeError("Source hash crosses kind/format boundaries")
                item["names"].append(name)
            if db.execute("SELECT count(*) FROM progress.derivations").fetchone()[0] != state["payloads"]:
                raise RuntimeError("Atomic progress count differs")
            digests = {digest: (blob_id, fmt) for blob_id, digest, fmt in db.execute("SELECT id,sha256,format FROM blobs")}
            next_id = max((v[0] for v in digests.values()), default=0) + 1
            def rows():
                for sha in sorted(identities):
                    if sha > state["last_sha256"]:
                        raw, width, height = source.execute("SELECT data,width,height FROM blobs WHERE sha256=?", (sha,)).fetchone()
                        item = identities[sha]; yield sha, item["kind"], item["format"], raw, width, height
            inputs = iter(rows()); started = time.monotonic()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pending = deque(); exhausted = False
                while pending or not exhausted:
                    while len(pending) < workers * 2 and not exhausted:
                        row = next(inputs, None)
                        if row is None: exhausted = True
                        else: pending.append(pool.submit(encode, row, dimension))
                    if not pending: continue
                    old_sha, count, digest, fmt, codec, decoded_count, stored, width, height = pending.popleft().result()
                    if digest in digests:
                        blob_id, known_format = digests[digest]
                        if known_format != fmt: raise RuntimeError("Response digest crosses formats")
                    else:
                        blob_id = next_id; next_id += 1; digests[digest] = (blob_id, fmt)
                        db.execute("INSERT INTO blobs VALUES (?,?,?,?,?,?,?,?)", (blob_id, digest, fmt, codec, decoded_count, stored, width, height))
                    db.execute("INSERT INTO progress.derivations VALUES (?,?,?,?)", (old_sha, blob_id, digest, count))
                    db.executemany("INSERT INTO runtime_assets VALUES (?,?)", ((name, blob_id) for name in identities[old_sha]["names"]))
                    state.update(last_sha256=old_sha, payloads=state["payloads"] + 1)
                    if state["payloads"] % 256 == 0:
                        db.execute("UPDATE progress.checkpoint SET value=? WHERE key='state'", (json.dumps(state, sort_keys=True),)); db.commit()
                        print(json.dumps({"committed_payloads": state["payloads"], "elapsed_seconds": round(time.monotonic()-started, 1)}), flush=True)
            if (state["payloads"] != len(identities)
                    or identity_digest(db.execute("SELECT identity FROM runtime_assets ORDER BY identity")) != original_identity
                    or db.execute("PRAGMA foreign_key_check").fetchall()
                    or db.execute("SELECT count(*) FROM progress.derivations d LEFT JOIN blobs b ON d.blob_id=b.id WHERE b.id IS NULL OR b.sha256!=d.payload_sha256").fetchone()[0]):
                raise RuntimeError("Complete runtime identities, provenance joins or foreign keys differ")
            metadata_write(db, "optimization", json.dumps(identity, sort_keys=True))
            metadata_write(db, "policy", "Complete local runtime; exact Google SVG and resized lossless WebP; acquisition provenance in separate compressed sidecar; no remote fallback")
            db.execute("UPDATE progress.checkpoint SET value=? WHERE key='state'", (json.dumps(state, sort_keys=True),)); db.commit()
            acquisitions, mappings = provenance(source, db, pending_auxiliary, identity, expected)
    auxiliary_bytes, auxiliary_sha = file_digest(pending_auxiliary)
    pack_bytes, pack_sha = file_digest(work)
    candidate = dict(expected)
    candidate.update(runtime_schema=2, kitchen_format="webp", runtime_identity_sha256=original_identity,
                     pack_bytes=pack_bytes, pack_sha256=pack_sha, optimization=identity,
                     provenance={"filename": auxiliary.name, "bytes": auxiliary_bytes, "sha256": auxiliary_sha,
                                 "original_records": acquisitions, "derivations": mappings},
                     policy="Runtime-only integer-ID SQLite, lossless WebP and exact zlib SVG; full acquisition provenance separately preserved")
    verify(work, candidate)
    for staged, final in ((work, destination), (pending_auxiliary, auxiliary)):
        if final.exists():
            if not resume or file_digest(final) != file_digest(staged):
                raise RuntimeError("Preserve conflicting published candidate")
        else: final.hardlink_to(staged)
    pending_manifest = output / "bundle-manifest.pending.json"
    with pending_manifest.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(candidate, indent=2) + "\n"); stream.flush(); os.fsync(stream.fileno())
    candidate_manifest.hardlink_to(pending_manifest)
    pending_manifest.unlink(); pending_auxiliary.unlink(); work.unlink()
    print(json.dumps({"candidate_bytes": pack_bytes, "candidate_sha256": pack_sha,
                      "provenance_bytes": auxiliary_bytes, "provenance_sha256": auxiliary_sha}), flush=True)
    return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("pack", "manifest", "output"): parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--dimension", type=int, choices=(128, 160), default=160)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    derive(args.pack.resolve(), args.manifest.resolve(), args.output.resolve(), workers=args.workers, resume=args.resume, dimension=args.dimension)

if __name__ == "__main__": main()

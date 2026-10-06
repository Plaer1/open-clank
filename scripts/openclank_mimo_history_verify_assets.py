#!/usr/bin/env python3
"""Verify D09 source asset bytes and their owner-scoped archive metadata."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import unquote_to_bytes


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def decoded_data_url(url: str) -> bytes:
    try:
        metadata, payload = url[5:].split(",", 1)
        raw = unquote_to_bytes(payload)
        if any(item.lower() == "base64" for item in metadata.split(";")[1:]):
            return base64.b64decode(raw, validate=True)
        return raw
    except (ValueError, binascii.Error) as exc:
        raise SystemExit("invalid inline data URL in a selected source") from exc


def source_audit(path: Path) -> tuple[dict[str, set[str]], list[dict[str, object]], dict[str, object]]:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        ids = {
            table: {str(row[0]) for row in conn.execute(f"SELECT id FROM {table}")}
            for table in ("session", "message", "part")
        }
        assets: list[dict[str, object]] = []
        for part_id, raw in conn.execute("SELECT id, data FROM part WHERE json_valid(data)"):
            item = json.loads(raw)
            if not isinstance(item, dict) or item.get("type") != "file":
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url.lower().startswith("data:"):
                continue
            blob = decoded_data_url(url)
            assets.append(
                {
                    "asset_id": f"mimo-file:{part_id}",
                    # The importer deliberately hashes the exact URL string;
                    # full source JSON keeps that URL and inline bytes losslessly.
                    "archive_content_hash": "sha256:" + hashlib.sha256(url.encode()).hexdigest(),
                    "decoded_content_hash": "sha256:" + hashlib.sha256(blob).hexdigest(),
                    "decoded_byte_size": len(blob),
                }
            )
    finally:
        conn.close()
    digest = hashlib.sha256()
    for asset in sorted(assets, key=lambda item: str(item["asset_id"])):
        digest.update(str(asset["asset_id"]).encode())
        digest.update(b"\0")
        digest.update(str(asset["decoded_content_hash"]).encode())
        digest.update(b"\0")
        digest.update(str(asset["decoded_byte_size"]).encode())
        digest.update(b"\n")
    summary = {
        "source": str(path),
        "source_sha256": sha256_file(path),
        "row_counts": {table: len(values) for table, values in ids.items()},
        "inline_file_assets": len(assets),
        "decoded_inline_bytes": sum(int(item["decoded_byte_size"]) for item in assets),
        "asset_verification_sha256": "sha256:" + digest.hexdigest(),
    }
    return ids, assets, summary


def write_new_json(path: Path, payload: object) -> None:
    if path.exists():
        raise SystemExit(f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    sources = [Path(value) for value in args.source]
    if len(sources) != 2:
        raise SystemExit("D09 requires exactly the two selected sources")
    audits = [source_audit(path) for path in sources]
    intersections = {
        table: len(audits[0][0][table] & audits[1][0][table])
        for table in ("session", "message", "part")
    }
    if any(intersections.values()):
        raise SystemExit(f"selected source IDs overlap: {intersections}")
    expected = {str(item["asset_id"]): item for _, assets, _ in audits for item in assets}
    conn = sqlite3.connect(f"file:{Path(args.archive).resolve()}?mode=ro", uri=True)
    try:
        actual = {
            str(asset_id): {"content_hash": content_hash, "byte_size": byte_size}
            for asset_id, content_hash, byte_size in conn.execute(
                "SELECT asset_id, content_hash, byte_size FROM conversation_part_assets "
                "WHERE owner='e' AND asset_id LIKE 'mimo-file:%'"
            )
        }
    finally:
        conn.close()
    mismatches = {
        asset_id: {"expected": value, "actual": actual.get(asset_id)}
        for asset_id, value in expected.items()
        if actual.get(asset_id) != {
            "content_hash": value["archive_content_hash"],
            "byte_size": None,
        }
    }
    if mismatches:
        raise SystemExit(f"archive asset verification failed for {len(mismatches)} assets")
    write_new_json(
        Path(args.output),
        {
            "schema": "open-clank-d09-inline-asset-verification/v1",
            "owner": "e",
            "source_id_intersections": intersections,
            "sources": [summary for _, _, summary in audits],
            "verified_assets": len(expected),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

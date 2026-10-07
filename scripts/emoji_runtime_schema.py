"""Compact offline artwork schema; stdlib, exact bounded response-byte codecs."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import struct
import xml.etree.ElementTree as ET
import zlib

SCHEMA_VERSION = 2
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
IDENTITY = re.compile(r"^[gk]:[0-9a-f]+(?:-[0-9a-f]+)*(?:_[0-9a-f]+(?:-[0-9a-f]+)*)?$")
MIME = {"svg": "image/svg+xml", "webp": "image/webp"}


def create_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    PRAGMA page_size=1024;
    PRAGMA user_version=2;
    PRAGMA foreign_keys=ON;
    CREATE TABLE blobs(id INTEGER PRIMARY KEY, sha256 BLOB NOT NULL CHECK(length(sha256)=32),
      format TEXT NOT NULL CHECK(format IN ('svg','webp')), codec INTEGER NOT NULL,
      decoded_bytes INTEGER NOT NULL CHECK(decoded_bytes>0), data BLOB NOT NULL,
      width REAL, height REAL,
      CHECK((format='svg' AND codec=1) OR (format='webp' AND codec=0)));
    CREATE TABLE runtime_assets(identity TEXT PRIMARY KEY,
      blob_id INTEGER NOT NULL REFERENCES blobs(id)) WITHOUT ROWID;
    CREATE TABLE metadata(key TEXT PRIMARY KEY, value BLOB NOT NULL,
      codec INTEGER NOT NULL CHECK(codec=1), decoded_bytes INTEGER NOT NULL CHECK(decoded_bytes>0));
    """)


def assert_schema(db: sqlite3.Connection) -> None:
    columns = {"blobs": ("id", "sha256", "format", "codec", "decoded_bytes", "data", "width", "height"),
               "runtime_assets": ("identity", "blob_id"), "metadata": ("key", "value", "codec", "decoded_bytes")}
    if (db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
            or db.execute("PRAGMA page_size").fetchone()[0] != 1024
            or {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")} != set(columns)):
        raise ValueError("unsupported offline artwork runtime schema")
    for table, names in columns.items():
        if tuple(r[1] for r in db.execute(f"PRAGMA table_info({table})")) != names:
            raise ValueError("offline artwork runtime columns differ")
    if [(r[2], r[3], r[4]) for r in db.execute("PRAGMA foreign_key_list(runtime_assets)")] != [("blobs", "blob_id", "id")]:
        raise ValueError("offline artwork runtime foreign key differs")


def encode_payload(data: bytes, codec: int, *, limit: int = MAX_IMAGE_BYTES) -> bytes:
    if not isinstance(data, bytes) or not 0 < len(data) <= limit or codec not in (0, 1):
        raise ValueError("invalid offline artwork payload")
    return zlib.compress(data, 6) if codec == 1 else data


def decode_payload(data: bytes, codec: int, decoded_bytes: int, *, limit: int = MAX_IMAGE_BYTES) -> bytes:
    if (not isinstance(data, bytes) or type(decoded_bytes) is not int
            or not 0 < decoded_bytes <= limit or len(data) > limit + 65536 or codec not in (0, 1)):
        raise ValueError("invalid offline artwork codec or size")
    if codec == 0:
        result = data
    else:
        decoder = zlib.decompressobj()
        try:
            result = decoder.decompress(data, decoded_bytes + 1)
        except zlib.error as error:
            raise ValueError("invalid compressed offline artwork") from error
        if not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
            raise ValueError("compressed offline artwork stream is incomplete or has trailing bytes")
    if len(result) != decoded_bytes:
        raise ValueError("offline artwork decoded length differs")
    return result


def read_metadata(db: sqlite3.Connection) -> dict[str, str]:
    result = {}
    for key, value, codec, count in db.execute("SELECT key,value,codec,decoded_bytes FROM metadata"):
        if codec != 1:
            raise ValueError("offline artwork metadata must use exact zlib")
        result[key] = decode_payload(value, codec, count, limit=MAX_METADATA_BYTES).decode("utf-8")
    return result


def webp_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 25 or data[:4] != b"RIFF" or data[8:12] != b"WEBP" or struct.unpack("<I", data[4:8])[0] != len(data) - 8:
        raise ValueError("invalid WebP container")
    position = 12
    dimensions = None
    while position < len(data):
        if position + 8 > len(data):
            raise ValueError("incomplete WebP chunk")
        tag = data[position:position + 4]
        size = struct.unpack("<I", data[position + 4:position + 8])[0]
        start = position + 8
        end = start + size
        if end + (size & 1) > len(data) or tag == b"VP8 ":
            raise ValueError("invalid or lossy WebP chunk")
        if tag == b"VP8L":
            if dimensions or size < 5 or data[start] != 0x2f:
                raise ValueError("invalid lossless WebP header")
            bits = int.from_bytes(data[start + 1:start + 5], "little")
            if bits >> 29:
                raise ValueError("unsupported lossless WebP version")
            dimensions = (1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff))
        position = end + (size & 1)
    if dimensions is None:
        raise ValueError("lossless WebP image chunk is missing")
    return dimensions


def decoded_blob(row: tuple, *, max_dimension: int | None = None) -> tuple[bytes, str, str]:
    # SELECT sha256,format,codec,decoded_bytes,data,width,height FROM blobs.
    digest, format_name, codec, count, stored, width, height = row
    if (format_name, codec) not in (("svg", 1), ("webp", 0)) or not isinstance(digest, bytes) or len(digest) != 32:
        raise ValueError("invalid offline artwork format or digest")
    data = decode_payload(stored, codec, count)
    if hashlib.sha256(data).digest() != digest:
        raise ValueError("offline artwork response SHA-256 differs")
    if format_name == "svg":
        try:
            tag = ET.fromstring(data).tag.rsplit("}", 1)[-1]
        except ET.ParseError as error:
            raise ValueError("invalid offline artwork SVG") from error
        if tag != "svg":
            raise ValueError("offline artwork SVG root differs")
    else:
        dimensions = webp_dimensions(data)
        if dimensions != (width, height) or (max_dimension is not None and max(dimensions) > max_dimension):
            raise ValueError("offline artwork WebP dimensions differ")
    return data, MIME[format_name], digest.hex()

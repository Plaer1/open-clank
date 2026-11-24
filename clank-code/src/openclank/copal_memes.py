"""Validation and transformation for the native Copal Wiki interchange format."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import unicodedata
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any


MEMES_FORMAT = "copal-memes"
MEMES_SCHEMA_VERSION = 1
MEMES_MIME = "application/vnd.openclank.memes+json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_DOCUMENTS = 10_000
_MAX_ASSETS = 10_000
_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_ASSET_BYTES = 512 * 1024 * 1024
_MAX_ASSET_TOTAL_BYTES = 1024 * 1024 * 1024
_MIME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")


class MemesValidationError(ValueError):
    """A client-correctable `.memes` validation failure."""


def _fail(message: str) -> None:
    raise MemesValidationError(message)


def _json_loads(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, parse_constant=lambda token: _fail(f"JSON constant {token} is not allowed"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MemesValidationError(".memes must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        _fail(".memes root must be an object")
    return value


def _safe_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        _fail(f"{label} must be a non-empty UTF-8 path")
    normalized = PurePosixPath(value.replace("\\", "/")).as_posix()
    path = PurePosixPath(normalized)
    if normalized != value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or any(ord(char) < 0x20 for char in value):
        _fail(f"{label} must be a relative portable path")
    return value


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail(f"{label} must be a portable resource identifier")
    return value


def _decode_base64(value: Any, label: str, max_bytes: int = _MAX_ASSET_BYTES) -> bytes:
    if not isinstance(value, str):
        _fail(f"{label} must be base64 text")
    if len(value) > ((max_bytes + 2) // 3) * 4:
        _fail(f"{label} exceeds the decoded byte limit")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise MemesValidationError(f"{label} is not valid base64") from exc
    if base64.b64encode(decoded).decode("ascii") != value:
        _fail(f"{label} must use canonical base64")
    return decoded


def _digest(value: Any, data: bytes, label: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value) or value != hashlib.sha256(data).hexdigest():
        _fail(f"{label} does not match the supplied bytes")


def _json_finite(value: Any, label: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise MemesValidationError(f"{label} is not finite JSON") from exc


def _validate_raw_source(raw_source: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw_source, dict) or set(raw_source) != {"encoding", "base64", "sha256"}:
        _fail(f"{label} must contain encoding, base64, and sha256")
    if raw_source["encoding"] != "utf-8":
        _fail(f"{label}.encoding must be utf-8")
    data = _decode_base64(raw_source["base64"], f"{label}.base64", _MAX_SOURCE_BYTES)
    _digest(raw_source["sha256"], data, f"{label}.sha256")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MemesValidationError(f"{label} is not valid UTF-8") from exc
    return raw_source


def _portable_name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def validate_memes_payload(raw: bytes) -> dict[str, Any]:
    """Validate and return a detached native `.memes` envelope."""
    value = _json_loads(raw)
    if set(value) != {"format", "schemaVersion", "documents", "assets", "extensions"}:
        _fail(".memes envelope keys are not exactly format/schemaVersion/documents/assets/extensions")
    if value["format"] != MEMES_FORMAT or value["schemaVersion"] != MEMES_SCHEMA_VERSION:
        _fail("unsupported .memes format or schema version")
    documents = value["documents"]
    assets = value["assets"]
    extensions = value["extensions"]
    if not isinstance(documents, list) or len(documents) > _MAX_DOCUMENTS:
        _fail(".memes documents must be a bounded array")
    if not isinstance(assets, list) or len(assets) > _MAX_ASSETS:
        _fail(".memes assets must be a bounded array")
    if not isinstance(extensions, dict):
        _fail(".memes extensions must be an object")
    if any(key in extensions for key in ("owner", "workspace", "workspace_id", "account", "account_id")):
        _fail(".memes cannot grant authority through extensions")
    expected_heads = extensions.get("expectedHeads")
    if expected_heads is not None:
        if not isinstance(expected_heads, dict) or any(
            not isinstance(document_id, str)
            or not _ID.fullmatch(document_id)
            or not isinstance(head, str)
            or not head
            or len(head) > 256
            for document_id, head in expected_heads.items()
        ):
            _fail(".memes extensions.expectedHeads must map portable IDs to non-empty heads")

    seen_resource_ids: set[str] = set()
    seen_resource_names: set[str] = set()
    for index, document in enumerate(documents):
        label = f"documents[{index}]"
        if not isinstance(document, dict) or set(document) - {"exportId", "name", "kind", "record", "rawSource"}:
            _fail(f"{label} has unsupported fields")
        export_id = _safe_id(document.get("exportId"), f"{label}.exportId")
        if export_id in seen_resource_ids:
            _fail(f"duplicate document exportId: {export_id}")
        seen_resource_ids.add(export_id)
        document_name = _safe_name(document.get("name"), f"{label}.name")
        document_name_key = _portable_name_key(document_name)
        if document_name_key in seen_resource_names:
            _fail(f"duplicate document name: {document_name}")
        seen_resource_names.add(document_name_key)
        if document.get("kind") != "wiki":
            _fail(f"{label}.kind must be wiki")
        if not isinstance(document.get("record"), dict):
            _fail(f"{label}.record must be an object")
        _json_finite(document["record"], f"{label}.record")
        if "rawSource" in document:
            _validate_raw_source(document["rawSource"], f"{label}.rawSource")

    estimated_asset_total = 0
    decoded_asset_total = 0
    for index, asset in enumerate(assets):
        label = f"assets[{index}]"
        expected = {"assetId", "name", "mime", "byteLength", "sha256", "base64"}
        if not isinstance(asset, dict) or set(asset) != expected:
            _fail(f"{label} fields are invalid")
        asset_id = _safe_id(asset["assetId"], f"{label}.assetId")
        name = _safe_name(asset["name"], f"{label}.name")
        name_key = _portable_name_key(name)
        if asset_id in seen_resource_ids or name_key in seen_resource_names:
            _fail(f"duplicate asset identity: {asset_id} or {name}")
        seen_resource_ids.add(asset_id)
        seen_resource_names.add(name_key)
        if not isinstance(asset["mime"], str) or not _MIME.fullmatch(asset["mime"]):
            _fail(f"{label}.mime is invalid")
        encoded = asset["base64"]
        if not isinstance(encoded, str) or len(encoded) > ((_MAX_ASSET_BYTES + 2) // 3) * 4:
            _fail(f"{label}.base64 exceeds the decoded byte limit")
        estimated_asset_total += (len(encoded) * 3) // 4
        if estimated_asset_total > _MAX_ASSET_TOTAL_BYTES:
            _fail(".memes assets exceed the aggregate byte limit")
        data = _decode_base64(asset["base64"], f"{label}.base64", _MAX_ASSET_BYTES)
        if len(data) > _MAX_ASSET_BYTES:
            _fail(f"{label} exceeds the per-asset byte limit")
        decoded_asset_total += len(data)
        if decoded_asset_total > _MAX_ASSET_TOTAL_BYTES:
            _fail(".memes assets exceed the aggregate byte limit")
        if not isinstance(asset["byteLength"], int) or isinstance(asset["byteLength"], bool) or asset["byteLength"] != len(data):
            _fail(f"{label}.byteLength does not match the supplied bytes")
        _digest(asset["sha256"], data, f"{label}.sha256")
    _json_finite(value, ".memes envelope")
    return deepcopy(value)


def raw_source(data: bytes) -> dict[str, str]:
    """Build immutable UTF-8 provenance for a native record byte string."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MemesValidationError("native Wiki source is not UTF-8") from exc
    if len(data) > _MAX_SOURCE_BYTES:
        raise MemesValidationError("native Wiki source exceeds the source limit")
    return {
        "encoding": "utf-8",
        "base64": base64.b64encode(data).decode("ascii"),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def record_bytes(record: dict[str, Any]) -> bytes:
    _json_finite(record, "native Wiki record")
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def remap_record(value: Any, document_ids: dict[str, str], asset_ids: dict[str, str], key: str = "") -> Any:
    """Remap known native relationship/asset references without dropping unknown fields."""
    if isinstance(value, list):
        return [remap_record(item, document_ids, asset_ids, key) for item in value]
    if not isinstance(value, dict):
        if isinstance(value, str):
            lowered = key.casefold()
            if lowered in {"assetid", "asset_id", "targetassetid", "target_asset_id", "assetdocumentid"}:
                return asset_ids.get(value, value)
            if lowered in {"documentid", "document_id", "targetdocumentid", "target_document_id", "sourcedocumentid", "source_document_id"}:
                return document_ids.get(value, value)
        return value
    return {name: remap_record(item, document_ids, asset_ids, name) for name, item in value.items()}


def mime_for_name(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"

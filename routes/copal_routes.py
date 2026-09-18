"""User-scoped Open Clank API for Copal's owned Redb bridge."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import math
import mimetypes
import re
import stat
import tempfile
import time
import unicodedata
import uuid
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Mapping
from urllib.parse import quote

import yaml

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from core.middleware import require_admin
from src.auth_helpers import copal_owner_for_user, require_user
from src.openclank.copal_bridge import CopalBridgeError
from src.openclank.file_policy import FilePolicyRepository
from src.openclank.history_client import HistoryClient, HistoryClientError
from src.openclank.copal_resources import copal_resource_descriptor
from src.openclank.copal_memes import (
    MEMES_FORMAT,
    MEMES_MIME,
    MEMES_SCHEMA_VERSION,
    MemesValidationError,
    mime_for_name,
    raw_source,
    record_bytes,
    remap_record,
    validate_memes_payload,
)
from src.openclank.copal_calendar_projection import reconcile_projection
from src.openclank.copal_planning import (
    EVENT_KIND,
    MIGRATION_KIND,
    MIGRATION_NAME,
    TRACKS_KIND,
    TRACKS_NAME,
    PlanningValidationError,
    canonical_documents,
    event_document_name,
    event_from_document,
    legacy_inventory,
    merge_event,
    planning_projection,
    revision_fingerprint,
    serialize_event,
    serialize_track_registry,
    track_preorder,
    track_registry_from_document,
    validate_event,
)
from src.openclank.copal_bases import (
    BaseDefinitionError,
    canonical_base_property,
    dump_base_definition,
    parse_base_definition,
    query_base,
    set_frontmatter_property,
    transform_base_definition,
)
from src.upload_limits import COPAL_IMPORT_MAX_BYTES, copy_upload_limited, read_upload_limited


logger = logging.getLogger(__name__)


_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_WORKSPACE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SESSION_COOKIE = "odysseus_session"
_KIND = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_NOTE_PROPERTY = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_NOTE_LINK = re.compile(r"(!?)\[\[([^\]\n]+)\]\]")
_NOTE_TAG = re.compile(r"(?<![\w/])#([\w][\w/-]*)", re.UNICODE)
_NOTE_KIND = "note"
_WIKI_KIND = "wiki"
_NOTE_KINDS = {_NOTE_KIND, _WIKI_KIND}
_OPERATION_KIND = "copal-operation"
_CONTROL_DOCUMENT_KINDS = {
    EVENT_KIND,
    TRACKS_KIND,
    MIGRATION_KIND,
    _OPERATION_KIND,
    "treehouse-state",
    "calendar-projection",
}
_NOTE_SCHEMA_VERSION = 1
_NOTE_MAX_PROPERTIES_BYTES = 262_144
_COPAL_IMPORT_MAX_FILES = 10_000
_COPAL_IMPORT_MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
_COPAL_IMPORT_MAX_MEMBER_BYTES = 512 * 1024 * 1024
_COPAL_IMPORT_MAX_COMPRESSION_RATIO = 250
_ATTACHMENT_LIFECYCLE_PREFIX = "__copal_attachment_lifecycle__"
_ATTACHMENT_PENDING_RETENTION_SECONDS = 60 * 60
_DOCUMENT_ACTION_PREFIX = ".copal/document-actions/"


def _is_asset_kind(kind: Any) -> bool:
    return str(kind or "") == "asset"


def _is_compatibility_kind(kind: Any) -> bool:
    return str(kind or "") == "compatibility"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateDocument(_StrictModel):
    actionId: str | None = Field(default=None, min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=512)
    kind: str = Field(default=_NOTE_KIND, min_length=1, max_length=64)
    content: str = Field(default="", max_length=8_388_608)
    properties: dict[str, Any] = Field(default_factory=dict, max_length=256)
    relations: list[dict[str, Any]] = Field(default_factory=list, max_length=10_000)
    corpus: str = Field(default="notes", pattern="^(notes|wiki)$")


class WriteDocument(_StrictModel):
    actionId: str | None = Field(default=None, min_length=1, max_length=160)
    content: str = Field(max_length=8_388_608)
    base: str | None = Field(default=None, max_length=128)
    properties: dict[str, Any] | None = Field(default=None, max_length=256)
    relations: list[dict[str, Any]] | None = Field(default=None, max_length=10_000)


class ConvertDocument(_StrictModel):
    actionId: str | None = Field(default=None, min_length=1, max_length=160)
    base: str | None = Field(default=None, max_length=128)


class AttachmentMutation(_StrictModel):
    actionId: str = Field(min_length=1, max_length=160)
    documentId: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=512)
    mime: str = Field(default="application/octet-stream", max_length=160)
    contentBase64: str = Field(min_length=1, max_length=67_108_864)
    content: str | None = Field(default=None, max_length=8_388_608)
    base: str | None = Field(default=None, max_length=128)
    caption: str = Field(default="", max_length=512)
    prepareOnly: bool = False
    sourceTextHash: str | None = Field(default=None, max_length=128)


class AttachmentCommit(_StrictModel):
    actionId: str = Field(min_length=1, max_length=160)
    documentId: str = Field(min_length=1, max_length=128)
    content: str = Field(max_length=8_388_608)
    base: str = Field(min_length=1, max_length=128)
    sourceTextHash: str = Field(min_length=1, max_length=128)
    assetId: str = Field(min_length=1, max_length=128)
    assetName: str = Field(min_length=1, max_length=512)


class RenameDocument(_StrictModel):
    actionId: str | None = Field(default=None, min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=512)


class CheckpointDocument(_StrictModel):
    message: str | None = Field(default=None, max_length=512)


class RestoreDocument(_StrictModel):
    actionId: str | None = Field(default=None, min_length=1, max_length=160)
    commit: str = Field(min_length=1, max_length=128)


class ReconcileCalendar(_StrictModel):
    document_id: str | None = Field(default=None, max_length=128)


class ValidateBase(_StrictModel):
    content: str = Field(max_length=262_144)


class MigrateBase(_StrictModel):
    base: str | None = Field(default=None, max_length=128)


class TransformBase(_StrictModel):
    """A typed, source-preserving Base gesture.

    ``apply`` is deliberately opt-in: the default operation is a pure
    preview, so a stale UI gesture cannot write merely because it was sent to
    the server for validation.
    """

    actionId: str | None = Field(default=None, min_length=1, max_length=160)
    command: dict[str, Any] = Field(default_factory=dict, max_length=32)
    source: str | None = Field(default=None, max_length=262_144)
    base: str | None = Field(default=None, max_length=128)
    apply: bool = False


class BaseQueryPreview(_StrictModel):
    """Query an authenticated Base draft without persisting its source."""

    source: str = Field(max_length=262_144)
    base: str | None = Field(default=None, max_length=128)
    definitionRevision: int | None = Field(default=None, ge=0)


class EditBaseRow(_StrictModel):
    property: str = Field(min_length=1, max_length=128)
    value: Any = None
    base: str | None = Field(default=None, max_length=128)
    actionId: str | None = Field(default=None, min_length=1, max_length=160)
    clear: bool = False


class EventMutation(_StrictModel):
    patch: dict[str, Any] = Field(default_factory=dict)
    base: str | None = Field(default=None, max_length=128)


class CreateEvent(_StrictModel):
    event: dict[str, Any] = Field(default_factory=dict)


class TrackMutation(_StrictModel):
    tracks: list[dict[str, Any]]
    metadata: dict[str, Any] = Field(default_factory=dict)
    base: str | None = Field(default=None, max_length=128)


class PlanningMigration(_StrictModel):
    action: str = Field(default="apply", pattern="^(apply|rollback)$")


class TaskMutation(_StrictModel):
    actionId: str | None = Field(default=None, min_length=1, max_length=160)
    operationId: str | None = Field(default=None, min_length=1, max_length=160)
    resourceKey: dict[str, Any] = Field(default_factory=dict, max_length=16)
    taskId: str = Field(min_length=1, max_length=256)
    expectedRevision: dict[str, Any] | str | None = None
    anchor: dict[str, Any] = Field(default_factory=dict, max_length=16)
    checked: bool


class TaskCreate(_StrictModel):
    actionId: str | None = Field(default=None, min_length=1, max_length=160)
    operationId: str | None = Field(default=None, min_length=1, max_length=160)
    resourceKey: dict[str, Any] = Field(default_factory=dict, max_length=16)
    expectedRevision: dict[str, Any] | str | None = None
    text: str = Field(min_length=1, max_length=4096)


def _task_source_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _task_source_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Project every Markdown checkbox without inventing a second task store."""
    lines = str(document.get("text") or "").split("\n")
    blocks = document.get("blocks") if isinstance(document.get("blocks"), list) else []
    rows: list[dict[str, Any]] = []
    occurrences: defaultdict[str, int] = defaultdict(int)
    heading_path: list[str] = []
    offset = 0
    for line_index, source in enumerate(lines):
        heading = re.match(r"^(#{1,6})\s+(.*)$", source)
        if heading:
            level = len(heading.group(1))
            heading_path = [*heading_path[:level - 1], heading.group(2).strip()]
        match = re.match(r"^(?P<indent>\s*)(?P<marker>[-*+])\s+\[(?P<checked>[ xX])\](?P<tail>.*)$", source)
        if not match:
            offset += len(source) + 1
            continue
        block = blocks[line_index] if line_index < len(blocks) and isinstance(blocks[line_index], dict) else {}
        block_id = block.get("id") if isinstance(block.get("id"), str) else None
        fingerprint = _task_source_hash(source)
        occurrence = occurrences[fingerprint]
        occurrences[fingerprint] += 1
        stable_block = block_id or f"src-{fingerprint[:24]}-{occurrence}"
        rows.append({
            "blockId": stable_block,
            "line": line_index + 1,
            "source": source,
            "sourceRange": {"from": offset, "to": offset + len(source)},
            "expectedTextHash": fingerprint,
            "done": match.group("checked").lower() == "x",
            "text": match.group("tail").lstrip(),
            "headingPath": list(heading_path),
        })
        offset += len(source) + 1
    return rows


def _bridge(request: Request):
    bridge = getattr(request.app.state, "copal_bridge", None)
    if not bridge:
        raise HTTPException(503, "Copal storage bridge is unavailable")
    return bridge


def _workspace(request: Request, value: str | None = None) -> str:
    workspace = (value or request.headers.get("X-Copal-Workspace") or "default").strip()
    if not _WORKSPACE.fullmatch(workspace):
        raise HTTPException(400, "Invalid Copal workspace")
    return workspace


def _scope(request: Request, workspace: str | None = None) -> dict[str, str]:
    expected_actor = request.headers.get("X-Copal-Account")
    if expected_actor and expected_actor != _actor_account_id(request):
        raise HTTPException(409, detail={"outcome": "stale_session", "message": "The editing account changed. Reopen this document."})
    return {
        "owner": copal_owner_for_user(require_user(request)),
        "workspace_id": _workspace(request, workspace),
    }


def _actor_account_id(request: Request) -> str | None:
    username = require_user(request)
    if not username:
        return "local-installation"
    manager = getattr(request.app.state, "auth_manager", None)
    if manager is not None and hasattr(manager, "account_id"):
        return manager.account_id(username)
    return None


def _resource_view(request: Request, scope: dict[str, str], document: dict[str, Any]) -> dict[str, Any]:
    """Attach server-issued equality metadata without changing access authority."""
    result = _note_view(document)
    account_id = _actor_account_id(request)
    # Legacy embedders without immutable account metadata can keep using their
    # existing routes, but must not mint a cross-app identity from a username.
    if not account_id or not result.get("id"):
        return result
    return {
        **result,
        "resource": copal_resource_descriptor(
            result, owner_account_id=str(account_id), workspace_id=scope["workspace_id"],
        ),
    }


def _stream_owner_is_current(
    request: Request,
    authenticated_owner: str,
    session_token: str | None,
) -> bool:
    if not authenticated_owner:
        return True
    auth_manager = getattr(request.app.state, "auth_manager", None)
    if auth_manager is None or not session_token:
        return False
    try:
        return auth_manager.get_username_for_token(session_token) == authenticated_owner
    except Exception:
        return False


def _doc_id(value: str) -> str:
    if not _ID.fullmatch(value):
        raise HTTPException(400, "Invalid document ID")
    return value


def _name(value: str) -> str:
    name = value.strip().replace("\\", "/")
    path = PurePosixPath(name)
    if not name or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise HTTPException(400, "Invalid document name")
    return str(path)


def _note_properties(values: dict[str, Any] | None) -> dict[str, Any]:
    properties = dict(values or {})
    for key in properties:
        if not isinstance(key, str) or not _NOTE_PROPERTY.fullmatch(key):
            raise HTTPException(422, f"Invalid note property name: {key!r}")
    try:
        encoded = json.dumps(properties, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise HTTPException(422, "Note properties must be finite JSON values") from exc
    if len(encoded.encode("utf-8")) > _NOTE_MAX_PROPERTIES_BYTES:
        raise HTTPException(413, "Note properties exceed the 256 KiB limit")
    return properties


def _record_id(prefix: str, deterministic_seed: str | None = None) -> str:
    if deterministic_seed is not None:
        digest = hashlib.sha256(deterministic_seed.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}_{digest}"
    return f"{prefix}_{uuid.uuid4().hex}"


def _block_from_line(line: str) -> dict[str, Any]:
    block: dict[str, Any]
    if not line:
        block = {"type": "blank", "text": ""}
    elif match := re.fullmatch(r"(#{1,6})\s+(.*)", line):
        block = {"type": "heading", "level": len(match.group(1)), "text": match.group(2)}
    elif match := re.fullmatch(r"(\s*)([-*+])\s+\[([ xX])\]\s*(.*)", line):
        block = {"type": "task", "indent": len(match.group(1)), "marker": match.group(2), "checked": match.group(3).lower() == "x", "text": match.group(4)}
    elif match := re.fullmatch(r"(\s*)[-*+]\s+(.*)", line):
        block = {"type": "bullet", "indent": len(match.group(1)), "text": match.group(2)}
    elif match := re.fullmatch(r"(\s*)(\d+)\.\s+(.*)", line):
        block = {"type": "ordered", "indent": len(match.group(1)), "number": int(match.group(2)), "text": match.group(3)}
    elif match := re.fullmatch(r"\s*>\s?(.*)", line):
        block = {"type": "quote", "text": match.group(1)}
    elif re.fullmatch(r"\s*```.*", line):
        block = {"type": "code-fence", "text": line.strip()[3:]}
    elif re.fullmatch(r"\s*(?:---+|___+|\*\*\*+)\s*", line):
        block = {"type": "divider", "text": ""}
    elif line.count("|") >= 2:
        block = {"type": "table-row", "text": line}
    else:
        block = {"type": "paragraph", "text": line}
    block["source"] = line
    return block


def _block_line(block: dict[str, Any]) -> str:
    if isinstance(block.get("source"), str):
        return block["source"]
    kind = block.get("type")
    text = str(block.get("text") or "")
    if kind == "heading":
        return f"{'#' * max(1, min(6, int(block.get('level') or 1)))} {text}"
    if kind == "task":
        return f"{' ' * max(0, int(block.get('indent') or 0))}{block.get('marker') or '-'} [{'x' if block.get('checked') else ' '}] {text}"
    if kind == "bullet":
        return f"{' ' * max(0, int(block.get('indent') or 0))}- {text}"
    if kind == "ordered":
        return f"{' ' * max(0, int(block.get('indent') or 0))}{max(1, int(block.get('number') or 1))}. {text}"
    if kind == "quote":
        return f"> {text}"
    if kind == "code-fence":
        return f"```{text}"
    if kind == "divider":
        return "---"
    return text


def _note_blocks(
    body: str,
    previous: list[dict[str, Any]] | None = None,
    deterministic_namespace: str | None = None,
) -> list[dict[str, Any]]:
    old = [block for block in previous or [] if isinstance(block, dict) and isinstance(block.get("id"), str)]
    unused = {block["id"] for block in old}
    exact: dict[str, list[str]] = defaultdict(list)
    for block in old:
        exact[json.dumps({key: value for key, value in block.items() if key not in {"id", "relationIds"}}, sort_keys=True)].append(block["id"])
    blocks = [_block_from_line(line) for line in body.split("\n")]
    # Reserve exact matches first so inserting a same-type block cannot steal
    # the stable ID (and attached relations) of unchanged downstream content.
    for block in blocks:
        signature = json.dumps(block, sort_keys=True)
        block_id = next((value for value in exact.get(signature, []) if value in unused), None)
        if block_id is not None:
            block["id"] = block_id
            unused.discard(block_id)
    for index, block in enumerate(blocks):
        block_id = block.get("id")
        if block_id is None and index < len(old) and old[index].get("type") == block.get("type") and old[index]["id"] in unused:
            block_id = old[index]["id"]
        seed = None
        if deterministic_namespace is not None:
            seed = f"{deterministic_namespace}\0block\0{index}\0{json.dumps(block, sort_keys=True, ensure_ascii=False)}"
        block["id"] = block_id or _record_id("blk", seed)
        unused.discard(block["id"])
    return blocks


def _note_body_text(body: Any) -> str:
    if isinstance(body, str):
        return body
    if not isinstance(body, dict) or body.get("type") != "doc" or not isinstance(body.get("blocks"), list):
        raise ValueError("database note body is not a document tree")
    return "\n".join(_block_line(block) for block in body["blocks"] if isinstance(block, dict))


def _note_tasks(document_id: str, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks = []
    offset = 0
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            offset += 1
            continue
        source = _block_line(block)
        if block.get("type") == "task" and isinstance(block.get("id"), str):
            tasks.append({
                "id": f"{document_id}:{block['id']}",
                "blockId": block["id"],
                "line": index + 1,
                "sourceRange": {"from": offset, "to": offset + len(source)},
                "expectedTextHash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "done": bool(block.get("checked")),
                "text": str(block.get("text") or ""),
            })
        offset += len(source) + 1
    return tasks


def _task_projection(request: Request, scope: dict[str, str], document: dict[str, Any]) -> list[dict[str, Any]]:
    view = _resource_view(request, scope, document)
    resource_key = (view.get("resource") or {}).get("key")
    rows = _task_source_rows(view)
    return [
        {
            "id": f"{view.get('id')}:{row['blockId']}",
            "type": "markdown",
            "source": "vault",
            "resourceKey": resource_key,
            "sourceRevision": view.get("head"),
            "document": {
                "id": view.get("id"),
                "name": view.get("name"),
                "head": view.get("head"),
                "kind": view.get("kind"),
                "readOnly": bool(view.get("readOnly") or view.get("rawPreserved") or view.get("note_error")),
            },
            "anchor": {
                "blockId": row["blockId"],
                "sourceRange": row["sourceRange"],
                "expectedTextHash": row["expectedTextHash"],
                "expectedText": row["source"],
            },
            "text": row["text"],
            "checked": row["done"],
            "line": row["line"],
            "label": view.get("name"),
            "headingPath": row["headingPath"],
            "tags": list(view.get("tags") or []),
            "capabilities": (view.get("resource") or {}).get("capabilities") or {"read": True, "edit": False},
        }
        for row in rows
    ]


def _task_revision(documents: list[dict[str, Any]]) -> str:
    material = [
        {"id": str(document.get("id") or ""), "head": str(document.get("head") or "")}
        for document in documents
        if document.get("kind") in _NOTE_KINDS or str(document.get("kind") or "") in {"markdown", "text"}
    ]
    material.sort(key=lambda item: item["id"])
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _task_cursor(
    scope: dict[str, str],
    revision: str,
    offset: int,
    query: str,
    completed: str | None,
    *,
    source: str = "all",
    bridge_cursor: str | None = None,
) -> str:
    payload = {
        "owner": scope["owner"],
        "workspace": scope["workspace_id"],
        "revision": revision,
        "offset": offset,
        "query": query,
        "completed": completed,
        "source": source,
    }
    if bridge_cursor:
        payload["bridgeCursor"] = bridge_cursor
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return __import__("base64").urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _read_task_cursor(value: str) -> dict[str, Any]:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(__import__("base64").urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, detail={"outcome": "invalid_cursor", "message": "Task cursor is invalid"}) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("revision"), str) or not isinstance(payload.get("offset"), int):
        raise HTTPException(400, detail={"outcome": "invalid_cursor", "message": "Task cursor is invalid"})
    return payload


def _expected_revision(value: dict[str, Any] | str | None) -> str | None:
    if isinstance(value, dict):
        raw = value.get("value")
        return str(raw) if raw is not None else None
    return str(value) if value is not None else None


def _task_action_digest(action_id: str, payload: BaseModel) -> str:
    body = payload.model_dump(exclude_none=True) if hasattr(payload, "model_dump") else payload.dict(exclude_none=True)
    body.pop("actionId", None)
    body.pop("operationId", None)
    return hashlib.sha256(json.dumps({"actionId": action_id, "payload": body}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _property_type(value: Any, key: str) -> str:
    if isinstance(value, bool):
        return "checkbox"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "tags" if "tag" in key.lower() else "list"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return "date"
    return "text"


def _editable_source_property(value: Any) -> str:
    """Validate a user property edit without allowing derived file fields."""
    try:
        property_name = canonical_base_property(value, "$.property")
    except BaseDefinitionError as exc:
        raise HTTPException(422, detail={"diagnostics": exc.diagnostics}) from exc
    if not _NOTE_PROPERTY.fullmatch(property_name):
        raise HTTPException(422, "Invalid note property name")
    if property_name.startswith(("file.", "formula.")) or property_name in {"file", "this"}:
        raise HTTPException(422, "That Base column is read-only")
    return property_name


def _property_records(
    properties: dict[str, Any],
    previous: Any = None,
    deterministic_namespace: str | None = None,
) -> list[dict[str, Any]]:
    old = {
        item.get("key"): item for item in previous or []
        if isinstance(item, dict) and isinstance(item.get("key"), str) and isinstance(item.get("id"), str)
    }
    return [
        {
            "id": old.get(key, {}).get("id") or _record_id(
                "prop",
                f"{deterministic_namespace}\0property\0{key}" if deterministic_namespace is not None else None,
            ),
            "key": key,
            "type": _property_type(value, key),
            "value": value,
        }
        for key, value in properties.items()
    ]


def _property_values(records: Any) -> dict[str, Any]:
    if isinstance(records, dict):
        return _note_properties(records)
    if not isinstance(records, list):
        return {}
    return _note_properties({
        record["key"]: record.get("value")
        for record in records
        if isinstance(record, dict) and isinstance(record.get("key"), str)
    })


def _note_relations(
    body: str,
    blocks: list[dict[str, Any]],
    requested: list[dict[str, Any]] | None = None,
    previous: list[dict[str, Any]] | None = None,
    deterministic_namespace: str | None = None,
) -> list[dict[str, Any]]:
    requested_by_name = {
        str(item.get("target") or "").casefold(): item
        for item in requested or []
        if isinstance(item, dict) and isinstance(item.get("target"), str)
    }
    old = {
        (item.get("kind"), item.get("target"), item.get("fragment")): item
        for item in previous or [] if isinstance(item, dict)
    }
    relations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for match in _NOTE_LINK.finditer(body):
        raw = match.group(2).split("|", 1)[0].strip()
        target, _, fragment = raw.partition("#")
        target = target.strip()
        if not target:
            continue
        kind = "embed" if match.group(1) else "link"
        fragment = fragment.strip()
        key = (kind, target, fragment)
        if key in seen:
            continue
        seen.add(key)
        prior = old.get(key, {})
        supplied = requested_by_name.get(target.casefold(), {})
        supplied_document = supplied.get("targetDocumentId")
        if not isinstance(supplied_document, str) or not _ID.fullmatch(supplied_document):
            supplied_document = None
        supplied_block = supplied.get("targetBlockId")
        if not isinstance(supplied_block, str) or not _ID.fullmatch(supplied_block):
            supplied_block = None
        line = body.count("\n", 0, match.start())
        relation = {
            "id": prior.get("id") or _record_id(
                "rel",
                f"{deterministic_namespace}\0body-relation\0{match.start()}\0{kind}\0{target}\0{fragment}"
                if deterministic_namespace is not None else None,
            ),
            "kind": kind,
            "origin": "body",
            "sourceBlockId": blocks[min(line, len(blocks) - 1)]["id"] if blocks else None,
            "target": target,
            "targetDocumentId": supplied_document or prior.get("targetDocumentId"),
            "targetBlockId": supplied_block or prior.get("targetBlockId"),
        }
        if fragment:
            relation["fragment"] = fragment
        relations.append(relation)
    explicit = requested if requested is not None else [
        relation for relation in previous or []
        if isinstance(relation, dict) and relation.get("origin") == "explicit"
    ]
    block_ids = {block["id"] for block in blocks}
    for explicit_index, item in enumerate(explicit):
        if not isinstance(item, dict) or item.get("origin") == "body":
            continue
        kind = item.get("kind")
        target = str(item.get("target") or "").strip()
        fragment = str(item.get("fragment") or "").strip()
        if kind not in {"link", "embed", "parent", "collection", "asset"} or not target:
            continue
        key = (kind, target, fragment)
        if key in seen:
            continue
        seen.add(key)
        prior = old.get(key, {})
        source_block = item.get("sourceBlockId")
        target_document = item.get("targetDocumentId")
        target_block = item.get("targetBlockId")
        relation = {
            "id": prior.get("id") or _record_id(
                "rel",
                f"{deterministic_namespace}\0explicit-relation\0{explicit_index}\0{kind}\0{target}\0{fragment}"
                if deterministic_namespace is not None else None,
            ),
            "kind": kind,
            "origin": "explicit",
            "sourceBlockId": source_block if source_block in block_ids else None,
            "target": target,
            "targetDocumentId": target_document if isinstance(target_document, str) and _ID.fullmatch(target_document) else None,
            "targetBlockId": target_block if isinstance(target_block, str) and _ID.fullmatch(target_block) else None,
        }
        if fragment:
            relation["fragment"] = fragment
        relations.append(relation)
    return relations


def _note_tags(body: str, properties: dict[str, Any]) -> list[str]:
    tags: set[str] = set(_NOTE_TAG.findall(body))
    for key, value in properties.items():
        if key.lower() not in {"tag", "tags"}:
            continue
        values = value if isinstance(value, list) else str(value or "").split(",")
        for tag in values:
            normalized = str(tag).strip().lstrip("#")
            if normalized:
                tags.add(normalized)
    return sorted(tags, key=str.casefold)


def _note_projection_hash(body: str, properties: dict[str, Any]) -> str:
    payload = json.dumps(
        {"body": body, "properties": properties},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_safe_yaml(value: Any, *, budget: list[int] | None = None) -> Any:
    """Normalize safe-loader values into bounded finite JSON data."""
    budget = budget or [10_000]
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("frontmatter is too structurally complex")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not (float("-inf") < value < float("inf")):
            raise ValueError("frontmatter contains a non-finite number")
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe_yaml(item, budget=budget) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _json_safe_yaml(item, budget=budget)
            for key, item in value.items()
        }
    return str(value)


def _import_markdown_record(
    source: str,
    identity: str = "imported-markdown",
) -> tuple[str, list[dict[str, str]]]:
    """Convert Markdown into a canonical note envelope without losing source."""
    body = source
    properties: dict[str, Any] = {}
    diagnostics: list[dict[str, str]] = []
    if source.startswith("---\n") or source.startswith("---\r\n"):
        lines = source.splitlines(keepends=True)
        closing = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if closing is None:
            diagnostics.append({"code": "unterminated_frontmatter", "message": "Frontmatter was preserved as note body."})
        else:
            header = "".join(lines[1:closing])
            try:
                parsed = yaml.safe_load(header) if header.strip() else {}
                if parsed is None:
                    parsed = {}
                if not isinstance(parsed, dict):
                    raise ValueError("frontmatter root is not an object")
                properties = _note_properties(_json_safe_yaml(parsed))
                body = "".join(lines[closing + 1:])
                if body.startswith("\r\n"):
                    body = body[2:]
                elif body.startswith("\n"):
                    body = body[1:]
            except (HTTPException, ValueError, yaml.YAMLError) as exc:
                diagnostics.append({"code": "invalid_frontmatter", "message": f"Frontmatter was preserved as note body: {exc}"})
                body = source
                properties = {}
    languages = sorted({
        match.group(1).casefold()
        for match in re.finditer(r"^```\s*([A-Za-z0-9_-]+)", source, re.MULTILINE)
        if match.group(1).casefold() in {"dataview", "dataviewjs", "tasks", "templater"}
    })
    compatibility = [
        {"kind": "plugin-query-block", "language": language, "execution": "inert"}
        for language in languages
    ]
    return _encode_note(
        body,
        properties,
        import_source=source,
        compatibility=compatibility,
        deterministic_namespace=f"markdown-import\0{identity}",
    ), diagnostics


def _encode_note(
    body: str,
    properties: dict[str, Any] | None = None,
    relations: list[dict[str, Any]] | None = None,
    previous: dict[str, Any] | None = None,
    *,
    import_source: str | None = None,
    compatibility: list[dict[str, Any]] | None = None,
    deterministic_namespace: str | None = None,
) -> str:
    clean = _note_properties(properties)
    previous_body = previous.get("body") if isinstance(previous, dict) else None
    previous_blocks = previous_body.get("blocks") if isinstance(previous_body, dict) else None
    blocks = _note_blocks(body, previous_blocks, deterministic_namespace)
    relation_records = _note_relations(
        body,
        blocks,
        relations,
        previous.get("relations") if isinstance(previous, dict) else None,
        deterministic_namespace,
    )
    tags = _note_tags(body, clean)
    previous_tags = {
        str(relation.get("target") or "").casefold(): relation
        for relation in (previous.get("relations") if isinstance(previous, dict) else []) or []
        if isinstance(relation, dict) and relation.get("kind") == "tag"
    }
    for tag in tags:
        match = re.search(rf"(?<![\w/])#{re.escape(tag)}(?=$|[^\w/-])", body, re.IGNORECASE)
        line = body.count("\n", 0, match.start()) if match else None
        prior = previous_tags.get(tag.casefold(), {})
        relation_records.append({
            "id": prior.get("id") or _record_id(
                "rel",
                f"{deterministic_namespace}\0tag\0{tag.casefold()}" if deterministic_namespace is not None else None,
            ),
            "kind": "tag",
            "sourceBlockId": blocks[min(line, len(blocks) - 1)]["id"] if line is not None and blocks else None,
            "target": tag,
            "targetDocumentId": None,
            "targetBlockId": None,
        })
    relation_ids: dict[str, list[str]] = defaultdict(list)
    for relation in relation_records:
        if relation.get("sourceBlockId"):
            relation_ids[relation["sourceBlockId"]].append(relation["id"])
    for block in blocks:
        if relation_ids.get(block["id"]):
            block["relationIds"] = relation_ids[block["id"]]
    record = {
        "schemaVersion": _NOTE_SCHEMA_VERSION,
        "body": {"type": "doc", "blocks": blocks},
        "properties": _property_records(
            clean,
            previous.get("properties") if isinstance(previous, dict) else None,
            deterministic_namespace,
        ),
        "relations": relation_records,
        "tags": tags,
    }
    extensions = dict(previous.get("extensions") or {}) if isinstance(previous, dict) and isinstance(previous.get("extensions"), dict) else {}
    projection_hash = _note_projection_hash(body, clean)
    if import_source is not None:
        extensions["interchange"] = {
            "format": "markdown",
            "source": import_source,
            "projectionHash": projection_hash,
            "modified": False,
        }
    elif isinstance(extensions.get("interchange"), dict):
        interchange = dict(extensions["interchange"])
        interchange["modified"] = interchange.get("projectionHash") != projection_hash
        extensions["interchange"] = interchange
    if compatibility is not None:
        extensions["compatibility"] = compatibility
    if extensions:
        record["extensions"] = extensions
    encoded = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 16_777_216:
        raise HTTPException(413, "Database note and interchange source exceed the 16 MiB limit")
    return encoded


def _future_schema_version(record: Any) -> int | None:
    if not isinstance(record, dict):
        return None
    candidate = record.get("schemaVersion")
    # The native body accepts either the current document-tree object or a
    # string body.  A future marker on null/array/scalar content is malformed
    # recovery data, not a decodable future native record.
    if (
        isinstance(candidate, int)
        and not isinstance(candidate, bool)
        and candidate > _NOTE_SCHEMA_VERSION
        and isinstance(record.get("body"), (dict, str))
    ):
        return candidate
    return None


def _note_view(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("kind") not in _NOTE_KINDS:
        return document
    if document.get("format") == "copal-note-v1" and document.get("storage") == "database":
        result = dict(document)
        # Newer bridge versions already classify future records.  Recover the
        # concrete schema number from preserved bytes so older bridge-shaped
        # projections and the UI expose the same version-aware state.
        if result.get("recoveryState") == "unsupported-future" and not result.get("sourceSchemaVersion"):
            source = result.get("rawSource")
            if not isinstance(source, dict) and isinstance(result.get("extensions"), dict):
                source = result["extensions"].get("rawSource")
            try:
                raw_bytes = __import__("base64").b64decode(source["base64"], validate=True) if isinstance(source, dict) else b""
                parsed = json.loads(raw_bytes.decode("utf-8"))
                version = _future_schema_version(parsed)
                if version is not None:
                    result["sourceSchemaVersion"] = version
                elif source:
                    result["recoveryState"] = "malformed-preserved"
                    result["sourceFormat"] = "unknown"
            except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        return result
    result = dict(document)
    loose_storage = document.get("storage") == "files" or document.get("format") == "copal-loose-v1"
    raw = str(document.get("text") or "")
    try:
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get("schemaVersion") != _NOTE_SCHEMA_VERSION:
            raise ValueError("unsupported Copal note schema")
        body = _note_body_text(record.get("body"))
        properties = _property_values(record.get("properties"))
        relations = record.get("relations")
        if not isinstance(relations, list):
            relations = []
        relations = [
            relation for relation in relations
            if isinstance(relation, dict)
            and relation.get("kind") in {"link", "embed", "tag", "parent", "collection", "asset"}
            and isinstance(relation.get("target"), str)
            and relation["target"].strip()
        ]
        tags = record.get("tags")
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            tags = _note_tags(body, properties)
        blocks = record.get("body", {}).get("blocks", []) if isinstance(record.get("body"), dict) else []
        tasks = _note_tasks(str(document.get("id") or "note"), blocks)
        course = properties.get("course")
        skill = properties.get("skill")
        treehouse = ({
            "id": document.get("id"),
            "course": course,
            "skill": skill,
            "prerequisite": properties.get("depends_on"),
            "evidence_task_ids": [task["id"] for task in tasks],
            "source_document_id": document.get("id"),
            "source_head": document.get("head"),
        } if course is not None or skill is not None else None)
        preserved_extension_source = record.get("extensions", {}).get("rawSource") if isinstance(record.get("extensions"), dict) else None
        if not isinstance(preserved_extension_source, dict):
            outer_extensions = document.get("extensions")
            preserved_extension_source = outer_extensions.get("rawSource") if isinstance(outer_extensions, dict) else None
        result.update({
            "text": body,
            "properties": properties,
            "propertyDefinitions": record.get("properties") if isinstance(record.get("properties"), list) else [],
            "frontmatter": properties,
            "relations": relations,
            "links": list(dict.fromkeys(relation["target"] for relation in relations if relation.get("kind") in {"link", "embed"})),
            "tags": list(dict.fromkeys(tags)),
            "blocks": blocks,
            "tasks": tasks,
            "treehouse": treehouse,
            "format": "copal-loose-v1" if loose_storage else "copal-note-v1",
            "storage": "files" if loose_storage else "database",
            "corpus": document.get("corpus") or ("wiki" if document.get("kind") == _WIKI_KIND else "notes"),
            "extensions": record.get("extensions") if isinstance(record.get("extensions"), dict) else {},
            "rawSource": preserved_extension_source if isinstance(preserved_extension_source, dict) else None,
            "recoveryState": "supported",
            "sourceFormat": "native",
        })
    except (HTTPException, json.JSONDecodeError, TypeError, ValueError) as exc:
        is_legacy_markdown = bool(raw.strip()) and not raw.lstrip().startswith(("{", "["))
        future_version = None
        if not is_legacy_markdown:
            try:
                parsed = json.loads(raw)
                future_version = _future_schema_version(parsed)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        preserved = None
        try:
            preserved = raw_source(raw.encode("utf-8")) if raw else None
        except (UnicodeEncodeError, MemesValidationError):
            preserved = None
        result.update({
            # Markdown imports remain previewable as source; malformed native
            # JSON stays blank in the editable projection and is recovered via
            # rawSource below. Neither state is silently rewritten.
            "text": raw if is_legacy_markdown else "", "properties": {}, "frontmatter": {}, "relations": [], "links": [], "tags": [],
            "blocks": [], "format": "copal-loose-v1" if loose_storage else "copal-note-v1", "storage": "files" if loose_storage else "database",
            "corpus": document.get("corpus") or ("wiki" if document.get("kind") == _WIKI_KIND else "notes"),
            "extensions": {},
            "note_error": None if is_legacy_markdown and document.get("kind") == _WIKI_KIND else str(exc),
            "rawPreserved": True,
            "rawSource": preserved,
            "formatNotice": "legacy-markdown" if is_legacy_markdown and document.get("kind") == _WIKI_KIND else None,
            "readOnly": bool(document.get("readOnly") or document.get("builtin") or (is_legacy_markdown and document.get("kind") == _WIKI_KIND)),
            "recoveryState": "legacy-import" if is_legacy_markdown else "unsupported-future" if future_version is not None else "malformed-preserved",
            "sourceFormat": "markdown" if is_legacy_markdown else "native" if future_version is not None else "unknown",
            **({"sourceSchemaVersion": future_version} if future_version is not None else {}),
        })
    return result


def _note_result(result: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(result)
    if isinstance(result.get("doc"), dict):
        normalized["doc"] = _note_view(result["doc"])
    return normalized


def _require_mutable_note(document: dict[str, Any]) -> None:
    if document.get("kind") in _NOTE_KINDS and (document.get("rawPreserved") or document.get("note_error")):
        raise HTTPException(409, "Copal note is preserved read-only until its stored schema can be decoded")


def _require_nonplanning_document_mutation(document: dict[str, Any]) -> None:
    if document.get("kind") in _CONTROL_DOCUMENT_KINDS or event_from_document(document):
        raise HTTPException(409, "Canonical Copal control records must use their domain API")


def _note_markdown(document: dict[str, Any]) -> str:
    properties = document.get("properties") if isinstance(document.get("properties"), dict) else {}
    body = str(document.get("text") or "")
    extensions = document.get("extensions") if isinstance(document.get("extensions"), dict) else {}
    interchange = extensions.get("interchange") if isinstance(extensions.get("interchange"), dict) else {}
    source = interchange.get("source")
    if (
        isinstance(source, str)
        and interchange.get("modified") is not True
        and interchange.get("projectionHash") == _note_projection_hash(body, properties)
    ):
        return source
    if not properties:
        return body
    lines = ["---"]
    for key, value in properties.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, allow_nan=False)}")
    lines.extend(["---", "", body])
    return "\n".join(lines)


def _preserved_source_bytes(document: dict[str, Any]) -> bytes | None:
    """Return validated recovery bytes supplied by the bridge, if present."""
    source = document.get("rawSource")
    # Supported native imports retain the exporter bytes as provenance inside
    # the native record's extensions.  The bridge exposes that record directly
    # for editable documents, so accept the nested form as well.  Recovery and
    # .memes export must use those bytes instead of reserializing the parsed
    # projection (which would lose whitespace and original field ordering).
    if not isinstance(source, dict):
        extensions = document.get("extensions")
        if isinstance(extensions, dict):
            source = extensions.get("rawSource")
    if not isinstance(source, dict) or source.get("encoding") != "utf-8":
        return None
    encoded = source.get("base64")
    digest = source.get("sha256")
    if not isinstance(encoded, str) or not isinstance(digest, str):
        return None
    try:
        data = __import__("base64").b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    return data if hashlib.sha256(data).hexdigest() == digest else None


def _validated_zip_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    seen: set[str] = set()
    portable_seen: dict[str, str] = {}
    expanded = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        mode = info.external_attr >> 16
        normalized = path.as_posix()
        unix_type = stat.S_IFMT(mode)
        if (
            not name
            or len(name.encode("utf-8", errors="surrogatepass")) > 4096
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or (path.parts and re.fullmatch(r"[A-Za-z]:", path.parts[0]))
            or stat.S_ISLNK(mode)
            or unix_type not in {0, stat.S_IFREG}
            or info.flag_bits & 0x1
        ):
            raise HTTPException(400, f"Unsafe ZIP member: {info.filename}")
        if normalized in seen:
            raise HTTPException(400, f"Duplicate ZIP member: {info.filename}")
        seen.add(normalized)
        portable = unicodedata.normalize("NFC", normalized).casefold()
        if previous := portable_seen.get(portable):
            raise HTTPException(
                400,
                f"ZIP members collide on portable filesystems: {previous!r} and {info.filename!r}",
            )
        portable_seen[portable] = info.filename
        if info.file_size > _COPAL_IMPORT_MAX_MEMBER_BYTES:
            raise HTTPException(413, f"ZIP member exceeds the 512 MB per-file limit: {info.filename}")
        if info.file_size and (
            info.compress_size == 0
            or info.file_size / max(1, info.compress_size) > _COPAL_IMPORT_MAX_COMPRESSION_RATIO
        ):
            raise HTTPException(413, f"ZIP member has an unsafe compression ratio: {info.filename}")
        expanded += info.file_size
        if len(members) >= _COPAL_IMPORT_MAX_FILES or expanded > _COPAL_IMPORT_MAX_EXPANDED_BYTES:
            raise HTTPException(413, "Copal import expands beyond its safety limit")
        members.append((info, path))
    return members


def _export_restore_identities(
    root: Path,
    members: list[tuple[zipfile.ZipInfo, PurePosixPath]],
    workspace: str,
) -> tuple[dict[str, dict[str, str]], bool]:
    """Validate Copal's reserved export manifest and remove it from user data.

    Plain Obsidian archives have no manifest.  A Copal export does, and its
    identity map is all-or-nothing so a truncated or edited backup cannot
    quietly remap stable document references.
    """
    manifest_name = ".copal/export-manifest.json"
    manifest_path = root / ".copal" / "export-manifest.json"
    if not manifest_path.is_file():
        return {}, False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Copal export manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != "copal-obsidian-export-v1":
        raise HTTPException(400, "Copal export manifest format is unsupported")
    if manifest.get("workspace") != workspace:
        raise HTTPException(400, "Copal export manifest belongs to a different workspace")
    documents = manifest.get("documents")
    if not isinstance(documents, list) or len(documents) > _COPAL_IMPORT_MAX_FILES:
        raise HTTPException(400, "Copal export manifest document list is invalid")

    identities: dict[str, dict[str, str]] = {}
    document_ids: set[str] = set()
    for document in documents:
        if not isinstance(document, dict):
            raise HTTPException(400, "Copal export manifest contains an invalid document entry")
        document_id = document.get("id")
        corpus = document.get("corpus")
        kind = document.get("kind")
        name = document.get("path")
        size = document.get("size")
        digest = document.get("sha256")
        has_integrity = size is not None or digest is not None
        if (
            not isinstance(document_id, str)
            or not _ID.fullmatch(document_id)
            or not isinstance(corpus, str)
            or not _KIND.fullmatch(corpus)
            or not isinstance(kind, str)
            or not _KIND.fullmatch(kind)
            or not isinstance(name, str)
            or (
                has_integrity
                and (
                    not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                )
            )
        ):
            raise HTTPException(400, "Copal export manifest contains invalid identity fields")
        normalized = PurePosixPath(name).as_posix()
        if (
            normalized != name
            or name == manifest_name
            or PurePosixPath(name).is_absolute()
            or any(part in {"", ".", ".."} for part in PurePosixPath(name).parts)
            or name in identities
            or document_id in document_ids
        ):
            raise HTTPException(400, "Copal export manifest contains duplicate or unsafe identities")
        identities[name] = {"id": document_id, "corpus": corpus, "kind": kind}
        document_ids.add(document_id)
        if has_integrity:
            target = root.joinpath(*PurePosixPath(name).parts)
            if not target.is_file() or target.stat().st_size != size:
                raise HTTPException(400, "Copal export manifest size does not match archive content")
            content_digest = hashlib.sha256()
            with target.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    content_digest.update(chunk)
            if content_digest.hexdigest() != digest:
                raise HTTPException(400, "Copal export manifest fingerprint does not match archive content")

    archive_paths = {relative.as_posix() for _, relative in members}
    if set(identities) != archive_paths - {manifest_name}:
        raise HTTPException(400, "Copal export manifest does not reconcile every archive entry")
    manifest_path.unlink()
    return identities, True


def _prepare_import_tree(root: Path, preserved_paths: set[str] | None = None) -> dict[str, Any]:
    prepared = 0
    diagnostics: list[dict[str, str]] = []
    preserved_paths = preserved_paths or set()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in {".md", ".markdown"}:
            continue
        relative = path.relative_to(root)
        if relative.as_posix() in preserved_paths:
            continue
        reserved_wiki = relative.parts[:2] == (".copal", "wiki")
        if any(part.startswith(".") for part in relative.parts) and not reserved_wiki:
            continue
        if path.stat().st_size > 8_388_608:
            diagnostics.append({
                "path": relative.as_posix(),
                "code": "oversized_markdown",
                "message": "Preserved as non-executable compatibility data because it exceeds the 8 MiB native-note limit.",
            })
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            diagnostics.append({
                "path": relative.as_posix(),
                "code": "non_utf8_markdown",
                "message": "Preserved as non-executable compatibility data.",
            })
            continue
        encoded, current = _import_markdown_record(source, relative.as_posix())
        path.write_text(encoded, encoding="utf-8", newline="")
        prepared += 1
        diagnostics.extend({"path": relative.as_posix(), **item} for item in current)
    return {"preparedDatabaseNotes": prepared, "diagnostics": diagnostics}


async def _call(request: Request, operation: str, args: dict[str, Any], *, timeout: float = 20):
    bridge = _bridge(request)
    if not bridge.is_alive():
        try:
            await bridge.start()
        except (asyncio.TimeoutError, CopalBridgeError, OSError) as exc:
            raise HTTPException(503, "Copal storage bridge is unavailable") from exc
    # ``trash`` is a read-only listing used by GET endpoints.  Capturing it
    # would create fake history rows whenever a user opens the trash view.
    capture_operations = {"create", "write", "rename", "delete", "restore", "restore_deleted", "checkpoint", "move", "replace"}
    history_client = None
    history_action_id = str(args.get("action_id") or args.get("actionId") or args.get("commandId") or request.headers.get("X-OpenClank-Action-Id") or "").strip()
    history_before = None
    history_status: dict[str, Any] = {"status": "unconfigured", "durable": False, "coverage": "NoCapture"}
    if operation in capture_operations:
        scope_owner = str(args.get("owner") or "")
        account_id = _actor_account_id(request)
        socket_path = __import__("os").environ.get("OPENCLANK_HISTORY_SOCKET", "").strip()
        if account_id and scope_owner and socket_path:
            if not history_action_id:
                # An omitted id describes a new mutation.  Never derive it
                # from arguments: a legitimate repeated write would otherwise
                # look like an idempotent retry and collide with its history.
                history_action_id = "route-" + uuid.uuid4().hex
            history_client = HistoryClient(socket_path, actor_id=scope_owner, account_id=str(account_id))
            if args.get("id"):
                try:
                    history_before = await bridge.call("get", {"owner": scope_owner, "workspace_id": args.get("workspace_id"), "id": args["id"]}, timeout=timeout)
                except Exception:
                    history_before = None
            def managed_bytes(document: Any) -> bytes | None:
                if not isinstance(document, dict):
                    return None
                fields = {key: document.get(key) for key in ("id", "name", "kind", "text", "properties", "relations", "extensions", "attachments", "propertyDefinitions", "tags", "blocks") if key in document}
                return json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
            def history_fingerprint(data: bytes | None) -> str:
                return "missing" if data is None else f"sha256:{hashlib.sha256(data).hexdigest()}:{len(data)}"
            before_bytes = managed_bytes(history_before)
            resource_id = str((history_before or {}).get("id") or args.get("id") or args.get("name") or f"intent:{history_action_id}")
            envelope = {
                "schema_version": 1, "action_id": history_action_id, "actor_account_id": str(account_id),
                "resource_key": {"account_id": str(account_id), "workspace_id": str(args.get("workspace_id") or ""), "provider": "copal", "resource_id": resource_id},
                "guard_resource_ids": [], "modified_resource_ids": [], "operation": operation,
                "expected_revision": None, "actor_id": scope_owner, "actor_kind": "user", "session_id": None,
                "run_id": None, "task_id": None, "tool_id": "copal-route", "before_revision": None,
                "expected_after_revision": None, "original_locator": None, "destination_locator": None,
                "timestamp_millis": int(time.time() * 1000), "coverage": {"metadata": {"coverage_kind": "KnownMutationHooks", "roots": [str(args.get("workspace_id") or "")], "exclusions": []}}, "per_resource_outcomes": None,
            }
            try:
                await asyncio.to_thread(history_client.prepare, envelope, content=before_bytes, fingerprint=history_fingerprint(before_bytes))
                history_status.update(status="prepared", durable=False, action_id=history_action_id, phase="before")
            except (HistoryClientError, OSError, asyncio.TimeoutError) as exc:
                paused = "history_paused_budget" in str(exc)
                history_status.update(status="paused" if paused else "failed", action_id=history_action_id, phase="budget" if paused else "before", error=str(exc))
                history_client = None
    try:
        result = await bridge.call(operation, args, timeout=timeout)
        if history_client is not None:
            after_doc = result.get("doc") if isinstance(result, dict) else None
            if after_doc is None and operation not in {"delete", "trash"} and args.get("id"):
                try:
                    after_doc = await bridge.call("get", {"owner": args.get("owner"), "workspace_id": args.get("workspace_id"), "id": args["id"]}, timeout=timeout)
                except Exception:
                    after_doc = None
            after_bytes = managed_bytes(after_doc)
            outcome = str(result.get("outcome") if isinstance(result, dict) else "committed")
            explicit_status = str(
                result.get("live_status") or result.get("liveStatus") or result.get("status") or ""
            ).casefold()
            if explicit_status in {"committed", "created", "restored", "deleted", "applied", "unchanged", "ok"}:
                live_status = "Committed"
            elif explicit_status in {"conflict", "stale"} or outcome in {"conflict", "stale"}:
                live_status = "Conflict"
            elif explicit_status in {"partial"} or outcome == "partial":
                live_status = "Partial"
            elif explicit_status in {"not_committed", "not committed", "failed", "error"} or outcome in {"failed", "error"}:
                live_status = "NotCommitted"
            else:
                live_status = "Committed" if outcome in {"committed", "created", "restored", "deleted", "applied", "unchanged"} else "Unknown"
            if operation == "create" and isinstance(after_doc, dict) and after_doc.get("id"):
                try:
                    await asyncio.to_thread(
                        history_client.rebind_resource,
                        history_action_id,
                        str(after_doc["id"]),
                    )
                except HistoryClientError as exc:
                    history_status.update(
                        status="failed",
                        phase="resource_rebind",
                        action_id=history_action_id,
                        error=str(exc),
                    )
            try:
                await asyncio.to_thread(history_client.record_live, history_action_id, {"action_id": history_action_id, "status": live_status, "fingerprint": history_fingerprint(after_bytes), "after_unavailable": False, "resource_id": after_doc.get("id") if isinstance(after_doc, dict) else None})
                if live_status == "Committed":
                    await asyncio.to_thread(history_client.complete, history_action_id, content=after_bytes, fingerprint=history_fingerprint(after_bytes))
                    history_status.update(status="complete", durable=True, phase="complete")
                else:
                    history_status.update(status="failed", phase="live_conflict")
            except (HistoryClientError, OSError, asyncio.TimeoutError) as exc:
                paused = "history_paused_budget" in str(exc)
                history_status.update(status="paused" if paused else "failed", phase="budget" if paused else "after", error=str(exc))
        if operation in capture_operations and isinstance(result, dict):
            result.setdefault("history", history_status)
        return result
    except asyncio.TimeoutError as exc:
        if history_client is not None:
            try:
                await asyncio.to_thread(history_client.record_live, history_action_id, {"action_id": history_action_id, "status": "NotCommitted", "fingerprint": None, "after_unavailable": True})
            except Exception:
                pass
        raise HTTPException(504, "Copal database operation timed out") from exc
    except CopalBridgeError as exc:
        if history_client is not None:
            try:
                await asyncio.to_thread(history_client.record_live, history_action_id, {"action_id": history_action_id, "status": "NotCommitted", "fingerprint": None, "after_unavailable": True})
            except Exception:
                pass
        message = str(exc)
        status = 404 if "not found" in message else 403 if "read-only" in message else 409 if "exists" in message or "stale_cursor" in message else 400
        raise HTTPException(status, message) from exc


def _calendar_owner(request: Request) -> str:
    """Use exactly the owner identity consumed by native Calendar routes."""
    from routes.calendar_routes import FALLBACK_OWNER

    return require_user(request) or FALLBACK_OWNER


async def _persist_projection_linkage(
    request: Request,
    scope: dict[str, str],
    planning_doc: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Keep cross-store identifiers in a hidden Copal document, never SQLite."""
    planning_id = _doc_id(str(planning_doc.get("id") or ""))
    name = f".copal/calendar-projection-{planning_id}.json"
    payload = json.dumps(
        {
            "schemaVersion": 1,
            "owner": scope["owner"],
            "calendarOwner": result.get("calendarOwner"),
            "workspace": scope["workspace_id"],
            "planningDocumentId": planning_id,
            "nativeCalendarId": result.get("calendarId"),
            "sourceRevision": result.get("sourceRevision"),
            "sourceHash": result.get("sourceHash"),
            "events": result.get("events", []),
            "diagnostics": result.get("diagnostics", []),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    indexed = await _call(request, "index", {**scope, "kind": "calendar-projection"})
    existing = next((doc for doc in indexed.get("docs", []) if doc.get("name") == name), None)
    if existing:
        write = await _call(
            request,
            "write",
            {**scope, "id": existing["id"], "content": payload, "base": existing.get("head")},
        )
        if write.get("outcome") == "stale":
            fresh = await _call(request, "get", {**scope, "id": existing["id"]})
            await _call(
                request,
                "write",
                {**scope, "id": existing["id"], "content": payload, "base": fresh.get("head")},
            )
    else:
        await _call(
            request,
            "create",
            {**scope, "name": name, "kind": "calendar-projection", "content": payload},
        )


async def _project_planning_document(
    request: Request,
    scope: dict[str, str],
    planning_doc: dict[str, Any],
    *,
    deleted: bool = False,
) -> dict[str, Any] | None:
    if planning_doc.get("kind") != "planning":
        return None
    try:
        planning = {} if deleted else json.loads(planning_doc.get("text") or "{}")
        if not isinstance(planning, dict):
            raise ValueError("planning document root must be an object")
        result = await asyncio.to_thread(
            reconcile_projection,
            planning,
            owner=_calendar_owner(request),
            workspace=scope["workspace_id"],
            planning_document_id=str(planning_doc["id"]),
            source_revision=str(planning_doc.get("head") or "") or None,
        )
        result["calendarOwner"] = _calendar_owner(request)
        if result.get("enabled"):
            try:
                await _persist_projection_linkage(request, scope, planning_doc, result)
            except Exception as exc:  # linkage is retryable; Copal commit stays canonical
                logger.warning("Copal calendar linkage write failed: %s", exc)
                result["linkageError"] = str(exc)
        return result
    except Exception as exc:
        logger.warning("Copal calendar projection failed after canonical commit: %s", exc)
        return {"enabled": True, "ok": False, "error": str(exc), "retryable": True}


async def _indexed_documents(request: Request, scope: dict[str, str]) -> list[dict[str, Any]]:
    indexed = await _call(request, "index", scope, timeout=60)
    return [_note_view(document) for document in indexed.get("docs") or [] if document.get("kind") != _OPERATION_KIND]


async def _planning_write_locked(request: Request, scope: dict[str, str]) -> bool:
    indexed = await _call(request, "index", {**scope, "kind": MIGRATION_KIND})
    for doc in indexed.get("docs") or []:
        try:
            marker = json.loads(str(doc.get("text") or "{}"))
        except json.JSONDecodeError:
            return True
        if marker.get("state") in {"applying", "complete"}:
            return True
    return False


async def _project_canonical_workspace(
    request: Request,
    scope: dict[str, str],
) -> dict[str, Any] | None:
    """Project all canonical event notes through the existing one-way Calendar writer."""
    docs = await _indexed_documents(request, scope)
    planning = planning_projection(docs)
    if not planning.get("canonical"):
        legacy = next((doc for doc in docs if doc.get("kind") == "planning"), None)
        return await _project_planning_document(request, scope, legacy) if legacy else None
    registry, _, _ = canonical_documents(docs)
    source = registry or next((doc for doc in docs if event_from_document(doc)), None)
    if not source:
        return None
    revision_docs = [source, *[doc for doc in docs if event_from_document(doc)]]
    try:
        result = await asyncio.to_thread(
            reconcile_projection,
            planning,
            owner=_calendar_owner(request),
            workspace=scope["workspace_id"],
            planning_document_id=str(source["id"]),
            source_revision=revision_fingerprint(revision_docs),
        )
        result["calendarOwner"] = _calendar_owner(request)
        if result.get("enabled"):
            try:
                await _persist_projection_linkage(request, scope, source, result)
            except Exception as exc:
                logger.warning("Copal canonical calendar linkage write failed: %s", exc)
                result["linkageError"] = str(exc)
        return result
    except Exception as exc:
        logger.warning("Copal canonical Calendar projection failed: %s", exc)
        return {"enabled": True, "ok": False, "error": str(exc), "retryable": True}


def _migration_report(planning_doc: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "legacyDocument": {"id": planning_doc.get("id"), "head": planning_doc.get("head")},
        "tracks": len(inventory["tracks"]),
        "events": len(inventory["events"]),
        "sharedEvents": sum(bool(event.get("sharedTrackIds")) for event in inventory["events"]),
        "fuzzyEvents": sum(event.get("startDate") == "FUZZY" or bool(event.get("fuzzy")) for event in inventory["events"]),
        "stages": sum(len(event.get("stages") or []) for event in inventory["events"]),
        "unknownFields": sum(len(event.get("copal_extra") or {}) for event in inventory["events"]),
        "diagnostics": inventory["diagnostics"],
    }


def _safe_export_name(doc: dict[str, Any]) -> str:
    name = _name(str(doc.get("name") or doc.get("id") or "Untitled"))
    suffix = PurePosixPath(name).suffix.lower()
    kind = str(doc.get("kind") or "markdown")
    if kind == TRACKS_KIND:
        return TRACKS_NAME
    if kind == MIGRATION_KIND:
        return MIGRATION_NAME
    if kind == "planning":
        return ".copal/planning.json"
    if kind.startswith("treehouse-") and suffix not in {".md", ".json"}:
        return f"TreeHouse/{name}.json"
    if not _is_asset_kind(kind) and kind not in {"base", "canvas"} and not suffix:
        return f"{name}.md"
    return name


async def _asset_file(request: Request, scope: dict[str, str], document_id: str, corpus: str | None = None) -> tuple[Path, str]:
    # Native Wiki assets live in the Wiki Redb alongside their page records;
    # older callers (image/audio URLs and Obsidian export) do not carry corpus
    # metadata, so probe both stores while retaining the scoped owner guard.
    asset = None
    # Preserve the legacy unqualified asset request first. The bridge treats
    # an omitted corpus as the Notes store, then we can fall back to Wiki for
    # older image URLs that do not carry corpus metadata.
    candidates = [corpus] if corpus else [None, "wiki"]
    for candidate in candidates:
        try:
            args = {**scope, "id": _doc_id(document_id)}
            if candidate:
                args["corpus"] = candidate
            asset = await _call(request, "asset_path", args)
            break
        except HTTPException as exc:
            if exc.status_code != 404 or corpus:
                raise
    if asset is None:
        raise HTTPException(404, "Asset not found")
    path = Path(asset["path"]).resolve()
    assets_root = (_bridge(request).data_dir / "assets").resolve()
    if path.parent != assets_root or not path.is_file():
        raise HTTPException(404, "Asset not found")
    return path, str(asset.get("name") or document_id)


def _native_record(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


async def _memes_export_payload(request: Request, scope: dict[str, str]) -> dict[str, Any]:
    listed = await _call(request, "list", {**scope, "corpus": "wiki"}, timeout=60)

    def scope_identity(value: dict[str, Any]) -> tuple[tuple[str, str, str, str], ...]:
        return tuple(sorted(
            (
                str(document.get("id") or ""),
                str(document.get("kind") or ""),
                str(document.get("name") or ""),
                str(document.get("head") or ""),
            )
            for document in value.get("docs") or []
            if not document.get("builtin")
            and not document.get("deleted")
            and document.get("kind") in {_WIKI_KIND, "asset"}
        ))

    initial_identity = scope_identity(listed)
    documents: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    expected_heads: dict[str, str] = {}
    for document in listed.get("docs") or []:
        if document.get("builtin") or document.get("deleted"):
            continue
        kind = str(document.get("kind") or "")
        if kind == _WIKI_KIND:
            raw = _preserved_source_bytes(document) or str(document.get("text") or "").encode("utf-8")
            documents.append({
                "exportId": str(document["id"]),
                "name": str(document["name"]),
                "kind": _WIKI_KIND,
                "record": _native_record(raw.decode("utf-8")),
                "rawSource": raw_source(raw),
            })
            if document.get("head"):
                expected_heads[str(document["id"])] = str(document["head"])
        elif kind == "asset":
            path, name = await _asset_file(request, scope, str(document["id"]), "wiki")
            data = await asyncio.to_thread(path.read_bytes)
            assets.append({
                "assetId": str(document["id"]),
                "name": name,
                "mime": mime_for_name(name),
                "byteLength": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "base64": __import__("base64").b64encode(data).decode("ascii"),
            })
            if document.get("head"):
                expected_heads[str(document["id"])] = str(document["head"])
    checked = await _call(request, "list", {**scope, "corpus": "wiki"}, timeout=60)
    if scope_identity(checked) != initial_identity:
        raise HTTPException(409, "Wiki changed during .memes export; retry")
    extensions: dict[str, Any] = {}
    if expected_heads:
        extensions["expectedHeads"] = expected_heads
    envelope = {
        "format": MEMES_FORMAT,
        "schemaVersion": MEMES_SCHEMA_VERSION,
        "documents": documents,
        "assets": assets,
        "extensions": extensions,
    }
    # Run the same strict validator used by preview/import before sending a
    # file to a client, so the producer cannot emit an invalid contract.
    return validate_memes_payload(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


async def _read_memes_upload(file: UploadFile) -> dict[str, Any]:
    filename = str(file.filename or "")
    content_type = str(file.content_type or "").split(";", 1)[0].strip().lower()
    if filename and not filename.casefold().endswith(".memes") and content_type != MEMES_MIME:
        raise HTTPException(415, "Choose a .memes file")
    raw = await read_upload_limited(file, COPAL_IMPORT_MAX_BYTES, ".memes file")
    try:
        return validate_memes_payload(raw)
    except MemesValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


async def _import_memes_payload(
    request: Request,
    scope: dict[str, str],
    payload: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    if mode not in {"import", "restore"}:
        raise HTTPException(400, "The .memes operation mode must be import or restore")
    listed = await _call(request, "list", {**scope, "corpus": "wiki"}, timeout=60)
    existing_portable_names: dict[str, dict[str, Any]] = {}
    for document in listed.get("docs") or []:
        if document.get("deleted"):
            continue
        name_key = unicodedata.normalize("NFC", str(document.get("name"))).casefold()
        prior = existing_portable_names.get(name_key)
        if prior is not None and (
            prior.get("id") != document.get("id") or prior.get("kind") != document.get("kind")
        ):
            raise HTTPException(409, f"Existing Wiki resources share a portable name: {document.get('name')}")
        existing_portable_names[name_key] = document
    extensions = payload.get("extensions", {})
    if "restore" in extensions and not isinstance(extensions["restore"], bool):
        raise HTTPException(400, "extensions.restore must be a boolean")
    if mode == "import" and extensions.get("restore") is True:
        raise HTTPException(400, "Choose Restore current Wiki explicitly for a guarded restore")
    restore_mode = mode == "restore"
    expected_heads = extensions.get("expectedHeads", {})
    if expected_heads is not None and not isinstance(expected_heads, dict):
        raise HTTPException(400, "extensions.expectedHeads must be an object")
    for export_id, expected in (expected_heads or {}).items():
        if not isinstance(export_id, str) or not isinstance(expected, str):
            raise HTTPException(400, "extensions.expectedHeads contains invalid values")
        current = next((doc for doc in listed.get("docs") or [] if str(doc.get("id")) == export_id), None)
        if current is not None and str(current.get("head") or "") != expected:
            raise HTTPException(409, "The .memes source is stale for this workspace")
    incoming_ids = {document["exportId"] for document in payload["documents"]} | {asset["assetId"] for asset in payload["assets"]}
    if restore_mode and set(expected_heads or {}) != incoming_ids:
        raise HTTPException(400, "A restore requires an expected head for every document and asset")
    if restore_mode:
        by_id = {str(doc.get("id")): doc for doc in listed.get("docs") or [] if not doc.get("deleted")}
        for document in payload["documents"]:
            current = by_id.get(document["exportId"])
            if not current or current.get("kind") != _WIKI_KIND or current.get("name") != document["name"]:
                raise HTTPException(409, f"Restore identity does not match Wiki name: {document['name']}")
        for asset in payload["assets"]:
            current = by_id.get(asset["assetId"])
            if not current or current.get("kind") != "asset" or current.get("name") != asset["name"]:
                raise HTTPException(409, f"Restore identity does not match asset name: {asset['name']}")
    # Native `.memes` archives use one portable `.copal/wiki/` namespace for
    # both pages and assets.  Keep this check kind-independent so a page and
    # an asset cannot overwrite one another while staging the vault.
    incoming_names: set[str] = set()
    for document in payload["documents"]:
        key = unicodedata.normalize("NFC", document["name"]).casefold()
        existing_document = existing_portable_names.get(key)
        if (
            existing_document is not None
            and (
                not restore_mode
                or existing_document.get("id") != document["exportId"]
                or existing_document.get("kind") != _WIKI_KIND
            )
        ) or key in incoming_names:
            raise HTTPException(409, f"Wiki name already exists: {document['name']}")
        incoming_names.add(key)
    for asset in payload["assets"]:
        key = unicodedata.normalize("NFC", asset["name"]).casefold()
        existing_asset = existing_portable_names.get(key)
        if (
            existing_asset is not None
            and (
                not restore_mode
                or existing_asset.get("id") != asset["assetId"]
                or existing_asset.get("kind") != "asset"
            )
        ) or key in incoming_names:
            raise HTTPException(409, f"Asset name already exists: {asset['name']}")
        incoming_names.add(key)

    asset_ids: dict[str, str] = {}
    document_ids: dict[str, str] = {}
    existing_ids = {str(doc.get("id")) for doc in listed.get("docs") or []}

    def new_id() -> str:
        while True:
            candidate = uuid.uuid4().hex
            if candidate not in existing_ids and candidate not in {*document_ids.values(), *asset_ids.values()}:
                return candidate

    with tempfile.TemporaryDirectory(prefix="copal-memes-") as temporary:
        root = Path(temporary) / "vault"
        wiki_root = root / ".copal" / "wiki"
        wiki_root.mkdir(parents=True)
        restore_ids: dict[str, dict[str, str]] = {}
        for document in payload["documents"]:
            document_ids[document["exportId"]] = new_id()
        for asset in payload["assets"]:
            asset_ids[asset["assetId"]] = new_id()
        if restore_mode:
            document_ids = {document["exportId"]: document["exportId"] for document in payload["documents"]}
            asset_ids = {asset["assetId"]: asset["assetId"] for asset in payload["assets"]}

        for document in payload["documents"]:
            original = document["record"]
            raw_source_meta = document.get("rawSource")
            content: bytes
            if raw_source_meta is not None:
                try:
                    source_bytes = __import__("base64").b64decode(raw_source_meta["base64"], validate=True)
                except (KeyError, TypeError, ValueError) as exc:
                    raise HTTPException(400, "The .memes rawSource is invalid") from exc
                if hashlib.sha256(source_bytes).hexdigest() != raw_source_meta.get("sha256"):
                    raise HTTPException(400, "The .memes rawSource digest is invalid")
                try:
                    parsed_source = json.loads(source_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed_source = None
                # Future, legacy, and malformed source bytes are authoritative
                # for recovery regardless of whether an exporter also supplied
                # a nonempty record projection.
                if not isinstance(parsed_source, dict) or parsed_source.get("schemaVersion") != _NOTE_SCHEMA_VERSION:
                    content = source_bytes
                else:
                    # A populated record is the explicit projection authority;
                    # an empty record delegates authority to rawSource. In
                    # either case retain raw bytes as provenance for recovery.
                    authoritative = original if original else parsed_source
                    remapped = remap_record(authoritative, document_ids, asset_ids)
                    # Keep the exporter-provided bytes as recovery/provenance
                    # even when supported native IDs and relations are remapped
                    # for this import.
                    extensions = remapped.setdefault("extensions", {})
                    if isinstance(extensions, dict):
                        extensions["rawSource"] = raw_source(source_bytes)
                    content = record_bytes(remapped)
            else:
                content = record_bytes(remap_record(original, document_ids, asset_ids))
            relative = PurePosixPath(".copal") / "wiki" / document["name"]
            target = root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            restore_ids[relative.as_posix()] = {"id": document_ids[document["exportId"]], "corpus": "wiki", "kind": "wiki"}
        for asset in payload["assets"]:
            data = __import__("base64").b64decode(asset["base64"], validate=True)
            relative = PurePosixPath(".copal") / "wiki" / asset["name"]
            target = root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            restore_ids[relative.as_posix()] = {"id": asset_ids[asset["assetId"]], "corpus": "wiki", "kind": "asset"}
        result = await _call(request, "import_vault", {
            **scope,
            "corpus": "wiki",
            "path": str(root),
            "planning_path": None,
            "note_kind": _WIKI_KIND,
            "restore_ids": restore_ids,
            "expected_heads": expected_heads if restore_mode else {},
        }, timeout=120)
    return {
        "documents": len(document_ids),
        "assets": len(asset_ids),
        "ids": document_ids,
        "assetIds": asset_ids,
        "restore": restore_mode,
        "operation": result.get("op"),
    }


def setup_copal_routes(*, policy_repository: FilePolicyRepository | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/copal", tags=["copal"])
    subscribers: dict[tuple[str, str], set[asyncio.Queue]] = defaultdict(set)

    def publish(scope: dict[str, str], event: str, data: dict[str, Any]) -> None:
        for queue in tuple(subscribers[(scope["owner"], scope["workspace_id"])]):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait({"event": event, "data": data})

    def lifecycle_repository(request: Request):
        return policy_repository or getattr(request.app.state, "files_policy_repository", None)

    def lifecycle_subject(request: Request, scope: dict[str, str]) -> str:
        return str(_actor_account_id(request) or scope["owner"])

    def lifecycle_key(action_id: str) -> str:
        return f"{_ATTACHMENT_LIFECYCLE_PREFIX}{action_id}"

    def lifecycle_load(request: Request, scope: dict[str, str], action_id: str) -> dict[str, Any] | None:
        repository = lifecycle_repository(request)
        getter = getattr(repository, "get_operation", None)
        if not callable(getter):
            return None
        loaded = getter(owner_subject_id=lifecycle_subject(request, scope), operation_id=lifecycle_key(action_id))
        return dict(loaded.get("receipt") or {}) if isinstance(loaded, Mapping) else None

    def lifecycle_save(request: Request, scope: dict[str, str], action_id: str, marker: Mapping[str, Any]) -> None:
        repository = lifecycle_repository(request)
        recorder = getattr(repository, "record_operation", None)
        if not callable(recorder):
            raise HTTPException(503, "Attachment lifecycle storage is unavailable")
        immutable = {
            key: marker.get(key)
            for key in (
                "action_id", "owner", "workspace_id", "generation", "document_id", "base",
                "source_text_hash", "source_ref", "source_item_id", "source_revision",
                "target_ref", "target_revision", "asset_name", "asset_size", "asset_digest", "intended_content_hash", "mime",
            )
        }
        digest = hashlib.sha256(json.dumps(immutable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        phase = str(marker.get("phase") or "pending")
        try:
            recorder(owner_subject_id=lifecycle_subject(request, scope), operation_id=lifecycle_key(action_id), request_digest=digest, generation=int(marker.get("generation") or 0), receipt=dict(marker), phase=phase)
        except TypeError:
            # Keep narrow third-party repositories readable during rollout;
            # the production repository persists the indexed phase.
            recorder(owner_subject_id=lifecycle_subject(request, scope), operation_id=lifecycle_key(action_id), request_digest=digest, generation=int(marker.get("generation") or 0), receipt=dict(marker))

    async def attachment_asset(request: Request, scope: dict[str, str], marker: Mapping[str, Any]) -> dict[str, Any] | None:
        """Resolve the exact prepared asset without scanning document bodies.

        The legacy route has no provider-side preparation descriptor, so its
        crash marker identifies the asset by a deterministic name plus byte
        digest/size.  Name alone is never sufficient: a collision or an asset
        belonging to another operation is treated as unavailable.
        """
        expected_id = str(marker.get("asset_id") or "").strip()
        expected_name = str(marker.get("asset_name") or "").strip()
        expected_size = marker.get("asset_size")
        expected_digest = str(marker.get("asset_digest") or "").strip().lower()
        if not expected_name or not expected_digest:
            return None
        raw: Any = None
        try:
            if expected_id:
                raw = await _call(request, "get", {**scope, "id": _doc_id(expected_id)})
            else:
                raw = await _call(request, "find_by_name", {**scope, "name": expected_name, "corpus": "all"})
        except Exception:
            return None
        if isinstance(raw, Mapping) and isinstance(raw.get("doc"), Mapping):
            raw = raw["doc"]
        if not isinstance(raw, Mapping):
            return None
        actual_id = str(raw.get("id") or "").strip()
        if not actual_id or (expected_id and actual_id != expected_id):
            return None
        if str(raw.get("kind") or "") != "asset" or str(raw.get("name") or "") != expected_name:
            return None
        try:
            if int(raw.get("size")) != int(expected_size):
                return None
        except (TypeError, ValueError):
            return None
        head = str(raw.get("head") or "")
        head_digest = head.split(":", 2)[1].lower() if head.startswith("sha256:") and ":" in head else ""
        if head_digest:
            if head_digest != expected_digest:
                return None
        else:
            # Some bridge implementations omit the content fingerprint from
            # metadata.  Verify the bytes through the scoped asset resolver;
            # without that proof the asset cannot be recovered or committed.
            try:
                path, _name = await _asset_file(request, scope, actual_id)
                if hashlib.sha256(await asyncio.to_thread(path.read_bytes)).hexdigest().lower() != expected_digest:
                    return None
            except Exception:
                return None
        return dict(raw)

    async def attachment_document_matches(request: Request, scope: dict[str, str], marker: Mapping[str, Any], asset_name: str) -> bool:
        """Prove that a staged write already reached its exact target.

        This is one targeted document read per marker, never a corpus scan.
        It lets reaping finalize a crash after the document CAS but before the
        lifecycle transition, while ordinary failed/stale staged assets are
        still collected.
        """
        intended_hash = str(marker.get("intended_content_hash") or "").strip().lower()
        document_id = str(marker.get("document_id") or "").strip()
        if not intended_hash or not document_id:
            return False
        try:
            document = _note_view(await _call(request, "get", {**scope, "id": _doc_id(document_id)}))
        except Exception:
            return False
        text = str(document.get("text") or "")
        return hashlib.sha256(text.encode("utf-8")).hexdigest().lower() == intended_hash and bool(
            re.search(rf"!\[\[{re.escape(asset_name)}(?:\|[^\]\n]*)?\]\]", text)
        )

    async def reap_attachment_markers(request: Request, scope: dict[str, str]) -> None:
        repository = lifecycle_repository(request)
        lister = getattr(repository, "list_operations", None)
        if not callable(lister):
            return
        now = time.time()
        last_reap = float(getattr(request.app.state, "copal_attachment_last_reap", 0.0) or 0.0)
        if now - last_reap < 60:
            return
        request.app.state.copal_attachment_last_reap = now
        # Filter in the lifecycle store so terminal records cannot consume a
        # bounded page before pending/staged markers are considered.  Always
        # consume from offset zero because each reaped row leaves the phase.
        for phase in ("pending", "staged"):
            phase_supported = True
            fallback_offset = 0
            for _page in range(64):
                if phase_supported:
                    try:
                        rows = lister(owner_subject_id=lifecycle_subject(request, scope), operation_prefix=_ATTACHMENT_LIFECYCLE_PREFIX, phase=phase, offset=0, limit=256)
                    except TypeError:
                        # Compatibility with a narrow test/embedding repository.
                        phase_supported = False
                        rows = lister(owner_subject_id=lifecycle_subject(request, scope), operation_prefix=_ATTACHMENT_LIFECYCLE_PREFIX, offset=fallback_offset, limit=256)
                else:
                    rows = lister(owner_subject_id=lifecycle_subject(request, scope), operation_prefix=_ATTACHMENT_LIFECYCLE_PREFIX, offset=fallback_offset, limit=256)
                if not rows:
                    break
                changed = False
                for row in rows:
                    marker = row.get("receipt") if isinstance(row, Mapping) else None
                    if not isinstance(marker, Mapping) or marker.get("phase") != phase:
                        continue
                    operation_id = str(row.get("operation_id") or "") if isinstance(row, Mapping) else ""
                    expected_action_id = operation_id.removeprefix(_ATTACHMENT_LIFECYCLE_PREFIX)
                    if not expected_action_id or str(marker.get("action_id") or "") != expected_action_id:
                        continue
                    try:
                        created = float(marker.get("created_unix_ms") or row.get("created_unix_ms", 0)) / 1000
                    except (TypeError, ValueError, OverflowError):
                        # A corrupt timestamp must not make a status request
                        # fail or turn an otherwise active marker into a reap.
                        continue
                    if not math.isfinite(created):
                        continue
                    if marker.get("owner") != scope["owner"]:
                        continue
                    if created <= 0 or now - created < _ATTACHMENT_PENDING_RETENTION_SECONDS:
                        continue
                    if marker.get("workspace_id") != scope["workspace_id"]:
                        continue
                    asset_id = str(marker.get("asset_id") or "").strip()
                    asset_name = str(marker.get("asset_name") or "").strip()
                    if not asset_name:
                        continue
                    try:
                        asset = await attachment_asset(request, scope, marker)
                        if asset is not None:
                            asset_id = str(asset["id"])
                            if await attachment_document_matches(request, scope, marker, asset_name):
                                receipt = marker.get("receipt") or {"outcome": "recovered", "actionId": marker.get("action_id"), "assetId": asset_id, "documentId": marker.get("document_id")}
                                lifecycle_save(request, scope, str(marker.get("action_id") or ""), {**dict(marker), "asset_id": asset_id, "phase": "consumed", "receipt": receipt, "consumed_at": now})
                                changed = True
                                continue
                            await _call(request, "delete", {**scope, "id": _doc_id(asset_id), "action_id": f"reap-{str(marker.get('action_id') or '')[:120]}"}, timeout=60)
                        lifecycle_save(request, scope, str(marker.get("action_id") or ""), {**dict(marker), "asset_id": asset_id or marker.get("asset_id"), "phase": "reaped", "reaped_at": now})
                        changed = True
                    except Exception:
                        continue
                if not phase_supported:
                    # The compatibility path cannot filter in SQL. Advance
                    # through bounded pages so terminal rows cannot hide a
                    # pending/staged marker behind the first page.  A changed
                    # row leaves the phase and shifts later rows left, so
                    # restart from zero after every mutation rather than
                    # skipping the row that moved into the current page.
                    fallback_offset = 0 if changed else fallback_offset + len(rows)
                elif not changed:
                    break

    @router.get("/status")
    async def status(request: Request, workspace: str | None = None):
        authenticated_owner = require_user(request)
        scope = {
            "owner": copal_owner_for_user(authenticated_owner),
            "workspace_id": _workspace(request, workspace),
        }
        result = await _call(request, "scoped_status", scope)
        return {
            **result,
            "visible_documents": result.get("documents", 0),
            "owner": scope["owner"],
            "workspace": scope["workspace_id"],
            "storage_namespace": "local" if not authenticated_owner else f"user:{authenticated_owner}",
            "account_id": _actor_account_id(request),
        }

    @router.get("/documents")
    async def list_documents(
        request: Request,
        workspace: str | None = None,
        query: str = Query("", max_length=512),
        kind: str | None = Query(None, max_length=64),
        corpus: str = Query("all", pattern="^(all|notes|wiki)$"),
        hidden: str = Query("exclude", pattern="^(exclude|include|only)$"),
    ):
        scope = _scope(request, workspace)
        if kind and not _KIND.fullmatch(kind):
            raise HTTPException(400, "Invalid document kind")
        result = await _call(request, "index", {**scope, "query": "", "kind": kind, "corpus": corpus})
        documents = [_resource_view(request, scope, document) for document in result.get("docs") or []]
        if kind is None:
            documents = [
                document
                for document in documents
                if not _is_compatibility_kind(document.get("kind")) and document.get("kind") != _OPERATION_KIND
            ]
        if hidden != "include":
            documents = [
                document
                for document in documents
                if bool(document.get("hidden")) is (hidden == "only")
            ]
        if corpus != "all":
            wanted = _WIKI_KIND if corpus == "wiki" else _NOTE_KIND
            documents = [document for document in documents if document.get("kind") == wanted]
        if query:
            needle = query.casefold()
            documents = [
                document for document in documents
                if needle in str(document.get("name") or "").casefold()
                or needle in str(document.get("text") or "").casefold()
                or needle in json.dumps(document.get("properties") or {}, ensure_ascii=False).casefold()
                or any(needle in str(tag).casefold() for tag in document.get("tags") or [])
            ]
        result["docs"] = documents
        return result

    @router.get("/planning")
    async def get_planning_projection(request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        try:
            return planning_projection(await _indexed_documents(request, scope))
        except PlanningValidationError as exc:
            raise HTTPException(422, str(exc)) from exc

    def _task_index_name(scope: dict[str, str]) -> str:
        identity = f"{scope['owner']}\0{scope['workspace_id']}".encode("utf-8")
        return f".copal/task-index/{hashlib.sha256(identity).hexdigest()}.json"

    async def _task_index_record(request: Request, scope: dict[str, str], indexed: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        name = _task_index_name(scope)
        # The task index is itself a durable Copal document. Resolve it by
        # identity so a repeat task query does not enumerate or read every
        # note just to discover the cached projection. Older bridges do not
        # expose this operation yet, so retain the indexed fallback during
        # the rolling bridge upgrade.
        bridge = _bridge(request)
        if indexed is None and getattr(bridge, "supports_task_index_lookup", False):
            try:
                document = await _call(request, "find_by_name", {**scope, "name": name, "corpus": "notes"})
            except (HTTPException, CopalBridgeError):
                document = None
            if isinstance(document, dict) and document.get("id"):
                try:
                    record = json.loads(str(document.get("text") or "{}"))
                except json.JSONDecodeError:
                    return None, document
                return record if isinstance(record, dict) else None, document
            return None, None
        if indexed is None:
            indexed = await _call(request, "index", scope, timeout=60)
        for document in indexed.get("docs") or []:
            if document.get("name") != name:
                continue
            try:
                record = json.loads(str(document.get("text") or "{}"))
            except json.JSONDecodeError:
                return None, document
            return record if isinstance(record, dict) else None, document
        return None, None

    def _task_record_items(record: dict[str, Any]) -> list[dict[str, Any]]:
        documents = record.get("documents")
        if isinstance(documents, dict):
            return [
                item
                for entry in documents.values()
                if isinstance(entry, dict)
                for item in entry.get("items") or []
                if isinstance(item, dict)
            ]
        return [item for item in record.get("items") or [] if isinstance(item, dict)]

    async def _store_task_index(
        request: Request,
        scope: dict[str, str],
        revision: str,
        items: list[dict[str, Any]],
        indexed: dict[str, Any] | None = None,
        documents: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        # Callers that already resolved the durable record can avoid a second
        # full index enumeration. Legacy callers may still pass ``indexed``.
        if indexed is None:
            indexed = {"docs": []}
        record, document = await _task_index_record(request, scope, indexed)
        if document is None:
            record, document = await _task_index_record(request, scope)
        content = json.dumps(
            {
                "schemaVersion": 2,
                "sourceRevision": revision,
                "items": items,
                "documents": documents or {},
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if document:
            result = await _call(request, "write", {**scope, "id": _doc_id(str(document.get("id") or "")), "content": content, "base": document.get("head"), "corpus": "notes"})
            if result.get("outcome") == "stale":
                latest = await _call(request, "get", {**scope, "id": _doc_id(str(document.get("id") or ""))})
                await _call(request, "write", {**scope, "id": _doc_id(str(document.get("id") or "")), "content": content, "base": latest.get("head"), "corpus": "notes"})
            return
        await _call(request, "create", {**scope, "name": _task_index_name(scope), "kind": _OPERATION_KIND, "content": content, "corpus": "notes"})

    async def _task_generation(
        request: Request,
        scope: dict[str, str],
        index_id: str | None,
        cached_revision: str | None = None,
        cached_document_ids: set[str] | None = None,
    ) -> tuple[str | None, list[str] | None]:
        """Return the newest source generation and every changed source id.

        The operation feed is newest-first. A cached index may be several
        source writes behind, so consuming only the first operation would
        silently lose edits to unopened notes. Walk the parent chain until the
        cached source operation, unioning all source changes. If the bounded
        operation retention window cannot reach that generation, force a
        complete rebuild instead of claiming the cache is current.
        """
        before = None
        changed_ids: set[str] = set()
        newest_source: str | None = None
        reached_cached = cached_revision is None
        for _ in range(128):
            result = await _call(request, "ops", {**scope, "limit": 1, **({"before": before} if before else {})})
            operation = (result.get("ops") or [None])[0]
            if not operation:
                # No cached operation means this is a legacy fingerprint or a
                # truncated history; callers must rebuild from source notes.
                return (newest_source, sorted(changed_ids)) if reached_cached else (None, None)
            operation_id = str(operation.get("op") or operation.get("id") or "")
            changed = [str(value) for value in operation.get("changedIds") or []]
            cached_changed_ids: set[str] = set(cached_document_ids or ())
            if cached_revision and changed and not cached_changed_ids and getattr(_bridge(request), "supports_keyed_task_index", False):
                try:
                    cached_rows = await _call(request, "task_index_get", {**scope, "ids": changed})
                    cached_changed_ids = {
                        str(value)
                        for value in (cached_rows.get("documents") or {}).keys()
                    }
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
                    cached_changed_ids = set()
            if index_id and changed and set(changed) == {index_id}:
                before = operation_id
                continue
            # The cached source operation is the boundary. Do not fetch its
            # document merely to classify it; repeat queries should touch the
            # durable index only.
            if cached_revision and operation_id == cached_revision:
                reached_cached = True
                return newest_source or cached_revision, sorted(changed_ids)
            operational_only = True
            for changed_id in changed:
                if index_id and changed_id == index_id:
                    continue
                try:
                    document = await _call(request, "get", {**scope, "id": _doc_id(changed_id)})
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
                    if changed_id in cached_changed_ids:
                        # A tombstone is a source change even though there is
                        # no current document to classify as an operational
                        # record. The patch phase removes its cached rows.
                        operational_only = False
                    continue
                if document.get("kind") != _OPERATION_KIND:
                    operational_only = False
                    break
            if changed and operational_only:
                before = operation_id
                continue
            if newest_source is None:
                newest_source = operation_id
            changed_ids.update(changed)
            before = operation_id
            if not cached_revision:
                # Initial builds intentionally go through the full source
                # index; there is no durable per-document cache to patch.
                return newest_source, sorted(changed_ids)
        return None, None

    def _keyed_task_index_enabled(request: Request) -> bool:
        return bool(
            getattr(_bridge(request), "supports_task_index_lookup", False)
            and getattr(_bridge(request), "supports_keyed_task_index", False)
        )

    async def _task_snapshot_keyed(request: Request, scope: dict[str, str]) -> tuple[list[dict[str, Any]], str]:
        """Reconcile the keyed bridge projection without materializing it on reads."""
        meta = await _call(request, "task_index_generation", scope)
        cached_revision = str(meta.get("sourceRevision") or "")
        generation, changed_ids = await _task_generation(
            request, scope, None, cached_revision or None, None
        )
        if cached_revision and changed_ids is not None and generation == cached_revision and not changed_ids:
            return [], cached_revision
        if cached_revision and changed_ids is not None and generation:
            changed = {str(value) for value in changed_ids if str(value)}
            changed_records = await _call(request, "task_index_get", {**scope, "ids": sorted(changed)})
            existing = changed_records.get("documents") if isinstance(changed_records, dict) else {}
            updates: dict[str, dict[str, Any]] = {}
            removed: list[str] = []
            for changed_id in changed:
                try:
                    fresh = await _call(request, "get", {**scope, "id": _doc_id(changed_id)})
                except HTTPException as exc:
                    if exc.status_code == 404:
                        removed.append(changed_id)
                        continue
                    raise
                if fresh.get("kind") in _NOTE_KINDS or str(fresh.get("kind") or "") in {"markdown", "text"}:
                    resource = (_resource_view(request, scope, fresh).get("resource") or {}).get("key") or {}
                    updates[changed_id] = {
                        "head": fresh.get("head"),
                        "resourceId": str(resource.get("resourceId") or ""),
                        "items": _task_projection(request, scope, fresh),
                    }
                else:
                    removed.append(changed_id)
            await _call(request, "task_index_update", {
                **scope,
                "generation": generation,
                "records": updates,
                "removed": removed,
                "sourceReads": len(changed),
            })
            return [], generation
        indexed = await _call(request, "index", scope, timeout=60)
        raw_documents = [
            document for document in indexed.get("docs") or []
            if document.get("kind") in _NOTE_KINDS or str(document.get("kind") or "") in {"markdown", "text"}
        ]
        revision = generation or _task_revision(raw_documents)
        document_records = {
            str(document.get("id")): {
                "head": document.get("head"),
                "resourceId": str(((_resource_view(request, scope, document).get("resource") or {}).get("key") or {}).get("resourceId") or ""),
                "items": _task_projection(request, scope, document),
            }
            for document in raw_documents if document.get("id")
        }
        await _call(request, "task_index_update", {
            **scope,
            "generation": revision,
            "records": document_records,
            "removed": [],
            "rebuild": True,
            "sourceReads": len(raw_documents),
        })
        items = [item for record in document_records.values() for item in record.get("items") or []]
        items.sort(key=lambda item: (str(item.get("label") or item.get("text") or "").casefold(), str(item.get("id") or "")))
        return items, revision

    async def _task_keyed_ready(request: Request, scope: dict[str, str]) -> tuple[str, int]:
        """Ensure generation is current; page rows remain inside the bridge."""
        meta = await _call(request, "task_index_generation", scope)
        revision = str(meta.get("sourceRevision") or "")
        if not revision:
            _, revision = await _task_snapshot_keyed(request, scope)
            meta = await _call(request, "task_index_generation", scope)
            revision = str(meta.get("sourceRevision") or revision)
        generation, changed_ids = await _task_generation(request, scope, None, revision, None)
        if changed_ids is None or generation != revision or changed_ids:
            await _task_snapshot_keyed(request, scope)
            meta = await _call(request, "task_index_generation", scope)
            revision = str(meta.get("sourceRevision") or revision)
        return revision, int(meta.get("total") or 0)

    async def _task_snapshot(request: Request, scope: dict[str, str]) -> tuple[list[dict[str, Any]], str]:
        # Locate the durable projection in O(1). The full document index is
        # needed only for the first build or when a bridge cannot report a
        # change generation (compatibility with pre-index bridges).
        indexed = None
        record, index_document = await _task_index_record(request, scope)
        generation, changed_ids = await _task_generation(
            request,
            scope,
            index_document.get("id") if index_document else None,
            str(record.get("sourceRevision") or "") if isinstance(record, dict) else None,
            {
                str(document_id)
                for document_id in (record.get("documents") or {}).keys()
            }
            if isinstance(record, dict) and isinstance(record.get("documents"), dict)
            else None,
        )
        revision = generation or (str(record.get("sourceRevision")) if isinstance(record, dict) else None)
        # A bridge without a continuous operation boundary requires a source
        # rebuild; never carry the stale cached revision into that fallback.
        if changed_ids is None:
            revision = None
        if (
            isinstance(record, dict)
            and changed_ids is not None
            and record.get("sourceRevision") == revision
            and isinstance(record.get("items"), list)
        ):
            return _task_record_items(record), revision
        if isinstance(record, dict) and isinstance(record.get("items"), list) and changed_ids is not None:
            changed = {str(value) for value in changed_ids if str(value)}
            document_records = {
                str(document_id): dict(entry)
                for document_id, entry in (record.get("documents") or {}).items()
                if isinstance(entry, dict)
            }
            # Migrate a v1 flat cache in memory. It becomes per-document on
            # the next write, while old records remain queryable.
            if not document_records:
                for item in _task_record_items(record):
                    document_id = str((item.get("document") or {}).get("id") or "")
                    if document_id:
                        document_records.setdefault(document_id, {"head": item.get("sourceRevision"), "items": []})["items"].append(item)
            for changed_id in changed:
                try:
                    fresh = await _call(request, "get", {**scope, "id": _doc_id(changed_id)})
                except HTTPException as exc:
                    if exc.status_code == 404:
                        document_records.pop(changed_id, None)
                        continue
                    raise
                if fresh.get("kind") in _NOTE_KINDS or str(fresh.get("kind") or "") in {"markdown", "text"}:
                    document_records[changed_id] = {"head": fresh.get("head"), "items": _task_projection(request, scope, fresh)}
                else:
                    document_records.pop(changed_id, None)
            items = [item for entry in document_records.values() for item in entry.get("items") or [] if isinstance(item, dict)]
            items.sort(key=lambda item: (str(item.get("label") or "").casefold(), str(item.get("id") or "")))
            await _store_task_index(request, scope, revision, items, indexed, document_records)
            return items, revision
        indexed = await _call(request, "index", scope, timeout=60)
        raw_documents = [
            document for document in indexed.get("docs") or []
            if document.get("kind") in _NOTE_KINDS or str(document.get("kind") or "") in {"markdown", "text"}
        ]
        # A bridge with no operation history (including old loose stores) uses
        # the metadata fingerprint only for this initial/recovery build.
        if not revision:
            revision = _task_revision(raw_documents)
        # _task_projection owns the single _resource_view/_note_view pass.
        # Passing an already parsed note back through _note_view would treat
        # its plain Markdown body as JSON and erase every task row.
        documents = raw_documents
        document_records = {
            str(document.get("id")): {"head": document.get("head"), "items": _task_projection(request, scope, document)}
            for document in documents
            if document.get("id")
        }
        items = [item for entry in document_records.values() for item in entry.get("items") or []]
        items.sort(key=lambda item: (str(item.get("label") or "").casefold(), str(item.get("id") or "")))
        await _store_task_index(request, scope, revision, items, indexed, document_records)
        return items, revision

    async def _refresh_task_index(request: Request, scope: dict[str, str]) -> None:
        """Refresh the durable projection after a source mutation or external refresh."""
        # Test/legacy bridges without the incremental lookup capability remain
        # query-on-demand; refreshing them would add an unrelated operation to
        # every document mutation while providing no durable cache benefit.
        if not getattr(_bridge(request), "supports_task_index_lookup", False):
            return
        if _keyed_task_index_enabled(request):
            await _task_keyed_ready(request, scope)
        else:
            await _task_snapshot(request, scope)

    async def _task_projection_receipt(request: Request, scope: dict[str, str]) -> dict[str, Any]:
        """Finalize the task projection after the source write has committed.

        The document/head is authoritative.  A projection outage therefore
        becomes a retryable receipt on a successful response instead of
        turning a durable write into an apparent failed request.
        """
        try:
            await _refresh_task_index(request, scope)
        except Exception:
            logger.warning("Copal task projection failed after source commit", exc_info=True)
            return {"status": "failed", "retryable": True, "code": "task_projection_failed"}
        return {"status": "ready", "retryable": False, "code": None}

    def _with_task_projection(result: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
        projections = result.get("projections")
        if not isinstance(projections, dict):
            projections = {}
        result["projections"] = {**projections, "tasks": receipt}
        return result

    async def _resolve_task_document(
        request: Request,
        scope: dict[str, str],
        resource_key: dict[str, Any],
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        resource_id = str(resource_key.get("resourceId") or "") if isinstance(resource_key, dict) else ""
        if resource_id and _keyed_task_index_enabled(request):
            resolved = await _call(request, "task_index_resolve", {**scope, "resourceId": resource_id})
            resolved_id = str(resolved.get("id") or "") if isinstance(resolved, dict) else ""
            if resolved_id and _ID.fullmatch(resolved_id):
                direct = await _call(request, "get", {**scope, "id": resolved_id})
                view = _resource_view(request, scope, direct)
                if (view.get("resource") or {}).get("key") == resource_key:
                    return direct
            raise HTTPException(404, detail={"outcome": "target_missing", "message": "Task resource is not in this workspace"})
        # Task IDs are source-authoritative and begin with the document id.
        # Resolve that id directly for checkbox edits; the opaque resource key
        # is still checked below so it cannot widen the tenant boundary.
        if task_id and ":" in task_id:
            document_id = task_id.split(":", 1)[0]
            if _ID.fullmatch(document_id):
                try:
                    direct = await _call(request, "get", {**scope, "id": document_id})
                except HTTPException as exc:
                    if exc.status_code == 404:
                        direct = None
                    else:
                        raise
                except KeyError as exc:
                    # The in-memory compatibility bridge exposes an explicit
                    # missing map key as KeyError(id). Any protocol/I/O error
                    # with another shape must remain visible to the caller.
                    if exc.args != (document_id,):
                        raise
                    direct = None
                if direct is not None:
                    view = _resource_view(request, scope, direct)
                    if (view.get("resource") or {}).get("key") == resource_key:
                        return direct
        documents = await _indexed_documents(request, scope)
        for document in documents:
            view = _resource_view(request, scope, document)
            if (view.get("resource") or {}).get("key") == resource_key:
                return document
        # Resource keys are opaque.  Deliberately do not accept a path or an
        # unscoped document id as a write authority.
        raise HTTPException(404, detail={"outcome": "target_missing", "message": "Task resource is not in this workspace"})

    async def _find_task_action(request: Request, scope: dict[str, str], action_id: str, digest: str) -> dict[str, Any] | None:
        indexed = await _call(request, "index", {**scope, "kind": _OPERATION_KIND})
        name = f".copal/task-actions/{hashlib.sha256(action_id.encode('utf-8')).hexdigest()}.json"
        for document in indexed.get("docs") or []:
            if document.get("name") != name:
                continue
            try:
                record = json.loads(str(document.get("text") or "{}"))
            except json.JSONDecodeError as exc:
                raise HTTPException(409, detail={"outcome": "idempotency_conflict", "message": "Task action receipt is invalid"}) from exc
            if record.get("digest") != digest:
                raise HTTPException(409, detail={"outcome": "idempotency_conflict", "message": "Action id was already used for a different task mutation"})
            result = record.get("result")
            return {**result, "replayed": True} if isinstance(result, dict) else None
        return None

    async def _record_task_action(request: Request, scope: dict[str, str], action_id: str, digest: str, result: dict[str, Any]) -> dict[str, Any]:
        name = f".copal/task-actions/{hashlib.sha256(action_id.encode('utf-8')).hexdigest()}.json"
        record = {"schemaVersion": 1, "actionId": action_id, "digest": digest, "result": result}
        try:
            await _call(request, "create", {**scope, "name": name, "kind": _OPERATION_KIND, "content": json.dumps(record, sort_keys=True, separators=(",", ":"))})
        except HTTPException as exc:
            if exc.status_code == 409:
                replay = await _find_task_action(request, scope, action_id, digest)
                if replay is not None:
                    return replay
            raise
        return result

    async def _find_document_action(request: Request, scope: dict[str, str], action_id: str, digest: str) -> dict[str, Any] | None:
        indexed = await _call(request, "index", {**scope, "kind": _OPERATION_KIND})
        name = f"{_DOCUMENT_ACTION_PREFIX}{hashlib.sha256(action_id.encode('utf-8')).hexdigest()}.json"
        for document in indexed.get("docs") or []:
            if document.get("name") != name:
                continue
            try:
                record = json.loads(str(document.get("text") or "{}"))
            except json.JSONDecodeError as exc:
                raise HTTPException(409, detail={"outcome": "idempotency_conflict", "message": "Document action receipt is invalid"}) from exc
            if record.get("digest") != digest:
                raise HTTPException(409, detail={"outcome": "idempotency_conflict", "message": "Action id was already used for a different document creation"})
            result = record.get("result")
            return {**result, "replayed": True} if isinstance(result, dict) else None
        return None

    async def _record_document_action(request: Request, scope: dict[str, str], action_id: str, digest: str, result: dict[str, Any]) -> dict[str, Any]:
        name = f"{_DOCUMENT_ACTION_PREFIX}{hashlib.sha256(action_id.encode('utf-8')).hexdigest()}.json"
        record = {"schemaVersion": 1, "actionId": action_id, "digest": digest, "result": result}
        try:
            await _call(request, "create", {**scope, "name": name, "kind": _OPERATION_KIND, "content": json.dumps(record, sort_keys=True, separators=(",", ":"))})
        except HTTPException as exc:
            # The Redb bridge reports its scoped name collision as 400 while
            # compatibility providers may use 409. In either case, a receipt
            # that won the race is authoritative and safe to replay.
            if exc.status_code in {400, 409}:
                replay = await _find_document_action(request, scope, action_id, digest)
                if replay is not None:
                    return replay
            raise
        return result

    @router.get("/tasks/query")
    @router.get("/tasks")
    async def query_tasks(
        request: Request,
        workspace: str | None = None,
        query: str = Query("", max_length=512),
        completed: str | None = Query(None, alias="completed", pattern="^(true|false)$"),
        source: str = Query("all", pattern="^(all|vault|markdown)$"),
        page_size: int = Query(100, alias="pageSize", ge=1, le=500),
        cursor: str | None = None,
        snapshot_revision: str | None = Query(None, alias="snapshotRevision", max_length=128),
    ):
        scope = _scope(request, workspace)
        if _keyed_task_index_enabled(request):
            bridge_source = "vault" if source == "markdown" else source
            revision, total = await _task_keyed_ready(request, scope)
            if snapshot_revision and snapshot_revision != revision:
                raise HTTPException(409, detail={"outcome": "stale_snapshot", "snapshotRevision": revision})
            bridge_cursor = None
            if cursor:
                parsed = _read_task_cursor(cursor)
                if (
                    parsed.get("owner") != scope["owner"]
                    or parsed.get("workspace") != scope["workspace_id"]
                    or parsed.get("query") != query
                    or parsed.get("completed") != completed
                    or parsed.get("source", "all") != source
                ):
                    raise HTTPException(409, detail={"outcome": "stale_cursor", "message": "Task cursor belongs to another query"})
                if parsed["revision"] != revision:
                    raise HTTPException(409, detail={"outcome": "stale_cursor", "snapshotRevision": revision})
                bridge_cursor = parsed.get("bridgeCursor")
                if not isinstance(bridge_cursor, str) or not bridge_cursor:
                    raise HTTPException(409, detail={"outcome": "stale_cursor", "message": "Task cursor has no bridge anchor"})
            page_result = await _call(request, "task_index_page", {
                **scope,
                "query": query,
                "completed": None if completed is None else completed == "true",
                "source": bridge_source,
                "cursor": bridge_cursor,
                "limit": page_size,
                "generation": revision,
            })
            page = [item for item in page_result.get("items") or [] if isinstance(item, dict)]
            next_bridge_cursor = page_result.get("nextCursor")
            next_cursor = (
                _task_cursor(
                    scope,
                    revision,
                    0,
                    query,
                    completed,
                    source=source,
                    bridge_cursor=str(next_bridge_cursor),
                )
                if next_bridge_cursor
                else None
            )
            metrics = {
                "sourceReads": int(page_result.get("sourceReads") or 0),
                "scannedRows": int(page_result.get("scannedRows") or 0),
                "scannedBytes": int(page_result.get("scannedBytes") or 0),
                "returnedRows": int(page_result.get("returnedRows") or len(page)),
                "rewrittenRows": int(page_result.get("rewrittenRows") or 0),
                "rewrittenBytes": int(page_result.get("rewrittenBytes") or 0),
            }
            matched_total = page_result.get("matchedTotal")
            indexed_total = int(page_result.get("indexedTotal") or total)
            return {
                "items": page,
                "tasks": page,
                "nextCursor": next_cursor,
                "snapshotRevision": revision,
                "queryRevision": revision,
                "complete": next_cursor is None,
                "total": int(matched_total if matched_total is not None else page_result.get("total") or total),
                "indexedTotal": indexed_total,
                "matchedTotal": matched_total,
                "totalExact": bool(page_result.get("totalExact", matched_total is not None)),
                "metrics": metrics,
                **metrics,
            }
        documents, revision = await _task_snapshot(request, scope)
        if snapshot_revision and snapshot_revision != revision:
            raise HTTPException(409, detail={"outcome": "stale_snapshot", "snapshotRevision": revision})
        offset = 0
        if cursor:
            parsed = _read_task_cursor(cursor)
            if parsed.get("owner") != scope["owner"] or parsed.get("workspace") != scope["workspace_id"] or parsed.get("query") != query or parsed.get("completed") != completed:
                raise HTTPException(409, detail={"outcome": "stale_cursor", "message": "Task cursor belongs to another query"})
            if parsed["revision"] != revision:
                raise HTTPException(409, detail={"outcome": "stale_cursor", "snapshotRevision": revision})
            offset = parsed["offset"]
        needle = query.casefold()
        effective_source = "vault" if source == "markdown" else source
        items = [
            item for item in documents
            if (effective_source == "all" or item.get("source") == effective_source)
            and (completed is None or item.get("checked") == (completed == "true"))
            and (not needle or needle in f"{item.get('text', '')} {item.get('label', '')}".casefold())
        ]
        page = items[offset:offset + page_size]
        next_offset = offset + len(page)
        next_cursor = _task_cursor(scope, revision, next_offset, query, completed, source=source) if next_offset < len(items) else None
        return {
            "items": page,
            "tasks": page,
            "nextCursor": next_cursor,
            "snapshotRevision": revision,
            "queryRevision": revision,
            "complete": next_cursor is None,
            "total": len(items),
        }

    async def _write_task(request: Request, scope: dict[str, str], payload: TaskMutation, *, create: bool = False):
        action_id = payload.actionId or payload.operationId
        if not action_id:
            raise HTTPException(422, "Task mutation requires actionId")
        digest = _task_action_digest(action_id, payload)
        replay = await _find_task_action(request, scope, action_id, digest)
        if replay is not None:
            return replay
        stored = await _resolve_task_document(request, scope, payload.resourceKey, task_id=payload.taskId)
        current = _note_view(await _call(request, "get", {**scope, "id": _doc_id(str(stored["id"]))}))
        if current.get("readOnly") or current.get("builtin") or current.get("rawPreserved") or current.get("note_error"):
            raise HTTPException(403, detail={"outcome": "read_only", "message": "Task target is read-only"})
        expected = _expected_revision(payload.expectedRevision)
        rows = _task_source_rows(current)
        row = None
        anchor_block = str(payload.anchor.get("blockId") or "")
        if anchor_block:
            row = next((candidate for candidate in rows if candidate["blockId"] == anchor_block), None)
        expected_hash = str(payload.anchor.get("expectedTextHash") or "")
        if row is None and expected_hash:
            matches = [candidate for candidate in rows if candidate["expectedTextHash"] == expected_hash]
            if len(matches) > 1:
                raise HTTPException(409, detail={"outcome": "conflict", "message": "Task fingerprint is ambiguous"})
            row = matches[0] if matches else None
        if row is None:
            raise HTTPException(409, detail={"outcome": "target_missing", "message": "Task line was deleted or moved"})
        if expected_hash and row["expectedTextHash"] != expected_hash and row["done"] != payload.checked:
            raise HTTPException(409, detail={"outcome": "conflict", "message": "Task line changed"})
        lines = str(current.get("text") or "").split("\n")
        line_index = int(row["line"]) - 1
        source = lines[line_index] if 0 <= line_index < len(lines) else ""
        if not re.match(r"^\s*[-*+]\s+\[[ xX]\].*$", source):
            raise HTTPException(409, detail={"outcome": "conflict", "message": "Task line changed"})
        expected_source = str(payload.anchor.get("expectedText") or "")
        if expected and expected != str(current.get("head") or "") and expected_source and row["done"] == payload.checked:
            normalize_marker = lambda value: re.sub(r"\[[ xX]\]", "[]", value, count=1)
            if normalize_marker(source) == normalize_marker(expected_source):
                fresh_task = next((item for item in _task_projection(request, scope, current) if item["id"] == payload.taskId), None)
                if fresh_task is not None:
                    recovered = {"outcome": "applied", "actionId": action_id, "task": fresh_task, "revision": current.get("head"), "replayed": True}
                    recovered = await _record_task_action(request, scope, action_id, digest, recovered)
                    publish(scope, "task", recovered)
                    return recovered
        lines[line_index] = re.sub(r"(\[[ xX]\])", "[x]" if payload.checked else "[ ]", source, count=1)
        body = "\n".join(lines)
        if current.get("kind") in _NOTE_KINDS:
            stored_record = await _call(request, "get", {**scope, "id": _doc_id(str(stored["id"]))})
            if stored_record.get("format") == "copal-note-v1":
                previous = {"body": {"type": "doc", "blocks": stored_record.get("blocks") or []}, "properties": stored_record.get("propertyDefinitions") or [], "relations": stored_record.get("relations") or [], "extensions": stored_record.get("extensions") or {}}
            else:
                try:
                    previous = json.loads(str(stored_record.get("text") or "{}"))
                except json.JSONDecodeError:
                    previous = None
            content = _encode_note(body, current.get("properties"), current.get("relations"), previous if isinstance(previous, dict) else None)
            corpus = "wiki" if current.get("kind") == _WIKI_KIND else "notes"
        else:
            content, corpus = body, "notes"
        try:
            result = await _call(request, "commit_guarded", {
                **scope, "action_id": action_id, "actor_id": _actor_account_id(request) or scope["owner"], "guards": [],
                "operations": [{"kind": "write", "owner": scope["owner"], "workspace_id": scope["workspace_id"], "id": _doc_id(str(stored["id"])), "revision": {"kind": "copalHead", "value": str(expected or current.get("head") or "")}, "content": content}],
            })
        except HTTPException as exc:
            if exc.status_code != 400:
                raise
            result = {"outcome": "unsupported"}
        guarded = result.get("outcome") in {"applied", "unchanged", "conflict", "idempotency_conflict"}
        if result.get("outcome") in {"unsupported", "created", None}:
            result = await _call(request, "write", {**scope, "id": _doc_id(str(stored["id"])), "content": content, "base": expected or current.get("head"), "corpus": corpus})
        if result.get("outcome") == "idempotency_conflict":
            raise HTTPException(409, detail={"outcome": "idempotency_conflict", "message": "Action id was already used for a different task mutation"})
        if result.get("outcome") == "stale":
            raise HTTPException(409, detail={"outcome": "stale", "doc": result.get("doc")})
        if result.get("outcome") == "conflict":
            raise HTTPException(409, detail={"outcome": "stale", "expectedRevision": expected, "actualRevision": result.get("actual") or result.get("revision")})
        fresh_raw = await _call(request, "get", {**scope, "id": _doc_id(str(stored["id"]))})
        fresh = _note_view(fresh_raw)
        task_projection = await _task_projection_receipt(request, scope)
        task = next((item for item in _task_projection(request, scope, fresh_raw) if item["id"] == payload.taskId), None)
        if task is None:
            raise HTTPException(409, detail={"outcome": "conflict", "message": "Task anchor no longer resolves to the submitted block"})
        response = {"outcome": "applied", "actionId": action_id, "task": task, "revision": fresh.get("head"), "replayed": False, "projections": {"tasks": task_projection}}
        if not guarded:
            response = await _record_task_action(request, scope, action_id, digest, response)
        publish(scope, "task", response)
        return response

    @router.post("/tasks/{task_id}/toggle")
    @router.patch("/tasks/{task_id}")
    async def mutate_task(task_id: str, payload: TaskMutation, request: Request, workspace: str | None = None):
        if payload.taskId != task_id:
            raise HTTPException(400, "Task id does not match request path")
        return await _write_task(request, _scope(request, workspace), payload)

    @router.post("/tasks")
    @router.post("/tasks/create")
    async def create_task(payload: TaskCreate, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        action_id = payload.actionId or payload.operationId
        if not action_id:
            raise HTTPException(422, "Task creation requires actionId")
        digest = _task_action_digest(action_id, payload)
        replay = await _find_task_action(request, scope, action_id, digest)
        if replay is not None:
            return replay
        stored = await _resolve_task_document(request, scope, payload.resourceKey)
        current = _note_view(await _call(request, "get", {**scope, "id": _doc_id(str(stored["id"]))}))
        if current.get("readOnly") or current.get("builtin") or current.get("rawPreserved") or current.get("note_error"):
            raise HTTPException(403, detail={"outcome": "read_only", "message": "Task target is read-only"})
        expected = _expected_revision(payload.expectedRevision)
        body = str(current.get("text") or "")
        body = f"{body}{'' if not body or body.endswith(chr(10)) else chr(10)}- [ ] {payload.text.strip()}"
        if current.get("kind") in _NOTE_KINDS:
            raw = await _call(request, "get", {**scope, "id": _doc_id(str(stored["id"]))})
            previous = {"body": {"type": "doc", "blocks": raw.get("blocks") or []}, "properties": raw.get("propertyDefinitions") or [], "relations": raw.get("relations") or [], "extensions": raw.get("extensions") or {}} if raw.get("format") == "copal-note-v1" else None
            content = _encode_note(body, current.get("properties"), current.get("relations"), previous)
            corpus = "wiki" if current.get("kind") == _WIKI_KIND else "notes"
        else:
            content, corpus = body, "notes"
        try:
            result = await _call(request, "commit_guarded", {
                **scope, "action_id": action_id, "actor_id": _actor_account_id(request) or scope["owner"], "guards": [],
                "operations": [{"kind": "write", "owner": scope["owner"], "workspace_id": scope["workspace_id"], "id": _doc_id(str(stored["id"])), "revision": {"kind": "copalHead", "value": str(expected or current.get("head") or "")}, "content": content}],
            })
        except HTTPException as exc:
            if exc.status_code != 400:
                raise
            result = {"outcome": "unsupported"}
        guarded = result.get("outcome") in {"applied", "unchanged", "conflict", "idempotency_conflict"}
        if result.get("outcome") in {"unsupported", "created", None}:
            result = await _call(request, "write", {**scope, "id": _doc_id(str(stored["id"])), "content": content, "base": expected or current.get("head"), "corpus": corpus})
        if result.get("outcome") == "idempotency_conflict":
            raise HTTPException(409, detail={"outcome": "idempotency_conflict", "message": "Action id was already used for a different task creation"})
        if result.get("outcome") == "stale":
            raise HTTPException(409, detail={"outcome": "stale", "doc": result.get("doc")})
        if result.get("outcome") == "conflict":
            raise HTTPException(409, detail={"outcome": "stale", "expectedRevision": expected, "actualRevision": result.get("actual") or result.get("revision")})
        fresh_raw = await _call(request, "get", {**scope, "id": _doc_id(str(stored["id"]))})
        fresh = _note_view(fresh_raw)
        task_projection = await _task_projection_receipt(request, scope)
        task = _task_projection(request, scope, fresh_raw)[-1]
        response = {"outcome": "applied", "actionId": action_id, "task": task, "revision": fresh.get("head"), "replayed": False, "projections": {"tasks": task_projection}}
        if not guarded:
            response = await _record_task_action(request, scope, action_id, digest, response)
        publish(scope, "task", response)
        return response

    @router.post("/planning/migrate")
    async def migrate_planning(
        payload: PlanningMigration,
        request: Request,
        workspace: str | None = None,
        dry_run: bool = Query(True),
    ):
        scope = _scope(request, workspace)
        docs = await _indexed_documents(request, scope)
        registry_doc, marker_doc, canonical_events = canonical_documents(docs)
        legacy = next((doc for doc in docs if doc.get("kind") == "planning"), None)
        try:
            if registry_doc:
                track_registry_from_document(registry_doc)
        except PlanningValidationError as exc:
            raise HTTPException(422, str(exc)) from exc

        if payload.action == "rollback":
            if dry_run:
                raise HTTPException(400, "Rollback requires dry_run=false")
            if not marker_doc:
                return {"ok": True, "action": "rollback", "changed": False, "reason": "No migration marker exists"}
            try:
                marker = json.loads(str(marker_doc.get("text") or "{}"))
            except json.JSONDecodeError as exc:
                raise HTTPException(409, "Migration marker is invalid; refusing unsafe rollback") from exc
            current = {doc.get("id"): doc for doc in docs}
            conflicts = []
            for created in marker.get("created") or []:
                doc = current.get(created.get("id"))
                if doc and doc.get("head") != created.get("head"):
                    conflicts.append({"id": doc.get("id"), "name": doc.get("name"), "expected": created.get("head"), "actual": doc.get("head")})
            if conflicts:
                raise HTTPException(409, detail={"message": "Canonical records changed after migration", "conflicts": conflicts})
            removed = []
            created_ids = {item.get("id") for item in marker.get("created") or []}
            for doc in docs:
                if doc.get("kind") == "calendar-projection" and any(
                    str(doc.get("name") or "").endswith(f"-{document_id}.json")
                    for document_id in created_ids
                ):
                    await _call(request, "delete", {**scope, "id": _doc_id(str(doc["id"]))})
                    removed.append(doc["id"])
            for created in reversed(marker.get("created") or []):
                document_id = created.get("id")
                if document_id in current:
                    await _call(request, "delete", {**scope, "id": _doc_id(str(document_id))})
                    removed.append(document_id)
            await _call(request, "delete", {**scope, "id": _doc_id(str(marker_doc["id"]))})
            projection = await _project_planning_document(request, scope, legacy) if legacy else None
            result = {"ok": True, "action": "rollback", "changed": True, "removed": removed, "calendar_projection": projection}
            publish(scope, "document", result)
            return result

        if not legacy:
            return {"ok": True, "dryRun": dry_run, "changed": False, "reason": "No legacy planning document exists"}
        try:
            inventory = legacy_inventory(legacy)
        except PlanningValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        report = _migration_report(legacy, inventory)
        report["eventNames"] = [event_document_name(event) for event in inventory["events"]]
        if dry_run:
            return {"ok": True, "dryRun": True, "changed": not bool(marker_doc), "report": report}

        marker: dict[str, Any]
        if marker_doc:
            try:
                marker = json.loads(str(marker_doc.get("text") or "{}"))
            except json.JSONDecodeError as exc:
                raise HTTPException(409, "Migration marker is invalid") from exc
            if marker.get("state") == "complete":
                return {"ok": True, "dryRun": False, "changed": False, "report": marker.get("report") or report, "marker": marker}
        else:
            marker = {
                "schemaVersion": 1,
                "state": "applying",
                "legacyDocument": {"id": legacy.get("id"), "head": legacy.get("head")},
                "preexistingIds": [event["id"] for event in canonical_events] + ([registry_doc["id"]] if registry_doc else []),
                "created": [],
                "mappings": {},
                "report": report,
            }
            created_marker = await _call(
                request,
                "create",
                {**scope, "name": MIGRATION_NAME, "kind": MIGRATION_KIND, "content": json.dumps(marker, sort_keys=True, separators=(",", ":"))},
            )
            marker_id = created_marker.get("doc", {}).get("id")
            if not marker_id:
                raise HTTPException(500, "Copal bridge did not return a migration marker id")
            marker_doc = await _call(request, "get", {**scope, "id": marker_id})

        preexisting = set(marker.get("preexistingIds") or [])
        created_by_id = {item.get("id"): item for item in marker.get("created") or []}
        mapping = dict(marker.get("mappings") or {})

        if not registry_doc:
            registry_content = serialize_track_registry(
                inventory["tracks"],
                {**inventory["metadata"], "legacyPlanningDocumentId": legacy.get("id")},
            )
            created_registry = await _call(
                request,
                "create",
                {**scope, "name": TRACKS_NAME, "kind": TRACKS_KIND, "content": registry_content},
            )
            registry_id = created_registry.get("doc", {}).get("id")
            if not registry_id:
                raise HTTPException(500, "Copal bridge did not return a track registry id")
            registry_doc = await _call(request, "get", {**scope, "id": registry_id})
        if registry_doc["id"] not in preexisting and registry_doc["id"] not in created_by_id:
            created_by_id[registry_doc["id"]] = {
                "id": registry_doc["id"], "head": registry_doc.get("head"), "kind": TRACKS_KIND, "name": registry_doc.get("name"),
            }

        current_docs = await _indexed_documents(request, scope)
        _, _, current_events = canonical_documents(current_docs)
        by_legacy = {str(event.get("legacyId")): event for event in current_events if event.get("legacyId")}
        tracks = track_registry_from_document(registry_doc).get("tracks") or []
        for event in inventory["events"]:
            legacy_id = str(event["legacyId"])
            existing = by_legacy.get(legacy_id)
            if existing:
                event_doc = next(doc for doc in current_docs if doc.get("id") == existing["id"])
            else:
                created_event = await _call(
                    request,
                    "create",
                    {
                        **scope,
                        "name": event_document_name(event),
                        "kind": EVENT_KIND,
                        "content": serialize_event(event, tracks=tracks),
                    },
                )
                event_id = created_event.get("doc", {}).get("id")
                if not event_id:
                    raise HTTPException(500, f"Copal bridge did not return an id for {legacy_id}")
                event_doc = await _call(request, "get", {**scope, "id": event_id})
                current_docs.append(event_doc)
                by_legacy[legacy_id] = event_from_document(event_doc) or {"id": event_id}
            mapping[legacy_id] = event_doc["id"]
            if event_doc["id"] not in preexisting and event_doc["id"] not in created_by_id:
                created_by_id[event_doc["id"]] = {
                    "id": event_doc["id"], "head": event_doc.get("head"), "kind": EVENT_KIND, "name": event_doc.get("name"),
                }

        marker.update({"state": "complete", "created": list(created_by_id.values()), "mappings": mapping, "report": report})
        marker_write = await _call(
            request,
            "write",
            {**scope, "id": marker_doc["id"], "content": json.dumps(marker, sort_keys=True, separators=(",", ":")), "base": marker_doc.get("head")},
        )
        if marker_write.get("outcome") == "stale":
            raise HTTPException(409, "Migration marker changed concurrently; rerun to resume")
        projection = await _project_canonical_workspace(request, scope)
        result = {"ok": True, "dryRun": False, "changed": True, "report": report, "marker": marker, "calendar_projection": projection}
        publish(scope, "document", result)
        return result

    @router.post("/planning/events")
    async def create_event(payload: CreateEvent, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        docs = await _indexed_documents(request, scope)
        registry, _, _ = canonical_documents(docs)
        try:
            tracks = track_registry_from_document(registry).get("tracks") or []
            event = validate_event(payload.event, tracks)
            result = await _call(
                request,
                "create",
                {**scope, "name": event_document_name(event), "kind": EVENT_KIND, "content": serialize_event(event, tracks=tracks)},
            )
        except PlanningValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        fresh = await _call(request, "get", {**scope, "id": result["doc"]["id"]})
        projection = await _project_canonical_workspace(request, scope)
        response = {**result, "doc": fresh, "event": event_from_document(fresh), "calendar_projection": projection}
        publish(scope, "document", response)
        return response

    @router.patch("/planning/events/{document_id}")
    async def patch_event(document_id: str, payload: EventMutation, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        docs = await _indexed_documents(request, scope)
        registry, _, _ = canonical_documents(docs)
        source = next((doc for doc in docs if doc.get("id") == _doc_id(document_id)), None)
        event = event_from_document(source or {})
        if not source or not event:
            raise HTTPException(404, "Canonical Copal event not found")
        try:
            tracks = track_registry_from_document(registry).get("tracks") or []
            patch = dict(payload.patch)
            # Timeline attachments are prepared by the Files facade and
            # committed here with the event's existing CAS head. The planning
            # schema predates this adapter, so retain the typed records in
            # Copal's explicit extension envelope instead of treating them as
            # arbitrary frontmatter or a Files mutation.
            attachment_extras = None
            if "attachments" in patch:
                attachments = patch.pop("attachments")
                if not isinstance(attachments, list) or len(attachments) > 64 or any(not isinstance(item, dict) for item in attachments):
                    raise PlanningValidationError("attachments must be a bounded list")
                extras = dict(event.get("copal_extra") or {})
                extras["attachments"] = attachments
                attachment_extras = extras
            merged = merge_event(event, patch, tracks)
            if attachment_extras is not None:
                merged["copal_extra"] = attachment_extras
            result = await _call(
                request,
                "write",
                {**scope, "id": document_id, "content": serialize_event(merged, tracks=tracks), "base": payload.base or source.get("head")},
            )
        except PlanningValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        if result.get("outcome") == "stale":
            raise HTTPException(409, detail={"outcome": "stale", "doc": result.get("doc")})
        fresh = await _call(request, "get", {**scope, "id": document_id})
        projection = await _project_canonical_workspace(request, scope)
        response = {**result, "doc": fresh, "event": event_from_document(fresh), "calendar_projection": projection}
        publish(scope, "document", response)
        return response

    @router.delete("/planning/events/{document_id}")
    async def delete_event(document_id: str, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        docs = await _indexed_documents(request, scope)
        registry, _, _ = canonical_documents(docs)
        source = next((doc for doc in docs if doc.get("id") == _doc_id(document_id)), None)
        if not source or not event_from_document(source):
            raise HTTPException(400, "Document is not a canonical Copal event")
        try:
            track_registry_from_document(registry)
        except PlanningValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        result = await _call(request, "delete", {**scope, "id": document_id})
        result["calendar_projection"] = await _project_canonical_workspace(request, scope)
        publish(scope, "deleted", {"id": document_id})
        return result

    @router.put("/planning/tracks")
    async def put_tracks(payload: TrackMutation, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        docs = await _indexed_documents(request, scope)
        registry, _, _ = canonical_documents(docs)
        try:
            tracks = track_preorder(payload.tracks)
            previous = track_registry_from_document(registry)
            metadata = {key: value for key, value in previous.items() if key not in {"schemaVersion", "tracks"}}
            metadata.update({key: value for key, value in payload.metadata.items() if key not in {"schemaVersion", "tracks"}})
            content = serialize_track_registry(tracks, metadata)
        except PlanningValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        if registry:
            result = await _call(
                request,
                "write",
                {**scope, "id": registry["id"], "content": content, "base": payload.base or registry.get("head")},
            )
            if result.get("outcome") == "stale":
                raise HTTPException(409, detail={"outcome": "stale", "doc": result.get("doc")})
            registry_id = registry["id"]
        else:
            result = await _call(request, "create", {**scope, "name": TRACKS_NAME, "kind": TRACKS_KIND, "content": content})
            registry_id = result["doc"]["id"]
        fresh = await _call(request, "get", {**scope, "id": registry_id})
        projection = await _project_canonical_workspace(request, scope)
        response = {**result, "doc": fresh, "tracks": tracks, "calendar_projection": projection}
        publish(scope, "document", response)
        return response

    @router.get("/trash")
    async def list_trash(request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        notes_trash = await _call(request, "trash", {**scope, "corpus": "notes"})
        wiki_trash = await _call(request, "trash", {**scope, "corpus": "wiki"})
        all_docs = (notes_trash.get("docs") or []) + (wiki_trash.get("docs") or [])
        return {"docs": all_docs}

    @router.post("/trash/{document_id}/restore")
    async def restore_deleted_document(document_id: str, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        normalized_id = _doc_id(document_id)
        trashed: dict[str, Any] | None = None
        trash_corpus: str | None = None
        for corpus in ("notes", "wiki"):
            trash = await _call(request, "trash", {**scope, "corpus": corpus})
            candidate = next(
                (document for document in trash.get("docs") or [] if document.get("id") == normalized_id),
                None,
            )
            if candidate:
                trashed = candidate
                trash_corpus = corpus
                break
        if not trashed or not trash_corpus:
            raise HTTPException(404, "Deleted Copal document not found")
        _require_nonplanning_document_mutation(trashed)
        result = await _call(
            request,
            "restore_deleted",
            {**scope, "id": normalized_id, "corpus": trash_corpus},
        )
        restored = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        projection = (
            await _project_canonical_workspace(request, scope)
            if event_from_document(restored) or restored.get("kind") == TRACKS_KIND
            else await _project_planning_document(request, scope, restored)
        )
        if projection is not None:
            result["calendar_projection"] = projection
        result = _note_result(result)
        _with_task_projection(result, await _task_projection_receipt(request, scope))
        publish(scope, "document", result)
        return result

    @router.get("/documents/{document_id}")
    async def get_document(document_id: str, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        document = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        return _resource_view(request, scope, document)

    @router.get("/documents/{document_id}/download")
    async def download_document(document_id: str, request: Request, workspace: str | None = None):
        """Download one logical Copal resource without exposing vault paths."""
        document = _note_view(await _call(request, "get", {**_scope(request, workspace), "id": _doc_id(document_id)}))
        kind = str(document.get("kind") or "note")
        preserved_bytes = _preserved_source_bytes(document)
        # A native imported record may remain fully editable while retaining
        # an exact exporter source.  When that provenance exists, download it
        # byte-for-byte; fresh native records without provenance continue to
        # use the normal Markdown projection.
        recovery_download = preserved_bytes is not None
        if kind in _NOTE_KINDS:
            payload_bytes = preserved_bytes if recovery_download else _note_markdown(document).encode("utf-8")
            extension = ".source" if recovery_download else ".md"
        else:
            payload_bytes = str(document.get("text") or "").encode("utf-8")
            extension = ".json"
        raw_name = str(document.get("name") or document.get("title") or document_id).strip()
        safe_name = "".join(char for char in raw_name if char not in {"\r", "\n", "/", "\\", '"'})[:200].strip() or str(document_id)
        if "." not in safe_name.rsplit(" ", 1)[-1]:
            safe_name += extension
        encoded = quote(safe_name, safe="")

        async def chunks():
            for offset in range(0, len(payload_bytes), 1024 * 1024):
                yield payload_bytes[offset:offset + 1024 * 1024]

        return StreamingResponse(
            chunks(),
            media_type=("application/octet-stream" if recovery_download else
                        ("text/markdown; charset=utf-8" if extension == ".md" else "application/json; charset=utf-8")),
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name.encode("ascii", "ignore").decode("ascii") or "copal-resource"}"; filename*=UTF-8\'\'{encoded}',
                "Content-Length": str(len(payload_bytes)),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )

    @router.get("/assets/{document_id}")
    async def get_asset(document_id: str, request: Request, workspace: str | None = None):
        path, name = await _asset_file(request, _scope(request, workspace), document_id)
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})

    @router.get("/attachments/usage")
    async def attachment_usage(request: Request, name: str = Query(min_length=1, max_length=512), workspace: str | None = None):
        scope = _scope(request, workspace)
        target = name.strip()
        documents = await _indexed_documents(request, scope)
        usages = []
        for document in documents:
            if document.get("kind") in {"asset", *_CONTROL_DOCUMENT_KINDS}:
                continue
            text = str(_note_view(document).get("text") or "")
            if re.search(rf"!\[\[{re.escape(target)}(?:\|[^\]\n]*)?\]\]", text):
                usages.append({"id": document.get("id"), "name": document.get("name"), "head": document.get("head")})
        return {"name": target, "count": len(usages), "usages": usages}

    @router.post("/attachments")
    async def mutate_attachment(payload: AttachmentMutation, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        await reap_attachment_markers(request, scope)
        document_id = _doc_id(payload.documentId)
        existing_raw = await _call(request, "get", {**scope, "id": document_id})
        existing = _note_view(existing_raw)
        _require_nonplanning_document_mutation(existing)
        _require_mutable_note(existing)
        if existing.get("readOnly") or existing.get("builtin"):
            raise HTTPException(403, detail={"outcome": "read_only", "message": "Attachment target is read-only"})
        try:
            encoded = str(payload.contentBase64).strip()
            raw = __import__("base64").b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, "Attachment bytes are not valid base64") from exc
        if not raw or len(raw) > _COPAL_IMPORT_MAX_MEMBER_BYTES:
            raise HTTPException(413, "Attachment exceeds the allowed size")
        name = _name(payload.name)
        suffix = Path(name).suffix.lower().lstrip(".") or "bin"
        source_hash = str(payload.sourceTextHash or hashlib.sha256(str(existing.get("text") or "").encode("utf-8")).hexdigest()).strip()
        repository = lifecycle_repository(request)
        generation_getter = getattr(repository, "generation", None)
        generation = int(generation_getter()) if callable(generation_getter) else 0
        # Every compatibility request gets a durable marker before the asset
        # write.  This keeps the old single-request endpoint recoverable when
        # asset creation or the later document transaction loses its reply.
        marker = {
            "action_id": payload.actionId, "phase": "pending", "owner": scope["owner"],
            "workspace_id": scope["workspace_id"], "document_id": document_id,
            "base": str(payload.base or existing.get("head") or ""),
            "source_text_hash": source_hash, "asset_name": name, "asset_size": len(raw),
            "asset_digest": hashlib.sha256(raw).hexdigest(),
            "mime": str(payload.mime or "application/octet-stream"),
            "created_unix_ms": int(time.time() * 1000), "generation": generation,
            **({"intended_content_hash": hashlib.sha256(str(payload.content).encode("utf-8")).hexdigest()} if payload.content is not None else {}),
        }
        prior_marker = lifecycle_load(request, scope, payload.actionId)
        if prior_marker:
            immutable_pairs = (
                ("document_id", document_id), ("base", marker["base"]),
                ("source_text_hash", source_hash), ("asset_name", name),
                ("asset_size", len(raw)), ("asset_digest", marker["asset_digest"]),
                ("mime", marker["mime"]),
            )
            if any(prior_marker.get(key) != expected for key, expected in immutable_pairs):
                raise HTTPException(409, detail={"outcome": "idempotency_conflict", "message": "Attachment operation was already used for a different request"})
            if prior_marker.get("phase") in {"aborted", "reaped"}:
                raise HTTPException(409, detail={"outcome": "stale", "message": "Attachment operation is no longer active"})
            if prior_marker.get("phase") == "consumed":
                current = await _call(request, "get", {**scope, "id": document_id})
                asset = await attachment_asset(request, scope, prior_marker)
                current_view = _note_view(current)
                if asset is None or not re.search(rf"!\[\[{re.escape(name)}(?:\|[^\]\n]*)?\]\]", str(current_view.get("text") or "")):
                    raise HTTPException(409, detail={"outcome": "resource_changed", "message": "Attachment operation can no longer be replayed"})
                return {"outcome": "unchanged", "actionId": payload.actionId, "asset": _resource_view(request, scope, asset), "doc": _resource_view(request, scope, current), "receipt": prior_marker.get("receipt") or {"outcome": "unchanged", "actionId": payload.actionId}}
            if prior_marker.get("phase") == "staged" and payload.content is not None:
                asset = await attachment_asset(request, scope, prior_marker)
                if asset is not None and await attachment_document_matches(request, scope, prior_marker, name):
                    receipt = prior_marker.get("receipt") or {"outcome": "recovered", "actionId": payload.actionId, "assetId": asset.get("id"), "documentId": document_id}
                    recovered_marker = {**prior_marker, "phase": "consumed", "receipt": receipt, "committed_content_hash": hashlib.sha256(str(payload.content).encode("utf-8")).hexdigest(), "consumed_at": time.time()}
                    lifecycle_save(request, scope, payload.actionId, recovered_marker)
                    current = await _call(request, "get", {**scope, "id": document_id})
                    return {"outcome": "unchanged", "actionId": payload.actionId, "asset": _resource_view(request, scope, asset), "doc": _resource_view(request, scope, current), "receipt": receipt}
        lifecycle_save(request, scope, payload.actionId, marker)
        asset_result = await _call(request, "put_asset_scoped", {
            **scope,
            "name": name,
            "ext": suffix,
            "base64": __import__("base64").b64encode(raw).decode("ascii"),
        }, timeout=60)
        asset_raw = asset_result.get("doc") if isinstance(asset_result, dict) else None
        if not isinstance(asset_raw, dict) or not asset_raw.get("id"):
            raise HTTPException(502, detail={"outcome": "attachment_failed", "message": "Attachment object was not created"})
        if str(asset_raw.get("kind") or "") != "asset" or str(asset_raw.get("name") or name) != name or int(asset_raw.get("size") or -1) != len(raw):
            raise HTTPException(409, detail={"outcome": "resource_changed", "message": "Prepared attachment asset identity changed"})
        asset_head = str(asset_raw.get("head") or "")
        if asset_head and (not asset_head.startswith("sha256:") or asset_head.split(":", 2)[1].lower() != marker["asset_digest"]):
            raise HTTPException(409, detail={"outcome": "resource_changed", "message": "Prepared attachment asset bytes changed"})
        asset = _resource_view(request, scope, asset_raw)
        marker = {
            **marker, "asset_id": str(asset_raw["id"]), "asset_name": str(asset_raw.get("name") or name),
            "phase": "staged",
        }
        lifecycle_save(request, scope, payload.actionId, marker)
        if payload.prepareOnly:
            return {"outcome": "prepared", "actionId": payload.actionId, "asset": asset, "preparation": marker}
        if payload.content is None:
            receipt = {"outcome": "asset_applied", "actionId": payload.actionId, "assetId": asset.get("id"), "documentId": document_id}
            lifecycle_save(request, scope, payload.actionId, {**marker, "phase": "consumed", "receipt": receipt, "committed_content_hash": hashlib.sha256(str(existing.get("text") or "").encode("utf-8")).hexdigest(), "consumed_at": time.time()})
            return {"outcome": "asset_applied", "actionId": payload.actionId, "asset": asset, "receipt": receipt}

        content = str(payload.content)
        if existing.get("kind") in _NOTE_KINDS:
            if existing_raw.get("format") == "copal-note-v1":
                previous = {"body": {"type": "doc", "blocks": existing_raw.get("blocks") or []}, "properties": existing_raw.get("propertyDefinitions") or [], "relations": existing_raw.get("relations") or [], "extensions": existing_raw.get("extensions") or {}}
            else:
                try:
                    previous = json.loads(str(existing_raw.get("text") or "{}"))
                except json.JSONDecodeError:
                    previous = None
            stored_content = _encode_note(content, existing.get("properties"), existing.get("relations"), previous if isinstance(previous, dict) else None)
        else:
            stored_content = content
        expected = payload.base or existing.get("head")
        if not expected:
            raise HTTPException(409, detail={"outcome": "stale", "message": "Attachment target has no current revision", "asset": asset})
        guarded = await _call(request, "commit_guarded", {
            **scope,
            "action_id": payload.actionId,
            "actor_id": _actor_account_id(request) or scope["owner"],
            "guards": [],
            "operations": [{"kind": "write", "owner": scope["owner"], "workspace_id": scope["workspace_id"], "id": document_id, "revision": {"kind": "copalHead", "value": str(expected)}, "content": stored_content}],
        }, timeout=60)
        if guarded.get("outcome") in {"conflict", "idempotency_conflict"}:
            raise HTTPException(409, detail={**guarded, "asset": asset})
        if guarded.get("outcome") not in {"applied", "unchanged"}:
            raise HTTPException(502, detail={**guarded, "asset": asset})
        fresh_raw = await _call(request, "get", {**scope, "id": document_id})
        fresh = _resource_view(request, scope, fresh_raw)
        receipt = {**guarded, "operationId": payload.actionId, "assetId": asset.get("id"), "documentId": document_id}
        lifecycle_save(request, scope, payload.actionId, {**marker, "phase": "consumed", "receipt": receipt, "committed_content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(), "consumed_at": time.time()})
        task_projection = await _task_projection_receipt(request, scope)
        response = {"outcome": guarded.get("outcome"), "actionId": payload.actionId, "asset": asset, "doc": fresh, "receipt": receipt}
        _with_task_projection(response, task_projection)
        publish(scope, "document", receipt)
        return response

    @router.get("/attachments/{action_id}/status")
    async def attachment_lifecycle_status(action_id: str, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        await reap_attachment_markers(request, scope)
        marker = lifecycle_load(request, scope, action_id)
        if not marker or marker.get("owner") != scope["owner"] or marker.get("workspace_id") != scope["workspace_id"]:
            raise HTTPException(404, detail={"outcome": "resource_unavailable", "message": "Attachment operation is unavailable"})
        if marker.get("phase") in {"pending", "staged"}:
            asset = await attachment_asset(request, scope, marker)
            if asset is not None:
                marker = {**marker, "asset_id": str(asset["id"]), "asset_name": str(asset.get("name") or marker.get("asset_name")), "phase": "staged"}
                lifecycle_save(request, scope, action_id, marker)
        # The binding fields are needed to resume a lost legacy prepare
        # response.  They are hashes/opaque IDs only; the immutable owner and
        # workspace checks above ensure they cannot disclose another tenant's
        # document or source content.
        return {key: value for key, value in marker.items() if key not in {"owner", "workspace_id"}}

    @router.post("/attachments/commit")
    async def commit_attachment(payload: AttachmentCommit, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        await reap_attachment_markers(request, scope)
        marker = lifecycle_load(request, scope, payload.actionId)
        if not marker or marker.get("owner") != scope["owner"] or marker.get("workspace_id") != scope["workspace_id"]:
            raise HTTPException(404, detail={"outcome": "resource_unavailable", "message": "Attachment operation is unavailable"})
        if any((marker.get(marker_key) != payload_value) for marker_key, payload_value in (
            ("document_id", payload.documentId), ("base", payload.base),
            ("source_text_hash", payload.sourceTextHash), ("asset_id", payload.assetId), ("asset_name", payload.assetName),
        )):
            raise HTTPException(409, detail={"outcome": "idempotency_conflict", "message": "Attachment operation was already used for a different request"})
        if marker.get("phase") in {"aborted", "reaped"}:
            raise HTTPException(409, detail={"outcome": "stale", "message": "Attachment preparation is no longer active"})
        if marker.get("phase") not in {"pending", "staged", "consumed"} or marker.get("document_id") != payload.documentId or marker.get("base") != payload.base or marker.get("asset_name") != payload.assetName:
            raise HTTPException(409, detail={"outcome": "stale", "message": "Attachment preparation identity changed"})
        # Resolve and verify the exact asset for every commit, including an
        # idempotent replay.  This prevents a consumed receipt from becoming
        # an authorization oracle after the asset is deleted or replaced.
        asset = await attachment_asset(request, scope, marker)
        if asset is None or str(asset.get("id") or "") != payload.assetId:
            raise HTTPException(409, detail={"outcome": "resource_unavailable", "message": "Prepared attachment asset is unavailable"})
        if marker.get("phase") == "consumed":
            current = await _call(request, "get", {**scope, "id": _doc_id(payload.documentId)})
            current_view = _note_view(current)
            if not re.search(rf"!\[\[{re.escape(payload.assetName)}(?:\|[^\]\n]*)?\]\]", str(current_view.get("text") or "")):
                raise HTTPException(409, detail={"outcome": "resource_changed", "message": "Attachment target no longer references the prepared asset"})
            committed_hash = str(marker.get("committed_content_hash") or "")
            if committed_hash and hashlib.sha256(str(current_view.get("text") or "").encode("utf-8")).hexdigest() != committed_hash:
                raise HTTPException(409, detail={"outcome": "resource_changed", "message": "Attachment target changed after commit"})
            return {"outcome": "unchanged", "actionId": payload.actionId, "assetId": marker.get("asset_id"), "doc": _resource_view(request, scope, current), "receipt": marker.get("receipt") or {"outcome": "unchanged", "actionId": payload.actionId}}
        existing_raw = await _call(request, "get", {**scope, "id": _doc_id(payload.documentId)})
        existing = _note_view(existing_raw)
        current_text = str(existing.get("text") or "")
        committed_hash = hashlib.sha256(payload.content.encode("utf-8")).hexdigest()
        # The document write and marker transition are separate durable
        # operations. If the process died after the write, recover by proving
        # the exact committed content and asset reference, then finalize the
        # marker instead of rejecting on the old base revision.
        if marker.get("phase") in {"pending", "staged"} and hashlib.sha256(current_text.encode("utf-8")).hexdigest() == committed_hash and re.search(rf"!\[\[{re.escape(payload.assetName)}(?:\|[^\]\n]*)?\]\]", current_text):
            receipt = marker.get("receipt") or {"outcome": "recovered", "actionId": payload.actionId, "assetId": payload.assetId, "documentId": payload.documentId}
            recovered_marker = {**marker, "asset_id": payload.assetId, "asset_name": payload.assetName, "phase": "consumed", "receipt": receipt, "committed_content_hash": committed_hash, "consumed_at": time.time()}
            lifecycle_save(request, scope, payload.actionId, recovered_marker)
            return {"outcome": "unchanged", "actionId": payload.actionId, "assetId": payload.assetId, "doc": _resource_view(request, scope, existing_raw), "receipt": receipt}
        if existing.get("head") != payload.base or hashlib.sha256(str(existing.get("text") or "").encode("utf-8")).hexdigest() != marker.get("source_text_hash"):
            raise HTTPException(409, detail={"outcome": "stale", "message": "Attachment target changed", "asset": {"id": payload.assetId, "name": payload.assetName}})
        if not re.search(rf"!\[\[{re.escape(payload.assetName)}(?:\|[^\]\n]*)?\]\]", payload.content):
            raise HTTPException(422, detail={"outcome": "invalid_attachment", "message": "Committed content does not reference the prepared asset"})
        if existing.get("kind") in _NOTE_KINDS:
            if existing_raw.get("format") == "copal-note-v1":
                previous = {"body": {"type": "doc", "blocks": existing_raw.get("blocks") or []}, "properties": existing_raw.get("propertyDefinitions") or [], "relations": existing_raw.get("relations") or [], "extensions": existing_raw.get("extensions") or {}}
            else:
                try:
                    previous = json.loads(str(existing_raw.get("text") or "{}"))
                except json.JSONDecodeError:
                    previous = None
            stored_content = _encode_note(payload.content, existing.get("properties"), existing.get("relations"), previous if isinstance(previous, dict) else None)
        else:
            stored_content = payload.content
        guarded = await _call(request, "commit_guarded", {
            **scope, "action_id": payload.actionId, "actor_id": _actor_account_id(request) or scope["owner"], "guards": [],
            "operations": [{"kind": "write", "owner": scope["owner"], "workspace_id": scope["workspace_id"], "id": payload.documentId, "revision": {"kind": "copalHead", "value": payload.base}, "content": stored_content}],
        }, timeout=60)
        if guarded.get("outcome") in {"conflict", "idempotency_conflict"}:
            raise HTTPException(409, detail={**guarded, "asset": {"id": payload.assetId, "name": payload.assetName}})
        if guarded.get("outcome") not in {"applied", "unchanged"}:
            raise HTTPException(502, detail=guarded)
        fresh = _resource_view(request, scope, await _call(request, "get", {**scope, "id": payload.documentId}))
        receipt = {**guarded, "operationId": payload.actionId, "assetId": payload.assetId, "documentId": payload.documentId}
        lifecycle_save(request, scope, payload.actionId, {**marker, "asset_id": payload.assetId, "asset_name": payload.assetName, "phase": "consumed", "receipt": receipt, "committed_content_hash": hashlib.sha256(payload.content.encode("utf-8")).hexdigest(), "consumed_at": time.time()})
        publish(scope, "document", receipt)
        return {"outcome": guarded.get("outcome"), "actionId": payload.actionId, "assetId": payload.assetId, "doc": fresh, "receipt": receipt}

    @router.delete("/attachments/{action_id}")
    async def abort_attachment(action_id: str, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        marker = lifecycle_load(request, scope, action_id)
        if not marker or marker.get("owner") != scope["owner"] or marker.get("workspace_id") != scope["workspace_id"]:
            raise HTTPException(404, detail={"outcome": "resource_unavailable", "message": "Attachment operation is unavailable"})
        if marker.get("phase") == "consumed":
            return {"outcome": "unchanged", "phase": "consumed"}
        if marker.get("phase") in {"reaped", "aborted"}:
            return {"outcome": "unchanged", "phase": str(marker.get("phase"))}
        asset_id = str(marker.get("asset_id") or "").strip()
        if not asset_id:
            asset = await attachment_asset(request, scope, marker)
            asset_id = str(asset.get("id") or "") if asset else ""
        delete_error: Exception | None = None
        if asset_id:
            try:
                await _call(request, "delete", {**scope, "id": _doc_id(asset_id), "action_id": f"abort-{action_id[:120]}"}, timeout=60)
            except Exception as exc:
                delete_error = exc
        if delete_error is not None:
            # Keep the marker recoverable so the bounded reaper can retry a
            # transient provider failure instead of hiding a live asset behind
            # an ``aborted`` terminal phase.
            raise HTTPException(503, detail={"outcome": "cleanup_pending", "message": "Attachment cleanup is pending; retry later"}) from delete_error
        lifecycle_save(request, scope, action_id, {**marker, "asset_id": asset_id or marker.get("asset_id"), "phase": "aborted", "aborted_at": time.time()})
        return {"outcome": "aborted", "phase": "aborted"}

    @router.post("/documents")
    async def create_document(payload: CreateDocument, request: Request, workspace: str | None = None):
        if not _KIND.fullmatch(payload.kind):
            raise HTTPException(400, "Invalid document kind")
        kind = _WIKI_KIND if payload.corpus == "wiki" and payload.kind == _NOTE_KIND else payload.kind
        _require_nonplanning_document_mutation({"kind": kind, "text": payload.content})
        if payload.corpus == "wiki" and kind != _WIKI_KIND:
            raise HTTPException(422, "The Wiki corpus accepts database Wiki records only")
        if kind not in _NOTE_KINDS and (payload.properties or payload.relations):
            raise HTTPException(422, "Typed properties and relations belong to database notes")
        scope = _scope(request, workspace)
        content = _encode_note(payload.content, payload.properties, payload.relations) if kind in _NOTE_KINDS else payload.content
        action_id = payload.actionId
        action_digest = None
        if action_id:
            action_material = {
                "actionId": action_id,
                "owner": scope["owner"],
                "workspaceId": scope["workspace_id"],
                "name": _name(payload.name),
                "kind": kind,
                "corpus": payload.corpus,
                # Hash the request semantics, rather than the encoded note.
                # The note codec allocates IDs when no prior record exists;
                # those IDs must not make an identical retry look different.
                "content": payload.content,
                "properties": payload.properties,
                "relations": payload.relations,
            }
            action_digest = hashlib.sha256(json.dumps(action_material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
            replay = await _find_document_action(request, scope, action_id, action_digest)
            if replay is not None:
                return replay
            # A response can be lost after the provider commits and before the
            # route can persist its receipt. Recover only one exact, scoped
            # document so a same-name or cross-workspace record is never an
            # action-id oracle.
            indexed = await _call(request, "index", {**scope, "kind": kind, "corpus": payload.corpus}, timeout=60)
            requested_relation_keys = {
                (str(relation.get("kind") or ""), str(relation.get("target") or ""), str(relation.get("fragment") or ""), str(relation.get("targetDocumentId") or ""), str(relation.get("targetBlockId") or ""))
                for relation in payload.relations
            }
            candidates = []
            for document in indexed.get("docs") or []:
                if document.get("name") != _name(payload.name):
                    continue
                view = _note_view(document)
                actual_relation_keys = {
                    (str(relation.get("kind") or ""), str(relation.get("target") or ""), str(relation.get("fragment") or ""), str(relation.get("targetDocumentId") or ""), str(relation.get("targetBlockId") or ""))
                    for relation in (view.get("relations") or [])
                }
                if (
                    view.get("kind") == kind
                    and view.get("corpus") == payload.corpus
                    and view.get("text") == payload.content
                    and (view.get("properties") or {}) == payload.properties
                    and actual_relation_keys == requested_relation_keys
                ):
                    candidates.append(view)
            if len(candidates) == 1:
                recovered = {"outcome": "created", "actionId": action_id, "doc": candidates[0], "replayed": True}
                recovered = _note_result(recovered)
                recovered = await _record_document_action(request, scope, action_id, action_digest, recovered)
                publish(scope, "document", recovered)
                return recovered
        result = await _call(
            request,
            "create",
            {**scope, "name": _name(payload.name), "kind": kind, "content": content, "corpus": payload.corpus, **({"action_id": payload.actionId} if payload.actionId else {})},
        )
        if result.get("doc", {}).get("id"):
            indexed = await _call(request, "get", {**scope, "id": result["doc"]["id"]})
            if kind == "planning":
                result["calendar_projection"] = await _project_planning_document(request, scope, indexed)
            elif event_from_document(indexed) or payload.kind == TRACKS_KIND:
                result["calendar_projection"] = await _project_canonical_workspace(request, scope)
        task_projection = await _task_projection_receipt(request, scope)
        result = _note_result(result)
        if action_id:
            result["actionId"] = action_id
            result["replayed"] = False
            result = await _record_document_action(request, scope, action_id, action_digest, result)
        _with_task_projection(result, task_projection)
        publish(scope, "document", result)
        return result

    @router.put("/documents/{document_id}")
    async def write_document(
        document_id: str,
        payload: WriteDocument,
        request: Request,
        workspace: str | None = None,
    ):
        scope = _scope(request, workspace)
        stored = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        existing = _note_view(stored)
        _require_nonplanning_document_mutation(existing)
        _require_mutable_note(existing)
        if existing.get("kind") == "planning" and await _planning_write_locked(request, scope):
            raise HTTPException(409, "Legacy planning JSON is read-only after canonical migration")
        if existing.get("kind") not in _NOTE_KINDS and (payload.properties is not None or payload.relations is not None):
            raise HTTPException(422, "Typed properties and relations belong to database notes")
        if stored.get("format") == "copal-note-v1":
            previous = {
                "body": {"type": "doc", "blocks": stored.get("blocks") or []},
                "properties": stored.get("propertyDefinitions") or [],
                "relations": stored.get("relations") or [],
                "extensions": stored.get("extensions") or {},
            }
        else:
            try:
                previous = json.loads(str(stored.get("text") or "{}")) if existing.get("kind") in _NOTE_KINDS else None
            except json.JSONDecodeError:
                previous = None
        content = (
            _encode_note(
                payload.content,
                payload.properties if payload.properties is not None else existing.get("properties"),
                payload.relations,
                previous if isinstance(previous, dict) else None,
            )
            if existing.get("kind") in _NOTE_KINDS
            else payload.content
        )
        write_corpus = "wiki" if existing.get("kind") == _WIKI_KIND else "notes"
        result = await _call(
            request,
            "write",
            {**scope, "id": _doc_id(document_id), "content": content, "base": payload.base, "corpus": write_corpus, **({"action_id": payload.actionId} if payload.actionId else {})},
        )
        if result.get("outcome") == "stale":
            authoritative = _resource_view(request, scope, result["doc"]) if isinstance(result.get("doc"), dict) else result.get("doc")
            raise HTTPException(409, detail={"outcome": "stale", "doc": authoritative})
        indexed = _note_view(await _call(request, "get", {**scope, "id": _doc_id(document_id)}))
        task_projection = await _task_projection_receipt(request, scope)
        projection = (
            await _project_canonical_workspace(request, scope)
            if event_from_document(existing) or event_from_document(indexed) or indexed.get("kind") == TRACKS_KIND
            else await _project_planning_document(request, scope, indexed)
        )
        if projection is not None:
            result["calendar_projection"] = projection
        if isinstance(result.get("doc"), dict):
            result["doc"] = _resource_view(request, scope, result["doc"])
        # The browser's shared ResourceBuffer acknowledges a write only from
        # a receipt carrying the exact immutable action identity it submitted.
        # The bridge result contains the committed document/head, but older
        # bridge versions do not echo this transport field themselves.
        if payload.actionId:
            result["actionId"] = payload.actionId
        _with_task_projection(result, task_projection)
        publish(scope, "document", result)
        return result

    @router.post("/documents/{document_id}/convert/preview")
    async def preview_document_conversion(
        document_id: str,
        request: Request,
        payload: ConvertDocument | None = None,
        workspace: str | None = None,
    ):
        """Return a byte/projection diff without changing the Wiki record."""
        scope = _scope(request, workspace)
        stored = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        current = _note_view(stored)
        if current.get("kind") != _WIKI_KIND or current.get("recoveryState") != "legacy-import":
            raise HTTPException(409, "Only a recognized legacy Markdown Wiki can be previewed")
        source_bytes = _preserved_source_bytes(current)
        source = str(current.get("text") or "")
        preview_limit = 4 * 1024 * 1024
        if source_bytes is not None and len(source_bytes) > preview_limit:
            raise HTTPException(413, "The Wiki conversion preview is limited to 4 MiB of source")
        if source_bytes is not None:
            try:
                source = source_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(409, "The preserved Wiki source is not valid UTF-8 Markdown") from exc
        if not source:
            raise HTTPException(409, "The preserved Wiki source is unavailable")
        if len(source.encode("utf-8")) > preview_limit:
            raise HTTPException(413, "The Wiki conversion preview is limited to 4 MiB of source")
        content, diagnostics = _import_markdown_record(source, identity=str(stored.get("id") or document_id))
        projected = _note_view({**stored, "text":content, "kind":_WIKI_KIND, "corpus":"wiki"})
        converted = str(projected.get("text") or "")
        diff = "".join(difflib.unified_diff(
            source.splitlines(keepends=True), converted.splitlines(keepends=True),
            fromfile="original source", tofile="native Wiki projection",
        ))
        max_preview_bytes = 512 * 1024
        diff_bytes = diff.encode("utf-8")
        diff_truncated = len(diff_bytes) > max_preview_bytes
        if diff_truncated:
            diff = diff_bytes[:max_preview_bytes].decode("utf-8", errors="ignore")
        converted_bytes = converted.encode("utf-8")
        after_limit = 64 * 1024
        after_truncated = len(converted_bytes) > after_limit
        return {
            "preview": True,
            "documentId": document_id,
            "sourceBytes": len(source_bytes if source_bytes is not None else source.encode("utf-8")),
            "sourceDigest": hashlib.sha256((source_bytes if source_bytes is not None else source.encode("utf-8"))).hexdigest(),
            "diff": diff,
            "diffTruncated": diff_truncated,
            "after": {
                "textBytes": len(converted_bytes),
                "textDigest": hashlib.sha256(converted_bytes).hexdigest(),
                "textExcerpt": converted_bytes[:after_limit].decode("utf-8", errors="ignore"),
                "textTruncated": after_truncated,
                "propertyCount": len(projected.get("properties") or {}),
                "relationCount": len(projected.get("relations") or []),
            },
            "diagnostics": list(diagnostics or [])[:100],
            "diagnosticsTruncated": len(diagnostics or []) > 100,
            "base": payload.base if payload else stored.get("head"),
        }

    @router.post("/documents/{document_id}/convert")
    async def convert_document(
        document_id: str,
        request: Request,
        payload: ConvertDocument | None = None,
        workspace: str | None = None,
    ):
        """Explicitly convert a preserved Markdown Wiki through normal CAS."""
        # This endpoint is deliberately separate from PUT: recovery is a user
        # choice and must never turn an unknown source into an empty document.
        scope = _scope(request, workspace)
        stored = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        current = _note_view(stored)
        if current.get("kind") != _WIKI_KIND or current.get("recoveryState") != "legacy-import":
            raise HTTPException(409, "Only a recognized legacy Markdown Wiki can be converted")
        source = str(current.get("text") or "")
        if not source:
            preserved = _preserved_source_bytes(current)
            source = preserved.decode("utf-8", errors="strict") if preserved else ""
        if not source:
            raise HTTPException(409, "The preserved Wiki source is unavailable")
        content, diagnostics = _import_markdown_record(source, identity=str(stored.get("id") or document_id))
        result = await _call(request, "write", {
            **scope,
            "id": _doc_id(document_id),
            "content": content,
            "base": (payload.base if payload else None) or stored.get("head"),
            "corpus": "wiki",
            **({"action_id": payload.actionId} if payload and payload.actionId else {}),
        })
        if result.get("outcome") == "stale":
            raise HTTPException(409, detail={"outcome": "stale", "doc": result.get("doc")})
        fresh = _note_view(await _call(request, "get", {**scope, "id": _doc_id(document_id)}))
        result = {**result, "doc": _resource_view(request, scope, fresh), "conversion": "markdown", "diagnostics": diagnostics}
        _with_task_projection(result, await _task_projection_receipt(request, scope))
        publish(scope, "document", result)
        return result

    @router.delete("/documents/{document_id}")
    async def delete_document(document_id: str, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        existing = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        _require_nonplanning_document_mutation(existing)
        doc_corpus = "wiki" if existing.get("kind") == _WIKI_KIND else "notes"
        result = await _call(request, "delete", {**scope, "id": _doc_id(document_id), "corpus": doc_corpus})
        projection = (
            await _project_canonical_workspace(request, scope)
            if event_from_document(existing) or existing.get("kind") == TRACKS_KIND
            else await _project_planning_document(request, scope, existing, deleted=True)
        )
        if projection is not None:
            result["calendar_projection"] = projection
        _with_task_projection(result, await _task_projection_receipt(request, scope))
        publish(scope, "deleted", {"id": document_id})
        return result

    @router.get("/documents/{document_id}/history")
    async def document_history(document_id: str, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        existing = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        doc_corpus = "wiki" if existing.get("kind") == _WIKI_KIND else "notes"
        return await _call(request, "history", {**scope, "id": _doc_id(document_id), "corpus": doc_corpus})

    @router.post("/documents/{document_id}/checkpoint")
    async def checkpoint_document(
        document_id: str,
        payload: CheckpointDocument,
        request: Request,
        workspace: str | None = None,
    ):
        scope = _scope(request, workspace)
        existing = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        _require_nonplanning_document_mutation(existing)
        doc_corpus = "wiki" if existing.get("kind") == _WIKI_KIND else "notes"
        result = await _call(
            request,
            "checkpoint",
            {**scope, "id": _doc_id(document_id), "message": payload.message, "corpus": doc_corpus},
        )
        result = _note_result(result)
        _with_task_projection(result, await _task_projection_receipt(request, scope))
        publish(scope, "document", result)
        return result

    @router.post("/documents/{document_id}/rename")
    async def rename_document(
        document_id: str,
        payload: RenameDocument,
        request: Request,
        workspace: str | None = None,
    ):
        scope = _scope(request, workspace)
        existing = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        _require_nonplanning_document_mutation(existing)
        doc_corpus = "wiki" if existing.get("kind") == _WIKI_KIND else "notes"
        result = await _call(
            request,
            "rename",
            {**scope, "id": _doc_id(document_id), "name": _name(payload.name), "corpus": doc_corpus, **({"action_id": payload.actionId} if payload.actionId else {})},
        )
        result = _note_result(result)
        _with_task_projection(result, await _task_projection_receipt(request, scope))
        publish(scope, "document", result)
        return result

    @router.post("/documents/{document_id}/restore")
    async def restore_document(
        document_id: str,
        payload: RestoreDocument,
        request: Request,
        workspace: str | None = None,
    ):
        scope = _scope(request, workspace)
        existing = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        _require_nonplanning_document_mutation(existing)
        doc_corpus = "wiki" if existing.get("kind") == _WIKI_KIND else "notes"
        result = await _call(
            request,
            "restore",
            {**scope, "id": _doc_id(document_id), "commit": payload.commit, "corpus": doc_corpus, **({"action_id": payload.actionId} if payload.actionId else {})},
        )
        indexed = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        projection = (
            await _project_canonical_workspace(request, scope)
            if event_from_document(indexed) or indexed.get("kind") == TRACKS_KIND
            else await _project_planning_document(request, scope, indexed)
        )
        if projection is not None:
            result["calendar_projection"] = projection
        result = _note_result(result)
        _with_task_projection(result, await _task_projection_receipt(request, scope))
        publish(scope, "document", result)
        return result

    @router.post("/calendar/reconcile")
    async def reconcile_calendar(
        payload: ReconcileCalendar,
        request: Request,
        workspace: str | None = None,
    ):
        """Idempotently repair native mirrors without changing Calendar reads."""
        scope = _scope(request, workspace)
        all_docs = await _indexed_documents(request, scope)
        registry, _, _ = canonical_documents(all_docs)
        try:
            track_registry_from_document(registry)
        except PlanningValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        if payload.document_id:
            docs = [await _call(request, "get", {**scope, "id": _doc_id(payload.document_id)})]
        else:
            try:
                canonical = planning_projection(all_docs).get("canonical")
            except PlanningValidationError as exc:
                raise HTTPException(422, str(exc)) from exc
            if canonical:
                projection = await _project_canonical_workspace(request, scope)
                return {"ok": projection is None or projection.get("ok", True), "projections": [projection] if projection else []}
            docs = [doc for doc in all_docs if doc.get("kind") == "planning"]
        results = []
        for doc in docs:
            projection = await _project_planning_document(request, scope, doc)
            if projection is not None:
                results.append({"documentId": doc.get("id"), **projection})
        return {"ok": all(item.get("ok", True) for item in results), "projections": results}

    @router.post("/bases/validate")
    async def validate_base(payload: ValidateBase, request: Request):
        _scope(request)  # enforce the same auth boundary as every Base operation
        try:
            definition, diagnostics = parse_base_definition(payload.content)
        except BaseDefinitionError as exc:
            raise HTTPException(422, detail={"diagnostics": exc.diagnostics}) from exc
        return {"ok": True, "definition": definition, "diagnostics": diagnostics, "canonical": dump_base_definition(definition)}

    @router.post("/bases/{base_id}/transform")
    @router.post("/bases/{base_id}/command")
    async def transform_base(
        base_id: str,
        payload: TransformBase,
        request: Request,
        workspace: str | None = None,
    ):
        """Transform a Base source snapshot, optionally submitting the result.

        The source and head are resolved inside the authenticated Copal
        scope.  A preview never calls ``write``; an applied command uses the
        same CAS write path as document edits and returns its complete receipt
        envelope when available from the bridge.
        """
        scope = _scope(request, workspace)
        base_doc = await _call(request, "get", {**scope, "id": _doc_id(base_id)})
        if base_doc.get("kind") != "base":
            raise HTTPException(400, "Document is not a Base")
        if payload.apply and payload.source is not None:
            raise HTTPException(400, "Applied Base commands must use the authorized persisted source")
        source = payload.source if payload.source is not None else str(base_doc.get("text") or "")
        current_revision = base_doc.get("head")
        try:
            transformed = transform_base_definition(
                source,
                payload.command,
                revision=current_revision,
                expected_revision=payload.base if payload.base is not None else None,
            )
        except BaseDefinitionError as exc:
            status = 409 if exc.diagnostics[0].get("code") == "stale_revision" else 422
            raise HTTPException(status, detail={"diagnostics": exc.diagnostics, "revision": current_revision}) from exc
        response: dict[str, Any] = {
            "ok": True,
            "base": {"id": base_doc.get("id"), "name": base_doc.get("name"), "head": current_revision},
            **transformed,
            "applied": False,
        }
        if not payload.apply:
            return response
        result = await _call(
            request,
            "write",
            {
                **scope,
                "id": _doc_id(base_id),
                "content": transformed["source"],
                "base": payload.base or current_revision,
                "action_id": payload.actionId,
            },
        )
        if result.get("outcome") == "stale":
            authoritative = _resource_view(request, scope, result["doc"]) if isinstance(result.get("doc"), dict) else result.get("doc")
            raise HTTPException(409, detail={"outcome": "stale", "doc": authoritative, "revision": current_revision})
        response.update({"applied": True, "result": _note_result(result)})
        if isinstance(result.get("doc"), dict):
            response["base"] = {
                "id": result["doc"].get("id", base_doc.get("id")),
                "name": result["doc"].get("name", base_doc.get("name")),
                "head": result["doc"].get("head"),
            }
        publish(scope, "document", result)
        return response

    @router.post("/bases/{base_id}/query/preview")
    @router.post("/bases/{base_id}/query-preview")
    @router.post("/bases/{base_id}/query")
    async def preview_base_query(
        base_id: str,
        payload: BaseQueryPreview,
        request: Request,
        workspace: str | None = None,
        view: str | None = Query(None, max_length=64),
        query: str = Query("", max_length=512),
        page: int = Query(1, ge=1),
        page_size: int = Query(100, ge=1, le=500),
    ):
        """Evaluate a current client draft against the authorized note corpus.

        This is deliberately separate from the persisted GET route.  A dirty
        source-mode Base can be shared by multiple leaves while retaining the
        persisted CAS head; a preview never writes and rejects a snapshot that
        was based on an older server revision.
        """
        scope = _scope(request, workspace)
        base_doc = await _call(request, "get", {**scope, "id": _doc_id(base_id)})
        if base_doc.get("kind") != "base":
            raise HTTPException(400, "Document is not a Base")
        current_revision = base_doc.get("head")
        if payload.base is not None and payload.base != current_revision:
            raise HTTPException(409, detail={"outcome": "stale", "revision": current_revision})
        try:
            definition, diagnostics = parse_base_definition(payload.source)
            indexed = await _call(request, "index", scope, timeout=60)
            result = query_base(
                definition,
                [_note_view(document) for document in indexed.get("docs", [])],
                view_id=view,
                query=query,
                page=page,
                page_size=page_size,
            )
        except BaseDefinitionError as exc:
            raise HTTPException(422, detail={"diagnostics": exc.diagnostics}) from exc
        return {
            "base": {"id": base_doc.get("id"), "name": base_doc.get("name"), "head": current_revision},
            "revision": current_revision,
            "definitionRevision": payload.definitionRevision,
            "draft": True,
            "diagnostics": diagnostics,
            **result,
        }

    @router.get("/bases/{base_id}/query")
    async def query_base_document(
        base_id: str,
        request: Request,
        workspace: str | None = None,
        view: str | None = Query(None, max_length=64),
        query: str = Query("", max_length=512),
        page: int = Query(1, ge=1),
        page_size: int = Query(100, ge=1, le=500),
    ):
        scope = _scope(request, workspace)
        base_doc = await _call(request, "get", {**scope, "id": _doc_id(base_id)})
        if base_doc.get("kind") != "base":
            raise HTTPException(400, "Document is not a Base")
        try:
            definition, diagnostics = parse_base_definition(base_doc.get("text") or "")
            indexed = await _call(request, "index", scope, timeout=60)
            result = query_base(
                definition,
                [_note_view(document) for document in indexed.get("docs", [])],
                view_id=view,
                query=query,
                page=page,
                page_size=page_size,
            )
        except BaseDefinitionError as exc:
            raise HTTPException(422, detail={"diagnostics": exc.diagnostics}) from exc
        return {
            "base": {"id": base_doc.get("id"), "name": base_doc.get("name"), "head": base_doc.get("head")},
            "definition": definition,
            "diagnostics": diagnostics,
            **result,
        }

    @router.post("/bases/{base_id}/migrate")
    async def migrate_base_document(
        base_id: str,
        payload: MigrateBase,
        request: Request,
        workspace: str | None = None,
        dry_run: bool = Query(True),
    ):
        scope = _scope(request, workspace)
        base_doc = await _call(request, "get", {**scope, "id": _doc_id(base_id)})
        if base_doc.get("kind") != "base":
            raise HTTPException(400, "Document is not a Base")
        try:
            definition, diagnostics = parse_base_definition(base_doc.get("text") or "")
        except BaseDefinitionError as exc:
            raise HTTPException(422, detail={"diagnostics": exc.diagnostics}) from exc
        canonical = dump_base_definition(definition)
        if dry_run:
            return {"ok": True, "dryRun": True, "changed": canonical != base_doc.get("text"), "canonical": canonical, "diagnostics": diagnostics}
        result = await _call(
            request,
            "write",
            {**scope, "id": _doc_id(base_id), "content": canonical, "base": payload.base or base_doc.get("head")},
        )
        if result.get("outcome") == "stale":
            raise HTTPException(409, detail={"outcome": "stale", "doc": result.get("doc")})
        publish(scope, "document", result)
        return {"ok": True, "dryRun": False, "result": result, "diagnostics": diagnostics}

    @router.patch("/bases/{base_id}/rows/{document_id}")
    async def edit_base_row(
        base_id: str,
        document_id: str,
        payload: EditBaseRow,
        request: Request,
        workspace: str | None = None,
    ):
        scope = _scope(request, workspace)
        base_doc = await _call(request, "get", {**scope, "id": _doc_id(base_id)})
        if base_doc.get("kind") != "base":
            raise HTTPException(400, "Document is not a Base")
        stored = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        source = _note_view(stored)
        _require_mutable_note(source)
        if source.get("kind") in {"base", "planning", "calendar-projection", "treehouse-state"} or _is_asset_kind(source.get("kind")):
            raise HTTPException(400, "That Base row is not editable")
        property_name = _editable_source_property(payload.property)
        if source.get("kind") in _NOTE_KINDS:
            properties = dict(source.get("properties") or {})
            if payload.clear:
                properties.pop(property_name, None)
            else:
                properties[property_name] = payload.value
            if stored.get("format") == "copal-note-v1":
                previous = {
                    "body": {"type": "doc", "blocks": stored.get("blocks") or []},
                    "properties": stored.get("propertyDefinitions") or [],
                    "relations": stored.get("relations") or [],
                    "extensions": stored.get("extensions") or {},
                }
            else:
                try:
                    previous = json.loads(str(stored.get("text") or "{}"))
                except json.JSONDecodeError:
                    previous = None
            content = _encode_note(
                str(source.get("text") or ""), properties, previous=previous if isinstance(previous, dict) else None,
            )
        else:
            try:
                content = set_frontmatter_property(
                    source.get("text") or "", property_name, payload.value, remove=payload.clear,
                )
            except BaseDefinitionError as exc:
                raise HTTPException(422, detail={"diagnostics": exc.diagnostics}) from exc
        result = await _call(
            request,
            "write",
            {
                **scope,
                "id": _doc_id(document_id),
                "content": content,
                "base": payload.base or source.get("head"),
                "action_id": payload.actionId,
            },
        )
        if result.get("outcome") == "stale":
            authoritative = _note_view(result["doc"]) if isinstance(result.get("doc"), dict) else result.get("doc")
            raise HTTPException(409, detail={"outcome": "stale", "doc": authoritative})
        result = _note_result(result)
        publish(scope, "document", result)
        return result

    @router.get("/operations")
    async def operations(
        request: Request,
        workspace: str | None = None,
        limit: int = Query(50, ge=1, le=500),
    ):
        require_admin(request)
        return await _call(request, "ops", {**_scope(request, workspace), "limit": limit})

    @router.get("/events")
    async def events(request: Request, workspace: str | None = None):
        authenticated_owner = require_user(request)
        scope = {
            "owner": copal_owner_for_user(authenticated_owner),
            "workspace_id": _workspace(request, workspace),
        }
        session_token = request.cookies.get(_SESSION_COOKIE) if authenticated_owner else None
        key = (scope["owner"], scope["workspace_id"])
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        subscribers[key].add(queue)

        async def stream() -> AsyncIterator[bytes]:
            try:
                if not _stream_owner_is_current(request, authenticated_owner, session_token):
                    return
                yield b"event: ready\ndata: {}\n\n"
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                        if not _stream_owner_is_current(request, authenticated_owner, session_token):
                            return
                        data = json.dumps(item["data"], separators=(",", ":"))
                        yield f"event: {item['event']}\ndata: {data}\n\n".encode()
                    except asyncio.TimeoutError:
                        if not _stream_owner_is_current(request, authenticated_owner, session_token):
                            return
                        yield b": keepalive\n\n"
            finally:
                subscribers[key].discard(queue)
                if not subscribers[key]:
                    subscribers.pop(key, None)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @router.get("/export/memes")
    async def export_memes(request: Request, workspace: str | None = None):
        """Download the scoped native Wiki interchange envelope."""
        scope = _scope(request, workspace)
        payload = await _memes_export_payload(request, scope)
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return Response(
            content=content,
            media_type=MEMES_MIME,
            headers={"Content-Disposition": 'attachment; filename="wiki.memes"'},
        )

    @router.post("/preview/memes")
    async def preview_memes(request: Request, file: UploadFile = File(...), workspace: str | None = None):
        """Validate a hosted `.memes` file without importing it."""
        _scope(request, workspace)
        payload = await _read_memes_upload(file)
        return {
            "preview": True,
            "format": payload["format"],
            "schemaVersion": payload["schemaVersion"],
            "documents": [{"name": item["name"], "kind": item["kind"]} for item in payload["documents"]],
            "assets": [{"name": item["name"], "mime": item["mime"], "byteLength": item["byteLength"]} for item in payload["assets"]],
        }

    @router.post("/import/memes")
    async def import_memes(
        request: Request,
        file: UploadFile = File(...),
        workspace: str | None = None,
        mode: str = "import",
    ):
        """Import a validated native Wiki envelope into the current scope."""
        scope = _scope(request, workspace)
        payload = await _read_memes_upload(file)
        result = await _import_memes_payload(request, scope, payload, mode)
        publish(scope, "document", {"operation": "import-memes", "result": result})
        return {"ok": True, "imported": result, "previewRequired": True}

    @router.post("/import/obsidian")
    async def import_obsidian(
        request: Request,
        file: UploadFile = File(...),
        workspace: str | None = None,
        corpus: str = Query("notes", pattern="^(notes|wiki)$"),
    ):
        """Import an Obsidian/Copal ZIP as one scoped Redb operation."""
        scope = _scope(request, workspace)
        with tempfile.TemporaryDirectory(prefix="copal-import-") as temporary:
            temporary_path = Path(temporary)
            archive_path = temporary_path / "upload.zip"
            compressed_bytes = await copy_upload_limited(file, archive_path, COPAL_IMPORT_MAX_BYTES, "Copal ZIP")
            try:
                archive = zipfile.ZipFile(archive_path)
            except zipfile.BadZipFile as exc:
                raise HTTPException(400, "Invalid Copal/Obsidian ZIP") from exc

            with archive:
                members = _validated_zip_members(archive)
                root = temporary_path / "vault"
                root.mkdir()
                expanded_bytes = 0
                try:
                    for info, relative in members:
                        target = root.joinpath(*relative.parts)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        member_bytes = 0
                        with archive.open(info) as source, target.open("xb") as destination:
                            while chunk := source.read(1024 * 1024):
                                member_bytes += len(chunk)
                                expanded_bytes += len(chunk)
                                if (
                                    member_bytes > info.file_size
                                    or member_bytes > _COPAL_IMPORT_MAX_MEMBER_BYTES
                                    or expanded_bytes > _COPAL_IMPORT_MAX_EXPANDED_BYTES
                                ):
                                    raise HTTPException(413, "Copal import expands beyond its declared safety limits")
                                destination.write(chunk)
                        if member_bytes != info.file_size:
                            raise HTTPException(400, f"ZIP member size is inconsistent: {info.filename}")
                except zipfile.BadZipFile as exc:
                    raise HTTPException(400, "Invalid or corrupt Copal/Obsidian ZIP") from exc

            restore_ids, restore_manifest = _export_restore_identities(
                root,
                members,
                scope["workspace_id"],
            )
            preserved_paths = {
                name for name, identity in restore_ids.items()
                if identity["kind"] == "compatibility"
            }
            preparation = await asyncio.to_thread(_prepare_import_tree, root, preserved_paths)
            planning = root / ".copal" / "planning.json"
            if not planning.is_file():
                planning = root / "move-data.json"
            result = await _call(
                request,
                "import_vault",
                {
                    **scope,
                    "path": str(root),
                    "planning_path": str(planning) if planning.is_file() else None,
                    "note_kind": _WIKI_KIND if corpus == "wiki" else _NOTE_KIND,
                    "restore_ids": restore_ids,
                },
                timeout=120,
            )

            projections = []
            indexed = await _call(request, "index", {**scope, "kind": "planning"})
            for document in indexed.get("docs", []):
                projection = await _project_planning_document(request, scope, document)
                if projection is not None:
                    projections.append({"documentId": document.get("id"), **projection})

        publish(scope, "document", {"operation": "import", "result": result})
        return {
            "ok": True,
            "imported": result,
            "files": len(members),
            "compressedBytes": compressed_bytes,
            "corpus": corpus,
            "preparation": preparation,
            "restoreManifest": {
                "present": restore_manifest,
                "identities": len(restore_ids),
            },
            "calendarProjections": projections,
        }

    @router.get("/export/obsidian")
    async def export_obsidian(request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        snapshot = await _call(request, "export_snapshot", scope, timeout=60)
        docs = [
            _note_view(document)
            for document in snapshot.get("docs", [])
            if not document.get("readOnly")
        ]
        canonical = bool(next((doc for doc in docs if doc.get("kind") == TRACKS_KIND), None))
        output = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        manifest = {
            "format": "copal-obsidian-export-v1",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "workspace": scope["workspace_id"],
            "documents": [],
        }
        try:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for doc in docs:
                    export_name = _safe_export_name(doc)
                    if event := event_from_document(doc):
                        export_name = event_document_name(event)
                    if doc.get("corpus") == "wiki":
                        export_name = _name(f".copal/wiki/{export_name}")
                    if canonical and doc.get("kind") == "planning":
                        export_name = ".copal/planning.legacy.json"
                    if _is_asset_kind(doc.get("kind")) or _is_compatibility_kind(doc.get("kind")):
                        try:
                            path, _ = await _asset_file(request, scope, doc["id"])
                        except HTTPException as exc:
                            if exc.status_code == 404:
                                raise HTTPException(
                                    409,
                                    f"Export integrity failure: asset bytes are missing for {export_name}",
                                ) from exc
                            raise
                        content_size = path.stat().st_size
                        content_digest = hashlib.sha256()
                        with path.open("rb") as source:
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                content_digest.update(chunk)
                        archive.write(path, export_name)
                    else:
                        content = _note_markdown(doc) if doc.get("kind") in _NOTE_KINDS else str(doc.get("text") or "")
                        content_bytes = content.encode("utf-8")
                        content_size = len(content_bytes)
                        content_digest = hashlib.sha256(content_bytes)
                        archive.writestr(export_name, content_bytes)
                    manifest["documents"].append({
                        "id": doc["id"],
                        "corpus": doc.get("corpus") or "system",
                        "kind": doc["kind"],
                        "path": export_name,
                        "size": content_size,
                        "sha256": content_digest.hexdigest(),
                    })
                archive.writestr(".copal/export-manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            export_size = output.tell()
            output.seek(0)
        except BaseException:
            output.close()
            raise

        async def export_chunks() -> AsyncIterator[bytes]:
            try:
                while chunk := output.read(1024 * 1024):
                    yield chunk
            finally:
                output.close()

        headers = {
            "Content-Disposition": 'attachment; filename="copal-obsidian-export.zip"',
            "Content-Length": str(export_size),
        }
        return StreamingResponse(export_chunks(), media_type="application/zip", headers=headers)

    @router.get("/export/download/{download_id}")
    async def download_native_export(request: Request, download_id: str, workspace: str | None = None):
        """Serve a short-lived native-tool export after rechecking owner scope."""
        scope = _scope(request, workspace)
        from src.openclank.copal_transfer import CopalTransferError, load_download

        try:
            path, metadata = load_download(_bridge(request), download_id=download_id, owner=scope["owner"], workspace=scope["workspace_id"])
        except CopalTransferError as exc:
            status = 403 if exc.code == "download_forbidden" else 404
            raise HTTPException(status, str(exc)) from exc
        return FileResponse(
            path,
            media_type="application/zip",
            filename=str(metadata.get("filename") or "copal-obsidian-export.zip"),
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    from routes.copal_treehouse_routes import setup_treehouse_routes
    router.include_router(
        setup_treehouse_routes(
            call=_call,
            scope_for=_scope,
            publish=publish,
            policy_repository=policy_repository,
        )
    )

    return router

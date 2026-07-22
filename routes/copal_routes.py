"""User-scoped Odysseus API for Copal's owned Redb bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import stat
import tempfile
import unicodedata
import uuid
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator

import yaml

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from core.middleware import require_admin
from src.auth_helpers import copal_owner_for_user, require_user
from src.openclank.copal_bridge import CopalBridgeError
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
    track_registry_from_document,
    validate_event,
    validate_tracks,
)
from src.openclank.copal_bases import (
    BaseDefinitionError,
    dump_base_definition,
    parse_base_definition,
    query_base,
    set_frontmatter_property,
)
from src.upload_limits import COPAL_IMPORT_MAX_BYTES, copy_upload_limited


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
_NOTE_SCHEMA_VERSION = 1
_NOTE_MAX_PROPERTIES_BYTES = 262_144
_COPAL_IMPORT_MAX_FILES = 10_000
_COPAL_IMPORT_MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
_COPAL_IMPORT_MAX_MEMBER_BYTES = 512 * 1024 * 1024
_COPAL_IMPORT_MAX_COMPRESSION_RATIO = 250


def _is_asset_kind(kind: Any) -> bool:
    return str(kind or "") == "asset"


def _is_compatibility_kind(kind: Any) -> bool:
    return str(kind or "") == "compatibility"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateDocument(_StrictModel):
    name: str = Field(min_length=1, max_length=512)
    kind: str = Field(default=_NOTE_KIND, min_length=1, max_length=64)
    content: str = Field(default="", max_length=8_388_608)
    properties: dict[str, Any] = Field(default_factory=dict, max_length=256)
    relations: list[dict[str, Any]] = Field(default_factory=list, max_length=10_000)
    corpus: str = Field(default="notes", pattern="^(notes|wiki)$")


class WriteDocument(_StrictModel):
    content: str = Field(max_length=8_388_608)
    base: str | None = Field(default=None, max_length=128)
    properties: dict[str, Any] | None = Field(default=None, max_length=256)
    relations: list[dict[str, Any]] | None = Field(default=None, max_length=10_000)


class RenameDocument(_StrictModel):
    name: str = Field(min_length=1, max_length=512)


class CheckpointDocument(_StrictModel):
    message: str | None = Field(default=None, max_length=512)


class RestoreDocument(_StrictModel):
    commit: str = Field(min_length=1, max_length=128)


class ReconcileCalendar(_StrictModel):
    document_id: str | None = Field(default=None, max_length=128)


class ValidateBase(_StrictModel):
    content: str = Field(max_length=262_144)


class MigrateBase(_StrictModel):
    base: str | None = Field(default=None, max_length=128)


class EditBaseRow(_StrictModel):
    property: str = Field(min_length=1, max_length=128)
    value: Any = None
    base: str | None = Field(default=None, max_length=128)


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


def _bridge(request: Request):
    bridge = getattr(request.app.state, "copal_bridge", None)
    if not bridge:
        raise HTTPException(503, "Copal database bridge is unavailable")
    return bridge


def _workspace(request: Request, value: str | None = None) -> str:
    workspace = (value or request.headers.get("X-Copal-Workspace") or "default").strip()
    if not _WORKSPACE.fullmatch(workspace):
        raise HTTPException(400, "Invalid Copal workspace")
    return workspace


def _scope(request: Request, workspace: str | None = None) -> dict[str, str]:
    return {
        "owner": copal_owner_for_user(require_user(request)),
        "workspace_id": _workspace(request, workspace),
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
    elif match := re.fullmatch(r"(\s*)-\s+\[([ xX])\]\s*(.*)", line):
        block = {"type": "task", "indent": len(match.group(1)), "checked": match.group(2).lower() == "x", "text": match.group(3)}
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
        return f"{' ' * max(0, int(block.get('indent') or 0))}- [{'x' if block.get('checked') else ' '}] {text}"
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
    return [
        {
            "id": f"{document_id}:{block['id']}",
            "blockId": block["id"],
            "line": index + 1,
            "done": bool(block.get("checked")),
            "text": str(block.get("text") or ""),
        }
        for index, block in enumerate(blocks)
        if isinstance(block, dict) and block.get("type") == "task" and isinstance(block.get("id"), str)
    ]


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


def _note_view(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("kind") not in _NOTE_KINDS:
        return document
    if document.get("format") == "copal-note-v1" and document.get("storage") == "database":
        return document
    result = dict(document)
    raw = str(document.get("text") or "")
    try:
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get("schemaVersion") != _NOTE_SCHEMA_VERSION:
            raise ValueError("unsupported database note schema")
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
            "format": "copal-note-v1",
            "storage": "database",
            "corpus": document.get("corpus") or ("wiki" if document.get("kind") == _WIKI_KIND else "notes"),
            "extensions": record.get("extensions") if isinstance(record.get("extensions"), dict) else {},
        })
    except (HTTPException, json.JSONDecodeError, TypeError, ValueError) as exc:
        result.update({
            "text": "", "properties": {}, "frontmatter": {}, "relations": [], "links": [], "tags": [],
            "blocks": [], "format": "copal-note-v1", "storage": "database",
            "corpus": document.get("corpus") or ("wiki" if document.get("kind") == _WIKI_KIND else "notes"),
            "extensions": {},
            "note_error": str(exc), "rawPreserved": True,
        })
    return result


def _note_result(result: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(result)
    if isinstance(result.get("doc"), dict):
        normalized["doc"] = _note_view(result["doc"])
    return normalized


def _require_mutable_note(document: dict[str, Any]) -> None:
    if document.get("kind") in _NOTE_KINDS and (document.get("rawPreserved") or document.get("note_error")):
        raise HTTPException(409, "Database note is preserved read-only until its stored schema can be decoded")


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
            raise HTTPException(503, "Copal database bridge is unavailable") from exc
    try:
        return await bridge.call(operation, args, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, "Copal database operation timed out") from exc
    except CopalBridgeError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 403 if "read-only" in message else 409 if "exists" in message else 400
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
    return [_note_view(document) for document in indexed.get("docs") or []]


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


async def _asset_file(request: Request, scope: dict[str, str], document_id: str) -> tuple[Path, str]:
    asset = await _call(request, "asset_path", {**scope, "id": _doc_id(document_id)})
    path = Path(asset["path"]).resolve()
    assets_root = (_bridge(request).data_dir / "assets").resolve()
    if path.parent != assets_root or not path.is_file():
        raise HTTPException(404, "Asset not found")
    return path, str(asset.get("name") or document_id)


def setup_copal_routes() -> APIRouter:
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
        documents = [_note_view(document) for document in result.get("docs") or []]
        if kind is None:
            documents = [
                document
                for document in documents
                if not _is_compatibility_kind(document.get("kind"))
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
        tracks = track_registry_from_document(registry).get("tracks") or []
        try:
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
        tracks = track_registry_from_document(registry).get("tracks") or []
        source = next((doc for doc in docs if doc.get("id") == _doc_id(document_id)), None)
        event = event_from_document(source or {})
        if not source or not event:
            raise HTTPException(404, "Canonical Copal event not found")
        try:
            merged = merge_event(event, payload.patch, tracks)
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
        source = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        if not event_from_document(source):
            raise HTTPException(400, "Document is not a canonical Copal event")
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
            tracks = validate_tracks(payload.tracks)
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
        # Try notes first, then wiki for deleted docs.
        try:
            result = await _call(request, "restore_deleted", {**scope, "id": _doc_id(document_id), "corpus": "notes"})
        except Exception:
            result = await _call(request, "restore_deleted", {**scope, "id": _doc_id(document_id), "corpus": "wiki"})
        restored = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        projection = (
            await _project_canonical_workspace(request, scope)
            if event_from_document(restored) or restored.get("kind") == TRACKS_KIND
            else await _project_planning_document(request, scope, restored)
        )
        if projection is not None:
            result["calendar_projection"] = projection
        result = _note_result(result)
        publish(scope, "document", result)
        return result

    @router.get("/documents/{document_id}")
    async def get_document(document_id: str, request: Request, workspace: str | None = None):
        document = await _call(request, "get", {**_scope(request, workspace), "id": _doc_id(document_id)})
        return _note_view(document)

    @router.get("/assets/{document_id}")
    async def get_asset(document_id: str, request: Request, workspace: str | None = None):
        path, name = await _asset_file(request, _scope(request, workspace), document_id)
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})

    @router.post("/documents")
    async def create_document(payload: CreateDocument, request: Request, workspace: str | None = None):
        if not _KIND.fullmatch(payload.kind):
            raise HTTPException(400, "Invalid document kind")
        kind = _WIKI_KIND if payload.corpus == "wiki" and payload.kind == _NOTE_KIND else payload.kind
        if payload.corpus == "wiki" and kind != _WIKI_KIND:
            raise HTTPException(422, "The Wiki corpus accepts database Wiki records only")
        if kind not in _NOTE_KINDS and (payload.properties or payload.relations):
            raise HTTPException(422, "Typed properties and relations belong to database notes")
        scope = _scope(request, workspace)
        content = _encode_note(payload.content, payload.properties, payload.relations) if kind in _NOTE_KINDS else payload.content
        result = await _call(
            request,
            "create",
            {**scope, "name": _name(payload.name), "kind": kind, "content": content, "corpus": payload.corpus},
        )
        if result.get("doc", {}).get("id"):
            indexed = await _call(request, "get", {**scope, "id": result["doc"]["id"]})
            if kind == "planning":
                result["calendar_projection"] = await _project_planning_document(request, scope, indexed)
            elif event_from_document(indexed) or payload.kind == TRACKS_KIND:
                result["calendar_projection"] = await _project_canonical_workspace(request, scope)
        result = _note_result(result)
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
            {**scope, "id": _doc_id(document_id), "content": content, "base": payload.base, "corpus": write_corpus},
        )
        if result.get("outcome") == "stale":
            authoritative = _note_view(result["doc"]) if isinstance(result.get("doc"), dict) else result.get("doc")
            raise HTTPException(409, detail={"outcome": "stale", "doc": authoritative})
        indexed = _note_view(await _call(request, "get", {**scope, "id": _doc_id(document_id)}))
        projection = (
            await _project_canonical_workspace(request, scope)
            if event_from_document(existing) or event_from_document(indexed) or indexed.get("kind") == TRACKS_KIND
            else await _project_planning_document(request, scope, indexed)
        )
        if projection is not None:
            result["calendar_projection"] = projection
        result = _note_result(result)
        publish(scope, "document", result)
        return result

    @router.delete("/documents/{document_id}")
    async def delete_document(document_id: str, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        existing = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        doc_corpus = "wiki" if existing.get("kind") == _WIKI_KIND else "notes"
        result = await _call(request, "delete", {**scope, "id": _doc_id(document_id), "corpus": doc_corpus})
        projection = (
            await _project_canonical_workspace(request, scope)
            if event_from_document(existing) or existing.get("kind") == TRACKS_KIND
            else await _project_planning_document(request, scope, existing, deleted=True)
        )
        if projection is not None:
            result["calendar_projection"] = projection
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
        doc_corpus = "wiki" if existing.get("kind") == _WIKI_KIND else "notes"
        result = await _call(
            request,
            "checkpoint",
            {**scope, "id": _doc_id(document_id), "message": payload.message, "corpus": doc_corpus},
        )
        result = _note_result(result)
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
        doc_corpus = "wiki" if existing.get("kind") == _WIKI_KIND else "notes"
        result = await _call(
            request,
            "rename",
            {**scope, "id": _doc_id(document_id), "name": _name(payload.name), "corpus": doc_corpus},
        )
        result = _note_result(result)
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
        doc_corpus = "wiki" if existing.get("kind") == _WIKI_KIND else "notes"
        result = await _call(
            request,
            "restore",
            {**scope, "id": _doc_id(document_id), "commit": payload.commit, "corpus": doc_corpus},
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
        if payload.document_id:
            docs = [await _call(request, "get", {**scope, "id": _doc_id(payload.document_id)})]
        else:
            all_docs = await _indexed_documents(request, scope)
            if planning_projection(all_docs).get("canonical"):
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

    @router.get("/bases/{base_id}/query")
    async def query_base_document(
        base_id: str,
        request: Request,
        workspace: str | None = None,
        view: str | None = Query(None, max_length=64),
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
        if source.get("kind") in _NOTE_KINDS:
            properties = dict(source.get("properties") or {})
            properties[payload.property] = payload.value
            if stored.get("format") == "copal-note-v1":
                previous = {
                    "body": {"type": "doc", "blocks": stored.get("blocks") or []},
                    "properties": stored.get("propertyDefinitions") or [],
                    "relations": stored.get("relations") or [],
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
                content = set_frontmatter_property(source.get("text") or "", payload.property, payload.value)
            except BaseDefinitionError as exc:
                raise HTTPException(422, detail={"diagnostics": exc.diagnostics}) from exc
        result = await _call(
            request,
            "write",
            {**scope, "id": _doc_id(document_id), "content": content, "base": payload.base or source.get("head")},
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

    from routes.copal_treehouse_routes import setup_treehouse_routes
    router.include_router(setup_treehouse_routes(call=_call, scope_for=_scope, publish=publish))

    return router

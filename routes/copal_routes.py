"""User-scoped Odysseus API for Copal's owned Redb bridge."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import mimetypes
import re
import stat
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from core.middleware import require_admin
from src.auth_helpers import require_user
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
from src.upload_limits import COPAL_IMPORT_MAX_BYTES, read_upload_limited


logger = logging.getLogger(__name__)


_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_WORKSPACE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_KIND = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_COPAL_IMPORT_MAX_FILES = 10_000
_COPAL_IMPORT_MAX_EXPANDED_BYTES = 256 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateDocument(_StrictModel):
    name: str = Field(min_length=1, max_length=512)
    kind: str = Field(default="markdown", min_length=1, max_length=64)
    content: str = Field(default="", max_length=8_388_608)


class WriteDocument(_StrictModel):
    content: str = Field(max_length=8_388_608)
    base: str | None = Field(default=None, max_length=128)


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
        "owner": require_user(request) or "local",
        "workspace_id": _workspace(request, workspace),
    }


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


def _validated_zip_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    expanded = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        mode = info.external_attr >> 16
        if (
            not name
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or stat.S_ISLNK(mode)
            or info.flag_bits & 0x1
        ):
            raise HTTPException(400, f"Unsafe ZIP member: {info.filename}")
        expanded += info.file_size
        if len(members) >= _COPAL_IMPORT_MAX_FILES or expanded > _COPAL_IMPORT_MAX_EXPANDED_BYTES:
            raise HTTPException(413, "Copal import expands beyond its safety limit")
        members.append((info, path))
    return members


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
        status = 404 if "not found" in message else 409 if "exists" in message else 400
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
    return list(indexed.get("docs") or [])


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
    if kind not in {"asset", "base", "canvas"} and not suffix:
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
    async def status(request: Request):
        scope = _scope(request)
        result = await _call(request, "status", {})
        visible = await _call(request, "list", scope)
        return {**result, "visible_documents": len(visible.get("docs", [])), "workspace": scope["workspace_id"]}

    @router.get("/documents")
    async def list_documents(
        request: Request,
        workspace: str | None = None,
        query: str = Query("", max_length=512),
        kind: str | None = Query(None, max_length=64),
    ):
        scope = _scope(request, workspace)
        if kind and not _KIND.fullmatch(kind):
            raise HTTPException(400, "Invalid document kind")
        return await _call(request, "index", {**scope, "query": query, "kind": kind})

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
        return await _call(request, "trash", _scope(request, workspace))

    @router.post("/trash/{document_id}/restore")
    async def restore_deleted_document(document_id: str, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        result = await _call(request, "restore_deleted", {**scope, "id": _doc_id(document_id)})
        restored = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        projection = (
            await _project_canonical_workspace(request, scope)
            if event_from_document(restored) or restored.get("kind") == TRACKS_KIND
            else await _project_planning_document(request, scope, restored)
        )
        if projection is not None:
            result["calendar_projection"] = projection
        publish(scope, "document", result)
        return result

    @router.get("/documents/{document_id}")
    async def get_document(document_id: str, request: Request, workspace: str | None = None):
        return await _call(request, "get", {**_scope(request, workspace), "id": _doc_id(document_id)})

    @router.get("/assets/{document_id}")
    async def get_asset(document_id: str, request: Request, workspace: str | None = None):
        path, name = await _asset_file(request, _scope(request, workspace), document_id)
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})

    @router.post("/documents")
    async def create_document(payload: CreateDocument, request: Request, workspace: str | None = None):
        if not _KIND.fullmatch(payload.kind):
            raise HTTPException(400, "Invalid document kind")
        scope = _scope(request, workspace)
        result = await _call(
            request,
            "create",
            {**scope, "name": _name(payload.name), "kind": payload.kind, "content": payload.content},
        )
        if result.get("doc", {}).get("id"):
            indexed = await _call(request, "get", {**scope, "id": result["doc"]["id"]})
            if payload.kind == "planning":
                result["calendar_projection"] = await _project_planning_document(request, scope, indexed)
            elif event_from_document(indexed) or payload.kind == TRACKS_KIND:
                result["calendar_projection"] = await _project_canonical_workspace(request, scope)
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
        existing = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        if existing.get("kind") == "planning" and await _planning_write_locked(request, scope):
            raise HTTPException(409, "Legacy planning JSON is read-only after canonical migration")
        result = await _call(
            request,
            "write",
            {**scope, "id": _doc_id(document_id), "content": payload.content, "base": payload.base},
        )
        if result.get("outcome") == "stale":
            raise HTTPException(409, detail={"outcome": "stale", "doc": result.get("doc")})
        indexed = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        projection = (
            await _project_canonical_workspace(request, scope)
            if event_from_document(existing) or event_from_document(indexed) or indexed.get("kind") == TRACKS_KIND
            else await _project_planning_document(request, scope, indexed)
        )
        if projection is not None:
            result["calendar_projection"] = projection
        publish(scope, "document", result)
        return result

    @router.delete("/documents/{document_id}")
    async def delete_document(document_id: str, request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        existing = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        result = await _call(request, "delete", {**scope, "id": _doc_id(document_id)})
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
        return await _call(request, "history", {**_scope(request, workspace), "id": _doc_id(document_id)})

    @router.post("/documents/{document_id}/checkpoint")
    async def checkpoint_document(
        document_id: str,
        payload: CheckpointDocument,
        request: Request,
        workspace: str | None = None,
    ):
        scope = _scope(request, workspace)
        result = await _call(
            request,
            "checkpoint",
            {**scope, "id": _doc_id(document_id), "message": payload.message},
        )
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
        result = await _call(
            request,
            "rename",
            {**scope, "id": _doc_id(document_id), "name": _name(payload.name)},
        )
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
        result = await _call(
            request,
            "restore",
            {**scope, "id": _doc_id(document_id), "commit": payload.commit},
        )
        indexed = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        projection = (
            await _project_canonical_workspace(request, scope)
            if event_from_document(indexed) or indexed.get("kind") == TRACKS_KIND
            else await _project_planning_document(request, scope, indexed)
        )
        if projection is not None:
            result["calendar_projection"] = projection
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
                indexed.get("docs", []),
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
        source = await _call(request, "get", {**scope, "id": _doc_id(document_id)})
        if source.get("kind") in {"base", "asset", "planning", "calendar-projection", "treehouse-state"}:
            raise HTTPException(400, "That Base row is not editable")
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
            raise HTTPException(409, detail={"outcome": "stale", "doc": result.get("doc")})
        publish(scope, "document", result)
        return result

    @router.get("/operations")
    async def operations(request: Request, limit: int = Query(50, ge=1, le=500)):
        require_admin(request)
        return await _call(request, "ops", {"limit": limit})

    @router.get("/events")
    async def events(request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        key = (scope["owner"], scope["workspace_id"])
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        subscribers[key].add(queue)

        async def stream() -> AsyncIterator[bytes]:
            try:
                yield b"event: ready\ndata: {}\n\n"
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                        data = json.dumps(item["data"], separators=(",", ":"))
                        yield f"event: {item['event']}\ndata: {data}\n\n".encode()
                    except asyncio.TimeoutError:
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
    ):
        """Import an Obsidian/Copal ZIP as one scoped Redb operation."""
        scope = _scope(request, workspace)
        content = await read_upload_limited(file, COPAL_IMPORT_MAX_BYTES, "Copal ZIP")
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as exc:
            raise HTTPException(400, "Invalid Copal/Obsidian ZIP") from exc

        with archive, tempfile.TemporaryDirectory(prefix="copal-import-") as temporary:
            members = _validated_zip_members(archive)
            root = Path(temporary)
            for info, relative in members:
                target = root.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as destination:
                    while chunk := source.read(1024 * 1024):
                        destination.write(chunk)
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
            "calendarProjections": projections,
        }

    @router.get("/export/obsidian")
    async def export_obsidian(request: Request, workspace: str | None = None):
        scope = _scope(request, workspace)
        snapshot = await _call(request, "export_snapshot", scope, timeout=60)
        docs = snapshot.get("docs", [])
        canonical = bool(next((doc for doc in docs if doc.get("kind") == TRACKS_KIND), None))
        output = io.BytesIO()
        manifest = {
            "format": "copal-obsidian-export-v1",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "workspace": scope["workspace_id"],
            "documents": [],
        }
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for doc in docs:
                export_name = _safe_export_name(doc)
                if canonical and doc.get("kind") == "planning":
                    export_name = ".copal/planning.legacy.json"
                if doc.get("kind") == "asset":
                    try:
                        path, _ = await _asset_file(request, scope, doc["id"])
                    except HTTPException as exc:
                        if exc.status_code != 404:
                            raise
                        continue
                    archive.write(path, export_name)
                else:
                    archive.writestr(export_name, str(doc.get("text") or ""))
                manifest["documents"].append({"id": doc["id"], "kind": doc["kind"], "path": export_name})
            archive.writestr(".copal/export-manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        output.seek(0)
        headers = {"Content-Disposition": 'attachment; filename="copal-obsidian-export.zip"'}
        return StreamingResponse(output, media_type="application/zip", headers=headers)

    from routes.copal_treehouse_routes import setup_treehouse_routes
    router.include_router(setup_treehouse_routes(call=_call, scope_for=_scope, publish=publish))

    return router

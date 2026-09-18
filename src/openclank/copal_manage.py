"""Semantic Copal mutation adapter used by the native ``manage_copal`` tool.

The adapter keeps the model away from route paths and storage internals. Each
operation reads the current owner/workspace record immediately before its one
canonical bridge write and returns a terminal conflict instead of replaying.
High-impact previews are explicit and persisted as owner/workspace-scoped
operation records so a later apply can survive a process restart.
"""

from __future__ import annotations

import copy
import asyncio
from datetime import UTC, datetime
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from src.openclank.copal_bridge import CopalBridge
from src.openclank.copal_bases import BaseDefinitionError, dump_base_definition, parse_base_definition, set_frontmatter_property
from src.openclank.copal_calendar_projection import reconcile_projection
from src.openclank.copal_planning import (
    EVENT_KIND,
    TRACKS_KIND,
    canonical_documents,
    event_from_document,
    merge_event,
    planning_projection,
    reparent_track,
    serialize_event,
    serialize_track_registry,
    track_registry_from_document,
    validate_event,
)
from src.openclank.copal_treehouse import (
    TREEHOUSE_COMMAND_TYPES,
    TreeHouseError,
    apply_legacy_migration,
    apply_treehouse_command,
    compute_treehouse_projections,
    new_treehouse_state,
    plan_legacy_migration,
    public_treehouse_snapshot,
    state_fingerprint,
    validate_treehouse_state,
)
from src.openclank.copal_transfer import (
    CopalTransferError,
    apply_import,
    create_export,
    preview_export,
    preview_import,
)
from src.openclank.copal_treehouse_repository import TreeHouseRepository, TreeHouseRepositoryError
from src.openclank.treehouse_field_guide import instantiate_field_guide
from src.openclank.history_client import HistoryClient, HistoryClientError
from src.constants import DATA_DIR


MANAGE_ACTIONS = (
    "notes.create", "notes.edit", "notes.patch_metadata", "notes.rename", "notes.checkpoint", "notes.restore_version.preview", "notes.restore_version.apply", "notes.trash", "notes.restore_trash",
    "wiki.create", "wiki.edit", "wiki.patch_metadata", "wiki.rename", "wiki.checkpoint", "wiki.restore_version.preview", "wiki.restore_version.apply", "wiki.trash", "wiki.restore_trash",
    "timeline.event.create", "timeline.event.update", "timeline.event.trash", "timeline.event.restore_trash",
    "timeline.track.create", "timeline.track.update", "timeline.track.reparent",
    "galaxy.link_event_track", "galaxy.unlink_event_track", "graph.link", "graph.unlink",
    "mind.heading.add", "mind.heading.rename", "mind.heading.move", "mind.heading.reparent", "mind.heading.delete",
    "todo.create", "todo.update", "todo.complete", "todo.trash",
    "bases.migrate.preview", "bases.migrate.apply", "bases.row.update",
    "treehouse.command", "treehouse.migrate.preview", "treehouse.migrate.apply",
    "maintenance.bulk_trash.preview", "maintenance.bulk_trash.apply",
    "maintenance.bulk_restore.preview", "maintenance.bulk_restore.apply",
    "maintenance.calendar_reconcile", "maintenance.import.preview", "maintenance.import.apply",
    "maintenance.export.preview", "maintenance.export.apply",
)
OPERATION_KIND = "copal-operation"
OPERATION_PREFIX = ".copal/operations/"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_WORKSPACE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PREVIEWS: dict[str, dict[str, Any]] = {}
_ACTION_FIELDS = {
    **{action: {"action", "workspace", "name", "content", "properties", "relations"} for action in ("notes.create", "wiki.create")},
    **{action: {"action", "workspace", "id", "content"} for action in ("notes.edit", "wiki.edit")},
    **{action: {"action", "workspace", "id", "patch"} for action in ("notes.patch_metadata", "wiki.patch_metadata", "notes.checkpoint", "wiki.checkpoint", "timeline.event.update", "timeline.track.update", "todo.update", "graph.link", "graph.unlink")},
    **{action: ({"action", "workspace", "id", "commitId"} | ({"previewToken"} if action.endswith(".apply") else set())) for action in ("notes.restore_version.preview", "notes.restore_version.apply", "wiki.restore_version.preview", "wiki.restore_version.apply")},
    **{action: {"action", "workspace", "id", "name"} for action in ("notes.rename", "wiki.rename")},
    **{action: {"action", "workspace", "id"} for action in ("notes.trash", "notes.restore_trash", "wiki.trash", "wiki.restore_trash", "timeline.event.trash", "timeline.event.restore_trash", "todo.complete", "todo.trash")},
    "timeline.event.create": {"action", "workspace", "event"}, "timeline.track.create": {"action", "workspace", "track"},
    "timeline.track.reparent": {"action", "workspace", "id", "parentTrackId"},
    "galaxy.link_event_track": {"action", "workspace", "id", "trackId"}, "galaxy.unlink_event_track": {"action", "workspace", "id", "trackId"},
    **{action: {"action", "workspace", "id", "patch"} for action in ("mind.heading.add", "mind.heading.rename", "mind.heading.move", "mind.heading.reparent", "mind.heading.delete")},
    "todo.create": {"action", "workspace", "event"},
    **{action: {"action", "workspace", "id", "previewToken"} for action in ("bases.migrate.preview", "bases.migrate.apply")},
    "bases.row.update": {"action", "workspace", "id", "patch"},
    "treehouse.command": {"action", "workspace", "command", "commandId", "expectedRevision"},
    "treehouse.migrate.preview": {"action", "workspace", "commandId", "expectedRevision"},
    "treehouse.migrate.apply": {"action", "workspace", "commandId", "expectedRevision", "previewToken"},
    **{action: {"action", "workspace", "ids", "previewToken"} for action in ("maintenance.bulk_trash.preview", "maintenance.bulk_trash.apply", "maintenance.bulk_restore.preview", "maintenance.bulk_restore.apply")},
    "maintenance.calendar_reconcile": {"action", "workspace"},
    "maintenance.import.preview": {"action", "workspace", "attachmentId", "corpus"},
    "maintenance.import.apply": {"action", "workspace", "attachmentId", "corpus", "previewToken"},
    "maintenance.export.preview": {"action", "workspace", "options"},
    "maintenance.export.apply": {"action", "workspace", "options", "previewToken"},
}


class CopalManageError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_request", detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


class _HistoryMutationBridge:
    """Capture Copal's canonical managed writes at the bridge mutation boundary."""

    _MUTATIONS = frozenset({
        "create", "write", "rename", "checkpoint", "delete", "restore_deleted", "restore",
        "move", "replace", "trash",
    })

    def __init__(self, bridge: Any, client: HistoryClient, *, owner: str, account_id: str, workspace: str, docs: dict[str, dict[str, Any]], actor_id: str | None = None, action_id: str | None = None):
        self._bridge = bridge
        self._client = client
        self._owner = owner
        self._account_id = account_id
        self._workspace = workspace
        self._docs = docs
        self._actor_id = str(actor_id or owner)
        self._action_id = str(action_id).strip() if action_id else None

    @staticmethod
    def _bytes(doc: dict[str, Any] | None) -> bytes | None:
        if not doc or doc.get("text") is None:
            return None
        # Preserve the complete managed envelope, including typed fields and
        # provider extensions.  A text-only history record cannot restore a
        # database note without silently dropping relations or attachments.
        return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")

    @staticmethod
    def _fingerprint(doc: dict[str, Any] | None) -> str:
        if not doc:
            return "absent"
        return str(doc.get("head") or "present")

    def _envelope(self, operation: str, args: dict[str, Any], resource_id: str, before: dict[str, Any] | None, action_id: str) -> dict[str, Any]:
        now = int(time.time() * 1000)
        revision = before.get("head") if before else None
        return {
            "schema_version": 1,
            "action_id": action_id,
            "actor_account_id": self._account_id,
            "resource_key": {
                "account_id": self._account_id,
                "workspace_id": self._workspace,
                "provider": "copal",
                "resource_id": resource_id,
            },
            "guard_resource_ids": [],
            "modified_resource_ids": [],
            "operation": operation,
            "expected_revision": {"Opaque": {"kind": "head", "value": str(revision)}} if revision else None,
            "actor_id": self._actor_id,
            "actor_kind": "agent",
            "session_id": None,
            "run_id": None,
            "task_id": None,
            "tool_id": "manage_copal",
            "before_revision": {"Opaque": {"kind": "head", "value": str(revision)}} if revision else None,
            "expected_after_revision": None,
            "original_locator": {"display_name": str(before.get("name") or resource_id), "location_label": self._workspace, "opaque_ref": None} if before else None,
            "destination_locator": {"display_name": str(args.get("name") or resource_id), "location_label": self._workspace, "opaque_ref": None},
            "timestamp_millis": now,
            "coverage": {"metadata": {"coverage_kind": "KnownMutationHooks", "roots": [self._workspace], "exclusions": []}},
            "per_resource_outcomes": None,
        }

    async def call(self, operation: str, args: dict[str, Any] | None = None, *, timeout: float = 20) -> Any:
        args = args or {}
        if operation not in self._MUTATIONS:
            return await self._bridge.call(operation, args, timeout=timeout)
        supplied_action_id = str(args.get("actionId") or args.get("operationId") or args.get("commandId") or "").strip()
        raw_id = str(args.get("id") or args.get("name") or args.get("path") or f"{operation}:{supplied_action_id or uuid.uuid4().hex}")
        action_id = supplied_action_id or self._action_id or f"manage-{uuid.uuid4().hex}"
        before = self._docs.get(str(args.get("id")))
        envelope = self._envelope(operation, args, raw_id, before, action_id)
        before_bytes = self._bytes(before)
        history_status: dict[str, Any] = {
            "status": "paused",
            "durable": False,
            "action_id": envelope["action_id"],
            "phase": "before",
        }
        prepared = False
        try:
            await asyncio.to_thread(self._client.prepare, envelope, content=before_bytes, fingerprint=self._fingerprint(before))
            prepared = True
            history_status.update(status="prepared", durable=False)
        except Exception as exc:
            # A history worker outage must not turn a valid live Copal save into
            # an unavailable save. Unix transport errors and worker protocol
            # errors are both reported as an explicit best-effort status.
            paused = "history_paused_budget" in str(exc)
            history_status.update(
                status="paused" if paused else "failed",
                phase="budget" if paused else "before",
                error=str(exc),
            )
        try:
            result = await self._bridge.call(operation, args, timeout=timeout)
        except Exception:
            # Preserve the prepared action as an explicit non-commit when the
            # live provider fails or the bridge connection is interrupted.
            # The original provider exception remains authoritative.
            if prepared:
                try:
                    await asyncio.to_thread(
                        self._client.record_live,
                        envelope["action_id"],
                        {
                            "action_id": envelope["action_id"],
                            "status": "NotCommitted",
                            "fingerprint": None,
                            "after_unavailable": True,
                            "resource_id": raw_id,
                        },
                    )
                except Exception:
                    pass
            raise
        result = result if isinstance(result, dict) else {}
        stale = result.get("outcome") == "stale"
        status = "Conflict" if stale else ("Committed" if result.get("outcome") in {"committed", "created", "restored", "deleted", None} else "Unknown")
        after_doc = result.get("doc") if isinstance(result.get("doc"), dict) else None
        if prepared:
            receipt = {"action_id": envelope["action_id"], "status": status, "fingerprint": self._fingerprint(after_doc), "resource_id": after_doc.get("id") if isinstance(after_doc, dict) else raw_id}
            try:
                await asyncio.to_thread(self._client.record_live, envelope["action_id"], receipt)
                if status == "Committed":
                    # Creates initially use the caller's name as a locator. Bind
                    # the durable action to the provider-issued immutable id
                    # before completing it so restore cannot target a rename or
                    # a later resource that reused that name.
                    if operation == "create" and isinstance(after_doc, dict) and after_doc.get("id"):
                        rebind = getattr(self._client, "rebind_resource", None)
                        if callable(rebind):
                            await asyncio.to_thread(rebind, envelope["action_id"], str(after_doc["id"]))
                    await asyncio.to_thread(self._client.complete, envelope["action_id"], content=self._bytes(after_doc), fingerprint=self._fingerprint(after_doc))
                    history_status.update(status="complete", durable=True, phase="complete")
                else:
                    history_status.update(status="failed", phase="live_conflict")
            except Exception as exc:
                paused = "history_paused_budget" in str(exc)
                history_status.update(
                    status="paused" if paused else "failed",
                    phase="budget" if paused else "after",
                    error=str(exc),
                )
        result.setdefault("history", history_status)
        result.setdefault("action_id", action_id)
        return result


def _configured_history_client(owner: str, account_id: str | None, actor_id: str | None = None) -> HistoryClient | None:
    socket_path = os.environ.get("OPENCLANK_HISTORY_SOCKET", "").strip()
    if not socket_path:
        return None
    account = str(account_id or ("local-installation" if owner in {"", "local"} else owner))
    return HistoryClient(
        socket_path,
        actor_id=actor_id or owner or "local-installation",
        account_id=account,
    )


def _id(value: Any, field: str = "id") -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise CopalManageError(f"{field} must be a stable Copal ID", code="invalid_id")
    return value


def _workspace(value: Any) -> str:
    value = str(value or "default").strip()
    if not _WORKSPACE.fullmatch(value):
        raise CopalManageError("workspace must be a logical Copal workspace ID", code="invalid_workspace")
    return value


def _strict(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise CopalManageError("manage_copal arguments must be an object")
    action = arguments.get("action")
    if action not in MANAGE_ACTIONS:
        raise CopalManageError("action is not a supported Copal mutation", code="invalid_action")
    allowed = {"action", "workspace", "id", "name", "kind", "content", "properties", "relations", "patch", "edits", "event", "track", "parentTrackId", "trackId", "commitId", "previewToken", "ids", "query", "command", "payload", "commandId", "actionId", "operationId", "expectedRevision", "attachmentId", "corpus", "options"}
    extras = set(arguments) - allowed
    if extras:
        raise CopalManageError(f"unknown manage_copal field(s): {', '.join(sorted(extras))}", code="extra_field")
    value = {**arguments, "workspace": _workspace(arguments.get("workspace"))}
    action_extras = set(value) - (_ACTION_FIELDS[action] | {"actionId", "operationId"})
    if action_extras:
        raise CopalManageError(f"{action} does not accept field(s): {', '.join(sorted(action_extras))}", code="extra_field")
    return value


async def _docs(bridge: Any, owner: str, workspace: str, *, deleted: bool = False) -> list[dict[str, Any]]:
    if deleted:
        result = await bridge.call("trash", {"owner": owner or "local", "workspace_id": workspace, "corpus": "notes"}, timeout=60)
    else:
        result = await bridge.call("index", {"owner": owner or "local", "workspace_id": workspace, "query": ""}, timeout=60)
    return [doc for doc in result.get("docs") or [] if isinstance(doc, dict) and doc.get("kind") != OPERATION_KIND]


async def _operation_docs(bridge: Any, owner: str, workspace: str) -> list[dict[str, Any]]:
    result = await bridge.call("index", {"owner": owner or "local", "workspace_id": workspace, "kind": OPERATION_KIND, "query": ""}, timeout=60)
    return [doc for doc in result.get("docs") or [] if isinstance(doc, dict) and doc.get("kind") == OPERATION_KIND]


async def _deleted_docs(bridge: Any, owner: str, workspace: str) -> list[dict[str, Any]]:
    """Read both native stores because trash restore is corpus-sensitive."""
    result = []
    for corpus in ("notes", "wiki"):
        value = await bridge.call("trash", {"owner": owner or "local", "workspace_id": workspace, "corpus": corpus}, timeout=60)
        result.extend(doc for doc in value.get("docs") or [] if isinstance(doc, dict) and doc.get("kind") != OPERATION_KIND)
    return result


async def _treehouse_state(bridge: Any, owner: str, workspace: str, *, initialize: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the single TreeHouse aggregate through the same bridge boundary as routes."""
    docs = [
        doc for doc in await _docs(bridge, owner, workspace)
        if doc.get("kind") == "treehouse-state" and doc.get("name") == ".copal/treehouse-state.json"
    ]
    if len(docs) > 1:
        raise CopalManageError("multiple TreeHouse state documents exist", code="conflict")
    if not docs:
        if not initialize:
            return {"id": None, "head": None, "kind": "treehouse-state"}, new_treehouse_state(owner)
        initial = new_treehouse_state(owner)
        try:
            created = await bridge.call(
                "create",
                {
                    "owner": owner,
                    "workspace_id": workspace,
                    "name": ".copal/treehouse-state.json",
                    "kind": "treehouse-state",
                    "content": json.dumps(initial, separators=(",", ":"), ensure_ascii=False),
                },
                timeout=60,
            )
            document_id = (created or {}).get("doc", {}).get("id")
        except Exception:
            document_id = None
        if document_id:
            doc = await bridge.call("get", {"owner": owner, "workspace_id": workspace, "id": document_id}, timeout=60)
        else:
            refreshed = [
                item for item in await _docs(bridge, owner, workspace)
                if item.get("kind") == "treehouse-state" and item.get("name") == ".copal/treehouse-state.json"
            ]
            if len(refreshed) != 1:
                raise CopalManageError("TreeHouse state could not be initialized", code="initialization_failed")
            doc = refreshed[0]
    else:
        doc = docs[0]
    try:
        state = json.loads(str(doc.get("text") or "{}"))
        validate_treehouse_state(state)
    except (json.JSONDecodeError, TreeHouseError) as exc:
        raise CopalManageError("TreeHouse state is corrupt", code="corrupt_state") from exc
    return doc, state


async def _write_treehouse(bridge: Any, owner: str, workspace: str, doc: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    content = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
    if len(content.encode()) > 8_388_608:
        raise CopalManageError("TreeHouse state exceeds the document safety limit", code="state_too_large")
    result = await bridge.call(
        "write",
        {"owner": owner, "workspace_id": workspace, "id": doc["id"], "content": content, "base": doc.get("head"), "corpus": "notes"},
        timeout=60,
    )
    if (result or {}).get("outcome") == "stale":
        raise CopalManageError("TreeHouse changed before this write; refresh and retry explicitly", code="stale")
    return result


def _base_doc(docs: list[dict[str, Any]], document_id: str) -> dict[str, Any]:
    doc = next((item for item in docs if str(item.get("id")) == document_id), None)
    if not doc or doc.get("kind") != "base":
        raise CopalManageError("Base definition not found in this owner/workspace", code="not_found")
    return doc


def _result(action: str, workspace: str, *, doc: dict[str, Any] | None = None, saved: bool = True, data: Any = None, warnings: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "action": action, "workspace": workspace, "saved": saved, "resourceKind": doc.get("kind") if doc else None, "resourceId": doc.get("id") if doc else None, "head": doc.get("head") if doc else None, "openUrl": f"/copal/{action.split('.', 1)[0]}" + (f"?doc={doc['id']}" if doc and doc.get("id") else ""), "warnings": warnings or [], "data": data, **extra}


async def _preview(bridge: Any, action: str, args: dict[str, Any], owner: str, workspace: str, data: Any) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    normalized = {key: value for key, value in args.items() if key != "previewToken"}
    normalized["action"] = action.removesuffix(".preview") + ".apply"
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    apply_action = action.removesuffix(".preview") + ".apply"
    expires = time.time() + 300
    record = {"schemaVersion": 1, "operationId": token, "action": apply_action, "owner": owner, "workspace": workspace, "hash": hashlib.sha256(payload.encode()).hexdigest(), "expires": expires, "data": data, "state": "previewed"}
    created = await bridge.call(
        "create",
        {"owner": owner, "workspace_id": workspace, "name": f"{OPERATION_PREFIX}{token}.json", "kind": OPERATION_KIND, "corpus": "notes", "content": json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)},
        timeout=60,
    )
    _PREVIEWS[token] = record
    return _result(action, workspace, saved=False, data=data, previewToken=token, previewExpiresAt=expires, preview=True, operationId=(created.get("doc") or {}).get("id"))


async def _consume_preview(bridge: Any, action: str, args: dict[str, Any], owner: str, workspace: str) -> dict[str, Any]:
    token = str(args.get("previewToken") or "")
    record = None
    operation_doc = None
    for candidate in await _operation_docs(bridge, owner, workspace):
        try:
            candidate_record = json.loads(str(candidate.get("text") or "{}"))
        except json.JSONDecodeError:
            continue
        if candidate_record.get("operationId") == token:
            record, operation_doc = candidate_record, candidate
            break
    if not record or record.get("state") != "previewed" or record.get("expires", 0) < time.time() or record.get("action") != action or record.get("owner") != owner or record.get("workspace") != workspace:
        raise CopalManageError("preview token is missing, expired, or scoped to another request", code="invalid_preview")
    normalized = {key: value for key, value in args.items() if key != "previewToken"}
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    if hashlib.sha256(payload.encode()).hexdigest() != record.get("hash"):
        raise CopalManageError("preview payload changed", code="invalid_preview")
    record["state"] = "consumed"
    if operation_doc:
        updated = await bridge.call(
            "write",
            {"owner": owner, "workspace_id": workspace, "id": operation_doc.get("id"), "content": json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False), "base": operation_doc.get("head"), "corpus": "notes"},
            timeout=60,
        )
        if (updated or {}).get("outcome") == "stale":
            raise CopalManageError("preview operation changed before apply; request a fresh preview", code="stale_preview")
    _PREVIEWS.pop(token, None)
    return record


def _note_previous(doc: dict[str, Any]) -> dict[str, Any]:
    """Build the stable subset the canonical note encoder needs.

    ``index`` intentionally exposes a projection rather than raw storage. The
    projected blocks/property definitions are sufficient to preserve IDs while
    the bridge remains the only writer.
    """
    return {
        "body": {"type": "doc", "blocks": copy.deepcopy(doc.get("blocks") or [])},
        "properties": copy.deepcopy(doc.get("propertyDefinitions") or []),
        "relations": copy.deepcopy(doc.get("relations") or []),
        "extensions": copy.deepcopy(doc.get("extensions") or {}),
    }


def _encode_note_content(doc: dict[str, Any] | None, body: str, *, properties: dict[str, Any] | None = None, relations: list[dict[str, Any]] | None = None) -> str:
    """Encode a Copal database note/wiki without downgrading it to Markdown."""
    from routes.copal_routes import _encode_note

    previous = _note_previous(doc or {}) if doc and doc.get("format") == "copal-note-v1" else None
    return _encode_note(
        body,
        properties if properties is not None else (doc or {}).get("properties") or {},
        relations if relations is not None else (doc or {}).get("relations") or [],
        previous=previous,
    )


def _patched_note_content(doc: dict[str, Any], patch: dict[str, Any]) -> str:
    if not isinstance(patch, dict):
        raise CopalManageError("metadata patch must be an object", code="invalid_patch")
    allowed = {"properties", "removeProperties", "relations", "tags"}
    extras = set(patch) - allowed
    if extras:
        raise CopalManageError(f"metadata patch does not accept field(s): {', '.join(sorted(extras))}", code="extra_field")
    properties = copy.deepcopy(doc.get("properties") or {})
    values = patch.get("properties", {})
    if values is not None and not isinstance(values, dict):
        raise CopalManageError("patch.properties must be an object", code="invalid_patch")
    properties.update(copy.deepcopy(values or {}))
    removed = patch.get("removeProperties", [])
    if not isinstance(removed, list) or any(not isinstance(item, str) or not item.strip() for item in removed):
        raise CopalManageError("patch.removeProperties must be a list of property names", code="invalid_patch")
    for key in removed:
        properties.pop(key, None)
    relations = copy.deepcopy(doc.get("relations") or [])
    if "relations" in patch:
        if not isinstance(patch["relations"], list) or any(not isinstance(item, dict) for item in patch["relations"]):
            raise CopalManageError("patch.relations must be a list of relation objects", code="invalid_patch")
        relations = copy.deepcopy(patch["relations"])
    encoded = _encode_note_content(doc, str(doc.get("text") or ""), properties=properties, relations=relations)
    if "tags" in patch:
        tags = patch["tags"]
        if not isinstance(tags, list) or any(not isinstance(item, str) or not item.strip() for item in tags):
            raise CopalManageError("patch.tags must be a list of strings", code="invalid_patch")
        record = json.loads(encoded)
        record["tags"] = list(dict.fromkeys(item.strip().lstrip("#") for item in tags))
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return encoded


def _task_line(doc: dict[str, Any], task_id: str) -> tuple[int, dict[str, Any]]:
    tasks = [task for task in doc.get("tasks") or [] if isinstance(task, dict) and str(task.get("id")) == task_id]
    if len(tasks) != 1:
        raise CopalManageError("todo target must identify exactly one note task", code="not_found" if not tasks else "ambiguous_target")
    line = int(tasks[0].get("line") or 0)
    if line < 1:
        raise CopalManageError("todo target has no stable source line", code="invalid_target")
    return line, tasks[0]


def _heading_entries(text: str) -> list[dict[str, Any]]:
    entries = []
    for line, value in enumerate(str(text or "").splitlines(), 1):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", value)
        if match:
            entries.append({"line": line, "level": len(match.group(1)), "text": match.group(2).strip()})
    return entries


def _heading_path(entries: list[dict[str, Any]], index: int) -> list[str]:
    stack: list[dict[str, Any]] = []
    for entry in entries[:index + 1]:
        while stack and stack[-1]["level"] >= entry["level"]:
            stack.pop()
        stack.append(entry)
    return [item["text"] for item in stack]


def _resolve_heading(text: str, raw_path: Any) -> tuple[list[str], dict[str, Any], int]:
    path = [str(item).strip() for item in (raw_path if isinstance(raw_path, list) else [raw_path]) if str(item).strip()]
    if not path:
        raise CopalManageError("heading patch requires a non-empty structural path", code="missing_field")
    entries = _heading_entries(text)
    matches = [(index, entry) for index, entry in enumerate(entries) if _heading_path(entries, index) == path]
    if len(matches) != 1:
        raise CopalManageError("heading path must identify exactly one heading", code="ambiguous_target" if matches else "not_found")
    index, entry = matches[0]
    return path, entry, index


def _heading_section(entries: list[dict[str, Any]], index: int, line_count: int) -> tuple[int, int]:
    start = entries[index]["line"] - 1
    end = line_count
    for next_entry in entries[index + 1:]:
        if next_entry["level"] <= entries[index]["level"]:
            end = next_entry["line"] - 1
            break
    return start, end


def _mind_transform(text: str, action: str, patch: dict[str, Any]) -> str:
    if not isinstance(patch, dict):
        raise CopalManageError("Mind heading patch must be an object", code="invalid_patch")
    source = str(text or "")
    lines = source.splitlines()
    entries = _heading_entries(source)
    if action == "mind.heading.add":
        parent_path = patch.get("parentPath")
        title = str(patch.get("title") or "").strip()
        if not title:
            raise CopalManageError("heading.add requires title", code="missing_field")
        if parent_path in (None, [], ""):
            level = 1
            insert_at = len(lines)
        else:
            _, parent, parent_index = _resolve_heading(source, parent_path)
            level = min(6, parent["level"] + 1)
            _, insert_at = _heading_section(entries, parent_index, len(lines))
        lines[insert_at:insert_at] = [f"{'#' * level} {title}"]
        return "\n".join(lines)
    _, entry, index = _resolve_heading(source, patch.get("path"))
    start, end = _heading_section(entries, index, len(lines))
    if action == "mind.heading.rename":
        title = str(patch.get("title") or "").strip()
        if not title:
            raise CopalManageError("heading.rename requires title", code="missing_field")
        lines[entry["line"] - 1] = f"{'#' * entry['level']} {title}"
    elif action == "mind.heading.reparent":
        level = patch.get("newLevel")
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 6:
            raise CopalManageError("heading.reparent requires newLevel 1..6", code="invalid_patch")
        lines[entry["line"] - 1] = f"{'#' * level} {entry['text']}"
    elif action == "mind.heading.delete":
        del lines[start:end]
    elif action == "mind.heading.move":
        direction = patch.get("direction")
        if direction not in (-1, 1, "up", "down"):
            raise CopalManageError("heading.move direction must be up or down", code="invalid_patch")
        direction = -1 if direction in {-1, "up"} else 1
        target_index = index - 1 if direction < 0 else index + 1
        while 0 <= target_index < len(entries) and entries[target_index]["level"] > entry["level"]:
            target_index += -1 if direction < 0 else 1
        if target_index < 0 or target_index >= len(entries):
            return source
        target_start, target_end = _heading_section(entries, target_index, len(lines))
        block = lines[start:end]
        remainder = lines[:start] + lines[end:]
        if direction < 0:
            insertion = target_start
        else:
            insertion = target_end - (end - start)
        remainder[insertion:insertion] = block
        return "\n".join(remainder)
    else:
        raise CopalManageError("unsupported Mind heading action", code="invalid_action")
    return "\n".join(lines)


async def manage_copal(
    arguments: Any,
    *,
    owner: str | None = None,
    account_id: str | None = None,
    bridge: Any | None = None,
    history_client: Any | None = None,
    actor_id: str | None = None,
) -> dict[str, Any]:
    args = _strict(arguments)
    action, workspace, owner = args["action"], args["workspace"], str(owner or "local")
    # An explicitly supplied bridge is the caller's storage boundary (and is
    # used by local/fixture integrations).  The production adapter creates its
    # own bridge and therefore uses the account repository below.  Keeping the
    # distinction here prevents a fixture bridge from accidentally reading the
    # process-wide repository while still routing real agent calls through the
    # shared account access predicate.
    supplied_bridge = bridge is not None
    bridge = bridge or CopalBridge()
    # Repository-backed TreeHouse commands carry their own account/CAS
    # boundary and must remain usable when the document bridge is unavailable.
    # Other Copal mutations still load their bridge documents here.
    docs = [] if action == "treehouse.command" and not supplied_bridge else await _docs(bridge, owner, workspace)
    by_id = {str(doc.get("id")): doc for doc in docs if doc.get("id")}
    history_client = history_client or _configured_history_client(owner, account_id, actor_id)
    if history_client is not None:
        bridge = _HistoryMutationBridge(
            bridge,
            history_client,
            owner=owner,
            account_id=str(account_id or ("local-installation" if owner == "local" else owner)),
            workspace=workspace,
            docs=by_id,
            actor_id=actor_id,
            action_id=str(args.get("actionId") or args.get("operationId") or args.get("commandId") or "").strip() or None,
        )

    def transfer_error(exc: CopalTransferError) -> CopalManageError:
        return CopalManageError(str(exc), code=exc.code, detail=exc.detail)

    if action.endswith(".preview"):
        if action.startswith("maintenance.bulk_"):
            ids = args.get("ids")
            if not isinstance(ids, list) or not ids or any(not _ID.fullmatch(str(item)) for item in ids):
                raise CopalManageError("bulk maintenance preview requires stable ids", code="missing_ids")
            return await _preview(bridge, action, args, owner, workspace, {"ids": list(dict.fromkeys(map(str, ids))), "count": len(set(map(str, ids)))})
        if action.endswith("restore_version.preview"):
            document_id = _id(args.get("id")); doc = by_id.get(document_id)
            if not doc:
                raise CopalManageError("document not found in this owner/workspace", code="not_found")
            corpus = "wiki" if doc.get("kind") == "wiki" else "notes"
            history = await bridge.call("history", {"owner": owner, "workspace_id": workspace, "id": document_id, "corpus": corpus}, timeout=60)
            commits = history.get("commits") if isinstance(history, dict) else history
            commit_id = str(args.get("commitId") or "").strip()
            if not commit_id:
                raise CopalManageError("restore preview requires a commitId", code="missing_field")
            data = {"id": document_id, "commitId": commit_id, "sourceHead": doc.get("head"), "history": history}
            return await _preview(bridge, action, args, owner, workspace, data)
        if action == "bases.migrate.preview":
            document_id = _id(args.get("id"))
            doc = _base_doc(docs, document_id)
            try:
                definition, diagnostics = parse_base_definition(str(doc.get("text") or ""))
            except BaseDefinitionError as exc:
                raise CopalManageError("Base definition is invalid", code="invalid_definition", detail={"diagnostics": exc.diagnostics}) from exc
            canonical = dump_base_definition(definition)
            return await _preview(
                bridge,
                action,
                args,
                owner,
                workspace,
                {"id": document_id, "sourceHead": doc.get("head"), "changed": canonical != doc.get("text"), "canonical": canonical, "diagnostics": diagnostics},
            )
        if action == "treehouse.migrate.preview":
            doc, state = await _treehouse_state(bridge, owner, workspace, initialize=True)
            plan = plan_legacy_migration(docs, state)
            return await _preview(
                bridge,
                action,
                args,
                owner,
                workspace,
                {"sourceHead": doc.get("head"), "revision": state.get("revision"), "plan": plan, "commandId": str(args.get("commandId") or uuid.uuid4().hex)},
            )
        if action == "maintenance.import.preview":
            corpus = str(args.get("corpus") or "notes")
            if corpus not in {"notes", "wiki"}:
                raise CopalManageError("import corpus must be notes or wiki", code="invalid_corpus")
            try:
                data = preview_import(args.get("attachmentId"), owner, workspace, corpus)
            except CopalTransferError as exc:
                raise transfer_error(exc) from exc
            args["corpus"] = corpus
            return await _preview(bridge, action, args, owner, workspace, data)
        if action == "maintenance.export.preview":
            options = args.get("options") or {}
            if not isinstance(options, dict) or set(options) - {"includeWiki", "includeAssets"} or any(not isinstance(value, bool) for value in options.values()):
                raise CopalManageError("export options only accept includeWiki/includeAssets", code="invalid_options")
            try:
                snapshot = await bridge.call("export_snapshot", {"owner": owner, "workspace_id": workspace}, timeout=60)
                data = preview_export(snapshot, options)
            except CopalTransferError as exc:
                raise transfer_error(exc) from exc
            args["options"] = options
            return await _preview(bridge, action, args, owner, workspace, data)
        raise CopalManageError("unsupported preview action", code="invalid_action")
    if action.endswith(".apply") and action.startswith("maintenance.bulk_"):
        await _consume_preview(bridge, action, args, owner, workspace)
        ids = list(dict.fromkeys(map(str, args.get("ids") or [])))
        changed = []
        for document_id in ids:
            if action == "maintenance.bulk_trash.apply":
                if document_id not in by_id:
                    continue
                await bridge.call("delete", {"owner": owner, "workspace_id": workspace, "id": document_id, "corpus": "wiki" if by_id[document_id].get("kind") == "wiki" else "notes"}, timeout=30)
                changed.append(document_id)
            else:
                deleted = next((item for item in await _deleted_docs(bridge, owner, workspace) if str(item.get("id")) == document_id), None)
                await bridge.call("restore_deleted", {"owner": owner, "workspace_id": workspace, "id": document_id, "corpus": "wiki" if deleted and deleted.get("kind") == "wiki" else "notes"}, timeout=30)
                changed.append(document_id)
        return _result(action, workspace, data={"ids": changed, "count": len(changed)})

    if action == "maintenance.import.apply":
        corpus = str(args.get("corpus") or "notes")
        args["corpus"] = corpus
        record = await _consume_preview(bridge, action, args, owner, workspace)
        data = record.get("data") or {}
        if data.get("attachmentId") != str(args.get("attachmentId") or "") or data.get("corpus") != corpus:
            raise CopalManageError("import request changed after preview", code="invalid_preview")
        try:
            current = preview_import(args.get("attachmentId"), owner, workspace, corpus)
            if current.get("sourceHash") != data.get("sourceHash"):
                raise CopalManageError("attachment changed after preview; request a fresh preview", code="stale_preview")
            result = await apply_import(bridge, attachment_id=args.get("attachmentId"), owner=owner, workspace=workspace, corpus=corpus)
        except CopalTransferError as exc:
            raise transfer_error(exc) from exc
        return _result(action, workspace, saved=True, data=result)

    if action == "maintenance.export.apply":
        options = args.get("options") or {}
        if not isinstance(options, dict) or set(options) - {"includeWiki", "includeAssets"} or any(not isinstance(value, bool) for value in options.values()):
            raise CopalManageError("export options only accept includeWiki/includeAssets", code="invalid_options")
        args["options"] = options
        record = await _consume_preview(bridge, action, args, owner, workspace)
        snapshot = await bridge.call("export_snapshot", {"owner": owner, "workspace_id": workspace}, timeout=60)
        try:
            current = preview_export(snapshot, options)
        except CopalTransferError as exc:
            raise transfer_error(exc) from exc
        if current.get("sourceHash") != (record.get("data") or {}).get("sourceHash"):
            raise CopalManageError("Copal changed after export preview; request a fresh preview", code="stale_preview")
        try:
            result = await create_export(bridge, snapshot=snapshot, owner=owner, workspace=workspace, options=options)
        except CopalTransferError as exc:
            raise transfer_error(exc) from exc
        return _result(action, workspace, saved=False, data=result)

    if action.endswith("restore_version.apply"):
        record = await _consume_preview(bridge, action, args, owner, workspace)
        document_id = _id(args.get("id")); doc = by_id.get(document_id)
        if not doc or record.get("data", {}).get("id") != document_id:
            raise CopalManageError("document not found in this owner/workspace", code="not_found")
        if record.get("data", {}).get("sourceHead") != doc.get("head"):
            raise CopalManageError("document changed after preview; refresh and preview again", code="stale_preview")
        corpus = "wiki" if doc.get("kind") == "wiki" else "notes"
        result = await bridge.call("restore", {"owner": owner, "workspace_id": workspace, "id": document_id, "commit": record["data"]["commitId"], "corpus": corpus}, timeout=60)
        return _result(action, workspace, doc=result.get("doc") or doc, data=result)

    if action == "bases.migrate.apply":
        record = await _consume_preview(bridge, action, args, owner, workspace)
        data = record.get("data") or {}
        doc = _base_doc(docs, _id(args.get("id")))
        if data.get("sourceHead") != doc.get("head"):
            raise CopalManageError("Base changed after preview; refresh and preview again", code="stale_preview")
        if not data.get("changed"):
            return _result(action, workspace, doc=doc, data=data, warnings=["Base is already canonical"])
        result = await bridge.call(
            "write",
            {"owner": owner, "workspace_id": workspace, "id": doc["id"], "content": data["canonical"], "base": doc.get("head"), "corpus": "notes"},
            timeout=60,
        )
        if (result or {}).get("outcome") == "stale":
            raise CopalManageError("Base changed before this write; refresh and retry explicitly", code="stale")
        return _result(action, workspace, doc=result.get("doc") or doc, data=data)

    if action == "treehouse.migrate.apply":
        record = await _consume_preview(bridge, action, args, owner, workspace)
        data = record.get("data") or {}
        doc, state = await _treehouse_state(bridge, owner, workspace, initialize=True)
        if data.get("sourceHead") != doc.get("head") or int(data.get("revision", -1)) != int(state.get("revision", -2)):
            raise CopalManageError("TreeHouse changed after preview; refresh and preview again", code="stale_preview")
        try:
            next_state, migration_result, changed = apply_legacy_migration(
                state,
                data.get("plan") or {},
                actor_id="owner",
                command_id=_id(data.get("commandId"), "commandId"),
                expected_revision=state.get("revision"),
            )
        except TreeHouseError as exc:
            raise CopalManageError(str(exc), code=exc.code, detail=exc.details) from exc
        result = await _write_treehouse(bridge, owner, workspace, doc, next_state) if changed else {"doc": doc, "outcome": "unchanged"}
        return _result(action, workspace, doc=result.get("doc") or doc, data={"result": migration_result, "plan": data.get("plan"), "fingerprint": state_fingerprint(next_state)})

    if action in {"notes.create", "wiki.create"}:
        name = str(args.get("name") or "").strip()
        if not name or not isinstance(args.get("content", ""), str):
            raise CopalManageError("create requires name and string content", code="missing_field")
        kind = "wiki" if action.startswith("wiki") else "note"
        content = _encode_note_content(None, args.get("content", ""), properties=args.get("properties") or {}, relations=args.get("relations") or [])
        created = await bridge.call("create", {"owner": owner, "workspace_id": workspace, "name": name, "kind": kind, "corpus": "wiki" if kind == "wiki" else "notes", "content": content}, timeout=60)
        return _result(action, workspace, doc=created.get("doc") or {}, data=created)

    if action in {"notes.edit", "wiki.edit", "notes.patch_metadata", "wiki.patch_metadata", "notes.rename", "wiki.rename", "notes.checkpoint", "wiki.checkpoint", "notes.trash", "wiki.trash", "notes.restore_trash", "wiki.restore_trash"}:
        document_id = _id(args.get("id"))
        doc = by_id.get(document_id)
        restore = action.endswith("restore_trash")
        if restore:
            deleted_docs = await _deleted_docs(bridge, owner, workspace)
            doc = next((item for item in deleted_docs if item.get("id") == document_id), None)
        if not doc:
            raise CopalManageError("document not found in this owner/workspace", code="not_found")
        if action.endswith(".edit"):
            content = args.get("content")
            if not isinstance(content, str):
                raise CopalManageError("edit requires string content", code="missing_field")
            if doc.get("kind") in {"note", "wiki"}:
                content = _encode_note_content(doc, content)
            result = await bridge.call("write", {"owner": owner, "workspace_id": workspace, "id": document_id, "content": content, "base": doc.get("head"), "corpus": "wiki" if doc.get("kind") == "wiki" else "notes"}, timeout=60)
        elif action.endswith("patch_metadata"):
            content = _patched_note_content(doc, args.get("patch") or {}) if doc.get("kind") in {"note", "wiki"} else None
            if content is None:
                raise CopalManageError("metadata patches require a structured Copal note or wiki", code="unsupported_kind")
            result = await bridge.call("write", {"owner": owner, "workspace_id": workspace, "id": document_id, "content": content, "base": doc.get("head"), "corpus": "wiki" if doc.get("kind") == "wiki" else "notes"}, timeout=60)
        elif action.endswith("rename"):
            result = await bridge.call("rename", {"owner": owner, "workspace_id": workspace, "id": document_id, "name": str(args.get("name") or ""), "corpus": "wiki" if doc.get("kind") == "wiki" else "notes"}, timeout=30)
        elif action.endswith("checkpoint"):
            result = await bridge.call("checkpoint", {"owner": owner, "workspace_id": workspace, "id": document_id, "message": args.get("patch", {}).get("message") if isinstance(args.get("patch"), dict) else None, "corpus": "wiki" if doc.get("kind") == "wiki" else "notes"}, timeout=30)
        elif action.endswith("trash"):
            result = await bridge.call("delete", {"owner": owner, "workspace_id": workspace, "id": document_id, "corpus": "wiki" if doc.get("kind") == "wiki" else "notes"}, timeout=30)
        else:
            result = await bridge.call("restore_deleted", {"owner": owner, "workspace_id": workspace, "id": document_id, "corpus": "wiki" if doc.get("kind") == "wiki" else "notes"}, timeout=30)
        if (result or {}).get("outcome") == "stale":
            raise CopalManageError("Copal changed before this write; refresh and retry explicitly", code="stale", detail={"head": (result.get("doc") or {}).get("head")})
        current = result.get("doc") if isinstance(result, dict) else None
        return _result(action, workspace, doc=current or doc, data=result)

    if action in {"timeline.event.create", "timeline.event.update", "timeline.event.trash", "timeline.event.restore_trash"}:
        if action.endswith("create"):
            event = dict(args.get("event") or {})
            validate_event(event)
            name = f".copal/events/{event['id']}.json"
            created = await bridge.call("create", {"owner": owner, "workspace_id": workspace, "name": name, "kind": EVENT_KIND, "content": serialize_event(event)}, timeout=60)
            return _result(action, workspace, doc=created.get("doc") or {}, data=created)
        document_id = _id(args.get("id"))
        doc = by_id.get(document_id)
        if not doc or not event_from_document(doc):
            raise CopalManageError("Timeline event not found", code="not_found")
        if action.endswith("update"):
            event = merge_event(event_from_document(doc), dict(args.get("patch") or {}), ())
            validate_event(event)
            result = await bridge.call("write", {"owner": owner, "workspace_id": workspace, "id": document_id, "content": serialize_event(event), "base": doc.get("head"), "corpus": "notes"}, timeout=60)
        elif action.endswith("trash"):
            result = await bridge.call("delete", {"owner": owner, "workspace_id": workspace, "id": document_id, "corpus": "notes"}, timeout=30)
        else:
            result = await bridge.call("restore_deleted", {"owner": owner, "workspace_id": workspace, "id": document_id, "corpus": "notes"}, timeout=30)
        if result.get("outcome") == "stale":
            raise CopalManageError("Timeline event is stale; no replay was attempted", code="stale")
        return _result(action, workspace, doc=result.get("doc") or doc, data=result)

    if action in {"timeline.track.reparent", "timeline.track.update", "timeline.track.create"}:
        registry_doc = next((doc for doc in docs if doc.get("kind") == TRACKS_KIND), None)
        if not registry_doc:
            raise CopalManageError("Timeline track registry not found", code="not_found")
        registry = track_registry_from_document(registry_doc)
        if action == "timeline.track.create":
            track = dict(args.get("track") or {})
            track.setdefault("parentTrackId", None)
            registry["tracks"].append(track)
        elif action == "timeline.track.reparent":
            registry["tracks"] = reparent_track(registry.get("tracks") or [], _id(args.get("id")), args.get("parentTrackId"))
        else:
            track_id = _id(args.get("id")); patch = dict(args.get("patch") or {})
            target = next((item for item in registry["tracks"] if item.get("id") == track_id), None)
            if not target: raise CopalManageError("Timeline track not found", code="not_found")
            target.update(patch)
        content = serialize_track_registry(registry.get("tracks") or [], {key: value for key, value in registry.items() if key not in {"schemaVersion", "tracks"}})
        result = await bridge.call("write", {"owner": owner, "workspace_id": workspace, "id": registry_doc["id"], "content": content, "base": registry_doc.get("head"), "corpus": "notes"}, timeout=60)
        if result.get("outcome") == "stale": raise CopalManageError("Track registry is stale; no replay was attempted", code="stale")
        return _result(action, workspace, doc=result.get("doc") or registry_doc, data={"registry": registry})

    if action in {"galaxy.link_event_track", "galaxy.unlink_event_track"}:
        event_id = _id(args.get("id")); track_id = _id(args.get("trackId")); doc = by_id.get(event_id)
        if not doc or not event_from_document(doc): raise CopalManageError("Galaxy event not found", code="not_found")
        event = event_from_document(doc); shared = list(event.get("sharedTrackIds") or [])
        if action.endswith("link_event_track") and track_id not in shared: shared.append(track_id)
        if action.endswith("unlink_event_track"): shared = [item for item in shared if item != track_id]
        event["sharedTrackIds"] = shared; validate_event(event)
        result = await bridge.call("write", {"owner": owner, "workspace_id": workspace, "id": event_id, "content": serialize_event(event), "base": doc.get("head"), "corpus": "notes"}, timeout=60)
        if result.get("outcome") == "stale": raise CopalManageError("Galaxy event is stale", code="stale")
        return _result(action, workspace, doc=result.get("doc") or doc, data={"sharedTrackIds": shared})

    if action == "bases.row.update":
        base = _base_doc(docs, _id(args.get("id")))
        patch = args.get("patch") or {}
        if not isinstance(patch, dict) or set(patch) - {"property", "value", "baseId"}:
            raise CopalManageError("Base row update requires property and value", code="invalid_patch")
        property_name = str(patch.get("property") or "").strip()
        if not property_name:
            raise CopalManageError("Base row update requires a property", code="missing_field")
        row_id = _id(str(patch.get("baseId") or ""), "baseId")
        row = by_id.get(row_id)
        if not row or row.get("kind") in {"base", "planning", "calendar-projection", "treehouse-state"} or row.get("kind") == "asset":
            raise CopalManageError("Base row is not an editable Copal document", code="unsupported_kind")
        if row.get("format") == "copal-note-v1" or row.get("kind") in {"note", "wiki"}:
            content = _encode_note_content(row, str(row.get("text") or ""), properties={**(row.get("properties") or {}), property_name: patch.get("value")})
        else:
            try:
                content = set_frontmatter_property(str(row.get("text") or ""), property_name, patch.get("value"))
            except BaseDefinitionError as exc:
                raise CopalManageError("Base row frontmatter is invalid", code="invalid_row", detail={"diagnostics": exc.diagnostics}) from exc
        result = await bridge.call(
            "write",
            {"owner": owner, "workspace_id": workspace, "id": row_id, "content": content, "base": row.get("head"), "corpus": "wiki" if row.get("kind") == "wiki" else "notes"},
            timeout=60,
        )
        if (result or {}).get("outcome") == "stale":
            raise CopalManageError("Base row changed before this write; refresh and retry explicitly", code="stale")
        return _result(action, workspace, doc=result.get("doc") or row, data={"baseId": base.get("id"), "rowId": row_id, "property": property_name, "value": patch.get("value")})

    if action == "treehouse.command":
        command = args.get("command")
        if not isinstance(command, dict) or not isinstance(command.get("type"), str) or not isinstance(command.get("payload", {}), dict):
            raise CopalManageError("treehouse.command requires {type, payload}", code="invalid_command")
        if command["type"] not in TREEHOUSE_COMMAND_TYPES:
            raise CopalManageError("TreeHouse command type is not supported", code="unsupported_command", detail={"type": command["type"]})
        command_id = _id(args.get("commandId") or uuid.uuid4().hex, "commandId")
        # Native manage callers carry the authenticated immutable account as
        # ``owner``.  Keep this path on the same repository/predicate as HTTP;
        # only the explicit local compatibility owner uses bridge documents.
        configured_repository = os.environ.get("TREEHOUSE_REPOSITORY_PATH")
        repository = TreeHouseRepository(Path(configured_repository) if configured_repository else Path(DATA_DIR) / "treehouse.sqlite3") if owner not in {"local", ""} and not supplied_bridge else None
        if repository is not None:
            if not account_id:
                raise CopalManageError("TreeHouse requires an immutable account identity", code="identity_unavailable")
            caller_account_id = str(account_id)
            state, current_revision = repository.get_catalogue(caller_account_id, workspace)
            if state is None:
                state = instantiate_field_guide(new_treehouse_state(caller_account_id), caller_account_id)
                state, current_revision, _created = repository.create_catalogue_if_absent(caller_account_id, workspace, state)
            payload = command.get("payload") or {}
            if command["type"] == "progress.reset" and not payload.get("courseId"):
                rows = []
                for index, ref in enumerate(repository.accessible_course_refs(caller_account_id, workspace)):
                    source, source_revision = repository.get_catalogue(ref["ownerAccountId"], workspace)
                    if not source:
                        continue
                    progress, progress_revision = repository.get_progress(caller_account_id, ref["ownerAccountId"], workspace, ref["courseId"])
                    working = copy.deepcopy(source)
                    profile = (progress or {}).get("profiles", {}).get(caller_account_id) or working.get("profiles", {}).get(caller_account_id) or working.get("profiles", {}).get("owner")
                    working["profiles"] = {caller_account_id: copy.deepcopy(profile or {"id": caller_account_id, "displayName": caller_account_id, "roles": ["admin", "instructor", "learner"], "active": True})}
                    for key in ("enrollments", "submissions", "evidence", "events", "processedCommands", "progressResets"):
                        value = (progress or {}).get(key)
                        working[key] = copy.deepcopy(value) if value is not None else ([] if key == "events" else {})
                    reset_epoch = repository.progress_reset_epoch(caller_account_id, ref["ownerAccountId"], workspace, ref["courseId"])
                    next_state, result, changed = apply_treehouse_command(
                        working,
                        {"type": "progress.reset", "payload": {**payload, "courseId": ref["courseId"], "profileId": caller_account_id}},
                        actor_id=caller_account_id,
                        command_id=f"{command_id}-reset-{index}",
                        expected_revision=working.get("revision", source_revision),
                    )
                    if changed:
                        progress_projection = {
                            "schemaVersion": next_state.get("schemaVersion"), "revision": next_state.get("revision"),
                            "profiles": {caller_account_id: next_state.get("profiles", {}).get(caller_account_id, {})},
                            "enrollments": {key: value for key, value in next_state.get("enrollments", {}).items() if value.get("profileId") == caller_account_id or value.get("learnerId") == caller_account_id},
                            "submissions": {key: value for key, value in next_state.get("submissions", {}).items() if value.get("profileId") == caller_account_id or value.get("learnerId") == caller_account_id},
                            "evidence": {key: value for key, value in next_state.get("evidence", {}).items() if value.get("profileId") == caller_account_id or value.get("learnerId") == caller_account_id or value.get("actorId") == caller_account_id},
                            "events": [event for event in next_state.get("events", []) if event.get("actorId") == caller_account_id or event.get("subjectId") == caller_account_id],
                            "processedCommands": {key: value for key, value in next_state.get("processedCommands", {}).items() if value.get("actorId") == caller_account_id},
                            "progressResets": {key: value for key, value in next_state.get("progressResets", {}).items() if key.startswith(caller_account_id + ":") or key == caller_account_id},
                        }
                        grant = repository.active_grant(caller_account_id, workspace, ref["ownerAccountId"], ref["courseId"]) if ref["ownerAccountId"] != caller_account_id else None
                        rows.append({"owner_account_id": ref["ownerAccountId"], "course_id": ref["courseId"], "state": progress_projection, "expected_revision": progress_revision, "reset_epoch": reset_epoch + 1, "expected_reset_epoch": reset_epoch, "grant_id": grant["grant_id"] if grant else None, "access_revision": int(grant["revision"]) if grant else None, "result": result})
                reset_result = repository.reset_progress_all(caller_account_id, workspace, rows, command_id=command_id, payload={"type": command["type"], "payload": payload})
                latest_state, latest_revision = repository.get_catalogue(caller_account_id, workspace)
                snapshot = public_treehouse_snapshot(latest_state or state, caller_account_id)
                return _result("treehouse.command", workspace, doc={"id": None, "head": str(latest_revision), "kind": "treehouse-state"}, data={"result": reset_result, "snapshot": snapshot, "fingerprint": state_fingerprint(latest_state or state)}, accountId=caller_account_id)
            course_id = str(payload.get("courseId") or "")
            entity_id = str(payload.get("activityId") or payload.get("assignmentId") or payload.get("moduleId") or "")
            course_owner = repository.owner_for_course(caller_account_id, workspace, course_id) if course_id else None
            if not course_owner and entity_id:
                resolved = repository.owner_for_entity(caller_account_id, workspace, entity_id)
                if resolved:
                    course_owner, course_id = resolved
                    payload = {**payload, "courseId": course_id}
            if course_id and command["type"] not in {"course.create", "course.share", "course.accept_share"} and not course_owner:
                raise CopalManageError("TreeHouse course is not available to this account", code="not_found")
            target_owner = str(course_owner or caller_account_id)
            target_state, target_revision = repository.get_catalogue(target_owner, workspace)
            if target_state is None:
                if target_owner != caller_account_id:
                    raise CopalManageError("TreeHouse course is not available to this account", code="not_found")
                target_state, target_revision = state, current_revision
            grant = repository.active_grant(caller_account_id, workspace, target_owner, course_id) if target_owner != caller_account_id and course_id else None
            progress_commands = {"enrollment.enroll", "enrollment.unenroll", "enrollment.drop", "activity.complete", "submission.submit", "evidence.submit", "progress.reset"}
            progress_only = bool(course_id) and command["type"] in progress_commands
            if target_owner != caller_account_id and not progress_only and command["type"] not in {"course.accept_share"}:
                if not grant or not repository.access(caller_account_id, workspace, target_owner, course_id, "edit"):
                    raise CopalManageError("TreeHouse course is not editable by this account", code="forbidden")
            if command["type"] == "course.share" and caller_account_id != target_owner:
                raise CopalManageError("Only the course owner can share", code="forbidden")
            if command["type"] == "course.revoke_share" and caller_account_id != target_owner:
                raise CopalManageError("Only the course owner can revoke a share", code="forbidden")
            if command["type"] == "course.accept_share":
                token_grant = repository.grant_for_token(workspace, str(payload.get("shareToken") or ""))
                if not token_grant or token_grant["recipient_account_id"] != caller_account_id:
                    raise CopalManageError("Share link is invalid", code="share_not_found")
                target_owner = str(token_grant["owner_account_id"])
                course_id = str(token_grant["course_id"])
                target_state, target_revision = repository.get_catalogue(target_owner, workspace)
                if target_state is None:
                    raise CopalManageError("Shared course is unavailable", code="not_found")
            working_state = copy.deepcopy(target_state)
            if grant and target_owner != caller_account_id:
                # The repository grant is the authoritative accepted/revoked
                # record; mirror its active acceptance into the compatibility
                # domain view used by the command validator.
                found_grant = False
                for grant_record in working_state.get("courseGrants", {}).values():
                    if grant_record.get("courseId") == course_id and grant_record.get("recipientId") == caller_account_id and not grant_record.get("revokedAt"):
                        found_grant = True
                        grant_record["acceptedAt"] = grant_record.get("acceptedAt") or datetime.now(UTC).isoformat()
                if not found_grant:
                    working_state.setdefault("courseGrants", {})[grant["grant_id"]] = {
                        "id": grant["grant_id"], "grantId": grant["grant_id"],
                        "ownerId": target_owner, "ownerAccountId": target_owner,
                        "recipientId": caller_account_id, "courseId": course_id,
                        "capability": grant["role"], "acceptedAt": grant["accepted_at"], "revokedAt": grant["revoked_at"],
                    }
            if progress_only:
                progress_state, progress_revision = repository.get_progress(caller_account_id, target_owner, workspace, course_id)
                template_profile = working_state.get("profiles", {}).get(caller_account_id) or working_state.get("profiles", {}).get("owner", {})
                # Always construct the learner namespace from scratch.  The
                # first progress command has no stored row yet, and copying
                # the owner's profile/events in that case would let private
                # author activity influence prerequisites, awards or replay
                # receipts.  Curriculum entities remain available read-only.
                working_state["profiles"] = {
                    caller_account_id: copy.deepcopy(progress_state.get("profiles", {}).get(caller_account_id, {
                        "id": caller_account_id, "displayName": caller_account_id,
                        "roles": ["admin", "instructor", "learner"], "active": True,
                        "createdAt": template_profile.get("createdAt"),
                    })) if progress_state else {
                        "id": caller_account_id, "displayName": caller_account_id,
                        "roles": ["admin", "instructor", "learner"], "active": True,
                        "createdAt": template_profile.get("createdAt"),
                    }
                }
                for key in ("enrollments", "submissions", "evidence", "events", "processedCommands", "progressResets"):
                    value = (progress_state or {}).get(key)
                    working_state[key] = copy.deepcopy(value) if value is not None else ([] if key == "events" else {})
                reset_epoch = repository.progress_reset_epoch(caller_account_id, target_owner, workspace, course_id)
            else:
                progress_revision = 0
                reset_epoch = 0
            added_recipient_profile = False
            if command["type"] == "course.share":
                recipient_id = str(payload.get("recipientId") or "").strip()
                if not recipient_id or recipient_id == caller_account_id:
                    raise CopalManageError("Share recipient is invalid", code="recipient_not_found")
                if recipient_id not in working_state.get("profiles", {}):
                    working_state.setdefault("profiles", {})[recipient_id] = {
                        "id": recipient_id, "displayName": recipient_id,
                        "roles": ["admin", "instructor", "learner"], "active": True,
                    }
                    added_recipient_profile = True
            try:
                command_payload = dict(payload)
                if progress_only and command["type"] in {"activity.complete", "submission.submit", "evidence.submit"}:
                    command_payload.setdefault("resetGeneration", reset_epoch)
                next_state, command_result, changed = apply_treehouse_command(
                    working_state, {"type": command["type"], "payload": command_payload}, actor_id=caller_account_id,
                    command_id=command_id, expected_revision=working_state.get("revision", target_revision) if (progress_only or command["type"] == "course.accept_share") else (target_revision if args.get("expectedRevision") is None else args.get("expectedRevision")),
                )
                result = command_result
                if changed and command["type"] == "course.share":
                    if added_recipient_profile:
                        next_state.get("profiles", {}).pop(str(payload.get("recipientId")), None)
                    grant_result = repository.create_share(
                        owner_account_id=caller_account_id, workspace_id=workspace,
                        course_id=course_id, recipient_account_id=str(payload.get("recipientId") or ""),
                        role=str(payload.get("capability") or "learn"), access_revision=int(next_state.get("revision", 0)),
                        now=datetime.now(UTC).isoformat(), command_id=command_id, payload=payload,
                        state=next_state, expected_catalogue_revision=target_revision,
                    )
                    result = {**result, **grant_result}
                elif changed and command["type"] == "course.accept_share":
                    repository.accept_share(recipient_account_id=caller_account_id, workspace_id=workspace, token=str(payload.get("shareToken") or ""), now=datetime.now(UTC).isoformat())
                elif changed and command["type"] == "course.revoke_share":
                    grant_before = repository.grant(str(payload.get("grantId") or ""))
                    repository.revoke_share_with_catalogue(
                        owner_account_id=caller_account_id, workspace_id=workspace,
                        grant_id=str(payload.get("grantId") or ""),
                        expected_grant_revision=int(grant_before["revision"]) if grant_before else None,
                        expected_catalogue_revision=target_revision, state=next_state,
                        now=datetime.now(UTC).isoformat(),
                    )
                    result = {**result, "revoked": True}
                elif changed and progress_only:
                    progress = {
                        "schemaVersion": next_state.get("schemaVersion"),
                        "revision": next_state.get("revision"),
                        "profiles": {caller_account_id: next_state.get("profiles", {}).get(caller_account_id, {})},
                        "enrollments": {ident: value for ident, value in (next_state.get("enrollments") or {}).items() if value.get("profileId") == caller_account_id or value.get("learnerId") == caller_account_id},
                        "submissions": {ident: value for ident, value in (next_state.get("submissions") or {}).items() if value.get("profileId") == caller_account_id or value.get("learnerId") == caller_account_id},
                        "evidence": {ident: value for ident, value in (next_state.get("evidence") or {}).items() if value.get("profileId") == caller_account_id or value.get("learnerId") == caller_account_id or value.get("actorId") == caller_account_id},
                        "events": [event for event in (next_state.get("events") or []) if event.get("profileId") == caller_account_id or event.get("actorId") == caller_account_id],
                        "processedCommands": {ident: value for ident, value in (next_state.get("processedCommands") or {}).items() if value.get("actorId") == caller_account_id},
                        "progressResets": {ident: value for ident, value in (next_state.get("progressResets") or {}).items() if ident.startswith(caller_account_id + ":") or ident == caller_account_id},
                    }
                    next_epoch = max([reset_epoch, *[int(value or 0) for value in (progress.get("progressResets") or {}).values()]])
                    repository.put_progress(caller_account_id, target_owner, workspace, course_id, progress, expected_revision=progress_revision, reset_epoch=next_epoch, expected_reset_epoch=reset_epoch, grant_id=grant.get("grant_id") if grant else None, access_revision=int(grant["revision"]) if grant else None)
                elif changed:
                    repository.put_catalogue(target_owner, workspace, next_state, expected_revision=target_revision, access_grant_id=grant.get("grant_id") if grant else None, access_revision=int(grant["revision"]) if grant else None)
                snapshot = public_treehouse_snapshot(next_state, caller_account_id)
            except TreeHouseError as exc:
                raise CopalManageError(str(exc), code=exc.code, detail=exc.details) from exc
            except TreeHouseRepositoryError as exc:
                raise CopalManageError(str(exc), code=exc.code, detail=exc.details) from exc
            return _result(action, workspace, doc={"id": None, "head": str(next_state.get("revision", target_revision)), "kind": "treehouse-state"}, data={"result": result, "snapshot": snapshot, "fingerprint": state_fingerprint(next_state)})
        doc, state = await _treehouse_state(bridge, owner, workspace, initialize=True)
        try:
            next_state, command_result, changed = apply_treehouse_command(
                {**state},
                {"type": command["type"], "payload": command.get("payload") or {}},
                actor_id="owner",
                command_id=command_id,
                expected_revision=args.get("expectedRevision"),
            )
        except TreeHouseError as exc:
            raise CopalManageError(str(exc), code=exc.code, detail=exc.details) from exc
        result = await _write_treehouse(bridge, owner, workspace, doc, next_state) if changed else {"doc": doc, "outcome": "unchanged"}
        try:
            snapshot = public_treehouse_snapshot(next_state, "owner")
        except TreeHouseError as exc:
            raise CopalManageError(str(exc), code=exc.code, detail=exc.details) from exc
        return _result(action, workspace, doc=result.get("doc") or doc, data={"result": command_result, "snapshot": snapshot, "fingerprint": state_fingerprint(next_state)})

    if action.startswith("mind.heading."):
        document_id = _id(args.get("id")); doc = by_id.get(document_id)
        if not doc or doc.get("kind") not in {"note", "wiki", "markdown"}:
            raise CopalManageError("Mind source must be a Copal note, wiki, or Markdown document", code="not_found")
        content = _mind_transform(str(doc.get("text") or ""), action, args.get("patch") or {})
        if content == str(doc.get("text") or ""):
            return _result(action, workspace, doc=doc, saved=False, data={"changed": False})
        if doc.get("kind") in {"note", "wiki"}:
            content = _encode_note_content(doc, content)
        result = await bridge.call("write", {"owner": owner, "workspace_id": workspace, "id": document_id, "content": content, "base": doc.get("head"), "corpus": "wiki" if doc.get("kind") == "wiki" else "notes"}, timeout=60)
        if result.get("outcome") == "stale":
            raise CopalManageError("Mind source is stale; no replay was attempted", code="stale")
        return _result(action, workspace, doc=result.get("doc") or doc, data={"changed": True})

    if action in {"graph.link", "graph.unlink"}:
        document_id = _id(args.get("id")); doc = by_id.get(document_id)
        if not doc or doc.get("kind") not in {"note", "wiki"}:
            raise CopalManageError("Graph source must be a Copal note or wiki", code="not_found")
        patch = args.get("patch") or {}
        if not isinstance(patch, dict):
            raise CopalManageError("graph patch must be an object", code="invalid_patch")
        relations = [copy.deepcopy(item) for item in doc.get("relations") or [] if isinstance(item, dict)]
        if action == "graph.link":
            allowed = {"kind", "target", "targetDocumentId", "targetBlockId", "sourceBlockId", "id"}
            if set(patch) - allowed or not str(patch.get("target") or patch.get("targetDocumentId") or "").strip():
                raise CopalManageError("graph.link needs one target and only relation fields", code="invalid_patch")
            relation = {
                "id": str(patch.get("id") or f"rel-{hashlib.sha256(json.dumps(patch, sort_keys=True).encode()).hexdigest()[:16]}"),
                "kind": str(patch.get("kind") or "link"),
                "sourceBlockId": patch.get("sourceBlockId"),
                "target": str(patch.get("target") or patch.get("targetDocumentId")),
                "targetDocumentId": patch.get("targetDocumentId"),
                "targetBlockId": patch.get("targetBlockId"),
            }
            if any(item.get("id") == relation["id"] for item in relations):
                raise CopalManageError("relation already exists", code="already_exists")
            relations.append(relation)
        else:
            selectors = [key for key in ("id", "target", "targetDocumentId") if patch.get(key) not in (None, "")]
            if len(selectors) != 1:
                raise CopalManageError("graph.unlink requires exactly one relation selector", code="invalid_patch")
            key = selectors[0]; value = str(patch[key])
            matches = [item for item in relations if str(item.get(key) or "") == value]
            if len(matches) != 1:
                raise CopalManageError("graph.unlink target must identify exactly one relation", code="ambiguous_target" if matches else "not_found")
            relations = [item for item in relations if item is not matches[0]]
        content = _encode_note_content(doc, str(doc.get("text") or ""), relations=relations)
        result = await bridge.call("write", {"owner": owner, "workspace_id": workspace, "id": document_id, "content": content, "base": doc.get("head"), "corpus": "wiki" if doc.get("kind") == "wiki" else "notes"}, timeout=60)
        if result.get("outcome") == "stale":
            raise CopalManageError("Graph source is stale; no replay was attempted", code="stale")
        return _result(action, workspace, doc=result.get("doc") or doc, data={"relations": relations})

    if action == "todo.create":
        event = dict(args.get("event") or {})
        try:
            validate_event(event)
        except Exception as exc:
            raise CopalManageError(str(exc), code="invalid_event") from exc
        if not event.get("id"):
            raise CopalManageError("todo.create requires a stable event id", code="missing_field")
        created = await bridge.call("create", {"owner": owner, "workspace_id": workspace, "name": f".copal/events/{event['id']}.json", "kind": EVENT_KIND, "corpus": "notes", "content": serialize_event(event)}, timeout=60)
        return _result(action, workspace, doc=created.get("doc") or {}, data=created)

    if action in {"todo.update", "todo.complete", "todo.trash"}:
        raw_id = str(args.get("id") or "").strip()
        if ":" in raw_id:
            document_id, task_id = raw_id.split(":", 1)
            doc = by_id.get(document_id)
            if not doc:
                raise CopalManageError("Todo source document not found", code="not_found")
            line, task = _task_line(doc, raw_id)
            lines = str(doc.get("text") or "").splitlines()
            if line > len(lines):
                raise CopalManageError("Todo source line is no longer present", code="stale")
            patch = args.get("patch") or {}
            if action == "todo.trash":
                raise CopalManageError("Markdown todo deletion remains a Notes edit; no implicit destructive delete", code="unsupported_action")
            done = True if action == "todo.complete" else bool(patch.get("done", task.get("done")))
            text = str(patch.get("text", task.get("text") or "")).strip() if action == "todo.update" else str(task.get("text") or "")
            prefix = re.match(r"^(\s*)-\s*\[[ xX]\]\s+", lines[line - 1])
            if not prefix:
                raise CopalManageError("Todo source is not a supported task block", code="unsupported_kind")
            lines[line - 1] = f"{prefix.group(1)}- [{'x' if done else ' '}] {text}"
            updated_body = "\n".join(lines)
            content = _encode_note_content(doc, updated_body) if doc.get("kind") in {"note", "wiki"} else updated_body
            result = await bridge.call("write", {"owner": owner, "workspace_id": workspace, "id": document_id, "content": content, "base": doc.get("head"), "corpus": "wiki" if doc.get("kind") == "wiki" else "notes"}, timeout=60)
            if result.get("outcome") == "stale":
                raise CopalManageError("Todo source is stale; no replay was attempted", code="stale")
            return _result(action, workspace, doc=result.get("doc") or doc, data={"taskId": raw_id, "done": done, "text": text})
        event_doc = by_id.get(_id(raw_id)); event = event_from_document(event_doc) if event_doc else None
        if event and action == "todo.trash":
            result = await bridge.call("delete", {"owner": owner, "workspace_id": workspace, "id": raw_id, "corpus": "notes"}, timeout=30)
            return _result(action, workspace, doc=event_doc, data=result)
        raise CopalManageError("Todo target must be a stable note task or canonical event", code="not_found")

    if action == "maintenance.calendar_reconcile":
        planning = planning_projection(docs)
        source = next((doc for doc in docs if doc.get("kind") == TRACKS_KIND), None)
        if not planning.get("canonical"):
            source = next((doc for doc in docs if doc.get("kind") == "planning"), None)
            if not source:
                return _result(action, workspace, data={"enabled": True, "changed": False, "reason": "No planning workspace exists"})
            try:
                planning = json.loads(str(source.get("text") or "{}"))
            except json.JSONDecodeError as exc:
                raise CopalManageError("Legacy planning document is not valid JSON", code="invalid_planning") from exc
            source_revision = str(source.get("head") or "") or None
        else:
            canonical_docs = [source, *[doc for doc in docs if event_from_document(doc)]] if source else []
            source_revision = revision_fingerprint(canonical_docs) if canonical_docs else None
        if not source:
            return _result(action, workspace, data={"enabled": True, "changed": False, "reason": "No canonical planning source exists"})
        try:
            projection = await asyncio.to_thread(
                reconcile_projection,
                planning,
                owner=owner,
                workspace=workspace,
                planning_document_id=str(source["id"]),
                source_revision=source_revision,
            )
        except Exception as exc:
            return _result(action, workspace, saved=False, data={"enabled": True, "ok": False, "error": str(exc), "retryable": True})
        return _result(action, workspace, data=projection, warnings=["Calendar is a projection; Copal remains canonical"])

    raise CopalManageError("This mutation is reserved for the next implementation packet", code="unsupported_action")

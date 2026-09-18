"""Owner-scoped, read-only Copal tool adapter.

This module is deliberately below the HTTP routes.  Agent calls do not have a
browser ``Request`` object, so replaying loopback HTTP would lose the execution
owner and make workspace scoping easy to get wrong.  The adapter talks to the
same Redb bridge and uses the same projection/model functions as the routes.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from src.openclank.copal_bridge import CopalBridge
from src.openclank.copal_planning import (
    EVENT_KIND,
    TRACKS_KIND,
    canonical_documents,
    event_from_document,
    planning_projection,
    track_registry_from_document,
)
from src.openclank.copal_treehouse import (
    compute_treehouse_projections,
    new_treehouse_state,
    public_treehouse_snapshot,
    state_fingerprint,
    validate_treehouse_state,
)
from src.openclank.copal_treehouse_repository import TreeHouseRepository
from src.constants import DATA_DIR


VIEWS = ("notes", "wiki", "timeline", "galaxy", "graph", "mind", "bases", "treehouse", "todo")
READ_ACTIONS = (
    "notes.list", "notes.get", "notes.history", "trash.list",
    "wiki.list", "wiki.get", "wiki.history",
    "timeline.get", "timeline.event.get", "timeline.track.get",
    "galaxy.get", "graph.get", "mind.get_outline",
    "bases.list", "bases.get", "bases.query",
    "treehouse.get", "treehouse.integrity", "todo.list",
    "maintenance.status", "maintenance.operations",
)
_WORKSPACE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_LIMIT = 100
_MAX_GET_BYTES = 64 * 1024
_MAX_TEXT_BYTES = 16 * 1024
_ACTION_FIELDS = {
    "notes.list": {"action", "workspace", "query", "cursor", "limit"}, "notes.get": {"action", "workspace", "id"}, "notes.history": {"action", "workspace", "id"}, "trash.list": {"action", "workspace", "cursor", "limit"},
    "wiki.list": {"action", "workspace", "query", "cursor", "limit"}, "wiki.get": {"action", "workspace", "id"}, "wiki.history": {"action", "workspace", "id"},
    "timeline.get": {"action", "workspace", "cursor", "limit"}, "timeline.event.get": {"action", "workspace", "id"}, "timeline.track.get": {"action", "workspace", "id"},
    "galaxy.get": {"action", "workspace", "limit"}, "graph.get": {"action", "workspace", "query", "limit"}, "mind.get_outline": {"action", "workspace", "id"},
    "bases.list": {"action", "workspace", "query", "cursor", "limit"}, "bases.get": {"action", "workspace", "id"}, "bases.query": {"action", "workspace", "id", "section", "limit"},
    "treehouse.get": {"action", "workspace", "section"}, "treehouse.integrity": {"action", "workspace"}, "todo.list": {"action", "workspace", "query", "cursor", "limit"},
    "maintenance.status": {"action", "workspace"}, "maintenance.operations": {"action", "workspace", "cursor", "limit"},
}


class CopalReadError(ValueError):
    """Structured client error for malformed or unauthorized read calls."""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


def _workspace(value: Any) -> str:
    value = str(value or "default").strip()
    if not _WORKSPACE.fullmatch(value):
        raise CopalReadError("workspace must be a logical Copal workspace ID", code="invalid_workspace")
    return value


def _id(value: Any, field: str = "id") -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise CopalReadError(f"{field} must be a stable Copal resource ID", code="invalid_id")
    return value


def _limit(value: Any) -> int:
    if value is None:
        return 100
    if isinstance(value, bool):
        raise CopalReadError("limit must be an integer", code="invalid_limit")
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise CopalReadError("limit must be an integer", code="invalid_limit") from exc
    if value < 1:
        raise CopalReadError("limit must be at least 1", code="invalid_limit")
    return min(value, _MAX_LIMIT)


def _cursor(value: Any, action: str, workspace: str) -> int:
    if value in (None, ""):
        return 0
    try:
        raw = base64.urlsafe_b64decode(str(value).encode() + b"===")
        payload = json.loads(raw)
        if payload.get("action") != action or payload.get("workspace") != workspace:
            raise ValueError
        offset = int(payload["offset"])
        if offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CopalReadError("cursor is invalid or belongs to another read", code="invalid_cursor") from exc


def encode_cursor(action: str, workspace: str, offset: int) -> str:
    raw = json.dumps({"action": action, "workspace": workspace, "offset": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _strict_args(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise CopalReadError("read_copal arguments must be an object")
    allowed_common = {"action", "workspace", "id", "query", "section", "cursor", "limit"}
    extras = set(arguments) - allowed_common
    if extras:
        raise CopalReadError(f"unknown read_copal field(s): {', '.join(sorted(extras))}", code="extra_field")
    action = arguments.get("action")
    if action not in READ_ACTIONS:
        raise CopalReadError("action is not a supported pure Copal read", code="invalid_action")
    workspace = _workspace(arguments.get("workspace"))
    extras_for_action = set(arguments) - _ACTION_FIELDS[action]
    if extras_for_action:
        raise CopalReadError(f"{action} does not accept field(s): {', '.join(sorted(extras_for_action))}", code="extra_field")
    requires_id = action.endswith(".get") and action not in {"timeline.get", "galaxy.get", "graph.get", "treehouse.get"}
    allows_id = action in {"notes.get", "notes.history", "wiki.get", "wiki.history", "timeline.event.get", "timeline.track.get", "bases.get", "bases.query", "mind.get_outline"}
    if requires_id and not arguments.get("id"):
        raise CopalReadError(f"{action} requires id", code="missing_id")
    if not allows_id and action not in {"notes.get", "notes.history", "wiki.get", "wiki.history", "timeline.event.get", "timeline.track.get", "bases.get", "bases.query", "mind.get_outline"} and arguments.get("id"):
        raise CopalReadError(f"{action} does not accept id", code="extra_field")
    if action in {"notes.get", "notes.history", "wiki.get", "wiki.history", "timeline.event.get", "timeline.track.get", "bases.get", "mind.get_outline"} and any(key in arguments for key in ("query", "section", "cursor")):
        raise CopalReadError(f"{action} accepts only action, workspace, and id", code="extra_field")
    if action == "treehouse.get" and arguments.get("section") not in (None, "overview", "courses", "course", "skills", "assignments", "submissions", "evidence", "badges", "quests", "progress", "analytics"):
        raise CopalReadError("treehouse section is not supported", code="invalid_section")
    if action == "bases.query" and not arguments.get("id"):
        raise CopalReadError("bases.query requires the Base definition id", code="missing_id")
    return {**arguments, "action": action, "workspace": workspace, "limit": _limit(arguments.get("limit"))}


def _doc_view(doc: dict[str, Any]) -> dict[str, Any]:
    """Small stable document projection; raw owner/storage fields never leave."""
    result = {
        "id": doc.get("id"), "name": doc.get("name"), "kind": doc.get("kind"),
        "corpus": doc.get("corpus"), "head": doc.get("head"),
        "hidden": bool(doc.get("hidden")), "tags": list(doc.get("tags") or []),
        "properties": copy.deepcopy(doc.get("properties") or {}),
        "relations": copy.deepcopy(doc.get("relations") or []),
        "text": str(doc.get("text") or ""),
    }
    raw = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode()) > _MAX_GET_BYTES:
        result["text"] = result["text"][:_MAX_TEXT_BYTES] + "\n[truncated; call read_copal again for the bounded document page]"
        result["truncated"] = True
    return result


async def _docs(bridge: Any, owner: str, workspace: str) -> list[dict[str, Any]]:
    result = await bridge.call("index", {"owner": str(owner or "local"), "workspace_id": workspace, "query": ""}, timeout=60)
    return [doc for doc in result.get("docs") or [] if isinstance(doc, dict)]


def _page(items: list[Any], action: str, workspace: str, offset: int, limit: int) -> tuple[list[Any], dict[str, Any]]:
    selected = items[offset:offset + limit]
    next_offset = offset + len(selected)
    return selected, {"nextCursor": encode_cursor(action, workspace, next_offset) if next_offset < len(items) else None, "truncated": next_offset < len(items), "total": len(items)}


def _envelope(action: str, workspace: str, data: Any, *, kind: str | None = None, resource: dict[str, Any] | None = None, page: dict[str, Any] | None = None, projection: bool = False, sources: list[str] | None = None) -> dict[str, Any]:
    resource = resource or {}
    result = {"ok": True, "action": action, "workspace": workspace, "view": action.split(".", 1)[0], "resourceKind": kind or resource.get("kind"), "resourceId": resource.get("id"), "head": resource.get("head"), "data": data, "page": page or {"nextCursor": None, "truncated": False}, "openUrl": f"/copal/{action.split('.', 1)[0]}"}
    if resource.get("id"):
        result["openUrl"] += f"?doc={resource['id']}"
    if projection:
        result["projection"] = True
        result["sources"] = sources or []
    return result


async def read_copal(arguments: Any, *, owner: str | None = None, account_id: str | None = None, bridge: Any | None = None, admin: bool = False) -> dict[str, Any]:
    """Execute one bounded, owner/workspace-scoped pure read."""
    args = _strict_args(arguments)
    action, workspace = args["action"], args["workspace"]
    if action == "maintenance.operations" and not admin:
        raise CopalReadError("maintenance.operations requires an admin execution", code="forbidden")
    supplied_bridge = bridge is not None
    bridge = bridge or CopalBridge()
    docs = await _docs(bridge, owner or "local", workspace)
    by_id = {str(doc.get("id")): doc for doc in docs if doc.get("id")}
    if action in {"notes.list", "wiki.list", "trash.list"}:
        if action == "trash.list":
            result = await bridge.call("trash", {"owner": owner or "local", "workspace_id": workspace, "corpus": "notes"}, timeout=60)
            items = [_doc_view(doc) for doc in result.get("docs") or []]
        else:
            corpus = "wiki" if action.startswith("wiki") else "notes"
            items = [_doc_view(doc) for doc in docs if doc.get("corpus", "notes") == corpus and doc.get("kind") in ({"wiki"} if corpus == "wiki" else {"note", "markdown"})]
            query = str(args.get("query") or "").casefold()
            if query:
                items = [item for item in items if query in json.dumps(item, ensure_ascii=False).casefold()]
        selected, page = _page(items, action, workspace, _cursor(args.get("cursor"), action, workspace), args["limit"])
        return _envelope(action, workspace, selected, kind="wiki" if action.startswith("wiki") else "note", page=page)
    if action in {"notes.get", "wiki.get"}:
        doc = by_id.get(_id(args["id"]))
        if not doc or (action.startswith("wiki") and doc.get("kind") != "wiki") or (action.startswith("notes") and doc.get("kind") not in {"note", "markdown"}):
            raise CopalReadError("Copal document not found in this owner/workspace", code="not_found")
        return _envelope(action, workspace, _doc_view(doc), kind=doc.get("kind"), resource=doc)
    if action in {"notes.history", "wiki.history"}:
        doc = by_id.get(_id(args["id"]))
        if not doc:
            raise CopalReadError("Copal document not found in this owner/workspace", code="not_found")
        corpus = "wiki" if action.startswith("wiki") else "notes"
        history = await bridge.call("history", {"owner": owner or "local", "workspace_id": workspace, "id": doc["id"], "corpus": corpus}, timeout=60)
        return _envelope(action, workspace, history, kind=doc.get("kind"), resource=doc)
    if action == "timeline.get":
        projection = planning_projection(docs)
        return _envelope(action, workspace, projection, kind="timeline", projection=True, sources=[str(d.get("id")) for d in docs if event_from_document(d) or d.get("kind") == TRACKS_KIND])
    if action in {"timeline.event.get", "timeline.track.get"}:
        doc = by_id.get(_id(args["id"]))
        expected = EVENT_KIND if action.endswith("event.get") else TRACKS_KIND
        if not doc or (action.endswith("event.get") and not event_from_document(doc)) or (action.endswith("track.get") and doc.get("kind") != expected):
            raise CopalReadError("Timeline resource not found in this owner/workspace", code="not_found")
        value = _doc_view(doc)
        if action.endswith("track.get"):
            value["registry"] = track_registry_from_document(doc)
        return _envelope(action, workspace, value, kind="event" if action.endswith("event.get") else "track-registry", resource=doc)
    if action == "galaxy.get":
        projection = planning_projection(docs)
        return _envelope(action, workspace, {"tracks": projection.get("tracks", []), "events": projection.get("events", [])}, kind="galaxy-projection", projection=True, sources=[str(d.get("id")) for d in docs if event_from_document(d) or d.get("kind") == TRACKS_KIND])
    if action == "graph.get":
        nodes = [_doc_view(doc) for doc in docs if doc.get("kind") not in {"calendar-projection", "planning", "treehouse-state", "copal-operation", "asset"}]
        edges = [{"source": item["id"], **relation} for item in nodes for relation in item.get("relations", []) if isinstance(relation, dict)]
        return _envelope(action, workspace, {"nodes": nodes[:200], "edges": edges[:400]}, kind="graph-projection", projection=True, sources=[str(item["id"]) for item in nodes])
    if action == "mind.get_outline":
        doc = by_id.get(_id(args["id"]))
        if not doc:
            raise CopalReadError("Mind source document not found", code="not_found")
        headings = [{"level": len(match.group(1)), "text": match.group(2).strip(), "line": index + 1} for index, line in enumerate(str(doc.get("text") or "").splitlines()) if (match := re.match(r"^(#{1,6})\s+(.+)$", line))]
        return _envelope(action, workspace, {"source": _doc_view(doc), "headings": headings}, kind="mind-outline", resource=doc, projection=True, sources=[str(doc.get("id"))])
    if action in {"bases.list", "bases.get", "bases.query"}:
        bases = [doc for doc in docs if doc.get("kind") == "base"]
        if action == "bases.list":
            selected, page = _page([_doc_view(doc) for doc in bases], action, workspace, _cursor(args.get("cursor"), action, workspace), args["limit"])
            return _envelope(action, workspace, selected, kind="base", page=page)
        doc = by_id.get(_id(args["id"]))
        if not doc or doc.get("kind") != "base":
            raise CopalReadError("Base not found in this owner/workspace", code="not_found")
        if action == "bases.get":
            return _envelope(action, workspace, _doc_view(doc), kind="base", resource=doc)
        from src.openclank.copal_bases import parse_base_definition, query_base
        definition, diagnostics = parse_base_definition(str(doc.get("text") or ""))
        result = query_base(definition, [_doc_view(item) for item in docs], page=1, page_size=args["limit"])
        return _envelope(action, workspace, {"definition": definition, "diagnostics": diagnostics, "result": result}, kind="base-query", resource=doc, projection=True, sources=[str(item.get("id")) for item in bases])
    if action.startswith("treehouse."):
        # Authenticated agent callers use the same account keyed repository
        # as HTTP.  The bridge document is retained only for local legacy
        # fixtures and migration compatibility.
        configured_repository = os.environ.get("TREEHOUSE_REPOSITORY_PATH")
        repository = TreeHouseRepository(Path(configured_repository) if configured_repository else Path(DATA_DIR) / "treehouse.sqlite3")
        repository_owner = str(account_id or owner or "")
        if repository_owner not in {"", "local"} and not account_id and not supplied_bridge:
            raise CopalReadError("TreeHouse requires an immutable account identity", code="identity_unavailable")
        repository_state, repository_revision = repository.get_catalogue(repository_owner, workspace)
        state_doc = next((doc for doc in docs if doc.get("kind") == "treehouse-state" and doc.get("name") == ".copal/treehouse-state.json"), None)
        refs = repository.accessible_course_refs(repository_owner, workspace) if repository_owner not in {"", "local"} else []
        if refs:
            # The recipient index contains references only.  Re-read each
            # owner catalogue and project the granted course, so a tool read
            # cannot accidentally expose the owner's unrelated lessons,
            # drafts, learners, or events.
            state = new_treehouse_state(repository_owner)
            state["profiles"]["owner"] = {**state["profiles"]["owner"], "id": repository_owner, "displayName": str(owner or repository_owner)}
            state["profiles"][repository_owner] = state["profiles"].pop("owner")
            for ref in refs:
                source, source_revision = repository.get_catalogue(ref["ownerAccountId"], workspace)
                if not source:
                    continue
                course_id = ref["courseId"]
                course = (source.get("courses") or {}).get(course_id)
                if not course or course.get("deletedAt"):
                    continue
                state["courses"][course_id] = copy.deepcopy(course)
                modules = {key: value for key, value in (source.get("modules") or {}).items() if key in set(course.get("moduleIds") or []) and not value.get("deletedAt")}
                state["modules"].update(copy.deepcopy(modules))
                activity_ids = {item_id for module in modules.values() for item_id in module.get("activityIds", [])}
                assignment_ids = {item_id for module in modules.values() for item_id in module.get("assignmentIds", [])}
                state["activities"].update(copy.deepcopy({key: value for key, value in (source.get("activities") or {}).items() if key in activity_ids and value.get("status") == "published" and not value.get("deletedAt")}))
                state["assignments"].update(copy.deepcopy({key: value for key, value in (source.get("assignments") or {}).items() if key in assignment_ids and value.get("status") == "published" and not value.get("deletedAt")}))
                if ref["ownerAccountId"] == repository_owner:
                    state["activities"].update(copy.deepcopy({key: value for key, value in (source.get("activities") or {}).items() if key in activity_ids and value.get("status") != "published" and not value.get("deletedAt")}))
                    state["assignments"].update(copy.deepcopy({key: value for key, value in (source.get("assignments") or {}).items() if key in assignment_ids and value.get("status") != "published" and not value.get("deletedAt")}))
                skill_ids = {skill_id for item in (*state["activities"].values(), *state["assignments"].values()) if item.get("courseId") == course_id for skill_id in item.get("skillIds", [])}
                state["skills"].update(copy.deepcopy({key: value for key, value in (source.get("skills") or {}).items() if key in skill_ids and not value.get("deletedAt")}))
                visible_activity_ids = {key for key in state["activities"] if key in activity_ids}
                visible_assignment_ids = {key for key in state["assignments"] if key in assignment_ids}
                quest_ids = {key for key, value in (source.get("quests") or {}).items() if (set(value.get("activityIds") or []) & visible_activity_ids) or (set(value.get("assignmentIds") or []) & visible_assignment_ids)}
                state["quests"].update(copy.deepcopy({key: value for key, value in (source.get("quests") or {}).items() if key in quest_ids and not value.get("deletedAt")}))
                state["badges"].update(copy.deepcopy({key: value for key, value in (source.get("badges") or {}).items() if not value.get("deletedAt") and (value.get("criteria", {}).get("courseId") == course_id or value.get("criteria", {}).get("skillId") in skill_ids or value.get("criteria", {}).get("questId") in quest_ids)}))
                if ref["ownerAccountId"] != repository_owner:
                    grant = repository.active_grant(repository_owner, workspace, ref["ownerAccountId"], course_id)
                    if grant:
                        state["courseGrants"][grant["grant_id"]] = {
                            "id": grant["grant_id"],
                            "grantId": grant["grant_id"],
                            "ownerId": ref["ownerAccountId"],
                            "ownerAccountId": ref["ownerAccountId"],
                            "recipientId": repository_owner,
                            "courseId": course_id,
                            "capability": grant["role"],
                            "acceptedAt": grant["accepted_at"],
                            "revokedAt": grant["revoked_at"],
                        }
                progress, progress_revision = repository.get_progress(repository_owner, ref["ownerAccountId"], workspace, course_id)
                if progress:
                    for key in ("profiles", "enrollments", "submissions", "evidence", "events", "processedCommands", "progressResets"):
                        value = progress.get(key)
                        if isinstance(value, dict): state[key].update(copy.deepcopy(value))
                        elif isinstance(value, list): state[key].extend(copy.deepcopy(value))
                state["revision"] = max(int(state.get("revision", 0)), int(source_revision), int(progress_revision))
            state_doc = {"id": None, "head": str(state.get("revision", 0)), "kind": "treehouse-state"}
        elif repository_state is not None:
            state = repository_state
            state_doc = {"id": None, "head": str(repository_revision), "kind": "treehouse-state"}
        elif state_doc:
            try:
                state = json.loads(str(state_doc.get("text") or "{}"))
                validate_treehouse_state(state)
            except (json.JSONDecodeError, ValueError) as exc:
                raise CopalReadError("TreeHouse state is corrupt", code="corrupt_state") from exc
        else:
            state = new_treehouse_state(owner or "local")
            state_doc = {"id": None, "head": None, "kind": "treehouse-state"}
        if action == "treehouse.integrity":
            projection = compute_treehouse_projections(state)
            return _envelope(action, workspace, {"schemaVersion": state["schemaVersion"], "revision": state["revision"], "eventCount": len(state["events"]), "projectionLearners": len(projection["learners"]), "fingerprint": state_fingerprint(state)}, kind="treehouse", resource=state_doc)
        snapshot = public_treehouse_snapshot(state, repository_owner if refs else "owner")
        section = args.get("section")
        data = snapshot if not section or section == "overview" else {section: snapshot.get(section)}
        return _envelope(action, workspace, data, kind="treehouse", resource=state_doc)
    if action == "todo.list":
        tasks = []
        for doc in docs:
            for index, line in enumerate(str(doc.get("text") or "").splitlines()):
                match = re.match(r"^\s*-\s*\[([ xX])\]\s+(.+)$", line)
                if match:
                    tasks.append({"id": f"{doc.get('id')}:{index + 1}", "documentId": doc.get("id"), "line": index + 1, "done": match.group(1).lower() == "x", "text": match.group(2)})
        selected, page = _page(tasks, action, workspace, _cursor(args.get("cursor"), action, workspace), args["limit"])
        return _envelope(action, workspace, selected, kind="todo-projection", page=page, projection=True, sources=[str(item.get("documentId")) for item in tasks])
    if action == "maintenance.status":
        result = await bridge.call("scoped_status", {"owner": owner or "local", "workspace_id": workspace}, timeout=30)
        return _envelope(action, workspace, result, kind="maintenance")
    operations = await bridge.call("ops", {"owner": owner or "local", "workspace_id": workspace, "limit": args["limit"]}, timeout=30)
    return _envelope(action, workspace, operations, kind="maintenance")

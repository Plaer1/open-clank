"""Owner-qualified, read-only projection of managed engine orchestration state."""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit


class ProjectionError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class SessionBinding:
    host_chat_id: str
    engine_session_id: str
    map_revision: int
    binding_revision: int


def session_http_path(session_id: str, suffix: str, workspace: str) -> str:
    """Build a session URL without allowing params= to erase read filters."""
    parsed = urlsplit(suffix.lstrip("/"))
    if parsed.scheme or parsed.netloc or parsed.fragment or any(part == ".." for part in parsed.path.split("/")):
        raise ValueError("invalid managed session API path")
    endpoint = parsed.path.strip("/")
    if not endpoint:
        raise ValueError("managed session API path is required")
    params = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "directory"]
    params.append(("directory", workspace))
    return f"/session/{quote(session_id, safe='')}/{endpoint}?{urlencode(params, doseq=True)}"


def orchestration_read_suffix(suffix: str) -> tuple[str, str]:
    """Allow only the existing GET surfaces needed for child inspection."""
    parsed = urlsplit(suffix.lstrip("/"))
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path not in {"actors", "task", "message"}:
        raise ValueError("unsupported orchestration read endpoint")
    allowed = {"actors": set(), "task": set(), "message": {"limit", "agent_id", "before"}}[parsed.path]
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key not in allowed for key, _ in query) or len({key for key, _ in query}) != len(query):
        raise ValueError("unsupported orchestration read query")
    return parsed.path, parsed.query


def find_direct_peer(children: Any, root_session_id: str, child_session_id: str) -> dict[str, Any] | None:
    """Accept a child only when returned by the native root children read."""
    if not isinstance(children, list):
        return None
    return next((child for child in children if isinstance(child, dict)
                 and child.get("id") == child_session_id
                 and child.get("parentID") == root_session_id), None)


def resolve_binding(data: dict[str, Any], owner: str, chat_id: str, expected_revision: int) -> SessionBinding:
    """Resolve a host chat only from its authenticated owner's durable map."""
    if not isinstance(data, dict) or not isinstance(data.get("chats"), dict):
        raise ProjectionError("SESSION_MAP_UNAVAILABLE", "Owner session map is unavailable", 503)
    revision = data.get("mapRevision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ProjectionError("SESSION_MAP_INVALID", "Owner session map revision is invalid", 503)
    if revision != expected_revision:
        raise ProjectionError("STALE_MAP", "Session map revision is stale", 409)
    quarantine = data.get("quarantine", [])
    if isinstance(quarantine, list) and chat_id in quarantine:
        raise ProjectionError("CHAT_QUARANTINED", "Host chat mapping is quarantined", 503)
    entry = data["chats"].get(chat_id)
    if not isinstance(entry, dict) or entry.get("owner") != owner:
        raise ProjectionError("CHAT_NOT_MAPPED", "Host chat is not mapped for this owner", 404)
    current = entry.get("current")
    if not isinstance(current, str) or not current:
        raise ProjectionError("SESSION_MAP_INVALID", "Mapped engine session is invalid", 503)
    binding_revision = entry.get("revision", 0)
    if isinstance(binding_revision, bool) or not isinstance(binding_revision, int) or binding_revision < 0:
        raise ProjectionError("SESSION_MAP_INVALID", "Mapped session revision is invalid", 503)
    return SessionBinding(chat_id, current, revision, binding_revision)


def encode_cursor(native_cursor: str | None, binding: SessionBinding, actor_id: str, filter_key: str,
                  actor_session_id: str | None = None) -> str | None:
    if not native_cursor:
        return None
    raw = json.dumps({"before": native_cursor, "chat": binding.host_chat_id,
                      "map": binding.map_revision, "actor": actor_id,
                      "filter": filter_key, "actorSession": actor_session_id or ""}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None, binding: SessionBinding, actor_id: str, filter_key: str,
                  actor_session_id: str | None = None) -> str | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
    except Exception as exc:
        raise ProjectionError("INVALID_CURSOR", "Invalid orchestration cursor") from exc
    expected = {"chat": binding.host_chat_id, "map": binding.map_revision,
                "actor": actor_id, "filter": filter_key, "actorSession": actor_session_id or ""}
    if not isinstance(value, dict):
        raise ProjectionError("INVALID_CURSOR", "Invalid orchestration cursor")
    if any(value.get(key) != item for key, item in expected.items()) or not isinstance(value.get("before"), str) or not value["before"]:
        raise ProjectionError("CURSOR_SCOPE_MISMATCH", "Cursor belongs to a different chat, map revision, actor, or filter")
    return value["before"]


def validate_actors(value: Any, expected_session_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(actor, dict) or not isinstance(actor.get("actorID"), str)
        or not isinstance(actor.get("sessionID"), str) or actor.get("sessionID") != expected_session_id
        for actor in value
    ):
        raise ProjectionError("ENGINE_INVALID_RESPONSE", "Managed engine returned an invalid or cross-session actor registry", 502)
    return value


def validate_tasks(value: Any, expected_session_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(task, dict) or not isinstance(task.get("id"), str)
        or task.get("session_id") != expected_session_id
        for task in value
    ):
        raise ProjectionError("ENGINE_INVALID_RESPONSE", "Managed engine returned an invalid or cross-session task registry", 502)
    return value


def sanitize_actor(actor: dict[str, Any]) -> dict[str, Any]:
    """Keep operational actor fields while excluding provider grant identity."""
    result = dict(actor)
    context = actor.get("hostContext")
    if isinstance(context, dict):
        safe_context = {key: context[key] for key in ("chatID", "workspaceID", "goalID") if key in context}
        if safe_context:
            result["hostContext"] = safe_context
        else:
            result.pop("hostContext", None)
    return result


def actor_parent_edges(actors: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"kind": "actor_parent", "sessionId": actor.get("sessionID", ""),
             "parentActorId": actor["parentActorID"], "actorId": actor["actorID"]}
            for actor in actors if actor.get("parentActorID") and actor.get("actorID")]


def task_parent_edges(tasks: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"kind": "task_parent", "sessionId": task.get("session_id", ""),
             "parentTaskId": task["parent_task_id"], "taskId": task["id"]}
            for task in tasks if task.get("parent_task_id") and task.get("id")]


def find_session_actor(actors: list[dict[str, Any]], actor_id: str, session_id: str | None = None) -> dict[str, Any] | None:
    """Membership is established by the native session-scoped actors endpoint."""
    return next((actor for actor in actors if isinstance(actor, dict)
                 and actor.get("actorID") == actor_id
                 and (session_id is None or actor.get("sessionID") == session_id)), None)


def build_projection(binding: SessionBinding, actors: list[dict[str, Any]],
                     tasks: list[dict[str, Any]], workflow_runs: list[dict[str, Any]] | None,
                     workflow_available: bool | None = None) -> dict[str, Any]:
    """Keep actor and task relations typed; workflow availability is explicit."""
    edges = actor_parent_edges(actors) + task_parent_edges(tasks)
    return {
        "hostChatId": binding.host_chat_id,
        "engineSessionId": binding.engine_session_id,
        "mapRevision": binding.map_revision,
        "mappingRevision": binding.binding_revision,
        "actors": actors,
        "actorEdges": actor_parent_edges(actors),
        "tasks": tasks,
        "taskEdges": task_parent_edges(tasks),
        "edges": edges,
        "workflows": {"available": workflow_available,
                      "runs": workflow_runs if workflow_available is True else None,
                      "reason": None if workflow_available is True else
                               "runtime_unavailable" if workflow_available is False else
                               "runtime_availability_unreported"},
        "source": "managed_engine",
    }


# O01-B is a pure projection over pages already read under server authority.
# A source's cursor is wrapped for scope; this facade never invents source progress.
_TASK_OBSERVATION_SOURCES = (
    "native_task", "native_actor", "workflow", "copal", "scheduler", "scheduler_routine", "scheduler_run", "scheduler_wait", "jobs", "memory", "media",
)


@dataclass(frozen=True)
class TaskObservationScope:
    """Server-verified owner and O01 session membership, not client selectors."""

    owner: str
    binding: SessionBinding
    verified_session_ids: tuple[str, ...]
    workspace_id: str | None = None


def _task_observation_cursor_filter(scope: TaskObservationScope, source: str) -> str:
    authority = json.dumps([scope.owner, scope.workspace_id, sorted(scope.verified_session_ids)], separators=(",", ":"))
    return f"task-observation:{source}:{hashlib.sha256(authority.encode()).hexdigest()}"


def decode_task_observation_cursor(scope: TaskObservationScope, source: str, cursor: str | None) -> str | None:
    """Unwrap a native continuation only for the same source and verified scope."""
    if source not in {"native_task", "native_actor"}:
        raise ProjectionError("INVALID_OBSERVATION_SOURCE", "Observation source is not supported")
    project_task_observations(scope, {})  # Validate the server-supplied scope consistently.
    return decode_cursor(cursor, scope.binding, source, _task_observation_cursor_filter(scope, source), scope.binding.engine_session_id)


def project_task_observations(scope: TaskObservationScope, source_pages: dict[str, dict[str, Any]], *,
                              companion: bool = False, connection: str = "unknown",
                              verified_direct_peer: tuple[str, str] | None = None) -> dict[str, Any]:
    """Project bounded O01 pages without reading, mutating, or inferring sources.

    The caller must establish ownership and root/peer membership through O01
    before constructing scope and reading pages. Native task.owner is an actor
    assignment, never the authenticated owner. Other adapters are deliberately
    unavailable until their source owners provide independently verified pages.
    """
    def nonempty(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip()) and value == value.strip()

    def number(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def model_text(value: Any) -> bool:
        return (isinstance(value, str) and bool(value) and value == value.strip() and len(value.encode("utf-8")) <= 200
                and not any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in value))

    if (not isinstance(scope, TaskObservationScope) or not nonempty(scope.owner)
            or not isinstance(scope.binding, SessionBinding)
            or not nonempty(scope.binding.host_chat_id) or not nonempty(scope.binding.engine_session_id)
            or not number(scope.binding.map_revision) or not number(scope.binding.binding_revision)
            or not isinstance(scope.verified_session_ids, tuple) or not scope.verified_session_ids
            or any(not nonempty(value) for value in scope.verified_session_ids)
            or len(set(scope.verified_session_ids)) != len(scope.verified_session_ids)
            or scope.binding.engine_session_id not in scope.verified_session_ids
            or (scope.workspace_id is not None and not nonempty(scope.workspace_id))):
        raise ProjectionError("INVALID_OBSERVATION_SCOPE", "Server-verified observation scope is invalid")
    if verified_direct_peer is not None and (
            not isinstance(verified_direct_peer, tuple) or len(verified_direct_peer) != 2
            or verified_direct_peer[0] not in scope.verified_session_ids
            or verified_direct_peer[1] != scope.binding.engine_session_id
            or verified_direct_peer[0] == verified_direct_peer[1]):
        raise ProjectionError("INVALID_OBSERVATION_SCOPE", "Direct peer parent proof is invalid")
    if not isinstance(source_pages, dict) or any(key not in {"native_task", "native_actor"} for key in source_pages):
        raise ProjectionError("INVALID_OBSERVATION_SOURCE", "Observation source is not supported")

    pages: dict[str, dict[str, Any]] = {}
    for kind in ("native_task", "native_actor"):
        page = source_pages.get(kind)
        if page is None:
            pages[kind] = {"availability": "unavailable", "reason": "source_not_supplied", "items": []}
            continue
        if not isinstance(page, dict) or set(page) - {"availability", "reason", "observedAt", "nextCursor", "items"}:
            raise ProjectionError("INVALID_OBSERVATION_PAGE", "Observation page is invalid")
        availability = page.get("availability")
        items, observed_at, cursor, reason = page.get("items"), page.get("observedAt"), page.get("nextCursor"), page.get("reason")
        if (availability not in {"available", "partial", "unavailable"} or not isinstance(items, list) or len(items) > 100
                or (observed_at is not None and not number(observed_at))
                or (cursor is not None and (not nonempty(cursor) or len(cursor) > 4096))
                or (reason is not None and (not nonempty(reason) or len(reason) > 256))
                or (availability == "unavailable" and (items or cursor is not None or not reason))
                or (availability in {"available", "partial"} and observed_at is None)
                or (availability == "partial" and not reason)):
            raise ProjectionError("INVALID_OBSERVATION_PAGE", "Observation page availability or bounds are invalid")
        pages[kind] = {"availability": availability, "items": items,
                       **({"reason": reason} if reason else {}),
                       **({"observedAt": observed_at} if observed_at is not None else {}),
                       **({"nextCursor": encode_cursor(cursor, scope.binding, kind,
                           _task_observation_cursor_filter(scope, kind), scope.binding.engine_session_id)} if cursor else {})}

    sessions = set(scope.verified_session_ids)
    actors: dict[tuple[str, str], dict[str, Any]] = {}
    actor_rows = []
    actor_parents: list[tuple[dict[str, Any], str, str | None, str]] = []
    for actor in pages["native_actor"]["items"]:
        if not isinstance(actor, dict):
            raise ProjectionError("INVALID_OBSERVATION_ROW", "Actor observation is invalid")
        session_id, actor_id, status, revision = actor.get("sessionID"), actor.get("actorID"), actor.get("status"), actor.get("executionRevision")
        context = actor.get("hostContext")
        if (not nonempty(session_id) or session_id not in sessions or not nonempty(actor_id)
                or status not in {"pending", "running", "idle"} or not number(revision)
                or (actor.get("lastOutcome") is not None and actor["lastOutcome"] not in {"success", "failure", "cancelled"})
                or not isinstance(actor.get("description"), str)
                or (context is not None and (not isinstance(context, dict)
                    or (context.get("chatID") is not None and context["chatID"] != scope.binding.host_chat_id)
                    or (scope.workspace_id is not None and context.get("workspaceID") is not None
                        and context["workspaceID"] != scope.workspace_id)))):
            raise ProjectionError("INVALID_OBSERVATION_ROW", "Actor observation is outside the verified scope or invalid")
        display = actor.get("display")
        if display is not None:
            if isinstance(display,dict) and set(display) == {"ordinal","label","revision","origin"}:
                display = {**display, "renameAvailable":False}
            try:
                display_bytes = len(display.get("label", "").encode("utf-8")) if isinstance(display,dict) and isinstance(display.get("label"),str) else 0
            except UnicodeError:
                display_bytes = 0
            if (not isinstance(display,dict) or set(display) != {"ordinal","label","revision","origin","renameAvailable"}
                    or type(display["ordinal"]) is not int or not 0 < display["ordinal"] <= 9007199254740991
                    or type(display["revision"]) is not int or not 0 <= display["revision"] <= 9007199254740991
                    or not isinstance(display["label"],str) or not display["label"].strip()
                    or not 1 <= display_bytes <= 200 or any(ord(ch)<32 or 127<=ord(ch)<=159 for ch in display["label"])
                    or display["origin"] not in ("deterministic","helper","manual") or type(display["renameAvailable"]) is not bool):
                raise ProjectionError("INVALID_OBSERVATION_ROW", "Actor label is invalid")
        # Optional parent/model fields follow lastOutcome/display above: absent is fine, present-but-malformed rejects the row.
        parent_id, requested, effective = actor.get("parentActorID"), actor.get("requestedModel"), actor.get("effectiveModel")
        if ((parent_id is not None and not nonempty(parent_id)) or (requested is not None and not model_text(requested))
                or (effective is not None and (not isinstance(effective, dict) or set(effective) != {"providerID", "modelID"}
                    or not model_text(effective["providerID"]) or not model_text(effective["modelID"])))):
            raise ProjectionError("INVALID_OBSERVATION_ROW", "Actor parent or model is invalid")
        key = (session_id, actor_id)
        if key in actors:
            raise ProjectionError("INVALID_OBSERVATION_ROW", "Duplicate actor observation")
        source_ref = {"kind": "native_actor", "sessionId": session_id, "nativeId": actor_id, "executionRevision": revision}
        row = {"sourceRef": source_ref, "owner": scope.owner, "hostChatId": scope.binding.host_chat_id,
               **({"workspaceId": scope.workspace_id} if scope.workspace_id else {}),
               "displayCategory": "execution", "nativeStatus": status, "title": display["label"] if display else actor["description"][:200],
               **({"actorDisplay":display} if display else {}),
               **({"nativeOutcome": actor["lastOutcome"]} if actor.get("lastOutcome") is not None else {}),
               **({"requestedModel": requested} if requested is not None else {}),
               **({"effectiveModel": {"providerID": effective["providerID"], "modelID": effective["modelID"]}} if effective is not None else {}),
               "sourceRevision": revision, "observedAt": pages["native_actor"].get("observedAt"),
               "capabilities": ["inspect"], "linkCoverage": "observed", "links": []}
        actors[key] = row
        actor_rows.append(row)
        if parent_id is not None:
            parent_session = session_id
            provenance = "native_actor.parentActorID"
            if actor.get("mode") == "peer":
                parent_session = None
                if verified_direct_peer and session_id == actor_id == verified_direct_peer[0]:
                    parent_session = verified_direct_peer[1]
                    provenance = "native_actor.parentActorID+native_session.parentID"
            actor_parents.append((row, parent_id, parent_session, provenance))

    # Same-session parents can appear later on the page. A cross-session
    # parent uses the verified Native session edge for this canonical peer.
    for row, parent_id, parent_session, provenance in actor_parents:
        parent_loaded = (parent_session, parent_id) in actors
        if not parent_loaded:
            row["linkCoverage"] = "unknown"
        if parent_loaded or (parent_session and parent_session != row["sourceRef"]["sessionId"]):
            row["links"].append({"kind": "actor_parent",
                                 "target": {"kind": "native_actor", "sessionId": parent_session, "nativeId": parent_id},
                                 "provenance": provenance})

    task_rows = []
    task_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    task_parents: list[tuple[dict[str, Any], str]] = []
    seen_tasks: set[tuple[str, str]] = set()
    for task in pages["native_task"]["items"]:
        if not isinstance(task, dict):
            raise ProjectionError("INVALID_OBSERVATION_ROW", "Task observation is invalid")
        session_id, task_id, status = task.get("session_id"), task.get("id"), task.get("status")
        if (not nonempty(session_id) or session_id not in sessions or not nonempty(task_id)
                or status not in {"open", "in_progress", "blocked", "done", "abandoned"}
                or not isinstance(task.get("summary"), str)
                or (task.get("last_event_at") is not None and not number(task["last_event_at"]))
                or (task.get("owner") is not None and not nonempty(task["owner"]))
                or (task.get("parent_task_id") is not None and not nonempty(task["parent_task_id"]))):
            raise ProjectionError("INVALID_OBSERVATION_ROW", "Task observation is outside the verified scope or invalid")
        key = (session_id, task_id)
        if key in seen_tasks:
            raise ProjectionError("INVALID_OBSERVATION_ROW", "Duplicate task observation")
        seen_tasks.add(key)
        assigned = actors.get((session_id, task.get("owner")))
        row = {"sourceRef": {"kind": "native_task", "sessionId": session_id, "nativeId": task_id},
               "owner": scope.owner, "hostChatId": scope.binding.host_chat_id,
               **({"workspaceId": scope.workspace_id} if scope.workspace_id else {}),
               "displayCategory": "commitment", "nativeStatus": status, "title": task["summary"][:200],
               **({"sourceWatermark": task["last_event_at"]} if task.get("last_event_at") is not None else {}),
               "observedAt": pages["native_task"].get("observedAt"), "capabilities": ["inspect"],
               "linkCoverage": "unknown" if task.get("owner") and not assigned else "observed",
               "links": ([{"kind": "assigned_to", "target": {"kind": "native_actor", "sessionId": session_id,
                                                       "nativeId": task["owner"]}, "provenance": "native_task.owner"}]
                         if assigned else [])}
        task_rows.append(row)
        task_by_key[key] = row
        if task.get("parent_task_id") is not None:
            task_parents.append((row, task.get("parent_task_id")))

    # Task parents resolve within the same verified session after all task rows are validated.
    for row, parent_id in task_parents:
        session_id = row["sourceRef"]["sessionId"]
        parent = task_by_key.get((session_id, parent_id))
        if parent is None:
            row["linkCoverage"] = "unknown"
        else:
            row["links"].append({"kind": "task_parent", "target": {"kind": "native_task", "sessionId": session_id, "nativeId": parent_id},
                                 "provenance": "native_task.parent_task_id"})

    pages["native_task"]["items"] = task_rows
    pages["native_actor"]["items"] = actor_rows
    for kind in _TASK_OBSERVATION_SOURCES[2:]:
        pages[kind] = {"availability": "unavailable", "reason": "adapter_not_installed", "items": []}
    result = {"version": 1, "scope": {"owner": scope.owner, "hostChatId": scope.binding.host_chat_id,
            "rootSessionId": scope.binding.engine_session_id, "mapRevision": scope.binding.map_revision,
            "mappingRevision": scope.binding.binding_revision,
            "workspaceAvailability": "available" if scope.workspace_id else "unknown",
            **({"workspaceId": scope.workspace_id} if scope.workspace_id else {})}, "sources": pages}
    if companion is True:
        from src.openclank.companion_projection import project_companion
        result["companion"] = project_companion(scope, result, connection=connection)
    elif companion is not False:
        raise ProjectionError("INVALID_OBSERVATION_OPTION", "Companion option is invalid")
    return result


_SCHEDULER_OBSERVATION_SOURCES = ("scheduler_routine", "scheduler_run", "scheduler_wait")


def project_memory_job_observations(items: list[dict[str, Any]], *, observed_at: int,
                                    next_cursor: str | None) -> dict[str, Any]:
    """Project already-authorized source-linked jobs without source or job payloads."""
    if not isinstance(items, list) or len(items) > 50 or type(observed_at) is not int:
        raise ProjectionError("INVALID_OBSERVATION_PAGE", "Memory job page is invalid")
    result = []
    for row in items:
        if (type(row) is not dict or not isinstance(row.get('id'), str)
                or row.get('kind') not in ('s05_host_user_capture', 'completed_turn_review')
                or row.get('state') not in ('queued', 'active', 'awaiting_review', 'retry_wait', 'succeeded', 'failed_terminal', 'cancelled')
                or not isinstance(row.get('updated_at'), str)):
            raise ProjectionError("INVALID_OBSERVATION_PAGE", "Memory job is invalid")
        result.append({'sourceRef': {'kind': 'memory_job', 'nativeId': row['id']},
            'displayCategory': 'execution', 'nativeStatus': row['state'],
            'title': {'s05_host_user_capture': 'Memory capture'}.get(row['kind'], 'Memory turn review'),
            'sourceWatermark': row['updated_at'], 'capabilities': ['inspect'], 'linkCoverage': 'unknown', 'links': []})
    page = {'availability': 'partial', 'reason': 'source_linked_subset', 'observedAt': observed_at, 'items': result}
    if next_cursor:
        page['nextCursor'] = next_cursor
    return {'version': 1, 'sources': {'memory_job': page}}


def project_copal_observations(items: list[dict[str, Any]], *, revision: str, observed_at: int,
                               next_cursor: str | None) -> dict[str, Any]:
    """Project an already-authorized bounded workspace task page."""
    if not isinstance(items, list) or len(items) > 100 or not isinstance(revision, str) or not revision:
        raise ProjectionError("INVALID_OBSERVATION_PAGE", "Copal page is invalid")
    rows = []
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                or len(item["id"]) > 1024 or type(item.get("checked")) is not bool):
            raise ProjectionError("INVALID_OBSERVATION_PAGE", "Copal task is invalid")
        label = item.get("text") or item.get("label") or "Task"
        if not isinstance(label, str):
            raise ProjectionError("INVALID_OBSERVATION_PAGE", "Copal label is invalid")
        rows.append({"sourceRef": {"kind": "copal", "nativeId": item["id"]},
                     "displayCategory": "commitment", "nativeStatus": "completed" if item["checked"] else "open",
                     "title": label[:200], "sourceWatermark": revision, "capabilities": ["inspect"],
                     "linkCoverage": "unknown", "links": []})
    page = {"availability": "partial" if next_cursor else "available", "items": rows, "observedAt": observed_at}
    if next_cursor:
        page.update(reason="more_pages", nextCursor=next_cursor)
    return {"version": 1, "sources": {"copal": page}}


def _scheduler_cursor_filter(owner: str, workspace_id: str | None, account_id: str, policy_generation: int) -> str:
    return hashlib.sha256(json.dumps([owner, workspace_id, account_id, policy_generation], separators=(",", ":")).encode()).hexdigest()


def decode_scheduler_observation_cursor(owner: str, workspace_id: str | None, source: str, cursor: str | None, *, account_id: str, policy_generation: int) -> str | None:
    """Decode a source-owned continuation only under the same owner/scope."""
    if (source not in _SCHEDULER_OBSERVATION_SOURCES or not isinstance(owner, str) or not owner or owner != owner.strip()
            or not isinstance(account_id, str) or not account_id or type(policy_generation) is not int):
        raise ProjectionError("INVALID_OBSERVATION_SCOPE", "Scheduler observation scope is invalid")
    if workspace_id is not None and (not isinstance(workspace_id, str) or not workspace_id or workspace_id != workspace_id.strip()):
        raise ProjectionError("INVALID_OBSERVATION_SCOPE", "Scheduler observation workspace is invalid")
    if cursor is None:
        return None
    if not isinstance(cursor, str) or len(cursor) > 8192:
        raise ProjectionError("INVALID_CURSOR", "Invalid scheduler cursor")
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except Exception as exc:
        raise ProjectionError("INVALID_CURSOR", "Invalid scheduler cursor") from exc
    if (not isinstance(data, dict) or data.get("version") != 2 or data.get("source") != source
            or data.get("scope") != _scheduler_cursor_filter(owner, workspace_id, account_id, policy_generation)
            or not isinstance(data.get("after"), str) or not data["after"] or len(data["after"]) > 512):
        raise ProjectionError("CURSOR_SCOPE_MISMATCH", "Cursor belongs to a different scheduler source or scope")
    return data["after"]


def project_scheduler_observations(owner: str, workspace_id: str | None, source: str,
                                   items: list[dict[str, Any]], observed_at: int,
                                   next_native_cursor: str | None, *, account_id: str, policy_generation: int) -> dict[str, Any]:
    """Wrap an already-authorized safe scheduler page without inventing progress."""
    decode_scheduler_observation_cursor(owner, workspace_id, source, None, account_id=account_id, policy_generation=policy_generation)
    if (not isinstance(items, list) or len(items) > 100 or type(observed_at) is not int or observed_at < 0
            or (next_native_cursor is not None and (not isinstance(next_native_cursor, str)
                or not next_native_cursor or len(next_native_cursor) > 512))):
        raise ProjectionError("INVALID_OBSERVATION_PAGE", "Scheduler observation page is invalid")
    sources = {kind: {"availability": "unavailable", "reason": "source_not_requested", "items": []}
               for kind in _TASK_OBSERVATION_SOURCES}
    page: dict[str, Any] = {"availability": "partial", "reason": "legacy_or_unqualified_excluded",
                            "observedAt": observed_at, "items": items}
    if next_native_cursor:
        raw = json.dumps({"version": 2, "source": source, "scope": _scheduler_cursor_filter(owner, workspace_id, account_id, policy_generation),
                          "after": next_native_cursor}, separators=(",", ":")).encode()
        page.update(reason="more_pages", nextCursor=base64.urlsafe_b64encode(raw).decode().rstrip("="))
    sources[source] = page
    return {"version": 1, "scope": {"owner": owner,
            "workspaceAvailability": "available" if workspace_id else "unknown",
            **({"workspaceId": workspace_id} if workspace_id else {})}, "sources": sources}

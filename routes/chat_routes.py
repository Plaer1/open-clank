"""Chat routes — /api/chat, /api/chat_stream, /api/inject_context, /api/search."""

import asyncio
import json
import os
import re
import time
import logging
import uuid
from datetime import datetime
from typing import Dict, Any, AsyncGenerator, List, Optional

from fastapi import APIRouter, Request, HTTPException, Form, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from core.models import ChatMessage
from src import agent_runs
from src.chat_helpers import coerce_message_and_session
from src.endpoint_resolver import ResolvedModelTarget
from src.model_dispatch import stream_agent_target, stream_chat_target
from src.session_search import search_session_messages
from src.prompt_security import untrusted_context_message
from core.exceptions import SessionNotFoundError
from src.auth_helpers import effective_user, get_current_user
from routes.session_routes import _verify_session_owner
from routes.document_helpers import _owner_session_filter
from core.database import SessionLocal, get_session_mode, set_session_mode
from core.database import Session as DBSession, ChatMessage as DBChatMessage
from core.database import Document as DBDocument
from core.log_safety import redact_url
from routes.research_routes import _resolve_research_endpoint
from routes.prefs_routes import _load_for_user as _load_prefs_for_user
from routes.chat_helpers import (
    build_chat_context,
    save_assistant_response,
    run_post_response_tasks,
    _enforce_chat_privileges,
)
from src.action_intents import classify_tool_intent as _classify_tool_intent
from src.image_model_ids import looks_like_image_generation_model
from src.tool_policy import (
    WEB_TOOL_NAMES,
    build_effective_tool_policy,
    is_web_search_explicitly_denied,
    web_search_enabled_for_turn,
)
from src.openclank.chat_routing import (
    ChatRouteUnavailable,
    MANAGED_ENGINE_PUBLIC_URL,
    resolve_chat_route,
)
from src.openclank.filesystem_registry import FilesystemRegistryError, FilesystemRootRegistry
from src.openclank.file_policy import FilePolicyError, FilePolicyRepository
from src.openclank.workspace_policy_service import (
    WorkspacePolicyServiceError,
    resolve_owned_workspace,
)

logger = logging.getLogger(__name__)

# Track active streams for partial-save safety net
_active_streams: Dict[str, dict] = {}


def _server_approved_plan_state(session_id: str, owner: str) -> dict:
    """Return only the persisted plan state whose revision/digest is approved."""
    try:
        from src.plan_approval import approved_plan_state

        return approved_plan_state(session_id, owner)
    except (KeyError, ValueError, TypeError):
        return {}


def _server_approved_plan(session_id: str, owner: str) -> str:
    """Return only a plan whose persisted revision and digest are approved."""
    plan_state = _server_approved_plan_state(session_id, owner)
    plan = str(plan_state.get("plan") or "").strip()
    if plan:
        return plan[:8192]
    todos = plan_state.get("todos")
    if not isinstance(todos, list):
        return ""
    lines = []
    for todo in todos:
        if isinstance(todo, dict):
            text = str(todo.get("content") or todo.get("title") or todo.get("text") or "").strip()
            status = str(todo.get("status") or "pending").lower()
        else:
            text = str(todo or "").strip()
            status = "pending"
        if text:
            lines.append(f"- [{'x' if status in {'done', 'completed'} else ' '}] {text}")
    return "\n".join(lines)[:8192]


def _resolved_session_target(sess):
    try:
        route = resolve_chat_route(
            owner=getattr(sess, "owner", None),
            endpoint_id=getattr(sess, "endpoint_id", None),
            model_id=getattr(sess, "model", None),
            model_route_id=getattr(sess, "provider_model_route_id", None),
        )
        # The grant is capability metadata, never a credential. It is resolved
        # again by the host callback before every managed attempt.
        sess.provider_grant_id = route.provider_grant_id
        sess.provider_model_route_id = route.model_route_id
        capabilities = dict(route.capabilities or {})
        capabilities.setdefault("chat", True)
        capabilities.setdefault("tools", True)
        capabilities.setdefault("stream", True)
        capabilities.setdefault("auxiliary", True)
        return ResolvedModelTarget(
            transport="acp",
            endpoint_url=MANAGED_ENGINE_PUBLIC_URL,
            model_id=route.runtime_model,
            endpoint_id=route.connection_id,
            provider_id=route.connection_id,
            headers={},
            capabilities=capabilities,
            lifecycle="ephemeral",
        )
    except ChatRouteUnavailable as exc:
        raise HTTPException(400, str(exc)) from exc


def _stream_set(session_id: str, **fields) -> None:
    """Update fields on the active-stream entry for `session_id`, or
    no-op if the entry has already been popped. Using .get() avoids a
    KeyError race between `if x in d` and `d[x]["k"] = v` if a sibling
    finally pops the key in between (which becomes possible the moment
    a coroutine cancellation reaches an inner cleanup before the
    outermost cleanup runs)."""
    rec = _active_streams.get(session_id)
    if rec is None:
        return
    rec.update(fields)


def _agent_error_from_sse(frame: str) -> Optional[dict]:
    """Extract the safe typed payload from one Agent error SSE frame."""
    if not frame.startswith("event: error"):
        return None
    for line in frame.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            payload = json.loads(line[6:])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return {
            key: payload[key]
            for key in (
                "code", "error", "phase", "retryable", "status", "actions", "details"
            )
            if key in payload
        }
    return None


def _transcript_revision(session_id: str) -> int:
    db = SessionLocal()
    try:
        row = db.query(DBSession.transcript_revision).filter(DBSession.id == session_id).first()
        return int(row.transcript_revision or 0) if row else 0
    finally:
        db.close()


def _request_owner_is_admin(request: Request, owner: str | None) -> bool:
    check = getattr(getattr(request.app.state, "auth_manager", None), "is_admin", None)
    if not owner or not callable(check):
        return False
    try:
        return bool(check(owner))
    except Exception:
        return False


def _immutable_account_id(request: Request, owner: str | None) -> str | None:
    """Resolve the immutable subject used by Files ResourceRefs.

    Chat's owner field is intentionally the username for transcript and model
    attribution. Files capabilities are sealed to the account subject, so a
    help attachment must use the auth manager's stable account ID instead.
    """
    username = str(owner or "").strip().lower()
    if not username:
        return None
    auth_manager = getattr(getattr(request, "app", None), "state", None)
    auth_manager = getattr(auth_manager, "auth_manager", None)
    account_id = getattr(auth_manager, "account_id", None)
    try:
        resolved = account_id(username) if callable(account_id) else None
    except Exception:
        resolved = None
    if resolved:
        return str(resolved)
    if os.getenv("AUTH_ENABLED", "true").lower() == "false" and username == "local-installation":
        return "local-installation"
    return None


def _agent_memory_read_allowed(
    prefs: dict,
    *,
    incognito: bool = False,
    no_memory: bool = False,
) -> bool:
    """Resolve the one per-turn read/write memory authority sent to ACP."""
    if incognito or no_memory or not prefs.get("memory_enabled", True):
        return False
    from src.memory_gate import memory_mode

    return memory_mode(prefs) != "off"


def _turn_envelope(
    *,
    session_id: str,
    owner: str | None,
    workspace: str,
    authority_workspace_id: str = "",
    model: str,
    mode: str,
    incognito: bool,
    disabled_tools=(),
    allowed_tools=None,
    forced_tools=(),
    max_tool_calls: int = 0,
    max_rounds: int = 0,
    preset=None,
    active_document=None,
    active_email=None,
    no_memory: bool = False,
    memory_read_allowed: bool = True,
    compare_mode: bool = False,
    is_admin: bool = False,
    active_copal_context: dict | None = None,
    active_copal_help_context: dict | None = None,
    provider_grant_id: str | None = None,
    root_operation_id: str | None = None,
) -> dict:
    document = None
    if active_document is not None:
        document = {
            "id": getattr(active_document, "id", ""),
            "title": getattr(active_document, "title", ""),
            "language": getattr(active_document, "language", ""),
            "version": getattr(active_document, "version_count", 0),
        }
    envelope = {
        "transcript_revision": 0 if incognito else _transcript_revision(session_id),
        "owner": owner or "",
        "workspace": workspace or "",
        "authority_workspace_id": authority_workspace_id or "",
        "model": model,
        "mode": mode or "agent",
        # ACP's provider-native names are an implementation detail.  The
        # public interaction mode is captured per turn; the bridge maps Chat
        # and Agent to build, while Plan maps to plan.
        "provider_mode": "plan" if mode == "plan" else "build",
        "incognito": bool(incognito),
        "no_memory": bool(no_memory),
        "memory_read_allowed": bool(memory_read_allowed),
        "compare_mode": bool(compare_mode),
        "is_admin": bool(is_admin),
        "disabled_tools": sorted(str(name) for name in (disabled_tools or ())),
        "allowed_tools": (
            sorted(str(name) for name in allowed_tools)
            if allowed_tools is not None
            else None
        ),
        "forced_tools": sorted(str(name) for name in (forced_tools or ())),
        "max_tool_calls": max(0, int(max_tool_calls or 0)),
        # max_rounds is NOT mirrored into the envelope: the native loop
        # consumes it directly and no ACP consumer exists (identity Slice 04
        # rule — envelope fields are consumed or absent, never decorative).
        "system_prompt": getattr(preset, "system_prompt", "") if preset else "",
        "persona": getattr(preset, "character_name", "") if preset else "",
        "copal_workspace": str(
            (active_copal_context or {}).get("workspace") or "default"
        ),
        "active_resources": {
            "document": document,
            "email": dict(active_email) if active_email else None,
            "copal": dict(active_copal_context) if active_copal_context else None,
            "copal_help": dict(active_copal_help_context) if active_copal_help_context else None,
        },
    }
    if provider_grant_id:
        envelope["provider_grant_id"] = provider_grant_id
    if root_operation_id:
        # This is minted by the server for one root user turn and persisted
        # with that user message. It is deliberately not derived from the
        # longer-lived chat session ID.
        envelope["root_operation_id"] = root_operation_id
    return envelope


def _parse_active_copal_context(value: object) -> dict | None:
    """Validate the browser's bounded Copal pointer and untrusted fields.

    The pointer is never authorization or content. The Copal read adapter
    revalidates owner/workspace/kind/existence before any tool action.
    """
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "active_copal_context must be JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "active_copal_context must be an object")
    if set(value) != {"workspace", "view", "resourceKind", "resourceId"}:
        raise HTTPException(400, "active_copal_context must contain exactly workspace, view, resourceKind, resourceId")
    workspace = str(value.get("workspace") or "")
    view = value.get("view")
    kind = value.get("resourceKind")
    resource_id = value.get("resourceId")
    if not re.fullmatch(r"^[A-Za-z0-9._-]{1,64}$", workspace):
        raise HTTPException(400, "active_copal_context workspace is invalid")
    if view is not None and view not in {"notes", "wiki", "timeline", "galaxy", "graph", "mind", "bases", "treehouse", "todo"}:
        raise HTTPException(400, "active_copal_context view is invalid")
    if kind is not None and (not isinstance(kind, str) or not re.fullmatch(r"^[a-z][a-z0-9-]{0,63}$", kind)):
        raise HTTPException(400, "active_copal_context resourceKind is invalid")
    if resource_id is not None and (not isinstance(resource_id, str) or not re.fullmatch(r"^[A-Za-z0-9_-]{1,128}$", resource_id)):
        raise HTTPException(400, "active_copal_context resourceId is invalid")
    return {"workspace": workspace, "view": view, "resourceKind": kind, "resourceId": resource_id}


_ACTIVE_COPAL_HELP_FIELDS = frozenset({
    "workspace", "surface", "view", "resourceKind", "resourceId",
    "resourceRef", "pinnedResourceId", "pinnedResourceRef", "pinned", "selection", "baseId", "baseQuery",
    "taskId", "taskQuery", "courseId", "lessonId", "lessonTitle",
})
_ACTIVE_COPAL_HELP_VIEWS = frozenset({
    "notes", "wiki", "timeline", "galaxy", "graph", "mind", "bases",
    "treehouse", "todo", "editor", "files", "tasks",
})


def _parse_active_copal_help_context(value: object) -> dict | None:
    """Validate the visible, one-shot help attachment from the composer.

    This is deliberately a separate contract from ``active_copal_context``.
    The latter is the four-field pointer used by read_copal; help adds only
    bounded UI hints.  No account, path, body, URL or capability is accepted
    from the browser.  Resource identity is revalidated below using the same
    owner-scoped read adapter as the ordinary pointer.
    """
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "active_help_context must be JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "active_help_context must be an object")
    extras = set(value) - _ACTIVE_COPAL_HELP_FIELDS
    if extras:
        raise HTTPException(400, f"active_help_context has unsupported field(s): {', '.join(sorted(extras))}")
    workspace = str(value.get("workspace") or "")
    if not re.fullmatch(r"^[A-Za-z0-9._-]{1,64}$", workspace):
        raise HTTPException(400, "active_help_context workspace is invalid")
    surface = str(value.get("surface") or "").strip().lower()
    view = str(value.get("view") or "").strip().lower()
    if surface not in {"editor", "files", "timeline", "graph", "tasks", "treehouse"}:
        raise HTTPException(400, "active_help_context surface is invalid")
    if view not in _ACTIVE_COPAL_HELP_VIEWS:
        raise HTTPException(400, "active_help_context view is invalid")
    if surface != "files" and (value.get("resourceRef") or value.get("pinnedResourceRef")):
        raise HTTPException(400, "Files resource references require the Files help surface")
    if not isinstance(value.get("pinned", False), bool):
        raise HTTPException(400, "active_help_context pinned is invalid")

    def _bounded_id(name: str, limit: int = 128) -> str | None:
        item = value.get(name)
        if item in (None, ""):
            return None
        if not isinstance(item, str) or not re.fullmatch(r"^[A-Za-z0-9_.:-]{1,%d}$" % limit, item):
            raise HTTPException(400, f"active_help_context {name} is invalid")
        return item

    def _bounded_resource_ref(name: str) -> str | None:
        item = value.get(name)
        if item in (None, ""):
            return None
        # Files ResourceRefs are URL-safe opaque capabilities. They are
        # accepted only for the Files surface and are consumed internally for
        # revalidation; they are removed before context reaches the model.
        if (
            not isinstance(item, str)
            or len(item) > 16_384
            or not re.fullmatch(r"rr1\.[A-Za-z0-9_-]{16,16380}={0,2}", item)
        ):
            raise HTTPException(400, f"active_help_context {name} is invalid")
        return item

    def _bounded_text(name: str, limit: int) -> str | None:
        item = value.get(name)
        if item in (None, ""):
            return None
        if not isinstance(item, str) or len(item) > limit or any(ord(char) < 32 and char not in "\t\n\r" for char in item):
            raise HTTPException(400, f"active_help_context {name} is invalid")
        return item

    result = {
        "workspace": workspace,
        "surface": surface,
        "view": view,
        "resourceKind": _bounded_id("resourceKind", 64),
        "resourceId": _bounded_id("resourceId"),
        "resourceRef": _bounded_resource_ref("resourceRef"),
        "pinnedResourceId": _bounded_id("pinnedResourceId"),
        "pinnedResourceRef": _bounded_resource_ref("pinnedResourceRef"),
        "pinned": bool(value.get("pinned", False)),
        "selection": _bounded_text("selection", 512),
        "baseId": _bounded_id("baseId"),
        "baseQuery": _bounded_text("baseQuery", 512),
        "taskId": _bounded_id("taskId"),
        "taskQuery": _bounded_text("taskQuery", 512),
        "courseId": _bounded_id("courseId"),
        "lessonId": _bounded_id("lessonId"),
        "lessonTitle": _bounded_text("lessonTitle", 200),
    }
    return result


async def _revalidate_active_copal_help_context(
    context: dict | None,
    *,
    active_copal_context: dict | None,
    owner: str | None,
    bridge: object | None = None,
    owner_subject_id: str | None = None,
    files_policy_generation: int | None = None,
) -> dict | None:
    """Keep only help fields whose owner/workspace identity is still live."""
    if not context:
        return None
    if (
        active_copal_context
        and context.get("surface") != "files"
        and context.get("workspace") != active_copal_context.get("workspace")
    ):
        return {**context, "resourceId": None, "resourceRef": None, "pinnedResourceId": None, "pinnedResourceRef": None, "selection": None, "baseId": None, "baseQuery": None, "taskId": None, "taskQuery": None}

    async def _valid_pointer(resource_id: str | None, resource_ref: str | None) -> tuple[bool, str | None]:
        if not resource_id and not resource_ref:
            return False, None
        # Files selections use the same sealed ResourceRef authority as the
        # Files API. Stable public IDs are display identity only; without the
        # current opaque ref they cannot be used to infer a provider record.
        if context["surface"] == "files":
            if not resource_ref or not owner_subject_id:
                return False, None
            try:
                from src.openclank.resource_refs import resolve_resource_ref

                generation = files_policy_generation
                if generation is None:
                    try:
                        generation = int(FilePolicyRepository().generation())
                    except Exception:
                        return False, None
                ref = resolve_resource_ref(
                    resource_ref,
                    expected_owner_subject_id=owner_subject_id,
                    current_policy_generation=int(generation),
                    required_capability="stat",
                )
                if ref.workspace_id and str(ref.workspace_id) != context.get("workspace"):
                    return False, None
                if resource_id and resource_id != ref.stable_id:
                    return False, None
                return True, ref.stable_id
            except Exception:
                logger.info("Files help context ref was stale or revoked", exc_info=True)
                return False, None
        from src.openclank.copal_tools import read_copal
        pointer = {
            "workspace": context["workspace"],
            "view": "todo" if context["surface"] == "tasks" else context["view"],
            "resourceKind": context.get("resourceKind"),
            "resourceId": resource_id,
        }
        # TreeHouse lessons are part of the published state rather than a
        # document. Validate their parent state without treating a lesson id
        # as a document path or a write capability.
        if context["surface"] == "treehouse":
            try:
                result = await read_copal(
                    {"action": "treehouse.get", "workspace": context["workspace"]},
                    owner=owner,
                    bridge=bridge,
                )
                state = ((result or {}).get("data") or {}).get("state") or {}
                activities = (state.get("activities") or {}) if isinstance(state, dict) else {}
                return resource_id in activities, resource_id
            except Exception:
                return False, None
        try:
            validated = await _revalidate_active_copal_context(pointer, owner=owner, bridge=bridge)
            return bool(validated and validated.get("resourceId") == resource_id), resource_id
        except Exception:
            return False, None

    valid_active, active_id = await _valid_pointer(context.get("resourceId"), context.get("resourceRef"))
    valid_pinned, pinned_id = await _valid_pointer(context.get("pinnedResourceId"), context.get("pinnedResourceRef"))
    result = dict(context)
    if not valid_active:
        result.update({"resourceId": None, "resourceRef": None, "selection": None, "baseId": None, "baseQuery": None, "taskId": None, "taskQuery": None})
    else:
        result["resourceId"] = active_id or result.get("resourceId")
        # Opaque capabilities are checked by the server and then deliberately
        # dropped so the model receives a stable public identity, never a
        # replayable Files token.
        result["resourceRef"] = None
    if not valid_pinned:
        result["pinnedResourceId"] = None
        result["pinnedResourceRef"] = None
        result["pinned"] = False
    else:
        result["pinnedResourceId"] = pinned_id or result.get("pinnedResourceId")
        result["pinnedResourceRef"] = None
    if result.get("baseId") and result.get("baseId") != result.get("resourceId"):
        result["baseId"] = None
        result["baseQuery"] = None
    if result.get("taskId") and result.get("taskId") != result.get("resourceId"):
        result["taskId"] = None
    if result.get("surface") != "treehouse" and not result.get("resourceId"):
        result["courseId"] = None
        result["lessonId"] = None
        result["lessonTitle"] = None
    return result


async def _revalidate_active_copal_context(
    context: dict | None,
    *,
    owner: str | None,
    bridge: object | None = None,
) -> dict | None:
    """Revalidate an active Copal pointer before it reaches an agent.

    The browser sends identifiers only.  Shape validation above is not an
    existence or ownership check, so resource-bearing pointers are resolved
    through the same bounded read adapter the agent will use.  Invalid or
    unavailable resources fail closed by retaining only the view/workspace
    projection context; raw ids are never handed to the model as if valid.
    """
    if not context or not context.get("resourceId"):
        return context

    view = context.get("view")
    resource_kind = context.get("resourceKind")
    actions: list[str] = []
    from src.openclank.copal_tools import CopalReadError, read_copal

    if view == "notes" and resource_kind == "note":
        actions = ["notes.get"]
    elif view == "wiki" and resource_kind == "wiki":
        actions = ["wiki.get"]
    elif view == "mind" and resource_kind == "mind-source":
        actions = ["mind.get_outline"]
    elif view == "bases" and resource_kind == "base":
        actions = ["bases.get"]
    elif view == "timeline" and resource_kind == "timeline":
        # Timeline selection can point at either an event or the track
        # registry depending on which control most recently had focus.
        actions = ["timeline.event.get", "timeline.track.get"]
    elif view == "todo" and resource_kind == "todo-projection":
        # Todo selections are a projection over exact note-block IDs rather
        # than a separate document kind.  Validate the bounded projection and
        # retain the ID only when it is present in the current owner scope.
        try:
            result = await read_copal(
                {
                    "action": "todo.list",
                    "workspace": context["workspace"],
                    "limit": 100,
                },
                owner=owner,
                bridge=bridge,
            )
            rows = (result or {}).get("data") or []
            if any(
                isinstance(row, dict) and row.get("id") == context["resourceId"]
                for row in rows
            ):
                return context
        except Exception:
            logger.warning(
                "active Todo context could not be revalidated for %s/%s",
                context.get("workspace"),
                context.get("resourceId"),
                exc_info=True,
            )
        return {**context, "resourceId": None}
    else:
        # Projection-only selections have no canonical stable target to
        # revalidate.  Keep the workspace/view signal but discard the id.
        return {**context, "resourceId": None}

    for action in actions:
        try:
            await read_copal(
                {
                    "action": action,
                    "workspace": context["workspace"],
                    "id": context["resourceId"],
                },
                owner=owner,
                bridge=bridge,
            )
            return context
        except CopalReadError as exc:
            if exc.code != "not_found":
                logger.warning(
                    "active Copal context rejected for %s/%s: %s",
                    context.get("workspace"),
                    context.get("resourceId"),
                    exc.code,
                )
                break
        except Exception:
            # A bridge outage must not turn a browser-supplied id into trusted
            # model context.  The next explicit read can report the outage.
            logger.warning(
                "active Copal context could not be revalidated for %s/%s",
                context.get("workspace"),
                context.get("resourceId"),
                exc_info=True,
            )
            break

    return {**context, "resourceId": None}


def _insert_acp_resource_context(messages: list[dict], label: str, value: str) -> None:
    if not value:
        return
    index = next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
        len(messages),
    )
    messages.insert(index, untrusted_context_message(label, value))


def _message_plain_text(content: Any) -> str:
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return str(content or "")


def _last_user_plain_text(messages: List[Dict[str, Any]]) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            return _message_plain_text(msg.get("content"))
    return ""


def _ensure_current_request_is_latest_user(messages: List[Dict[str, Any]], current_message: str) -> List[Dict[str, Any]]:
    """Defensively keep detached streams grounded on the request that created them."""
    current = str(current_message or "").strip()
    if not current:
        return messages
    latest = _last_user_plain_text(messages).strip()
    if latest == current or current in latest or latest in current:
        return messages
    logger.warning(
        "[chat_stream] latest user context mismatch; appending current request for model call. latest=%r current=%r",
        latest[:120],
        current[:120],
    )
    repaired = list(messages or [])
    repaired.append({"role": "user", "content": current})
    return repaired


_WEB_FOLLOWUP_RE = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:check|try\s+again|look(?:\s+now|\s+it\s+up)?|search(?:\s+now|\s+online|\s+it)?|"
    r"do\s+it|again|approved|approve(?:d)?|yes|ok(?:ay)?|proceed|go\s+ahead|"
    r"send(?:\s+it)?|submit(?:\s+it)?|email(?:\s+them|\s+it)?)\??\s*$",
    re.I,
)
_RECENT_BROWSER_CONTEXT_RE = re.compile(
    r"\b(?:browser|browse|open\s+(?:the\s+)?(?:site|page|url|link)|click|"
    r"fill(?:\s+out)?|submit|send\s+(?:the\s+)?form|contact\s+form|web\s*form|"
    r"form\s+submission|playwright|automation)\b",
    re.I,
)


def _recent_session_text(sess, limit: int = 8, max_chars: int = 2000) -> str:
    history = getattr(sess, "history", None) or getattr(sess, "_history", None) or []
    chunks: List[str] = []
    for msg in history[-limit:]:
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        text = _message_plain_text(content).strip()
        if text:
            chunks.append(text)
    return " ".join(chunks)[-max_chars:]




def _is_contextual_browser_followup(message: str, sess) -> bool:
    """Treat short retry replies as browser tasks when recent context was forms/browser automation."""
    if not message or not _WEB_FOLLOWUP_RE.search(message):
        return False
    return bool(_RECENT_BROWSER_CONTEXT_RE.search(_recent_session_text(sess, limit=12, max_chars=4000)))


def _resolve_request_workspace(request, raw_value, *, owner: str | None = None) -> tuple:
    """Resolve the posted workspace for this request: (workspace, rejected).

    Privilege is checked BEFORE the path ever touches the filesystem. Admins
    retain the legacy vetting path. Standard users may bind only a folder that
    is already inside an assigned, readable agent root; containment is checked
    lexically before vet_workspace() resolves or stats the candidate. Invalid
    non-admin values are dropped uniformly so this request field cannot become
    a host-path oracle.

    vet_workspace rejects non-directories, sensitive roots (.ssh, .gnupg,
    ...), and filesystem roots; on rejection there is no confinement and the
    default tool-path allowlist applies. The rejected value is surfaced so the
    stream can tell an admin client (which believes a workspace is active)
    that it was dropped.
    """
    requested = (raw_value or "").strip()
    if not requested:
        return "", ""
    from src.tool_security import owner_is_admin_or_single_user
    owner = str(owner or get_current_user(request) or "")
    if not owner_is_admin_or_single_user(owner):
        candidate = os.path.abspath(os.path.expanduser(requested))
        try:
            registry = FilesystemRootRegistry()
            assignments = registry.visibility_for_subject(owner)
            visible = [
                item for item in assignments
                if item.get("root", {}).get("kind") == "recursive_directory"
                and "read" in set(item.get("capabilities") or [])
                and "read" in set(item.get("root", {}).get("capabilities") or [])
                and item.get("root", {}).get("enabled")
                and item.get("root", {}).get("availability") == "available"
                and registry._contains(str(item["root"].get("canonical_path") or ""), candidate)
            ]
            if not visible:
                return "", ""
            from src.tool_execution import vet_workspace
            workspace = vet_workspace(candidate) or ""
            if not workspace or not any(
                registry._contains(str(item["root"].get("canonical_path") or ""), workspace)
                for item in visible
            ):
                return "", ""
            # Keep the existing owner AgentBinding requirement: a human app
            # assignment alone cannot mint model-directed file authority.
            agent_scope = registry.agent_scope(owner, active_workspace=workspace, app_visibility=assignments)
            if not agent_scope.get("approved_root_ids"):
                return "", ""
            return workspace, ""
        except (FilesystemRegistryError, OSError, ValueError):
            return "", ""
    from src.tool_execution import vet_workspace
    workspace = vet_workspace(requested) or ""
    return workspace, (requested if not workspace else "")


def _canonical_workspace_path(
    request: Request,
    workspace_id: str,
    *,
    owner: str | None = None,
) -> str:
    """Resolve an opaque Workspace ID to its server-owned path and authority."""
    effective_owner = str(owner or get_current_user(request) or "").strip().lower()
    auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
    try:
        binding = resolve_owned_workspace(
            FilePolicyRepository(),
            workspace_id=workspace_id,
            owner_username=effective_owner,
            auth_manager=auth_manager,
            purpose="agent_workspace",
        )
        return binding.path
    except WorkspacePolicyServiceError:
        return ""


def _selected_chat_workspace_id(
    stored_workspace_id: str | None,
    *,
    supplied: bool,
    requested_workspace_id: str | None,
) -> str:
    """Choose a turn Workspace without letting input override chat state."""
    stored = str(stored_workspace_id or "").strip()
    requested = str(requested_workspace_id or "").strip()
    if stored and supplied and requested != stored:
        raise HTTPException(
            409,
            "This chat is bound to a different Workspace; reload the chat before sending",
        )
    return stored or (requested if supplied else "")


def _resolve_chat_workspace_binding(
    request: Request,
    *,
    selected_workspace_id: str,
    stored_workspace_id: str,
    workspace_id_supplied: bool,
    requested_legacy_workspace: str | None,
    owner: str | None,
) -> tuple[str, str]:
    """Resolve this turn's stable Workspace or vetted legacy path.

    A server-derived stable path may legitimately be a filesystem root; the
    final native-tool door re-resolves its ID and exact cwd. Only the legacy
    raw-path compatibility lane passes through ``vet_workspace`` here.
    """
    if selected_workspace_id:
        canonical_workspace = _canonical_workspace_path(
            request,
            selected_workspace_id,
            owner=owner,
        )
        if not canonical_workspace:
            raise HTTPException(
                409 if stored_workspace_id else 403,
                "Workspace access changed; choose an available Workspace",
            )
        return canonical_workspace, ""
    if not workspace_id_supplied:
        return _resolve_request_workspace(
            request,
            requested_legacy_workspace,
            owner=owner,
        )
    return "", ""


def _clear_orphaned_session_endpoint(sess, owner: str | None = None) -> bool:
    """Return whether a stored normalized route is no longer executable.

    This check is deliberately non-mutating. Revocation/disable is enforced at
    dispatch, while historical session provenance remains readable.
    """
    if not getattr(sess, "provider_model_route_id", None):
        return bool(getattr(sess, "model", "") or getattr(sess, "endpoint_id", None))
    try:
        resolve_chat_route(
            owner=owner or getattr(sess, "owner", None),
            endpoint_id=getattr(sess, "endpoint_id", None),
            model_id=getattr(sess, "model", None),
            model_route_id=getattr(sess, "provider_model_route_id", None),
        )
        return False
    except ChatRouteUnavailable:
        return True
    except Exception:
        logger.warning("Could not validate normalized session route", exc_info=True)
        return False


def _is_image_generation_session(sess, owner: str | None = None) -> bool:
    """Whether the selected normalized route explicitly supports image work."""
    model = (getattr(sess, "model", "") or "").strip()
    if looks_like_image_generation_model(model):
        return True
    try:
        route = resolve_chat_route(
            owner=owner or getattr(sess, "owner", None),
            endpoint_id=getattr(sess, "endpoint_id", None),
            model_id=model,
            model_route_id=getattr(sess, "provider_model_route_id", None),
        )
        return any(operation.startswith("image.") for operation in route.operations)
    except ChatRouteUnavailable:
        return False


def _first_image_attachment(chat_handler, att_ids: List[str], owner: str | None = None) -> Optional[Dict[str, Any]]:
    """Return the first attached image file that this owner can read."""
    upload_handler = getattr(chat_handler, "upload_handler", None)
    if not upload_handler:
        return None
    for att_id in att_ids or []:
        try:
            info = upload_handler.resolve_upload(att_id, owner=owner)
        except Exception as e:
            logger.warning("Failed to resolve image edit upload %s", att_id, exc_info=e)
            continue
        if not info:
            continue
        name = info.get("name") or info.get("original_name") or info.get("id") or ""
        mime = info.get("mime", "")
        try:
            if upload_handler.is_image_file(name, mime):
                return info
        except Exception:
            continue
    return None


def _recover_empty_session_model(sess, session_id: str, owner: str | None = None) -> bool:
    """Repair display fields from the session's stable normalized route ID."""
    current_model = (getattr(sess, "model", "") or "").strip()
    route_id = str(getattr(sess, "provider_model_route_id", None) or "").strip()
    if not route_id:
        return False
    try:
        route = resolve_chat_route(
            owner=owner or getattr(sess, "owner", None),
            endpoint_id=getattr(sess, "endpoint_id", None),
            model_id=current_model or None,
            model_route_id=route_id,
        )
        if (
            current_model == route.provider_model_id
            and getattr(sess, "endpoint_id", None) == route.public_endpoint_id
            and getattr(sess, "endpoint_url", "") == MANAGED_ENGINE_PUBLIC_URL
            and not getattr(sess, "headers", None)
        ):
            return False
        db = SessionLocal()
        try:
            db_session_q = db.query(DBSession).filter(DBSession.id == session_id)
            if owner:
                db_session_q = db_session_q.filter(DBSession.owner == owner)
            db_session = db_session_q.first()
            if db_session:
                db_session.model = route.provider_model_id
                db_session.endpoint_id = route.public_endpoint_id
                db_session.endpoint_url = MANAGED_ENGINE_PUBLIC_URL
                db_session.provider_model_route_id = route.model_route_id
                db_session.headers = {}
                db_session.updated_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()
        sess.model = route.provider_model_id
        sess.endpoint_id = route.public_endpoint_id
        sess.endpoint_url = MANAGED_ENGINE_PUBLIC_URL
        sess.provider_model_route_id = route.model_route_id
        sess.provider_grant_id = route.provider_grant_id
        sess.headers = {}
        logger.info("Repaired normalized route metadata for session %s", session_id)
        return True
    except ChatRouteUnavailable:
        return False
    except Exception as exc:
        logger.warning("Failed to recover normalized route for %s: %s", session_id, exc)
        return False


def _reconcile_selected_route_from_request(
    request: Request,
    sess,
    session_id: str,
    form_data,
    owner: str | None = None,
) -> bool:
    """Apply the model route the browser selected before streaming.

    The frontend creates a pending chat first and only materializes it on first
    send. Startup/default-model refreshes can race with that UI state, so the
    stream request includes the route selected at click/send time. The posted
    URL is compatibility metadata only and can never authorize a network hop.
    """
    selected_model = str(form_data.get("selected_model") or "").strip()
    selected_endpoint_id = str(form_data.get("selected_endpoint_id") or "").strip()
    selected_endpoint_url = str(form_data.get("selected_endpoint_url") or "").strip()
    if not selected_model:
        return False

    if selected_endpoint_url and selected_endpoint_url != MANAGED_ENGINE_PUBLIC_URL:
        raise HTTPException(400, "Selected route did not come from the provider catalogue")
    try:
        route = resolve_chat_route(
            owner=owner or getattr(sess, "owner", None),
            endpoint_id=selected_endpoint_id,
            model_id=selected_model,
        )
    except ChatRouteUnavailable as exc:
        raise HTTPException(400, str(exc)) from exc

    if (
        route.model_route_id == getattr(sess, "provider_model_route_id", None)
        and route.public_endpoint_id == getattr(sess, "endpoint_id", None)
        and route.provider_model_id == getattr(sess, "model", None)
        and getattr(sess, "endpoint_url", "") == MANAGED_ENGINE_PUBLIC_URL
        and not getattr(sess, "headers", None)
    ):
        sess.provider_grant_id = route.provider_grant_id
        return False

    db = SessionLocal()
    try:
        db_query = db.query(DBSession).filter(DBSession.id == session_id)
        if owner:
            db_query = db_query.filter(DBSession.owner == owner)
        db_session = db_query.first()
        if db_session:
            db_session.model = route.provider_model_id
            db_session.endpoint_url = MANAGED_ENGINE_PUBLIC_URL
            db_session.endpoint_id = route.public_endpoint_id
            db_session.provider_model_route_id = route.model_route_id
            db_session.headers = {}
            db_session.updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
    sess.model = route.provider_model_id
    sess.endpoint_url = MANAGED_ENGINE_PUBLIC_URL
    sess.endpoint_id = route.public_endpoint_id
    sess.provider_model_route_id = route.model_route_id
    sess.provider_grant_id = route.provider_grant_id
    sess.headers = {}
    logger.info(
        "Reconciled normalized provider route for %s: model=%r connection=%s",
        session_id,
        route.provider_model_id,
        route.connection_id,
    )
    return True


def _set_user_time_from_request(request: Request) -> None:
    """Copy browser timezone headers into the per-request context.

    This is intentionally ephemeral: it is used only while building prompts
    and running tools for this request. It is not persisted or logged.
    """
    try:
        tz_offset = request.headers.get("x-tz-offset")
        tz_name = request.headers.get("x-tz-name")
        from src.user_time import clear_user_time_context, set_user_tz_name, set_user_tz_offset

        clear_user_time_context()
        if tz_offset is not None:
            set_user_tz_offset(tz_offset)
        if tz_name:
            set_user_tz_name(tz_name)
    except Exception:
        pass


def setup_chat_routes(
    session_manager,
    chat_handler,
    chat_processor,
    memory_manager,
    research_handler,
    upload_handler,
    memory_vector=None,
    webhook_manager=None,
    skills_manager=None,
) -> APIRouter:
    router = APIRouter(tags=["chat"])

    # ------------------------------------------------------------------ #
    # POST /api/chat_stream
    # ------------------------------------------------------------------ #
    @router.post("/api/chat_stream")
    async def chat_stream(request: Request) -> StreamingResponse:
        body = None
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                try:
                    body = await request.json()
                except json.JSONDecodeError as e:
                    raise HTTPException(400, f"Invalid JSON: {e}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"Request parsing error: {e}")

        _set_user_time_from_request(request)

        form_data = await request.form()
        message = form_data.get("message")
        session = form_data.get("session")
        attachments = form_data.get("attachments")
        use_web = form_data.get("use_web")
        use_research = form_data.get("use_research")
        time_filter = form_data.get("time_filter")
        preset_id = form_data.get("preset_id")
        # Issue #3229: API callers send JSON, not FormData.  Read from the
        # JSON body as fallback so callers who send {"allow_bash": true}
        # actually get bash enabled.
        allow_bash = form_data.get("allow_bash") or (body or {}).get("allow_bash")
        allow_web_search = form_data.get("allow_web_search") or (body or {}).get("allow_web_search")
        use_rag = form_data.get("use_rag")
        search_context = form_data.get("search_context")  # pre-fetched web search results (compare mode)
        active_copal_context = _parse_active_copal_context(
            form_data.get("active_copal_context") or (body or {}).get("active_copal_context")
        )
        active_copal_help_context = _parse_active_copal_help_context(
            form_data.get("active_help_context") or (body or {}).get("active_help_context")
        )
        compare_mode = str(form_data.get("compare_mode") or (body or {}).get("compare_mode") or "").lower() == "true"
        incognito = str(form_data.get("incognito") or (body or {}).get("incognito") or "").lower() == "true"
        plan_mode = str(form_data.get("plan_mode") or (body or {}).get("plan_mode") or "").lower() == "true"
        requested_mode = str(form_data.get("mode") or (body or {}).get("mode") or "").lower()
        # "plan" as a mode value is the modern spelling of the legacy
        # plan_mode=true form flag.
        if requested_mode == "plan":
            plan_mode = True
        if requested_mode not in ("", "agent", "chat", "plan"):
            raise HTTPException(400, f"Unsupported mode: {requested_mode!r}")
        # chat = read-only tool surface with no plan machinery; plan = the
        # same surface plus the plan directive and plan files; agent = full
        # tool scope.
        chat_mode = "plan" if plan_mode else (requested_mode or "agent")
        # Capture the stable Workspace assertion now, but resolve it only after
        # session ownership and the stored session binding are known below.
        workspace_id_supplied = (
            "workspace_id" in form_data
            or bool(isinstance(body, dict) and "workspace_id" in body)
        )
        requested_workspace_id = str(
            form_data.get("workspace_id") or (body or {}).get("workspace_id") or ""
        ).strip()
        requested_legacy_workspace = (
            form_data.get("workspace") or (body or {}).get("workspace")
        )
        workspace = ""
        selected_workspace_id = ""
        workspace_rejected = ""
        # Approval is resolved from the owner/session-scoped server state after
        # the session is verified below. Browser-supplied plan text is never
        # trusted as an execution capability.
        approved_plan = ""
        user_requested_agent = True
        _search_enabled = web_search_enabled_for_turn(allow_web_search, use_web)
        _tool_intent = None
        _explicit_web_intent = False
        _explicit_browser_intent = False
        active_doc_id = form_data.get("active_doc_id", "").strip()
        logger.info(f"[doc-inject] chat_mode={chat_mode}, active_doc_id={active_doc_id!r}")

        # Active email reader — when the user has an email open in the UI, the
        # frontend passes its uid/folder/account so "reply", "summarize this",
        # etc. resolve to the real email instead of the agent inventing a
        # fake markdown draft.
        active_email_uid = form_data.get("active_email_uid", "").strip()
        active_email_folder = form_data.get("active_email_folder", "INBOX").strip() or "INBOX"
        active_email_account = form_data.get("active_email_account", "").strip()
        active_email_ctx: Optional[Dict[str, str]] = None
        # Always reset between requests so a stale active-email pointer from
        # a previous turn (different reader closed, different account, etc.)
        # can't leak in when the user has no email open this turn.
        try:
            from src.tool_implementations import clear_active_email
            clear_active_email()
        except Exception:
            pass
        if active_email_uid:
            active_email_ctx = {
                "uid": active_email_uid,
                "folder": active_email_folder,
                "account": active_email_account,
            }
            # Try to enrich with subject + from so the agent's system prompt
            # block can quote them. Best-effort: a stale cache is fine, a
            # missing email just means we pass uid/folder/account only.
            try:
                from routes.email_routes import _read_cache_get, _read_cache_key
                _ck = _read_cache_key(active_email_account or None, active_email_folder, active_email_uid, owner=get_current_user(request))
                _cached_email = _read_cache_get(_ck)
                if _cached_email and isinstance(_cached_email, dict):
                    active_email_ctx["subject"] = str(_cached_email.get("subject") or "")
                    active_email_ctx["from"] = str(
                        _cached_email.get("from_address")
                        or _cached_email.get("from")
                        or _cached_email.get("from_name")
                        or ""
                    )
                    _body_preview = (_cached_email.get("body") or "")[:2000]
                    if _body_preview:
                        active_email_ctx["body_preview"] = _body_preview
            except Exception as _e:
                logger.debug(f"[email-inject] cache enrich skipped: {_e}")
            # Stash so email tools can resolve "this email" without UID guessing.
            try:
                from src.tool_implementations import set_active_email
                set_active_email(
                    uid=active_email_uid,
                    folder=active_email_folder,
                    account=active_email_account or None,
                    subject=active_email_ctx.get("subject"),
                    sender=active_email_ctx.get("from"),
                )
            except Exception as _e:
                logger.debug(f"[email-inject] set_active_email failed: {_e}")
            logger.info(
                "[email-inject] active_email uid=%s folder=%s account=%s subject=%r",
                active_email_uid, active_email_folder, active_email_account or "(default)",
                active_email_ctx.get("subject", ""),
            )

        try:
            # Attachment-only sends: skip the message-required check when the
            # user has attached one or more files (the attachment IS the action).
            _has_atts = (
                bool(body and isinstance(body.get("attachments"), list) and body["attachments"])
                or bool(form_data.get("attachments"))
            )
            message, session = coerce_message_and_session(
                body, message, session, session_manager, allow_empty=_has_atts,
            )
            _tool_intent = _classify_tool_intent(message) if isinstance(message, str) else None
            _explicit_browser_intent = bool(re.search(
                r"\b(?:browser|browse|playwright|click|website|webpage)\b",
                str(message or ""),
                re.IGNORECASE,
            ))
            # Verify ownership AFTER coerce (which may resolve a default session)
            # but BEFORE loading. Prevents cross-user session hijack.
            _verify_session_owner(request, session)
            sess = session_manager.get_session(session)
            owner = effective_user(request)
            if not plan_mode:
                approved_plan = _server_approved_plan(session, owner or "")
            stored_workspace_id = str(
                getattr(sess, "workspace_id", None) or ""
            ).strip()
            selected_workspace_id = _selected_chat_workspace_id(
                stored_workspace_id,
                supplied=workspace_id_supplied,
                requested_workspace_id=requested_workspace_id,
            )
            workspace, workspace_rejected = _resolve_chat_workspace_binding(
                request,
                selected_workspace_id=selected_workspace_id,
                stored_workspace_id=stored_workspace_id,
                workspace_id_supplied=workspace_id_supplied,
                requested_legacy_workspace=requested_legacy_workspace,
                owner=owner,
            )
            active_copal_context = await _revalidate_active_copal_context(
                active_copal_context,
                owner=owner,
                bridge=getattr(request.app.state, "copal_bridge", None),
            )
            active_copal_help_context = await _revalidate_active_copal_help_context(
                active_copal_help_context,
                active_copal_context=active_copal_context,
                owner=owner,
                bridge=getattr(request.app.state, "copal_bridge", None),
                owner_subject_id=_immutable_account_id(request, owner),
            )
            _reconcile_selected_route_from_request(request, sess, session, form_data, owner=owner)
            if _clear_orphaned_session_endpoint(sess, owner=owner):
                raise HTTPException(400, "Selected model endpoint was removed. Pick another model in Settings.")
            # Issue #587: picker shows a model from the endpoint cache but
            # s.model never made it onto the DB row (first-send race after
            # endpoint setup, or a previous endpoint delete/recreate). Pull
            # the first cached model off the matching endpoint so the
            # upstream isn't called with model="" (which surfaces as a
            # generic 401/503).
            _recover_empty_session_model(sess, session, owner=owner)
            if not getattr(sess, "model", "").strip():
                raise HTTPException(
                    400,
                    "No model selected for this chat. Open the model picker and choose one before sending.",
                )
        except SessionNotFoundError as e:
            raise HTTPException(404, str(e))
        except (ValueError, ValidationError):
            raise HTTPException(400, "Invalid request parameters")

        # ------------------------------------------------------------------ #
        # Privilege gates that must fire BEFORE any LLM work / token spend.
        #   1. allowed_models — reject if session.model isn't in the user's
        #      configured allowlist (empty list = "no restriction").
        #   2. max_messages_per_day — count user-role ChatMessage rows owned
        #      by this user in the last UTC day; 429 if at/over the cap.
        # Admins always have full privileges via get_privileges (returns
        # ADMIN_PRIVILEGES wholesale) so this is a no-op for them.
        _enforce_chat_privileges(request, sess)

        # Provider credentials are leased only inside the managed engine. A
        # canonical chat session never reconstructs or persists auth headers.
        sess.headers = {}
        model_target = _resolved_session_target(sess)
        # One root identity spans the main chat operation and every model
        # child/tool operation it launches. It is persisted in the user
        # message metadata by build_chat_context; a session ID is never used
        # as an operation ID because sessions contain many independent roots.
        root_operation_id = f"root_{uuid.uuid4().hex}"

        # Check for research_pending BEFORE mode persist overwrites it
        do_research = str(use_research).lower() == "true"
        if not do_research:
            if get_session_mode(session) == 'research_pending':
                do_research = True
                logger.info(f"Session {session} in research_pending — auto-triggering research")

        att_ids = []
        if body and isinstance(body.get("attachments"), list):
            att_ids = [str(x) for x in body["attachments"]]
        elif attachments:
            try:
                att_ids = [str(x) for x in json.loads(attachments)]
            except Exception as e:
                logger.warning("Failed to parse attachments JSON, ignoring attachments", exc_info=e)

        image_generation_session = _is_image_generation_session(sess, owner=effective_user(request))
        no_memory = str(form_data.get("no_memory", "")).lower() == "true"
        _turn_memory_prefs = _load_prefs_for_user(owner) or {}
        memory_read_allowed = _agent_memory_read_allowed(
            _turn_memory_prefs,
            incognito=incognito,
            no_memory=no_memory,
        )
        pre_context_tool_policy = build_effective_tool_policy(
            last_user_message=message,
        )
        allow_tool_preprocessing = not incognito and not pre_context_tool_policy.block_all_tool_calls

        memory_response = None
        if not pre_context_tool_policy.blocks("manage_memory"):
            is_memory_cmd, _ = memory_manager.process_inline_memory_command(message)
            if is_memory_cmd:
                from src.auth_helpers import require_privilege
                require_privilege(request, "can_manage_memory")
            memory_response = await chat_handler.handle_memory_command(
                sess,
                message,
                owner=owner,
                incognito=incognito,
                no_memory=no_memory,
            )
        if memory_response:
            async def inline_memory_stream():
                yield f'data: {json.dumps({"delta": memory_response})}\n\n'
                yield "data: [DONE]\n\n"

            return StreamingResponse(inline_memory_stream(), media_type="text/event-stream")

        # Build shared context (stream path uses enhanced_message for context preface)
        ctx = await build_chat_context(
            sess, request, chat_handler, chat_processor,
            message=message,
            session_id=session,
            preset_id=preset_id,
            att_ids=att_ids,
            use_web=use_web,
            use_rag=use_rag,
            time_filter=time_filter,
            incognito=incognito,
            no_memory=not memory_read_allowed,
            search_context=search_context,
            compare_mode=compare_mode,
            webhook_manager=webhook_manager,
            use_enhanced_message=True,
            # Skills index only ships when the model can actually call
            # manage_skills (agent mode). In plain chat or incognito the
            # index would be useless / unwanted noise.
            agent_mode=(chat_mode == "agent"),
            allow_tool_preprocessing=allow_tool_preprocessing,
            structured_resources=model_target.transport == "acp",
            root_operation_id=root_operation_id,
            provider_grant_id=getattr(sess, "provider_grant_id", None),
        )

        _research_flags = {"do": do_research}  # Mutable container for generator scope

        # Query active document — prefer explicit ID from frontend, fall back to session lookup
        active_doc = None
        _doc_db = SessionLocal()
        try:
            if active_doc_id:
                logger.info(f"[doc-inject] active_doc_id from frontend: {active_doc_id}")
                # Scope to the caller's documents. The session and in-memory
                # fallbacks below are already owner/session-bound; this
                # explicit-id path looked up by id alone, so a user could
                # inject another user's document by passing its id.
                _doc_q = _doc_db.query(DBDocument).filter(DBDocument.id == active_doc_id)
                active_doc = _owner_session_filter(_doc_q, ctx.user).first()
                if active_doc:
                    doc_session = active_doc.session_id
                    doc_owner = getattr(active_doc, "owner", None)
                    if doc_owner and ctx.user and doc_owner != ctx.user:
                        logger.warning(
                            "[doc-inject] ignoring active_doc_id %s owned by another user",
                            active_doc_id,
                        )
                        active_doc = None
                    else:
                        # NOTE: previously dropped the doc when doc.session_id
                        # != current chat session — but that broke the common
                        # case of "open an email draft from one chat, ask a
                        # different chat to write into it". The frontend only
                        # sends active_doc_id for docs currently visible in
                        # the UI, and we already owner-checked above, so trust
                        # the explicit signal. We just log the mismatch and
                        # re-bind the doc to the current session so future
                        # turns find it via the session-fallback path too.
                        if doc_session and doc_session != session:
                            logger.info(
                                "[doc-inject] cross-session active_doc_id %s (was session %s, now %s) — accepting and rebinding",
                                active_doc_id, doc_session, session,
                            )
                            try:
                                active_doc.session_id = session
                                _doc_db.commit()
                            except Exception as _e:
                                _doc_db.rollback()
                                logger.warning(f"[doc-inject] session rebind failed: {_e}")
                        logger.info(f"[doc-inject] found by ID: title={active_doc.title!r}, lang={active_doc.language!r}, is_active={active_doc.is_active}, content_len={len(active_doc.current_content or '')}")
                else:
                    logger.warning(f"[doc-inject] NOT FOUND by ID {active_doc_id}")
            if not active_doc:
                _email_doc_q = _doc_db.query(DBDocument).filter(
                    DBDocument.session_id == session,
                    DBDocument.is_active == True,
                    DBDocument.language == "email",
                )
                active_doc = _owner_session_filter(_email_doc_q, ctx.user).order_by(DBDocument.updated_at.desc()).first()
                if active_doc:
                    logger.info(f"[doc-inject] found email draft by session fallback: title={active_doc.title!r}")
            if not active_doc:
                _session_doc_q = _doc_db.query(DBDocument).filter(
                    DBDocument.session_id == session,
                    DBDocument.is_active == True
                )
                active_doc = _owner_session_filter(_session_doc_q, ctx.user).order_by(DBDocument.updated_at.desc()).first()
                if active_doc:
                    logger.info(f"[doc-inject] found by session fallback: title={active_doc.title!r}")
            # Last resort: the document the agent itself just created/edited
            # (tracked in-memory by the tool layer). This rescues docs that
            # got orphaned from their session (session_id NULL) — otherwise
            # neither lookup above can associate them with this conversation,
            # so the agent never sees what it just wrote. Guarded so we never
            # leak a doc that belongs to a DIFFERENT session.
            if not active_doc:
                try:
                    from src.agent_tools.document_tools import get_active_document
                    _mem_id = get_active_document()
                    if _mem_id:
                        _mem_q = _doc_db.query(DBDocument).filter(DBDocument.id == _mem_id)
                        cand = _owner_session_filter(_mem_q, ctx.user).first()
                        if cand and (not cand.session_id or cand.session_id == session):
                            active_doc = cand
                            logger.info(f"[doc-inject] found by in-memory active id: title={active_doc.title!r} (session_id={cand.session_id!r})")
                except Exception as _e:
                    logger.debug(f"[doc-inject] in-memory fallback failed: {_e}")
            if not active_doc:
                logger.info(f"[doc-inject] no active doc for session {session}")
            if active_doc:
                _doc_db.expunge(active_doc)
        except Exception as e:
            logger.warning(f"Failed to query active document: {e}")
        finally:
            _doc_db.close()

        # Build disabled-tools set from frontend toggles + user privileges
        disabled_tools = set()
        allowed_tools = None
        # Only disable bash when the caller *explicitly* set it to a falsy
        # value. When unset (None), defer to per-user privilege checks below.
        # Web search is per-turn opt-in: either the chat pre-search setting
        # (`use_web=true`) or agent web toggle (`allow_web_search=true`) must
        # explicitly enable it.
        if allow_bash is not None and str(allow_bash).lower() != "true":
            disabled_tools.add("bash")
        _explicit_web_intent = _explicit_web_intent or bool(_tool_intent and _tool_intent.category == "web")
        if is_web_search_explicitly_denied(allow_web_search) or not _search_enabled:
            disabled_tools.update(WEB_TOOL_NAMES)
        if _explicit_web_intent:
            # A direct lookup/search request should not drift into personal
            # tools or shell fallbacks. It can only use web_search/web_fetch
            # when the request's explicit web setting enabled them.
            disabled_tools.update({
                "bash", "python",
                "search_chats", "manage_skills", "manage_memory", "recall_memory",
                "read_file", "write_file", "edit_file",
                "create_document", "edit_document", "update_document",
                "send_email", "reply_to_email",
                "manage_notes", "manage_calendar", "manage_tasks",
                "api_call",
            })
            if _search_enabled:
                disabled_tools.difference_update(WEB_TOOL_NAMES)
            else:
                disabled_tools.update(WEB_TOOL_NAMES)
        elif _search_enabled:
            disabled_tools.difference_update(WEB_TOOL_NAMES)

        # Nobody/incognito mode: deny tools that would expose the user's
        # persistent memory, past chats, or other identity-linked data.
        if incognito:
            disabled_tools.update({
                "manage_memory",      # persistent memory store
                "recall_memory",      # persistent memory reads
                "search_chats",       # past chat history
                "manage_skills",      # skill presets tied to user
                "create_session",
                "list_sessions",
                "manage_session",
                "send_to_session",
                "chat_with_model",
            })
        if not memory_read_allowed:
            disabled_tools.update({"manage_memory", "recall_memory"})

        # Active email reader open → strip the tools that let the agent drift
        # away from the visible email or skip review. The only allowed compose
        # path is ui_control open_email_reply, which opens the same draft editor
        # as the Reply button with the generated body pre-filled. This prevents
        # the model from falling back to direct SMTP when it botches a draft
        # call, and prevents fake email-shaped documents.
        if active_email_ctx and active_email_ctx.get("uid"):
            disabled_tools.update({
                "create_document",
                "send_email",
                "reply_to_email",
                "mcp__email__send_email",
                "mcp__email__reply_to_email",
            })

        # Enforce per-user privileges
        _privs = {}
        _user = ctx.user
        if _user and hasattr(request.app.state, 'auth_manager') and request.app.state.auth_manager:
            _privs = request.app.state.auth_manager.get_privileges(_user)
        if _privs:
            if not _privs.get("can_use_bash", True):
                disabled_tools.update({"bash", "python", "read_file", "write_file"})
            if not _privs.get("can_use_browser", True):
                disabled_tools.update(_BROWSER_MCP_TOOLS)
            if not _privs.get("can_use_documents", True):
                disabled_tools.update({"create_document", "edit_document", "update_document", "suggest_document"})
            if not _privs.get("can_generate_images", True):
                disabled_tools.add("generate_image")
            if not _privs.get("can_manage_memory", True):
                disabled_tools.update({"manage_memory", "recall_memory", "manage_skills"})
            if not _privs.get("can_use_research", True):
                _research_flags["do"] = False
            if not _privs.get("can_use_agent", True):
                raise HTTPException(403, "Agent access is disabled for this account.")

        # Native OpenCode host-path/process tools never inherit authority from
        # the child process CWD. The current owner/workspace projection exposes
        # only Rust-backed private lifetools and keeps every other file/process
        # name fail-closed.
        from src.tool_security import unavailable_strict_agent_tools
        disabled_tools.update(unavailable_strict_agent_tools(_user, workspace))

        # Global admin disabled tools
        from src.settings import get_setting
        _global_disabled = get_setting("disabled_tools", [])
        if _global_disabled and isinstance(_global_disabled, list):
            disabled_tools.update(_global_disabled)

        # Compare is a server-owned positive read-only capability gate. Client
        # flags can narrow it through disabled_tools, never widen it.
        if compare_mode:
            from src.tool_security import (
                COMPARE_READONLY_TOOLS,
                compare_mode_disabled_tools,
            )

            allowed_tools = set(COMPARE_READONLY_TOOLS)
            disabled_tools.update(compare_mode_disabled_tools())

        # Plan and chat modes are read-only: block every tool not on the
        # read-only allowlist; the ACP envelope enforces the same authority
        # server-side. Plan mode additionally gets the plan directive; chat
        # mode is the same surface with no plan machinery.
        if plan_mode:
            from src.tool_security import plan_mode_disabled_tools
            disabled_tools.update(plan_mode_disabled_tools())
        elif chat_mode == "chat":
            from src.tool_security import chat_mode_disabled_tools
            disabled_tools.update(chat_mode_disabled_tools())

        tool_policy = build_effective_tool_policy(
            disabled_tools=disabled_tools,
            last_user_message=message,
        )
        disabled_tools = tool_policy.all_disabled_names()
        research_blocked_by_policy = bool(
            tool_policy.blocks("trigger_research")
            or tool_policy.blocks("manage_research")
        )
        effective_do_research = bool(
            do_research and _research_flags["do"] and not research_blocked_by_policy
        )

        _effective_mode = 'research' if effective_do_research else chat_mode
        set_session_mode(session, _effective_mode)

        async def stream_with_save() -> AsyncGenerator[str, None]:
            # _effective_mode is read-only here; closure captures it from
            # the outer scope. (Was `nonlocal` but never reassigned.)
            research_sources = None
            web_sources = ctx.web_sources

            # Register active stream for partial-save safety net
            _active_streams[session] = {
                "status": "streaming",
                "partial": "",
                "query": message,
                "is_research": effective_do_research,
                "mode": _effective_mode,
                "root_operation_id": root_operation_id,
            }

            # The client sent a workspace the server refused to bind (deleted
            # folder, file path, sensitive dir, filesystem root). Tell it up
            # front so the UI can clear the pill instead of displaying a
            # confinement that is not actually in effect.
            if workspace_rejected:
                yield f"data: {json.dumps({'type': 'workspace_rejected', 'data': {'path': workspace_rejected}})}\n\n"

            if ctx.preprocessed.attachment_meta:
                yield f"data: {json.dumps({'type': 'attachments', 'data': ctx.preprocessed.attachment_meta})}\n\n"

            # Announce any docs auto-created during preprocess (e.g. fillable
            # PDF → editable markdown) so the editor pane switches to them
            # before the model starts streaming.
            for _opened in ctx.auto_opened_docs:
                yield (
                    f'data: {json.dumps({"type": "doc_update", **_opened})}\n\n'
                )

            if ctx.rag_sources:
                yield f"data: {json.dumps({'type': 'rag_sources', 'data': ctx.rag_sources})}\n\n"

            if web_sources:
                yield f"data: {json.dumps({'type': 'web_sources', 'data': web_sources})}\n\n"

            # Emit which memories were injected into context (captured before stream)
            if ctx.used_memories:
                yield f"data: {json.dumps({'type': 'memories_used', 'data': ctx.used_memories})}\n\n"

            # Run research as a background task (survives page refresh)
            if effective_do_research:
                _r_ep, _r_model, _r_headers = _resolve_research_endpoint(sess)
                _auth_keys = list(_r_headers.keys()) if _r_headers else []
                logger.info(f"Research endpoint resolved: model={_r_model}, endpoint={redact_url(_r_ep)}, auth_keys={_auth_keys}, sess_headers_keys={list(sess.headers.keys()) if isinstance(sess.headers, dict) else type(sess.headers)}")

                # Clarification round: only for very short/vague queries on first research message.
                # Skip in compare mode — each pane is a fresh session, so every one would
                # ask clarifying questions and the user would have to answer each pane
                # separately, breaking the parallel comparison.
                _prior_json = research_handler._get_session_json(session)
                _history_len = len(sess.history) if hasattr(sess, 'history') else 0
                _is_first_research = not _prior_json and _history_len <= 2 and not compare_mode

                if _is_first_research:
                    logger.info(f"First research message — asking clarifying questions for: {message[:60]}")
                    yield f'data: {json.dumps({"type": "model_info", "model": sess.model, "suffix": "Research"})}\n\n'
                    # Set DB mode to research_pending so the NEXT message auto-triggers research
                    set_session_mode(session, "research_pending")
                    ctx.messages.insert(0, {"role": "system", "content":
                        "The user wants to start deep web research. Before searching, ask 2-3 brief "
                        "clarifying questions to understand exactly what they want to know. For example: "
                        "what aspects matter most, are they comparing to something, what's their context "
                        "(moving, traveling, curiosity). Be conversational. Keep it short."
                    })
                    _skip_research = True
                else:
                    _skip_research = False

                if not _skip_research:
                    # Phase 2: Start actual research
                    def _on_research_done(_sid, _result, _sources, _findings):
                        """Persist research to DB when background task finishes."""
                        if incognito:
                            return
                        try:
                            _s = session_manager.get_session(_sid)
                            if not _s:
                                logger.warning(f"Session {_sid} expired before research completed")
                                return
                            _md = {"research": True, "model": _s.model}
                            if _sources:
                                _md["research_sources"] = _sources
                            if _findings:
                                _md["research_findings"] = _findings
                            save_assistant_response(
                                _s,
                                session_manager,
                                _sid,
                                _result,
                                _md,
                                research_sources=_sources,
                            )
                            logger.info(f"Research result persisted to DB for session {_sid}")
                        except Exception as _e:
                            logger.error(f"Failed to persist research to DB: {_e}")

                    # Check for prior research to continue from
                    _prior_report = ""
                    _prior_findings = None
                    _prior_urls = None
                    _prior_json = research_handler._get_session_json(session)
                    if _prior_json:
                        _prior_report = _prior_json.get("raw_report", "")
                        _prior_findings = _prior_json.get("raw_findings")
                        _src_urls = {s.get("url", "") for s in (_prior_json.get("sources") or []) if s.get("url")}
                        _prior_urls = _src_urls if _src_urls else None
                        if _prior_report:
                            logger.info(f"Continuing research for session {session} with {len(_src_urls)} prior URLs")

                    # Synthesize conversation into a focused research query
                    _research_query = await research_handler.synthesize_query(
                        sess, message, _r_ep, _r_model, _r_headers,
                    )
                    logger.info(f"Research query: {_research_query[:120]}")

                    research_handler.start_research(
                        session, _research_query, _r_ep, _r_model,
                        llm_headers=_r_headers,
                        prior_report=_prior_report,
                        prior_findings=_prior_findings,
                        prior_urls=_prior_urls,
                        on_complete=_on_research_done,
                        owner=_user,
                    )

                    _heartbeat_counter = 0
                    _last_progress = {}
                    _sent_avg = False
                    while True:
                        status = research_handler.get_status(session)
                        if not status or status["status"] != "running":
                            break
                        progress = status.get("progress", {})
                        if progress and progress != _last_progress:
                            _last_progress = progress
                            if not _sent_avg:
                                _sent_avg = True
                                progress = dict(progress)
                                progress["started_at"] = status.get("started_at")
                                avg = status.get("avg_duration")
                                if avg:
                                    progress["avg_duration"] = avg
                            yield f"data: {json.dumps({'type': 'research_progress', 'data': progress})}\n\n"
                            _heartbeat_counter = 0
                        else:
                            _heartbeat_counter += 1
                            yield f": heartbeat {_heartbeat_counter}\n\n"
                        await asyncio.sleep(1.0)

                    research_sources = research_handler.get_sources(session)
                    if research_sources:
                        yield f"data: {json.dumps({'type': 'research_sources', 'data': research_sources})}\n\n"

                    research_findings = research_handler.get_raw_findings(session)
                    if research_findings:
                        yield f"data: {json.dumps({'type': 'research_findings', 'data': research_findings})}\n\n"

                    # Signal frontend to fetch and render the research result
                    yield f"data: {json.dumps({'type': 'research_done', 'data': {'session_id': session}})}\n\n"
                    yield "data: [DONE]\n\n"
                    research_handler.clear_result(session)
                    _stream_set(session, status="done")
                    _active_streams.pop(session, None)
                    return

            messages = list(_ensure_current_request_is_latest_user(ctx.messages, message))
            if active_copal_context:
                # Copal context is a bounded, server-revalidated pointer.  It
                # must reach classic Agent as well as ACP; the latter has an
                # additional active-document/email envelope path below.
                _insert_acp_resource_context(
                    messages,
                    "active Copal resource pointer",
                    json.dumps(active_copal_context, ensure_ascii=False),
                )
            if active_copal_help_context:
                # Help context is also untrusted metadata.  It is visible to
                # the user before send, consumed once by the composer, and
                # revalidated above; it never contains document bodies/paths.
                _insert_acp_resource_context(
                    messages,
                    "active Copal help context",
                    json.dumps(active_copal_help_context, ensure_ascii=False),
                )
            if model_target.transport == "acp":
                if active_doc is not None:
                    _insert_acp_resource_context(
                        messages,
                        "active document",
                        json.dumps({
                            "id": getattr(active_doc, "id", ""),
                            "title": getattr(active_doc, "title", ""),
                            "language": getattr(active_doc, "language", ""),
                            "content": getattr(active_doc, "current_content", "") or "",
                        }, ensure_ascii=False),
                    )
                if active_email_ctx:
                    _insert_acp_resource_context(
                        messages,
                        "active email",
                        json.dumps(active_email_ctx, ensure_ascii=False),
                    )
                if approved_plan:
                    _insert_acp_resource_context(messages, "approved plan", approved_plan)

            # Auto-compact notification
            if ctx.was_compacted:
                yield f"data: {json.dumps({'type': 'compacted', 'context_length': ctx.context_length})}\n\n"
            if ctx.context_trimmed and not ctx.was_compacted:
                yield f"data: {json.dumps({'type': 'context_trimmed', 'data': {'context_length': ctx.context_length, 'messages_before': ctx.context_messages_before_trim, 'messages_after': ctx.context_messages_after_trim, 'tokens_before': ctx.context_tokens_before_trim, 'tokens_after': ctx.context_tokens_after_trim}})}\n\n"

            full_response = ""
            thinking_response = ""
            last_metrics = None

            # Send model name early so the frontend can show it during streaming
            _model_suffix = "Research" if effective_do_research else None
            _model_info = {"type": "model_info", "model": sess.model}
            if _model_suffix:
                _model_info["suffix"] = _model_suffix
            if ctx.preset.character_name:
                _model_info["character_name"] = ctx.preset.character_name
            yield f'data: {json.dumps(_model_info)}\n\n'

            if _is_image_generation_session(sess, owner=_user):
                from src.settings import get_user_setting
                if tool_policy.blocks("generate_image"):
                    _blocked_msg = tool_policy.reason_for("generate_image")
                    yield f'data: {json.dumps({"delta": _blocked_msg})}\n\n'
                    yield "data: [DONE]\n\n"
                    _active_streams.pop(session, None)
                    return
                if not get_user_setting("image_gen_enabled", _user, True):
                    yield f'data: {json.dumps({"delta": "Image generation is disabled by the administrator."})}\n\n'
                    yield "data: [DONE]\n\n"
                    _active_streams.pop(session, None)
                    return
                from src.ai_interaction import do_edit_image, do_generate_image
                _user_msg = message or ""
                _image_upload = _first_image_attachment(chat_handler, att_ids, owner=_user)
                _image_tool_name = "edit_image" if _image_upload else "generate_image"
                yield f'data: {json.dumps({"type": "tool_start", "tool": _image_tool_name, "command": _user_msg[:100]})}\n\n'
                yield ": heartbeat\n\n"
                _progress_queue: asyncio.Queue = asyncio.Queue()

                async def _image_progress_callback(progress: Dict[str, Any]):
                    try:
                        _progress_queue.put_nowait(progress)
                    except Exception:
                        pass

                if _image_upload:
                    _img_task = asyncio.create_task(do_edit_image(
                        _user_msg,
                        _image_upload.get("path", ""),
                        model_spec=sess.provider_model_route_id,
                        session_id=session,
                        owner=_user,
                        size="1024x1024",
                        progress_callback=_image_progress_callback,
                        root_operation_id=root_operation_id,
                        idempotency_key=f"chat_image_edit_{root_operation_id}",
                        grant_id=getattr(sess, "provider_grant_id", None),
                    ))
                else:
                    _img_task = asyncio.create_task(do_generate_image(
                        f"{_user_msg}\n{sess.provider_model_route_id}\n512x512",
                        session,
                        owner=_user,
                        root_operation_id=root_operation_id,
                        idempotency_key=f"chat_image_generate_{root_operation_id}",
                        grant_id=getattr(sess, "provider_grant_id", None),
                    ))
                _img_started = time.time()
                _img_tick = 0
                while not _img_task.done():
                    try:
                        _progress = await asyncio.wait_for(_progress_queue.get(), timeout=2.0)
                    except asyncio.TimeoutError:
                        _progress = None
                    _img_tick += 1
                    _elapsed = int(time.time() - _img_started)
                    _label = "Editing image" if _image_upload else "Generating image"
                    yield ": image generation still running\n\n"
                    _progress_data = {"type": "tool_progress", "tool": _image_tool_name, "message": f"{_label}… {_elapsed}s", "elapsed": _elapsed, "tick": _img_tick}
                    if isinstance(_progress, dict) and _progress.get("total"):
                        _step = int(_progress.get("step") or 0)
                        _total = int(_progress.get("total") or 0)
                        _percent = _progress.get("percent")
                        _progress_data.update({
                            "step": _step,
                            "total": _total,
                            "percent": _percent,
                            "message": f"{_label}… {_step}/{_total}",
                        })
                    yield f'data: {json.dumps(_progress_data)}\n\n'
                _img_result = await _img_task
                _img_output = _img_result.get("results", _img_result.get("error", ""))
                _img_tool_data = {"type": "tool_output", "tool": _image_tool_name, "command": _user_msg[:100], "output": _img_output, "exit_code": 0 if "error" not in _img_result else 1}
                for _k in ("image_url", "image_id", "image_prompt", "image_model", "image_size", "image_quality"):
                    if _k in _img_result:
                        _img_tool_data[_k] = _img_result[_k]
                if _image_upload:
                    _img_tool_data["source_image"] = {
                        "id": _image_upload.get("id"),
                        "name": _image_upload.get("name") or _image_upload.get("original_name"),
                    }
                yield f'data: {json.dumps(_img_tool_data)}\n\n'
                if _img_result.get("image_url"):
                    _img_event = {"type": "generated_image", "url": _img_result.get("image_url")}
                    for _k in ("image_url", "image_id", "image_prompt", "image_model", "image_size", "image_quality"):
                        if _img_result.get(_k):
                            _img_event[_k] = _img_result[_k]
                    yield f'data: {json.dumps(_img_event)}\n\n'
                _desc = _img_result.get("results", _img_result.get("error", "Image generation complete"))
                full_response = _desc
                yield f'data: {json.dumps({"delta": _desc})}\n\n'
                # Save to session history
                _ev = {"round": 1, "tool": "generate_image", "command": _user_msg[:100], "output": _img_output, "exit_code": 0 if "error" not in _img_result else 1}
                for _ek in ("image_url", "image_id", "image_prompt", "image_model", "image_size", "image_quality"):
                    if _img_result.get(_ek):
                        _ev[_ek] = _img_result[_ek]
                save_assistant_response(
                    sess,
                    session_manager,
                    session,
                    full_response,
                    {"model": sess.model},
                    tool_events=[_ev],
                    incognito=incognito,
                )
                yield f'data: {json.dumps({"type": "metrics", "data": {"total_time": 0}})}\n\n'
                yield "data: [DONE]\n\n"
                _active_streams.pop(session, None)
                return
            else:
                # ── Agent mode: full agent loop with tools ──
                _agent_rounds = 0
                _agent_tool_calls = 0
                _agent_tool_events = []
                _answered_by = None  # set if the selected model failed and a fallback answered
                _requested_model = sess.model
                _actual_model = None
                _agent_error = None
                try:
                    from src.settings import get_setting
                    from src.agent_tools import MAX_AGENT_ROUNDS as _DEFAULT_ROUNDS
                    # Per-message tool budget from settings; guard defensively in
                    # case settings.json was hand-edited to a non-numeric value
                    # (the HTTP admin endpoint validates, but direct edits bypass
                    # it). 0 = unlimited, matching auth_routes set_settings().
                    try:
                        _tool_budget = int(get_setting("agent_max_tool_calls", 0))
                    except (TypeError, ValueError):
                        _tool_budget = 0
                    # Per-message round cap from settings; clamp defensively in
                    # case settings.json was hand-edited to a bad value.
                    try:
                        _max_rounds = int(get_setting("agent_max_rounds", _DEFAULT_ROUNDS) or _DEFAULT_ROUNDS)
                    except (TypeError, ValueError):
                        _max_rounds = _DEFAULT_ROUNDS
                    _max_rounds = max(1, min(_max_rounds, 200))

                    _forced_tools = None
                    if _search_enabled:
                        _forced_tools = set(WEB_TOOL_NAMES)
                        if _explicit_browser_intent:
                            _forced_tools |= set(_BROWSER_MCP_TOOLS)
                    elif _explicit_browser_intent:
                        _forced_tools = set(_BROWSER_MCP_TOOLS)
                    copal_intent = bool(active_copal_context or active_copal_help_context) or bool(re.search(
                        r"\b(?:copal|timeline|wiki|galaxy|graph|mind|bases?|treehouse|meatbag tasks)\b",
                        str(message or ""),
                        re.IGNORECASE,
                    ))
                    if copal_intent:
                        _forced_tools = set(_forced_tools or ()) | {"read_copal"}

                    # Plan approval boundary: the user executed an approved
                    # plan — materialize it into the workspace per the
                    # canonical .clanker/futures/<metaplan>.md convention.
                    # Legacy bindings are translated once at this boundary.
                    # Best-effort; a write
                    # failure never blocks the turn.
                    if approved_plan and workspace:
                        try:
                            from src.plan_files import materialize_plan, metaplan_relpath
                            from src.plan_approval import bind_artifact_path

                            plan_state = _server_approved_plan_state(session, _user)
                            relpath = str(plan_state.get("artifact_relpath") or "").strip()
                            relpath = bind_artifact_path(
                                session,
                                _user,
                                relpath or metaplan_relpath(approved_plan),
                            )
                            materialize_plan(workspace, approved_plan, relative_path=relpath)
                        except Exception as _plan_exc:
                            logger.warning("approved_plan materialize failed: %s", _plan_exc)

                    _agent_envelope = _turn_envelope(
                        session_id=session,
                        owner=_user,
                        workspace=workspace,
                        authority_workspace_id=selected_workspace_id,
                        model=sess.model,
                        mode=chat_mode,
                        incognito=incognito,
                        disabled_tools=disabled_tools,
                        allowed_tools=allowed_tools,
                        forced_tools=_forced_tools,
                        max_tool_calls=_tool_budget,
                        max_rounds=_max_rounds,
                        preset=ctx.preset,
                        active_document=active_doc,
                        active_email=active_email_ctx,
                        no_memory=no_memory,
                        memory_read_allowed=memory_read_allowed,
                        compare_mode=compare_mode,
                        is_admin=_request_owner_is_admin(request, _user),
                        active_copal_context=active_copal_context,
                        active_copal_help_context=active_copal_help_context,
                        provider_grant_id=getattr(sess, "provider_grant_id", None),
                        root_operation_id=root_operation_id,
                    )

                    # stream_agent_target -> run_agent is the one strict Agent
                    # admission door. It owns endpoint-to-ACP projection and
                    # converts expected preflight failures into terminal typed
                    # SSE results. Keeping a second route-level rewrite here
                    # bypassed that contract and turned admissions into 500s.
                    _chunk_source = stream_agent_target(
                        model_target,
                        messages,
                        session_id=session,
                        owner=_user,
                        cwd=workspace or None,
                        supervisor=getattr(request.app.state, "mimo_supervisor", None),
                        temperature=ctx.preset.temperature,
                        max_tokens=ctx.preset.max_tokens,
                        prompt_type=preset_id,
                        max_tool_calls=_tool_budget,
                        max_rounds=_max_rounds,
                        context_length=ctx.context_length,
                        active_document=active_doc,
                        active_email=active_email_ctx,
                        disabled_tools=disabled_tools if disabled_tools else None,
                        tool_policy=tool_policy,
                        plan_mode=plan_mode,
                        approved_plan=approved_plan or None,
                        forced_tools=_forced_tools,
                        turn_envelope=_agent_envelope,
                        uploaded_files=ctx.uploaded_files,
                    )
                    async for chunk in _chunk_source:
                        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                            try:
                                data = json.loads(chunk[6:])
                                if "delta" in data:
                                    # Reasoning tokens arrive flagged thinking:true.
                                    # Forward them for the live indicator, but keep
                                    # them out of the saved reply (same as chat mode).
                                    if data.get("thinking"):
                                        thinking_response += data["delta"]
                                    else:
                                        full_response += data["delta"]
                                        _stream_set(session, partial=full_response)
                                    yield chunk
                                elif data.get("type") == "web_sources":
                                    web_sources = data.get("data", [])
                                    yield chunk
                                elif data.get("type") in (
                                    "tool_start", "tool_progress", "tool_output", "agent_step",
                                    "doc_stream_open", "doc_stream_delta",
                                    "doc_update", "doc_suggestions", "ui_control",
                                    "rounds_exhausted", "budget_exceeded",
                                    "loop_breaker_triggered",
                                    "intent_nudge_exhausted",
                                    "ask_user",
                                    "plan_update",
                                    "permission_request",
                                    "actor_accounting", "actor_accounting_failure",
                                    "user_replay", "usage", "commands_update",
                                    "mode_update", "config_update", "session_info",
                                    "protocol_error", "config_error",
                                ):
                                    if data.get("type") == "agent_step":
                                        _agent_rounds = max(_agent_rounds, data.get("round", 1))
                                    elif data.get("type") == "tool_start":
                                        _agent_tool_calls += 1
                                    elif data.get("type") == "tool_output":
                                        raw = data.get("data") if isinstance(data.get("data"), dict) else {}
                                        raw_input = raw.get("rawInput")
                                        event = {
                                            "id": data.get("id") or raw.get("toolCallId") or raw.get("tool_call_id"),
                                            "round": max(1, _agent_rounds or 1),
                                            "tool": data.get("tool") or raw.get("title") or "tool",
                                            "command": json.dumps(raw_input, ensure_ascii=False) if raw_input else "",
                                            "output": data.get("output") or "",
                                            "exit_code": 0 if data.get("status") == "completed" else 1,
                                            "structured": raw,
                                        }
                                        for block in raw.get("content") or []:
                                            if isinstance(block, dict) and block.get("type") == "diff":
                                                event["diff"] = {
                                                    "file": block.get("path") or "diff",
                                                    "text": block.get("newText") or "",
                                                }
                                                break
                                        _agent_tool_events.append(event)
                                    elif data.get("type") == "ask_user":
                                        _agent_tool_events.append({
                                            "id": data.get("id") or (data.get("data") or {}).get("id"),
                                            "round": max(1, _agent_rounds or 1),
                                            "tool": "question",
                                            "output": "",
                                            "exit_code": 0,
                                            "ask_user": data.get("data") or {},
                                        })
                                    yield chunk
                                elif data.get("type") == "fallback":
                                    # Selected model failed; a fallback answered.
                                    # Forward the notice and remember the real
                                    # model so metrics reflect it, not the masked
                                    # selected model.
                                    _answered_by = data.get("answered_by") or _answered_by
                                    _actual_model = _actual_model or _answered_by
                                    data["selected_model"] = data.get("selected_model") or _requested_model
                                    yield chunk
                                elif data.get("type") == "model_actual":
                                    _actual_model = data.get("model") or _actual_model
                                    data["requested_model"] = _requested_model
                                    yield f'data: {json.dumps(data)}\n\n'
                                elif data.get("type") == "metrics":
                                    last_metrics = data.get("data", {})
                                    _reported_model = last_metrics.get("model")
                                    last_metrics["requested_model"] = last_metrics.get("requested_model") or _requested_model
                                    last_metrics["model"] = _reported_model or _actual_model or _answered_by or _requested_model
                                    if ctx.context_trimmed:
                                        last_metrics["context_trimmed"] = True
                                        last_metrics["context_messages_before_trim"] = ctx.context_messages_before_trim
                                        last_metrics["context_messages_after_trim"] = ctx.context_messages_after_trim
                                        last_metrics["context_tokens_before_trim"] = ctx.context_tokens_before_trim
                                        last_metrics["context_tokens_after_trim"] = ctx.context_tokens_after_trim
                                    yield f'data: {json.dumps({"type": "metrics", "data": last_metrics})}\n\n'
                            except json.JSONDecodeError:
                                yield chunk
                        elif chunk.startswith("event: "):
                            _parsed_error = _agent_error_from_sse(chunk)
                            if _parsed_error is not None:
                                _agent_error = _parsed_error
                                _stream_set(session, status="error", error=_agent_error)
                            yield chunk
                        elif chunk == "data: [DONE]\n\n":
                            _has_tool_events = bool((last_metrics or {}).get("tool_events"))
                            if _agent_error is not None:
                                _metrics_to_save = {
                                    "agent_status": "error",
                                    "error": _agent_error,
                                    "model": _actual_model or _requested_model,
                                    "requested_model": _requested_model,
                                }
                                _response_to_save = f"Agent could not start: {_agent_error.get('error') or 'Unknown admission error.'}"
                                _saved_id = save_assistant_response(
                                    sess,
                                    session_manager,
                                    session,
                                    _response_to_save,
                                    _metrics_to_save,
                                    character_name=ctx.preset.character_name,
                                    incognito=incognito,
                                )
                                if _saved_id:
                                    yield f'data: {json.dumps({"type": "message_saved", "id": _saved_id})}\n\n'
                                _stream_set(session, status="error", error=_agent_error)
                            elif full_response or _agent_tool_events or _has_tool_events:
                                _response_to_save = full_response or "Done."
                                _metrics_to_save = dict(last_metrics or {})
                                if thinking_response.strip() and not _metrics_to_save.get("thinking"):
                                    _metrics_to_save["thinking"] = thinking_response.strip()
                                _saved_id = save_assistant_response(
                                    sess, session_manager, session, _response_to_save, _metrics_to_save,
                                    character_name=ctx.preset.character_name,
                                    web_sources=web_sources,
                                    rag_sources=ctx.rag_sources,
                                    used_memories=ctx.used_memories,
                                    tool_events=_agent_tool_events or None,
                                    incognito=incognito,
                                )
                                if _saved_id:
                                    yield f'data: {json.dumps({"type": "message_saved", "id": _saved_id})}\n\n'
                                    _root_turn_id = str(_metrics_to_save.get("root_turn_id") or "")
                                    if _root_turn_id:
                                        from src.agent_actor_accounting import close_agent_turn

                                        _actor_accounting = close_agent_turn(_root_turn_id, _saved_id)
                                        yield f'data: {json.dumps({"type": "actor_accounting", "data": _actor_accounting})}\n\n'
                                        if _actor_accounting.get("state") == "unavailable":
                                            yield f'data: {json.dumps({"type": "actor_accounting_failure", "data": _actor_accounting})}\n\n'
                                run_post_response_tasks(
                                    sess, session_manager, session, message, _response_to_save,
                                    _metrics_to_save, ctx.uprefs, memory_manager, memory_vector, webhook_manager,
                                    incognito=incognito, compare_mode=compare_mode,
                                    character_name=ctx.preset.character_name,
                                                            agent_rounds=_agent_rounds,
                                    agent_tool_calls=_agent_tool_calls,
                                    skills_manager=skills_manager,
                                    owner=_user,
                                    extract_skills=user_requested_agent,
                                    allow_background_extraction=(not tool_policy.block_all_tool_calls),
                                    memory_provider=chat_processor.memory_provider,
                                    no_memory=no_memory,
                                    root_operation_id=root_operation_id,
                                )
                            if _agent_error is None:
                                _stream_set(session, status="done")
                            yield chunk
                except (asyncio.CancelledError, GeneratorExit):
                    # Client disconnected — save partial response. Wrap
                    # the save in its own try so an exception inside
                    # add_message / save_sessions doesn't mask the
                    # original CancelledError (which prevented the
                    # outer finally from running and left _active_streams
                    # with a stale entry).
                    try:
                        if full_response and not incognito:
                            logger.info("Client disconnected mid-stream for session %s, saving partial response (%d chars)", session, len(full_response))
                            save_assistant_response(
                                sess,
                                session_manager,
                                session,
                                full_response,
                                {
                                    "stopped": True,
                                    "model": _actual_model or _answered_by or _requested_model,
                                    "requested_model": _requested_model,
                                },
                                tool_events=_agent_tool_events or None,
                                incognito=incognito,
                            )
                    except Exception:
                        logger.exception("Failed to save partial response on disconnect (session %s)", session)
                    raise
                finally:
                    _active_streams.pop(session, None)

        async def _safe_stream() -> AsyncGenerator[str, None]:
            """Wrapper that guarantees _active_streams cleanup even if stream_with_save
            raises before reaching a mode-specific finally block."""
            try:
                async for chunk in stream_with_save():
                    yield chunk
            finally:
                _active_streams.pop(session, None)

        # Compare panes are short-lived, single-shot generations whose sessions
        # exist only to drive that one pane — there's nothing to "resume" and
        # the user expects the pane's Stop button (which aborts the fetch,
        # closing this SSE) to promptly cancel the upstream LLM call. Detaching
        # them would keep burning upstream tokens/compute after the pane is
        # stopped or the comparison is abandoned, and would surface a stale
        # "still streaming" /resume target for a session nobody will revisit.
        #
        # So: stream them directly (no agent_runs wrapping). Starlette cancels
        # the underlying async generator (raising CancelledError/GeneratorExit
        # inside it) as soon as it notices the client disconnected — which the
        # mode-specific except blocks above already handle by saving the
        # partial response exactly once. This stops the upstream call promptly
        # without waiting on the next streamed chunk.
        #
        # Normal chat/agent streams keep the DETACHED behavior below: they
        # survive the client closing the tab / navigating away. The SSE response just subscribes (replay
        # buffered output + live); dropping the SSE only removes a subscriber —
        # the run keeps going and saves the assistant message on completion
        # regardless. Reconnect via /api/chat/resume.
        if compare_mode:
            return StreamingResponse(_safe_stream(), media_type="text/event-stream")

        agent_runs.start(session, _safe_stream(), owner=_user)
        return StreamingResponse(agent_runs.subscribe(session), media_type="text/event-stream")

    # The TUI control plane invokes this exact canonical handler in-process.
    # Keeping the callable private to the assembled app avoids a second chat
    # implementation and avoids self-HTTP (which would duplicate auth and
    # leak a device credential into an internal request).
    setattr(router, "openclank_chat_stream_handler", chat_stream)

    # ------------------------------------------------------------------ #
    # GET /api/chat/resume — reconnect to a detached run that's still going
    # (e.g. after reopening a session whose agent kept running in the background)
    # ------------------------------------------------------------------ #
    @router.get("/api/chat/resume/{session_id}")
    async def chat_resume(request: Request, session_id: str) -> StreamingResponse:
        _verify_session_owner(request, session_id)
        if agent_runs.get_status(session_id) is None:
            raise HTTPException(404, "No retained run for this session")
        raw_cursor = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
        try:
            cursor = max(0, int(raw_cursor))
        except ValueError:
            raise HTTPException(400, "Invalid stream cursor")
        return StreamingResponse(
            agent_runs.subscribe(session_id, after_seq=cursor),
            media_type="text/event-stream",
        )

    # ------------------------------------------------------------------ #
    # POST /api/chat/stop — cancel a detached run (Stop button). Closing the SSE
    # no longer stops it (it's detached), so the Stop button must call this.
    # ------------------------------------------------------------------ #
    @router.post("/api/chat/stop/{session_id}")
    async def chat_stop(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        stopped = agent_runs.stop(session_id)
        return {"stopped": stopped}

    # ------------------------------------------------------------------ #
    # POST /api/session/{session_id}/permission — resolve a pending mimo
    # permission prompt (C1). The prompt waits forever server-side; this is
    # the only way a blocked turn proceeds. Lifetime choices are once, chat,
    # workspace, always, or reject.
    # ------------------------------------------------------------------ #
    @router.post("/api/session/{session_id}/permission")
    async def resolve_permission(
        request: Request,
        session_id: str,
        request_id: str = Form(...),
        option_id: str = Form(...),
        secret: str = Form(""),
    ) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        if option_id not in ("once", "chat", "workspace", "always", "reject"):
            raise HTTPException(400, "option_id must be once|chat|workspace|always|reject")
        owner = effective_user(request) or ""
        if request_id.startswith("sudo_perm_"):
            # Sudo credential prompts are a native-shell concern: the secret is
            # handed straight to shell_policy's single-use stash and never
            # touches the supervisor, the grant store, logs, or the response.
            if option_id not in ("once", "reject"):
                raise HTTPException(400, "sudo password requests accept once|reject")
            from src.shell_policy import resolve_sudo_password

            resolved = resolve_sudo_password(
                request_id,
                option_id,
                secret=secret,
                owner=owner,
                session_id=session_id,
            )
            if not resolved:
                raise HTTPException(409, "permission request is stale, foreign, or already answered")
            return {"ok": True}
        sup = getattr(request.app.state, "mimo_supervisor", None)
        if sup and hasattr(sup, "permission_handler_for"):
            handler = sup.permission_handler_for(
                owner,
                request_id=request_id,
            )
        else:
            handler = getattr(sup, "permission_handler", None) if sup else None
        resolved = False
        if handler is not None:
            if hasattr(handler, "resolve_for"):
                resolved = handler.resolve_for(
                    request_id,
                    option_id,
                    owner=owner,
                    session_id=session_id,
                )
            else:
                resolved = handler.resolve(request_id, option_id)
        if not resolved:
            from src.agent_tools.filesystem_tools import resolve_file_approval

            resolved = resolve_file_approval(
                request_id,
                option_id,
                owner=owner,
                session_id=session_id,
            )
        if not resolved:
            from src.shell_policy import resolve_shell_approval

            resolved = resolve_shell_approval(
                request_id,
                option_id,
                owner=owner,
                session_id=session_id,
            )
        if not resolved:
            raise HTTPException(409, "permission request is stale, foreign, or already answered")
        return {"ok": True}

    @router.get("/api/mimo/permission-grants")
    async def list_permission_grants(request: Request) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        sup = getattr(request.app.state, "mimo_supervisor", None)
        if sup and hasattr(sup, "for_owner"):
            await sup.for_owner(owner)
        store = sup.grant_store_for(owner) if sup and hasattr(sup, "grant_store_for") else getattr(sup, "grant_store", None)
        if store is None:
            raise HTTPException(503, "permission grant store unavailable")
        return {"grants": store.list_records(owner=owner)}

    @router.post("/api/mimo/permission-grants/reset")
    async def reset_permission_grants(request: Request) -> Dict[str, Any]:
        """Reset Chat or Workspace-scoped approvals without touching sharing."""
        owner = effective_user(request) or ""
        body = await request.json()
        scope = str(body.get("scope") or "").strip().lower()
        if scope not in {"chat", "workspace"}:
            raise HTTPException(400, "scope must be chat or workspace")
        sup = getattr(request.app.state, "mimo_supervisor", None)
        store = sup.grant_store_for(owner) if sup and hasattr(sup, "grant_store_for") else getattr(sup, "grant_store", None)
        if store is None:
            raise HTTPException(503, "permission grant store unavailable")
        if scope == "chat":
            session_id = str(body.get("session_id") or "").strip()
            if not session_id:
                raise HTTPException(400, "session_id is required for a chat reset")
            _verify_session_owner(request, session_id)
            revoked = store.revoke_scope(owner=owner, session_id=session_id)
            pending = 0
            if sup and hasattr(sup, "permission_handler_for"):
                handler = sup.permission_handler_for(owner)
                if handler and hasattr(handler, "reject_scope"):
                    pending = handler.reject_scope(session_id=session_id)
            from src.agent_tools.filesystem_tools import (
                reject_file_approval_scope,
            )
            from src.shell_policy import reject_shell_approval_scope

            pending += reject_file_approval_scope(
                owner=owner,
                session_id=session_id,
            )
            pending += reject_shell_approval_scope(
                owner=owner,
                session_id=session_id,
            )
        else:
            workspace_id = str(body.get("workspace_id") or "").strip()
            if not workspace_id or len(workspace_id) > 256:
                raise HTTPException(
                    400,
                    "workspace_id is required for a workspace reset",
                )
            repository = FilePolicyRepository()
            auth_manager = getattr(
                getattr(request.app, "state", None), "auth_manager", None
            )
            account_id = (
                auth_manager.account_id(owner)
                if auth_manager is not None
                and callable(getattr(auth_manager, "account_id", None))
                else None
            )
            try:
                workspace_record = repository.get_workspace(workspace_id)
            except FilePolicyError as error:
                raise HTTPException(404, "Workspace was not found") from error
            if (
                not account_id
                or workspace_record.owner_subject_id != str(account_id)
            ):
                raise HTTPException(404, "Workspace was not found")
            legacy_workspace = ""
            try:
                location = repository.get_location(workspace_record.location_id)
                legacy_workspace = os.path.join(
                    location.canonical_path,
                    *(
                        workspace_record.relative_folder.split("/")
                        if workspace_record.relative_folder
                        else ()
                    ),
                )
            except FilePolicyError:
                # The stable identity remains resettable after its Location is
                # removed; only the old raw-path compatibility cleanup is lost.
                pass
            revoked = store.revoke_scope(
                owner=owner,
                workspace=legacy_workspace,
                workspace_id=workspace_id,
            )
            pending = 0
            if sup and hasattr(sup, "permission_handler_for"):
                handler = sup.permission_handler_for(owner)
                if handler and hasattr(handler, "reject_scope"):
                    pending = handler.reject_scope(
                        workspace=legacy_workspace,
                        authority_workspace_id=workspace_id,
                    )
            from src.agent_tools.filesystem_tools import (
                reject_file_approval_scope,
            )
            from src.shell_policy import reject_shell_approval_scope

            pending += reject_file_approval_scope(
                owner=owner,
                workspace=legacy_workspace,
                authority_workspace_id=workspace_id,
            )
            pending += reject_shell_approval_scope(
                owner=owner,
                workspace=legacy_workspace,
                authority_workspace_id=workspace_id,
            )
        return {"ok": True, "scope": scope, "revoked": revoked, "pending_rejected": pending}

    @router.post("/api/session/{session_id}/question")
    async def resolve_mimo_question(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        body = await request.json()
        request_id = str(body.get("request_id") or "")
        rejected = bool(body.get("rejected"))
        answers = body.get("answers")
        owner = effective_user(request) or ""
        sup = getattr(request.app.state, "mimo_supervisor", None)
        if sup and hasattr(sup, "question_handler_for"):
            handler = sup.question_handler_for(
                owner,
                request_id=request_id,
            )
        else:
            handler = getattr(sup, "question_handler", None) if sup else None
        if handler is None:
            raise HTTPException(503, "question handler not available")
        if not handler.resolve(
            request_id,
            owner=owner,
            session_id=session_id,
            answers=answers,
            rejected=rejected,
        ):
            raise HTTPException(409, "question is stale, invalid, or already answered")
        return {"ok": True}

    @router.delete("/api/mimo/permission-grants/{grant_id}")
    async def revoke_permission_grant(request: Request, grant_id: int) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        sup = getattr(request.app.state, "mimo_supervisor", None)
        store = sup.grant_store_for(owner) if sup and hasattr(sup, "grant_store_for") else getattr(sup, "grant_store", None)
        if store is None:
            raise HTTPException(503, "permission grant store unavailable")
        if not store.revoke(grant_id, owner=owner):
            raise HTTPException(404, "permission grant not found")
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # GET /api/chat/stream_status — check if a stream is active for a session
    # ------------------------------------------------------------------ #
    @router.get("/api/chat/stream_status/{session_id}")
    async def chat_stream_status(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        # A detached run can still be going even if _active_streams was popped;
        # report it as active so the client knows to reconnect via /resume.
        # Read once via .get() to avoid a KeyError race between the membership
        # check and the indexed read if a sibling stream's finally pops the
        # entry in between (same pattern _stream_set already uses).
        rec = _active_streams.get(session_id)
        if rec is None:
            if agent_runs.is_active(session_id):
                return {"status": "streaming", "detached": True}
            raise HTTPException(404, "No active stream for this session")
        return rec

    # ------------------------------------------------------------------ #
    # POST /api/inject_context
    # ------------------------------------------------------------------ #
    @router.post("/api/inject_context/{session_id}")
    async def inject_context(request: Request, session_id: str, context: str = Form(...)) -> Dict[str, str]:
        _verify_session_owner(request, session_id)
        try:
            sess = session_manager.get_session(session_id)
            msg = untrusted_context_message("injected research context", f"Research Context: {context}")
            sess.add_message(ChatMessage(msg["role"], msg["content"], metadata=msg.get("metadata")))
            session_manager.save_sessions()
            return {"status": "context_injected"}
        except KeyError:
            raise HTTPException(404, "Session not found")

    # ------------------------------------------------------------------ #
    # GET /api/search — search across chat messages
    # ------------------------------------------------------------------ #
    @router.get("/api/search")
    async def search_messages(
        request: Request,
        q: str = Query("", min_length=0),
        limit: int = Query(20, ge=1, le=100),
    ) -> List[Dict[str, Any]]:
        if not q or not q.strip():
            return []

        _user = effective_user(request)
        return [
            result.to_dict()
            for result in search_session_messages(
                q,
                limit=limit,
                owner=_user,
                restrict_owner=_user is not None,
                include_legacy_owner=False,
            )
        ]

    # ------------------------------------------------------------------ #
    # POST /api/rewrite — lightweight rewrite of last AI message (no tools)
    # ------------------------------------------------------------------ #
    @router.post("/api/rewrite")
    async def rewrite_message(request: Request) -> StreamingResponse:
        """Rewrite the last AI message with an instruction (shorter/simpler/etc).

        Unlike the full chat pipeline, this does NOT run the agent loop or tools.
        It just asks the LLM to rewrite the given text.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        session_id = body.get("session_id")
        original_text = body.get("original_text", "")
        instruction = body.get("instruction", "")

        if not session_id or not original_text or not instruction:
            raise HTTPException(400, "session_id, original_text, and instruction are required")

        _verify_session_owner(request, session_id)

        try:
            sess = session_manager.get_session(session_id)
        except (KeyError, SessionNotFoundError):
            raise HTTPException(404, "Session not found")

        messages = [
            {"role": "system", "content": (
                "You are rewriting a previous response. Follow the instruction exactly. "
                "Output ONLY the rewritten text — no preamble, no explanation, no meta-commentary. "
                "Preserve any formatting (markdown, code blocks, lists) from the original."
            )},
            {"role": "user", "content": (
                f"Here is the original response:\n\n{original_text}\n\n"
                f"Instruction: {instruction}"
            )},
        ]
        rewrite_target = _resolved_session_target(sess)
        rewrite_root_operation_id = f"root_{uuid.uuid4().hex}"
        rewrite_envelope = {
            "root_operation_id": rewrite_root_operation_id,
            "provider_grant_id": getattr(sess, "provider_grant_id", None),
            "allowed_tools": [],
            "memory_read_allowed": False,
            "no_memory": True,
        }

        async def stream_rewrite() -> AsyncGenerator[str, None]:
            full_response = ""
            try:
                async for chunk in stream_chat_target(
                    rewrite_target,
                    messages,
                    session_id=f"rewrite-{rewrite_root_operation_id}",
                    owner=effective_user(request),
                    supervisor=getattr(request.app.state, "mimo_supervisor", None),
                    turn_envelope=rewrite_envelope,
                    temperature=0.7,
                    # 0 = let the server decide (no cap). A hardcoded 4096 made
                    # local reasoning models (Qwen3 / R1) burn the whole budget
                    # inside <think> and emit no rewrite — the bubble just hung
                    # on "Rewriting...". Same fix as the chat max_tokens cap.
                    max_tokens=0,
                    tools=None,
                ):
                    if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                        try:
                            data = json.loads(chunk[6:])
                            if "delta" in data:
                                # Forward the chunk (so the client can show a
                                # thinking indicator) but DON'T fold reasoning
                                # tokens into the saved rewrite — only real
                                # content. reasoning_content arrives flagged
                                # with thinking:true.
                                if not data.get("thinking"):
                                    full_response += data["delta"]
                                yield chunk
                        except json.JSONDecodeError:
                            yield chunk
                    elif chunk.startswith("event: "):
                        yield chunk
                    elif chunk == "data: [DONE]\n\n":
                        # Update the last assistant message in session history.
                        # Strip reasoning-model <think> blocks so the persisted
                        # rewrite is just the rewritten text, not its scratchpad.
                        from src.research_utils import strip_thinking
                        full_response = strip_thinking(full_response).strip() or full_response
                        if full_response:
                            for msg in reversed(sess.history):
                                if (isinstance(msg, ChatMessage) and msg.role == 'assistant') or \
                                   (isinstance(msg, dict) and msg.get('role') == 'assistant'):
                                    if isinstance(msg, ChatMessage):
                                        msg.content = full_response
                                    else:
                                        msg['content'] = full_response
                                    break
                            # Update in DB too
                            db = SessionLocal()
                            try:
                                db_msg = (
                                    db.query(DBChatMessage)
                                    .filter(DBChatMessage.session_id == session_id, DBChatMessage.role == 'assistant')
                                    .order_by(DBChatMessage.timestamp.desc())
                                    .first()
                                )
                                if db_msg:
                                    db_msg.content = full_response
                                    db.commit()
                            except Exception as e:
                                logger.warning("Failed to update rewritten message in DB: %s", e)
                                db.rollback()
                            finally:
                                db.close()
                            session_manager.save_sessions()
                        yield chunk
            except Exception as e:
                logger.error("Rewrite stream error: %s", e)
                yield f'event: error\ndata: {json.dumps({"error": str(e), "status": 500})}\n\n'

        return StreamingResponse(stream_rewrite(), media_type="text/event-stream")

    return router

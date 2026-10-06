# routes/session_routes.py
import os
import re
import html
import json
import uuid
from datetime import datetime
from fastapi import APIRouter, Form, HTTPException, Response, Request
import httpx
import logging

from core.session_manager import SessionManager
from core.models import ChatMessage
from src.request_models import SessionResponse
from core.database import Session as DbSession, SessionLocal, Document, FilesImageResource, utcnow_naive
from src.auth_helpers import effective_user, owner_filter
from src.generated_images import gallery_owner_key
from src.session_image_cleanup import retire_session_image_refs, session_image_refs
from src.session_actions import is_session_recently_active
from src.upload_handler import reserve_message_upload_references
from src.openclank.chat_routing import (
    ChatRouteUnavailable,
    MANAGED_ENGINE_PUBLIC_URL,
    resolve_chat_route,
)
from src.openclank.chat_lifecycle import ChatLifecycleError, ChatLifecycleService
from src.openclank.file_policy import FilePolicyRepository
from src.openclank.workspace_policy_service import (
    WorkspacePolicyServiceError,
    resolve_owned_workspace,
)


def _sanitize_export_filename(name: str) -> str:
    """Return a conservative filename safe for Content-Disposition."""
    name = name if isinstance(name, str) else ""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:128]


# Blind-compare helper sessions are created with this name prefix. Their real
# model must never surface in the session list / sidebar — otherwise a blind
# comparison can be de-anonymized before the user votes (issue #1285).
COMPARE_SESSION_PREFIX = "[CMP] "
_UNVERIFIED_OWNER = object()


def _public_model(name: str, model: str) -> str:
    """Blank out the real model of blind-compare helper sessions so the
    session list can't be used to map a neutral pane label ("Model A") back
    to its model. The Compare UI tracks models client-side, so hiding it here
    costs the sidebar nothing. See issue #1285."""
    if (name or "").startswith(COMPARE_SESSION_PREFIX):
        return ""
    from src.openclank.mimo_projection import public_native_model_id

    return public_native_model_id(model)


def _content_to_text(content) -> str:
    """Flatten a message's content to plain text for text-based exports.

    History entries carry three shapes: a plain string, a multimodal list of
    content blocks (vision/image attachments), or None (assistant turns that
    persisted only native tool_calls). The txt/html/md exporters join and
    string-munge this value, so a list crashed the export (TypeError on join,
    AttributeError on .replace) and None rendered as the literal "None".
    Coerce to the text blocks, returning "" for anything without text.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("text")
        )
    return ""


def _message_role(message) -> str:
    if isinstance(message, ChatMessage):
        return message.role or ""
    if isinstance(message, dict):
        return message.get("role", "") or ""
    return getattr(message, "role", "") or ""


def _message_text(message) -> str:
    if isinstance(message, ChatMessage):
        content = message.content
    elif isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)
    return _content_to_text(content)


def _message_metadata(message) -> dict:
    if isinstance(message, ChatMessage):
        metadata = message.metadata
    elif isinstance(message, dict):
        metadata = message.get("metadata")
    else:
        metadata = getattr(message, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _reject_compact_during_active_run(session_id: str) -> None:
    from src import agent_runs
    if agent_runs.is_active(session_id):
        raise HTTPException(409, "Session has an active run; try compacting after it finishes")


async def _prepare_context_mutation(request: Request, session_id: str, *, session_owner=None,
                                    verified_owner=_UNVERIFIED_OWNER):
    """Reject live mutations and purge stale Open Clank agent execution projections."""
    _reject_compact_during_active_run(session_id)
    # Resolve the persisted owner before purging.  In auth-disabled mode the
    # request has no effective user, but the stored session owner remains the
    # authority for projection cleanup.
    stored_owner = (
        _verify_session_owner(request, session_id)
        if verified_owner is _UNVERIFIED_OWNER else verified_owner
    ) or session_owner
    from src.openclank.transcript_projection import purge_execution_projection

    try:
        await purge_execution_projection(
            getattr(request.app.state, "mimo_supervisor", None),
            session_id,
            owner=stored_owner or effective_user(request) or None,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return stored_owner


def _verify_session_owner(request: Request, session_id: str, session_manager=None):
    """Verify a middleware-authenticated owner matches the stored session."""
    user = effective_user(request)
    if not getattr(request.state, "authenticated", False) or not user:
        raise HTTPException(401, "Authentication required")
    db = SessionLocal()
    try:
        row = db.query(DbSession.owner).filter(DbSession.id == session_id).first()
    finally:
        db.close()
    if row is not None:
        if user and row.owner != user:
            raise HTTPException(404, f"Session {session_id} not found")
        return row.owner
    # No DB row — allow the caller to act on an in-memory ghost they own.
    # Incognito sessions deliberately exist only in this process, so route
    # callers do not all need to thread the manager through manually.
    if session_manager is None:
        session_manager = getattr(
            getattr(getattr(request, "app", None), "state", None),
            "session_manager",
            None,
        )
    if session_manager is not None:
        ghost = getattr(session_manager, "sessions", {}).get(session_id)
        if ghost is not None and (not user or getattr(ghost, "owner", None) == user):
            return getattr(ghost, "owner", None)
    raise HTTPException(404, f"Session {session_id} not found")


def _validated_session_workspace(request: Request, owner: str, workspace_id: str):
    """Resolve a stable chat Workspace through current Agent authority."""
    auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
    try:
        return resolve_owned_workspace(
            FilePolicyRepository(),
            workspace_id=workspace_id,
            owner_username=owner,
            auth_manager=auth_manager,
            purpose="agent_workspace",
        )
    except WorkspacePolicyServiceError as error:
        raise HTTPException(403, "Workspace is unavailable") from error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["sessions"])


_HIDDEN_SYSTEM_SESSION_NAMES = {
    "[Task] Chat Sessions Tidy",
    "[Task] Documents Tidy",
    "[Task] Memory Tidy",
    "[Task] Research Tidy",
    "[Task] Email Mark Boundaries",
    "[Task] Email Tags",
    "[Task] Skills Audit",
}


def setup_session_routes(
    session_manager: SessionManager,
    config: dict,
    webhook_manager=None,
    upload_handler=None,
):
    """Setup session routes with the provided manager and config"""

    REQUEST_TIMEOUT = config.get("REQUEST_TIMEOUT", 20)
    SESSION_MODEL_VALIDATION_TIMEOUT = min(float(REQUEST_TIMEOUT or 20), 3.0)
    OPENAI_API_KEY = config.get("OPENAI_API_KEY")
    SESSIONS_FILE = config.get("SESSIONS_FILE")

    async def _set_chat_archived(request: Request, sid: str, archived: bool):
        _verify_session_owner(request, sid, session_manager)
        owner = effective_user(request)
        service = ChatLifecycleService(
            session_manager=session_manager,
            mimo_supervisor=getattr(request.app.state, "mimo_supervisor", None),
        )
        try:
            await service.set_archived(owner=owner, session_id=sid, archived=archived)
        except ChatLifecycleError as exc:
            if exc.code == "active_run":
                raise HTTPException(409, str(exc)) from exc
            if exc.code == "projection_busy":
                raise HTTPException(503, str(exc)) from exc
            raise HTTPException(404, f"Session {sid} not found") from exc
        return {"status": "archived" if archived else "unarchived"}
    
    @router.get("/sessions")
    def list_sessions(request: Request):
        user = effective_user(request)
        active_incognito_id = str(request.query_params.get("active_incognito_id") or "").strip()
        # Lazy purge: incognito sessions are ephemeral by design — wipe leftovers
        # from the DB and session_manager so they vanish on the next page refresh.
        # BUT: skip sessions that were created within the last 10 minutes.
        # Without that guard, the purge nukes the active "Nobody" session on the
        # very first /api/sessions call after creation, killing the in-flight
        # chat. The frontend's own _cleanupIncognitoSessions handler knows which
        # session is current and won't delete the live one — this server-side
        # purge exists only to catch ghosts the frontend missed (tab close,
        # crash). Only clean up rows old enough to be definitely orphaned.
        try:
            from datetime import timedelta as _td
            _cutoff = utcnow_naive() - _td(minutes=10)
            _purge_db = SessionLocal()
            try:
                from core.database import ChatMessage as _DbMsg
                _ghosts = _purge_db.query(DbSession).filter(
                    DbSession.name.in_(("Nobody", "Incognito")),
                    DbSession.created_at < _cutoff,
                ).all()
                for _g in _ghosts:
                    if active_incognito_id and _g.id == active_incognito_id:
                        continue
                    _purge_db.query(_DbMsg).filter(_DbMsg.session_id == _g.id).delete()
                    _purge_db.delete(_g)
                    if hasattr(session_manager, "delete_session"):
                        try:
                            session_manager.delete_session(_g.id)
                        except Exception:
                            pass
                if _ghosts:
                    _purge_db.commit()
            finally:
                _purge_db.close()
        except Exception:
            pass
        user_sessions = session_manager.get_sessions_for_user(user)
        # Fetch folder info from DB for each session
        db = SessionLocal()
        try:
            folder_map = {}
            token_map = {}
            important_map = {}
            created_map = {}
            updated_map = {}
            last_msg_map = {}
            mode_map = {}
            msg_count_map = {}
            persona_map = {}
            workspace_map = {}
            q = db.query(DbSession.id, DbSession.folder, DbSession.total_input_tokens, DbSession.total_output_tokens, DbSession.is_important, DbSession.created_at, DbSession.updated_at, DbSession.last_message_at, DbSession.mode, DbSession.message_count, DbSession.persona, DbSession.workspace_id).filter(DbSession.archived == False)
            q = owner_filter(q, DbSession, user)
            rows = q.all()
            for row in rows:
                folder_map[row.id] = row.folder
                token_map[row.id] = (row.total_input_tokens or 0) + (row.total_output_tokens or 0)
                important_map[row.id] = row.is_important or False
                created_map[row.id] = row.created_at.isoformat() if row.created_at else None
                updated_map[row.id] = row.updated_at.isoformat() if row.updated_at else None
                # Fall back to updated_at then created_at so sessions that
                # predate the column (or have no messages) still sort sanely.
                last_msg_map[row.id] = (
                    row.last_message_at.isoformat() if row.last_message_at
                    else (row.updated_at.isoformat() if row.updated_at
                          else (row.created_at.isoformat() if row.created_at else None))
                )
                mode_map[row.id] = row.mode
                msg_count_map[row.id] = row.message_count or 0
                workspace_map[row.id] = row.workspace_id
                try:
                    persona_map[row.id] = json.loads(row.persona) if row.persona else None
                except Exception:
                    persona_map[row.id] = None
            # Sessions with active documents that have content
            from sqlalchemy import func
            doc_session_ids = set(
                r[0] for r in owner_filter(
                    db.query(Document.session_id)
                    .filter(Document.is_active == True,
                            Document.current_content != None,
                            func.trim(Document.current_content) != ""),
                    Document, user)
                .distinct().all()
            )
            img_session_ids = {
                str(session_id)
                for (provenance,) in owner_filter(
                    db.query(FilesImageResource.provenance).filter(
                        FilesImageResource.kind == "image",
                        FilesImageResource.is_active.is_(True),
                    ),
                    FilesImageResource,
                    user,
                ).all()
                for session_id in (
                    list((provenance or {}).get("session_ids", []))
                    + [str((provenance or {}).get("session_id") or "")]
                )
                if session_id
            }
        finally:
            db.close()

        sessions = [{"id": s.id, "name": s.name, "model": _public_model(s.name, s.model),
                     "endpoint_url": s.endpoint_url, "endpoint_id": getattr(s, "endpoint_id", None), "rag": s.rag,
                     "archived": s.archived, "folder": folder_map.get(s.id),
                     "total_tokens": token_map.get(s.id, 0),
                     "is_important": important_map.get(s.id, False),
                     "created_at": created_map.get(s.id),
                     "updated_at": updated_map.get(s.id),
                     "last_message_at": last_msg_map.get(s.id),
                     "has_documents": s.id in doc_session_ids,
                     "has_images": s.id in img_session_ids,
                     "mode": mode_map.get(s.id),
                     "message_count": msg_count_map.get(s.id, 0),
                     "workspace_id": workspace_map.get(s.id),
                     "persona": persona_map.get(s.id)}
                    for s in user_sessions.values()
                    if not s.archived
                    and (s.name or "").strip() not in ("Nobody", "Incognito")
                    and (s.name or "").strip() not in _HIDDEN_SYSTEM_SESSION_NAMES]

        return sessions
    
    @router.post("/session", response_model=SessionResponse)
    async def create_session(
        request: Request,
        name: str = Form(""),
        endpoint_url: str = Form(""),
        model: str = Form(""),
        rag: str = Form(None),
        skip_validation: str = Form(None),
        incognito: str = Form(None),
        api_key: str = Form(""),
        endpoint_id: str = Form(""),
        workspace_id: str = Form(""),
    ):
        user = effective_user(request)
        workspace_value = (
            str(workspace_id or "").strip()
            if isinstance(workspace_id, str)
            else ""
        )
        if workspace_value:
            _validated_session_workspace(request, user, workspace_value)
        request_api_key = api_key.strip() if api_key else ""
        if request_api_key:
            raise HTTPException(
                400,
                "Provider credentials must be added through the Providers interface",
            )

        # Empty skip-validation sessions are non-executing placeholders used by
        # email/document workflows. Every executable session must select one
        # exact normalized provider route from /api/models.
        placeholder = (
            str(skip_validation).lower() == "true"
            and not str(endpoint_id or "").strip()
            and not str(model or "").strip()
            and not str(endpoint_url or "").strip()
        )
        selected_route = None
        if placeholder:
            endpoint_url = ""
            endpoint_id = ""
            model_to_use = ""
        else:
            try:
                selected_route = resolve_chat_route(
                    owner=user,
                    endpoint_id=endpoint_id,
                    model_id=model,
                )
            except ChatRouteUnavailable as exc:
                raise HTTPException(400, str(exc)) from exc
            endpoint_url = MANAGED_ENGINE_PUBLIC_URL
            endpoint_id = selected_route.public_endpoint_id
            model_to_use = selected_route.provider_model_id

        auth_manager = getattr(request.app.state, "auth_manager", None)
        get_privileges = getattr(auth_manager, "get_privileges", None)
        privileges = get_privileges(user) if get_privileges and user else {}
        allowed = set((privileges or {}).get("allowed_models") or [])
        restricted = bool((privileges or {}).get("allowed_models_restricted")) or bool(allowed)
        if (privileges or {}).get("block_all_models") or (
            restricted
            and model_to_use not in allowed
            and (
                selected_route is None
                or selected_route.model_route_id not in allowed
            )
        ):
            raise HTTPException(403, f"Your account is not allowed to use model {model_to_use!r}")
        
        sid = str(uuid.uuid4())
        user = effective_user(request)
        is_incognito = str(incognito).lower() == "true"
        if is_incognito:
            from core.models import Session

            session = Session(
                id=sid,
                name=name or "Nobody",
                endpoint_url=endpoint_url or "",
                model=model_to_use,
                endpoint_id=endpoint_id.strip() or None,
                provider_model_route_id=(
                    selected_route.model_route_id if selected_route else None
                ),
                rag=False,
                owner=user,
                incognito=True,
                workspace_id=workspace_value or None,
            )
            session_manager.sessions[sid] = session
        else:
            session = session_manager.create_session(
                session_id=sid,
                name=name or "",
                endpoint_url=endpoint_url or "",
                model=model_to_use,
                rag=str(rag).lower() == "true" if rag else False,
                owner=user,
                endpoint_id=endpoint_id.strip() or None,
                provider_model_route_id=(
                    selected_route.model_route_id if selected_route else None
                ),
                workspace_id=workspace_value or None,
            )
        # Managed provider credentials are leased inside the engine. Session
        # metadata is always secret-free.
        session.headers = {}
        # Fire webhook (sync-safe)
        if webhook_manager and not is_incognito:
            webhook_manager.fire_and_forget("session.created", {
                "session_id": sid, "name": session.name, "model": model_to_use,
            })
        # Fire event for automation tasks
        if not is_incognito:
            from src.event_bus import fire_event
            fire_event("session_created", user)
        return SessionResponse(
            id=sid,
            name=session.name,
            model=model_to_use,
            endpoint_url=endpoint_url or "",
            endpoint_id=endpoint_id.strip() or None,
            rag=False if is_incognito else (str(rag).lower() == "true" if rag else False),
            archived=False,
            workspace_id=workspace_value or None,
        )    
    @router.patch("/session/{sid}")
    async def rename_session(
        request: Request, sid: str,
        name: str = Form(None), folder: str = Form(None),
        model: str = Form(None), endpoint_url: str = Form(None),
        endpoint_id: str = Form(None),
        workspace_id: str = Form(None),
    ):
        _verify_session_owner(request, sid)
        try:
            session = session_manager.get_session(sid)
        except KeyError:
            raise HTTPException(404, f"Session {sid} not found")
        result = {"id": sid}
        workspace_supplied = isinstance(workspace_id, str)
        prior_workspace = getattr(session, "workspace_id", None)
        if workspace_supplied:
            workspace_value = workspace_id.strip()
            if workspace_value:
                _validated_session_workspace(
                    request,
                    effective_user(request),
                    workspace_value,
                )
            await _prepare_context_mutation(request, sid)
            if not session_manager.update_session_workspace(
                sid,
                workspace_value or None,
            ):
                raise HTTPException(404, f"Session {sid} not found")
            result["workspace_id"] = workspace_value or None
            if workspace_value and workspace_value != prior_workspace:
                from src.openclank.achievement_producers import record_activity
                record_activity(request, "chat.workspace.rebound", str(uuid.uuid4()), {
                    "sessionId": sid, "newWorkspaceId": workspace_value, "sessionIdPreserved": True,
                }, workspace_id=workspace_value)
        if name is not None:
            session_manager.update_session_name(sid, name)
            result["name"] = name
        # Update folder assignment
        if folder is not None:
            db = SessionLocal()
            try:
                db_session = db.query(DbSession).filter(DbSession.id == sid).first()
                if db_session:
                    db_session.folder = folder if folder else None
                    db_session.updated_at = utcnow_naive()
                    db.commit()
                    result["folder"] = folder if folder else None
            finally:
                db.close()
        # Switch model/endpoint mid-session
        if model is not None and (endpoint_id is not None or endpoint_url is not None):
            user = effective_user(request)
            try:
                selected_route = resolve_chat_route(
                    owner=user,
                    endpoint_id=endpoint_id,
                    model_id=model,
                )
            except ChatRouteUnavailable as exc:
                raise HTTPException(400, str(exc)) from exc
            endpoint_url = MANAGED_ENGINE_PUBLIC_URL
            endpoint_id = selected_route.public_endpoint_id
            model = selected_route.provider_model_id
            auth_manager = getattr(request.app.state, "auth_manager", None)
            get_privileges = getattr(auth_manager, "get_privileges", None)
            privileges = get_privileges(user) if get_privileges and user else {}
            allowed = set((privileges or {}).get("allowed_models") or [])
            restricted = bool((privileges or {}).get("allowed_models_restricted")) or bool(allowed)
            if (privileges or {}).get("block_all_models") or (
                restricted
                and model not in allowed
                and selected_route.model_route_id not in allowed
            ):
                raise HTTPException(403, f"Your account is not allowed to use model {model!r}")
            await _prepare_context_mutation(request, sid)
            next_headers = {}
            # Persist to DB
            db = SessionLocal()
            try:
                db_session = db.query(DbSession).filter(DbSession.id == sid).first()
                if db_session:
                    db_session.model = model
                    db_session.endpoint_url = endpoint_url
                    db_session.endpoint_id = endpoint_id or None
                    db_session.provider_model_route_id = selected_route.model_route_id
                    db_session.headers = next_headers
                    db_session.updated_at = utcnow_naive()
                    db.commit()
            finally:
                db.close()
            # Publish the new in-memory route only after persistence succeeds.
            # A failed commit must leave the active session on its old route.
            session.model = model
            session.endpoint_url = endpoint_url
            session.endpoint_id = endpoint_id or None
            session.provider_model_route_id = selected_route.model_route_id
            session.headers = next_headers
            result["model"] = model
            result["endpoint_url"] = endpoint_url
            result["endpoint_id"] = endpoint_id or None
        return result

    @router.get("/session/{sid}/mimo-state")
    async def get_mimo_state(request: Request, sid: str, refresh: bool = False):
        """Return the owner-scoped negotiated Open Clank agent control-plane snapshot."""
        _verify_session_owner(request, sid)
        try:
            session = session_manager.get_session(sid)
        except KeyError:
            raise HTTPException(404, f"Session {sid} not found")
        if not getattr(session, "provider_model_route_id", None):
            return {"available": False, "reason": "Session does not use Open Clank agent"}

        owner = effective_user(request) or getattr(session, "owner", None)
        if not owner:
            raise HTTPException(403, "Open Clank agent sessions require an authenticated owner")
        from src.openclank.transcript_projection import get_mimo_state as _get_state

        state = _get_state(sid, owner=owner)
        if refresh or not state.get("config_options"):
            supervisor = getattr(request.app.state, "mimo_supervisor", None)
            if supervisor is None:
                raise HTTPException(503, "Open Clank engine is unavailable")
            try:
                state = await supervisor.negotiate_session(
                    sid, owner=owner
                )
            except RuntimeError as exc:
                raise HTTPException(503, str(exc)) from exc
        return {"available": True, **state}

    @router.post("/session/{sid}/plan/approve")
    async def approve_plan(request: Request, sid: str):
        """CAS-approve the exact server-persisted plan revision for this session."""
        _verify_session_owner(request, sid, session_manager)
        owner = effective_user(request)
        if not owner:
            raise HTTPException(403, "Plan approval requires an authenticated owner")
        try:
            body = await request.json()
            revision = int(body.get("revision"))
            digest = str(body.get("digest") or "")
        except (TypeError, ValueError, AttributeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, "revision and digest are required") from exc
        if revision <= 0 or len(digest) != 64:
            raise HTTPException(400, "revision and digest are required")
        from src.plan_approval import approve_plan as _approve_plan

        try:
            plan_state = _approve_plan(sid, owner, revision=revision, digest=digest)
        except KeyError as exc:
            raise HTTPException(404, "Session plan is unavailable") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "plan_state": plan_state}

    @router.post("/session/{sid}/plan/draft")
    async def save_plan_draft(request: Request, sid: str):
        """Persist a displayed draft; this endpoint never grants execution."""
        _verify_session_owner(request, sid, session_manager)
        owner = effective_user(request)
        if not owner:
            raise HTTPException(403, "Plan drafts require an authenticated owner")
        try:
            body = await request.json()
            plan = str(body.get("plan") or "")
        except (TypeError, AttributeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, "plan is required") from exc
        from src.plan_approval import save_plan_draft as _save_plan_draft

        try:
            plan_state = _save_plan_draft(sid, owner, plan)
        except KeyError as exc:
            raise HTTPException(404, "Session plan is unavailable") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "plan_state": plan_state}

    @router.post("/session/{sid}/plan/clear")
    async def clear_plan(request: Request, sid: str):
        """Clear server draft/approval state without deleting materialized files."""
        _verify_session_owner(request, sid, session_manager)
        owner = effective_user(request)
        if not owner:
            raise HTTPException(403, "Plan clearing requires an authenticated owner")
        from src.plan_approval import clear_plan as _clear_plan

        try:
            plan_state = _clear_plan(sid, owner)
        except KeyError as exc:
            raise HTTPException(404, "Session plan is unavailable") from exc
        return {"ok": True, "plan_state": plan_state}

    async def _goal_proxy(
        request: Request,
        sid: str,
        method: str,
        *,
        suffix: str = "goal",
        payload: dict | None = None,
        timeout: float = 20.0,
    ):
        _verify_session_owner(request, sid, session_manager)
        try:
            session = session_manager.get_session(sid)
        except KeyError:
            raise HTTPException(404, f"Session {sid} not found")
        if not getattr(session, "provider_model_route_id", None):
            raise HTTPException(400, "Goals are available on Open Clank agent sessions")
        owner = effective_user(request) or getattr(session, "owner", None)
        if not owner:
            raise HTTPException(403, "Open Clank agent goals require an owner")
        supervisor = getattr(request.app.state, "mimo_supervisor", None)
        request_session = getattr(supervisor, "session_http_request", None)
        if not callable(request_session):
            raise HTTPException(503, "Open Clank agent is unavailable")
        try:
            response = await request_session(
                sid,
                method,
                suffix,
                owner=owner,
                payload=payload,
                timeout=timeout,
            )
        except HTTPException:
            raise
        except Exception as exc:
            from src.openclank.agent_supervisor import AgentSupervisorAdmissionError

            if isinstance(exc, AgentSupervisorAdmissionError):
                raise HTTPException(exc.status, str(exc)) from exc
            if isinstance(exc, (RuntimeError, httpx.HTTPError)):
                raise HTTPException(503, str(exc)) from exc
            raise
        try:
            body = response.json()
        except ValueError as exc:
            raise HTTPException(502, "Open Clank agent returned an invalid response") from exc
        if response.status_code >= 400:
            detail = body.get("error") if isinstance(body, dict) else None
            if isinstance(detail, dict):
                detail = detail.get("message")
            if not detail and isinstance(body, dict):
                data = body.get("data")
                detail = data.get("message") if isinstance(data, dict) else body.get("message")
            raise HTTPException(
                response.status_code if response.status_code < 600 else 502,
                str(detail or "Open Clank agent goal request failed")[:500],
            )
        return body

    @router.get("/session/{sid}/goal")
    async def get_session_goal(request: Request, sid: str):
        return await _goal_proxy(request, sid, "GET")

    @router.post("/session/{sid}/goal")
    async def update_session_goal(request: Request, sid: str, payload: dict):
        if payload.get("action") != "verify":
            return await _goal_proxy(
                request,
                sid,
                "POST",
                payload=payload,
            )

        target = payload.get("target")
        goal_id = target.get("goalID") if isinstance(target, dict) else None
        revision = target.get("expectedRevision") if isinstance(target, dict) else None
        if (
            not isinstance(goal_id, str)
            or not goal_id
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            raise HTTPException(400, "verify requires the current goal ID and revision")
        await _goal_proxy(
            request,
            sid,
            "POST",
            suffix="command",
            payload={
                "command": "goal",
                "arguments": f"verify {goal_id} {revision}",
            },
            timeout=240.0,
        )
        return await _goal_proxy(request, sid, "GET")

    @router.put("/session/{sid}/persona")
    async def set_session_persona(request: Request, sid: str):
        """Attach a persona to THIS chat only (identity ruling: the in-chat
        persona menu is chat-specific; the global default persona covers
        branding and new chats)."""
        _verify_session_owner(request, sid)
        try:
            session_manager.get_session(sid)
        except KeyError:
            raise HTTPException(404, f"Session {sid} not found")
        body = await request.json()
        character_name = str(body.get("character_name") or "").strip()[:100]
        system_prompt = str(body.get("system_prompt") or "").strip()[:10000]
        if not character_name and not system_prompt:
            raise HTTPException(400, "character_name or system_prompt required")
        record = {"character_name": character_name, "system_prompt": system_prompt}
        try:
            temperature = body.get("temperature")
            if temperature is not None:
                record["temperature"] = max(0.0, min(2.0, float(temperature)))
            max_tokens = body.get("max_tokens")
            if max_tokens is not None:
                record["max_tokens"] = max(0, min(65536, int(max_tokens)))
        except (TypeError, ValueError):
            raise HTTPException(400, "temperature/max_tokens must be numeric")
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == sid).first()
            if not db_session:
                raise HTTPException(404, f"Session {sid} not found")
            db_session.persona = json.dumps(record, ensure_ascii=False)
            db_session.updated_at = utcnow_naive()
            db.commit()
        finally:
            db.close()
        return {"success": True, "persona": record}

    @router.delete("/session/{sid}/persona")
    async def clear_session_persona(request: Request, sid: str):
        """Remove this chat's persona — it speaks as the default persona again."""
        _verify_session_owner(request, sid)
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == sid).first()
            if not db_session:
                raise HTTPException(404, f"Session {sid} not found")
            db_session.persona = None
            db_session.updated_at = utcnow_naive()
            db.commit()
        finally:
            db.close()
        return {"success": True}

    @router.patch("/session/{sid}/mimo-config")
    async def set_mimo_config(request: Request, sid: str):
        """Validate, acknowledge, and persist one negotiated Open Clank agent option."""
        _verify_session_owner(request, sid)
        try:
            session = session_manager.get_session(sid)
        except KeyError:
            raise HTTPException(404, f"Session {sid} not found")
        if not getattr(session, "provider_model_route_id", None):
            raise HTTPException(409, "Session does not use Open Clank agent")
        owner = effective_user(request) or getattr(session, "owner", None)
        if not owner:
            raise HTTPException(403, "Open Clank agent sessions require an authenticated owner")
        body = await request.json()
        config_id = str(body.get("config_id") or "").strip()
        value = body.get("value")
        if not config_id or not isinstance(value, str):
            raise HTTPException(400, "config_id and string value are required")
        if config_id == "model":
            raise HTTPException(
                409,
                "Change models through the model picker so the stable provider route is preserved",
            )
        supervisor = getattr(request.app.state, "mimo_supervisor", None)
        if supervisor is None:
            raise HTTPException(503, "Open Clank engine is unavailable")
        try:
            state = await supervisor.set_session_config(
                sid,
                config_id,
                value,
                owner=owner,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:
            from src.openclank.acp_client import RPCError

            if isinstance(exc, RPCError):
                raise HTTPException(409, str(exc)) from exc
            raise
        return {"status": "ok", **state}
    
    @router.post("/session/{sid}/inject_messages")
    async def inject_messages(request: Request, sid: str):
        """Bulk-inject messages into a session's history (for group chat sync)."""
        _verify_session_owner(request, sid)
        await _prepare_context_mutation(request, sid)
        try:
            sess = session_manager.get_session(sid)
        except KeyError:
            raise HTTPException(404, f"Session {sid} not found")
        body = await request.json()
        messages = body.get("messages", [])
        from core.models import ChatMessage
        owner = effective_user(request)
        try:
            for message in messages:
                missing_id = reserve_message_upload_references(
                    upload_handler,
                    owner,
                    message.get("content"),
                    message.get("metadata"),
                    session_id=sid,
                )
                if missing_id:
                    raise HTTPException(
                        409,
                        f"Referenced upload is no longer available: {missing_id}",
                    )
        except (AttributeError, TypeError, ValueError) as exc:
            raise HTTPException(400, "Invalid message attachment metadata") from exc
        for m in messages:
            sess.add_message(ChatMessage(m["role"], m["content"], metadata=m.get("metadata")))
        session_manager.save_sessions()
        return {"ok": True, "count": len(messages)}

    @router.post("/session/{sid}/delete")
    async def delete_session_beacon(request: Request, sid: str):
        """Delete session via POST (for navigator.sendBeacon on page close)."""
        return await delete_session(request, sid)

    @router.post("/sessions/bulk-delete")
    async def bulk_delete_sessions(request: Request):
        """Beacon cleanup uses the same owned, observable erasure path."""
        try:
            body = await request.json()
            ids = body.get("ids", [])
        except Exception:
            ids = []
        if not isinstance(ids, list) or len(ids) > 500:
            raise HTTPException(422, "ids must be a bounded list")
        deleted_count = 0
        for sid in ids:
            try:
                await delete_session(request, str(sid))
                deleted_count += 1
            except HTTPException as exc:
                if exc.status_code == 403 and isinstance(exc.detail, dict) and exc.detail.get("error") == "SESSION_STARRED":
                    continue
                raise
        return {"deleted": deleted_count}

    @router.get("/session/{sid}/delete-impact")
    def session_delete_impact(request: Request, sid: str):
        owner = _verify_session_owner(request, sid, session_manager)
        from src.openclank.session_deletion import deletion_impact
        db = SessionLocal()
        try:
            result = deletion_impact(db, owner=owner, session_id=sid, upload_handler=upload_handler)
            row = db.query(DbSession).filter(DbSession.id == sid).first()
            result["protected"] = bool(row and row.is_important)
            return result
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            db.close()

    @router.delete("/session/{sid}")
    async def delete_session(request: Request, sid: str):
        """Permanently delete a session and all its messages."""
        owner = _verify_session_owner(request, sid, session_manager)
        try:
            # Block deletion of starred/favorited sessions
            db = SessionLocal()
            try:
                db_sess = db.query(DbSession).filter(DbSession.id == sid).first()
                if db_sess and db_sess.is_important:
                    raise HTTPException(
                        status_code=403,
                        detail={"error": "SESSION_STARRED", "message": "Unstar the session before deleting it"}
                    )
            finally:
                db.close()

            service = ChatLifecycleService(session_manager=session_manager,
                mimo_supervisor=getattr(request.app.state, "mimo_supervisor", None),
                memory_provider=getattr(request.app.state, "memory_provider", None))
            try:
                return await service.erase(owner=owner, session_id=sid)
            except ChatLifecycleError as exc:
                status = 409 if exc.code == "active_run" else 503
                raise HTTPException(status, {"error": "SESSION_DELETE_INCOMPLETE", "message": str(exc), "retryable": exc.code == "erasure_pending"}) from exc
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting session {sid}: {e}")
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "SESSION_DELETE_ERROR",
                    "message": "Failed to delete session"
                }
            )
    
    @router.delete("/sessions/all")
    async def delete_all_sessions(request: Request):
        """Admin deletion uses the same scoped erasure for every conversation."""
        from core.middleware import require_admin
        require_admin(request)
        db = SessionLocal()
        try:
            rows = db.query(DbSession.id, DbSession.owner).all()
        finally:
            db.close()
        identities = {str(row.id): str(row.owner or "") for row in rows}
        identities.update({str(sid): str(getattr(session, "owner", "") or "") for sid, session in session_manager.sessions.items()})
        # Engine-only projections remain part of the administrator's all
        # operation. Their persisted owner is authoritative; an unowned
        # bridge mapping cannot be erased under a guessed account.
        from src.openclank.transcript_projection import list_projections, purge_execution_projection
        supervisor = getattr(request.app.state, "mimo_supervisor", None)
        projections = {str(row["odysseus_session_id"]): row for row in list_projections()}
        bridge = getattr(supervisor, "bridge", None) if supervisor else None
        mapped = bridge.mapped_sessions() if bridge is not None else {}
        unknown = set(mapped) - set(identities) - set(projections)
        if unknown:
            raise HTTPException(409, "Engine mapping has no authoritative account identity; explicit reconciliation required")
        deleted = []
        for sid, projection in projections.items():
            if sid in identities:
                continue
            owner = str(projection.get("owner") or "")
            if not owner:
                raise HTTPException(409, "Engine projection has no account identity; explicit conversion required")
            _reject_compact_during_active_run(sid)
            try:
                await purge_execution_projection(supervisor, sid, owner=owner)
            except Exception as exc:
                raise HTTPException(503, {"error": "SESSION_DELETE_INCOMPLETE", "message": "Engine projection deletion incomplete", "deleted": deleted, "failed_session_id": sid}) from exc
            deleted.append(sid)
        for sid, owner in identities.items():
            if not owner:
                raise HTTPException(409, "Conversation has no account identity; explicit conversion required")
            _reject_compact_during_active_run(sid)
            supervisor = getattr(request.app.state, "mimo_supervisor", None)
            service = ChatLifecycleService(session_manager=session_manager,
                mimo_supervisor=supervisor,
                memory_provider=getattr(request.app.state, "memory_provider", None))
            try:
                await service.erase(owner=owner, session_id=sid, allow_protected=True)
            except ChatLifecycleError as exc:
                raise HTTPException(503, {"error": "SESSION_DELETE_INCOMPLETE", "message": str(exc), "deleted": deleted, "failed_session_id": sid}) from exc
            deleted.append(sid)
        return {"status": "deleted", "count": len(deleted)}

    @router.post("/session/{sid}/archive")
    async def archive_session(request: Request, sid: str):
        """Archive a session, keeping its data but removing it from active sessions."""
        return await _set_chat_archived(request, sid, True)
    
    @router.post("/session/{sid}/unarchive")
    async def unarchive_session(request: Request, sid: str):
        """Restore an archived session back to the active session list."""
        return await _set_chat_archived(request, sid, False)

    @router.get("/sessions/archived")
    def list_archived_sessions(request: Request, search: str = "", offset: int = 0, limit: int = 20, sort: str = "recent", model: str = ""):
        """List archived sessions for the archive browser."""
        user = effective_user(request)
        db = SessionLocal()
        try:
            q = db.query(DbSession).filter(DbSession.archived == True)
            if not user:
                raise HTTPException(403, "Authentication required")
            q = q.filter(DbSession.owner == user)
            if search:
                safe_search = search.replace('%', r'\%').replace('_', r'\_')
                q = q.filter(DbSession.name.ilike(f"%{safe_search}%", escape='\\'))
            if model:
                # Contains match (mirrors the name filter above). The old
                # f"%{model}" was a SUFFIX-only match, so filtering by "gpt-4"
                # dropped "gpt-4o" and over-matched on shared suffixes; it also
                # left LIKE wildcards in the user value unescaped.
                safe_model = model.replace('%', r'\%').replace('_', r'\_')
                q = q.filter(DbSession.model.ilike(f"%{safe_model}%", escape='\\'))
            total = q.count()
            sort_map = {
                "recent": DbSession.updated_at.desc(),
                "oldest": DbSession.updated_at.asc(),
                "most-messages": DbSession.message_count.desc().nulls_last(),
                "alpha": DbSession.name.asc(),
            }
            order = sort_map.get(sort, DbSession.updated_at.desc())
            rows = q.order_by(order).offset(offset).limit(limit).all()
            sessions = []
            for s in rows:
                sessions.append({
                    "id": s.id,
                    "name": s.name,
                    "model": s.model,
                    "message_count": s.message_count or 0,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                    "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                    "is_important": s.is_important,
                    "workspace_id": s.workspace_id,
                })
            return {"sessions": sessions, "total": total}
        finally:
            db.close()

    @router.get("/session/{sid}/export")
    def export_session(request: Request, sid: str, fmt: str = "md", filename: str = ""):
        """Export conversation history as a downloadable file.

        Supported formats: md (markdown), txt (plain text), json, html
        """
        _verify_session_owner(request, sid)
        try:
            session = session_manager.get_session(sid)
        except KeyError:
            raise HTTPException(404, f"Session {sid} not found")

        safe_name = re.sub(r'[^\w\-_]', '_', session.name)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = _sanitize_export_filename(filename)

        if fmt == "json":
            import json as _json
            data = {
                "name": session.name,
                "model": session.model,
                "exported": datetime.now().isoformat(),
                "messages": [{"role": m.role, "content": m.content} for m in session.history],
            }
            out_name = filename or f"conversation_{safe_name}_{timestamp}.json"
            return Response(
                content=_json.dumps(data, indent=2, ensure_ascii=False),
                media_type="application/json",
                headers={"Content-Disposition": f"attachment; filename={out_name}"},
            )

        if fmt == "txt":
            lines = []
            for m in session.history:
                lines.append(f"[{m.role.upper()}]")
                lines.append(_content_to_text(m.content))
                lines.append("")
            out_name = filename or f"conversation_{safe_name}_{timestamp}.txt"
            return Response(
                content="\n".join(lines),
                media_type="text/plain",
                headers={"Content-Disposition": f"attachment; filename={out_name}"},
            )

        if fmt == "html":
            safe_title = html.escape(session.name or "")
            html_parts = [
                "<!DOCTYPE html><html><head>",
                f"<meta charset='utf-8'><title>{safe_title}</title>",
                "<style>body{font-family:monospace;max-width:800px;margin:2rem auto;padding:0 1rem;background:#111;color:#ddd}",
                ".msg{margin:1rem 0;padding:0.8rem;border-radius:6px;border:1px solid #333}",
                ".user{background:#1a1a2e}.ai{background:#1a2e1a}",
                ".role{font-weight:bold;margin-bottom:0.4rem;opacity:0.7;text-transform:uppercase;font-size:0.85em}",
                "pre{background:#000;padding:0.5rem;border-radius:4px;overflow-x:auto}</style></head><body>",
                f"<h1>{safe_title}</h1>",
            ]
            for m in session.history:
                cls = "user" if m.role == "user" else "ai"
                content = _content_to_text(m.content).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                content = content.replace("\n", "<br>")
                html_parts.append(f'<div class="msg {cls}"><div class="role">{m.role}</div>{content}</div>')
            html_parts.append("</body></html>")
            out_name = filename or f"conversation_{safe_name}_{timestamp}.html"
            return Response(
                content="\n".join(html_parts),
                media_type="text/html",
                headers={"Content-Disposition": f"attachment; filename={out_name}"},
            )

        # Default: markdown
        markdown_lines = []
        markdown_lines.append(f"# Conversation: {session.name}")
        markdown_lines.append(f"*Exported on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
        markdown_lines.append(f"*Model: {session.model}*")
        markdown_lines.append("\n---\n")
        for message in session.history:
            role = message.role.upper()
            content = _content_to_text(message.content)
            markdown_lines.append(f"### {role}")
            markdown_lines.append(f"{content}\n")
            markdown_lines.append("---\n")
        if len(markdown_lines) > 3:
            markdown_lines.pop()
        out_name = filename or f"conversation_{safe_name}_{timestamp}.md"
        return Response(
            content="\n".join(markdown_lines),
            media_type="text/markdown",
            headers={"Content-Disposition": f"attachment; filename={out_name}"},
        )
    
    @router.post("/sessions/save")
    def sessions_save_now(request: Request):
        user = effective_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        session_manager.save_sessions()
        return {"ok": True, "path": SESSIONS_FILE}
    
    @router.post("/session/openai")
    async def create_session_openai(
        request: Request,
        name: str = Form("New Chat (OpenAI)"),
        model: str = Form("gpt-4o"),
        rag: str = Form(None)
    ):
        if not OPENAI_API_KEY:
            raise HTTPException(400, "Server missing OPENAI_API_KEY")
        sid = str(uuid.uuid4())
        user = effective_user(request)
        session = session_manager.create_session(
            session_id=sid,
            name="",
            endpoint_url="https://api.openai.com/v1/chat/completions",
            model=model,
            rag=str(rag).lower() == "true",
            owner=user,
        )
        session.headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        session_manager.save_sessions()
        from src.event_bus import fire_event
        fire_event("session_created", user)
        return {"id": sid, "name": "", "model": model}
    
    @router.post("/session/{session_id}/important")
    async def mark_session_important(request: Request, session_id: str, important: bool = Form(True)):
        """Mark a session as important to protect it from automatic cleanup."""
        _verify_session_owner(request, session_id)
        try:
            # Validate session exists
            session_manager.get_session(session_id)

            # Update in database
            db = SessionLocal()
            try:
                db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
                if db_session:
                    db_session.is_important = important
                    db_session.updated_at = utcnow_naive()
                    db.commit()

                    # Update in memory if it exists
                    if session_id in session_manager.sessions:
                        session_manager.sessions[session_id].is_important = important

                    return {"status": "success", "is_important": important}
                else:
                    raise HTTPException(404, f"Session {session_id} not found")

            except HTTPException:
                raise
            except Exception as e:
                db.rollback()
                logger.error(f"Error updating session {session_id} importance: {e}")
                raise HTTPException(500, "Failed to update session importance")
            finally:
                db.close()

        except KeyError:
            raise HTTPException(404, f"Session {session_id} not found")

    @router.post("/sessions/auto-sort")
    async def auto_sort_sessions(request: Request, skip_llm: bool = False):
        """Use AI to categorize all sessions into folders.

        Phase 1 deletes empty/throwaway sessions and Phase 2 asks the LLM
        to assign folders. When `skip_llm=true` the endpoint returns
        after Phase 1 — used by the "Tidy (no AI)" UI affordance so
        users can clean junk without spending tokens.
        """
        from src.openclank.modality_facade import complete_text
        user = effective_user(request)
        if not user:
            raise HTTPException(401, "Authentication required")
        single_user_mode = False
        user_sessions = session_manager.get_sessions_for_user(user)

        # Delete empty and throwaway sessions before sorting
        from core.database import ChatMessage as DbMsg
        db = SessionLocal()
        deleted_empty = 0
        deleted_throwaway = 0
        # Names that indicate a throwaway/test session (case-insensitive exact or prefix match)
        _THROWAWAY_NAMES = {
            "test", "testing", "asdf", "asd", "hello", "hi", "hey",
            "yo", "sup", "hola", "hii", "hiii", "heyo",
            "foo", "bar", "baz", "tmp", "temp", "scratch", "untitled",
            "new chat", "delete", "remove", "junk", "trash", "xxx",
            "abc", "qwerty", "blah", "stuff", "whatever", "idk",
            "ok", "lol", "bruh", "hmm", "hm", "meh",
        }
        _THROWAWAY_MAX_MESSAGES = 4  # only delete if <= this many messages
        try:
            rows_q = db.query(DbSession).filter(DbSession.archived == False)
            if user:
                rows_q = rows_q.filter(DbSession.owner == user)
            elif not single_user_mode:
                rows_q = rows_q.filter(DbSession.owner == user)
            rows = rows_q.limit(2000).all()
            folder_map = {r.id: r.folder for r in rows}
            # Precompute per-session message counts in TWO aggregate queries
            # instead of 1–3 queries PER session — with many chats the per-row
            # loop was doing thousands of round-trips and blowing the timeout.
            from sqlalchemy import func as _sa_func
            _counts = dict(db.query(DbMsg.session_id, _sa_func.count(DbMsg.id)).group_by(DbMsg.session_id).all())
            _asst_counts = dict(
                db.query(DbMsg.session_id, _sa_func.count(DbMsg.id))
                .filter(DbMsg.role == "assistant").group_by(DbMsg.session_id).all()
            )
            cleanup_now = utcnow_naive()
            for row in rows:
                # Never delete important sessions
                if getattr(row, 'is_important', False):
                    continue
                # Always delete incognito sessions during cleanup
                if (row.name or "").strip() == "Incognito":
                    should_delete = True
                    deleted_throwaway += 1
                    db.delete(row)
                    if hasattr(session_manager, 'delete_session'):
                        session_manager.delete_session(row.id)
                    continue
                if is_session_recently_active(row, now=cleanup_now):
                    continue
                msg_count = _counts.get(row.id, 0)
                should_delete = False
                if msg_count == 0:
                    should_delete = True
                    deleted_empty += 1
                elif msg_count <= _THROWAWAY_MAX_MESSAGES:
                    name = (row.name or "").strip().lower()
                    # Check first user message content (AI renames sessions, so
                    # "hi" becomes "Casual Greeting Exchange" — name alone won't match)
                    first_msg = db.query(DbMsg.content).filter(
                        DbMsg.session_id == row.id, DbMsg.role == "user"
                    ).order_by(DbMsg.timestamp).first()
                    first_text = (first_msg[0] or "").strip().lower() if first_msg else ""
                    # Count assistant messages — if user sent something but AI never replied, it's dead
                    assistant_count = _asst_counts.get(row.id, 0)
                    if name in _THROWAWAY_NAMES or name.startswith("chat:") or first_text in _THROWAWAY_NAMES:
                        should_delete = True
                        deleted_throwaway += 1
                    # Single user message with no AI response = dead session
                    elif msg_count == 1 and assistant_count == 0:
                        should_delete = True
                        deleted_throwaway += 1
                    # Short phrase (1-3 words) with no real AI conversation (<=2 msgs)
                    elif msg_count <= 2 and first_text and len(first_text.split()) <= 3 and len(first_text) <= 40:
                        should_delete = True
                        deleted_throwaway += 1
                if should_delete:
                    db.delete(row)
                    if hasattr(session_manager, 'delete_session'):
                        session_manager.delete_session(row.id)
            if deleted_empty or deleted_throwaway:
                db.commit()
                logger.info(f"Auto-sort: deleted {deleted_empty} empty + {deleted_throwaway} throwaway sessions")
        finally:
            db.close()

        # Re-fetch after cleanup
        if deleted_empty or deleted_throwaway:
            user_sessions = session_manager.get_sessions_for_user(user)

        # Short-circuit when the caller only wanted the cleanup phase
        # (the "Tidy (no AI)" path). Shape mirrors the post-Phase-1
        # branch below so the frontend can render the same toast.
        if skip_llm:
            return {
                "status": "ok",
                "updated": 0,
                "folders": [],
                "deleted_empty": deleted_empty,
                "deleted_throwaway": deleted_throwaway,
                "unfiled_remaining": 0,
                "skipped_llm": True,
            }

        # Tidy works in batches: only sessions that don't already have a
        # folder, capped at TIDY_BATCH_SIZE (most recent first). Sending
        # all 100+ chats to one LLM call blows the context window, makes
        # the request slow, and re-bills the same tokens every click for
        # already-sorted chats. Skipping sessions with `current_folder`
        # means each Tidy press only handles new unfiled chats.
        TIDY_BATCH_SIZE = 15
        all_candidates = []
        for s in user_sessions.values():
            if s.archived or s.name == "Incognito":
                continue
            if folder_map.get(s.id):
                # Already in a folder — skip on this pass.
                continue
            name = s.name or "(unnamed)"
            all_candidates.append({
                "id": s.id,
                "name": name,
                "updated_at": getattr(s, "updated_at", None) or getattr(s, "created_at", None) or "",
                "current_folder": None,
            })

        # Most-recent first, then take the top N for this batch.
        all_candidates.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        unfiled_total = len(all_candidates)
        session_list = all_candidates[:TIDY_BATCH_SIZE]

        if len(session_list) < 2:
            if deleted_empty or deleted_throwaway:
                return {
                    "status": "ok",
                    "updated": 0,
                    "folders": [],
                    "deleted_empty": deleted_empty,
                    "deleted_throwaway": deleted_throwaway,
                    "unfiled_remaining": unfiled_total,
                }
            return {"status": "skipped", "reason": "No unfiled sessions to sort"}

        # Build prompt
        names_text = "\n".join(f'  "{s["id"][:8]}": "{s["name"]}"' for s in session_list)
        prompt = (
            "You are a session organizer. Group these chat sessions into folders by topic.\n\n"
            "Rules:\n"
            "- Be aggressive about grouping — put EVERY session in a folder\n"
            "- Use short folder names (2-4 words max)\n"
            "- Use the 8-char ID prefixes exactly as given\n"
            "- Output ONLY raw JSON, no markdown fences, no explanation\n\n"
            "Required JSON format:\n"
            '{"folders": {"Folder Name": ["id_prefix1", "id_prefix2"], "Other Folder": ["id_prefix3"]}}\n\n'
            f"Sessions (id_prefix: name):\n{{\n{names_text}\n}}"
        )

        try:
            # 16384 (was 4096): with many chats the folder JSON is large, and a
            # reasoning model spends tokens thinking first — 4096 truncated the
            # JSON mid-output, so it never parsed ("invalid JSON for auto-sort").
            import hashlib

            raw = await complete_text(
                owner=user or "local-installation",
                purpose="utility",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_output_tokens=16384,
                idempotency_key=(
                    "auto-sort-sessions-"
                    + hashlib.sha256(names_text.encode("utf-8")).hexdigest()[:32]
                ),
            )
            logger.info(f"Auto-sort raw response ({len(raw)} chars): {raw[:300]}")
            # Extract JSON from response — handle markdown fences, leading text,
            # reasoning-model <think> blocks, and trailing commas.
            text = raw.strip()
            # Reasoning models emit <think>…</think> (often containing { } that
            # would derail the brace scan) before the answer — drop it first.
            text = re.sub(r'<think(?:ing)?>[\s\S]*?</think(?:ing)?>', '', text, flags=re.I).strip()

            def _loads_lenient(s):
                """Parse JSON, retrying once with trailing commas stripped."""
                if not s:
                    return None
                for cand in (s, re.sub(r',(\s*[}\]])', r'\1', s)):
                    try:
                        return json.loads(cand)
                    except json.JSONDecodeError:
                        continue
                return None

            result = _loads_lenient(text)
            # Markdown code fence
            if result is None:
                fence_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)```', text)
                if fence_match:
                    result = _loads_lenient(fence_match.group(1).strip())
            # First { … last } block
            if result is None:
                brace_start = text.find('{')
                brace_end = text.rfind('}')
                if brace_start >= 0 and brace_end > brace_start:
                    result = _loads_lenient(text[brace_start:brace_end + 1])
            if result is None:
                logger.error(f"Auto-sort: could not parse JSON from: {text[:500]}")
                raise HTTPException(502, "AI returned invalid JSON for auto-sort — the model may not follow JSON instructions; try a different utility model in Settings.")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Auto-sort LLM call failed: {e}")
            raise HTTPException(502, f"Auto-sort failed: {str(e)}")

        folders = result.get("folders", {})
        if not folders:
            return {"status": "skipped", "reason": "AI found no groupings"}

        # Build id -> folder map
        id_prefix_map = {s["id"][:8]: s["id"] for s in session_list}
        assignments = {}
        for folder_name, ids in folders.items():
            for sid_or_prefix in ids:
                # Match by full ID or prefix
                full_id = None
                if sid_or_prefix in id_prefix_map.values():
                    full_id = sid_or_prefix
                else:
                    # Try prefix match
                    prefix = sid_or_prefix.rstrip(".").rstrip(" ")
                    if prefix in id_prefix_map:
                        full_id = id_prefix_map[prefix]
                    else:
                        # Fuzzy prefix match
                        for p, fid in id_prefix_map.items():
                            if fid.startswith(prefix) or prefix.startswith(p):
                                full_id = fid
                                break
                if full_id:
                    assignments[full_id] = folder_name

        # Apply folder assignments
        updated = 0
        db = SessionLocal()
        try:
            for sid, folder_name in assignments.items():
                db_session_q = db.query(DbSession).filter(DbSession.id == sid)
                if user:
                    db_session_q = db_session_q.filter(DbSession.owner == user)
                elif not single_user_mode:
                    db_session_q = db_session_q.filter(DbSession.owner == user)
                db_session = db_session_q.first()
                if db_session:
                    db_session.folder = folder_name
                    db_session.updated_at = utcnow_naive()
                    updated += 1
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Auto-sort DB update failed: {e}")
            raise HTTPException(500, "Failed to apply folder assignments")
        finally:
            db.close()

        # How many unfiled chats are left after this batch — the
        # frontend uses this to decide whether to show "Tidy more" or
        # "All sorted!" in the toast.
        unfiled_remaining_after = max(0, unfiled_total - updated)
        return {
            "status": "ok",
            "folders": list(folders.keys()),
            "updated": updated,
            "deleted_empty": deleted_empty,
            "deleted_throwaway": deleted_throwaway,
            "unfiled_remaining": unfiled_remaining_after,
        }

    @router.get("/session/{session_id}/context_info")
    async def get_context_info(request: Request, session_id: str):
        """Get the real context length for a session's model from the endpoint."""
        _verify_session_owner(request, session_id)
        session = session_manager.get_session(session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if not session.endpoint_url or not session.model:
            return {"context_length": None}
        try:
            from src.model_context import get_context_length
            ctx = get_context_length(session.endpoint_url, session.model)
            return {"context_length": ctx, "model": session.model}
        except Exception:
            return {"context_length": None}

    return router

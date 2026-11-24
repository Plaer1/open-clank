# routes/session_routes.py
import asyncio
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


async def _bounded_transfer_json(request: Request, limit: int):
    """Read only the bounded request stream for selected-history endpoints."""
    data = bytearray()
    async for chunk in request.stream():
        if not isinstance(chunk, bytes) or len(chunk) > limit - len(data):
            raise ValueError('request exceeds limit')
        data.extend(chunk)
    return json.loads(data)


def setup_session_routes(
    session_manager: SessionManager,
    config: dict,
    webhook_manager=None,
    upload_handler=None,
):
    """Setup session routes with the provided manager and config"""

    from routes.prompt_suggestion_routes import setup_prompt_suggestion_routes
    router.include_router(setup_prompt_suggestion_routes(session_manager, _verify_session_owner))

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
                from src.openclank.chat_source_bridge import canonical_source_provider
                # Names do not prove an erased source; guarded stores use the
                # canonical chat-erasure lifecycle instead of this raw purge.
                _ghosts = [] if canonical_source_provider(request.app.state) is not None else _purge_db.query(DbSession).filter(
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
            style_map = {}
            created_map = {}
            updated_map = {}
            last_msg_map = {}
            mode_map = {}
            msg_count_map = {}
            persona_map = {}
            workspace_map = {}
            q = db.query(DbSession.id, DbSession.folder, DbSession.total_input_tokens, DbSession.total_output_tokens, DbSession.is_important, DbSession.created_at, DbSession.updated_at, DbSession.last_message_at, DbSession.mode, DbSession.message_count, DbSession.persona, DbSession.workspace_id, DbSession.response_style, DbSession.response_style_revision).filter(DbSession.archived == False)
            q = owner_filter(q, DbSession, user)
            rows = q.all()
            for row in rows:
                folder_map[row.id] = row.folder
                token_map[row.id] = (row.total_input_tokens or 0) + (row.total_output_tokens or 0)
                important_map[row.id] = row.is_important or False
                style_map[row.id] = (row.response_style, row.response_style_revision)
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
                     "response_style": style_map.get(s.id, ("normal", 0))[0],
                     "response_style_revision": style_map.get(s.id, ("normal", 0))[1],
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
        response_style: str = Form("normal"),
    ):
        from src.openclank.response_style import normalize_mode
        try: response_style = normalize_mode(response_style)
        except ValueError as exc: raise HTTPException(422, "Invalid response style") from exc
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
        
        connector = getattr(request.state, "connector_create", None)
        sid = connector["session_id"] if connector else str(uuid.uuid4())
        user = effective_user(request)
        def connector_replay():
            if connector is None:
                return None
            db = SessionLocal()
            try:
                row = db.query(DbSession).filter(DbSession.id == sid).first()
                if row is None:
                    return None
                if (row.owner != user or row.connector_create_digest != connector["digest"]
                        or row.workspace_id != (workspace_value or None)
                        or row.endpoint_id != (endpoint_id.strip() or None)
                        or row.model != model_to_use or row.name != (name or "")
                        or row.provider_model_route_id != (selected_route.model_route_id if selected_route else None)):
                    raise HTTPException(409, "Operation key already bound to another session request")
                if not connector["identity"].current():
                    raise HTTPException(401, "Connector credential changed")
                return SessionResponse(id=sid, name=row.name, model=row.model, endpoint_url=row.endpoint_url,
                    endpoint_id=row.endpoint_id, rag=row.rag, archived=row.archived, workspace_id=row.workspace_id,
                    response_style=row.response_style, response_style_revision=row.response_style_revision)
            finally:
                db.close()
        if connector is not None:
            if user != connector["identity"].owner or not connector["identity"].current():
                raise HTTPException(401, "Connector credential changed")
            replay = connector_replay()
            if replay is not None:
                return replay
        is_incognito = str(incognito).lower() == "true"
        if is_incognito:
            if response_style != 'normal': raise HTTPException(409, 'Response style unavailable for incognito')
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
                response_style=response_style, response_style_revision=0,
            )
            session_manager.sessions[sid] = session
        else:
            try:
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
                response_style=response_style,
                connector_create_digest=connector["digest"] if connector else None,
                connector_current=connector["identity"].current if connector else None,
                )
            except Exception:
                if connector is None:
                    raise
                replay = connector_replay()
                if replay is None:
                    raise
                return replay
        # Managed provider credentials are leased inside the engine. Session
        # metadata is always secret-free.
        session.headers = {}
        from src.openclank.code_index_lifecycle import session_workspace_index_event
        await session_workspace_index_event(request, session)
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
            response_style=response_style, response_style_revision=0,
        )    
    def _response_style_identity(request: Request):
        from src.auth_helpers import require_user, is_internal_tool_identity
        owner = require_user(request)
        state = request.app.state
        auth = getattr(state, 'auth_manager', None)
        barrier = getattr(state, 'account_request_barrier', None)
        factory = getattr(state, 'capture_original_credential', None)
        if (is_internal_tool_identity(owner) or getattr(request.state, 'api_token', False)
                or getattr(request.state, 'internal_tool_authenticated', False)
                or auth is None or barrier is None or not callable(factory)):
            raise HTTPException(403, 'Named human authority unavailable')
        account = auth.account_id(owner)
        if not account or auth.username_for_account_id(account) != owner:
            raise HTTPException(403, 'Named human authority unavailable')
        try: credential = factory(request, owner, account)
        except Exception as exc: raise HTTPException(403, 'Named human authority unavailable') from exc
        if not callable(credential): raise HTTPException(403, 'Named human authority unavailable')
        return owner, account, state, auth, barrier, factory, credential

    async def _response_style_access(request: Request, sid: str, identity, body=None):
        from src.openclank.response_style import normalize_mode, style_snapshot, MAX_REVISION
        owner, account, state, auth, barrier, factory, credential = identity
        if body is not None:
            if type(body) is not dict or set(body) != {'mode', 'expected_revision'}:
                raise HTTPException(422, 'Invalid response style request')
            try:
                mode = normalize_mode(body['mode'])
                expected = style_snapshot(mode, body['expected_revision'])['revision']
            except ValueError as exc:
                raise HTTPException(422, 'Invalid response style request') from exc
        async with barrier.admitted(owner):
            if (getattr(state,'auth_manager',None) is not auth or getattr(state,'account_request_barrier',None) is not barrier
                    or getattr(state,'capture_original_credential',None) is not factory or credential() is not True
                    or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner):
                raise HTTPException(403, 'Named human authority unavailable')
            ghost = session_manager.sessions.get(sid)
            if ghost is not None and getattr(ghost, 'incognito', False):
                raise HTTPException(404, 'Session not found')
            with SessionLocal() as db:
                row = db.query(DbSession).filter(DbSession.id == sid, DbSession.owner == owner).first()
                if row is None: raise HTTPException(404, 'Session not found')
                current = style_snapshot(row.response_style, row.response_style_revision)
                if body is None: return current
                if current['revision'] != expected: raise HTTPException(409, 'Response style changed')
                if mode == current['mode']:
                    matched = db.query(DbSession).filter(DbSession.id == sid, DbSession.owner == owner,
                        DbSession.response_style == mode, DbSession.response_style_revision == expected).update(
                        {DbSession.response_style: mode}, synchronize_session=False)
                    if matched != 1:
                        db.rollback(); raise HTTPException(409, 'Response style changed')
                    db.commit()
                    return current
                if expected == MAX_REVISION: raise HTTPException(409, 'Response style revision exhausted')
                changed = db.query(DbSession).filter(DbSession.id == sid, DbSession.owner == owner,
                    DbSession.response_style_revision == expected).update(
                    {DbSession.response_style: mode, DbSession.response_style_revision: expected + 1}, synchronize_session=False)
                if changed != 1:
                    db.rollback(); raise HTTPException(409, 'Response style changed')
                db.commit()
                return style_snapshot(mode, expected + 1)

    @router.get('/session/{sid}/response-style')
    async def get_response_style(request: Request, sid: str):
        return await _response_style_access(request, sid, _response_style_identity(request))

    @router.put('/session/{sid}/response-style')
    async def put_response_style(request: Request, sid: str):
        identity = _response_style_identity(request)
        try:
            raw = await request.body()
            if len(raw) > 4096: raise ValueError('response style request too large')
            body = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, 'Invalid response style request') from exc
        if type(body) is not dict: raise HTTPException(422, 'Invalid response style request')
        return await _response_style_access(request, sid, identity, body)

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
            from src.openclank.code_index_lifecycle import session_workspace_index_event
            if prior_workspace and workspace_value != prior_workspace:
                await session_workspace_index_event(
                    request, session, action="release", workspace_id=prior_workspace,
                )
            if not session_manager.update_session_workspace(
                sid,
                workspace_value or None,
            ):
                raise HTTPException(404, f"Session {sid} not found")
            result["workspace_id"] = workspace_value or None
            if workspace_value:
                await session_workspace_index_event(request, session)
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
            session = session_manager.get_session(sid, allow_pending_history=True)
        except KeyError:
            raise HTTPException(404, f"Session {sid} not found")
        if not getattr(session, "provider_model_route_id", None):
            return {"available": False, "native_route_selected": False, "reason": "Session does not use Open Clank agent"}

        owner = effective_user(request) or getattr(session, "owner", None)
        if not owner:
            raise HTTPException(403, "Open Clank agent sessions require an authenticated owner")
        from src.openclank.transcript_projection import get_mimo_state as _get_state

        state = _get_state(sid, owner=owner)
        if refresh:
            supervisor = getattr(request.app.state, "mimo_supervisor", None)
            if supervisor is None:
                raise HTTPException(503, "Open Clank engine is unavailable")
            try:
                state = await supervisor.negotiate_session(
                    sid, owner=owner
                )
            except RuntimeError as exc:
                raise HTTPException(503, str(exc)) from exc
        if not state:
            return {"available": False, "native_route_selected": True, "reason": "SESSION_NOT_READY", "retryable": True}
        return {"available": True, "native_route_selected": True, **state}

    async def _plan_write(request: Request, sid: str, operation: str):
        # Freeze the authenticated account before the first suspension.
        owner = effective_user(request)
        auth = getattr(request.app.state, "auth_manager", None)
        barrier = getattr(request.app.state, "account_request_barrier", None)
        account = auth.account_id(owner) if auth and owner and getattr(request.state, "authenticated", False) else None
        if not account or not barrier or auth.username_for_account_id(account) != owner:
            raise HTTPException(403, "Plan account unavailable")
        async with barrier.admitted(owner):
            def current():
                return auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
            if not current(): raise HTTPException(409, "Plan account changed")
            _verify_session_owner(request, sid, session_manager)
            try:
                body = await request.json() if operation != "clear" else {}
                if not isinstance(body, dict): raise ValueError("invalid request")
                if not current(): raise ValueError("plan account changed")
                from src.plan_approval import approve_plan, save_plan_draft, clear_plan
                if operation == "approve":
                    revision, digest = int(body.get("revision")), str(body.get("digest") or "")
                    if revision <= 0 or len(digest) != 64: raise ValueError("revision and digest are required")
                    plan = approve_plan(sid, owner, revision=revision, digest=digest, current_authority=current)
                elif operation == "draft":
                    plan = save_plan_draft(sid, owner, str(body.get("plan") or ""), current_authority=current)
                else:
                    plan = clear_plan(sid, owner, current_authority=current)
                return {"ok": True, "plan_state": plan}
            except KeyError as exc:
                raise HTTPException(404, "Session plan is unavailable") from exc
            except (TypeError, ValueError, AttributeError, json.JSONDecodeError) as exc:
                raise HTTPException(409, str(exc)) from exc

    @router.post("/session/{sid}/plan/approve")
    async def approve_plan(request: Request, sid: str):
        return await _plan_write(request, sid, "approve")

    @router.post("/session/{sid}/plan/draft")
    async def save_plan_draft(request: Request, sid: str):
        return await _plan_write(request, sid, "draft")

    @router.post("/session/{sid}/plan/clear")
    async def clear_plan(request: Request, sid: str):
        return await _plan_write(request, sid, "clear")

    async def _plan_feedback_request(request: Request, sid: str, *, write: bool):
        owner = effective_user(request)
        auth = getattr(request.app.state, "auth_manager", None)
        barrier = getattr(request.app.state, "account_request_barrier", None)
        account = auth.account_id(owner) if auth and owner and getattr(request.state, "authenticated", False) else None
        if not account or not barrier or auth.username_for_account_id(account) != owner:
            raise HTTPException(403, "Feedback account unavailable")
        repository = getattr(request.app.state, 'files_policy_repository', None)
        capture = getattr(request.app.state, 'capture_original_credential', None)
        try:
            credential_current = capture(request, owner, account)
        except Exception as exc:
            raise HTTPException(403, "Feedback credential unavailable") from exc
        async with barrier.admitted(owner):
            files_generation=None
            def current():
                return (request.app.state.auth_manager is auth and request.app.state.account_request_barrier is barrier
                    and getattr(request.app.state, 'files_policy_repository', None) is repository
                    and credential_current() and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                    and (files_generation is None or repository.generation()==files_generation))
            if not current(): raise HTTPException(409, "Feedback account changed")
            _verify_session_owner(request, sid, session_manager)
            try:
                from src.openclank import plan_feedback
                if not write:
                    if request.query_params.get('scope') == '1':
                        actor=request.query_params.get('actor'); actor_session=request.query_params.get('actor_session')
                        return {'scope':await plan_feedback.capture_feedback_scope(request,sid,owner,account,session_manager,actor_id=actor,actor_session_id=actor_session,current_authority=current)}
                    return {"entries": plan_feedback.read(sid, owner, account, current_authority=current)}
                body = await request.json()
                if type(body) is not dict or set(body) - {"request_key", "text", "revision", "digest", "feedback_id", "comment_revision", "fileTarget", "resourceTarget", "retarget", "batchMembers"}:
                    raise ValueError("invalid feedback request")
                if 'batchMembers' in body:
                    if set(body)!={'request_key','batchMembers'}:raise ValueError('invalid feedback batch')
                    prepared=plan_feedback.inspect_batch_members(sid,owner,account,request_key=body['request_key'],members=body['batchMembers'],current_authority=current)
                    if 'existing' in prepared:return {'entry':prepared['existing']}
                    target=prepared['target'];files_generation=repository.generation()
                    if await plan_feedback.recapture_feedback_scope(request,sid,owner,account,session_manager,target['scope'],current_authority=current)!=target['scope']:
                        raise ValueError('feedback scope changed')
                    from src.openclank.plan_feedback_files import capture_feedback_resource
                    verified=[]
                    for item in target['items']:
                        frozen={k:item[k] for k in ('resource','anchor','anchorVerification') if k in item}
                        verified.append(await capture_feedback_resource(request,target['binding'],account,frozen=frozen))
                        if not current():raise ValueError('feedback account changed')
                    for index,item in enumerate(target['items']):
                        frozen={k:item[k] for k in ('resource','anchor','anchorVerification') if k in item}
                        if await capture_feedback_resource(request,target['binding'],account,frozen=frozen)!=verified[index] or not current():raise ValueError('feedback batch changed')
                    if not current() or await plan_feedback.recapture_feedback_scope(request,sid,owner,account,session_manager,target['scope'],current_authority=current)!=target['scope']:
                        raise ValueError('feedback scope changed')
                    return {'entry':plan_feedback.save_resource_batch(sid,owner,account,request_key=body['request_key'],members=body['batchMembers'],observed_target=target,verified_members=verified,current_authority=current)}
                if 'resourceTarget' in body:
                    if 'fileTarget' in body or 'revision' in body or 'digest' in body: raise ValueError('invalid feedback target')
                    spec=body['resourceTarget']
                    if type(spec) is not dict or set(spec) not in ({'resourceRef','expectedRevision','anchor','scope'},{'resourceRef','expectedRevision','anchor','scope','designContext'}): raise ValueError('invalid feedback target')
                    observed_scope=await plan_feedback.recapture_feedback_scope(request,sid,owner,account,session_manager,spec['scope'],current_authority=current)
                    if plan_feedback._scope(spec['scope'])!=observed_scope: raise ValueError('feedback scope changed')
                    binding=plan_feedback.inspect_resource_binding(sid,owner,current_authority=current)
                    from src.openclank.plan_feedback_files import capture_feedback_resource
                    files_generation=request.app.state.files_policy_repository.generation()
                    if 'designContext' in spec and (type(spec['designContext']) is not dict or type(spec['designContext'].get('output')) is not dict): raise ValueError('design context unavailable')
                    require_source_write='designContext' in spec and spec['designContext']['output'].get('destination')!='new-file'
                    resource=await capture_feedback_resource(request,binding,account,resource_ref=spec['resourceRef'],expected_revision=spec['expectedRevision'],anchor=spec['anchor'],require_write=require_source_write)
                    design=None
                    if 'designContext' in spec:
                        from src.openclank.plan_feedback_files import capture_design_context
                        design=await capture_design_context(request,binding,account,spec['designContext'],resource['resource'])
                        if await capture_feedback_resource(request,binding,account,frozen=resource,require_write=require_source_write)!=resource: raise ValueError('design source changed')
                        if await capture_design_context(request,binding,account,design,resource['resource'],frozen=True)!=design: raise ValueError('design assets changed')
                    if not current() or await plan_feedback.recapture_feedback_scope(request,sid,owner,account,session_manager,observed_scope,current_authority=current)!=observed_scope: raise ValueError('feedback scope changed')
                    return {'entry':plan_feedback.save_resource(sid,owner,account,request_key=body.get('request_key'),text=body.get('text'),resource=resource,observed_binding=binding,observed_scope=observed_scope,design_context=design,feedback_id=body.get('feedback_id'),comment_revision=body.get('comment_revision'),retarget=body.get('retarget',False),current_authority=current)}
                if 'retarget' in body: raise ValueError('invalid feedback target')
                file_target=None;observed_base=None
                if 'fileTarget' in body:
                    spec=body['fileTarget']
                    if type(spec) is not dict or set(spec) not in ({'resourceRef','expectedRevision'},{'resourceRef','expectedRevision','selection'}):raise ValueError('invalid feedback target')
                    base=plan_feedback.inspect(sid,owner,account,current_authority=current)
                    if base['revision']!=body.get('revision') or base['digest']!=body.get('digest'):raise ValueError('plan target changed')
                    observed_base=base
                    from src.openclank.plan_feedback_files import capture_feedback_file
                    files_generation=request.app.state.files_policy_repository.generation()
                    file_target=await capture_feedback_file(request,base,account,resource_ref=spec['resourceRef'],expected_revision=spec['expectedRevision'],selection=spec.get('selection'))
                    if not current():raise ValueError('feedback account changed')
                entry = plan_feedback.save(sid, owner, account, request_key=body.get("request_key"),
                    text=body.get("text"), revision=body.get("revision"), digest=body.get("digest"),
                    feedback_id=body.get("feedback_id"), comment_revision=body.get("comment_revision"),
                    file_target=file_target,observed_base=observed_base,current_authority=current)
                return {"entry": entry}
            except KeyError as exc: raise HTTPException(404, "Feedback unavailable") from exc
            except (ValueError, TypeError, RuntimeError) as exc: raise HTTPException(409, str(exc)) from exc

    @router.get("/session/{sid}/plan/feedback")
    async def read_plan_feedback(request: Request, sid: str):
        return await _plan_feedback_request(request, sid, write=False)

    @router.post("/session/{sid}/plan/feedback")
    async def save_plan_feedback(request: Request, sid: str):
        return await _plan_feedback_request(request, sid, write=True)

    @router.post('/session/{sid}/plan/feedback/{feedback_id}/verify-file')
    async def verify_plan_feedback_file(request: Request, sid: str, feedback_id: str):
        from src.openclank import plan_feedback
        from src.openclank.plan_feedback_files import capture_feedback_file
        owner=effective_user(request);auth=getattr(request.app.state,'auth_manager',None);barrier=getattr(request.app.state,'account_request_barrier',None)
        account=auth.account_id(owner) if auth and owner and getattr(request.state,'authenticated',False) else None
        if not account or not barrier or auth.username_for_account_id(account)!=owner:raise HTTPException(403,'Feedback account unavailable')
        repository=getattr(request.app.state,'files_policy_repository',None)
        capture=getattr(request.app.state,'capture_original_credential',None)
        try:
            credential_current=capture(request,owner,account)
        except Exception as exc:
            raise HTTPException(403,'Feedback credential unavailable') from exc
        try:
            async with barrier.admitted(owner):
                current=lambda: (request.app.state.auth_manager is auth and request.app.state.account_request_barrier is barrier
                    and getattr(request.app.state,'files_policy_repository',None) is repository and credential_current()
                    and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner)
                _verify_session_owner(request,sid,session_manager)
                saved=plan_feedback.inspect(sid,owner,account,feedback_id=feedback_id,current_authority=current)
                if saved['target'].get('kind')=='resource':
                    from src.openclank.plan_feedback_files import capture_feedback_resource
                    binding=plan_feedback.inspect_resource_binding(sid,owner,current_authority=current)
                    if binding!=saved['target']['binding']: raise ValueError('feedback target changed')
                    if 'designContext' in saved['target']:
                        from src.openclank.plan_feedback_files import compare_design_input
                        repository=request.app.state.files_policy_repository; generation=repository.generation()
                        inputs=[saved['target']['resource'],*saved['target']['designContext']['assets']]
                        standard_source=saved['target']['designContext'].get('standardSource')
                        if standard_source: inputs.append(standard_source['resource'])
                        comparisons=[await compare_design_input(request,binding,account,item) for item in inputs]
                        if not current() or request.app.state.files_policy_repository is not repository or repository.generation()!=generation or plan_feedback.inspect_resource_binding(sid,owner,current_authority=current)!=binding: raise ValueError('feedback target changed')
                        destination_state=None
                        if saved['target']['designContext']['output']['destination']=='new-file':
                            from src.openclank.plan_feedback_files import capture_design_folder
                            try:
                                await capture_design_folder(request,binding,account,saved['target']['designContext']['output']['folder'],frozen=True)
                                destination_state={'state':'current'}
                            except Exception:
                                destination_state={'state':'unavailable'}
                        result={'state':'current' if all(x['state']=='current' for x in comparisons) and (destination_state is None or destination_state['state']=='current') else 'changed',
                            'designInputs':comparisons,**({'standardInputIndex':len(comparisons)-1} if standard_source else {}),**({'designDestination':destination_state} if destination_state is not None else {}),'anchorState':'verified' if saved['target']['anchor']['kind'] in ('whole','text') or 'anchorVerification' in saved['target'] else 'unverified'}
                        if comparisons[0]['state']=='current':
                            _,result['resourceRef']=await capture_feedback_resource(request,binding,account,frozen=saved['target'],return_ref=True)
                        if not current() or request.app.state.files_policy_repository is not repository or repository.generation()!=generation or plan_feedback.inspect_resource_binding(sid,owner,current_authority=current)!=binding: raise ValueError('feedback target changed')
                        return result
                    _,ref=await capture_feedback_resource(request,binding,account,frozen=saved['target'],return_ref=True)
                    if not current() or plan_feedback.inspect_resource_binding(sid,owner,current_authority=current)!=binding: raise ValueError('feedback target changed')
                    return {'state':'current','resourceRef':ref,'anchorState':'verified' if saved['target']['anchor']['kind'] in ('whole','text') or 'anchorVerification' in saved['target'] else 'unverified'}
                base=plan_feedback.inspect(sid,owner,account,current_authority=current)
                if plan_feedback._base(saved['target'])!=base or not saved['target'].get('file'):raise ValueError('feedback target changed')
                _,ref=await capture_feedback_file(request,saved['target'],account,frozen=saved['target']['file'],return_ref=True)
                if not current() or plan_feedback.inspect(sid,owner,account,current_authority=current)!=base:raise ValueError('feedback target changed')
                return {'state':'current','resourceRef':ref}
        except (ValueError,TypeError,RuntimeError,KeyError):raise HTTPException(409,'Feedback file is stale or unavailable')

    async def _goal_proxy(
        request: Request,
        sid: str,
        method: str,
        *,
        suffix: str = "goal",
        payload: dict | None = None,
        timeout: float = 20.0,
        before_issue=None,
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
                **({'before_issue':before_issue} if before_issue is not None else {}),
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

    @router.post("/session/{sid}/mimo-operation")
    async def native_session_operation(request: Request, sid: str):
        """Bounded native operations under canonical host session ownership."""
        owner = _verify_session_owner(request, sid, session_manager)
        body = await request.json()
        if not isinstance(body, dict): raise HTTPException(400, "Operation must be an object")
        operation = body.get("operation")
        allowed_keys = {
            "commands-refresh": {"operation", "reload_skills"},
            "compact": {"operation"}, "btw": {"operation", "question"},
            "recovery": {"operation"}, "recover": {"operation", "assistantMessageID", "turnPolicy"},
            "rebuild": {"operation", "requestID", "turnPolicy"},
            "timeline": {"operation"}, "undo": {"operation", "host_message_id", "expected_transcript_revision"},
            "redo": {"operation", "expected_transcript_revision"}, "history-reconcile": {"operation", "expected_transcript_revision"},
            "fork-source": {"operation"},
            "fork": {"operation", "expected_transcript_revision", "expected_source_key", "host_message_id", "title", "request_id"},
            "fork-status": {"operation", "child_session_id"},
            "fork-reconcile": {"operation", "expected_transcript_revision", "child_session_id"},
        }
        if not isinstance(operation, str) or operation not in allowed_keys:
            raise HTTPException(400, "Unsupported native session operation")
        if set(body) - allowed_keys[operation]: raise HTTPException(400, "Unsupported operation fields")
        if operation in {"fork-source", "fork", "fork-status", "fork-reconcile"}:
            from src.openclank.session_fork import ForkOperationError, fork_receipt_status, fork_source_key, fork_source_kind
            with SessionLocal() as db:
                row = db.query(DbSession).filter(DbSession.id == sid, DbSession.owner == owner).first()
                if row is None or row.archived:
                    raise HTTPException(409, "Fork requires an active saved chat")
                kind = fork_source_kind(row)
                source = {"kind": kind, "workspace_id": row.workspace_id,
                          "transcript_revision": int(row.transcript_revision or 0)}
                if kind == "native":
                    source["source_key"] = fork_source_key(row)
            if operation == "fork-source":
                from src.openclank.conversation_archive import get_conversation_archive
                if get_conversation_archive().chat_erasure_started(owner=owner, chat_id=sid):
                    raise HTTPException(409, "Original fork source is unavailable")
                return {"operation": operation, "result": source}
            if kind != "native":
                raise HTTPException(409, "This chat only supports transcript copies")
            auth = getattr(request.app.state, "auth_manager", None)
            account = auth.account_id(owner) if auth else None
            if not account or auth.username_for_account_id(account) != owner:
                raise HTTPException(403, "Original fork account is unavailable")
            candidate_id = body.get("child_session_id") if operation != "fork" else body.get("request_id")
            if candidate_id is not None or operation == "fork-reconcile":
                try:
                    if not isinstance(candidate_id, str) or str(uuid.UUID(candidate_id)) != candidate_id:
                        raise ValueError("invalid candidate selector")
                except (ValueError, AttributeError) as exc:
                    raise HTTPException(400, "Invalid original fork selector") from exc
            if operation == "fork":
                if ("expected_source_key" in body and body["expected_source_key"] != source["source_key"]):
                    raise HTTPException(409, "The original Native source changed. Select it again.")
                host_message = body.get("host_message_id")
                if host_message is not None and (not isinstance(host_message, str) or not host_message or len(host_message) > 256):
                    raise HTTPException(400, "Invalid original source message ID")
            if operation in {"fork-status", "fork-reconcile"}:
                if not source["workspace_id"]:
                    raise HTTPException(409, "Original fork workspace is unavailable")
                _validated_session_workspace(request, owner, source["workspace_id"])
                try:
                    receipt = fork_receipt_status(sid=sid, owner=owner, account_id=account, candidate_id=candidate_id)
                except ForkOperationError as exc:
                    raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
                if operation == "fork-status" or receipt["status"] == "settled":
                    return {"operation": operation, "result": receipt}
                if receipt["status"] != "pending":
                    raise HTTPException(409, {"code": "FORK_RECEIPT_UNAVAILABLE",
                                              "message": "The original fork receipt is unavailable or stale",
                                              "candidate_id": candidate_id})
            if operation == "fork":
                body["expected_source_key"] = source["source_key"]
            body["_source_account_id"] = account
        if operation in {"timeline", "undo", "redo", "history-reconcile", "fork", "fork-reconcile"}:
            session = session_manager.get_session(sid, allow_pending_history=True)
        else:
            session = session_manager.get_session(sid)
        if not session.provider_model_route_id and operation not in {"fork", "fork-reconcile"}:
            raise HTTPException(409, "Session does not use Open Clank agent")
        if operation == "commands-refresh":
            reload_skills = body.get("reload_skills", False)
            if type(reload_skills) is not bool: raise HTTPException(400, "reload_skills must be boolean")
            control = getattr(getattr(request.app.state, "mimo_supervisor", None), "ready_refresh_commands", None)
            if not callable(control): raise HTTPException(503, "Ready command refresh is unavailable")
            from src.openclank.mimo_supervisor import SupervisorAdmissionError
            try: result = await control(owner, session_id=sid, reload_skills=reload_skills)
            except SupervisorAdmissionError as exc: raise HTTPException(exc.status, {"code": exc.code, "message": "Catalog target is not ready", "retryable": exc.retryable}) from exc
            return {"operation": operation, "result": result}
        if operation in {"fork", "fork-reconcile"}:
            from src import agent_runs
            if agent_runs.is_active(sid):
                raise HTTPException(409, "Wait for the active chat turn to finish before forking")
            control = getattr(getattr(request.app.state, "mimo_supervisor", None), "session_fork_operation", None)
            if not callable(control):
                raise HTTPException(503, {"code": "FORK_OWNER_UNAVAILABLE", "message": "Native fork controls are unavailable",
                                          "not_issued": operation == "fork"})
            try:
                result = await control(sid, owner=owner, operation=operation, payload=body, manager=session_manager)
            except ForkOperationError as exc:
                raise HTTPException(exc.status, {"code": exc.code, "message": str(exc), "candidate_id": exc.candidate_id}) from exc
            except Exception as exc:
                from src.openclank.agent_supervisor import AgentSupervisorAdmissionError
                if isinstance(exc, AgentSupervisorAdmissionError):
                    raise HTTPException(exc.status, {"code": exc.code, "message": "Native fork owner is unavailable",
                                                      "retryable": exc.retryable, "candidate_id": body.get("child_session_id"),
                                                      "not_issued": operation == "fork"}) from exc
                raise
            return {"operation": operation, "result": result}
        if operation in {"timeline", "undo", "redo", "history-reconcile"}:
            if operation != "timeline": _reject_compact_during_active_run(sid)
            from src.openclank.session_operations import HistoryOperationError
            control = getattr(getattr(request.app.state, "mimo_supervisor", None), "session_history_operation", None)
            if not callable(control): raise HTTPException(503, "Native history controls are unavailable")
            file_authority = None
            if operation in {"undo", "redo", "history-reconcile"}:
                # A missing human/Files owner leaves transcript controls usable,
                # but cannot grant a file effect or synthesize a human request.
                try:
                    from src.openclank.session_file_history import human_file_authority
                    file_authority = human_file_authority(request, sid=sid, owner=owner, session_manager=session_manager)
                except Exception:
                    file_authority = None
            try:
                result = await control(sid, owner=owner, operation=operation, payload=body, file_authority=file_authority)
            except HistoryOperationError as exc:
                raise HTTPException(exc.status, {"code": exc.code, "message": str(exc), "choices": exc.choices}) from exc
            except Exception as exc:
                from src.openclank.agent_supervisor import AgentSupervisorAdmissionError
                if isinstance(exc, AgentSupervisorAdmissionError): raise HTTPException(exc.status, {"code": exc.code, "message": "Native history owner is unavailable", "retryable": exc.retryable}) from exc
                raise
            draft = result.get("restore_draft") if isinstance(result, dict) else None
            if isinstance(draft, dict):
                refs = draft.get("attachment_refs") or []
                resolver = getattr(upload_handler, "resolve_upload", None)
                missing = []
                for ref in refs:
                    identifier = ref.get("attachment_id")
                    try: available = callable(resolver) and resolver(identifier, owner=owner, allow_admin=False)
                    except (ValueError, PermissionError, OSError): available = None
                    if not available: missing.append(identifier)
                draft["available"] = not missing
                draft["unavailable_attachment_ids"] = missing
                if missing: draft["reason"] = "OWNER_UPLOAD_UNAVAILABLE"
            return {"operation": operation, "result": result}
        if operation == "recovery":
            return {"operation": operation, "result": await _goal_proxy(request, sid, "GET", suffix="recovery")}
        if operation in {"compact", "recover", "rebuild"}: _reject_compact_during_active_run(sid)
        if operation in {"recover", "rebuild"}:
            candidate_id = body.get("assistantMessageID") if operation == "recover" else body.get("requestID")
            if operation == "recover":
                if not isinstance(candidate_id, str) or not re.fullmatch(r"msg_[A-Za-z0-9]+", candidate_id):
                    raise HTTPException(400, "assistantMessageID must identify an interrupted candidate")
            elif not isinstance(candidate_id, str) or not re.fullmatch(r"rebuild_[0-9a-f]{32}", candidate_id):
                raise HTTPException(400, "requestID must identify the original human rebuild request")
            from src.tool_policy import normalize_turn_policy_snapshot
            try:
                policy = normalize_turn_policy_snapshot(body.get("turnPolicy"))
            except ValueError as exc:
                raise HTTPException(409, {"code": "RECOVERY_POLICY_UNAVAILABLE",
                    "message": "Recovery requires explicit current chat policy; reload and retry."}) from exc
            if policy["incognito"] or policy["noMemory"] or policy["useResearch"] or policy["compareMode"]:
                raise HTTPException(409, {"code": "RECOVERY_POLICY_UNSUPPORTED",
                    "message": "This recovery mode is unavailable; recovery has not started."})
            state=request.app.state; auth=getattr(state,'auth_manager',None)
            barrier=getattr(state,'account_request_barrier',None); factory=getattr(state,'capture_original_credential',None)
            reset_factory=getattr(state,'memory_reset_coordinator',None); lifecycle=getattr(state,'account_owner_lifecycle',None)
            if (auth is None or barrier is None or not callable(factory) or not callable(reset_factory) or lifecycle is None
                    or getattr(request.state,'api_token',False) or getattr(request.state,'internal_tool_authenticated',False)):
                raise HTTPException(403,"Named human recovery authority is unavailable")
            account=auth.account_id(owner)
            if not account or auth.username_for_account_id(account)!=owner:
                raise HTTPException(403,"Named human recovery authority is unavailable")
            credential=factory(request,owner,account)
            admission=await barrier.acquire(owner)
            def recovery_current():
                try:
                    barrier.assert_live_admission(owner, admission)
                    return (getattr(state, 'auth_manager', None) is auth
                        and getattr(state, 'account_request_barrier', None) is barrier
                        and getattr(state, 'capture_original_credential', None) is factory
                        and getattr(state, 'memory_reset_coordinator', None) is reset_factory
                        and getattr(state, 'account_owner_lifecycle', None) is lifecycle)
                except Exception:
                    return False
            async def recovery_preflight():
                from src.openclank.analysis_snapshot_host import _owned
                if not recovery_current(): return False
                def durable_current():
                    try:
                        return (credential() is True and auth.account_id(owner)==account
                            and auth.username_for_account_id(account)==owner
                            and not lifecycle.owner_has_active_operation(owner)
                            and not reset_factory().owner_is_fenced(owner)
                            and _verify_session_owner(request,sid,session_manager)==owner)
                    except Exception:
                        return False
                checked = await _owned(durable_current)
                return checked is True and recovery_current()
            try:
                if not await recovery_preflight(): raise HTTPException(409,"Managed recovery authority changed")
                if operation == "recover":
                    candidates = await _goal_proxy(request, sid, "GET", suffix="recovery")
                    if not isinstance(candidates, list) or not any(item.get("assistantMessageID") == candidate_id for item in candidates if isinstance(item, dict)):
                        raise HTTPException(409, "Interrupted candidate is no longer recoverable")
                supervisor = getattr(request.app.state, "mimo_supervisor", None)
                install = getattr(supervisor, "set_managed_policy_installer", None)
                if callable(install):
                    from routes.orchestration_routes import install_input_policy_resolver
                    from types import SimpleNamespace
                    policy_app = request.app
                    install(owner, lambda bridge: install_input_policy_resolver(SimpleNamespace(app=policy_app), bridge, session_manager))
                runtime_home = getattr(supervisor, "_runtime_home", None)
                prepare = getattr(supervisor, "prepare_managed_handoff", None)
                if not callable(runtime_home) or not callable(prepare):
                    raise HTTPException(503, "Managed recovery authority is unavailable")
                from src.openclank.session_map import OwnerSessionMap
                from src.openclank.orchestration_projection import resolve_binding, validate_actors, find_session_actor
                try:
                    mapping = OwnerSessionMap(runtime_home(owner) / "session-map.json", owner)
                    snapshot = mapping.read()
                    binding = resolve_binding(snapshot, owner, sid, snapshot["mapRevision"])
                    actors = validate_actors(await _goal_proxy(request, sid, "GET", suffix="actors"), binding.engine_session_id)
                    actor = find_session_actor(actors, "main", binding.engine_session_id)
                    if not actor or actor.get("mode") != "main":
                        raise ValueError("main actor is unavailable")
                    revision = actor.get("executionRevision")
                    if type(revision) is not int or revision < 0:
                        raise ValueError("main actor revision is unavailable")
                    actor_agent = actor.get("agent")
                    if not isinstance(actor_agent, str) or not actor_agent:
                        raise HTTPException(409, {"code": "RECOVERY_MODE_UNAVAILABLE", "message": "The current actor mode is unavailable."})
                    actor_mode = actor_agent if actor_agent in {"chat", "plan"} else "agent"
                    if actor_mode != policy["mode"]:
                        raise HTTPException(409, {"code": "RECOVERY_MODE_UNSUPPORTED", "message": "Recovery cannot change the actor mode."})
                    from routes.orchestration_routes import effective_receipt_policy_ceiling
                    ceiling = effective_receipt_policy_ceiling(request, owner, sid, {"turnPolicy": policy, "text": ""}, session_manager)
                    if ceiling["blockAllToolCalls"]:
                        raise HTTPException(409, {"code": "RECOVERY_POLICY_UNAVAILABLE", "message": "Current recovery authority is unavailable."})
                    route = resolve_chat_route(owner=owner, endpoint_id=session.endpoint_id,
                        model_id=session.model, model_route_id=session.provider_model_route_id)
                    model, context = actor.get("effectiveModel") or {}, actor.get("hostContext") or {}
                    if (model.get("providerID") != route.connection_id or model.get("modelID") != route.provider_model_id
                        or context.get("chatID") != sid or context.get("grantID") != route.provider_grant_id):
                        raise ValueError("main actor route or grant changed")
                    grant_revision = None
                    if route.provider_grant_id:
                        from src.openclank.provider_store import ProviderStore
                        grant_revision = ProviderStore().resolve_share_scope(recipient=owner, grant_id=route.provider_grant_id).revision
                        if context.get("grantRevision") != grant_revision:
                            raise ValueError("main actor grant revision changed")
                    recovery_id = uuid.uuid5(uuid.NAMESPACE_URL, f"openclank-{'recovery' if operation == 'recover' else 'manual-rebuild'}:{owner}:{sid}:{candidate_id}").hex
                    root_id, handoff_id = "recovery_" + recovery_id, recovery_id
                    managed_route = {"rootOperationID": root_id, "connectionID": route.connection_id,
                        "providerID": route.connection_id, "billingLane": route.billing_lane,
                        "modelID": route.provider_model_id, "modelRouteID": route.model_route_id,
                        **({"grantID": route.provider_grant_id, "grantRevision": grant_revision} if route.provider_grant_id else {}),
                        **({"inheritedAccountID": context["accountID"]} if context.get("accountID") else {})}
                    try:
                        if not await recovery_preflight(): raise ValueError("recovery human authority changed")
                        prepared = await prepare(owner=owner, host_chat_id=sid, engine_session_id=binding.engine_session_id,
                            root_session_id=binding.engine_session_id, root_operation_id=root_id,
                            request_id=candidate_id, handoff_id=handoff_id, actor_id="main", expected_execution_revision=revision,
                            admission_ceiling=ceiling, turn_policy=policy, input_text="",
                            recovery_human=(account,barrier,admission,recovery_current,recovery_preflight))
                        if not isinstance(prepared, dict) or prepared.get("prepared") is not True or prepared.get("replayed") is not False:
                            raise HTTPException(409, "Recovery status is unknown; reconcile the original request")
                    except ValueError as exc:
                        if "conflict" in str(exc) or "already reserved at another revision" in str(exc):
                            raise HTTPException(409, {"code": "RECOVERY_POLICY_CONFLICT",
                                "message": "This recovery identity is already bound to another policy or revision."}) from exc
                        raise
                except HTTPException:
                    raise
                except Exception as exc:
                    raise HTTPException(409, "Managed recovery authority is unavailable or changed") from exc
                # Keep the reservation on transport/unknown outcomes for exact reconciliation.
                if not recovery_current(): raise HTTPException(409, "Managed recovery authority changed")
                issue=asyncio.create_task(_goal_proxy(request, sid, "POST", suffix=f"turn/{candidate_id}/resume" if operation == "recover" else "manual-rebuild",
                    payload={"managedRecovery": {"rootOperationID": root_id, "handoffID": handoff_id,
                        "expectedExecutionRevision": revision, "managedRoute": managed_route, "turnPolicy": policy}},
                    before_issue=recovery_preflight, timeout=360.0 if operation == "rebuild" else 20.0))
                try:
                    result=await asyncio.shield(issue)
                except asyncio.CancelledError:
                    while not issue.done():
                        try: await asyncio.shield(issue)
                        except asyncio.CancelledError: continue
                        except Exception: break
                    issue.result()
                    raise
                if operation == "rebuild" and not await recovery_preflight():
                    raise HTTPException(409, "The original rebuild outcome is unknown; current authority changed")
            finally:
                await barrier.release(admission)
        else:
            route = resolve_chat_route(owner=owner, endpoint_id=session.endpoint_id, model_route_id=session.provider_model_route_id)
            payload = {"providerID": route.connection_id, "modelID": route.provider_model_id}
            if operation == "compact":
                payload["auto"] = False
                suffix = "summarize"
            else:
                question = body.get("question")
                if not isinstance(question, str) or not question.strip(): raise HTTPException(400, "A side question is required")
                payload["question"] = question
                suffix = "ask"
            result = await _goal_proxy(request, sid, "POST", suffix=suffix, payload=payload, timeout=120.0)
        if operation == "rebuild" and (type(result) is not dict or set(result) != {"outcome"}
                or result.get("outcome") not in {"rebuilt", "unavailable", "insert-failed", "compaction-requested"}):
            raise HTTPException(502, "The original rebuild outcome is unknown; do not retry automatically")
        return {"operation": operation, "result": result,
                **({"deliveryState": "pending"} if operation == "recover" else {})}

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

    @router.patch("/session/{sid}/engine-config")
    async def set_engine_config(request: Request, sid: str):
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
        if config_id == "agent_profile":
            _reject_compact_during_active_run(sid)
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
                auth=getattr(request.app.state,'auth_manager',None)
                account_for=getattr(auth,'account_id',None); owner_for=getattr(auth,'username_for_account_id',None)
                account_id=account_for(owner) if callable(account_for) and callable(owner_for) else None
                if account_id and owner_for(account_id) != owner:
                    account_id=None
                return await service.erase(owner=owner, session_id=sid,current_account_id=account_id)
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

    async def _selected_history_snapshot(request: Request, *, source_body=None, selection=None):
        """Stage and reauthorize one selected Files snapshot for preview or private selection."""
        import asyncio, hashlib, tempfile, sys
        from fastapi.responses import JSONResponse
        from src.openclank.files_principal import files_principal
        from src.openclank.files_facade import ProviderContext
        from src.openclank.resource_refs import resolve_resource_ref
        from src.openclank.session_transfer_preview import MAX_SOURCE, MAX_RESULT, PreviewUnavailable, copy_path, inspect_snapshot, inspect_selected_snapshot

        account, owner, is_admin = files_principal(request)
        from src.auth_helpers import is_internal_tool_identity
        if is_internal_tool_identity(owner) or getattr(request.state, 'internal_tool_authenticated', False):
            raise HTTPException(403, 'Selected history requires a human account')
        auth = getattr(request.app.state, 'auth_manager', None)
        barrier = getattr(request.app.state, 'account_request_barrier', None)
        factory = getattr(request.app.state, 'files_facade_factory', None)
        if not auth or not barrier or not callable(factory) or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner:
            raise HTTPException(403, 'Selected history is unavailable')

        async def owned(fn):
            task = asyncio.create_task(asyncio.to_thread(fn))
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if task.done(): result = task.result(); break
            if cancelled: raise asyncio.CancelledError
            return result

        async with barrier.admitted(owner):
            try:
                if source_body is None:
                    body = await _bounded_transfer_json(request, 8192)
                else:
                    body = source_body
                allowed = {'resourceRef','expectedRevision','threadID','cursor','sourceKind','workspaceID'}
                if type(body) is not dict or set(body) - allowed or body.get('sourceKind') != 'standalone-snapshot':
                    raise PreviewUnavailable('invalid snapshot request')
                token, expected = body.get('resourceRef'), body.get('expectedRevision')
                if type(token) is not str or len(token) > 4096 or type(expected) is not dict or set(expected) != {'kind','value'} or any(type(expected[k]) is not str or not expected[k] or len(expected[k]) > 512 for k in expected):
                    raise PreviewUnavailable('resource revision unavailable')
                if expected['kind'] != 'hostObjectIdentity': raise PreviewUnavailable('resource revision unavailable')
                if body.get('threadID') is not None and type(body['threadID']) is not str: raise PreviewUnavailable('invalid thread')
                if body.get('cursor') is not None and type(body['cursor']) is not str: raise PreviewUnavailable('invalid cursor')
                repo = FilePolicyRepository(); generation = repo.generation()
                ref = resolve_resource_ref(token, expected_owner_subject_id=account, current_policy_generation=generation, required_capability='download')
                workspace_id = body.get('workspaceID')
                if (sys.platform not in {'linux','darwin'} or os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_NONBLOCK')
                        or ref.provider != 'host' or ref.kind not in {'file','exact_file'}
                        or type(workspace_id) is not str or not workspace_id.strip()
                        or workspace_id != workspace_id.strip() or len(workspace_id) > 256):
                    raise PreviewUnavailable('selected workspace unavailable')
                workspace = resolve_owned_workspace(repo, workspace_id=workspace_id, owner_username=owner,
                    auth_manager=auth, purpose='app_folder', observation_only=True)
                from src.openclank.files_host_provider import _path as host_path
                source_path = host_path(ref.origin_id)
                root_path = os.path.realpath(workspace.path)
                def inside_workspace():
                    candidate = os.path.realpath(source_path)
                    return bool(len(candidate) <= 4096 and os.path.commonpath((root_path, candidate)) == root_path and candidate != root_path)
                if not inside_workspace(): raise PreviewUnavailable('source outside workspace')
                context = ProviderContext(account, owner, generation, is_admin, workspace.workspace.id)
                files = factory(request, context)
                def current():
                    try:
                        return (auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                            and repo.generation() == generation and inside_workspace()
                            and resolve_owned_workspace(repo, workspace_id=workspace_id,
                                owner_username=owner, auth_manager=auth, purpose='app_folder', observation_only=True) == workspace)
                    except Exception: return False
                if not current(): raise PreviewUnavailable('source authority changed')
                before = await asyncio.wait_for(files.stat(context, resource_ref=token, purpose='inspect_sqlite'), timeout=10)
                if not current() or before.get('id') != ref.stable_id or before.get('revision') != expected:
                    raise PreviewUnavailable('source changed')
                size = before.get('size')
                if type(size) is not int or size < 0 or size > MAX_SOURCE: raise PreviewUnavailable('source size unavailable')
                source = await asyncio.wait_for(files.content(context, resource_ref=token, capability='download', expected_revision=expected), timeout=10)
                if not current() or source.size != size or source.etag != expected['value']:
                    raise PreviewUnavailable('source changed')
                with tempfile.TemporaryDirectory(prefix='openclank-history-preview-') as temporary:
                    path = os.path.join(temporary, 'snapshot.sqlite')
                    if source.path is not None:
                        digest = await owned(lambda: copy_path(source, path, size))
                    elif source.stream is not None:
                        digest_state = hashlib.sha256(); total = 0
                        with open(path, 'wb') as output:
                            async with asyncio.timeout(30):
                                async for chunk in source.stream(0, size):
                                    if not current() or not isinstance(chunk, bytes) or len(chunk) > 1024 * 1024:
                                        raise PreviewUnavailable('source stream unavailable')
                                    total += len(chunk)
                                    if total > size or total > MAX_SOURCE: raise PreviewUnavailable('source exceeds limit')
                                    digest_state.update(chunk)
                                    await owned(lambda chunk=chunk: output.write(chunk))
                        if total != size: raise PreviewUnavailable('source changed')
                        digest = digest_state.hexdigest()
                    else: raise PreviewUnavailable('source transport unavailable')
                    if not current(): raise PreviewUnavailable('source authority changed')
                    scope = hashlib.sha256(json.dumps({'account':account,'resource':ref.stable_id,'workspace':workspace.workspace.id,
                        'policy':generation,'revision':expected,'sha256':digest,'schema':'codex-thread-history-v6'},sort_keys=True,separators=(',',':')).encode()).hexdigest()
                    page = await owned(lambda: inspect_selected_snapshot(path, thread_id=body.get('threadID'), selectors=selection)
                        if selection is not None else inspect_snapshot(path, scope=scope, thread_id=body.get('threadID'), cursor=body.get('cursor')))
                after = await asyncio.wait_for(files.stat(context, resource_ref=token, purpose='inspect_sqlite'), timeout=10)
                if not current() or after.get('id') != ref.stable_id or after.get('revision') != expected or after.get('size') != size:
                    raise PreviewUnavailable('source changed')
                result = {'version':1,'available':True,'source':{'kind':'codex-thread-history-v6','snapshotSHA256':digest,
                    'snapshotBytes':size,'resourceID':ref.stable_id,'workspaceID':workspace.workspace.id,'resourceRevision':expected,'walCoverage':'unknown'},'page':page}
                if selection is not None:
                    async def source_current():
                        if not current(): return False
                        try:
                            final = await asyncio.wait_for(files.stat(context, resource_ref=token, purpose='inspect_sqlite'), timeout=10)
                            return (current() and final.get('id') == ref.stable_id and final.get('revision') == expected
                                    and final.get('size') == size)
                        except Exception:
                            return False
                    return result, current, source_current, workspace
                if len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_RESULT:
                    raise PreviewUnavailable('result exceeds limit')
                return JSONResponse(result, headers={'Cache-Control':'private, no-store'})
            except (ValueError, TypeError, OSError, UnicodeError, KeyError) as exc:
                if selection is not None:
                    raise PreviewUnavailable('selected source unavailable') from exc
                detail = str(exc) if isinstance(exc, PreviewUnavailable) else ''
                reason = ('unsupported_schema' if detail in {'unsupported schema','unsupported migrations','unsupported indexes'}
                    else 'stale_cursor' if detail == 'cursor unavailable'
                    else 'source_changed' if detail in {'source changed','source authority changed'}
                    else 'unavailable')
                return JSONResponse({'version':1,'available':False,'reason':reason}, status_code=409,
                    headers={'Cache-Control':'private, no-store'})

    @router.post('/session-transfer/backup-preference')
    async def selected_backup_preference(request: Request):
        """Preview or explicitly apply one account preference from a selected Files object."""
        import hashlib
        from fastapi.responses import JSONResponse
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user, _mutate_user
        from src.auth_helpers import is_internal_tool_identity
        from src.openclank.backup_preference_preview import MAX_SOURCE_BYTES, preview_backup
        from src.openclank.files_facade import ProviderContext
        from src.openclank.files_host_provider import _path as host_path
        from src.openclank.files_principal import files_principal
        from src.openclank.resource_refs import resolve_resource_ref

        field='deliverable_reduced_motion'
        issued=False
        try:
            account, owner, is_admin = files_principal(request)
            if (is_internal_tool_identity(owner) or getattr(request.state,'internal_tool_authenticated',False)
                    or getattr(request.state,'api_token',False)):
                raise ValueError('human account required')
            state=request.app.state
            auth=getattr(state,'auth_manager',None)
            barrier=getattr(state,'account_request_barrier',None)
            factory=getattr(state,'files_facade_factory',None)
            repo=getattr(state,'files_policy_repository',None)
            credential_factory=getattr(state,'capture_original_credential',None)
            if not auth or not barrier or not repo or not callable(factory) or not callable(credential_factory):
                raise ValueError('source unavailable')
            credential=credential_factory(request,owner,account)
            if not callable(credential) or credential() is not True:raise ValueError('account unavailable')
            body=await _bounded_transfer_json(request,8192)
            common={'phase','resourceRef','expectedRevision','workspaceID','expectedAccountID'}
            if type(body) is not dict or body.get('phase') not in ('preview','apply'):
                raise ValueError('request unavailable')
            expected_keys=common | ({'sourceSHA256','value','destinationBefore'} if body['phase']=='apply' else set())
            if set(body)!=expected_keys or body['expectedAccountID']!=account:raise ValueError('request unavailable')
            token,revision,workspace_id=body['resourceRef'],body['expectedRevision'],body['workspaceID']
            if (type(token) is not str or not token or len(token)>4096 or type(workspace_id) is not str
                    or not workspace_id or workspace_id!=workspace_id.strip() or len(workspace_id)>256
                    or type(revision) is not dict or set(revision)!={'kind','value'} or revision['kind']!='hostObjectIdentity'
                    or type(revision['value']) is not str or re.fullmatch(r'[0-9a-f]{64}',revision['value']) is None):
                raise ValueError('source unavailable')
            if body['phase']=='apply':
                before_value=body['destinationBefore']
                if (type(body['value']) is not bool or type(body['sourceSHA256']) is not str
                        or re.fullmatch(r'[0-9a-f]{64}',body['sourceSHA256']) is None
                        or type(before_value) is not dict or set(before_value)!={'present','value'}
                        or type(before_value['present']) is not bool
                        or (type(before_value['value']) is not bool if before_value['present'] else before_value['value'] is not None)):
                    raise ValueError('selection unavailable')
            def destination(prefs):
                if field not in prefs:return {'present':False,'value':None}
                if type(prefs[field]) is not bool:raise ValueError('destination unavailable')
                return {'present':True,'value':prefs[field]}
            async with barrier.admitted(owner):
                generation=repo.generation()
                ref=resolve_resource_ref(token,expected_owner_subject_id=account,current_policy_generation=generation,required_capability='download')
                if ref.provider!='host' or ref.kind not in {'file','exact_file'}:raise ValueError('source unavailable')
                workspace=resolve_owned_workspace(repo,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,
                    purpose='app_folder',observation_only=True)
                path=host_path(ref.origin_id);root=os.path.realpath(workspace.path)
                def current():
                    try:
                        candidate=os.path.realpath(path)
                        return (getattr(state,'auth_manager',None) is auth and getattr(state,'account_request_barrier',None) is barrier
                            and getattr(state,'files_policy_repository',None) is repo and getattr(state,'files_facade_factory',None) is factory
                            and getattr(state,'capture_original_credential',None) is credential_factory and credential() is True
                            and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                            and repo.generation()==generation and len(candidate)<=4096 and candidate!=root
                            and os.path.commonpath((root,candidate))==root
                            and resolve_owned_workspace(repo,workspace_id=workspace_id,owner_username=owner,
                                auth_manager=auth,purpose='app_folder',observation_only=True)==workspace)
                    except Exception:return False
                if not current():raise ValueError('source unavailable')
                context=ProviderContext(account,owner,generation,is_admin,workspace.workspace.id)
                files=factory(request,context)
                first=await asyncio.wait_for(files.stat(context,resource_ref=token,purpose='snapshot'),timeout=10)
                if (not current() or first.get('id')!=ref.stable_id or first.get('revision')!=revision
                        or first.get('kind') not in ('file','exact_file')):raise ValueError('source changed')
                size=first.get('size')
                if type(size) is not int or not 1<=size<=MAX_SOURCE_BYTES:raise ValueError('source unavailable')
                source=await asyncio.wait_for(files.content(context,resource_ref=token,capability='download',expected_revision=revision),timeout=10)
                if not current() or source.size!=size or source.etag!=revision['value']:raise ValueError('source changed')
                raw=bytearray()
                if source.data is not None:
                    if type(source.data) is not bytes or len(source.data)!=size:raise ValueError('source unavailable')
                    raw.extend(source.data)
                elif source.stream is not None:
                    async with asyncio.timeout(30):
                        async for chunk in source.stream(0,size):
                            if not current() or type(chunk) is not bytes or len(chunk)>1024*1024 or len(raw)+len(chunk)>size:
                                raise ValueError('source changed')
                            raw.extend(chunk)
                else:raise ValueError('source transport unavailable')
                if len(raw)!=size:raise ValueError('source changed')
                digest=hashlib.sha256(raw).hexdigest(); preview=preview_backup(bytes(raw));raw.clear()
                last=await asyncio.wait_for(files.stat(context,resource_ref=token,purpose='snapshot'),timeout=10)
                if (not current() or last.get('id')!=ref.stable_id or last.get('revision')!=revision or last.get('size')!=size):
                    raise ValueError('source changed')
                source_view={'resourceID':ref.stable_id,'workspaceID':workspace.workspace.id,'resourceRevision':revision,
                    'sourceSHA256':digest,'sourceBytes':size}
                if body['phase']=='preview':
                    with PREFERENCE_LOCK:
                        if not current():raise ValueError('account changed')
                        before_value=destination(_load_for_user(owner))
                    if not current():raise ValueError('account changed')
                    return {'version':1,'state':'preview','accountID':account,'source':source_view,'mapping':preview,'destinationBefore':before_value}
                if body['sourceSHA256']!=digest or body['value'] is not preview['value']:
                    raise ValueError('source changed')
                class DestinationConflict(Exception):pass
                def prepare(prefs):
                    if destination(prefs)!=before_value:raise DestinationConflict()
                    prefs[field]=preview['value']
                    return prefs,None
                try:
                    issued=True
                    saved,_=_mutate_user(owner,prepare,before_save=current)
                except DestinationConflict:
                    issued=False
                    return JSONResponse({'version':1,'state':'conflict'},status_code=409)
                # A post-issue source/account change cannot revoke a completed
                # preference write; report unknown rather than assert its outcome.
                after=await asyncio.wait_for(files.stat(context,resource_ref=token,purpose='snapshot'),timeout=10)
                if (not current() or after.get('id')!=ref.stable_id or after.get('revision')!=revision
                        or after.get('size')!=size or destination(saved)!={'present':True,'value':preview['value']}):
                    raise ValueError('outcome unavailable')
                return {'version':1,'state':'complete','accountID':account,'value':preview['value'],'sourceSHA256':digest}
        except asyncio.CancelledError:
            raise
        except Exception:
            return JSONResponse({'version':1,'state':'unknown' if issued else 'unavailable'},status_code=409)

    @router.post("/session-transfer/preview")
    async def preview_selected_session_history(request: Request):
        return await _selected_history_snapshot(request)

    @router.post("/session-transfer/import")
    async def import_selected_session_history(request: Request):
        import asyncio
        from fastapi.responses import JSONResponse
        from src.openclank.files_principal import files_principal
        from src.openclank.session_transfer_import import prepare_import, execute_selected_import, publish_import_map, hydrate_import_visibility, _hash
        from src.memory_scope import chat_workspace
        from src.model_dispatch import get_agent_supervisor
        account, owner, _ = files_principal(request)
        from src.auth_helpers import is_internal_tool_identity
        if is_internal_tool_identity(owner) or getattr(request.state, 'internal_tool_authenticated', False):
            raise HTTPException(403, 'Selected history requires a human account')
        auth = getattr(request.app.state, 'auth_manager', None)
        barrier = getattr(request.app.state, 'account_request_barrier', None)
        if not auth or not barrier or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner:
            raise HTTPException(403, 'Selected history is unavailable')
        selector = None
        async with barrier.admitted(owner):
            try:
                body = await _bounded_transfer_json(request, 65536)
                common = {'phase','resourceRef','expectedRevision','sourceKind','workspaceID','sourceSHA256','threadID','items','name'}
                if type(body) is not dict or body.get('phase') not in ('prepare','issue') or set(body) != (common | ({'selector'} if body.get('phase') == 'issue' else set())):
                    raise ValueError('invalid import request')
                if type(body['name']) is not str or body['name'].strip() in ({'Nobody','Incognito'} | _HIDDEN_SYSTEM_SESSION_NAMES):
                    raise ValueError('destination name is unavailable')
                source_body = {key:body[key] for key in ('resourceRef','expectedRevision','sourceKind','workspaceID','threadID')}
                selected, source_sync, source_current, workspace = await _selected_history_snapshot(request, source_body=source_body, selection=body['items'])
                source = selected['source']
                if body['sourceSHA256'] != source['snapshotSHA256'] or not await source_current(): raise ValueError('source changed')
                intent = prepare_import(account=account, owner=owner, resource_id=source['resourceID'],
                    snapshot_sha=source['snapshotSHA256'], workspace_id=source['workspaceID'], workspace_path=workspace.path,
                    thread_id=body['threadID'], name=body['name'], items=selected['page']['items'])
                p = intent['provenance']
                selector = {'sourceKey':intent['key'], 'snapshotSHA256':p['snapshotSHA256'],
                            'selectionSHA256':p['selectionSHA256'], 'intentSHA256':_hash(p)}
                if body['phase'] == 'prepare':
                    return JSONResponse({'version':1,'state':'prepared','selector':selector},headers={'Cache-Control':'private, no-store'})
                if body['selector'] != selector: raise ValueError('selection changed')
                supervisor = get_agent_supervisor()
                pool = supervisor.owner_lifecycle_pool()
                async def perform(phase, host_commit, utility):
                    reserved = await phase('reserve')
                    if reserved.get('state') != 'reserved' or reserved.get('fresh') is not True: return {'state':'unknown'}
                    issued = await phase('issue')
                    if issued.get('state') != 'host_issuing' or issued.get('fresh') is not True: return {'state':'unknown'}
                    if await host_commit() not in ('inserted','already_present'): return {'state':'unknown'}
                    host = await phase('ack_host')
                    if host.get('state') != 'host_committed': return {'state':'unknown'}
                    result = await publish_import_map(intent=intent, utility=utility, phase=phase, host_commit=host_commit,
                        receipt=host, current=lambda: utility._selected_import_current() and source_sync(),
                        memory_workspace_id=chat_workspace(), copal_workspace='default')
                    if result.get('state') == 'complete' and not await hydrate_import_visibility(session_manager, p, utility._selected_import_current):
                        return {'state':'visibility_unavailable'}
                    return result
                outcome = await execute_selected_import(pool=pool, barrier=barrier, auth=auth, owner=owner, account=account,
                    intent=intent, continuation=perform, source_current=source_current, source_current_sync=source_sync)
                state = outcome.get('state') if outcome.get('state') in ('complete','unknown','conflict','visibility_unavailable') else 'unknown'
                result = {'version':1,'state':state,'selector':selector}
                if state == 'complete': result['chatID'] = p['hostChatID']
                return JSONResponse(result,headers={'Cache-Control':'private, no-store'})
            except asyncio.CancelledError:
                raise
            except Exception:
                return JSONResponse({'version':1,'state':'unknown' if selector is not None else 'unavailable',
                    **({'selector':selector} if selector is not None else {})},status_code=409,headers={'Cache-Control':'private, no-store'})

    @router.post("/session-transfer/reconcile")
    async def reconcile_selected_session_history(request: Request):
        import asyncio
        from fastapi.responses import JSONResponse
        from src.openclank.files_principal import files_principal
        from src.openclank.session_transfer_import import execute_selected_import, reconcile_import_map, settle_reconciled_import, hydrate_import_visibility
        from src.model_dispatch import get_agent_supervisor
        account, owner, _ = files_principal(request)
        from src.auth_helpers import is_internal_tool_identity
        if is_internal_tool_identity(owner) or getattr(request.state, 'internal_tool_authenticated', False):
            raise HTTPException(403, 'Selected history requires a human account')
        auth = getattr(request.app.state, 'auth_manager', None)
        barrier = getattr(request.app.state, 'account_request_barrier', None)
        if not auth or not barrier or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner:
            raise HTTPException(403, 'Selected history is unavailable')
        async with barrier.admitted(owner):
            try:
                body = await _bounded_transfer_json(request, 4096)
                if type(body) is not dict or set(body) != {'selector'} or type(body['selector']) is not dict:
                    raise ValueError('invalid selector')
                selector = body['selector']
                if (set(selector) != {'sourceKey','snapshotSHA256','selectionSHA256','intentSHA256'}
                        or not re.fullmatch(r'v1:[a-f0-9]{64}:[a-f0-9]{64}', selector.get('sourceKey',''))
                        or any(type(selector.get(k)) is not str or not re.fullmatch(r'[a-f0-9]{64}',selector[k])
                               for k in ('snapshotSHA256','selectionSHA256','intentSHA256'))):
                    raise ValueError('invalid selector')
                intent = {'key':selector['sourceKey'], 'selector':selector, 'provenance':{'owner':owner,'account':account}}
                pool = get_agent_supervisor().owner_lifecycle_pool()
                async def observe(phase, _host_commit, utility):
                    receipt = await phase('reconcile')
                    p = receipt.get('provenance')
                    if type(p) is not dict or p.get('account') != account or p.get('owner') != owner:
                        return {'state':'unknown'}
                    repo = FilePolicyRepository()
                    generation = repo.generation()
                    workspace = resolve_owned_workspace(repo, workspace_id=p.get('workspaceID'), owner_username=owner,
                        auth_manager=auth, purpose='app_folder', observation_only=True)
                    binding = receipt.get('mapBinding') or {}
                    if binding.get('physicalCwd') != workspace.path:
                        return {'state':'unknown'}
                    result = await reconcile_import_map(provenance=p, receipt=receipt, utility=utility)
                    if (repo.generation() != generation or auth.account_id(owner) != account
                            or auth.username_for_account_id(account) != owner
                            or resolve_owned_workspace(repo, workspace_id=p.get('workspaceID'), owner_username=owner,
                                auth_manager=auth, purpose='app_folder', observation_only=True) != workspace):
                        return {'state':'unknown'}
                    if result.get('state') == 'complete':
                        await settle_reconciled_import(p, utility)
                        if not await hydrate_import_visibility(session_manager, p, utility._selected_import_current):
                            return {'state':'visibility_unavailable'}
                        result['chatID'] = p['hostChatID']
                    return result
                result = await execute_selected_import(pool=pool, barrier=barrier, auth=auth, owner=owner,
                    account=account, intent=intent, continuation=observe)
                return JSONResponse({'version':1,'selector':selector,**result},headers={'Cache-Control':'private, no-store'})
            except asyncio.CancelledError:
                raise
            except Exception:
                return JSONResponse({'version':1,'state':'unknown'},status_code=409,headers={'Cache-Control':'private, no-store'})

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
        except Exception as exc:
            from src.openclank.session_operations import HistoryOperationError
            if isinstance(exc, HistoryOperationError): raise HTTPException(409, {"code": exc.code, "message": "Resolve pending history before exporting"}) from exc
            raise

        safe_name = re.sub(r'[^\w\-_]', '_', session.name)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = _sanitize_export_filename(filename)

        if fmt == "json":
            import json as _json
            data = {
                "name": session.name,
                "model": session.model,
                "response_style": session.response_style,
                "exported": datetime.now().isoformat(),
                "messages": [{"role": m.role, "content": m.content} for m in session.history],
            }
            # This optional history is scoped to the authenticated immutable account.
            from src.auth_helpers import require_user, is_internal_tool_identity
            from src.openclank.chat_ratings import export_chat
            user = require_user(request)
            auth = getattr(request.app.state, "auth_manager", None)
            account = auth.account_id(user) if auth and not is_internal_tool_identity(user) else None
            if account and auth.username_for_account_id(account) == user and not getattr(session, "incognito", False):
                db = SessionLocal()
                try:
                    data["chat_ratings"] = export_chat(db, account=account, chat_id=sid)
                    source_row = db.query(DbSession).filter(DbSession.id == sid, DbSession.owner == user).first()
                    source = (source_row.mimo_state or {}).get("selected_history_import") if source_row else None
                    if (type(source) is dict and source.get("version") == 1 and source.get("account") == account
                            and source.get("hostChatID") == sid and source.get("coverage") == "selected_partial"
                            and source.get("sourceTime") == "unknown" and source.get("ordering") == "import_order"
                            and type(source.get("selection")) is list and 1 <= len(source["selection"]) <= 100
                            and all(type(source.get(k)) is str and re.fullmatch(r"[0-9a-f]{64}", source[k])
                                    for k in ("snapshotSHA256", "selectionSHA256"))):
                        data["import_provenance"] = {"version": 1, "source": "codex-thread-history-v6",
                            "coverage": "selected_partial", "sourceTime": "unknown", "ordering": "import_order",
                            "snapshotSHA256": source["snapshotSHA256"], "selectionSHA256": source["selectionSHA256"],
                            "selectedCount": len(source["selection"])}
                finally:
                    db.close()
            else:
                data["chat_ratings"] = {"version": 1, "state": "unavailable", "events": []}
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
                html_parts.append(f'<div class="msg {cls}"><div class="role">{html.escape(str(m.role))}</div>{content}</div>')
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
            from src.openclank.chat_source_bridge import canonical_source_provider
            source_protected = canonical_source_provider(request.app.state) is not None
            for row in rows:
                # Raw tidy deletion cannot prove retirement of archived sources.
                if source_protected:
                    continue
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
                "cleanup_unavailable": source_protected,
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
                "cleanup_unavailable": source_protected,
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
            "cleanup_unavailable": source_protected,
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

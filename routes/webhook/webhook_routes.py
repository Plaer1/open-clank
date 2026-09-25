"""Webhook, API Token, and sync chat routes."""

import os
import uuid
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Form
from pydantic import BaseModel, Field

from core.database import SessionLocal, Webhook
from src.webhook_manager import WebhookManager, validate_webhook_url, validate_events

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["webhooks"])

# Input limits
MAX_NAME_LEN = 100
MAX_URL_LEN = 2048
MAX_SECRET_LEN = 256
MAX_MESSAGE_LEN = 32_000


from core.middleware import require_admin as _require_admin


def _caller_owns_session(sess_owner, caller) -> bool:
    """Strict session-ownership gate for the token-authenticated sync-chat
    endpoint (`POST /api/v1/chat`).

    Mirrors ``_verify_session_owner`` in session_routes.py and the null-owner
    gates in notes/calendar/gallery: a caller may resume a session ONLY when
    its owner matches them exactly. A null/empty session owner (legacy or
    migrated rows) is deliberately NOT resumable by an arbitrary token — the
    old ``sess_owner and sess_owner != caller`` form skipped the check whenever
    ``sess_owner`` was falsy, so any chat-scoped token (e.g. a paired mobile
    device) could resume such a session, inject a message, and read back its
    history and reuse the owner's endpoint credentials. Fail closed: an
    unresolvable caller also returns False.
    """
    if not caller:
        return False
    return sess_owner == caller


def setup_webhook_routes(
    webhook_manager: WebhookManager,
    auth_manager,
    session_manager=None,
    api_key_manager=None,
) -> APIRouter:

    @router.get("/webhooks")
    def list_webhooks(request: Request):
        _require_admin(request)
        db = SessionLocal()
        try:
            hooks = db.query(Webhook).all()
            return [
                {
                    "id": w.id,
                    "name": w.name,
                    "url": w.url,
                    "has_secret": bool(w.secret),
                    "events": w.events.split(",") if w.events else [],
                    "is_active": w.is_active,
                    "last_triggered_at": w.last_triggered_at.isoformat() if w.last_triggered_at else None,
                    "last_status_code": w.last_status_code,
                    "last_error": w.last_error,
                    "created_at": w.created_at.isoformat() if w.created_at else None,
                }
                for w in hooks
            ]
        finally:
            db.close()

    @router.post("/webhooks")
    def create_webhook(
        request: Request,
        name: str = Form(""),
        url: str = Form(""),
        secret: str = Form(""),
        events: str = Form(""),
    ):
        _require_admin(request)
        name = name.strip()[:MAX_NAME_LEN]
        if not name:
            raise HTTPException(400, "Webhook name is required")
        try:
            url = validate_webhook_url(url)
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            events = validate_events(events)
        except ValueError as e:
            raise HTTPException(400, str(e))

        secret_val = secret.strip()[:MAX_SECRET_LEN] or None
        # Encrypt the secret at rest using the same Fernet key as API keys
        encrypted_secret = None
        if secret_val and api_key_manager:
            encrypted_secret = api_key_manager.encrypt_api_key(secret_val)
        elif secret_val:
            encrypted_secret = secret_val  # Fallback if no encryption available

        webhook_id = str(uuid.uuid4())[:8]
        db = SessionLocal()
        try:
            db.add(Webhook(
                id=webhook_id,
                name=name,
                url=url,
                secret=encrypted_secret,
                events=events,
                is_active=True,
            ))
            db.commit()
        finally:
            db.close()

        return {"id": webhook_id, "name": name}

    @router.post("/webhooks/{webhook_id}/test")
    async def test_webhook(request: Request, webhook_id: str):
        _require_admin(request)
        db = SessionLocal()
        try:
            wh = db.query(Webhook).filter(Webhook.id == webhook_id).first()
            if not wh:
                raise HTTPException(404, "Webhook not found")
            url, secret = wh.url, wh.secret
        finally:
            db.close()

        await webhook_manager.deliver_test(webhook_id, url, secret)
        return {"status": "sent"}

    @router.patch("/webhooks/{webhook_id}")
    def toggle_webhook(request: Request, webhook_id: str):
        _require_admin(request)
        db = SessionLocal()
        try:
            wh = db.query(Webhook).filter(Webhook.id == webhook_id).first()
            if not wh:
                raise HTTPException(404, "Webhook not found")
            wh.is_active = not wh.is_active
            db.commit()
            return {"id": webhook_id, "is_active": wh.is_active}
        finally:
            db.close()

    @router.delete("/webhooks/{webhook_id}")
    def delete_webhook(request: Request, webhook_id: str):
        _require_admin(request)
        db = SessionLocal()
        try:
            deleted = db.query(Webhook).filter(Webhook.id == webhook_id).delete()
            db.commit()
            if not deleted:
                raise HTTPException(404, "Webhook not found")
        finally:
            db.close()
        return {"status": "deleted"}

    # ================================================================
    # Sync Chat Endpoint (for n8n / Make / Activepieces)
    # ================================================================

    class SyncChatRequest(BaseModel):
        message: str = Field(..., max_length=MAX_MESSAGE_LEN)
        model: Optional[str] = Field(None, max_length=200)
        session: Optional[str] = Field(None, max_length=100)
        endpoint_id: Optional[str] = Field(None, max_length=200)
        model_route_id: Optional[str] = Field(None, max_length=200)
        api_key: Optional[str] = Field(None, max_length=256)
        base_url: Optional[str] = Field(None, max_length=MAX_URL_LEN)
        provider: Optional[str] = Field(None, max_length=50)

    @router.post("/v1/chat")
    async def sync_chat(request: Request, body: SyncChatRequest):
        if not getattr(request.state, "api_token", False):
            raise HTTPException(403, "This endpoint requires an API token")
        scopes = set(getattr(request.state, "api_token_scopes", []) or [])
        if "chat" not in scopes:
            raise HTTPException(403, "API token is not scoped for chat")
        token_owner = getattr(request.state, "api_token_owner", None)
        if not token_owner:
            raise HTTPException(403, "API token has no provider owner")
        if body.api_key or body.base_url or body.provider:
            raise HTTPException(
                400,
                "Provider credentials and routes must be configured through /api/v1/providers",
            )
        if not session_manager:
            raise HTTPException(503, "Session manager is unavailable")

        from core.models import ChatMessage
        from src.openclank.chat_routing import (
            ChatRouteUnavailable,
            MANAGED_ENGINE_PUBLIC_URL,
            list_chat_routes,
            normalized_provider_owner,
            resolve_chat_route,
        )
        from src.openclank.modality_facade import complete_text
        from src.openclank.provider_store import ProviderStore

        message = body.message.strip()
        if not message:
            raise HTTPException(400, "Message is required")

        session_id = body.session
        sess = None
        selected_route = None

        # --- Case 1: Resume an existing session ---
        if session_id and session_manager:
            try:
                sess = session_manager.get_session(session_id)
            except (KeyError, Exception):
                raise HTTPException(404, "Session not found")
            # SECURITY: verify the API-token's user owns this session — without
            # this any token holder could resume any user's chat by passing its
            # ID. The token's user is on request.state.user (set by API-token
            # middleware); fall back to require_user if not present.
            try:
                from src.auth_helpers import get_current_user as _gcu
                _tok_user = token_owner or getattr(request.state, "user", None) or _gcu(request)
            except Exception:
                _tok_user = None
            # Strict ownership (see _caller_owns_session): fail closed so a
            # null-owner / cross-owner session can't be resumed by an arbitrary
            # chat-scoped token.
            _sess_owner = getattr(sess, "owner", None)
            if not _caller_owns_session(_sess_owner, _tok_user):
                raise HTTPException(404, "Session not found")
            try:
                selected_route = resolve_chat_route(
                    owner=token_owner,
                    endpoint_id=getattr(sess, "endpoint_id", None),
                    model_id=getattr(sess, "model", None),
                    model_route_id=getattr(
                        sess,
                        "provider_model_route_id",
                        None,
                    ),
                )
            except ChatRouteUnavailable as exc:
                raise HTTPException(409, str(exc)) from exc

        if not sess:
            try:
                if body.model_route_id or body.endpoint_id:
                    selected_route = resolve_chat_route(
                        owner=token_owner,
                        endpoint_id=body.endpoint_id,
                        model_id=body.model,
                        model_route_id=body.model_route_id,
                    )
                else:
                    own, shared = list_chat_routes(token_owner)
                    available = [*own, *shared]
                    if body.model:
                        available = [
                            route
                            for route in available
                            if route.provider_model_id == body.model
                        ]
                    by_id = {
                        route.model_route_id: route
                        for route in available
                    }
                    bindings = ProviderStore().list_route_bindings(
                        owner=normalized_provider_owner(token_owner),
                    )
                    selected_route = next(
                        (
                            by_id[binding.model_route_id]
                            for binding in bindings
                            if binding.purpose == "chat"
                            and binding.enabled
                            and binding.model_route_id in by_id
                        ),
                        available[0] if len(available) == 1 else None,
                    )
                    if selected_route is None:
                        raise ChatRouteUnavailable(
                            "Choose one normalized provider connection and model route"
                        )
            except ChatRouteUnavailable as exc:
                raise HTTPException(400, str(exc)) from exc

            sid = str(uuid.uuid4())
            sess = session_manager.create_session(
                session_id=sid,
                name="API Chat",
                endpoint_url=MANAGED_ENGINE_PUBLIC_URL,
                model=selected_route.provider_model_id,
                owner=token_owner,
                endpoint_id=selected_route.public_endpoint_id,
                provider_model_route_id=selected_route.model_route_id,
            )
            sess.headers = {}
            session_manager.save_sessions()
            session_id = sid

        if selected_route is None:  # pragma: no cover - defensive invariant
            raise HTTPException(409, "Session has no normalized provider route")

        sess.add_message(ChatMessage("user", message))

        messages = [{"role": m.role, "content": m.content} for m in sess.history]

        reply = await complete_text(
            owner=token_owner,
            purpose="chat",
            messages=messages,
            model_route_id=selected_route.model_route_id,
            grant_id=selected_route.provider_grant_id,
            root_operation_id=f"root_{uuid.uuid4().hex}",
            idempotency_key=f"sync-chat-{uuid.uuid4().hex}",
        )
        sess.add_message(ChatMessage("assistant", reply))
        session_manager.save_sessions()

        webhook_manager.fire_and_forget("chat.completed", {
            "session_id": session_id, "model": sess.model,
            "user_message": message[:2000], "response": reply[:2000],
        })

        return {"response": reply, "session_id": session_id, "model": sess.model}

    return router

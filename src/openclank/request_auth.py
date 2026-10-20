"""Always-on authentication for HTTP and WebSocket application entry points.

Identity/token authorities and the account admission barrier are supplied by
canonical app assembly. Importing this module starts no app or database.
"""
from __future__ import annotations

import asyncio as _asyncio
import logging
import secrets
from datetime import datetime

import bcrypt as _bcrypt
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, RedirectResponse

from core.auth import normalize_known_username
from core.middleware import (INTERNAL_TOOL_OWNER_HEADER, get_application_route_path, is_cors_preflight, with_asgi_root_path)

logger = logging.getLogger(__name__)


def install_request_auth(app, *, auth_manager, session_factory, api_token_model,
                         account_call_next, trusted_loopback, desktop_ready_path, session_cookie):
    """Install the real app authentication boundary without a bypass option."""
    SESSION_COOKIE = session_cookie
    SessionLocal = session_factory
    ApiToken = api_token_model
    _call_next_for_account_owner = account_call_next
    _is_trusted_loopback = trusted_loopback
    AUTH_EXEMPT_EXACT = {
        "/api/auth/setup",
        "/api/auth/signup",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/status",
        "/api/auth/integrations/presets",
        "/api/health",
        desktop_ready_path,
        "/api/version",
        "/api/tui/v1/info",
        "/api/tui/v1/device/start",
        "/api/tui/v1/device/token",
        "/api/internal/frankenmemory/tool",
        "/api/internal/copal/tool",
        "/login",
    }
    # Bootstrap serves code/fonts/icons and translations, never an HTML app shell
    # or arbitrary static data. Every other static request passes session auth.
    _AUTH_BOOTSTRAP_STATIC_PREFIXES = ("/static/js/", "/static/fonts/", "/static/icons/", "/static/i18n/")
    _AUTH_BOOTSTRAP_STATIC_SUFFIXES = (".js", ".css", ".woff", ".woff2", ".ttf", ".otf", ".png", ".svg", ".ico")
    # Dynamic paths whose own handler proves identity via a path-embedded
    # secret instead of the session/bearer auth. The route handler at
    # routes/task_routes.py validates the per-task `webhook_token` itself
    # and returns 404 on mismatch, so the path is the credential — the
    # UI labels these URLs "no auth needed" precisely because external
    # callers (Zapier, n8n, curl) can't supply a session cookie. Without
    # this exemption AuthMiddleware rejects every POST with 401 before
    # the token is ever checked.
    import re as _re
    AUTH_EXEMPT_PATTERNS = [
        _re.compile(r"^/api/tasks/[^/]+/webhook/[^/]+/?$"),
        # Opaque published-file grants authenticate in their route handler.
        # Owner grants still require the matching session; public grants are
        # deliberately anonymous until expiry or revocation.
        _re.compile(r"^/api/files/download/[^/]+/?$"),
    ]

    def _is_auth_exempt(path: str, method: str = "GET") -> bool:
        # Boot/login UI reads these settings before it has a session. Writes
        # are owner-scoped and must pass normal authentication plus the account
        # lifecycle admission barrier.
        if path in {"/api/auth/settings", "/api/auth/features", "/api/auth/policy"}:
            return str(method or "").upper() in {"GET", "HEAD"}
        if path in AUTH_EXEMPT_EXACT:
            return True
        if str(method).upper() in {"GET", "HEAD"} and "\\" not in path and not any(part in {".", ".."} for part in path.split("/")):
            if path == "/static/sw.js":
                return True
            if _re.fullmatch(r"/static/manifest(?:\.[A-Za-z0-9_-]+)?\.json", path):
                return True
            if _re.fullmatch(r"/static/i18n/[A-Za-z0-9_-]+\.json", path):
                return True
            if path.startswith(_AUTH_BOOTSTRAP_STATIC_PREFIXES) and path.endswith(_AUTH_BOOTSTRAP_STATIC_SUFFIXES):
                return True
        return any(p.match(path) for p in AUTH_EXEMPT_PATTERNS)

    # In-memory token cache: prefix → token metadata.  TUI tokens share the
    # same authority but are path-confined by client_kind.
    # query was running on every API-bearer request and scanning bcrypt
    # checks linearly. With this cache, we hit the DB only when the cache
    # version bumps (token created/revoked) — see _token_cache_invalidate
    # in app.state, called by routes/api_token_routes.
    _token_cache: dict = {}
    _token_cache_lock = _asyncio.Lock()
    _token_cache_dirty = True

    def _token_cache_invalidate():
        app.state._token_cache_dirty = True
    app.state.invalidate_token_cache = _token_cache_invalidate
    app.state._token_cache = _token_cache
    app.state._token_cache_dirty = True

    def _refresh_token_cache():
        """Rebuild the prefix→[(id,hash)] map from the DB.

        Readers hold the previous dict reference until the swap completes;
        a clear()-then-update() left a window where the map was empty and
        concurrent auth checks returned 401.
        """
        nonlocal _token_cache
        from collections import defaultdict
        new_map = defaultdict(list)
        db = SessionLocal()
        try:
            rows = db.query(ApiToken).filter(ApiToken.is_active == True).all()
            for r in rows:
                owner_key = normalize_known_username(auth_manager.users, getattr(r, "owner", None))
                if not owner_key:
                    logger.warning(
                        "Ignoring active API token '%s' for unknown auth user '%s'",
                        getattr(r, "id", ""),
                        getattr(r, "owner", None),
                    )
                    continue
                if getattr(r, "revoked_at", None) is not None:
                    continue
                scopes = [s.strip() for s in (getattr(r, "scopes", "") or "chat").split(",") if s.strip()]
                new_map[r.token_prefix].append(
                    (
                        r.id,
                        r.token_hash,
                        owner_key,
                        scopes,
                        getattr(r, "client_kind", None) or "api",
                        getattr(r, "expires_at", None),
                    )
                )
        finally:
            db.close()
        _token_cache = dict(new_map)
        app.state._token_cache = _token_cache
        app.state._token_cache_dirty = False

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = get_application_route_path(request.scope)
            # A genuine CORS preflight (OPTIONS + Access-Control-Request-Method)
            # carries no credentials by design and must reach CORSMiddleware to be
            # answered. AuthMiddleware is the outermost middleware, so gating the
            # preflight on auth 401s it before CORS can respond -- which blocks
            # every cross-origin browser/WebView client before the real request
            # is sent. Let real preflights through (only OPTIONS w/ the ACRM
            # header; never a credentialed request).
            if is_cors_preflight(request.method, request.headers):
                return await call_next(request)
            if _is_auth_exempt(path, request.method):
                # Public grants remain credential-scoped; owner-only grants
                # still need an optional *verified* browser session. Bootstrap
                # and public resources never manufacture an identity.
                cookie = request.cookies.get(SESSION_COOKIE)
                owner = None
                if cookie:
                    try:
                        valid, _refresh = auth_manager.validate_session(cookie)
                        if valid:
                            owner = normalize_known_username(auth_manager.users, auth_manager.get_username_for_token(cookie))
                    except Exception:
                        owner = None
                if owner:
                    request.state.current_user = owner
                    request.state.authenticated = True
                    request.state.api_token = False
                    return await _call_next_for_account_owner(request, call_next, owner)
                return await call_next(request)
            # In-process internal-tool token bypass. Used by the agent
            # tool layer when it HTTP-loopbacks to admin-gated routes
            # (no admin cookie available in that context). Restricted to
            # loopback clients + matching token to keep it locked down.
            try:
                from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN as _ITT, INTERNAL_TOOL_USER
                _hdr = request.headers.get(INTERNAL_TOOL_HEADER)
                if _hdr and secrets.compare_digest(_hdr, _ITT) and _is_trusted_loopback(request):
                    # Impersonation: when the agent's loopback call sets
                    # X-Open-Clank-Owner, attribute the request to that user only
                    # if they exist. Authorization checks remain separate; this
                    # is just owner attribution for notes/calendar/etc.
                    _auth_mgr = getattr(request.app.state, "auth_manager", None) or auth_manager
                    _impersonate = normalize_known_username(
                        getattr(_auth_mgr, "users", {}),
                        request.headers.get(INTERNAL_TOOL_OWNER_HEADER),
                    )
                    if _impersonate:
                        if _auth_mgr.is_account_lifecycle_fenced(_impersonate):
                            return JSONResponse(
                                status_code=409,
                                content={"error": "Account lifecycle operation in progress"},
                            )
                        request.state.current_user = _impersonate
                    else:
                        request.state.current_user = INTERNAL_TOOL_USER
                    request.state.authenticated = True
                    request.state.internal_tool_authenticated = True
                    request.state.api_token = False
                    return await _call_next_for_account_owner(
                        request,
                        call_next,
                        _impersonate,
                    ) if _impersonate else await call_next(request)
            except Exception as _e:
                logger.warning("Internal tool auth header check failed", exc_info=_e)
            if not auth_manager.is_configured:
                # No users yet — redirect to login for first-time setup
                if not path.startswith("/api/"):
                    return RedirectResponse(
                        url=with_asgi_root_path(request.scope, "/login"),
                        status_code=302,
                    )
                return JSONResponse(status_code=401, content={"error": "Setup required"})

            # --- Bearer token auth (API tokens for external integrations) ---
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith(("Bearer ody_", "Bearer oct_")):
                raw_token = auth_header[7:]
                requested_kind = "tui" if raw_token.startswith("oct_") else "api"
                # Sanity check: tokens are a four-character prefix plus a
                # high-entropy URL-safe body.
                if len(raw_token) < 12 or len(raw_token) > 100:
                    return JSONResponse(status_code=401, content={"error": "Invalid API token"})
                prefix = raw_token[:8]
                try:
                    if app.state._token_cache_dirty:
                        async with _token_cache_lock:
                            if app.state._token_cache_dirty:
                                await _asyncio.to_thread(_refresh_token_cache)
                    candidates = list(_token_cache.get(prefix, ()))
                    matched_id = None
                    matched_owner = None
                    matched_scopes = []
                    matched_kind = None
                    for tid, thash, owner, scopes, client_kind, expires_at in candidates:
                        if client_kind != requested_kind:
                            continue
                        if expires_at is not None and expires_at <= datetime.utcnow():
                            continue
                        if _bcrypt.checkpw(raw_token.encode(), thash.encode()):
                            matched_id = tid
                            matched_owner = owner
                            matched_scopes = scopes or []
                            matched_kind = client_kind
                            break
                    if matched_id:
                        if auth_manager.is_account_lifecycle_fenced(matched_owner):
                            return JSONResponse(
                                status_code=409,
                                content={"error": "Account lifecycle operation in progress"},
                            )
                        if matched_kind == "tui" and not path.startswith("/api/tui/v1/"):
                            return JSONResponse(
                                status_code=403,
                                content={"error": "TUI token is confined to the TUI API"},
                            )
                        # Update last_used_at off the hot path. Doing it
                        # inline used to keep the request open across an
                        # extra commit; do it fire-and-forget instead.
                        async def _touch_last_used(tid: str):
                            def _do():
                                _db = SessionLocal()
                                try:
                                    _db.query(ApiToken).filter(ApiToken.id == tid).update(
                                        {"last_used_at": datetime.utcnow()}
                                    )
                                    _db.commit()
                                finally:
                                    _db.close()
                            try:
                                await _asyncio.to_thread(_do)
                            except Exception as _e:
                                logger.debug("Failed to update token last_used_at", exc_info=_e)
                        _asyncio.create_task(_touch_last_used(matched_id))
                        # Keep bearer-token callers out of normal cookie/user
                        request.state.current_user = "api"
                        request.state.authenticated = True
                        request.state.api_token = True
                        request.state.api_token_id = matched_id
                        request.state.api_token_owner = matched_owner
                        request.state.api_token_scopes = matched_scopes
                        request.state.api_token_client_kind = matched_kind
                        return await _call_next_for_account_owner(
                            request,
                            call_next,
                            matched_owner,
                        )
                except Exception:
                    logger.warning("API token auth error", exc_info=False)
                # Invalid bearer token — reject immediately
                return JSONResponse(status_code=401, content={"error": "Invalid API token"})

            # --- Cookie-based session auth ---
            token = request.cookies.get(SESSION_COOKIE)
            valid_session, refresh_cookie = auth_manager.validate_session(token)
            if not valid_session:
                if path.startswith("/api/"):
                    return JSONResponse(status_code=401, content={"error": "Not authenticated"})
                return RedirectResponse(
                    url=with_asgi_root_path(request.scope, "/login"),
                    status_code=302,
                )

            # Attach current username to request state for downstream routes
            request.state.current_user = normalize_known_username(auth_manager.users, auth_manager.get_username_for_token(token))
            if not request.state.current_user:
                return JSONResponse(status_code=401, content={"error": "Not authenticated"})
            request.state.authenticated = True
            request.state.api_token = False
            response = await _call_next_for_account_owner(
                request,
                call_next,
                request.state.current_user,
            )
            if refresh_cookie:
                # The session just slid (activity renewal) and came from a
                # "remember me" login — renew the persistent cookie's max_age
                # too, or an active browser outlives its cookie while the
                # server-side session is still valid.
                from routes.auth_routes import _session_cookie_is_secure

                cookie_lifetime = auth_manager.session_cookie_lifetime(token)

                response.set_cookie(
                    key=SESSION_COOKIE,
                    value=token,
                    httponly=True,
                    samesite="lax",
                    secure=_session_cookie_is_secure(request),
                    path="/",
                    **cookie_lifetime,
                )
            return response

    app.add_middleware(AuthMiddleware)
    logger.info("Authentication is required")

    class SocketAuthMiddleware:
        def __init__(self, app):
            self.downstream = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "websocket":
                return await self.downstream(scope, receive, send)
            # No app WebSocket route is currently registered. Any future socket
            # nevertheless receives the same verified account boundary; public
            # HTTP bootstrap and resource grants never admit a socket.
            try:
                connection = HTTPConnection(scope)
                token = connection.cookies.get(SESSION_COOKIE)
                valid, _refresh = auth_manager.validate_session(token)
                owner = normalize_known_username(auth_manager.users, auth_manager.get_username_for_token(token)) if valid else None
                if not owner or auth_manager.is_account_lifecycle_fenced(owner):
                    await send({"type": "websocket.close", "code": 1008})
                    return
            except Exception:
                await send({"type": "websocket.close", "code": 1008})
                return
            state = scope.setdefault("state", {})
            state.update(current_user=owner, authenticated=True, api_token=False)
            await self.downstream(scope, receive, send)

    # Starlette supplies the app keyword to middleware constructors.
    app.add_middleware(SocketAuthMiddleware)
    return AuthMiddleware

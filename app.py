# app.py — slim orchestrator
import mimetypes
import os
import sys
import asyncio
import json
import time

# On Windows, asyncio.create_subprocess_exec/shell require the ProactorEventLoop.
# When started via `python -m uvicorn` from a terminal, uvicorn sets this
# automatically. But the VS Code debugger (and other non-uvicorn entrypoints)
# use the default SelectorEventLoop, which raises NotImplementedError on any
# subprocess call. Force ProactorEventLoop here so the right loop is always
# used, regardless of how the process is launched.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def register_static_mime_types() -> None:
    """Force stable JS module MIME types across platforms.

    Some native Windows setups inherit stale/incorrect registry mappings for
    ``.js``/``.mjs``, which can make Starlette serve ES modules with a non-JS
    ``Content-Type`` and cause the UI to load but fail on click. Re-register the
    standard MIME types at startup so static assets are served consistently.
    """

    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("application/javascript", ".mjs")


register_static_mime_types()

# Windows: force HuggingFace/fastembed to COPY model files instead of symlinking.
# On a network-share/UNC data dir Windows can't follow HF's symlinks ([WinError
# 1463]), so the ONNX embedding model fails to load. huggingface_hub reads this
# at import time, so set it before anything pulls it in. (Mirrored in
# src/embeddings.py for non-server entrypoints.)
if os.name == "nt":
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from dotenv import load_dotenv
# encoding="utf-8-sig" tolerates a UTF-8 BOM in .env — a common Windows gotcha
# when the file is saved from Notepad. Without this, the first key parses as
# "﻿AUTH_ENABLED" instead of "AUTH_ENABLED", so AUTH_ENABLED=false (etc.)
# is silently ignored and the user is unexpectedly forced to log in (issue #142).
# utf-8-sig reads plain UTF-8 (no BOM) identically, so this is safe everywhere.
load_dotenv(encoding="utf-8-sig")

import asyncio
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

# Core imports
from core.constants import (
    BASE_DIR, STATIC_DIR, SESSIONS_FILE,
    REQUEST_TIMEOUT, OPENAI_API_KEY, AUTH_FILE,
)
from core.database import SessionLocal, ApiToken
from core.middleware import (
    INTERNAL_TOOL_HEADER,
    INTERNAL_TOOL_OWNER_HEADER,
    INTERNAL_TOOL_TOKEN,
    INTERNAL_TOOL_USER,
    INTERNAL_TOOL_WORKSPACE_HEADER,
    SecurityHeadersMiddleware,
    get_application_route_path,
    is_cors_preflight,
    path_is_route_or_child,
    with_asgi_root_path,
)
from core.auth import AuthManager, normalize_known_username
from core.exceptions import (
    SessionNotFoundError, InvalidFileUploadError,
    LLMServiceError, WebSearchError,
)

import bcrypt as _bcrypt

from src.app_helpers import abs_join, serve_html_with_nonce
from src.generated_images import (
    GENERATED_IMAGE_HEADERS,
    gallery_owner_key,
    has_generated_image_provenance,
    resolve_generated_image_path,
)
from src.shutdown_lifecycle import run_shutdown_phase as _run_shutdown_phase
from src.openclank.account_request_barrier import (
    AccountRequestBarrier,
    AccountRequestFenced,
)
from starlette.responses import RedirectResponse

# ========= LOGGING =========
import logging.handlers
from core.constants import DATA_DIR

_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Clear existing handlers to avoid duplicates
for _h in list(_root_logger.handlers):
    _root_logger.removeHandler(_h)

_console_h = logging.StreamHandler()
_console_h.setFormatter(_formatter)
_root_logger.addHandler(_console_h)

try:
    _log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _log_file = os.path.join(_log_dir, "app.log")

    # RotatingFileHandler is not multi-process safe (e.g. if uvicorn is run with --workers N).
    # Open Clank is single-process by convention, so this is acceptable, but be aware that
    # concurrent log rotation issues can arise if multiple workers are configured.
    _file_h = logging.handlers.RotatingFileHandler(
        _log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _file_h.setFormatter(_formatter)
    _root_logger.addHandler(_file_h)
except Exception as e:
    _root_logger.warning(f"Failed to initialize file logging handler (falling back to console-only): {e}")

logger = logging.getLogger(__name__)

# ========= APP =========
# Lifespan is defined below (after all helpers it references are in scope)
# and passed to FastAPI so we can use the modern context-manager lifecycle
# instead of the deprecated @app.on_event("startup"/"shutdown") decorators.
app = FastAPI(
    title="AI Chat Application",
    description="Comprehensive AI chat with memory, research, and multi-modal capabilities",
    version="1.0.0",
)

# ========= CORS =========
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
allowed_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost,http://127.0.0.1").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=CORS_ALLOW_METHODS,
    allow_headers=[
        "Accept",
        "Authorization",
        "Content-Type",
        "X-API-Key",
        "X-Auth-Token",
        INTERNAL_TOOL_HEADER,
        INTERNAL_TOOL_OWNER_HEADER,
        INTERNAL_TOOL_WORKSPACE_HEADER,
        "X-Requested-With",
        "X-TZ-Offset",
    ],
)

# ========= RESPONSE COMPRESSION (gzip) =========
# The frontend's text assets (style.css, index.html, the JS bundles) shipped
# uncompressed on every cold load. gzip cuts CSS/JS/HTML by ~75-85% on the wire
# with no behavioural change. Starlette's GZipMiddleware excludes
# `text/event-stream` by default, so the SSE streams (chat, shell, and research,
# all served with media_type="text/event-stream") are never
# compressed or buffered; only complete bodies over minimum_size are. The
# security-header middleware composes cleanly on top.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

# ========= SECURITY HEADERS MIDDLEWARE =========
app.add_middleware(SecurityHeadersMiddleware)


# ========= REQUEST TIMEOUT (FALLBACK FOR HUNG HANDLERS) =========
# If a single request takes longer than REQUEST_HARD_TIMEOUT, abort it and
# return 504 instead of holding the event loop hostage. Whitelisted paths
# (streaming, long-running shell exec, research) are exempt because they
# legitimately stay open. Without this, a single hung subprocess.run or
# missing-timeout httpx call locks up the entire server for everyone.
import asyncio as _asyncio
from starlette.middleware.base import BaseHTTPMiddleware as _BaseHTTPMiddleware
from starlette.responses import JSONResponse as _JSONResponse

REQUEST_HARD_TIMEOUT = float(os.getenv("REQUEST_HARD_TIMEOUT", "45"))
_TIMEOUT_EXEMPT_PREFIXES = (
    "/api/chat",            # streaming
    "/api/shell/stream",    # SSE
    "/api/research",        # multi-minute jobs
    "/api/model/download",  # tmux setup may run pip installs
    "/api/cookbook/setup",  # remote pacman/apt installs
    "/api/upload",          # large files
    "/api/image",           # diffusion proxies (inpaint/harmonize/upscale/etc.) — own 120s httpx timeout
    "/api/memory/audit",    # retains own 120s LLM inactivity timeout
    "/api/memory/import-batches",  # durable per-file work has no outer elapsed-time deadline
)


def _is_timeout_exempt(path: str, method: str) -> bool:
    if any(path.startswith(prefix) for prefix in _TIMEOUT_EXEMPT_PREFIXES):
        return True
    normalized_method = str(method or "GET").upper()
    # Durable account convergence deliberately uses unbounded request/writer
    # drains and may cross several local stores.  Cancelling it at 45 seconds
    # can strand a live-PID claim that the recovery route then cannot acquire.
    if normalized_method == "DELETE" and path == "/api/auth/users":
        return True
    if (
        normalized_method == "PUT"
        and path.startswith("/api/auth/users/")
        and path.endswith("/rename")
    ):
        return True
    if (
        normalized_method == "POST"
        and path.startswith("/api/auth/account-operations/")
        and path.endswith("/resume")
    ):
        return True
    return False


class _RequestTimeoutMiddleware(_BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path or ""
        if _is_timeout_exempt(path, request.method):
            return await call_next(request)
        try:
            return await _asyncio.wait_for(call_next(request), timeout=REQUEST_HARD_TIMEOUT)
        except _asyncio.TimeoutError:
            return _JSONResponse(
                {"detail": f"Request exceeded {REQUEST_HARD_TIMEOUT:.0f}s timeout"},
                status_code=504,
            )


class _InteractiveActivityMiddleware(_BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        from src.interactive_gate import should_track_interactive_request, track_interactive_request

        path = request.url.path or ""
        if not should_track_interactive_request(path, request.method):
            return await call_next(request)
        async def _stop_background():
            try:
                await task_scheduler.stop_background_tasks_for_foreground(reason=f"foreground request {request.method} {path}")
            except Exception:
                logging.getLogger("app.foreground_gate").debug("foreground task stop failed", exc_info=True)
        asyncio.create_task(_stop_background())
        async with track_interactive_request(path, request.method):
            return await call_next(request)


class _SlowRequestLogMiddleware(_BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = getattr(response, "status_code", 0) or 0
            return response
        finally:
            elapsed = time.perf_counter() - start
            try:
                threshold = float(os.getenv("ODYSSEUS_SLOW_REQUEST_LOG_SECONDS", "0.75") or "0.75")
            except Exception:
                threshold = 0.75
            if elapsed >= threshold:
                logging.getLogger("app.slow_request").warning(
                    "slow_request method=%s path=%s status=%s elapsed=%.3fs",
                    request.method,
                    request.url.path,
                    status,
                    elapsed,
                )


app.add_middleware(_RequestTimeoutMiddleware)
app.add_middleware(_InteractiveActivityMiddleware)
app.add_middleware(_SlowRequestLogMiddleware)

# ========= AUTH =========
from routes.auth_routes import setup_auth_routes, SESSION_COOKIE
from src.desktop_shell_readiness import (
    DESKTOP_SHELL_CHALLENGE_HEADER,
    DESKTOP_SHELL_READY_PATH,
    DesktopShellBinding,
)

auth_manager = AuthManager()
app.state.auth_manager = auth_manager
_account_request_barrier = AccountRequestBarrier(
    auth_manager.is_account_lifecycle_fenced
)
app.state.drain_account_owner_requests = _account_request_barrier.drain


async def _call_next_for_account_owner(request, call_next, owner):
    """Admit one owner request and retain it through any streaming body."""
    try:
        return await _account_request_barrier.track_response(
            owner,
            lambda: call_next(request),
        )
    except AccountRequestFenced:
        return JSONResponse(
            status_code=409,
            content={"error": "Account lifecycle operation in progress"},
        )


from routes.prefs_routes import backfill_memory_modes
backfill_memory_modes(auth_manager.users)
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() != "false"
_DESKTOP_SHELL_BINDING = DesktopShellBinding.capture()
LOCALHOST_BYPASS = os.getenv("LOCALHOST_BYPASS", "false").lower() == "true"
if LOCALHOST_BYPASS:
    logger.warning("LOCALHOST_BYPASS is enabled, loopback requests bypass authentication. Do not expose this instance to a network.")

# Headers that prove a request was forwarded by a proxy/tunnel (cloudflared,
# nginx, Caddy, Tailscale Funnel, …). cloudflared connects to the app FROM
# 127.0.0.1, so without this check every tunneled request would look local.
_PROXY_FWD_HEADERS = (
    "cf-connecting-ip", "cf-ray", "cf-visitor",
    "x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded",
)


def _is_trusted_loopback(request: Request) -> bool:
    """Accept only a direct loopback connection, never a proxied one."""
    host = request.client.host if request.client else None
    return host in ("127.0.0.1", "::1") and not any(
        request.headers.get(header) for header in _PROXY_FWD_HEADERS
    )


if AUTH_ENABLED:
    AUTH_EXEMPT_EXACT = {
        "/api/auth/setup",
        "/api/auth/signup",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/status",
        "/api/auth/features",
        "/api/auth/integrations/presets",
        "/api/health",
        DESKTOP_SHELL_READY_PATH,
        "/api/version",
        "/api/tui/v1/info",
        "/api/tui/v1/device/start",
        "/api/tui/v1/device/token",
        "/api/internal/frankenmemory/tool",
        "/api/internal/copal/tool",
        "/login",
    }
    AUTH_EXEMPT_PREFIXES = ["/static"]
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
        if path == "/api/auth/settings":
            return str(method or "").upper() in {"GET", "HEAD"}
        if path in AUTH_EXEMPT_EXACT:
            return True
        if any(path_is_route_or_child(path, p) for p in AUTH_EXEMPT_PREFIXES):
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
        nonlocal_dict = app.state.__dict__
        nonlocal_dict["_token_cache_dirty"] = True
    app.state.invalidate_token_cache = _token_cache_invalidate
    app.state._token_cache = _token_cache
    app.state._token_cache_dirty = True

    def _refresh_token_cache():
        """Rebuild the prefix→[(id,hash)] map from the DB.

        Readers hold the previous dict reference until the swap completes;
        a clear()-then-update() left a window where the map was empty and
        concurrent auth checks returned 401.
        """
        global _token_cache
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
                    request.state.api_token = False
                    return await _call_next_for_account_owner(
                        request,
                        call_next,
                        _impersonate,
                    ) if _impersonate else await call_next(request)
            except Exception as _e:
                logger.warning("Internal tool auth header check failed", exc_info=_e)
            # Allow DIRECT localhost requests (internal service calls from
            # heartbeats etc.). Tunnel/proxy-forwarded requests are excluded by
            # _is_trusted_loopback so LOCALHOST_BYPASS can't be abused over a
            # Cloudflare tunnel / reverse proxy. Keep LOCALHOST_BYPASS=false for
            # network-exposed deployments regardless.
            if LOCALHOST_BYPASS and _is_trusted_loopback(request):
                return await call_next(request)
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
            request.state.current_user = auth_manager.get_username_for_token(token)
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
    logger.info("Auth middleware enabled (AUTH_ENABLED=true)")
else:
    logger.info("Auth middleware disabled (set AUTH_ENABLED=true to enable)")


# The provider hard cut deliberately leaves no compatibility handlers. Keep
# this guard outside authentication so retired route names are uniformly 404.
from src.openclank.retired_provider_routes import RetiredProviderRouteMiddleware

app.add_middleware(RetiredProviderRouteMiddleware)

# ========= STATIC FILES =========
os.makedirs(STATIC_DIR, exist_ok=True)


class _RevalidatingStatic(StaticFiles):
    """Serve static assets normally, but force the browser to REVALIDATE
    source files (.js/.css/.html) on every load instead of serving a stale
    copy from disk cache. The app ships raw ES modules with no build step or
    versioned URLs, so browsers were caching modules across deploys — a code
    change wouldn't appear without a manual hard-refresh. `no-cache` keeps the
    cached bytes but requires a conditional request; unchanged files still
    return a cheap 304 (ETag/Last-Modified are preserved)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path.endswith((".js", ".css", ".html")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _RevalidatingStatic(directory=STATIC_DIR), name="static")

# ========= GENERATED IMAGES =========
@app.get("/api/generated-image/{filename}")
async def serve_generated_image(filename: str, request: Request):
    """Serve only an active image with exact durable owner provenance."""
    from src.auth_helpers import effective_user
    from core.database import SessionLocal as _SL, GalleryImage as _GI

    owner = gallery_owner_key(effective_user(request))
    if owner is None:
        raise HTTPException(status_code=404, detail="Image not found")

    # Prove owner provenance before resolving or touching the byte path.  A
    # missing/foreign row and any provenance-store failure deliberately share
    # the same response, and none of them are allowed to fall through to disk.
    if not has_generated_image_provenance(
        _SL,
        _GI,
        filename=filename,
        owner=owner,
    ):
        raise HTTPException(status_code=404, detail="Image not found")

    img_path = resolve_generated_image_path(filename)
    ext = filename.rsplit('.', 1)[-1].lower()
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "gif": "image/gif",
        "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
        "mkv": "video/x-matroska", "m4v": "video/mp4",
    }.get(ext, "application/octet-stream")
    # Personalized bytes are never placed in a shared/browser-persistent cache.
    return FileResponse(
        str(img_path),
        media_type=mime,
        headers=GENERATED_IMAGE_HEADERS,
    )

# ========= YOUTUBE INIT =========
from services.youtube import init_youtube
init_youtube()

# ========= RAG (vector document RAG) =========
# Canonical Frankenmemory SQLite personal-document RAG. Initialized lazily via
# get_rag_manager(); it does not require ChromaDB or another vector service.
from src.rag_singleton import get_rag_manager
rag_manager = get_rag_manager()
rag_available = rag_manager is not None
if rag_available:
    logger.info("Vector document RAG initialized")
else:
    logger.info("Frankenmemory document RAG not available at startup; routes will retry lazily")

# ========= IMPORT CONFIG =========
from src.config import config

# ========= COMPONENT INITIALIZATION =========
from src.app_initializer import initialize_managers

components = initialize_managers(BASE_DIR, rag_manager)

session_manager   = components["session_manager"]
from src.assistant_log import set_session_manager as _set_asst_sm
_set_asst_sm(session_manager)
# Set the global session manager singleton (used by core.models.Session.add_message)
from core.models import set_session_manager_instance
set_session_manager_instance(session_manager)
app.state.session_manager = session_manager
memory_manager    = components["memory_manager"]
memory_vector     = components.get("memory_vector")
memory_provider_registry = components.get("memory_provider_registry")
# Provider-always: app_initializer guarantees a provider object (never None).
memory_provider   = components["memory_provider"]
app.state.memory_provider = memory_provider
upload_handler    = components["upload_handler"]
app.state.upload_handler = upload_handler
personal_docs_mgr = components["personal_docs_manager"]
app.state.personal_docs_manager = personal_docs_mgr
api_key_manager   = components["api_key_manager"]
preset_manager    = components["preset_manager"]
from src.openclank.account_lifecycle import build_account_owner_lifecycle
account_owner_lifecycle = build_account_owner_lifecycle(
    preset_manager,
    session_factory=SessionLocal,
    artifact_root=Path(DATA_DIR) / "model-artifacts",
)
app.state.account_owner_lifecycle = account_owner_lifecycle
auth_manager.configure_account_lifecycle_fence(
    account_owner_lifecycle.owner_has_active_operation
)
chat_processor    = components["chat_processor"]
research_handler  = components["research_handler"]
app.state.research_handler = research_handler
chat_handler      = components["chat_handler"]
model_discovery   = components["model_discovery"]
skills_manager    = components["skills_manager"]

# One app-owned fm-mcp child serves Python, lifetools, and MiMo through this
# authenticated loopback boundary. The broker credential stays in this process
# and scoped lifetools descriptors; it is never exported host-wide.
_memory_broker_base = os.getenv(
    "OPEN_CLANK_INTERNAL_BASE_URL",
    f"http://127.0.0.1:{int(os.getenv('APP_PORT', '7777'))}",
).rstrip("/")
_memory_broker_url = f"{_memory_broker_base}/api/internal/frankenmemory/tool"
from src.openclank.acp_bridge import (
    configure_copal_broker,
    configure_frankenmemory_broker,
    frankenmemory_broker_token,
)
configure_frankenmemory_broker(_memory_broker_url, INTERNAL_TOOL_TOKEN)
app.state.memory_broker_url = _memory_broker_url
_copal_broker_url = f"{_memory_broker_base}/api/internal/copal/tool"
configure_copal_broker(_copal_broker_url, INTERNAL_TOOL_TOKEN)
app.state.copal_broker_url = _copal_broker_url

# TTS
from services.tts import get_tts_service

tts_service = get_tts_service()
logger.info("TTS service initialized (provider managed via admin settings)")

# ========= EXCEPTION HANDLERS =========
@app.exception_handler(SessionNotFoundError)
async def session_not_found_handler(request: Request, exc: SessionNotFoundError):
    return JSONResponse(status_code=404, content={"error": "SESSION_NOT_FOUND", "message": str(exc)})

@app.exception_handler(InvalidFileUploadError)
async def invalid_file_upload_handler(request: Request, exc: InvalidFileUploadError):
    return JSONResponse(status_code=400, content={"error": "INVALID_FILE_UPLOAD", "message": str(exc)})

@app.exception_handler(LLMServiceError)
async def llm_service_error_handler(request: Request, exc: LLMServiceError):
    return JSONResponse(status_code=502, content={"error": "LLM_SERVICE_ERROR", "message": str(exc)})

@app.exception_handler(WebSearchError)
async def web_search_error_handler(request: Request, exc: WebSearchError):
    return JSONResponse(status_code=502, content={"error": "WEB_SEARCH_ERROR", "message": str(exc)})

# ========= WEBHOOK MANAGER =========
from src.webhook_manager import WebhookManager

webhook_manager = WebhookManager(api_key_manager=api_key_manager)

# ========= INCLUDE ROUTERS =========

# Auth
auth_router = setup_auth_routes(
    auth_manager,
    account_lifecycle=account_owner_lifecycle,
)
app.include_router(auth_router)

# Register the literal history settings path before the legacy session
# history parameter route (``/api/history/{sid}``). Starlette evaluates
# routes in registration order, so leaving this until the later History block
# makes ``GET /api/history/settings`` look like a session lookup.
from routes.history.history_routes import setup_history_settings_routes
app.include_router(setup_history_settings_routes())


@app.post("/api/activity/heartbeat")
async def activity_heartbeat():
    from src.interactive_gate import mark_browser_activity
    await mark_browser_activity()
    async def _stop_background():
        try:
            await task_scheduler.stop_background_tasks_for_foreground(reason="browser heartbeat")
        except Exception:
            logging.getLogger("app.foreground_gate").debug("heartbeat task stop failed", exc_info=True)
    asyncio.create_task(_stop_background())
    return {"ok": True}


# Uploads
from routes.upload_routes import setup_upload_routes
upload_router, upload_cleanup_func = setup_upload_routes(upload_handler)
app.include_router(upload_router)
upload_cleanup_task = None

# Agent-published downloads share one owner-scoped, revocable lifecycle.
from routes.published_file_routes import setup_published_file_routes
app.include_router(setup_published_file_routes())

# Emoji SVG proxy (same-origin, lazy-cached Twemoji) — lets the chat render
# emojis as flat SVG instead of system color glyphs.
from routes.emoji_routes import setup_emoji_routes
app.include_router(setup_emoji_routes())

# Sessions
from routes.session_routes import setup_session_routes
session_config = {"REQUEST_TIMEOUT": REQUEST_TIMEOUT, "OPENAI_API_KEY": OPENAI_API_KEY, "SESSIONS_FILE": SESSIONS_FILE}
app.include_router(setup_session_routes(
    session_manager,
    session_config,
    webhook_manager=webhook_manager,
    upload_handler=upload_handler,
))

# Admin Danger Zone wipes (Settings → System → Danger Zone)
from routes.admin_wipe.admin_wipe_routes import setup_admin_wipe_routes
app.include_router(setup_admin_wipe_routes(session_manager, memory_provider=memory_provider))

# Memory
from routes.memory.memory_routes import setup_memory_routes
from services.memory.forget_coordinator import require_memory_lifecycle_convergence
from src.memory_provider import MemoryRequestRejectedError
memory_router = setup_memory_routes(
    memory_manager,
    session_manager,
    memory_provider=memory_provider,
    skills_manager=skills_manager,
)
memory_lifecycle = getattr(memory_router, "memory_lifecycle", None)
memory_skill_forget = getattr(memory_router, "memory_skill_forget", None)
app.include_router(memory_router)


@app.post("/api/internal/frankenmemory/tool", include_in_schema=False)
async def internal_frankenmemory_tool(request: Request, body: Dict[str, object]):
    """Private loopback broker for the single app-supervised fm-mcp process."""
    token = request.headers.get(INTERNAL_TOOL_HEADER, "")
    owner = (request.headers.get(INTERNAL_TOOL_OWNER_HEADER) or "").strip()
    workspace_id = (
        request.headers.get(INTERNAL_TOOL_WORKSPACE_HEADER) or ""
    ).strip()
    try:
        expected_token = frankenmemory_broker_token(
            INTERNAL_TOOL_TOKEN, owner, workspace_id
        )
    except ValueError:
        expected_token = ""
    if (
        not token
        or not expected_token
        or not secrets.compare_digest(token, expected_token)
        or not _is_trusted_loopback(request)
        or (
            AUTH_ENABLED
            and owner not in getattr(auth_manager, "users", {})
        )
    ):
        raise HTTPException(status_code=403, detail="Internal memory broker only")
    name = str(body.get("name") or "")
    arguments = body.get("arguments")
    from src.frankenmemory_provider import BROKER_MEMORY_TOOLS

    if name not in BROKER_MEMORY_TOOLS or not isinstance(arguments, dict):
        raise HTTPException(status_code=400, detail="Invalid memory tool request")
    if name == "memory_quality":
        # Scoped broker credentials only need the read-only startup handshake.
        # Never let them project global rebuild/maintenance controls into the
        # app-owned Frankenmemory process.
        requested_rebuild = arguments.get("rebuild_graph_fts")
        if requested_rebuild is not None and requested_rebuild is not False:
            raise HTTPException(
                status_code=400,
                detail="Memory quality rebuild is unavailable through the broker",
            )
        scoped_arguments = {"rebuild_graph_fts": False}
    else:
        scoped_arguments = dict(arguments)
        scoped_arguments["owner"] = owner
        scoped_arguments["workspace_id"] = workspace_id
    invoke = getattr(memory_provider, "invoke_tool", None)
    if not callable(invoke):
        raise HTTPException(status_code=503, detail="Frankenmemory broker unavailable")
    try:
        async with _account_request_barrier.admitted(owner):
            result = await invoke(name, scoped_arguments)
            if name == "memory_quality":
                return {
                    key: result[key]
                    for key in ("schema_version", "database_id")
                    if key in result
                }
            return result
    except AccountRequestFenced as exc:
        raise HTTPException(
            status_code=409,
            detail="Account lifecycle operation in progress",
        ) from exc
    except MemoryRequestRejectedError as exc:
        logger.info("internal frankenmemory tool %s rejected: %s", name, exc)
        raise HTTPException(
            status_code=409,
            detail="Frankenmemory rejected the memory operation",
        ) from exc
    except Exception as exc:
        logger.warning("internal frankenmemory tool %s failed: %s", name, exc)
        raise HTTPException(status_code=502, detail="Frankenmemory tool failed") from exc


@app.post("/api/internal/copal/tool", include_in_schema=False)
async def internal_copal_tool(request: Request, body: Dict[str, object]):
    """Private loopback broker for the single app-supervised Copal bridge."""
    owner = (request.headers.get(INTERNAL_TOOL_OWNER_HEADER) or "").strip()
    workspace = (request.headers.get("X-Open-Clank-Copal-Workspace") or "").strip()
    token = request.headers.get("X-Open-Clank-Copal-Token", "")
    from src.openclank.acp_bridge import copal_broker_token

    try:
        expected_token = copal_broker_token(INTERNAL_TOOL_TOKEN, owner, workspace)
    except ValueError:
        expected_token = ""
    if (
        not token
        or not expected_token
        or not secrets.compare_digest(token, expected_token)
        or not _is_trusted_loopback(request)
        or AUTH_ENABLED and owner not in getattr(auth_manager, "users", {})
    ):
        raise HTTPException(status_code=403, detail="Internal Copal broker only")

    operation = str(body.get("operation") or "").strip()
    args = body.get("args")
    allowed_operations = {
        "status", "scoped_status", "index", "get", "history", "ops",
        "create", "write", "checkpoint", "rename", "trash", "restore",
        "restore_deleted", "delete", "asset_path", "export_snapshot",
    }
    if operation not in allowed_operations or not isinstance(args, dict):
        raise HTTPException(status_code=400, detail="Invalid Copal broker request")
    scoped_args = dict(args)
    if "owner" in scoped_args and str(scoped_args["owner"] or "").strip() != owner:
        raise HTTPException(status_code=403, detail="Copal owner scope mismatch")
    if "workspace" in scoped_args and str(scoped_args["workspace"] or "").strip() not in {"", workspace}:
        raise HTTPException(status_code=403, detail="Copal workspace scope mismatch")
    if "workspace_id" in scoped_args and str(scoped_args["workspace_id"] or "").strip() not in {"", workspace}:
        raise HTTPException(status_code=403, detail="Copal workspace scope mismatch")
    bridge = getattr(request.app.state, "copal_bridge", None)
    if bridge is None:
        raise HTTPException(status_code=503, detail="Copal broker unavailable")
    try:
        async with _account_request_barrier.admitted(owner):
            result = await bridge.call(operation, scoped_args)
            return {"ok": True, "result": result}
    except AccountRequestFenced as exc:
        raise HTTPException(
            status_code=409,
            detail="Account lifecycle operation in progress",
        ) from exc
    except Exception as exc:
        logger.warning("internal Copal tool failed: %s", exc)
        raise HTTPException(status_code=502, detail="Copal tool failed") from exc


from routes.skills_routes import setup_skills_routes
app.include_router(setup_skills_routes(skills_manager))

# Chat
from routes.chat_routes import setup_chat_routes
_chat_router = setup_chat_routes(
    session_manager, chat_handler, chat_processor,
    memory_manager, research_handler, upload_handler,
    memory_vector=memory_vector,
    webhook_manager=webhook_manager,
    skills_manager=skills_manager,
)
app.state.openclank_chat_stream_handler = getattr(
    _chat_router,
    "openclank_chat_stream_handler",
)
app.include_router(_chat_router)


async def _quiesce_account_owner_writers(owner: str) -> dict[str, object]:
    """Close every detached late-writer seam before account inventory."""

    owner = str(owner or "").strip()
    if not owner:
        raise ValueError("account owner is required for writer quiescence")

    from routes.chat_helpers import drain_owner_background_tasks
    from routes.skills_routes import quiesce_owner_skill_job_handles
    from src import agent_runs as _agent_runs
    from src.memory_maintenance import drain_owner_grooms

    receipts: dict[str, object] = {}
    # Detached agent cancellation can itself finish persistence or dispatch
    # post-response work, so join it before taking the chat-task snapshot.
    receipts["agent_runs"] = await _agent_runs.quiesce_owner(owner)
    receipts["chat_background"] = await drain_owner_background_tasks(owner)

    scheduler_quiesce = getattr(
        globals().get("task_scheduler"),
        "quiesce_owner_lifecycle",
        None,
    )
    if not callable(scheduler_quiesce):
        raise RuntimeError("account scheduler writer drain is unavailable")
    receipts["task_scheduler"] = await scheduler_quiesce(owner)
    receipts["skill_jobs"] = await quiesce_owner_skill_job_handles(owner)

    email_drain = getattr(
        globals().get("email_router"),
        "drain_owner_writers",
        None,
    )
    if not callable(email_drain):
        raise RuntimeError("account email writer drain is unavailable")
    receipts["email_writers"] = await email_drain(owner)

    drain_reconcile = getattr(
        memory_lifecycle,
        "drain_owner_reconcile_tasks",
        None,
    )
    receipts["memory_reconcile"] = (
        await drain_reconcile(owner)
        if callable(drain_reconcile)
        else {"available": False}
    )
    receipts["memory_groom"] = await drain_owner_grooms(owner)

    # Deep-research quiescence persists the owner fence.  Run it last so no
    # later drain failure can leave a fresh operation fenced without a receipt.
    research_quiesce = getattr(research_handler, "quiesce_owner", None)
    if not callable(research_quiesce):
        raise RuntimeError("account research writer drain is unavailable")
    receipts["research"] = await research_quiesce(owner)
    return receipts


app.state.quiesce_account_owner_writers = _quiesce_account_owner_writers

# Research (background deep-research tasks)
from routes.research.research_routes import setup_research_routes
app.include_router(setup_research_routes(research_handler, session_manager=session_manager))

# History
from routes.history.history_routes import setup_history_routes
app.include_router(setup_history_routes(session_manager, upload_handler=upload_handler))

# Search
from routes.search_routes import setup_search_routes
app.include_router(setup_search_routes(config))

# Presets
from routes.preset_routes import setup_preset_routes
app.include_router(setup_preset_routes(preset_manager))

# Diagnostics
from routes.diagnostics_routes import setup_diagnostics_routes
app.include_router(setup_diagnostics_routes(rag_manager, rag_available, research_handler, memory_vector))

# Cleanup
from routes.cleanup.cleanup_routes import setup_cleanup_routes
app.include_router(setup_cleanup_routes(session_manager))

# Personal docs
from routes.personal_routes import setup_personal_routes
app.include_router(setup_personal_routes(personal_docs_mgr, rag_manager, rag_available))

# Embedding model management
from routes.embedding_routes import setup_embedding_routes
app.include_router(setup_embedding_routes())

# Normalized provider control plane and its read-only model compatibility
# projections. This is the only provider authority registered by the app.
from routes.provider_v1_routes import setup_provider_v1_routes
app.include_router(setup_provider_v1_routes())

# Copal's first-party Redb workspace adapter.
from routes.copal_routes import setup_copal_routes
from src.openclank.file_policy import FilePolicyRepository

# Files and semantic attachment consumers share one application-scoped policy
# authority. A route-local repository can observe a different generation after
# a reset/revocation and would make preparation/final CAS inconsistent.
files_policy_repository = FilePolicyRepository()
app.state.files_policy_repository = files_policy_repository
app.include_router(setup_copal_routes(policy_repository=files_policy_repository))

# TTS
from routes.tts_routes import setup_tts_routes
app.include_router(setup_tts_routes(tts_service))

# STT
from services.stt import get_stt_service
stt_service = get_stt_service()
from routes.stt_routes import setup_stt_routes
app.include_router(setup_stt_routes(stt_service))
logger.info("STT service initialized (provider managed via settings)")

# Documents (artifacts/canvas)
from routes.document_routes import setup_document_routes
document_router = setup_document_routes(session_manager, upload_handler)
app.include_router(document_router)

# Signatures (reusable image stamps)
from routes.signature_routes import setup_signature_routes
app.include_router(setup_signature_routes())

# Gallery (image library)
from routes.gallery.gallery_routes import setup_gallery_routes
app.include_router(setup_gallery_routes())

# Persisted image-editor drafts (server-backed projects)
from routes.editor_draft_routes import setup_editor_draft_routes
app.include_router(setup_editor_draft_routes())

# Imps managed image projects (editable state + Lore-backed Save)
from routes.image_project_routes import setup_image_project_routes
app.include_router(setup_image_project_routes())

# Scheduled tasks + event bus
from src.task_scheduler import TaskScheduler
task_scheduler = TaskScheduler(session_manager, auth_manager=auth_manager)
app.state.task_scheduler = task_scheduler
from src.event_bus import set_task_scheduler
set_task_scheduler(task_scheduler)
from routes.task_routes import setup_task_routes
app.include_router(setup_task_routes(task_scheduler))

from routes.assistant_routes import setup_assistant_routes
app.include_router(setup_assistant_routes(task_scheduler))

# Calendar (CalDAV)
from routes.calendar_routes import setup_calendar_routes
calendar_router = setup_calendar_routes(upload_handler=upload_handler)
app.include_router(calendar_router)

# Shell (user-facing command execution)
from routes.shell_routes import setup_shell_routes
app.include_router(setup_shell_routes())

# Cookbook (model download/serve/cache, cookbook state sync)
from routes.cookbook_routes import setup_cookbook_routes
app.include_router(setup_cookbook_routes())

from routes.workspace_routes import setup_workspace_routes
app.include_router(setup_workspace_routes())

from routes.odysseus_files_routes import setup_odysseus_files_routes
app.include_router(setup_odysseus_files_routes())

# Versioned provider-neutral Files namespace. The existing Files routes remain
# active until Host/Copal/Gallery/Library parity is proven; this additive facade
# owns opaque ResourceRefs and never shadows provider bytes.
from routes.files_facade_routes import setup_files_facade_routes
app.include_router(setup_files_facade_routes(policy_repository=files_policy_repository))

# Canonical Location/Workspace/People/Agent policy state. Compatibility root
# routes remain during migration, but scoped resets write only this ledger.
from routes.file_policy_routes import setup_file_policy_routes
app.include_router(setup_file_policy_routes())

# Hardware model fitting (cookbook "What Fits?" tab)
from routes.hwfit_routes import setup_hwfit_routes
app.include_router(setup_hwfit_routes())

# Model A/B Comparison
from routes.compare.compare_routes import setup_compare_routes
app.include_router(setup_compare_routes(session_manager))

# User Preferences
from routes.prefs_routes import setup_prefs_routes
app.include_router(setup_prefs_routes())

# Backup (export/import user data)
from routes.backup_routes import setup_backup_routes
app.include_router(setup_backup_routes(
    memory_manager, preset_manager, skills_manager, memory_provider=memory_provider
))

from routes.font_routes import setup_font_routes
app.include_router(setup_font_routes())


# MCP (Model Context Protocol)
from src.mcp_manager import McpManager
from src.agent_tools import set_mcp_manager
from routes.mcp_routes import setup_mcp_routes

mcp_manager = McpManager()
set_mcp_manager(mcp_manager)
app.include_router(setup_mcp_routes(mcp_manager))
logger.info("MCP routes initialized")

# AI Interaction tools (debates, pipelines, self-managing AI, UI control)
from src.ai_interaction import set_session_manager as set_ai_session_manager, set_memory_manager as set_ai_memory_manager, set_rag_manager as set_ai_rag_manager
set_ai_session_manager(session_manager)
set_ai_memory_manager(
    memory_manager,
    memory_vector,
    provider=memory_provider,
    lifecycle=memory_lifecycle,
)
set_ai_rag_manager(rag_manager, personal_docs_mgr)
logger.info("AI interaction tools initialized (session, memory, RAG, UI control)")

# Webhooks
from routes.webhook_routes import setup_webhook_routes
app.include_router(setup_webhook_routes(webhook_manager, auth_manager, session_manager, api_key_manager))

# API Tokens
from routes.api_token_routes import setup_api_token_routes
app.include_router(setup_api_token_routes())

logger.info("Webhook & API token routes initialized")

# Notes (Google Keep-style notes/todos)
from routes.note.note_routes import setup_note_routes
app.include_router(setup_note_routes(task_scheduler, upload_handler=upload_handler))

# Email
from routes.email_routes import setup_email_routes
from routes.email_pollers import configure_owner_lifecycle as configure_email_owner_lifecycle
configure_email_owner_lifecycle(
    auth_manager.is_account_lifecycle_fenced,
    lambda owner: owner == "local-installation" or owner in auth_manager.users,
)
email_router = setup_email_routes()
app.state.invalidate_email_owner_runtime = email_router.invalidate_owner_runtime
app.include_router(email_router)

# Codex integration — HTTP surface for the Codex plugin/MCP bridge. Reuses
# api_token scopes (todos:read|write, email:read|draft|send) so external
# Codex sessions can only touch the data the user explicitly allowed. Mounted
# AFTER email so the codex_routes can borrow the email router for shared
# search/threading helpers.
from routes.codex_routes import setup_codex_routes, setup_claude_routes
app.include_router(setup_codex_routes(
    email_router=email_router,
    memory_router=memory_router,
    calendar_router=calendar_router,
    document_router=document_router,
))
app.include_router(setup_claude_routes())

from routes.vault_routes import setup_vault_routes
app.include_router(setup_vault_routes())

# Contacts (CardDAV)
from routes.contacts.contacts_routes import setup_contacts_routes
app.include_router(setup_contacts_routes())

from companion import setup_companion_routes
app.include_router(setup_companion_routes())

from routes.tui_routes import setup_tui_routes
_tui_router = setup_tui_routes(
    auth_manager=auth_manager,
    request_barrier=_account_request_barrier,
)
app.state.invalidate_tui_owner_runtime = _tui_router.invalidate_owner_runtime
app.include_router(_tui_router)

# ========= ROUTES (kept in app.py) =========

@app.get("/")
async def serve_index(request: Request):
    static_path = abs_join(BASE_DIR, "static/index.html")
    if os.path.exists(static_path):
        return serve_html_with_nonce(request, static_path)
    # No static bundle — fall back to a root-level index.html if one is shipped.
    # If neither exists, serve_html_with_nonce logs it and returns a generic 500:
    # a missing index.html is a broken deployment (server fault), not a client
    # "not found". This keeps the app-shell route consistent with the other
    # bundled-template routes instead of mislabelling the fault as a 404.
    return serve_html_with_nonce(request, abs_join(BASE_DIR, "index.html"))

# One browser-target registry (static/js/appletRoutes.js) owns canonical
# applet addresses. These literal shell routes serve the SPA for every direct
# applet path so deep links and refresh work; /copal/* stays as a compatibility
# alias that redirects to the canonical direct path. Unknown URLs and API
# routes are intentionally NOT caught here — they keep meaningful errors.
_SHELL_APPLET_PATHS = (
    "/notes",       # legacy alias -> Editor
    "/code",        # legacy alias -> Editor
    "/bases",       # legacy alias -> Editor (Base leaf)
    "/editor",
    "/wiki",        # Wiki page type inside Editor; never a separate applet
    "/timeline",
    "/todo",
    "/graph",
    "/mind",        # legacy alias -> Graph (mind mode)
    "/galaxy",      # legacy alias -> Graph (galaxy mode)
    "/treehouse",
    "/files",
    "/calendar",
    "/cookbook",
    "/email",
    "/memory",
    "/gallery",
    "/tasks",
    "/library",
)
for _shell_path in _SHELL_APPLET_PATHS:
    app.add_api_route(
        _shell_path, serve_index, methods=["GET"], include_in_schema=False,
        name=f"serve_shell{_shell_path.replace('/', '_')}",
    )

@app.get("/settings", include_in_schema=False)
@app.get("/settings/{panel}", include_in_schema=False)
async def serve_settings(request: Request, panel: str | None = None):
    # Settings panels (including History and File Access) are reachable by
    # direct navigation; the client opens the named panel once.
    return await serve_index(request)

# Legacy /copal/* addresses redirect to the canonical direct applet path,
# preserving query state (doc/mode). They are aliases, not destinations.
_COPAL_VIEW_TO_PATH = {
    "notes": "/editor",
    "editor": "/editor",
    "code": "/editor",
    "bases": "/editor",
    "wiki": "/wiki",
    "timeline": "/timeline",
    "todo": "/todo",
    "graph": "/graph",
    "mind": "/graph",
    "galaxy": "/graph",
    "treehouse": "/treehouse",
    "files": "/files",
    "calendar": "/calendar",
}

@app.get("/copal", include_in_schema=False)
@app.get("/copal/{view}", include_in_schema=False)
async def serve_copal_alias(request: Request, view: str = "notes"):
    from urllib.parse import urlencode
    target = _COPAL_VIEW_TO_PATH.get((view or "notes").lower())
    if target is None:
        # Unknown copal subpath: still the SPA shell so client-side aliases can
        # resolve it; do not invent a new applet.
        return await serve_index(request)
    query = dict(request.query_params)
    if (view or "").lower() in ("mind", "galaxy") and "mode" not in query:
        query["mode"] = view.lower()
    # `/bases` resolves the Bases leaf intent via the client registry; the
    # legacy `/copal/bases` alias must preserve it the same way or bookmarks
    # lose the leaf. Symmetric with SEGMENTS.bases.openBases.
    if (view or "").lower() == "bases" and "open" not in query:
        query["open"] = "bases"
    suffix = ("?" + urlencode(query)) if query else ""
    return RedirectResponse(url=f"{target}{suffix}", status_code=302)

@app.get("/backgrounds")
async def serve_backgrounds(request: Request):
    """Sandbox page for prototyping background effects. No auth required."""
    return serve_html_with_nonce(request, abs_join(BASE_DIR, "static/backgrounds.html"))

@app.get("/login")
async def serve_login(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse(url="/", status_code=302)
    return serve_html_with_nonce(request, abs_join(BASE_DIR, "static/login.html"))

@app.get("/api/version")
async def get_version():
    from core.constants import APP_VERSION
    return {"version": APP_VERSION}

@app.get("/api/health")
async def health_check() -> Dict[str, str]:
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get(DESKTOP_SHELL_READY_PATH, include_in_schema=False)
async def desktop_shell_readiness_check(request: Request) -> JSONResponse:
    """Prove this exact loopback listener is the shell-spawned backend child."""
    binding = _DESKTOP_SHELL_BINDING
    challenge = request.headers.get(DESKTOP_SHELL_CHALLENGE_HEADER, "")
    if (
        binding is None
        or not _is_trusted_loopback(request)
        or request.url.scheme != "http"
        or request.headers.get("host") != binding.expected_host_header
        or not binding.challenge_is_valid(challenge)
    ):
        raise HTTPException(status_code=404, detail="Not found")

    from src.readiness import check_readiness

    result = check_readiness(supervisor=getattr(app.state, "mimo_supervisor", None))
    ready = bool(result.get("ready"))
    return JSONResponse(
        status_code=200 if ready else 503,
        content=binding.payload(
            challenge,
            ready=ready,
            auth_enabled=AUTH_ENABLED,
        ),
        headers={"Cache-Control": "no-store"},
    )

@app.post("/api/client-perf")
async def client_perf(request: Request):
    """Low-volume frontend timing reports for stalls that happen before SSE logs."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        kind = str(data.get("type") or "client").replace("\n", " ")[:80]
        total_ms = float(data.get("total_ms") or 0)
        stages = data.get("stages") if isinstance(data.get("stages"), list) else []
        stage_txt = " ".join(
            f"{str(s.get('name') or '')[:40]}={float(s.get('delta_ms') or 0):.0f}ms"
            for s in stages[:20]
            if isinstance(s, dict)
        )
        extra = str(data.get("extra") or "").replace("\n", " ")[:200]
        logging.getLogger("app.client_perf").warning(
            "client_perf type=%s total=%.0fms %s%s",
            kind,
            total_ms,
            stage_txt,
            f" extra={extra}" if extra else "",
        )
    except Exception:
        logging.getLogger("app.client_perf").debug("client_perf log failed", exc_info=True)
    return {"ok": True}

@app.get("/api/ready")
async def readiness_check() -> JSONResponse:
    """Readiness / integrity self-check — DB, data dir, local-first storage.

    Unlike /api/health (liveness), this returns 503 unless every critical
    subsystem is whole, so an orchestrator can gate traffic on real readiness.
    """
    from src.readiness import check_readiness
    result = check_readiness(supervisor=getattr(app.state, "mimo_supervisor", None))
    return JSONResponse(status_code=200 if result.get("ready") else 503, content=result)

@app.get("/api/runtime")
async def runtime_info() -> Dict[str, object]:
    in_docker = os.path.exists("/.dockerenv")
    if not in_docker:
        try:
            with open("/proc/1/cgroup", "r", encoding="utf-8", errors="ignore") as fh:
                cg = fh.read()
            in_docker = any(marker in cg for marker in ("docker", "containerd", "kubepods"))
        except Exception:
            in_docker = False
    ollama_url = (
        os.getenv("OLLAMA_BASE_URL")
        or os.getenv("OLLAMA_URL")
        or ("http://host.docker.internal:11434/v1" if in_docker else "http://127.0.0.1:11434/v1")
    )
    return {
        "in_docker": in_docker,
        "ollama_base_url": ollama_url,
    }

# ========= LIFECYCLE =========

@asynccontextmanager
async def _lifespan(app):
    """Modern lifespan context manager replacing deprecated @app.on_event."""
    # ── STARTUP ──
    await _startup_event()
    yield
    # ── SHUTDOWN ──
    await _shutdown_event()

app.router.lifespan_context = _lifespan


async def _startup_event():
    global upload_cleanup_task
    logger.info("Application starting up...")
    # The managed provider engine is a hard installation boundary.  A source
    # setup must build it before launch; packages and containers ship the exact
    # verified payload.  Recovery mode is deliberately non-ready and exists
    # only so an operator can run diagnostics after a failed installation.
    try:
        from core.constants import APP_VERSION
        from src.openclank.engine_build import source_fingerprint, verify_install

        _engine_source = source_fingerprint(Path(__file__).resolve().parent / "packages" / "mimo-code")
        app.state.engine_verification = verify_install(
            expected_version=APP_VERSION,
            expected_source_sha256=_engine_source,
            run_smoke=True,
            acp_smoke=True,
        ).require()
    except Exception as exc:
        app.state.engine_verification = None
        if str(os.environ.get("OPENCLANK_RECOVERY_MODE") or "").lower() not in {
            "1", "true", "yes", "on"
        }:
            raise RuntimeError(f"Open Clank managed engine preflight failed: {exc}") from exc
        logger.error("Recovery mode: managed engine preflight failed: %s", exc)
    webhook_manager.set_loop(asyncio.get_running_loop())
    # Wipe any leftover incognito sessions from previous process — they're
    # ephemeral by design and must not survive a restart.
    try:
        from core.database import SessionLocal as _SL, Session as _DbSess, ChatMessage as _DbMsg
        _db = _SL()
        try:
            _ghosts = _db.query(_DbSess).filter(_DbSess.name.in_(("Nobody", "Incognito"))).all()
            for _g in _ghosts:
                _db.query(_DbMsg).filter(_DbMsg.session_id == _g.id).delete()
                _db.delete(_g)
            if _ghosts:
                _db.commit()
                logger.info(f"Purged {len(_ghosts)} leftover incognito session(s)")
        finally:
            _db.close()
    except Exception as e:
        logger.debug(f"Incognito purge skipped: {e}")
    # Strong refs to fire-and-forget startup tasks. Without this, Python may
    # GC tasks created with `asyncio.create_task(...)` before they finish.
    _startup_tasks: list[asyncio.Task] = getattr(app.state, "_startup_tasks", [])
    app.state._startup_tasks = _startup_tasks

    # Copal is an owned in-process loose-file lifecycle adapter, not a second
    # public web server. Ordinary vault files are now the live source of truth;
    # Redb remains an explicit rollback backend selected with
    # ``COPAL_STORAGE=redb`` and is never deleted by this switch.
    try:
        storage = str(os.environ.get("COPAL_STORAGE") or "files").strip().lower()
        if storage in {"redb", "db", "database"}:
            from src.openclank.copal_bridge import CopalBridge
            app.state.copal_bridge = CopalBridge()
        else:
            from src.openclank.copal_loose import LooseCopalBridge
            app.state.copal_bridge = LooseCopalBridge(
                os.environ.get("COPAL_LOOSE_ROOT")
                or str(Path(DATA_DIR) / "copal-vaults")
            )
        await app.state.copal_bridge.start()
    except Exception as e:
        logger.error("Copal %s bridge failed to start: %s", storage if "storage" in locals() else "storage", e, exc_info=True)
        app.state.copal_bridge = None
    if upload_cleanup_func:
        upload_cleanup_task = asyncio.create_task(upload_cleanup_func())
    # Always-on monitor that auto-continues the agent when a background bash
    # job (#!bg) finishes — re-invokes the turn with the job output.
    try:
        from src.bg_monitor import start_bg_monitor
        _startup_tasks.append(start_bg_monitor())
    except Exception as _e:
        logger.warning("Failed to start background-job monitor: %s", _e)
    # MCP servers can be slow or blocked by local tooling. Connect them after
    # the web server is accepting traffic instead of delaying the whole UI.
    async def _startup_mcp_connections():
        try:
            from src.builtin_mcp import register_builtin_servers
            await register_builtin_servers(mcp_manager)
        except BaseException as e:
            logger.warning(f"Built-in MCP registration failed (non-critical): {type(e).__name__}: {e}")
        try:
            await mcp_manager.connect_all_enabled()
        except asyncio.TimeoutError:
            logger.warning("User MCP startup timed out (non-critical)")
        except BaseException as e:
            logger.warning(f"MCP startup failed (non-critical): {type(e).__name__}: {e}")

    _startup_tasks.append(asyncio.create_task(_startup_mcp_connections()))

    # Startup warmups are opt-in. They make later requests a little warmer, but
    # they also compete with the first seconds of real UI use on slow or busy
    # machines. Default to clear/idle startup and let requests warm what they use.
    _startup_warmups_enabled = str(os.getenv("ODYSSEUS_STARTUP_WARMUPS", "")).lower() in {"1", "true", "yes", "on"}
    if _startup_warmups_enabled:
        async def _warmup_tool_index():
            try:
                from src.tool_index import get_tool_index
                idx = await asyncio.to_thread(get_tool_index)
                if idx:
                    await asyncio.to_thread(idx.get_tools_for_query, "warmup", 8)
                    logger.info("[startup] Tool index pre-warmed")
            except Exception as e:
                logger.warning(f"Tool index warmup failed (non-critical): {type(e).__name__}: {e}")

        _startup_tasks.append(asyncio.create_task(_warmup_tool_index()))

        async def _warmup_endpoints():
            try:
                import httpx
                urls = (
                    await asyncio.to_thread(model_discovery.warmup_ping_urls)
                    if model_discovery else []
                )
                for url in urls:
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            await client.get(url)
                        logger.info(f"Warmup ping OK: {url}")
                    except Exception as e:
                        logger.debug(f"Warmup ping failed for endpoint: {e}")
            except Exception as e:
                logger.debug(f"Warmup ping skipped: {e}")

        _startup_tasks.append(asyncio.create_task(_warmup_endpoints()))
    else:
        logger.info("Startup warmups disabled (set ODYSSEUS_STARTUP_WARMUPS=1 to enable)")

    # Keep-alive is opt-in. The ping path performs model discovery, and when
    # stale LAN endpoints are configured it can add periodic backend pressure
    # that delays unrelated UI requests such as Notes/Documents.
    _keepalive_enabled = str(os.getenv("ODYSSEUS_MODEL_KEEPALIVE", "")).lower() in {"1", "true", "yes", "on"}
    if _keepalive_enabled:
        async def _keepalive_loop():
            while True:
                try:
                    await asyncio.sleep(60)
                    await _warmup_endpoints()
                except Exception as e:
                    logger.warning(f"Keepalive loop error: {e}")
                    await asyncio.sleep(300)  # Back off on error

        _startup_tasks.append(asyncio.create_task(_keepalive_loop()))

    async def _initialize_and_migrate_memory():
        if not memory_provider:
            return
        await memory_provider.initialize()
        if memory_lifecycle is not None:
            reconciled = await memory_lifecycle.reconcile()
            if any(reconciled.values()):
                logger.info(
                    "Memory-derived skill forget reconciliation: %s",
                    reconciled,
                )
            require_memory_lifecycle_convergence(reconciled)
        if getattr(memory_provider, "provider_id", "") != "frankenmemory":
            return
        from core.atomic_io import atomic_write_json
        ledger_path = os.path.join(DATA_DIR, "memory_provider_migration_v1.json")
        try:
            with open(ledger_path, encoding="utf-8") as handle:
                ledger = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            ledger = {"version": 1, "migrated_ids": [], "ownerless_ids": []}
        migrated = set(ledger.get("migrated_ids") or [])
        ownerless = set(ledger.get("ownerless_ids") or [])
        for entry in memory_manager.load_all():
            legacy_id = str(entry.get("id") or "")
            if not legacy_id or legacy_id in migrated:
                continue
            owner = str(entry.get("owner") or "").strip()
            if not owner:
                ownerless.add(legacy_id)
                continue
            metadata = dict(entry.get("metadata") or {})
            metadata.update({
                "migrated_from": "memory.json",
                "legacy_id": legacy_id,
                "legacy_timestamp": entry.get("timestamp"),
            })
            record = await memory_provider.remember(
                str(entry.get("text") or ""),
                owner=owner,
                session_id=entry.get("session_id"),
                category=entry.get("category") or "fact",
                source=entry.get("source") or "legacy_migration",
                metadata=metadata,
            )
            if entry.get("pinned"):
                await memory_provider.pin(record.id, True, owner=owner)
            migrated.add(legacy_id)
            ledger.update({
                "migrated_ids": sorted(migrated),
                "ownerless_ids": sorted(ownerless),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            atomic_write_json(ledger_path, ledger)
        ledger.update({
            "migrated_ids": sorted(migrated),
            "ownerless_ids": sorted(ownerless),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        atomic_write_json(ledger_path, ledger)
        logger.info(
            "Memory migration checked: %d migrated, %d ownerless awaiting claim",
            len(migrated), len(ownerless),
        )

    # Memory lifecycle recovery is a readiness boundary: do not serve traffic
    # while provider state and memory-derived skills may still be split.
    await _initialize_and_migrate_memory()

    # Frankenmemory maintenance is deliberately one small task, not another
    # scheduler framework. Zero (the default) disables it for deployments that
    # groom externally; an explicit interval runs one pass per real owner.
    from src.memory_maintenance import groom_interval_hours, groom_loop
    from src.memory_scope import chat_workspace, memory_owner
    _groom_hours = groom_interval_hours()
    if memory_provider and getattr(memory_provider, "provider_id", "") == "frankenmemory":
        if _groom_hours <= 0:
            logger.info("Frankenmemory auto-groom disabled (FM_GROOM_INTERVAL_HOURS=0)")
        else:
            def _memory_groom_owners():
                if AUTH_ENABLED and auth_manager.is_configured:
                    return list(auth_manager.users)
                return [memory_owner(None)]

            _startup_tasks.append(asyncio.create_task(groom_loop(
                memory_provider,
                _groom_hours,
                owners=_memory_groom_owners,
                workspace_id=chat_workspace(),
                owner_is_fenced=auth_manager.is_account_lifecycle_fenced,
            )))
            logger.info("Frankenmemory auto-groom scheduled every %.2fh", _groom_hours)

    async def _ensure_default_tasks():
        # Create/reconcile default automation tasks + personal assistant for every user.
        owners = set()
        try:
            import json as _json
            auth_path = AUTH_FILE
            with open(auth_path, encoding="utf-8") as f:
                users = _json.load(f).get("users", {})
            owners.update(users.keys())
        except Exception as e:
            logger.debug(f"Default task auth-owner scan: {e}")

        # Also reconcile owners already present in scheduled_tasks. This cleans
        # up stale/demo/deleted-user built-ins that are no longer in auth.json;
        # otherwise their old scheduled rows can keep firing forever.
        try:
            from core.database import SessionLocal, ScheduledTask
            from src.task_scheduler import HOUSEKEEPING_DEFAULTS
            builtin_names = []
            for defs in HOUSEKEEPING_DEFAULTS.values():
                builtin_names.append(defs["name"])
                builtin_names.extend(defs.get("legacy_names") or [])
            db_seed = SessionLocal()
            try:
                rows = db_seed.query(ScheduledTask.owner).filter(
                    (ScheduledTask.action.in_(list(HOUSEKEEPING_DEFAULTS.keys())))
                    | (ScheduledTask.name.in_(builtin_names))
                ).distinct().all()
                owners.update(row[0] for row in rows if row[0])
            finally:
                db_seed.close()
        except Exception as e:
            logger.debug(f"Default task existing-owner scan: {e}")

        try:
            for uname in sorted(owners):
                try:
                    await task_scheduler.ensure_defaults(uname)
                except Exception as e:
                    logger.debug(f"ensure_defaults({uname}): {e}")
        except Exception as e:
            logger.debug(f"Default tasks: {e}")

    # Reconcile built-in tasks before the runner starts. Otherwise legacy
    # scheduled built-ins can fire once before being converted to event tasks.
    await _ensure_default_tasks()

    # Disk-backed skills are not covered by the DB legacy-owner sweep. Repair
    # only ownerless SKILL.md files; a non-empty owner remains valid provenance
    # even after that account is removed.
    try:
        import json as _json
        auth_path = AUTH_FILE
        with open(auth_path, encoding="utf-8") as f:
            users = _json.load(f).get("users", {})
        primary_owner = None
        for uname, udata in users.items():
            if udata.get("is_admin") is True:
                primary_owner = uname
                break
        if not primary_owner and users:
            primary_owner = next(iter(users))
        if primary_owner:
            changed = skills_manager.backfill_owner(primary_owner)
            if changed:
                logger.info("Assigned %s ownerless legacy skill file(s) to %s", changed, primary_owner)
    except Exception as e:
        logger.debug(f"Skill owner backfill skipped: {e}")

    # Start scheduled task runner — skip when running under a cron-driven
    # deployment where an external worker drives task firing. Mirrors
    # `ODYSSEUS_INPROCESS_POLLERS` from the email pollers.
    _tasks_inprocess = os.environ.get("ODYSSEUS_INPROCESS_TASKS", "1").strip().lower()
    if _tasks_inprocess not in ("0", "false", "no", "off", ""):
        await task_scheduler.start()
    else:
        logger.info(
            "In-process task scheduler disabled (ODYSSEUS_INPROCESS_TASKS=0); "
            "drive task firing externally (e.g. cron)."
        )
    # Periodic null-owner sweep — re-runs the legacy-owner assignment hourly
    # so any data created while auth was disabled / localhost-bypassed gets
    # claimed by the admin instead of staying world-visible (M19).
    async def _null_owner_sweep_loop():
        while True:
            try:
                await asyncio.sleep(3600)
                from core.database import _migrate_assign_legacy_owner
                await asyncio.to_thread(_migrate_assign_legacy_owner)
            except Exception as e:
                logger.debug(f"Null-owner sweep skipped: {e}")
                await asyncio.sleep(3600)

    _startup_tasks.append(asyncio.create_task(_null_owner_sweep_loop()))

    # Nightly skill audit — at ~02:00 local, test + judge a batch of the
    # least-recently-checked skills, auto-fixing/escalating weak ones (never
    # deletes). Rotates through the library so each night covers different
    # skills. Gated by the `skill_audit_nightly` setting (default on); hour via
    # `skill_audit_hour` (default 2), batch size via `skill_audit_batch` (8).
    async def _skill_audit_nightly_loop():
        from datetime import timedelta
        while True:
            try:
                from src.settings import get_setting
                hour = int(get_setting("skill_audit_hour", 2) or 2)
            except Exception:
                hour = 2
            now = datetime.now()
            nxt = now.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep(max(60, (nxt - now).total_seconds()))
            try:
                from src.settings import get_setting
                if not get_setting("skill_audit_nightly", True):
                    continue
                batch = int(get_setting("skill_audit_batch", 8) or 8)
                from routes.skills_routes import run_scheduled_skill_audit
                await run_scheduled_skill_audit(
                    skills_manager, owner=None, max_skills=batch,
                    owner_is_fenced=auth_manager.is_account_lifecycle_fenced,
                )
            except Exception as e:
                logger.warning(f"Nightly skill audit failed: {e}")

    _startup_tasks.append(asyncio.create_task(_skill_audit_nightly_loop()))

    # Cookbook serve lifecycle — kills scheduler-launched serves whose
    # window-end has passed. Paired with the cookbook_serve builtin
    # action; both are no-ops unless a scheduled task actually launches
    # something with end_after_min set. Removing this line + the
    # cookbook_serve entry in BUILTIN_ACTIONS + src/cookbook_serve_lifecycle.py
    # removes the feature.
    from src.cookbook_serve_lifecycle import cookbook_serve_lifecycle_loop
    _startup_tasks.append(asyncio.create_task(cookbook_serve_lifecycle_loop()))

    # ── One installation history writer ──
    # The app supervisor mints one immutable credential per authenticated
    # account and gives the Rust writer only the credential file path. Worker
    # requests receive the matching client through their trusted account
    # context; no shared account token or owner-grant environment is used.
    _history_binary = os.environ.get("OPENCLANK_HISTORY_SERVICE_BIN", "").strip()
    if not _history_binary:
        _app_resources = os.environ.get("APP_RESOURCES", "").strip()
        # A packaged release is authoritative.  The repository release target
        # is only a development fallback and is health-negotiated by the
        # supervisor before it becomes reachable from the app.
        _history_candidates = []
        if _app_resources:
            _history_candidates.append(
                Path(_app_resources) / "libexec/openclank/history/openclank-history-service"
            )
        _history_candidates.append(
            Path(__file__).resolve().parent / "packages/openclank-history/target/release/openclank-history-service",
        )
        _history_binary = next(
            (str(_candidate) for _candidate in _history_candidates if _candidate.is_file() and os.access(_candidate, os.X_OK)),
            "",
        )
    if _history_binary:
        try:
            from src.openclank.history_client import (
                HistoryServiceSupervisor,
                ScopedHistoryCredential,
            )
            from src.openclank.history_settings import history_root, settings_path

            _history_credentials = []
            for _username, _record in getattr(auth_manager, "users", {}).items():
                _username = str(_username).strip()
                if not _username or not isinstance(_record, dict):
                    continue
                _account_id = str(_record.get("account_id") or "").strip()
                if not _account_id:
                    continue
                _capabilities = {"capture", "read", "restore", "settings-read", "settings-write"}
                if _record.get("is_admin") is True:
                    _capabilities.add("admin")
                _history_credentials.append(
                    ScopedHistoryCredential(
                        # A wildcard actor binding is still account-scoped;
                        # the authenticated request supplies the concrete
                        # human or agent actor id for action authorization.
                        actor_id="*",
                        account_id=_account_id,
                        capabilities=frozenset(_capabilities),
                    )
                )
            if not _history_credentials:
                # Before first account setup, retain a stable installation
                # partition so the writer remains available in no-auth mode.
                _history_credentials.append(
                    ScopedHistoryCredential(
                        actor_id="*",
                        account_id="local-installation",
                        capabilities=frozenset(
                            {
                                "admin",
                                "capture",
                                "read",
                                "restore",
                                "settings-read",
                                "settings-write",
                            }
                        ),
                    )
                )
            # The Files root registry is the provider authority for physical
            # destinations.  Publish only enabled, available roots with the
            # immutable accounts that own or were assigned each root; the
            # history worker rejects a path that is not paired with one of
            # these trusted root identities.
            from src.openclank.filesystem_registry import FilesystemRootRegistry

            _history_account_by_owner = {
                str(_username): str(_record.get("account_id") or "").strip()
                for _username, _record in getattr(auth_manager, "users", {}).items()
                if isinstance(_record, dict) and str(_record.get("account_id") or "").strip()
            }
            _history_admin_accounts = {
                str(_record.get("account_id") or "").strip()
                for _record in getattr(auth_manager, "users", {}).values()
                if isinstance(_record, dict)
                and _record.get("is_admin") is True
                and str(_record.get("account_id") or "").strip()
            }
            def _history_project_roots(_filesystem_data: dict[str, object]) -> list[dict[str, object]]:
                """Project one locked Files registry snapshot into service bindings."""
                projected: list[dict[str, object]] = []
                for _root_id, _root in (_filesystem_data.get("roots") or {}).items():
                    if not isinstance(_root, dict) or not _root.get("enabled") or _root.get("availability") != "available":
                        continue
                    _root_kind = str(_root.get("kind") or "recursive_directory")
                    if _root_kind not in {"exact_file", "recursive_directory"}:
                        continue
                    _accounts: set[str] = set(_history_admin_accounts)
                    _owner_account = _history_account_by_owner.get(str(_root.get("owner_id") or ""))
                    if _owner_account:
                        _accounts.add(_owner_account)
                    for _assignment in (_filesystem_data.get("visibility_assignments") or {}).values():
                        if not isinstance(_assignment, dict) or not _assignment.get("enabled") or str(_assignment.get("root_id") or "") != str(_root_id):
                            continue
                        if str(_assignment.get("subject_kind") or "user") == "user":
                            _assigned_account = _history_account_by_owner.get(str(_assignment.get("subject_id") or ""))
                            if _assigned_account:
                                _accounts.add(_assigned_account)
                    if _accounts and str(_root.get("canonical_path") or "").strip():
                        projected.append(
                            {
                                "root_id": str(_root_id),
                                "canonical_path": str(_root["canonical_path"]),
                                "kind": _root_kind,
                                "account_ids": sorted(_accounts),
                                "workspace_ids": [],
                            }
                        )
                return projected

            _history_authorized_roots: list[dict[str, object]] = []
            _history_root_generation = 0
            try:
                _filesystem_registry = FilesystemRootRegistry(
                    os.environ.get("ODYSSEUS_FILES_REGISTRY") or None
                )
                _filesystem_data = _filesystem_registry.snapshot()
                _history_root_generation = int(_filesystem_data.get("generation") or 0)
                _history_authorized_roots = _history_project_roots(_filesystem_data)
            except Exception as _root_error:
                logger.warning("Open Clank history root bindings paused: %s", _root_error)
            _history_socket = os.environ.get(
                "OPENCLANK_HISTORY_SOCKET",
                str(settings_path().parent / "history.sock"),
            )
            # RestoreHost must never inherit the worker's cwd or place
            # receipts beside user files. A configured Files host root is
            # authoritative; otherwise the private history root and an empty
            # map make host restore fail closed until Files publishes opaque
            # ResourceKey registrations.
            _history_restore_host_root = os.environ.get("OPENCLANK_HISTORY_HOST_ROOT") or str(history_root())
            _history_restore_receipt_root = os.environ.get(
                "OPENCLANK_HISTORY_RECEIPT_ROOT",
                str(history_root() / "restore-receipts"),
            )
            _history_restore_resource_map = os.environ.get(
                "OPENCLANK_HISTORY_RESOURCE_MAP",
                str(history_root() / "resource-map.json"),
            )
            _history_supervisor = HistoryServiceSupervisor(
                _history_binary,
                socket_path=_history_socket,
                catalog_path=settings_path().parent / "history.redb",
                lore_root=history_root(),
                credential_file=settings_path().parent / "history-credentials.json",
                credentials=_history_credentials,
                host_root=_history_restore_host_root,
                receipt_root=_history_restore_receipt_root,
                resource_map=_history_restore_resource_map,
                authorized_roots=_history_authorized_roots,
            )
            await _history_supervisor.start()
            app.state.history_supervisor = _history_supervisor
            os.environ["OPENCLANK_HISTORY_SOCKET"] = str(_history_socket)
            from src.openclank.files_service_client import set_history_binding_provider

            def _files_history_binding(
                owner: str,
                lane: str,
                scope: dict[str, object] | None = None,
            ) -> dict[str, object]:
                owner_text = str(owner or "").strip()
                account = "local-installation"
                owner_record: dict[str, object] | None = None
                if owner_text:
                    record = getattr(auth_manager, "users", {}).get(owner_text)
                    if isinstance(record, dict):
                        owner_record = record
                        account = str(record.get("account_id") or account).strip()
                actor = owner_text or "local-installation"
                if lane == "agent":
                    actor = f"agent:{actor}"
                requested_ids = {
                    str(value)
                    for value in ((scope or {}).get("visible_root_ids") or [])
                    if str(value).strip()
                }
                is_admin = bool(owner_record and owner_record.get("is_admin") is True)
                bindings = [
                    dict(root)
                    for root in _history_authorized_roots
                    if account in set(root.get("account_ids") or [])
                    and (is_admin or not requested_ids or str(root.get("root_id")) in requested_ids)
                ]
                if not is_admin and not requested_ids:
                    # A non-admin child must receive an explicit Files scope;
                    # owner or cwd paths are never an implicit grant.
                    bindings = []
                workspace_root = str(bindings[0]["canonical_path"]) if bindings else ""
                workspace_id = (
                    str((scope or {}).get("history_workspace_id") or "").strip()
                    or (f"files:{bindings[0]['root_id']}" if bindings else "")
                )
                return {
                    "socket": str(_history_socket),
                    "account_id": account,
                    "actor_id": actor,
                    "token": _history_supervisor.credential_token(actor, account),
                    "workspace_id": workspace_id,
                    "workspace_root": workspace_root,
                    "root_bindings": bindings,
                }

            set_history_binding_provider(_files_history_binding)
            async def _history_root_refresh_loop():
                nonlocal _history_authorized_roots, _history_root_generation
                while True:
                    await asyncio.sleep(1.0)
                    try:
                        _latest = await asyncio.to_thread(_filesystem_registry.snapshot)
                        _latest_generation = int(_latest.get("generation") or 0)
                        if _latest_generation == _history_root_generation:
                            continue
                        _latest_roots = _history_project_roots(_latest)
                        # The supervisor restarts one worker generation with
                        # both credentials and roots as a single publication.
                        await _history_supervisor.sync_authorized_roots(_latest_roots)
                        _history_authorized_roots = _latest_roots
                        _history_root_generation = _latest_generation
                    except asyncio.CancelledError:
                        raise
                    except Exception as _root_refresh_error:
                        logger.error("Open Clank history root refresh paused: %s", _root_refresh_error)

            _startup_tasks.append(asyncio.create_task(_history_root_refresh_loop()))
            logger.info("Open Clank history writer started for %d account(s)", len(_history_credentials))
        except Exception as _history_error:
            app.state.history_supervisor = None
            logger.warning("Open Clank history writer paused: %s", _history_error)

    # ── Open Clank managed-engine supervisor ──
    _agent_drive = (
        os.environ.get("OPEN_CLANK_AGENT_DRIVE")
        or os.environ.get("OPENTHESIUS_DRIVE")
        or "mimo"
    )
    if _agent_drive == "mimo":
        async def _startup_mimo():
            try:
                import sys as _sys
                _agent_src = (
                    os.environ.get("OPEN_CLANK_AGENT_SRC")
                    or os.environ.get("OPENTHESIUS_SRC")
                    or str(Path(__file__).resolve().parent / "src")
                )
                if _agent_src not in _sys.path:
                    _sys.path.insert(0, _agent_src)
                # Phase 5: ensure fm-mcp binary exists before supervisor starts
                from src.openclank.fmmcp_builder import ensure_fmmcp_built
                await ensure_fmmcp_built()

                from src.openclank.agent_supervisor import select_host_provider_owner
                from src.openclank.agent_supervisor_factory import build_agent_supervisor
                from src.openclank.local_model_executors import (
                    build_local_executor_broker,
                )

                # Parse OPEN_CLANK_SAFE_DIRS (colon-separated, ~ expanded).
                _raw = (
                    os.environ.get("OPEN_CLANK_SAFE_DIRS")
                    or os.environ.get("OPENTHESIUS_SAFE_DIRS")
                    or ""
                )
                _safe_dirs = [os.path.expanduser(d.strip()) for d in _raw.split(":") if d.strip()] if _raw else []

                # If OPEN_CLANK_AGENT_HOME is set, auto-include the agent
                # home in safe dirs so the agent can read/write its own files.
                _agent_home = (
                    os.environ.get("OPEN_CLANK_AGENT_HOME")
                    or os.environ.get("THESIUS_AGENT_HOME")
                )
                if _agent_home:
                    _agent_home = os.path.expanduser(_agent_home)
                    if _agent_home not in _safe_dirs:
                        _safe_dirs.append(_agent_home)

                _safe_dirs = _safe_dirs or None

                # AUTH_ENABLED is the deployment's isolation contract, not a
                # snapshot of whether first-run setup has happened yet.  A
                # fresh install starts the supervisor before the first admin
                # exists; booting the ownerless worker in that window leaves
                # it permanently unpartitioned after /api/auth/setup creates
                # the first account, which makes that account's endpoint
                # models fail admission with MODEL_NOT_PROJECTED.  Keep the
                # pool partitioned from process start whenever auth is
                # enabled; it will lazily create the first owner worker after
                # login and never needs to migrate an ownerless runtime.
                _auth_enabled = AUTH_ENABLED
                _initial_owner = ""
                _host_provider_owner = ""
                if _auth_enabled:
                    _admin_owners = [
                        str(name)
                        for name, record in getattr(auth_manager, "users", {}).items()
                        if isinstance(record, dict) and record.get("is_admin") is True
                    ]
                    _initial_owner = _admin_owners[0] if _admin_owners else ""
                    _host_provider_owner = select_host_provider_owner(
                        _admin_owners,
                        os.environ.get("OPENCLANK_HOST_PROVIDER_OWNER", ""),
                    )
                    if _admin_owners and not _host_provider_owner:
                        logger.warning(
                            "host model providers disabled: configure OPENCLANK_HOST_PROVIDER_OWNER when multiple admins exist"
                        )
                # One installation-wide spool and registered executor broker
                # back every owner worker.  The engine receives artifact IDs
                # and recipe IDs only; no worker gets filesystem or command
                # authority for local model execution.
                _model_artifacts = account_owner_lifecycle.artifact_store
                _local_executor_broker = build_local_executor_broker(
                    _model_artifacts,
                )
                app.state.model_artifacts = _model_artifacts
                app.state.local_executor_broker = _local_executor_broker
                _sup = build_agent_supervisor(
                    memory_provider=memory_provider,
                    safe_dirs=_safe_dirs,
                    auth_enabled=_auth_enabled,
                    initial_owner=_initial_owner,
                    host_provider_owner=_host_provider_owner,
                    local_executor_broker=_local_executor_broker,
                )
                _reap_orphaned_mimo_workers()
                await _sup.start()
                app.state.agent_supervisor = _sup
                app.state.mimo_supervisor = _sup
                task_scheduler._mimo_supervisor = _sup
                from src.model_dispatch import set_agent_supervisor
                set_agent_supervisor(_sup)
                logger.info("[openthesius] mimo supervisor started")
            except Exception as e:
                logger.error("Open Clank managed engine failed to start: %s", e, exc_info=True)
                app.state.mimo_supervisor = None
                from src.model_dispatch import set_agent_supervisor
                set_agent_supervisor(None)
                if str(os.environ.get("OPENCLANK_RECOVERY_MODE") or "").lower() not in {
                    "1", "true", "yes", "on"
                }:
                    raise
        await _startup_mimo()

    logger.info("Application startup complete")

def _reap_orphaned_children(grace_seconds: float = 2.0) -> None:
    """Last-resort SIGTERM→SIGKILL containment for unregistered direct children.

    Normal MCP and Open Clank agent children are closed by their registered owners before
    this runs. Linux exposes the direct-child inventory through ``/proc``;
    other platforms skip this defensive check explicitly.
    """
    import signal as _signal
    import time as _time

    if not sys.platform.startswith("linux"):
        logger.info("child reaper skipped: direct-child inventory unsupported on %s", sys.platform)
        return

    me = os.getpid()

    def _live_children() -> list[int]:
        pids = []
        try:
            entries = os.listdir("/proc")
        except OSError:
            return pids
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "r") as fh:
                    tail = fh.read().rsplit(")", 1)[1].split()
                state, ppid = tail[0], int(tail[1])
            except (OSError, ValueError, IndexError):
                continue
            if ppid == me and state != "Z":
                pids.append(int(entry))
        return pids

    victims = _live_children()
    if not victims:
        return
    logger.warning("reaping %d orphaned child process(es) at shutdown: %s", len(victims), victims)
    for pid in victims:
        try:
            os.kill(pid, _signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = _time.time() + grace_seconds
    while _time.time() < deadline and _live_children():
        _time.sleep(0.1)
    for pid in _live_children():
        try:
            os.kill(pid, _signal.SIGKILL)
        except ProcessLookupError:
            pass


def _is_orphaned_open_clank_agent_worker(
    cmd: str,
    ppid: int,
    me: int,
    parent_cmd: str | None,
) -> bool:
    """Return whether this is an orphaned bundled Open Clank agent worker.

    Match both the repository-owned binary and its ``acp`` command. Personal
    ``mimo serve`` processes on the same PC are outside Open Clank's lifecycle
    and must never be touched.
    """
    bundled_binary = str((Path(__file__).resolve().parent / "bin" / "mimo").resolve())
    argv = cmd.split()
    if len(argv) < 2 or argv[0] != bundled_binary or argv[1] != "acp":
        return False
    if ppid == me:
        return False
    if parent_cmd is not None and "app.py" in parent_cmd:
        return False
    return True


def _reap_orphaned_mimo_workers() -> None:
    """Startup sweep: stop bundled agent workers orphaned by a prior run.

    Sibling to the generation-dir sweep in ``MimoSupervisorPool.start()``. The
    shutdown reap (``_reap_orphaned_children``) only catches children still
    parented to this process; procs reparented to a subreaper when the previous
    run was SIGKILL'd/OOM'd survive restarts and leak ~50 MB each. Safe because
    this runs before the supervisor spawns its own children, and a concurrent
    app.py's direct children are spared by ``_is_orphaned_mimo_worker``.
    """
    import signal as _signal
    import time as _time

    if not sys.platform.startswith("linux"):
        return

    me = os.getpid()
    victims: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat") as fh:
                tail = fh.read().rsplit(")", 1)[1].split()
            state, ppid = tail[0], int(tail[1])
            if state == "Z":
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode(errors="replace")
        except (OSError, ValueError, IndexError):
            continue
        try:
            with open(f"/proc/{ppid}/cmdline", "rb") as fh:
                parent_cmd = fh.read().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            parent_cmd = None  # parent gone => reparented orphan
        if _is_orphaned_open_clank_agent_worker(cmd, ppid, me, parent_cmd):
            victims.append(pid)
    if not victims:
        return

    def _alive(p: int) -> bool:
        try:
            with open(f"/proc/{p}/stat") as fh:
                return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False

    logger.warning(
        "reaping %d orphaned Open Clank agent worker(s) at startup: %s",
        len(victims),
        victims,
    )
    for pid in victims:
        try:
            os.kill(pid, _signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = _time.time() + 6.0
    while _time.time() < deadline:
        if not any(_alive(p) for p in victims):
            break
        _time.sleep(0.2)
    for pid in victims:
        if _alive(pid):
            try:
                os.kill(pid, _signal.SIGKILL)
            except ProcessLookupError:
                pass


async def _shutdown_event():
    shutdown_started = time.monotonic()
    logger.info("Application shutting down...")

    async def _stop_copal():
        bridge = getattr(app.state, "copal_bridge", None)
        if bridge:
            await bridge.stop()

    async def _stop_mimo():
        supervisor = getattr(app.state, "mimo_supervisor", None)
        try:
            if supervisor:
                await supervisor.stop()
        finally:
            from src.model_dispatch import set_agent_supervisor
            set_agent_supervisor(None)

    async def _stop_history():
        supervisor = getattr(app.state, "history_supervisor", None)
        try:
            if supervisor:
                await supervisor.stop()
        finally:
            from src.openclank.files_service_client import set_history_binding_provider
            set_history_binding_provider(None)

    async def _stop_agent_runs():
        from src import agent_runs
        await agent_runs.shutdown()

    async def _cancel_startup_tasks():
        startup_tasks = list(getattr(app.state, "_startup_tasks", []))
        for task in startup_tasks:
            if not task.done():
                task.cancel()
        if startup_tasks:
            await asyncio.gather(*startup_tasks, return_exceptions=True)
        getattr(app.state, "_startup_tasks", []).clear()

    async def _cancel_upload_cleanup():
        if upload_cleanup_task:
            upload_cleanup_task.cancel()
            try:
                await upload_cleanup_task
            except asyncio.CancelledError:
                pass

    async def _stop_memory_provider():
        shutdown = getattr(memory_provider, "shutdown", None)
        if callable(shutdown):
            await shutdown()

    await _run_shutdown_phase("startup_tasks", _cancel_startup_tasks, timeout=2.0)
    await _run_shutdown_phase("agent_runs", _stop_agent_runs, timeout=2.0)
    await _run_shutdown_phase("task_scheduler", task_scheduler.stop, timeout=2.0)
    await _run_shutdown_phase("copal_bridge", _stop_copal, timeout=2.0)
    await _run_shutdown_phase("history_supervisor", _stop_history, timeout=12.0)
    await _run_shutdown_phase("mimo_supervisor", _stop_mimo, timeout=7.0)
    await _run_shutdown_phase("upload_cleanup", _cancel_upload_cleanup, timeout=2.0)
    await _run_shutdown_phase("webhooks", webhook_manager.close, timeout=2.0)
    await _run_shutdown_phase("memory_provider", _stop_memory_provider, timeout=5.0)
    await _run_shutdown_phase("mcp_servers", mcp_manager.disconnect_all, timeout=6.0)
    await _run_shutdown_phase(
        "defensive_child_reaper",
        lambda: asyncio.to_thread(_reap_orphaned_children),
        timeout=3.0,
    )
    logger.info(
        "Application shutdown complete (duration_s=%.3f)",
        time.monotonic() - shutdown_started,
    )


if __name__ == "__main__":
    import uvicorn

    bind_host = os.getenv("APP_BIND", "127.0.0.1")
    bind_port = int(os.getenv("APP_PORT", "7777"))

    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info", timeout_graceful_shutdown=10)

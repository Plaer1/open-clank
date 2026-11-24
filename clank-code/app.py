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
    """Force stable JS module and ZIP download MIME types across platforms.

    Some native Windows setups inherit stale/incorrect registry mappings for
    ``.js``/``.mjs``, which can make Starlette serve ES modules with a non-JS
    ``Content-Type`` and cause the UI to load but fail on click. Re-register the
    standard MIME types at startup so static assets are served consistently.
    Windows registry ZIP aliases must likewise not override the published
    curriculum download descriptors.
    """

    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("application/javascript", ".mjs")
    mimetypes.add_type("application/zip", ".zip")


register_static_mime_types()

# Windows: force HuggingFace/fastembed to COPY model files instead of symlinking.
# On a network-share/UNC data dir Windows can't follow HF's symlinks ([WinError
# 1463]), so the ONNX embedding model fails to load. huggingface_hub reads this
# at import time, so set it before anything pulls it in. (Mirrored in
# src/embeddings.py for non-server entrypoints.)
if os.name == "nt":
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from src.source_layout import load_source_environment

load_source_environment()

# Resolve the host-owned worker before consumers bind their process configuration.
from src.openclank.memery_builder import memery_worker_path
os.environ["OPEN_CLANK_MEMERY_WORKER"] = memery_worker_path()

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
    resolve_gallery_image_path,
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
    # Current resolution is a bounded compound operation with repeated native
    # authority/source fences, not a fixed-duration lookup. Keep those bounds
    # and explicit cancellation; do not impose the generic elapsed deadline.
    if normalized_method == "POST" and path == "/api/memory/code/resource":
        return True
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
                threshold = float(os.getenv("OPEN_CLANK_SLOW_REQUEST_LOG_SECONDS", "0.75") or "0.75")
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
from src.openclank.conversation_archive import configure_conversation_archive_owner_resolver, get_conversation_archive
configure_conversation_archive_owner_resolver(
    auth_manager.account_id, auth_manager.username_for_account_id,
)
def _owner_request_fenced(owner):
    if auth_manager.is_account_lifecycle_fenced(owner):
        return True
    factory = getattr(app.state, "memory_reset_coordinator", None)
    return bool(factory and factory().owner_is_fenced(owner))


_account_request_barrier = AccountRequestBarrier(_owner_request_fenced,
    absolute_fence=auth_manager.is_account_lifecycle_fenced)
app.state.account_request_barrier = _account_request_barrier
from src import agent_runs as _account_agent_runs
_account_agent_runs.configure_request_barrier(_account_request_barrier)
app.state.drain_account_owner_requests = _account_request_barrier.drain


async def _call_next_for_account_owner(request, call_next, owner):
    """Admit one owner request and retain it through any streaming body."""
    try:
        path = get_application_route_path(request.scope)
        interactive = not bool(getattr(request.state, "api_token", False) or getattr(request.state, "internal_tool_authenticated", False))
        recovery = interactive and request.method == "POST" and path == "/api/memory/nuke"
        bootstrap = request.method == "GET" and (path == "/" or path.startswith("/static/") or (
            not request.url.query and path in {"/api/auth/status", "/api/auth/policy",
                                               "/api/auth/features", "/api/auth/settings"}))
        allow_fenced = (recovery or bootstrap) and not auth_manager.is_account_lifecycle_fenced(owner)
        return await _account_request_barrier.track_response(
            owner,
            lambda: call_next(request),
            allow_fenced=allow_fenced,
        )
    except AccountRequestFenced:
        return JSONResponse(
            status_code=409,
            content={"error": "Account lifecycle operation in progress"},
        )


_DESKTOP_SHELL_BINDING = DesktopShellBinding.capture()

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


from src.openclank.request_auth import install_request_auth

install_request_auth(
    app,
    auth_manager=auth_manager,
    session_factory=SessionLocal,
    api_token_model=ApiToken,
    account_call_next=_call_next_for_account_owner,
    trusted_loopback=_is_trusted_loopback,
    desktop_ready_path=DESKTOP_SHELL_READY_PATH,
    session_cookie=SESSION_COOKIE,
)


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


@app.get("/docs", include_in_schema=False)
@app.get("/docs/", include_in_schema=False)
@app.get("/docs/index.html", include_in_schema=False)
@app.get("/static/docs/index.html", include_in_schema=False)
async def retired_official_guide():
    # Documentation lives in the existing Copal and Wiki handbook. Register
    # the retired static reader before the generic mount so it stays retired.
    raise HTTPException(status_code=404, detail="Not found")


app.mount("/static", _RevalidatingStatic(directory=STATIC_DIR), name="static")

# ========= GENERATED IMAGES =========
@app.get("/api/generated-image/{filename}")
async def serve_generated_image(filename: str, request: Request):
    """Serve only an active image with exact durable owner provenance."""
    from src.auth_helpers import effective_user
    from core.database import SessionLocal as _SL
    from src.openclank.files_image_store import FilesImageStore

    owner = gallery_owner_key(effective_user(request))
    if owner is None:
        raise HTTPException(status_code=404, detail="Image not found")

    # Prove owner provenance before resolving or touching the byte path.  A
    # missing/foreign row and any provenance-store failure deliberately share
    # the same response, and none of them are allowed to fall through to disk.
    if not FilesImageStore(session_factory=_SL).owns_locator(owner, filename):
        raise HTTPException(status_code=404, detail="Image not found")

    img_path = resolve_gallery_image_path(filename, require_exists=True)
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
# Canonical Memery SQLite personal-document RAG. Initialized lazily via
# get_rag_manager(); it does not require ChromaDB or another vector service.
from src.rag_singleton import get_rag_manager
rag_manager = get_rag_manager()
rag_available = rag_manager is not None
if rag_available:
    logger.info("Vector document RAG initialized")
else:
    logger.info("Memery document RAG not available at startup; routes will retry lazily")

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
canonical_memory_provider = components.get("canonical_memory_provider")
app.state.canonical_memory_provider = canonical_memory_provider
# Routes and authenticated agent tools share the current lifecycle owners.
session_manager.lifecycle_context = app.state
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
app.state.media_artifact_store = account_owner_lifecycle.artifact_store
auth_manager.configure_account_lifecycle_fence(
    account_owner_lifecycle.owner_has_active_operation
)
chat_processor    = components["chat_processor"]
research_handler  = components["research_handler"]
app.state.research_handler = research_handler
chat_handler      = components["chat_handler"]
model_discovery   = components["model_discovery"]
skills_manager    = components["skills_manager"]
from src.openclank.prepared_discovery import PreparedSkillsReader
app.state.prepared_discovery = PreparedSkillsReader(state=app.state, manager=skills_manager)

# One app-owned Memery worker serves Python, lifetools, and MiMo through this
# authenticated loopback boundary. The broker credential stays in this process
# and scoped lifetools descriptors; it is never exported host-wide.
_memory_broker_base = os.getenv(
    "OPEN_CLANK_INTERNAL_BASE_URL",
    f"http://127.0.0.1:{int(os.getenv('APP_PORT', '7777'))}",
).rstrip("/")
_memory_broker_url = f"{_memory_broker_base}/api/internal/memery/tool"
from src.openclank.acp_bridge import (
    configure_copal_broker,
    configure_memery_broker,
    memery_broker_token,
)
configure_memery_broker(_memory_broker_url, INTERNAL_TOOL_TOKEN)
app.state.memory_broker_url = _memory_broker_url
_copal_broker_url = f"{_memory_broker_base}/api/internal/copal/tool"
configure_copal_broker(_copal_broker_url, INTERNAL_TOOL_TOKEN)
app.state.copal_broker_url = _copal_broker_url
_browser_control_broker_url = f"{_memory_broker_base}/api/internal/browser-control/tool"
from src.openclank.browser_control_broker import configure_browser_control_broker
configure_browser_control_broker(_browser_control_broker_url, INTERNAL_TOOL_TOKEN)
app.state.browser_control_broker_url = _browser_control_broker_url

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
from routes.desktop_control_routes import setup_desktop_control_routes
app.include_router(setup_desktop_control_routes())
from routes.annotator_routes import setup_annotator_routes
app.include_router(setup_annotator_routes(
    source_root=Path(__file__).resolve().parent,
    configured_root=os.environ.get("OPENCLANK_ANNOTATOR_SOURCE_ROOT"),
))

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
session_router = setup_session_routes(
    session_manager,
    session_config,
    webhook_manager=webhook_manager,
    upload_handler=upload_handler,
)
app.include_router(session_router)
from routes.orchestration_routes import setup_orchestration_routes
orchestration_router = setup_orchestration_routes(session_manager)
app.include_router(orchestration_router)

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
    canonical_memory_provider=canonical_memory_provider,
)
memory_lifecycle = getattr(memory_router, "memory_lifecycle", None)
canonical_memory_lifecycle = None
if canonical_memory_provider is not None and canonical_memory_provider is not memory_provider:
    from services.memory.forget_coordinator import MemoryLifecycleCoordinator
    canonical_memory_lifecycle = MemoryLifecycleCoordinator(canonical_memory_provider, skills_manager)
memory_skill_forget = getattr(memory_router, "memory_skill_forget", None)
app.state.memory_reset_coordinator = memory_router.memory_reset_coordinator
app.include_router(memory_router)


def _s06_admitted_ready(state, supervisor):
    """Check installed source/reset/profile authority without starting a worker."""
    from core.database import SessionLocal
    from services.memory.nuke_coordinator import MemoryNukeCoordinator
    from src.openclank.chat_source_bridge import canonical_source_provider, origin_available, _require_reset_origin_indexes
    from src.openclank.memory_job_admission import SingleHostProfile, _conn
    profile = getattr(state, 's05_profile', None)
    source = canonical_source_provider(state)
    factory = getattr(state, 'memory_reset_coordinator', None)
    coordinator = factory() if callable(factory) else None
    if (type(profile) is not SingleHostProfile or source is None or profile.provider is not source
            or source is not memory_provider or type(coordinator) is not MemoryNukeCoordinator
            or coordinator.memory_provider is not source or getattr(supervisor, '_s05_foreground_runtime', None) is None
            or source._guarded_preparation.capabilities.get('admitted') is not True
            or source._guarded_preparation.capabilities.get('source_erase') is not True):
        return False
    profile.assert_current()
    from src.openclank.conversation_archive import archive_origin_profile_ready
    if (source.persisted_database_identity()['database_path'] != profile.path
            or not archive_origin_profile_ready(profile)):
        return False
    with SessionLocal() as db:
        if not origin_available(db): return False
        _require_reset_origin_indexes(db)
        from src.openclank.host_source_schema import require_current_connection
        require_current_connection(db.connection())
    with _conn(profile.path) as db:
        return source._s06_source_capability(db) and source._s06_admitted_capability(db) and source._s06_erase_capability(db)


def _live_memory_policy_capabilities(state):
    """Report current installed support; callers must still authorize each use."""
    from src.openclank.memory_policy import _inactive_capabilities
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.memery_database import GuardedStorePreparation
    from src.openclank.memory_job_admission import SingleHostProfile, ForegroundRuntime, _conn
    from src.task_scheduler import TaskScheduler
    result = _inactive_capabilities()
    try:
        profile = getattr(state, 's05_profile', None)
        provider = canonical_source_provider(state)
        supervisor = getattr(state, 'mimo_supervisor', None)
        scheduler = getattr(state, 'task_scheduler', None)
        runtime = getattr(supervisor, '_s05_foreground_runtime', None)
        if (state is not app.state or type(profile) is not SingleHostProfile or provider is None
                or provider is not memory_provider or profile.provider is not provider
                or getattr(supervisor, '_s05_profile', None) is not profile
                or type(runtime) is not ForegroundRuntime or runtime.profile is not profile
                or type(scheduler) is not TaskScheduler or scheduler._s05_registry_state is not state):
            return result
        profile.assert_current()
        preparation = getattr(provider, '_guarded_preparation', None)
        if type(preparation) is not GuardedStorePreparation or preparation.database_path != profile.path:
            return result
        preparation.assert_valid()
        from src.openclank.conversation_archive import archive_origin_profile_ready
        if not archive_origin_profile_ready(profile):
            return result
        with _conn(profile.path) as db:
            occupancy = db.execute('SELECT verified,coverage_generation,clock_generation FROM fm_v2_s05_occupancy WHERE singleton=1').fetchone()
        if not occupancy or tuple(occupancy) != (1, profile.coverage_generation, profile.clock_generation):
            return result
        controller = scheduler._s05_controller
        if (type(controller) is not tuple or len(controller) != 2 or controller[0] is not profile
                or type(controller[1]) is not dict or not controller[1]):
            return result
        result['consumers']['scheduler'] = True
        if (getattr(state, 's08_rules_callback', None) is _s08_rules_callback
                and getattr(state, 'install_policy_publication_callbacks', None) is _install_policy_publication_callbacks):
            if provider._host_s08_policy_ready('summary'):
                result['axes']['importance'] = ['rules']
                result['axes']['merge'] = ['rules']
            if provider._host_s08_policy_ready('connection'):
                result['axes']['connections'] = ['rules']
        if (getattr(state, 's06_admitted_callback', None) is _s06_admitted_callback
                and getattr(state, 's07_context_augment_callback', None) is _s07_context_augment_callback
                and getattr(state, 's07_context_delivery_callback', None) is _s07_context_delivery_callback
                and _s06_admitted_ready(state, supervisor)
                and callable(getattr(provider, '_s07_bounded_candidates', None))
                and callable(getattr(provider, '_s07_revalidate_candidates', None))):
            # The Native guarded opener compared the complete expected schema,
            # including curated FTS, before issuing this sealed preparation.
            # Its cookie and the same owned process must still be current.
            with _conn(profile.path) as db:
                context_ready = provider._s06_context_admitted_capability(db)
            if context_ready and provider._host_bounded_write_available():
                preparation.assert_valid()
                result['consumers']['automatic_context_recall'] = True
        if ('rank_learning_v1' in provider._native_memory_ops_support() and provider._host_bounded_write_available()):
            preparation.assert_valid()
            result['consumers']['rank_learning'] = True
        profile.assert_current()
        return result
    except Exception:
        return _inactive_capabilities()


async def _s07_nonblocking_guard(mapping, chat_id, deadline, operation):
    """Try the existing chat guard without retaining a late acquisition."""
    cm = mapping.chat_admission_lock(chat_id, blocking=False)
    acquire = asyncio.create_task(asyncio.to_thread(cm.__enter__))
    def release_late(task):
        if task.cancelled(): return
        try:
            task.result()
        except Exception:
            return
        cm.__exit__(None, None, None)
    try:
        done, _ = await asyncio.wait((acquire,), timeout=max(0,deadline-asyncio.get_running_loop().time()))
    except BaseException:
        if acquire.done(): release_late(acquire)
        else: acquire.add_done_callback(release_late)
        raise
    if acquire not in done:
        acquire.add_done_callback(release_late)
        return None
    try:
        guard = acquire.result()
    except (OSError, ValueError):
        return None
    try:
        mapping.validate_chat_guard(chat_id, guard)
        return await operation(guard)
    finally:
        cm.__exit__(None, None, None)


def _s07_recall_query(query, root_operation_id):
    """Remove only the one Host-generated header for this exact active root."""
    prefix = f"[turn_source_id={root_operation_id}]\n"
    return query[len(prefix):] if query.startswith(prefix) else query


async def _s07_context_augment_callback(bridge, identity, query, context, deadline, *, _boundary=None):
    """Prepare an ephemeral untrusted part from exact active-turn authority."""
    from src.openclank.memory_context_admission import Candidate, S07ContextEvidence, context_fragments, select_candidates
    from src.openclank.memory_job_admission import SingleHostProfile, TargetAuthority
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_policy import STORE
    from src.openclank.session_map import OwnerSessionMap
    from src.memery_external_sources import CurrentHostUserSource, SourceRef
    from src.memory_provider import RecallInvocation
    from src.prompt_security import untrusted_context_message
    from sqlalchemy import text as sql_text
    import math
    loop=asyncio.get_running_loop(); entered=loop.time()
    trace = logger.isEnabledFor(logging.DEBUG)
    timings = {}; phase = ['validate', entered]; outcome = ['rejected']
    def mark(name):
        if trace:
            now = loop.time(); timings[phase[0]] = round((now-phase[1])*1000, 3); phase[:] = [name, now]
    if (type(deadline) is not float or not math.isfinite(deadline) or deadline<=entered or type(identity) is not dict
            or set(identity)!={'sessionID','rootOperationID','actorID','executionRevision','innerTurnID'}
            or identity.get('actorID')!='main' or type(query) is not str or not query.strip() or len(query)>8192
            or type(context) is not str or len(context)>16000):
        if trace:
            logger.debug('S07_HOST outcome=%s phases_ms=%s', 'timeout' if type(deadline) is float and math.isfinite(deadline) and deadline<=entered else 'rejected', {'validate':round((loop.time()-entered)*1000,3)})
        return None
    deadline=min(deadline,entered+0.2)
    try:
        async with asyncio.timeout_at(deadline):
            mark('entry')
            if _boundary is None:
                original = await bridge.resolve_managed_recall_turn(identity)
            else:
                from src.openclank.acp_bridge import _S07RecallBoundary
                if type(_boundary) is not _S07RecallBoundary: return None
                await _boundary.assert_current(bridge, identity)
                original = _boundary.snapshot
            owner=original.get('owner'); chat_id=original.get('stableChatID')
            if type(owner) is not str or not owner or type(chat_id) is not str or not chat_id: return None
            from src.openclank.memery_preferences import owner_preferences
            recall_settings = (_boundary.recall_prefs if _boundary is not None and _boundary.recall_prefs is not None
                               else await asyncio.to_thread(owner_preferences, owner))
            if not recall_settings["memery_prompt_recall_enabled"]: return None
            supervisor=app.state.mimo_supervisor
            worker=supervisor.worker_for_owner(owner)
            if worker is None or worker.bridge is not bridge: return None
            profile=getattr(app.state,'s05_profile',None)
            provider=canonical_source_provider(app.state)
            if type(profile) is not SingleHostProfile or provider is None or profile.provider is not provider or memory_provider is not provider: return None
            account=auth_manager.account_id(owner)
            if (not account or original.get('ownerAccountID') != account
                    or auth_manager.username_for_account_id(account)!=owner): return None
            pool=supervisor.owner_lifecycle_pool(); authority=TargetAuthority(auth_manager=auth_manager,supervisor=pool)
            mark('target')
            target=await asyncio.to_thread(authority.resolve,account)
            if target.memory_owner!=provider._scope(owner).owner or target.memory_workspace!=provider._workspace_id: return None
            mapping=OwnerSessionMap(pool._runtime_home(owner)/'session-map.json',owner)
            verifier=CurrentHostUserSource(owner=owner,auth_manager=auth_manager,session_factory=SessionLocal,
                archive=get_conversation_archive(),workspace_repository=getattr(app.state,'files_policy_repository',None),policy_store=STORE,guarded_preparation=getattr(getattr(getattr(app.state,'s05_profile',None),'provider',None),'_guarded_preparation',None))
            def read_ref():
                with SessionLocal() as db:
                    row=db.execute(sql_text("SELECT m.source_body_revision,m.source_body_hash,s.workspace_id FROM chat_messages m JOIN sessions s ON s.id=m.session_id WHERE m.id=:message AND m.session_id=:chat AND m.role='user'"),{'message':identity['rootOperationID'],'chat':chat_id}).first()
                if not row or row[0]!=1 or type(row[1]) is not str or (row[2] if row[2] is not None else '')!=original.get('authorityWorkspaceID'): return None
                return SourceRef(2 if row[2] is None else 1,'host_user_body',account,chat_id,'main',identity['rootOperationID'],'body',row[0],row[1],row[2],None)
            mark('source_ref')
            source=await asyncio.to_thread(read_ref)
            if type(source) is not SourceRef: return None
            async def before_query_authority():
                if _boundary is None:
                    return await bridge.resolve_managed_recall_turn(identity) == original
                return await _boundary.assert_current_policy(bridge, identity)
            async def after_validation_authority():
                if _boundary is None:
                    return await bridge.resolve_managed_recall_turn(identity) == original
                # The gateway owns the final fresh policy admission before disclosure.
                return await _boundary.assert_current(bridge, identity)
            async def check_source(guard, check_name, authority_check):
                mark(check_name + '_profile')
                mapping.validate_chat_guard(chat_id,guard)
                await asyncio.to_thread(profile.assert_current)
                mark(check_name + '_policy')
                if not await authority_check(): return False
                # These reads are synchronous and independent of each other;
                # join their actual worker before releasing the chat guard.
                def current_source():
                    def measured(name, action):
                        if not trace: return action()
                        started = loop.time()
                        try: return action()
                        finally: timings[check_name + '_' + name] = round((loop.time()-started)*1000, 3)
                    return (measured('account', lambda: auth_manager.account_id(owner)==account and auth_manager.username_for_account_id(account)==owner)
                        and measured('fence', lambda: not _owner_request_fenced(owner))
                        and measured('target', lambda: authority.resolve(account)==target)
                        and measured('source', lambda: verifier.verify_feedback_origin(source).get('state')=='available'))
                from src.openclank.memory_job_admission import _completed_turn_thread
                mark(check_name + '_grouped')
                source_current, cancelled = await _completed_turn_thread(current_source)
                if cancelled: raise asyncio.CancelledError
                if not source_current: return False
                if _boundary is None:
                    if await bridge.resolve_managed_recall_turn(identity) != original: return False
                else:
                    await _boundary.assert_current(bridge, identity)
                mapping.validate_chat_guard(chat_id,guard)
                return True
            async def select_under_guard(guard):
                mark('pre_check')
                if not await check_source(guard, 'pre', before_query_authority): return None
                recall_query = _s07_recall_query(query, identity['rootOperationID'])
                invocation=RecallInvocation(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'],identity['innerTurnID'],secrets.token_urlsafe(24))
                mark('query')
                from src.openclank.memory_context_admission import pop_fts_ordinal, s07_features
                found=await provider._s07_bounded_candidates(owner=target.memory_owner,workspace_id=target.memory_workspace,invocation=invocation,query=recall_query,deadline=deadline)
                if found is None: return None
                result,refs=found; candidates=[]; identities={}; ordinals={}
                for hit,ref in zip(result['results'],refs):
                    ordinal=pop_fts_ordinal(hit)
                    record=hit.get('record') if type(hit) is dict else None
                    if type(record) is not dict or type(record.get('id')) is not str or type(record.get('content')) is not str or len(record['content'])>4096: continue
                    key=(ref.block_id,ref.revision,ref.value_sha256)
                    if key in identities: return None
                    identities[key]=record['id']; ordinals[record['id']]=ordinal; candidates.append(Candidate(ref,record['content']))
                mark('select')
                selection=await asyncio.to_thread(select_candidates,owner=target.memory_owner,workspace_id=target.memory_workspace,project_id=None,
                    privacy='normal',query=recall_query,context=context_fragments(context),candidates=tuple(candidates),
                    limit=recall_settings["memery_prompt_recall_limit"], char_budget=recall_settings["memery_prompt_recall_chars"])
                if not selection.candidates:
                    outcome[0] = 'empty'
                    return 'empty'
                # Rank only the already-selected, paired references. Keep the
                # original FTS ordinals and the caller's one total deadline.
                selection=await provider._rank_s07_selected(selection,identities,owner=target.memory_owner,workspace=target.memory_workspace,deadline=deadline)
                selected=tuple((identities[(c.revision.block_id,c.revision.revision,c.revision.value_sha256)],c.revision) for c in selection.candidates)
                mark('revalidate')
                valid=await provider._s07_revalidate_candidates(owner=target.memory_owner,workspace_id=target.memory_workspace,invocation=invocation,selected=selected,deadline=deadline)
                mark('final_check')
                if not valid or not await check_source(guard, 'final', after_validation_authority): return None
                if loop.time()>=deadline:
                    outcome[0] = 'timeout'
                    return None
                body='\n'.join(f'- [Memery {record_id}] {candidate.text}' for (record_id, _), candidate in zip(selected, selection.candidates))
                text=untrusted_context_message('saved memory: relevant context',body)['content']
                if loop.time()>=deadline:
                    outcome[0] = 'timeout'
                    return None
                outcome[0] = 'ready'
                return text,S07ContextEvidence(owner,account,source,target,invocation,selected,s07_features(ordinals,selected))
            async with _account_request_barrier.admitted(owner):
                return await _s07_nonblocking_guard(mapping,chat_id,deadline,select_under_guard)
    except asyncio.CancelledError:
        outcome[0] = 'timeout' if loop.time() >= deadline else 'cancelled'
        raise
    except Exception as exc:
        outcome[0] = ('timeout' if isinstance(exc, TimeoutError) else
                      type(exc).__name__ if type(exc).__name__ in {'ValueError','PermissionError','RuntimeError','OSError'} else 'exception')
        return None
    finally:
        if trace:
            mark('done')
            logger.debug('S07_HOST outcome=%s phases_ms=%s', outcome[0], timings)


app.state.s07_context_augment_callback = _s07_context_augment_callback


class _StandingGuidanceEvidence:
    """Turn-local host evidence; never supplied by an engine or browser."""
    def __init__(self, identity, original, account, target, refs, source, items):
        self.identity, self.original, self.account, self.target = identity, original, account, target
        self.refs, self.source, self.items = refs, source, items


async def _s10_guidance_current(bridge, identity, deadline, evidence=None):
    from src.openclank.memory_job_admission import SingleHostProfile, TargetAuthority
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_policy import STORE
    from src.openclank.memory_policy_projection import policy_coordinator
    from src.openclank.session_map import OwnerSessionMap
    from src.memery_external_sources import CurrentHostUserSource, SourceRef
    from sqlalchemy import text as sql_text
    original = await bridge._resolve_standing_guidance_turn(identity)
    owner, chat = original.get('owner'), original.get('stableChatID')
    account = auth_manager.account_id(owner) if owner else None
    supervisor = app.state.mimo_supervisor; pool = supervisor.owner_lifecycle_pool()
    worker = pool.worker_for_owner(owner) if owner else None
    key = pool._key(owner) if owner else None; worker_state = pool._states.get(key) if key else None
    epoch = pool._owner_lifecycle_epoch(key) if key else None
    process = getattr(worker,'_proc',None); holder = getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)
    generation = getattr(worker,'installed_generation',None)
    profile = getattr(app.state, 's05_profile', None); provider = canonical_source_provider(app.state)
    coordinator = policy_coordinator()
    if (not account or original.get('ownerAccountID') != account or auth_manager.username_for_account_id(account) != owner
        or worker is None or worker._bridge is not bridge or process is None or worker_state is None
        or worker_state.active is not worker or worker_state.status != 'ready' or worker_state.generation != generation
        or type(generation) is not int or not holder or holder != bridge._managed_provider_generation or not worker.is_alive()
        or type(profile) is not SingleHostProfile
        or profile.provider is not provider or provider is not memory_provider or coordinator is None
        or coordinator.projection is not getattr(app.state, 'memory_policy_projection', None)):
        raise ValueError('guidance authority unavailable')
    authority = TargetAuthority(auth_manager=auth_manager, supervisor=pool)
    target = await asyncio.to_thread(authority.resolve, account)
    if target.memory_owner != provider._scope(owner).owner or target.memory_workspace != provider._workspace_id:
        raise ValueError('guidance target changed')
    mapping = OwnerSessionMap(pool._runtime_home(owner) / 'session-map.json', owner)
    verifier = CurrentHostUserSource(owner=owner, auth_manager=auth_manager, session_factory=SessionLocal,
        archive=get_conversation_archive(), workspace_repository=getattr(app.state,'files_policy_repository',None), policy_store=STORE, guarded_preparation=getattr(getattr(getattr(app.state,'s05_profile',None),'provider',None),'_guarded_preparation',None))
    def source_read():
        with SessionLocal() as db:
            row = db.execute(sql_text("SELECT m.source_body_revision,m.source_body_hash,s.workspace_id,i.origin_json FROM chat_messages m JOIN sessions s ON s.id=m.session_id JOIN host_source_delivery_intent i ON i.chat_id=m.session_id AND i.message_id=m.id AND i.owner=:account AND i.part_id='body' AND i.revision=1 WHERE m.id=:message AND m.session_id=:chat AND m.role='user'"), {'account':account,'message':identity['rootOperationID'],'chat':chat}).first()
        if not row or row[0] != 1 or type(row[1]) is not str or (row[2] if row[2] is not None else '') != original.get('authorityWorkspaceID'): raise ValueError('guidance source unavailable')
        origin = json.loads(row[3]); choices = json.loads(origin['choices_json'])
        if any(choices.get(k) is not False for k in ('incognito','noMemory','compareMode')): raise ValueError('guidance privacy unavailable')
        return SourceRef(2 if row[2] is None else 1,'host_user_body',account,chat,'main',identity['rootOperationID'],'body',row[0],row[1],row[2],None)
    source = await asyncio.to_thread(source_read)
    async def check(guard):
        mapping.validate_chat_guard(chat, guard); await asyncio.to_thread(profile.assert_current)
        pool._assert_owner_lifecycle(key,epoch)
        privileges = auth_manager.get_privileges(owner); allowed = privileges.get('can_manage_memory') if type(privileges) is dict else False
        if type(allowed) is not bool or not allowed: return None
        policy = await asyncio.to_thread(STORE.effective_snapshot,owner,read_privilege=allowed,write_privilege=allowed,incognito=False,no_memory=False,compare_mode=False)
        context = bridge._session_context.get(identity['sessionID'], {})
        if (policy.get('eligibility_state') != 'available' or policy.get('read_allowed') is not True
            or context.get('memory_read_allowed') is not True or context.get('incognito') or context.get('auxiliary')
            or await bridge._resolve_standing_guidance_turn(identity) != original or auth_manager.account_id(owner) != account
            or auth_manager.username_for_account_id(account) != owner or await asyncio.to_thread(_owner_request_fenced,owner)
            or pool._states.get(key) is not worker_state or worker_state.active is not worker or worker_state.status != 'ready'
            or worker_state.generation != generation or pool.worker_for_owner(owner) is not worker or not worker.is_alive()
            or worker._proc is not process or worker._bridge is not bridge
            or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) != holder or bridge._managed_provider_generation != holder
            or await asyncio.to_thread(authority.resolve,account) != target
            or (await asyncio.to_thread(verifier.verify_feedback_origin,source)).get('state') != 'available'):
            return None
        refs = await asyncio.to_thread(coordinator.checked_refs,owner,account,target.memory_workspace)
        mapping.validate_chat_guard(chat,guard)
        return refs
    refs = await _s07_nonblocking_guard(mapping,chat,deadline,check)
    if not refs: raise ValueError('guidance policy unavailable')
    if evidence is not None and (evidence.original != original or evidence.account != account or evidence.target != target
            or evidence.refs != refs or evidence.source != source or evidence.identity != identity):
        raise ValueError('guidance authority changed')
    return owner, account, target, refs, source, provider, mapping, check, original


async def _s10_guidance_select_callback(bridge, identity, deadline):
    try:
        original = await bridge._resolve_standing_guidance_turn(identity)
        async with _account_request_barrier.admitted(original['owner']):
            owner, account, target, refs, source, provider, mapping, check, original = await _s10_guidance_current(bridge,identity,deadline)
            result = await provider._host_standing_guidance_select(owner=target.memory_owner,workspace_id=target.memory_workspace,
                account_id=account,policy_projection=refs,deadline=deadline)
            items = result['items']
            if not items: return None
            if await _s07_nonblocking_guard(mapping,original['stableChatID'],deadline,check) != refs: return None
            from src.memory_digest import render_standing_guidance
            text = render_standing_guidance([item['text'] for item in items])
            return text, _StandingGuidanceEvidence(dict(identity),original,account,target,refs,source,items), result['coverage']
    except Exception:
        return None


async def _s10_guidance_delivery_callback(bridge, identity, evidence, proof):
    if type(evidence) is not _StandingGuidanceEvidence: return False
    try:
        deadline = asyncio.get_running_loop().time() + 0.2
        async with asyncio.timeout_at(deadline):
            async with _account_request_barrier.admitted(evidence.original['owner']):
                owner, account, target, refs, source, provider, mapping, check, original = await _s10_guidance_current(bridge,identity,deadline,evidence)
                result = await provider._host_standing_guidance_revalidate(owner=target.memory_owner,workspace_id=target.memory_workspace,
                    account_id=account,policy_projection=refs,items=evidence.items,deadline=deadline)
                return result == {'version':1,'state':'current'} and await _s07_nonblocking_guard(mapping,original['stableChatID'],deadline,check) == refs
    except Exception:
        return False


app.state.s10_guidance_select_callback = _s10_guidance_select_callback
app.state.s10_guidance_delivery_callback = _s10_guidance_delivery_callback


def _memory_requirement_store_present() -> bool:
    """Standing requirements exist only on a guarded memory store profile (env default: legacy)."""
    return os.environ.get('OPENCLANK_MEMORY_STORE_PROFILE', 'legacy-v16') in ('guarded-v17', 'guarded-v18')


async def _publish_requirement_answer(owner: str) -> dict:
    """Current publish_file requirement for one authenticated owner; raises when it cannot be known."""
    if not _memory_requirement_store_present():
        return {'version': 1, 'store': 'none', 'deny_public': False, 'receipt': ''}
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.openclank.memory_job_admission import TargetAuthority
    from src.openclank.memory_policy_projection import policy_coordinator
    account = auth_manager.account_id(owner) if owner else None
    if not account or auth_manager.username_for_account_id(account) != owner or _owner_request_fenced(owner):
        raise ValueError('requirement owner unavailable')
    provider = canonical_source_provider(app.state); coordinator = policy_coordinator()
    if provider is None or provider is not memory_provider or coordinator is None:
        raise ValueError('requirement store unavailable')
    pool = app.state.mimo_supervisor.owner_lifecycle_pool()
    target = await asyncio.to_thread(TargetAuthority(auth_manager=auth_manager, supervisor=pool).resolve, account)
    if target.memory_owner != provider._scope(owner).owner or target.memory_workspace != provider._workspace_id:
        raise ValueError('requirement target changed')
    refs = await asyncio.to_thread(coordinator.checked_refs, owner, account, target.memory_workspace)
    result = await provider._host_standing_guidance_select(owner=target.memory_owner, workspace_id=target.memory_workspace,
        account_id=account, policy_projection=refs, deadline=asyncio.get_running_loop().time() + 1.0)
    deny = {'version': 1, 'effect': 'publish_file', 'deny': {'audience': 'public'}}
    for item in result['items']:
        if item.get('requirement') == deny:
            return {'version': 1, 'deny_public': True, 'receipt': item['receipt_id'].removeprefix('cs_s10_purpose_')[:12]}
    return {'version': 1, 'deny_public': False, 'receipt': ''}


async def _publish_requirement_inprocess(owner: str) -> dict:
    """In-process (Host) publish_file path: no turn identity exists here, so check by owner."""
    async with _account_request_barrier.admitted(owner):
        return await _publish_requirement_answer(owner)


import src.tool_execution as _tool_execution_module
_tool_execution_module.HOST_PUBLISH_REQUIREMENT_CHECK = _publish_requirement_inprocess

async def _queued_source_fence_callback(bridge, source_data, preimage, mutation):
    """Authorize only the exact queued origin's limited pre-mutation fence."""
    from dataclasses import fields
    from src.openclank.queued_source import QueuedSourceRef
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.session_map import OwnerSessionMap
    from src.openclank.memory_job_admission import TargetAuthority
    from src.openclank.memory_job_admission import SingleHostProfile
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.memery_external_sources import CurrentQueuedSource
    import hashlib, json
    try:
        if (mutation not in ('part_delete','part_edit','holder_retire') or type(source_data) is not dict
            or set(source_data)!={field.name for field in fields(QueuedSourceRef)}): return None
        source=QueuedSourceRef(**source_data)
        supervisor=app.state.mimo_supervisor; pool=supervisor.owner_lifecycle_pool()
        owner=getattr(bridge,'_owner',None)
        reset_lease=pool.queued_reset_lease_for_source(owner,bridge,source) if mutation=='holder_retire' and owner else None
        if reset_lease is None:
            owner=auth_manager.username_for_account_id(source.account_id)
            if not isinstance(owner,str) or not owner or auth_manager.account_id(owner)!=source.account_id: return None
        if reset_lease is None and _owner_request_fenced(owner): return None
        source_hash=hashlib.sha256(json.dumps(['queued.producer-source.v1',source.account_id,source.chat_id,source.native_session_id,
            source.request_id,source.message_id,source.part_id],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        def current_worker():
            if reset_lease is not None:
                return pool.queued_retirement_worker(owner,bridge,claim=reset_lease.claim,source_hash=source_hash)
            if mutation=='holder_retire': return pool.queued_retirement_worker(owner,bridge)
            active=supervisor.worker_for_owner(owner)
            return active if active is not None and getattr(active,'_bridge',None) is bridge else None
        worker=current_worker()
        provider=canonical_source_provider(app.state); profile=getattr(app.state,'s05_profile',None)
        if worker is None or getattr(worker,'_bridge',None) is not bridge or type(profile) is not SingleHostProfile or profile.provider is not provider:
            return None
        if reset_lease is None: profile.assert_current()
        mapping=OwnerSessionMap(pool._runtime_home(owner)/'session-map.json',owner)
        authority=reset_lease.authority if reset_lease is not None else TargetAuthority(auth_manager=auth_manager,supervisor=pool)
        def resolve():
            return authority.resolve(source.account_id) if reset_lease is None else authority._resolve_queued_reset(source.account_id,lease=reset_lease,claim=reset_lease.claim)
        def evidence_for():
            return pool.queued_retirement_evidence(owner,bridge,claim=reset_lease.claim,source_hash=source_hash) if reset_lease is not None else pool.queued_retirement_evidence(owner,bridge)
        def permitted(): return reset_lease is not None or not _owner_request_fenced(owner)
        verifier=CurrentQueuedSource(owner=owner,auth_manager=auth_manager,session_factory=SessionLocal,
            archive=get_conversation_archive(),workspace_repository=getattr(app.state,'files_policy_repository',None),
            mapping=mapping,target_authority=authority,provider=provider,bridge=bridge,worker=worker)
        deadline=asyncio.get_running_loop().time()+3.0
        async def issue(guard):
            if reset_lease is not None: await pool.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim,source_hash=source_hash)
            if not permitted() or current_worker() is not worker: return None
            evidence=evidence_for() if mutation=='holder_retire' else None
            if mutation=='holder_retire' and evidence is None: return None
            if reset_lease is None: profile.assert_current()
            def producer_epoch():
                if current_worker() is not worker or not worker.is_alive() or not permitted():return None
                if reset_lease is not None: return reset_lease.epoch
                return pool._owner_lifecycle_epoch(owner)
            result=await verifier.fence_for_mutation(source,guard=guard,preimage=preimage,producer_epoch=producer_epoch,reset_lease=reset_lease)
            if reset_lease is not None: await pool.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim,source_hash=source_hash)
            if reset_lease is None: profile.assert_current()
            if (not permitted() or current_worker() is not worker or canonical_source_provider(app.state) is not provider
                or (mutation=='holder_retire' and evidence_for()!=evidence)): return None
            if mutation=='holder_retire':
                target=resolve()
                def retirement_current():
                    try:
                        if reset_lease is not None: pool.assert_queued_reset_lease(reset_lease,claim=reset_lease.claim,source_hash=source_hash)
                        mapping.validate_chat_guard(source.chat_id,guard)
                        profile.assert_current()
                        return (permitted() and current_worker() is worker
                            and evidence_for()==evidence
                            and resolve()==target and canonical_source_provider(app.state) is provider
                            and auth_manager.account_id(owner)==source.account_id and auth_manager.username_for_account_id(source.account_id)==owner)
                    except Exception: return False
                task=asyncio.create_task(asyncio.to_thread(verifier._archive.record_queued_retirement,source,result['fence'],
                    holder_sha256=evidence[0],generation=evidence[1],epoch=evidence[2],current=retirement_current))
                cancelled=False
                while not task.done():
                    try: await asyncio.shield(task)
                    except asyncio.CancelledError: cancelled=True
                staged_fence=task.result()
                if cancelled: raise asyncio.CancelledError
                if type(staged_fence) is not dict: return None
                result['fence']=staged_fence
                if reset_lease is not None:
                    await pool.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim,source_hash=source_hash)
                    if current_worker() is not worker: return None
                elif not retirement_current(): return None
            return {'state':'fenced','source':result['source'],'preimage':result['preimage'],'fence':result['fence']}
        async with asyncio.timeout_at(deadline):
            if reset_lease is not None:
                return await _s07_nonblocking_guard(mapping,source.chat_id,deadline,issue)
            async with _account_request_barrier.admitted(owner):
                return await _s07_nonblocking_guard(mapping,source.chat_id,deadline,issue)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


app.state.queued_source_fence_callback = _queued_source_fence_callback


async def _queued_marker_scan(worker, owner, account, session, *, marker_state, current):
    """Exhaust the exact bounded marker inventory without changing it."""
    from dataclasses import fields
    from src.openclank.queued_source import QueuedSourceRef
    from src.openclank.managed_protocol import validate_engine_method_request, validate_engine_method_result
    after=None; markers=[]; previous=None
    while True:
        if not await current(): raise ValueError('queued marker authority changed')
        method='_openclank/session/v1/queued-source/markers'
        request=validate_engine_method_request(method,{'owner':owner,'sessionID':session,'after':after,'limit':128})
        async with asyncio.timeout(4): value=await worker.managed_engine_call(method,request)
        value=validate_engine_method_result(method,value)
        if not await current() or value.get('state')!='ready': raise ValueError('queued markers unavailable')
        for item in value['items']:
            raw=item.get('source')
            state=item.get('state')
            if ((state not in ('enrolled','fenced') if marker_state=='mixed' else state!=marker_state) or type(raw) is not dict
                or set(raw)!={f.name for f in fields(QueuedSourceRef)}
                or (state=='enrolled' and type(item.get('preimage')) is not dict)
                or (state=='fenced' and type(item.get('fence')) is not dict)):
                raise ValueError('queued marker unavailable')
            source=QueuedSourceRef(**raw); key=(source.chat_id,source.native_session_id,source.request_id)
            if source.account_id!=account or (previous is not None and key<=previous): raise ValueError('queued marker changed')
            previous=key;markers.append((source,item))
            if len(markers)>128: raise ValueError('queued markers too large')
        after=value['nextAfter']
        if after is None: return markers
        if not value['items'] or tuple(after)!=previous: raise ValueError('queued marker cursor unavailable')


async def _queued_reset_preview(*, owner, account_id, operation_id, expected, provider):
    """Point-in-time verified-only preview; never a retirement authorization."""
    import hashlib, json
    from contextlib import AsyncExitStack
    from dataclasses import fields
    from src import agent_runs
    from src.openclank.account_request_barrier import _current_admission
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_job_admission import SingleHostProfile, ForegroundRuntime, TargetAuthority, _conn
    from src.openclank.session_map import OwnerSessionMap
    from src.openclank.queued_source import QueuedSourceRef
    from src.openclank.managed_protocol import validate_engine_method_request, validate_engine_method_result
    from src.memery_external_sources import CurrentQueuedSource
    pool=app.state.mimo_supervisor.owner_lifecycle_pool()
    profile=getattr(app.state,'s05_profile',None); archive=get_conversation_archive()
    barrier=getattr(app.state,'account_request_barrier',None)
    supervisor=app.state.mimo_supervisor
    if (type(profile) is not SingleHostProfile or profile.provider is not provider or barrier is None
        or getattr(supervisor,'_s05_profile',None) is not profile
        or type(getattr(supervisor,'_s05_foreground_runtime',None)) is not ForegroundRuntime
        or supervisor._s05_foreground_runtime.profile is not profile):
        raise ValueError('queued preview unavailable')
    profile.assert_current()
    authority=TargetAuthority(auth_manager=auth_manager,supervisor=pool)
    target=authority.resolve(account_id)
    if not await _refresh_queued_reset_capability(app.state,profile,provider,owner,account_id,target,authority):
        raise ValueError('queued reset live capability unavailable')
    async with pool._owner_lock(owner):
        state=pool._states.get(owner); worker=state.active if state is not None else None
        if (worker is None or pool._workers.get(owner) is not worker or not worker.is_alive()
            or set(state.in_flight)!={worker} or state.in_flight.get(worker)!=0
            or getattr(worker,'_private_runtime',False) or owner in pool._owner_lifecycle_blocked
            or any(o==owner and not task.done() for task,o in pool._background_task_owners.items())
            or any(key[0]==owner for key in pool._history_leases)):
            raise ValueError('queued preview worker unavailable')
        bridge=getattr(worker,'_bridge',None); holder=getattr(bridge,'_managed_provider_generation',None)
        generation=getattr(worker,'installed_generation',None); epoch=pool._owner_lifecycle_epoch(owner)
        if (bridge is None or not isinstance(holder,str) or not holder or type(generation) is not int
            or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder):
            raise ValueError('queued preview holder unavailable')
    async def current():
        profile.assert_current()
        if (canonical_source_provider(app.state) is not provider or authority.resolve(account_id)!=target
            or getattr(supervisor,'_s05_profile',None) is not profile
            or type(getattr(supervisor,'_s05_foreground_runtime',None)) is not ForegroundRuntime
            or supervisor._s05_foreground_runtime.profile is not profile
            or auth_manager.account_id(owner)!=account_id or auth_manager.username_for_account_id(account_id)!=owner
            or pool._states.get(owner) is not state or state.active is not worker or pool._workers.get(owner) is not worker
            or set(state.in_flight)!={worker} or state.in_flight.get(worker)!=0 or not worker.is_alive()
            or pool._owner_lifecycle_epoch(owner)!=epoch or owner in pool._owner_lifecycle_blocked
            or getattr(worker,'_bridge',None) is not bridge or getattr(bridge,'_managed_provider_generation',None)!=holder
            or getattr(worker,'installed_generation',None)!=generation
            or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
            or any(o==owner and not t.done() for t,o in pool._background_task_owners.items())
            or any(k[0]==owner for k in pool._history_leases)):
            raise ValueError('queued preview changed')
        async with bridge._managed_execution_lock:
            if (bridge._turns or bridge._managed_actor_turns or bridge._managed_actor_executions
                or bridge._prepared_managed_handoffs or bridge._managed_logging_uncertain):
                raise ValueError('queued preview execution unavailable')
        if any(o==owner and not t.done() for t,o in agent_runs._DRAIN_TASK_OWNERS.items()) or any(
            r.owner==owner and (r.status=='running' or r.cleanup_unresolved) for r in agent_runs._RUNS.values()):
            raise ValueError('queued preview drain unavailable')
        async with barrier._lock:
            admitted=_current_admission.get()
            if any(a is not admitted and not a.released for a in barrier._active.get(owner,())):
                raise ValueError('queued preview admission unavailable')
        def occupancy():
            profile.assert_current()
            with _conn(profile.path) as db:
                row=db.execute('SELECT verified,coverage_generation,clock_generation,foreground_epoch FROM fm_v2_s05_occupancy WHERE singleton=1').fetchone()
                if (not row or tuple(row[:3])!=(1,profile.coverage_generation,profile.clock_generation)
                    or db.execute("SELECT 1 FROM fm_v2_s05_foreground WHERE state IN ('active','unknown') LIMIT 1").fetchone()
                    or db.execute("SELECT 1 FROM fm_v2_s05_reservations WHERE state IN ('reserved','dispatched','uncertain') LIMIT 1").fetchone()):
                    raise ValueError('queued preview occupancy unavailable')
                return row[3]
        return await asyncio.wait_for(asyncio.to_thread(occupancy), timeout=4.0)
    foreground=await current()
    def authority_check():
        profile.assert_current()
        if (auth_manager.account_id(owner)!=account_id or auth_manager.username_for_account_id(account_id)!=owner
            or canonical_source_provider(app.state) is not provider or authority.resolve(account_id)!=target):
            raise ValueError('queued preview authority changed')
    payload=await asyncio.to_thread(archive._queued_reset_producer_inventory_read,account_id=account_id,
        operation_id=operation_id,expected=expected,check=authority_check)
    data=json.loads(payload)
    producer={'version':1,'holderSHA256':hashlib.sha256(holder.encode()).hexdigest(),'generation':generation,'ownerEpoch':epoch}
    if (data['producers']!=[producer] or not 1<=len(data['sources'])<=128
        or any(row['association']!='verified' or row['producer']!=producer for row in data['sources'])):
        raise ValueError('queued preview producer unavailable')
    sessions=tuple(bridge._session_context)
    if not sessions: raise ValueError('queued preview session unavailable')
    session=sessions[0]
    async def scan_current(): return await current()==foreground
    markers=[(source,item['preimage']) for source,item in await _queued_marker_scan(worker,owner,account_id,session,marker_state='enrolled',current=scan_current)]
    mapping=OwnerSessionMap(pool._runtime_home(owner)/'session-map.json',owner)
    verifier=CurrentQueuedSource(owner=owner,auth_manager=auth_manager,session_factory=SessionLocal,archive=archive,
        workspace_repository=getattr(app.state,'files_policy_repository',None),mapping=mapping,
        target_authority=authority,provider=provider,bridge=bridge,worker=worker)
    async with AsyncExitStack() as stack:
        guards={}
        for chat in sorted({source.chat_id for source,_ in markers}):
            guard=await stack.enter_async_context(mapping.async_chat_admission_lock(chat,blocking=False,existing_only=True))
            if guard is None: raise ValueError('queued preview guard unavailable')
            if await current()!=foreground: raise ValueError('queued preview changed')
            guards[chat]=guard
        checks=[{'source':source,'preimage':preimage,'guard':guards[source.chat_id],'verifier':verifier} for source,preimage in markers]
        def check():
            authority_check()
            if archive.queued_reset_inventory(account_id,operation_id)!=expected: raise ValueError('queued inventory changed')
        if await current()!=foreground: raise ValueError('queued preview changed')
        result=await provider._host_queued_origin_preflight_read(checks,owner=owner,account_id=account_id,
            payload_json=payload,target_authority=authority,check_authority=check)
        if await current()!=foreground: raise ValueError('queued preview changed')
    fingerprint=hashlib.sha256(json.dumps([expected,hashlib.sha256(payload.encode()).hexdigest(),target.fingerprint,
        producer,foreground,result['fingerprint']],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'version':1,'fingerprint':fingerprint}

app.state.queued_reset_preview = _queued_reset_preview


async def _queued_reset_retirement(lease):
    """Close one claim-bound producer; never enables the reset consumer."""
    import hashlib, json
    from contextlib import AsyncExitStack
    from dataclasses import asdict
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.session_map import OwnerSessionMap
    from src.openclank.managed_protocol import validate_engine_method_request, validate_engine_method_result
    from src.memery_external_sources import CurrentQueuedSource
    pool=app.state.mimo_supervisor.owner_lifecycle_pool()
    owner=lease.owner;account=lease.account_id;worker=lease.worker;bridge=lease.bridge
    provider=lease.provider;authority=lease.authority;archive=get_conversation_archive()
    async def current():
        try:
            from src.openclank.mimo_supervisor import _queued_owned_thread
            await _queued_owned_thread(provider._assert_reset_retirement_ready,lease.reset_transport_witness)
            await pool.validate_queued_reset_lease(lease,claim=lease.claim)
            return True
        except Exception: return False
    if not await current(): raise ValueError('queued reset authority unavailable')
    if lease.pending is not None:
        from src.openclank.acp_client import RPCError
        try: pool._settle_queued_reset_pending(lease)
        except RPCError: pass
    sessions=tuple(getattr(bridge,'_session_context',{}))
    if not sessions: raise ValueError('queued reset session unavailable')
    session=sessions[0]
    before=await _queued_marker_scan(worker,owner,account,session,marker_state='mixed',current=current)
    if not before or not await current(): raise ValueError('queued reset markers unavailable')
    mapping=OwnerSessionMap(pool._runtime_home(owner)/'session-map.json',owner)
    verifier=CurrentQueuedSource(owner=owner,auth_manager=auth_manager,session_factory=SessionLocal,archive=archive,
        workspace_repository=getattr(app.state,'files_policy_repository',None),mapping=mapping,
        target_authority=authority,provider=provider,bridge=bridge,worker=worker)
    async with AsyncExitStack() as stack:
        guards={}
        for chat in sorted({source.chat_id for source,_ in before}):
            guard=await stack.enter_async_context(mapping.async_chat_admission_lock(chat,blocking=False,existing_only=True))
            if guard is None or not await current(): raise ValueError('queued reset guard unavailable')
            guards[chat]=guard
        checks=[{'source':source,('preimage' if item['state']=='enrolled' else 'fence'):
            item['preimage' if item['state']=='enrolled' else 'fence'],
            'guard':guards[source.chat_id],'verifier':verifier} for source,item in before]
        preflight=await provider._host_queued_origin_preflight(checks,claim=lease.claim,inventory=lease.inventory,
            target_authority=authority,reset_lease=lease)
        if not await current(): raise ValueError('queued reset preflight changed')
    # Native mutation calls back through the same lease and acquires each chat
    # guard itself. Holding the preflight guards here would deadlock that path.
    for source,item in before:
        if item['state']=='fenced': continue
        if not await current(): raise ValueError('queued reset authority changed')
        method='_openclank/session/v1/queued-source/retire-marker'
        request=validate_engine_method_request(method,{'sessionID':source.native_session_id,'messageID':source.message_id,'partID':source.part_id})
        result=await pool.queued_reset_mutate(lease,method,request)
        result=validate_engine_method_result(method,result)
        if result.get('state')!='fenced' or not await current(): raise ValueError('queued marker retirement unavailable')
    after=await _queued_marker_scan(worker,owner,account,session,marker_state='fenced',current=current)
    def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    initial=[asdict(source) for source,_ in before];terminal=[asdict(source) for source,_ in after]
    if initial!=terminal or not await current(): raise ValueError('queued terminal inventory changed')
    fences=[]
    for source,item in after:
        source_hash=hashlib.sha256(canonical(['queued.producer-source.v1',source.account_id,source.chat_id,source.native_session_id,
            source.request_id,source.message_id,source.part_id])).hexdigest()
        evidence=pool.queued_retirement_evidence(owner,bridge,claim=lease.claim,source_hash=source_hash)
        if evidence is None or not await current(): raise ValueError('queued retirement evidence unavailable')
        await pool.record_queued_reset_progress(lease,source_hash=source_hash,state='fenced')
        async with mapping.async_chat_admission_lock(source.chat_id,blocking=False,existing_only=True) as guard:
            if guard is None or not await current(): raise ValueError('queued reset guard unavailable')
            await verifier.complete_retirement(source,item['fence'],guard=guard,evidence=evidence,
                lease_current=lambda: pool.queued_reset_lease_for_source(owner,bridge,source) is lease,reset_lease=lease)
            if not await current(): raise ValueError('queued retirement changed')
        await pool.record_queued_reset_progress(lease,source_hash=source_hash,state='completed')
        fences.append(item['fence'])
    if not await current(): raise ValueError('queued reset authority changed')
    return await pool.checkpoint_queued_reset_retirement(lease,marker_sha256=hashlib.sha256(canonical(terminal)).hexdigest(),
        fence_sha256=hashlib.sha256(canonical(fences)).hexdigest(),preflight_sha256=preflight['fingerprint'])

app.state.queued_reset_retirement = _queued_reset_retirement


async def _queued_reset_finalize(closure, *, claim, result, accumulated_counts):
    from src.openclank.mimo_supervisor import _queued_owned_thread
    await _queued_owned_thread(closure.lease.provider._assert_reset_retirement_ready,closure.lease.reset_transport_witness)
    return await app.state.mimo_supervisor.owner_lifecycle_pool().finalize_queued_reset_closure(
        closure,claim=claim,result=result,accumulated_counts=accumulated_counts)

app.state.queued_reset_finalize = None


async def _refresh_queued_reset_capability(state, profile, provider, owner, account_id, target, authority):
    """One user-triggered, read-only refresh; never starts a child or profile."""
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.openclank.memory_job_admission import ForegroundRuntime, SingleHostProfile, _conn
    from src.openclank.mimo_supervisor import _queued_owned_thread
    if not provider._reset_retirement_ready_local(): state.queued_reset_finalize=None
    supervisor=getattr(state,'mimo_supervisor',None)
    runtime=getattr(supervisor,'_s05_foreground_runtime',None)
    def current():
        if (state is not app.state or type(profile) is not SingleHostProfile or profile.provider is not provider
            or canonical_source_provider(state) is not provider or getattr(state,'s05_profile',None) is not profile
            or getattr(supervisor,'_s05_profile',None) is not profile or type(runtime) is not ForegroundRuntime
            or runtime.profile is not profile or getattr(state,'mimo_supervisor',None) is not supervisor
            or getattr(supervisor,'_s05_foreground_runtime',None) is not runtime
            or auth_manager.account_id(owner)!=account_id or auth_manager.username_for_account_id(account_id)!=owner
            or authority.resolve(account_id)!=target): raise ValueError('queued reset profile unavailable')
        profile.assert_current()
        with _conn(profile.path) as db:
            row=db.execute('SELECT verified,coverage_generation,clock_generation FROM fm_v2_s05_occupancy WHERE singleton=1').fetchone()
            if not row or tuple(row)!=(1,profile.coverage_generation,profile.clock_generation):
                raise ValueError('queued reset profile unavailable')
        profile.assert_current()
    try:
        await _queued_owned_thread(current)
        if not provider._reset_retirement_ready_local():
            if not await provider._host_negotiate_reset_retirement(owner=target.memory_owner,workspace_id=target.memory_workspace):
                return False
        await _queued_owned_thread(current)
        await _queued_owned_thread(provider._assert_reset_retirement_ready)
        state.queued_reset_finalize=_queued_reset_finalize
        return True
    except Exception:
        state.queued_reset_finalize=None
        return False


async def _refresh_reset_capability_for_owner(*, state, owner, account_id, provider, terminal_s08=False):
    """A reset preview may refresh capability without a queued source."""
    from src.openclank.memory_job_admission import TargetAuthority
    try:
        supervisor=getattr(state,'mimo_supervisor',None)
        authority=TargetAuthority(auth_manager=auth_manager,supervisor=supervisor.owner_lifecycle_pool())
        target=authority.resolve(account_id)
        ready=await _refresh_queued_reset_capability(state,getattr(state,'s05_profile',None),provider,
            owner,account_id,target,authority)
        if ready and terminal_s08 is True:
            await provider._host_negotiate_terminal_s08(owner=target.memory_owner,workspace_id=target.memory_workspace)
        return ready
    except Exception:
        return False


app.state.refresh_reset_capability_for_owner = _refresh_reset_capability_for_owner


async def _queued_holder_retirement(lease):
    """Withdraw every exact old-holder queued origin before allowing reclaim."""
    import hashlib
    import json
    from dataclasses import fields
    from src.openclank.queued_source import QueuedSourceRef
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.session_map import OwnerSessionMap
    from src.openclank.memory_job_admission import TargetAuthority, SingleHostProfile
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.memery_external_sources import CurrentQueuedSource
    from src.openclank.managed_protocol import validate_engine_method_request, validate_engine_method_result
    try:
        supervisor=app.state.mimo_supervisor; pool=supervisor.owner_lifecycle_pool()
        owner=lease.owner; worker=lease.worker; bridge=lease.bridge
        evidence=pool.queued_retirement_evidence(owner,bridge)
        if evidence is None or pool.queued_retirement_worker(owner,bridge) is not worker: return False
        account=auth_manager.account_id(owner)
        if not account or auth_manager.username_for_account_id(account)!=owner: return False
        def holder_current():
            try:
                return (not _owner_request_fenced(owner) and pool.queued_retirement_worker(owner,bridge) is worker
                    and pool.queued_retirement_evidence(owner,bridge)==evidence
                    and auth_manager.account_id(owner)==account and auth_manager.username_for_account_id(account)==owner)
            except Exception: return False
        # A context merely selects this worker's process-global DB; each item
        # independently proves its own exact old session and binding below.
        session=None
        for candidate in tuple(getattr(bridge,'_session_context',{})):
            try:
                binding=await bridge._handle_session_binding_read({'sessionID':candidate})
                if binding.get('owner')==owner and binding.get('engineSessionID')==candidate and holder_current():
                    session=candidate; break
            except Exception: continue
        if session is None: return False
        method='_openclank/session/v1/queued-source/markers'
        request=validate_engine_method_request(method,{'owner':owner,'sessionID':session,'after':None,'limit':128})
        async with asyncio.timeout(4): initial=await worker.managed_engine_call(method,request)
        initial=validate_engine_method_result(method,initial)
        if not holder_current() or await bridge._handle_session_binding_read({'sessionID':session})!=binding: return False
        if initial.get('state')=='legacy_empty': return True
        if initial.get('state')!='ready' or not getattr(app.state,'queued_source_mutation_ready',False): return False
        provider=canonical_source_provider(app.state); profile=getattr(app.state,'s05_profile',None)
        if type(profile) is not SingleHostProfile or profile.provider is not provider: return False
        profile.assert_current()
        mapping=OwnerSessionMap(pool._runtime_home(owner)/'session-map.json',owner)
        authority=TargetAuthority(auth_manager=auth_manager,supervisor=pool)
        target=authority.resolve(account)
        def current():
            try:
                profile.assert_current()
                return holder_current() and canonical_source_provider(app.state) is provider and authority.resolve(account)==target
            except Exception: return False
        verifier=CurrentQueuedSource(owner=owner,auth_manager=auth_manager,session_factory=SessionLocal,
            archive=get_conversation_archive(),workspace_repository=getattr(app.state,'files_policy_repository',None),
            mapping=mapping,target_authority=authority,provider=provider,bridge=bridge,worker=worker)
        async def page(after):
            method='_openclank/session/v1/queued-source/markers'
            request=validate_engine_method_request(method,{'owner':owner,'sessionID':session,'after':after,'limit':128})
            if not current(): raise ValueError('queued holder changed')
            async with asyncio.timeout(4): value=await worker.managed_engine_call(method,request)
            value=validate_engine_method_result(method,value)
            if value.get('state')!='ready' or not current(): raise ValueError('queued inventory unavailable')
            return value
        async def scan(terminal):
            digest=hashlib.sha256(); after=None; previous=None
            while True:
                result=initial if not terminal and after is None else await page(after)
                if not current(): raise ValueError('queued holder changed')
                items=result['items']
                for item in items:
                    raw=item.get('source')
                    if type(raw) is not dict or set(raw)!={field.name for field in fields(QueuedSourceRef)}: raise ValueError('queued source unavailable')
                    source=QueuedSourceRef(**raw); key=(source.chat_id,source.native_session_id,source.request_id)
                    if source.account_id!=account or (previous is not None and key<=previous): raise ValueError('queued inventory changed')
                    previous=key
                    digest.update(json.dumps(raw,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')+b'\n')
                    if not terminal:
                        if item['state']=='enrolled':
                            method='_openclank/session/v1/queued-source/retire-marker'
                            request=validate_engine_method_request(method,{'sessionID':source.native_session_id,'messageID':source.message_id,'partID':source.part_id})
                            async with asyncio.timeout(5): response=await worker.managed_engine_call(method,request)
                            validate_engine_method_result(method,response)
                            # An unavailable reply is not proof either way. The
                            # complete second scan must independently see and
                            # verify the exact terminal marker and prior stage.
                            if not current(): raise ValueError('queued marker retirement unavailable')
                        elif item['state']!='fenced': raise ValueError('queued marker unavailable')
                    else:
                        if item['state']!='fenced': raise ValueError('queued marker not retired')
                        deadline=asyncio.get_running_loop().time()+4.0
                        async def finish(guard):
                            await verifier.complete_retirement(source,item['fence'],guard=guard,evidence=evidence,lease_current=current)
                            return True
                        async with asyncio.timeout_at(deadline):
                            async with _account_request_barrier.admitted(owner):
                                completed=await _s07_nonblocking_guard(mapping,source.chat_id,deadline,finish)
                        if completed is not True: raise ValueError('queued retirement guard unavailable')
                        if not current(): raise ValueError('queued holder changed')
                after=result['nextAfter']
                if after is None: return digest.digest()
                if not items or previous is None or tuple(after)!=previous: raise ValueError('queued cursor changed')
        before=await scan(False); after=await scan(True)
        return before==after and current()
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


async def _queued_source_stage_callback(bridge, identity, entry, references, producer):
    """Optionally stage content-free exact queued mappings before turn retirement."""
    if getattr(app.state, 'queued_source_staging_ready', False) is not True:
        return False
    from src.openclank.conversation_archive import TrustedQueuedPending, get_conversation_archive, _valid_queued_producer
    from src.openclank.memory_policy import STORE
    from src.openclank.queued_source import match_queued_boundary
    from src.openclank.session_map import OwnerSessionMap
    from src.openclank.managed_protocol import validate_engine_method_result
    import hashlib
    try:
        owner=entry.get('owner');account=entry.get('ownerAccountID');chat=entry.get('stableChatID')
        if (not all(isinstance(v,str) and v for v in (owner,account,chat)) or identity.get('actorID')!='main'
            or not isinstance(references,dict) or not 1<=len(references)<=32 or not _valid_queued_producer(producer)): return False
        pool=app.state.mimo_supervisor.owner_lifecycle_pool()
        worker=app.state.mimo_supervisor.worker_for_owner(owner)
        if worker is None or getattr(worker,'_bridge',None) is not bridge: return False
        holder=bridge._managed_provider_generation
        if (not holder or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
            or producer!={'version':1,'holderSHA256':hashlib.sha256(holder.encode()).hexdigest(),'generation':getattr(worker,'installed_generation',None),'ownerEpoch':pool._owner_lifecycle_epoch(owner)}): return False
        mapping=OwnerSessionMap(pool._runtime_home(owner)/'session-map.json',owner)
        deadline=asyncio.get_running_loop().time()+0.2
        async def stage_under_guard(guard):
            mapping.validate_chat_guard(chat,guard)
            binding=await bridge._handle_session_binding_read({'sessionID':identity['sessionID']})
            binding_keys=('engineSessionID','stableChatID','owner','canonicalCwd','authorityWorkspaceID','memoryWorkspaceID','copalWorkspace','mappingRevision')
            frozen={k:binding[k] for k in binding_keys}
            if binding['stableChatID']!=chat or binding['owner']!=owner: return False
            archive=get_conversation_archive(); staged=0
            for request_id,ref in references.items():
                remaining=deadline-asyncio.get_running_loop().time()
                if remaining<=0: break
                method='_openclank/session/v1/queued-source/read'
                value=await asyncio.wait_for(worker.managed_engine_call(method,{'owner':owner,'stableChatID':chat,
                    'sessionID':identity['sessionID'],'requestID':request_id}),timeout=remaining)
                value=validate_engine_method_result(method,value)
                proof=match_queued_boundary(value.get('proof'),owner=owner,account_id=account,chat_id=chat,
                    session_id=identity['sessionID'],actor_id='main',workspace_id=binding['authorityWorkspaceID'],receipt=value)
                if (not proof or value.get('messageID')!=ref['messageID'] or value.get('partID')!=ref['partID']
                    or value.get('payloadHash')!=ref['payloadHash'] or hashlib.sha256(value['proof'].encode('utf-8')).hexdigest()!=ref['proofSHA256']
                    or proof!=ref['proof'] or await bridge._handle_session_binding_read({'sessionID':identity['sessionID']})!=binding):
                    continue
                async with bridge._managed_execution_lock:
                    if (bridge._managed_actor_turns.get(bridge._managed_turn_key(identity)) is not entry or entry.get('queuedSourceClosing')
                        or entry.get('queuedSourceForwarded')!=references): return False
                pending=TrustedQueuedPending(dict(identity),proof,ref['proofSHA256'],ref['payloadHash'],ref['messageID'],ref['partID'],frozen,dict(producer))
                def current():
                    try:
                        mapping.validate_chat_guard(chat,guard)
                        privilege=bool((auth_manager.get_privileges(owner) or {}).get('can_manage_memory',True))
                        policy=STORE.effective_snapshot(owner,read_privilege=privilege,write_privilege=privilege,
                            incognito=False,no_memory=False,compare_mode=False)
                        return (bridge._managed_actor_turns.get(bridge._managed_turn_key(identity)) is entry
                            and entry.get('state')=='active' and not entry.get('queuedSourceClosing')
                            and entry.get('queuedSourceForwarded')==references and entry.get('queuedSourceProducer')==producer and bridge._managed_provider_generation==holder
                            and app.state.mimo_supervisor.worker_for_owner(owner) is worker and worker.is_alive()
                            and getattr(worker,'installed_generation',None)==producer['generation'] and pool._owner_lifecycle_epoch(owner)==producer['ownerEpoch']
                            and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder
                            and auth_manager.account_id(owner)==account and auth_manager.username_for_account_id(account)==owner
                            and not _owner_request_fenced(owner) and policy.get('eligibility_state')=='available' and policy.get('capture_allowed') is True)
                    except Exception: return False
                task=asyncio.create_task(asyncio.to_thread(archive.record_trusted_queued_pending,pending,current=current))
                cancelled=False
                while not task.done():
                    try: await asyncio.shield(task)
                    except asyncio.CancelledError: cancelled=True
                result=task.result()
                if cancelled: raise asyncio.CancelledError
                if result.get('state')=='pending':
                    async with bridge._managed_execution_lock:
                        if bridge._managed_actor_turns.get(bridge._managed_turn_key(identity)) is entry:
                            entry.setdefault('queuedSourceStaged',{})[request_id]=pending
                            staged+=1
            return staged>0
        async with asyncio.timeout_at(deadline):
            async with _account_request_barrier.admitted(owner):
                return bool(await _s07_nonblocking_guard(mapping,chat,deadline,stage_under_guard))
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


app.state.queued_source_stage_callback = _queued_source_stage_callback


async def _queued_source_completion_callback(bridge, identity, staged):
    """Persist only a verified completed turn for already-staged candidates."""
    from src.openclank.conversation_archive import TrustedQueuedPending, get_conversation_archive
    from src.openclank.memory_policy import STORE
    from src.openclank.session_map import OwnerSessionMap
    from src.openclank.transcript_projection import read_managed_turn_receipt
    import hashlib
    try:
        if type(staged) is not dict or not 1<=len(staged)<=32 or any(type(v) is not TrustedQueuedPending for v in staged.values()): return False
        first=next(iter(staged.values()));owner=first.proof['owner'];account=first.proof['accountID'];chat=first.proof['stableChatID']
        if any(item.identity!=identity or item.proof['owner']!=owner or item.proof['accountID']!=account or item.proof['stableChatID']!=chat or item.producer!=first.producer for item in staged.values()): return False
        pool=app.state.mimo_supervisor.owner_lifecycle_pool();worker=app.state.mimo_supervisor.worker_for_owner(owner)
        if worker is None or getattr(worker,'_bridge',None) is not bridge: return False
        holder=bridge._managed_provider_generation
        if (not holder or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
            or first.producer!={'version':1,'holderSHA256':hashlib.sha256(holder.encode()).hexdigest(),'generation':getattr(worker,'installed_generation',None),'ownerEpoch':pool._owner_lifecycle_epoch(owner)}): return False
        mapping=OwnerSessionMap(pool._runtime_home(owner)/'session-map.json',owner)
        async with _account_request_barrier.admitted(owner):
            async with mapping.async_chat_admission_lock(chat,blocking=False) as guard:
                mapping.validate_chat_guard(chat,guard)
                receipt=await asyncio.to_thread(read_managed_turn_receipt,chat,owner,identity)
                binding=await bridge._handle_session_binding_read({'sessionID':identity['sessionID']})
                if (not receipt or receipt.get('outcome')!='completed' or receipt.get('accountID')!=account
                    or any(binding.get(k)!=first.binding.get(k) for k in first.binding)
                    or bridge._managed_provider_generation!=holder): return False
                receipt_keys=('version','owner','accountID','chatID','holderID','rootSessionID','sessionID','rootOperationID',
                    'actorID','executionRevision','innerTurnID','outcome','workspaceRevision','mappingRevision','authorityWorkspaceID')
                receipt={key:receipt[key] for key in receipt_keys}
                def current():
                    try:
                        mapping.validate_chat_guard(chat,guard)
                        privilege=bool((auth_manager.get_privileges(owner) or {}).get('can_manage_memory',True))
                        policy=STORE.effective_snapshot(owner,read_privilege=privilege,write_privilege=privilege,
                            incognito=False,no_memory=False,compare_mode=False)
                        return (auth_manager.account_id(owner)==account and auth_manager.username_for_account_id(account)==owner
                            and bridge._managed_provider_generation==holder and app.state.mimo_supervisor.worker_for_owner(owner) is worker and worker.is_alive()
                            and getattr(worker,'installed_generation',None)==first.producer['generation'] and pool._owner_lifecycle_epoch(owner)==first.producer['ownerEpoch'] and not _owner_request_fenced(owner)
                            and policy.get('eligibility_state')=='available' and policy.get('capture_allowed') is True)
                    except Exception: return False
                archive=get_conversation_archive();completed=0
                for pending in staged.values():
                    task=asyncio.create_task(asyncio.to_thread(archive.complete_trusted_queued_pending,pending=pending,receipt=receipt,current=current))
                    cancelled=False
                    while not task.done():
                        try: await asyncio.shield(task)
                        except asyncio.CancelledError: cancelled=True
                    if task.result(): completed+=1
                    if cancelled: raise asyncio.CancelledError
                return completed>0
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


app.state.queued_source_completion_callback = _queued_source_completion_callback




async def _maintenance_source_callback(bridge, identity, original, boundary, request):
    """Attest one exact, currently eligible host-user source without its text."""
    trace = logger.isEnabledFor(logging.DEBUG)
    phase = 'setup'
    started = time.monotonic() if trace else 0.0
    timings = {} if trace else None
    def _phase(name):
        nonlocal phase, started
        if timings is not None:
            now=time.monotonic(); timings[phase]=round((now-started)*1000,3); started=now
        phase=name
    try:
        from dataclasses import asdict
        from sqlalchemy import text as sql_text
        from src.memery_external_sources import CurrentHostUserSource, SourceRef
        from src.openclank.conversation_archive import get_conversation_archive
        from src.openclank.memory_policy import STORE
        from src.openclank.session_map import OwnerSessionMap
        from src.secret_storage import keyed_digest
        owner=original.get('owner'); account=original.get('ownerAccountID'); chat=original.get('stableChatID')
        if not all(isinstance(value,str) and value for value in (owner,account,chat)):
            raise ValueError('maintenance source unavailable')
        pool=app.state.mimo_supervisor.owner_lifecycle_pool()
        worker=app.state.mimo_supervisor.worker_for_owner(owner)
        if worker is None or worker.bridge is not bridge: raise ValueError('maintenance worker changed')
        mapping=OwnerSessionMap(pool._runtime_home(owner)/'session-map.json',owner)
        archive=get_conversation_archive()
        verifier=CurrentHostUserSource(owner=owner,auth_manager=auth_manager,session_factory=SessionLocal,
            archive=archive,workspace_repository=getattr(app.state,'files_policy_repository',None),policy_store=STORE,guarded_preparation=getattr(getattr(getattr(app.state,'s05_profile',None),'provider',None),'_guarded_preparation',None))
        _phase("account_barrier")
        async with _account_request_barrier.admitted(owner):
            _phase("chat_guard")
            async with mapping.async_chat_admission_lock(chat) as guard:
                mapping.validate_chat_guard(chat,guard)
                _phase("first_resolve")
                if (auth_manager.account_id(owner)!=account or auth_manager.username_for_account_id(account)!=owner
                        or await bridge.resolve_managed_recall_turn(identity)!=original):
                    raise ValueError('maintenance account changed')
                def current_source():
                    with SessionLocal() as db:
                        row=db.execute(sql_text("SELECT m.source_body_revision,m.source_body_hash,s.workspace_id,i.origin_json,i.origin_state FROM chat_messages m JOIN sessions s ON s.id=m.session_id JOIN host_source_delivery_intent i ON i.owner=:account AND i.chat_id=s.id AND i.message_id=m.id AND i.part_id='body' AND i.revision=1 WHERE m.id=:message AND m.session_id=:chat AND m.role='user'"),
                            {'account':account,'message':identity['rootOperationID'],'chat':chat}).first()
                    if not row or row[0]!=1 or row[1]!=boundary.get('bodySHA256') or (row[2] if row[2] is not None else '')!=original.get('authorityWorkspaceID'):
                        raise ValueError('maintenance source changed')
                    origin=json.loads(row[3]); choices=json.loads(origin['choices_json'])
                    if (origin.get('capture_eligible') is not True or any(choices.get(key) is not False for key in ('incognito','noMemory','compareMode'))
                            or row[4] not in ('pending','delivered')):
                        raise ValueError('maintenance source not eligible')
                    return SourceRef(2 if row[2] is None else 1,'host_user_body',account,chat,'main',identity['rootOperationID'],'body',1,row[1],row[2],None)
                _phase("source_sql")
                source=await asyncio.to_thread(current_source)
                _phase("source_verify")
                if (await asyncio.to_thread(verifier.verify_feedback_origin,source)).get('state')!='available':
                    raise ValueError('maintenance source unavailable')
                _phase("policy")
                privileges=auth_manager.get_privileges(owner); allowed=privileges.get('can_manage_memory',True) if isinstance(privileges,dict) else False
                snapshot=STORE.capture_eligibility(owner,read_privilege=allowed,write_privilege=allowed,incognito=False,no_memory=False,compare_mode=False)
                if snapshot.get('eligibility_state')!='available' or snapshot.get('capture_allowed') is not True:
                    raise ValueError('maintenance write unavailable')
                mapping.validate_chat_guard(chat,guard)
                _phase("final_resolve")
                from src.openclank.acp_bridge import _claim_maintenance_finalizer
                finalizer = _claim_maintenance_finalizer(bridge, identity, original, boundary, request)
                if finalizer is None:
                    if await bridge.resolve_managed_recall_turn(identity)!=original: raise ValueError('maintenance turn changed')
                else:
                    await finalizer()
                _phase("attestation")
                if request.get('version') == 2:
                    from src.openclank.chat_source_bridge import _source_layout_hash
                    layout=request.get('layout'); position=request.get('sourcePosition')
                    layout_hash=_source_layout_hash(layout,position,request['textSHA256'])
                    if (layout_hash is None or boundary.get('sourcePosition') != position
                            or list(boundary.get('parts', ())) != layout):
                        raise ValueError('maintenance layout unavailable')
                    payload={'version':2,'source':asdict(source),'identity':identity,'messageID':request['messageID'],
                             'partID':request['partID'],'textSHA256':request['textSHA256'],
                             'sourcePosition':position,'layoutSHA256':layout_hash}
                    domain='openclank.c2.maintenance-source.v2'
                else:
                    payload={'version':1,'source':asdict(source),'identity':identity,'messageID':request['messageID'],
                             'partID':request['partID'],'textSHA256':request['textSHA256'],'position':0}
                    domain='openclank.c2.maintenance-source.v1'
                encoded=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))
                return json.dumps({'payload':payload,'mac':keyed_digest(encoded,context=domain)},
                                  ensure_ascii=False,sort_keys=True,separators=(',',':'))
    except BaseException as exc:
        if timings is not None:
            now=time.monotonic(); timings[phase]=round((now-started)*1000,3)
            outcome=('cancelled' if isinstance(exc,asyncio.CancelledError) else
                     'timeout' if isinstance(exc,TimeoutError) else
                     'refused' if isinstance(exc,ValueError) else 'error')
            try: logger.debug('maintenance source unavailable phase=%s outcome=%s timings_ms=%s',phase,outcome,timings)
            except Exception: pass
        raise

app.state.maintenance_source_callback = _maintenance_source_callback


async def _s07_context_delivery_callback(bridge, identity, evidence, proof):
    """Record first acknowledged context delivery using host-held evidence only."""
    from src.memory_provider import FoundObservation
    from src.memery_provider import _HostObservationProjection, _HostContextAdmittedObservation
    from src.memery_external_sources import CurrentHostUserSource, SourceRef
    from src.openclank.memory_context_admission import S07ContextEvidence, found_features_for
    from src.openclank.memory_job_admission import TargetAuthority, _conn
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_policy import STORE
    from src.openclank.session_map import OwnerSessionMap
    trace = logger.isEnabledFor(logging.DEBUG)
    loop = asyncio.get_running_loop()
    phase = ['validation', loop.time()]
    timings = {}
    outcome = ['rejected']
    def mark(name):
        if trace:
            now = loop.time()
            timings[phase[0]] = round(timings.get(phase[0], 0) + (now - phase[1]) * 1000, 3)
            phase[:] = [name, now]
    try:
        mark('evidence')
        if (type(evidence) is not S07ContextEvidence or type(evidence.source) is not SourceRef
                or type(proof) is not dict or set(proof) != {'attestationID','textSHA256','contextPartID','dispatchID','bodySHA256'}
                or identity.get('actorID') != 'main' or not 1 <= len(evidence.selected) <= 3): return False
        invocation=evidence.invocation
        if (invocation.session_id != identity.get('sessionID') or invocation.root_operation_id != identity.get('rootOperationID')
                or invocation.actor_id != identity.get('actorID') or invocation.execution_revision != identity.get('executionRevision')
                or invocation.inner_turn_id != identity.get('innerTurnID')): return False
        supervisor=app.state.mimo_supervisor; owner=evidence.owner; source=evidence.source
        mark('s06_readiness')
        if not await asyncio.to_thread(_s06_admitted_ready,app.state,supervisor): return False
        provider=canonical_source_provider(app.state)
        mark('context_cap')
        with _conn(provider._local_db_path()) as db:
            if not provider._s06_context_admitted_capability(db): return False
        mark('actor_account_source')
        original=await bridge.resolve_managed_recall_turn(identity)
        worker=supervisor.worker_for_owner(owner)
        if (worker is None or worker.bridge is not bridge or original.get('owner') != owner
                or original.get('ownerAccountID') != evidence.account_id or source.account_id != evidence.account_id
                or original.get('stableChatID') != source.chat_id or identity.get('rootOperationID') != source.message_id
                or auth_manager.account_id(owner) != evidence.account_id
                or auth_manager.username_for_account_id(evidence.account_id) != owner): return False
        pool=supervisor.owner_lifecycle_pool(); authority=TargetAuthority(auth_manager=auth_manager,supervisor=pool)
        mapping=OwnerSessionMap(pool._runtime_home(owner)/'session-map.json',owner)
        verifier=CurrentHostUserSource(owner=owner,auth_manager=auth_manager,session_factory=SessionLocal,
            archive=get_conversation_archive(),workspace_repository=getattr(app.state,'files_policy_repository',None),policy_store=STORE,guarded_preparation=getattr(getattr(getattr(app.state,'s05_profile',None),'provider',None),'_guarded_preparation',None))
        async def current(guard):
            mapping.validate_chat_guard(source.chat_id,guard)
            return (not await asyncio.to_thread(_owner_request_fenced,owner)
                and await bridge.resolve_managed_recall_turn(identity)==original
                and auth_manager.account_id(owner)==evidence.account_id
                and await asyncio.to_thread(authority.resolve,evidence.account_id)==evidence.target
                and (await asyncio.to_thread(verifier.verify_feedback_origin,source)).get('state')=='available'
                and await asyncio.to_thread(_s06_admitted_ready,app.state,supervisor))
        async with _account_request_barrier.admitted(owner):
            async with mapping.async_chat_admission_lock(source.chat_id) as guard:
                mark('pre_check')
                if not await current(guard): return False
                deadline=asyncio.get_running_loop().time()+0.2
                mark('revalidate')
                if not await provider._s07_revalidate_candidates(owner=evidence.target.memory_owner,workspace_id=evidence.target.memory_workspace,
                        invocation=evidence.invocation,selected=evidence.selected,deadline=deadline): return False
                # One send/no-send answer for the whole attempt, made before the first found write. The delivery task is created once per attestation and re-awaited on retry (acp_bridge), so no write of this evidence is rebuilt with a different answer.
                send_features=False
                if evidence.features:
                    from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
                    def shadow_pref():
                        try:
                            with PREFERENCE_LOCK: return _load_for_user(owner).get('memory_shadow_ranking_enabled') is True
                        except Exception: return False
                    send_features=(await asyncio.to_thread(shadow_pref)
                        and await provider._host_negotiate_found_features(owner=evidence.target.memory_owner,workspace_id=evidence.target.memory_workspace))
                for cid,ref in evidence.selected:
                    mark('found_check')
                    if not await current(guard): return False
                    mark('append_found')
                    result=await provider._host_append_found(FoundObservation(evidence.target.memory_owner,evidence.target.memory_workspace,source,evidence.invocation,ref,
                        features=found_features_for(send_features,evidence.features,cid)))
                    mark('origin_lookup')
                    cursor='o:'; matches=[]
                    while cursor is not None:
                        rows, following=await asyncio.to_thread(provider._host_source_refs,account_id=source.account_id,chat_id=source.chat_id,
                            message_id=source.message_id,target_authority=authority,after_job_id=cursor,limit=128)
                        matches.extend(row for row in rows if type(row) is _HostObservationProjection and row.source==source)
                        if len(matches)>1 or (following is not None and (not following.startswith('o:') or following<=cursor)): return False
                        cursor=following
                    mark('admitted_check')
                    if len(matches)!=1 or not await current(guard): return False
                    mark('append_admitted')
                    await provider._host_append_context_admitted(_HostContextAdmittedObservation(matches[0],result['event_id'],
                        proof['contextPartID'],proof['attestationID'],proof['textSHA256'],proof['dispatchID'],proof['bodySHA256']))
                mark('final_check')
                accepted = await current(guard)
                if accepted: outcome[0] = 'accepted'
                return accepted
    except asyncio.CancelledError:
        outcome[0] = 'cancelled'
        raise
    except Exception as exc:
        outcome[0] = ('timeout' if isinstance(exc, TimeoutError) else
                      type(exc).__name__ if type(exc).__name__ in {'ValueError','PermissionError','RuntimeError','OSError'} else 'exception')
        return False
    finally:
        if trace:
            mark('done')
            logger.debug('S07_DELIVERY_HOST outcome=%s phases_ms=%s', outcome[0], timings)


app.state.s07_context_delivery_callback = _s07_context_delivery_callback


async def _maintenance_stage_callback(bridge, identity, proof, descriptor):
    """Persist only a verified pending reference while its original turn is live."""
    from src.memery_external_sources import CurrentHostUserSource, SourceRef, _source_ref_error
    from src.openclank.chat_source_bridge import canonical_source_provider, guarded_source_mutation
    from src.openclank.conversation_archive import TrustedMaintenancePending, get_conversation_archive
    from src.openclank.memory_job_admission import SingleHostProfile, _conn
    from src.openclank.memory_policy import STORE
    try:
        if (not callable(getattr(app.state,'maintenance_reconcile_callback',None))
            or type(proof) is not dict or type(descriptor) is not dict):
            return False
        ref=SourceRef(**proof['source'])
        if _source_ref_error(ref) or identity.get('actorID')!='main':
            return False
        resolved=await bridge.resolve_managed_recall_turn(identity)
        owner=resolved.get('owner')
        provider=canonical_source_provider(app.state)
        profile=getattr(app.state,'s05_profile',None)
        reset_factory=getattr(app.state,'memory_reset_coordinator',None)
        if (type(owner) is not str or not owner or resolved.get('ownerAccountID')!=ref.account_id
            or resolved.get('stableChatID')!=ref.chat_id or resolved.get('authorityWorkspaceID')!=('' if ref.version == 2 and ref.workspace_id is None else ref.workspace_id)
            or not callable(reset_factory) or provider is None or type(profile) is not SingleHostProfile or profile.provider is not provider):
            return False
        profile.assert_current()
        if _owner_request_fenced(owner):
            return False
        def capability():
            with _conn(profile.path) as db:
                return provider._maintenance_capability(db)
        if not await asyncio.to_thread(capability):
            return False
        archive=get_conversation_archive()
        source=CurrentHostUserSource(owner=owner,auth_manager=auth_manager,session_factory=SessionLocal,
            archive=archive,workspace_repository=app.state.files_policy_repository,policy_store=STORE,guarded_preparation=getattr(getattr(getattr(app.state,'s05_profile',None),'provider',None),'_guarded_preparation',None))
        binding_fields=('engineSessionID','stableChatID','owner','canonicalCwd','authorityWorkspaceID','memoryWorkspaceID','copalWorkspace','mappingRevision')
        binding_full=await bridge._handle_session_binding_read({'sessionID':identity['sessionID']})
        binding={key:binding_full[key] for key in binding_fields}
        if (binding['engineSessionID']!=identity['sessionID'] or binding['stableChatID']!=ref.chat_id
            or binding['owner']!=owner or binding['authorityWorkspaceID']!=('' if ref.version == 2 and ref.workspace_id is None else ref.workspace_id)
            or binding['mappingRevision']!=resolved.get('mappingRevision')):
            return False
        pending=TrustedMaintenancePending(json.loads(json.dumps(identity)),json.loads(json.dumps(proof)),json.loads(json.dumps(descriptor)),json.loads(json.dumps(binding)))
        async with guarded_source_mutation(state=app.state,owner=owner,chat_id=ref.chat_id) as guard:
            if guard is None or guard.provider is not provider:
                return False
            verified=await asyncio.to_thread(source.verify_feedback_origin,ref)
            if verified.get('state')!='available' or verified.get('source')!=ref:
                return False
            if await bridge.resolve_managed_recall_turn(identity)!=resolved:
                return False
            fresh_binding=await bridge._handle_session_binding_read({'sessionID':identity['sessionID']})
            if {key:fresh_binding[key] for key in binding_fields}!=binding:
                return False
            key=bridge._managed_turn_key(identity)
            async with bridge._managed_execution_lock:
                entry=bridge._managed_actor_turns.get(key)
                if (not isinstance(entry,dict) or entry.get('state')!='active' or entry.get('ownerAccountID')!=ref.account_id
                    or entry.get('owner')!=owner or entry.get('maintenanceAcks',{}).get(descriptor.get('summaryMessageID'))!=proof):
                    return False
                def current():
                    try:
                        guard.validate(provider=provider,owner=owner,chat_id=ref.chat_id)
                        profile.assert_current()
                        if _owner_request_fenced(owner) or auth_manager.account_id(owner)!=ref.account_id:
                            return False
                        with SessionLocal() as db:
                            if not source._authority(db,ref,manual_review=False):
                                return False
                        checked=source.verify_feedback_origin(ref)
                        return checked.get('state')=='available' and checked.get('source')==ref
                    except Exception:
                        return False
                task=asyncio.create_task(asyncio.to_thread(archive.record_trusted_maintenance_pending,pending,current=current))
                try:
                    receipt=await asyncio.shield(task)
                except asyncio.CancelledError:
                    while not task.done():
                        try: await asyncio.shield(task)
                        except asyncio.CancelledError: continue
                    try: task.result()
                    except Exception: pass
                    raise
                if type(receipt) is not dict or receipt.get('state')!='pending':
                    return False
                scheduler=getattr(app.state,'task_scheduler',None)
                wake=getattr(scheduler,'request_completed_turn_wake',None)
                if callable(wake): wake()
                return True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


app.state.maintenance_stage_callback = _maintenance_stage_callback


async def _s08_rules_callback(state, lifecycle, selection, origins, router, *, output_kind="summary", journal=None, expected_policy_refs=None):
    """Hold the original account admission through the entire owned S08 attempt."""
    from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
    if (state is not app.state or type(lifecycle) is not CompletedTurnLifecycleOwner
            or output_kind not in ('summary','connection','journal_synthesis','importance_target')):
        return False
    barrier=getattr(state,'account_request_barrier',None)
    if barrier is not _account_request_barrier: return False
    try:
        admission=await barrier.acquire(lifecycle.owner)
    except Exception:
        return False
    task=asyncio.create_task(_s08_rules_admitted(state,lifecycle,selection,origins,router,admission=admission,output_kind=output_kind,journal=journal,expected_policy_refs=expected_policy_refs))
    cancelled=False
    try:
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: cancelled=True
        if cancelled:
            try: task.result()
            except BaseException: pass
            raise asyncio.CancelledError
        return task.result()
    finally:
        release=asyncio.create_task(barrier.release(admission))
        while not release.done():
            try: await asyncio.shield(release)
            except asyncio.CancelledError: cancelled=True
        release.result()
        if cancelled and not task.cancelled():
            raise asyncio.CancelledError


async def _s08_rules_admitted(state, lifecycle, selection, origins, router, *, admission, output_kind='summary', journal=None, expected_policy_refs=None):
    """Keep original custody while releasing chat guards around remote waiting."""
    from src.openclank.s08_source_guards import held_s08_sources
    from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner, canonical_source_provider
    from src.openclank.memory_job_admission import AssignmentAuthority, _completed_turn_thread
    from src.openclank.memory_policy import STORE
    from src.openclank.memory_policy_projection import PolicyProjection
    from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
    if (state is not app.state or type(lifecycle) is not CompletedTurnLifecycleOwner
            or canonical_source_provider(state) is not lifecycle.provider or _owner_request_fenced(lifecycle.owner)
            or output_kind not in ('summary','connection','journal_synthesis','importance_target')):
        return False
    projection=getattr(state,'memory_policy_projection',None)
    if type(projection) is not PolicyProjection: return False
    try:
        def current():
            lifecycle.profile.assert_current()
            return (canonical_source_provider(state) is lifecycle.provider
                and getattr(state,'auth_manager',None) is auth_manager
                and getattr(state,'account_owner_lifecycle',None) is lifecycle.account_lifecycle
                and getattr(state,'s05_profile',None) is lifecycle.profile
                and getattr(state,'account_request_barrier',None) is _account_request_barrier
                and getattr(state,'memory_policy_projection',None) is projection
                and not _owner_request_fenced(lifecycle.owner)
                and auth_manager.account_id(lifecycle.owner)==lifecycle.account_id
                and auth_manager.username_for_account_id(lifecycle.account_id)==lifecycle.owner
                and lifecycle.target_authority.resolve(lifecycle.account_id)==lifecycle.binding
                and not lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner))
        def cleanup_current():
            _account_request_barrier.assert_live_admission(lifecycle.owner, admission)
            if not current(): raise ValueError('S08 original custody changed')
            return True
        def policy_flags():
            with PREFERENCE_LOCK:
                prefs=_load_for_user(lifecycle.owner)
                if type(prefs) is not dict: raise ValueError('S08 preferences unavailable')
                values=tuple(prefs.get(key) for key in ('memory_policy_consolidation_enabled',
                    'memory_policy_importance_enabled','memory_policy_connections_enabled'))
                if any(value is not None and type(value) is not bool for value in values):
                    raise ValueError('S08 preferences unavailable')
                return tuple(value is True for value in values)
        async def current_flags(expected=None):
            if not current(): return None
            flags,cancelled=await _completed_turn_thread(policy_flags)
            if cancelled: raise asyncio.CancelledError
            if not current() or (expected is not None and flags!=expected): return None
            return flags
        flags=await current_flags()
        if flags is None or (output_kind=='connection' and not flags[2]) or (output_kind=='journal_synthesis' and not flags[0]) or (output_kind=='importance_target' and not flags[1]): return False   # journal: consolidation preference, default Off
        legacy=output_kind=='summary' and not flags[0] and not flags[1]
        assignment=AssignmentAuthority(owner=lifecycle.owner,policy_store=STORE,source_authority=lifecycle.source_authority)
        provider=lifecycle.provider
        new_session=getattr(provider,'_host_s08_new_policy_session',None)
        prepare=getattr(provider,'_host_s08_prepare_policy',None)
        ready_phase=getattr(provider,'_host_s08_ready_policy',None)
        dispatch=getattr(provider,'_host_s08_dispatch_policy',None)
        run=getattr(provider,'_host_s08_run_policy',None)
        commit=getattr(provider,'_host_s08_commit_policy',None)
        readiness=getattr(provider,'_host_s08_policy_ready',None)
        negotiate=getattr(provider,'_host_negotiate_s08_policy',None)
        pending=getattr(provider,'_host_s08_pending_policy',None)
        reconcile=getattr(provider,'_host_s08_reconcile_policy',None)
        disposition=getattr(provider,'_host_s08_policy_disposition',None)
        cancel_original=getattr(provider,'_host_s08_cancel_policy',None)
        terminal=getattr(provider,'_host_s08_terminal_policy',None)
        if not legacy:
            if not all(callable(method) for method in (new_session,prepare,ready_phase,dispatch,run,commit,readiness,negotiate,pending,reconcile,disposition,cancel_original,terminal)):
                return False
            if readiness(output_kind) is not True:
                # Capability negotiation is profile metadata, not a source grant.
                await negotiate(owner=lifecycle.binding.memory_owner,workspace_id=lifecycle.binding.memory_workspace)
                if await current_flags(flags) is None or readiness(output_kind) is not True: return False
        session=None; settled=False
        try:
            async with held_s08_sources(lifecycle=lifecycle,selection=selection,origins=origins,current=current,journal=journal,importance_target=output_kind=='importance_target') as (guards,validate_sources):
                if not await validate_sources() or await current_flags(flags) is None: return False
                if legacy:
                    result=await provider._host_s08_rules_once(lifecycle=lifecycle,selection=selection,origins=origins,
                        policy_projection=projection,assignment=assignment,guards=guards,router=router)
                    return bool(current() and result.get('state')=='succeeded')
                session=new_session(lifecycle,selection,origins,projection,assignment,router,output_kind,**({'journal':journal} if journal is not None else {}),**({'expected_policy_refs':expected_policy_refs} if expected_policy_refs is not None else {}))
                if session is None: return False
                if await prepare(session,guards=guards)!={'state':'prepared'}: return False
            # Worker readiness and response wait do not hold chat source guards.
            if await ready_phase(session)!={'state':'ready'}: return False
            async with held_s08_sources(lifecycle=lifecycle,selection=selection,origins=origins,current=current,journal=journal,importance_target=output_kind=='importance_target') as (guards,validate_sources):
                if not await validate_sources() or await current_flags(flags) is None: return False
                if await dispatch(session,guards=guards)!={'state':'issued'}: return False
            if await run(session)!={'state':'terminal'}: return False
            kind=disposition(session)
            if kind=='terminal':
                result=await terminal(session,lifecycle=lifecycle,current=cleanup_current)
                settled=type(result) is dict and result.get('state')=='settled'
                return settled
            if kind=='no_proposal':
                result=await commit(session,cleanup_current=cleanup_current)
            elif kind=='proposal':
                async with held_s08_sources(lifecycle=lifecycle,selection=selection,origins=origins,current=current,journal=journal,importance_target=output_kind=='importance_target') as (guards,validate_sources):
                    if not await validate_sources() or await current_flags(flags) is None: return False
                    result=await commit(session,guards=guards)
            else:
                return False
            if (type(result) is not dict or set(result)!={'version','state','candidate_id'}
                or type(result['version']) is not int or result['version']!=1
                or (result['state']=='staged' and (type(result['candidate_id']) is not str or not result['candidate_id']))
                or (result['state']=='no_proposal' and result['candidate_id'] is not None)
                or result['state'] not in ('staged','no_proposal')):
                return False
            settled=True
            return bool(cleanup_current())
        finally:
            if session is not None and not settled:
                try:
                    await cancel_original(session,lifecycle=lifecycle,current=cleanup_current)
                except Exception:
                    # Unknown original remains in the provider's retained custody.
                    pass
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


async def _s08_original_status_callback(state, lifecycle):
    """Reconcile only the retained original under current named read authority."""
    from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner, canonical_source_provider
    from src.openclank.memory_job_admission import _completed_turn_thread
    from src.openclank.memory_policy import STORE
    from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
    if (state is not app.state or type(lifecycle) is not CompletedTurnLifecycleOwner
        or canonical_source_provider(state) is not lifecycle.provider): return False
    provider=lifecycle.provider;barrier=getattr(state,'account_request_barrier',None)
    pending=getattr(provider,'_host_s08_pending_policy',None);reconcile=getattr(provider,'_host_s08_reconcile_policy',None);cancel_original=getattr(provider,'_host_s08_cancel_policy',None)
    projection=getattr(state,'memory_policy_projection',None)
    if barrier is not _account_request_barrier or type(projection) is not PolicyProjection or not callable(pending) or not callable(reconcile) or not callable(cancel_original):
        return False
    try: admission=await barrier.acquire(lifecycle.owner)
    except Exception: return False
    async def owned():
        def current():
            lifecycle.profile.assert_current()
            if (canonical_source_provider(state) is not provider or getattr(state,'memory_policy_projection',None) is not projection
                or getattr(state,'auth_manager',None) is not auth_manager
                or getattr(state,'account_owner_lifecycle',None) is not lifecycle.account_lifecycle
                or getattr(state,'s05_profile',None) is not lifecycle.profile
                or getattr(state,'account_request_barrier',None) is not barrier
                or _owner_request_fenced(lifecycle.owner) or auth_manager.account_id(lifecycle.owner)!=lifecycle.account_id
                or auth_manager.username_for_account_id(lifecycle.account_id)!=lifecycle.owner
                or lifecycle.target_authority.resolve(lifecycle.account_id)!=lifecycle.binding
                or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner)):
                raise ValueError('S08 status authority changed')
            privileges=auth_manager.get_privileges(lifecycle.owner)
            allowed=privileges.get('can_manage_memory',True) if isinstance(privileges,dict) else False
            if type(allowed) is not bool or not allowed: raise ValueError('S08 status read unavailable')
            policy=STORE.effective_snapshot(lifecycle.owner,read_privilege=allowed,write_privilege=allowed,
                incognito=False,no_memory=False,compare_mode=False)
            coordinator=policy_coordinator()
            if (not policy.get('read_allowed',False) or coordinator is None or coordinator.projection is not projection):
                raise ValueError('S08 status read unavailable')
            return coordinator.checked_refs(lifecycle.owner,lifecycle.account_id,lifecycle.binding.memory_workspace)
        refs,cancelled=await _completed_turn_thread(current)
        if cancelled: raise asyncio.CancelledError
        session=pending(lifecycle)
        if session is None: return False
        def read_current():
            barrier.assert_live_admission(lifecycle.owner, admission)
            return current()==refs
        await reconcile(session,lifecycle=lifecycle,policy_projection=projection,current=read_current)
        # The provider validates whether the exact retained original is eligible;
        # settled or unknown originals are never treated as a new send.
        if pending(lifecycle) is session:
            await cancel_original(session,lifecycle=lifecycle,current=read_current)
        after,cancelled=await _completed_turn_thread(current)
        if cancelled: raise asyncio.CancelledError
        return refs==after
    task=asyncio.create_task(owned());cancelled=False
    try:
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: cancelled=True
        if cancelled:
            try: task.result()
            except BaseException: pass
            raise asyncio.CancelledError
        try: return task.result()
        except Exception: return False
    finally:
        release=asyncio.create_task(barrier.release(admission))
        while not release.done():
            try: await asyncio.shield(release)
            except asyncio.CancelledError: cancelled=True
        release.result()
        if cancelled and not task.cancelled(): raise asyncio.CancelledError


app.state.s08_rules_callback = None
app.state.s08_original_status_callback = _s08_original_status_callback


async def _maintenance_reconcile_callback(state, lifecycle, hint):
    """Publish one exact origin-ready pending artifact through its live owner."""
    from src.memery_external_sources import SourceRef, _source_ref_error
    from src.openclank.acp_bridge import ACPBridge
    from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner, canonical_source_provider
    from src.openclank.conversation_archive import TrustedMaintenanceReady
    if (state is not app.state or type(lifecycle) is not CompletedTurnLifecycleOwner or type(hint) is not dict
        or set(hint)!={'chat_id','message_id','content_hash','seq'} or type(hint['seq']) is not int or hint['seq']<1):
        return False
    owner=lifecycle.owner;account=lifecycle.account_id;provider=canonical_source_provider(state)
    if provider is not lifecycle.provider or _owner_request_fenced(owner):
        return False
    source_authority=lifecycle.source_authority;archive=source_authority._archive
    try:
        pool=state.mimo_supervisor.owner_lifecycle_pool()
        worker=pool.worker_for_owner(owner)
        bridge=worker._bridge if worker is not None and worker.is_alive() else None
        if not isinstance(bridge,ACPBridge): return False
        async with lifecycle.mapping.async_chat_admission_lock(hint['chat_id']) as guard:
            def owner_current():
                try:
                    lifecycle.mapping.validate_chat_guard(hint['chat_id'],guard)
                    lifecycle.profile.assert_current()
                    return (not _owner_request_fenced(owner) and auth_manager.account_id(owner)==account
                            and lifecycle.target_authority.resolve(account)==lifecycle.binding)
                except Exception:
                    return False
            page=await asyncio.to_thread(archive.read_trusted_maintenance_pending,owner=account,chat_id=hint['chat_id'],
                message_id=hint['message_id'],content_hash=hint['content_hash'],after=hint['seq']-1,limit=1,authorize=owner_current)
            if len(page['items'])!=1 or type(page['items'][0]) is not TrustedMaintenanceReady or page['items'][0].seq!=hint['seq']:
                return False
            ready=page['items'][0];payload=ready.payload();ref=SourceRef(**payload['proof']['source'])
            if (_source_ref_error(ref) or ref.account_id!=account or ref.chat_id!=hint['chat_id']
                or ref.message_id!=hint['message_id'] or ref.content_hash!=hint['content_hash']):
                return False
            child=await asyncio.to_thread(lifecycle.for_source,source=ref,guard=guard)
            if not owner_current(): return False
            proof,value=await bridge.resolve_ready_maintenance_summary(ready)
            if proof!=payload['proof'] or not owner_current(): return False
            child=await asyncio.to_thread(lifecycle.for_source,source=ref,guard=guard)
            if not owner_current(): return False
            publish=asyncio.create_task(provider._host_append_derived_compaction(ready=ready,value=value,lifecycle=child,
                source_owner=lifecycle,guard=guard,policy_projection=getattr(state,'memory_policy_projection',None)))
            try:
                await asyncio.shield(publish)
            except asyncio.CancelledError:
                while not publish.done():
                    try: await asyncio.shield(publish)
                    except asyncio.CancelledError: continue
                publish.result()
                raise
            def current():
                try:
                    if not owner_current(): return False
                    lifecycle.for_source(source=ref,guard=guard)
                    return owner_current()
                except Exception:
                    return False
            task=asyncio.create_task(asyncio.to_thread(archive.ack_trusted_maintenance_pending,ready=ready,current=current))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try: await asyncio.shield(task)
                    except asyncio.CancelledError: continue
                try: task.result()
                except Exception: pass
                raise
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


# Installed only when current-policy publication is serialized with every
# supported preference writer; a post-IPC check alone cannot prove that.
app.state.maintenance_reconcile_callback = None


async def _install_policy_publication_callbacks(state, profile, registry):
    """Install only after the live native child proves this exact capability."""
    from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner, canonical_source_provider
    from src.openclank.memory_job_admission import SingleHostProfile
    from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
    state.s08_rules_callback = None
    # Status is independently read-authorized for each valid registry owner.
    state.s08_original_status_callback = _s08_original_status_callback
    state.maintenance_reconcile_callback = None
    state.queued_reset_finalize = None
    if (state is not app.state or type(profile) is not SingleHostProfile or type(registry) is not dict
            or registry.get('state') != 'ready' or not registry.get('owners')):
        return False
    projection=getattr(state,'memory_policy_projection',None)
    coordinator=policy_coordinator()
    provider=canonical_source_provider(state)
    if (type(projection) is not PolicyProjection or coordinator is None or coordinator.projection is not projection
            or profile.provider is not provider or projection._preparation is not getattr(provider,'_guarded_preparation',None)):
        return False
    def validate():
        profile.assert_current()
        for lifecycle, _, _ in registry['owners']:
            if (type(lifecycle) is not CompletedTurnLifecycleOwner or lifecycle.profile is not profile
                    or lifecycle.provider is not provider or lifecycle.target_authority.resolve(lifecycle.account_id)!=lifecycle.binding):
                raise ValueError('policy owner unavailable')
            coordinator.checked_refs(lifecycle.owner,lifecycle.account_id,lifecycle.binding.memory_workspace)
        profile.assert_current()
    try:
        # Profile support is factual and may be negotiated before any user has
        # accepted a policy; the normal publication checks below remain intact.
        first=registry['owners'][0][0]
        await asyncio.to_thread(profile.assert_current)
        await provider._host_negotiate_s08_policy(owner=first.binding.memory_owner,workspace_id=first.binding.memory_workspace)
        await asyncio.to_thread(validate)
        capability=await provider._host_policy_publication_status(owner=first.binding.memory_owner,workspace_id=first.binding.memory_workspace)
        await asyncio.to_thread(validate)
        if not capability['policy_writer']: return False
        if capability['s08']:
            state.s08_rules_callback = _s08_rules_callback
            state.s08_original_status_callback = _s08_original_status_callback
        if capability['maintenance']: state.maintenance_reconcile_callback = _maintenance_reconcile_callback
        from src.openclank.memory_job_admission import ForegroundRuntime
        supervisor=getattr(state,'mimo_supervisor',None)
        runtime=getattr(supervisor,'_s05_foreground_runtime',None)
        if (getattr(supervisor,'_s05_profile',None) is profile and type(runtime) is ForegroundRuntime
            and runtime.profile is profile):
            await _refresh_queued_reset_capability(state,profile,provider,first.owner,first.account_id,
                first.binding,first.target_authority)
        return True
    except Exception:
        return False


app.state.install_policy_publication_callbacks = _install_policy_publication_callbacks


async def _s06_admitted_callback(bridge, identity, invocation_id, observations, proof):
    """Revalidate host-held recall evidence immediately before admitted writes."""
    from src.memory_provider import FoundObservation
    from src.memery_provider import _HostObservationProjection, _HostAdmittedObservation
    from src.memery_external_sources import CurrentHostUserSource
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_job_admission import TargetAuthority
    from src.openclank.memory_policy import STORE
    from src.openclank.session_map import OwnerSessionMap
    import hashlib
    try:
        if (type(observations) is not tuple or not 1 <= len(observations) <= 100
                or any(type(item) is not FoundObservation for item in observations)
                or type(proof) is not dict or set(proof) != {'toolCallID','attestationID','resultSHA256','dispatchID','bodySHA256'}):
            return False
        supervisor = app.state.mimo_supervisor
        if not await asyncio.to_thread(_s06_admitted_ready, app.state, supervisor): return False
        original = await bridge.resolve_managed_feedback_invocation(identity, invocation_id)
        owner = original.get('owner')
        worker = supervisor.worker_for_owner(owner)
        if (not owner or worker is None or worker.bridge is not bridge or _owner_request_fenced(owner)
                or (await bridge._handle_managed_tool_admit({**identity, 'toolName':'recall_memory', 'kind':'memory_read'})).get('allowed') is not True):
            return False
        source = observations[0].source
        if (any(item.source != source or item.owner != observations[0].owner or item.workspace_id != observations[0].workspace_id
                or item.invocation.recall_attempt_id != invocation_id for item in observations)
                or source.message_id != identity['rootOperationID'] or source.chat_id != original.get('stableChatID')
                or ('' if source.version == 2 and source.workspace_id is None else source.workspace_id) != original.get('authorityWorkspaceID')
                or original.get('ownerAccountID') != source.account_id
                or auth_manager.account_id(owner) != source.account_id
                or auth_manager.username_for_account_id(source.account_id) != owner):
            return False
        pool = supervisor.owner_lifecycle_pool()
        authority = TargetAuthority(auth_manager=auth_manager, supervisor=pool)
        target = authority.resolve(source.account_id)
        provider = canonical_source_provider(app.state)
        if (provider is not memory_provider or target.memory_owner != observations[0].owner
                or target.memory_workspace != observations[0].workspace_id): return False
        mapping = OwnerSessionMap(pool._runtime_home(owner) / 'session-map.json', owner)
        verifier = CurrentHostUserSource(owner=owner, auth_manager=auth_manager, session_factory=SessionLocal,
            archive=get_conversation_archive(), workspace_repository=getattr(app.state,'files_policy_repository',None), policy_store=STORE, guarded_preparation=getattr(getattr(getattr(app.state,'s05_profile',None),'provider',None),'_guarded_preparation',None))
        async def current(guard):
            mapping.validate_chat_guard(source.chat_id, guard)
            return (not _owner_request_fenced(owner) and await bridge.resolve_managed_feedback_invocation(identity, invocation_id) == original
                and auth_manager.account_id(owner) == source.account_id
                and (await bridge._handle_managed_tool_admit({**identity, 'toolName':'recall_memory', 'kind':'memory_read'})).get('allowed') is True
                and authority.resolve(source.account_id) == target and await asyncio.to_thread(_s06_admitted_ready, app.state, supervisor)
                and (await asyncio.to_thread(verifier.verify_feedback_origin, source)).get('state') == 'available')
        async with mapping.async_chat_admission_lock(source.chat_id) as guard:
            if not await current(guard): return False
            cursor = 'o:'; matches = []
            while cursor is not None:
                rows, following = await asyncio.to_thread(provider._host_source_refs, account_id=source.account_id,
                    chat_id=source.chat_id, message_id=source.message_id, target_authority=authority, after_job_id=cursor, limit=128)
                matches.extend(row for row in rows if type(row) is _HostObservationProjection and row.source == source)
                if len(matches) > 1 or (following is not None and (not following.startswith('o:') or following <= cursor)):
                    return False
                cursor = following
            if len(matches) != 1: return False
            for observation in observations:
                if not await current(guard): return False
                material = json.dumps([observation.owner, invocation_id, observation.memory.block_id, observation.memory.revision], ensure_ascii=False, separators=(',', ':')).encode()
                found_id = 'cs_s06found_' + hashlib.sha256(material).hexdigest()
                await provider._host_append_admitted(_HostAdmittedObservation(matches[0], found_id,
                    proof['toolCallID'], proof['attestationID'], proof['resultSHA256'], proof['dispatchID'], proof['bodySHA256']))
            return await current(guard)
    except Exception:
        return False


async def _managed_memery_search(caller_bridge, arguments):
    from src.memory_scope import chat_workspace
    owner = caller_bridge._owner
    workspace_id = chat_workspace()
    # Private call identity is minted by the active ACP turn, not by the
    # model or by the owner/workspace broker credential.
    import hashlib
    if (not isinstance(arguments, dict) or set(arguments) != {'identity','invocationID','search'}
        or type(arguments['identity']) is not dict or (arguments['invocationID'] is not None and type(arguments['invocationID']) is not str)
        or type(arguments['search']) is not dict or set(arguments['search']) - {'query','kind','scene','tier','limit'}
        or type(arguments['search'].get('query')) is not str or len(arguments['search']['query']) > 8192):
        raise HTTPException(status_code=400, detail="Invalid qualified recall request")
    identity, invocation_id, search = arguments['identity'], arguments['invocationID'], dict(arguments['search'])
    try:
        encoded = json.dumps(search, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(encoded.encode('utf-8')) > 16384:
            raise ValueError('request too large')
        supervisor = app.state.mimo_supervisor
        worker = supervisor.worker_for_owner(owner)
        bridge = worker.bridge if worker else None
        if bridge is not caller_bridge or not worker.is_alive():
            raise ValueError('recall authority unavailable')
        async def current():
            try:
                admitted = (await bridge.resolve_managed_feedback_invocation(identity, invocation_id)
                    if invocation_id is not None else await bridge.resolve_managed_recall_turn(identity))
                account_id = auth_manager.account_id(owner)
                if (supervisor.worker_for_owner(owner) is not worker or worker.bridge is not bridge or not worker.is_alive()
                        or admitted.get('ownerAccountID') != account_id or admitted.get('owner') != owner or not account_id
                        or auth_manager.username_for_account_id(account_id) != owner
                        or _owner_request_fenced(owner)):
                    raise ValueError('recall owner changed')
                policy = await bridge._handle_managed_tool_admit({**identity, 'toolName':'recall_memory', 'kind':'memory_read'})
                if policy.get('allowed') is not True:
                    raise ValueError('recall permission changed')
                return admitted
            except Exception as exc:
                raise PermissionError('recall authority unavailable') from exc
        original = await current()
        async def execute():
            async with _account_request_barrier.admitted(owner):
                if await current() != original:
                    raise ValueError('recall binding changed')
                # Source qualification is optional; it never grants recall.
                # The same owner/chat admission guard serializes successful
                # observation writes with edits, vetoes and deletion.
                source_context = None
                try:
                    if invocation_id is None:
                        raise ValueError('observation invocation unavailable')
                    from core.database import SessionLocal
                    from sqlalchemy import text as sql_text
                    from src.memery_external_sources import CurrentHostUserSource, SourceRef
                    from src.openclank.chat_source_bridge import canonical_source_provider, origin_available, _require_reset_origin_indexes
                    from src.openclank.conversation_archive import get_conversation_archive
                    from src.openclank.memory_job_admission import TargetAuthority, SingleHostProfile, _conn
                    from src.openclank.memory_policy import STORE
                    from src.openclank.session_map import OwnerSessionMap
                    profile = getattr(app.state, 's05_profile', None)
                    pool = supervisor.owner_lifecycle_pool()
                    source_provider = canonical_source_provider(app.state)
                    # Only the selected provider's exact returned values can
                    # carry revision references; native recall stays native.
                    if (type(profile) is not SingleHostProfile or source_provider is None
                        or profile.provider is not source_provider or memory_provider is not source_provider
                        or source_provider._guarded_preparation.capabilities.get('source_erase') is not True):
                        raise ValueError('observation source capability unavailable')
                    profile.assert_current()
                    if _owner_request_fenced(owner):
                        raise PermissionError('observation reset is active')
                    def capability():
                        from services.memory.nuke_coordinator import MemoryNukeCoordinator
                        factory = getattr(app.state, 'memory_reset_coordinator', None)
                        coordinator = factory() if callable(factory) else None
                        if type(coordinator) is not MemoryNukeCoordinator or coordinator.memory_provider is not source_provider:
                            return False
                        with SessionLocal() as host_db:
                            if not origin_available(host_db):
                                return False
                            _require_reset_origin_indexes(host_db)
                        with _conn(profile.path) as db:
                            return (source_provider._s06_source_capability(db) and source_provider._s06_erase_capability(db)
                                and db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='s06_observation_rebind_successor'").fetchone() is not None)
                    if not await asyncio.to_thread(capability):
                        raise ValueError('observation source capability unavailable')
                    account = auth_manager.account_id(owner)
                    if not account or original.get('ownerAccountID') != account:
                        raise ValueError('observation account unavailable')
                    authority = TargetAuthority(auth_manager=auth_manager, supervisor=pool)
                    target = authority.resolve(account)
                    if (not account or target.memory_owner != source_provider._scope(owner).owner
                        or target.memory_workspace != workspace_id):
                        raise PermissionError('observation target changed')
                    mapping = OwnerSessionMap(pool._runtime_home(owner) / 'session-map.json', owner)
                    verifier = CurrentHostUserSource(owner=owner, auth_manager=auth_manager, session_factory=SessionLocal,
                        archive=get_conversation_archive(), workspace_repository=getattr(app.state,'files_policy_repository',None), policy_store=STORE, guarded_preparation=getattr(getattr(getattr(app.state,'s05_profile',None),'provider',None),'_guarded_preparation',None))
                    chat_id = original.get('stableChatID')
                    if type(chat_id) is not str or not chat_id:
                        raise ValueError('observation chat unavailable')
                    def source_ref():
                        with SessionLocal() as db:
                            row = db.execute(sql_text("SELECT m.source_body_revision,m.source_body_hash,s.workspace_id FROM chat_messages m JOIN sessions s ON s.id=m.session_id WHERE m.id=:message AND m.session_id=:chat AND m.role='user'"),
                                {'message':identity['rootOperationID'],'chat':chat_id}).first()
                        if not row or row[0] != 1 or type(row[1]) is not str or (row[2] is not None and type(row[2]) is not str):
                            return None
                        if (row[2] if row[2] is not None else '') != original.get('authorityWorkspaceID'):
                            raise PermissionError('observation workspace changed')
                        return SourceRef(2 if row[2] is None else 1,'host_user_body',account,chat_id,'main',identity['rootOperationID'],'body',row[0],row[1],row[2],None)
                    async with mapping.async_chat_admission_lock(chat_id) as guard:
                        if await current() != original or authority.resolve(account) != target:
                            raise PermissionError('observation binding changed')
                        ref = await asyncio.to_thread(source_ref)
                        status = await asyncio.to_thread(verifier.verify_feedback_origin,ref) if ref else {'state':'unavailable'}
                        if status.get('state') == 'denied':
                            raise PermissionError('observation privacy denied')
                        if status.get('state') == 'available':
                            mapping.validate_chat_guard(chat_id,guard)
                            source_context = (mapping,authority,target,verifier,ref,profile,source_provider)
                except PermissionError:
                    raise
                except Exception:
                    source_context = None
                limit = search.get('limit',10)
                qualified = (source_context is not None and search.get('tier') in (None,'curated')
                    and bool(search['query']) and type(limit) is int and 1 <= limit <= 100)
                if qualified:
                    result, refs = await source_provider._host_qualified_search(owner=target.memory_owner, workspace_id=workspace_id,
                        query=search['query'], limit=limit)
                else:
                    from src.memory_provider import NativeMemoryProvider
                    if isinstance(memory_provider, NativeMemoryProvider):
                        if (search.get('kind') is not None or search.get('scene') is not None
                                or search.get('tier') not in (None, 'curated')
                                or type(limit) is not int or not 1 <= limit <= 100):
                            raise ValueError('native recall filters are unavailable')
                        hits = await memory_provider.recall(search['query'], owner=owner, top_k=limit)
                        result = {'results':[{'record':{'id':hit.memory.id, 'content':hit.memory.text,
                            'kind':hit.memory.kind, 'source':hit.memory.source, 'owner':hit.memory.owner,
                            'metadata':hit.memory.metadata}, 'score':hit.score, 'source_label':'native'} for hit in hits],
                            'total':len(hits)}
                    else:
                        result = await memory_provider.invoke_tool('search', {**search, 'owner':owner, 'workspace_id':workspace_id})
                    refs = ()
                if await current() != original:
                    raise ValueError('recall binding changed')
                from src.openclank.memory_recall_format import format_qualified_search
                label = None
                label_available = False
                try:
                    label_reader = getattr(memory_provider, 'handler_display_label', None)
                    if callable(label_reader):
                        label = await label_reader(owner=owner)
                        label_available = True
                except Exception:
                    label = None
                qualified_text, unchanged = format_qualified_search(result, label, render_identity=label_available)
                observations = []
                if source_context is not None:
                    mapping, authority, target, verifier, ref, profile, source_provider = source_context
                    async with mapping.async_chat_admission_lock(ref.chat_id) as guard:
                        if (await current() != original or auth_manager.account_id(owner) != ref.account_id or authority.resolve(ref.account_id) != target
                                or _owner_request_fenced(owner)):
                            raise ValueError('recall binding changed')
                        profile.assert_current()
                        source_status = await asyncio.to_thread(verifier.verify_feedback_origin,ref)
                        if source_status.get('state') == 'denied':
                            raise PermissionError('recall privacy denied')
                        from src.memory_provider import FoundObservation, RecallInvocation
                        invocation = RecallInvocation(identity['sessionID'],identity['rootOperationID'],identity['actorID'],
                            identity['executionRevision'],identity['innerTurnID'],invocation_id)
                        for index, memory_ref in enumerate(refs if source_status.get('state') == 'available' else ()):
                            if memory_ref is None or index >= len(unchanged) or not unchanged[index]:
                                continue
                            if (await current() != original or auth_manager.account_id(owner) != ref.account_id or authority.resolve(ref.account_id) != target
                                    or _owner_request_fenced(owner)):
                                raise ValueError('recall binding changed')
                            event_source = await asyncio.to_thread(verifier.verify_feedback_origin,ref)
                            if event_source.get('state') == 'denied':
                                raise PermissionError('recall privacy denied')
                            if event_source.get('state') != 'available':
                                break
                            try:
                                observation = FoundObservation(target.memory_owner,workspace_id,ref,invocation,memory_ref)
                                await source_provider._host_append_found(observation)
                                observations.append(observation)
                            except Exception:
                                logger.warning('memory feedback observation unavailable')
                            mapping.validate_chat_guard(ref.chat_id,guard)
                        if (await current() != original or auth_manager.account_id(owner) != ref.account_id or authority.resolve(ref.account_id) != target
                                or _owner_request_fenced(owner)):
                            raise ValueError('recall binding changed')
                attestation = None
                if observations:
                    try:
                        if await current() != original:
                            raise PermissionError('recall authority changed')
                        attestation = await bridge.issue_managed_recall_attestation(identity, invocation_id,
                            qualified_text, tuple(observations))
                    except PermissionError:
                        raise
                    except Exception:
                        logger.warning('memory admission attestation unavailable')
                return {'qualified_text':qualified_text, 'admission_attestation':attestation}
        result = (await bridge.run_managed_feedback_call(identity, invocation_id,
            hashlib.sha256(encoded.encode('utf-8')).hexdigest(), execute)
            if invocation_id is not None else await execute())
        async with _account_request_barrier.admitted(owner):
            if await current() != original or owner not in getattr(auth_manager, 'users', {}):
                raise ValueError('recall binding changed')
            return result
    except Exception:
        raise HTTPException(status_code=403, detail="Qualified recall unavailable") from None


app.state.managed_memery_search = _managed_memery_search


@app.post("/api/internal/memery/tool", include_in_schema=False)
async def internal_memery_tool(request: Request, body: Dict[str, object]):
    """Private loopback broker for the single app-supervised Memery worker."""
    token = request.headers.get(INTERNAL_TOOL_HEADER, "")
    owner = (request.headers.get(INTERNAL_TOOL_OWNER_HEADER) or "").strip()
    workspace_id = (
        request.headers.get(INTERNAL_TOOL_WORKSPACE_HEADER) or ""
    ).strip()
    try:
        expected_token = memery_broker_token(
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
            owner not in getattr(auth_manager, "users", {})
        )
    ):
        raise HTTPException(status_code=403, detail="Internal memory broker only")
    name = str(body.get("name") or "")
    arguments = body.get("arguments")
    from src.memery_provider import BROKER_MEMORY_TOOLS

    if name == "qualified_project_policy":
        # Resolution only: no Memory read, source receipt, or policy mutation.
        if (type(arguments) is not dict or set(arguments) != {'identity','invocationID','toolName','toolCallID','workspace'}
            or type(arguments['identity']) is not dict or any(type(arguments[k]) is not str or not arguments[k] or len(arguments[k])>4096 for k in ('invocationID','toolName','toolCallID','workspace'))):
            raise HTTPException(status_code=400, detail="Invalid policy qualification")
        try:
            supervisor=request.app.state.mimo_supervisor
            worker=supervisor.worker_for_owner(owner); bridge=worker.bridge if worker else None
            if bridge is None or not worker.is_alive(): raise ValueError('worker unavailable')
            async with _account_request_barrier.admitted(owner):
                result=await bridge.resolve_managed_policy_invocation(arguments['identity'],arguments['invocationID'],arguments['toolName'],arguments['toolCallID'])
                account=auth_manager.account_id(owner)
                if (not account or auth_manager.username_for_account_id(account)!=owner or result.get('owner')!=owner
                    or result.get('ownerAccountID')!=account or _owner_request_fenced(owner)
                    or not isinstance(result.get('canonicalCwd'),str)
                    or os.path.realpath(result['canonicalCwd'])!=os.path.realpath(arguments['workspace'])
                    or supervisor.worker_for_owner(owner) is not worker or worker.bridge is not bridge or not worker.is_alive()):
                    raise ValueError('policy scope changed')
                decision=await bridge._handle_managed_tool_admit({**arguments['identity'],'toolName':arguments['toolName'],'kind':'project_policy_read'})
                if decision.get('allowed') is not True or await bridge.resolve_managed_policy_invocation(arguments['identity'],arguments['invocationID'],arguments['toolName'],arguments['toolCallID'])!=result:
                    raise ValueError('policy authority changed')
                if (auth_manager.account_id(owner)!=account or auth_manager.username_for_account_id(account)!=owner
                    or _owner_request_fenced(owner) or supervisor.worker_for_owner(owner) is not worker
                    or worker.bridge is not bridge or not worker.is_alive()):
                    raise ValueError('policy owner changed')
                return {'version':1,'identity':arguments['identity'],'invocationID':arguments['invocationID'],
                    'toolName':arguments['toolName'],'toolCallID':arguments['toolCallID'],'owner':owner,'accountID':account,
                    'stableChatID':result['stableChatID'],'authorityWorkspaceID':result['authorityWorkspaceID'],
                    'mappingRevision':result['mappingRevision'],'workspaceRevision':result['workspaceRevision'],
                    'mapRevision':result['mapRevision'],'policyRevision':result['policyRevision']}
        except Exception as exc:
            raise HTTPException(status_code=409, detail="Policy qualification unavailable") from exc

    if name == "qualified_skill_use":
        # Resolution only: no Memory read, source receipt, or policy mutation.
        if (type(arguments) is not dict or set(arguments) != {'identity','invocationID','toolName','toolCallID','workspace'}
            or arguments.get('toolName') != 'skill'
            or type(arguments['identity']) is not dict or any(type(arguments[k]) is not str or not arguments[k] or len(arguments[k])>4096 for k in ('invocationID','toolName','toolCallID','workspace'))):
            raise HTTPException(status_code=400, detail="Invalid skill qualification")
        try:
            supervisor=request.app.state.mimo_supervisor
            worker=supervisor.worker_for_owner(owner); bridge=worker.bridge if worker else None
            if bridge is None or not worker.is_alive(): raise ValueError('worker unavailable')
            async with _account_request_barrier.admitted(owner):
                result=await bridge.resolve_managed_policy_invocation(arguments['identity'],arguments['invocationID'],arguments['toolName'],arguments['toolCallID'])
                account=auth_manager.account_id(owner)
                key=bridge._managed_turn_key(arguments['identity'])
                async with bridge._managed_execution_lock:
                    original=bridge._managed_actor_turns.get(key)
                    execution=bridge._managed_actor_executions.get(key[:4])
                    admission=original.get('ownerAdmission') if isinstance(original,dict) else None
                    original_barrier=original.get('ownerAdmissionBarrier') if isinstance(original,dict) else None
                    credential=original.get('originalCredentialCurrent') if isinstance(original,dict) else None
                if (original is not execution or not isinstance(original,dict) or original.get('state')!='active'
                    or original_barrier is not _account_request_barrier or admission is None
                    or original.get('ownerAdmissionAccount')!=account or not callable(credential)
                    or credential() is not True or not await original_barrier.is_live(owner,admission)):
                    raise ValueError('skill original admission unavailable')
                if (not account or auth_manager.username_for_account_id(account)!=owner or result.get('owner')!=owner
                    or result.get('ownerAccountID')!=account or _owner_request_fenced(owner)
                    or not isinstance(result.get('canonicalCwd'),str)
                    or os.path.realpath(result['canonicalCwd'])!=os.path.realpath(arguments['workspace'])
                    or supervisor.worker_for_owner(owner) is not worker or worker.bridge is not bridge or not worker.is_alive()):
                    raise ValueError('skill scope changed')
                decision=await bridge._handle_managed_tool_admit({**arguments['identity'],'toolName':arguments['toolName'],'kind':'skill_use'})
                if decision.get('allowed') is not True or await bridge.resolve_managed_policy_invocation(arguments['identity'],arguments['invocationID'],arguments['toolName'],arguments['toolCallID'])!=result:
                    raise ValueError('skill authority changed')
                if (bridge._managed_actor_turns.get(key) is not original or bridge._managed_actor_executions.get(key[:4]) is not original
                    or original.get('state')!='active' or original.get('ownerAdmission') is not admission
                    or original.get('ownerAdmissionBarrier') is not original_barrier or original.get('originalCredentialCurrent') is not credential
                    or credential() is not True or not await original_barrier.is_live(owner,admission)
                    or auth_manager.account_id(owner)!=account or auth_manager.username_for_account_id(account)!=owner
                    or _owner_request_fenced(owner) or supervisor.worker_for_owner(owner) is not worker
                    or worker.bridge is not bridge or not worker.is_alive()):
                    raise ValueError('skill owner changed')
                return {'version':1,'identity':arguments['identity'],'invocationID':arguments['invocationID'],
                    'toolName':arguments['toolName'],'toolCallID':arguments['toolCallID'],'owner':owner,'accountID':account,
                    'stableChatID':result['stableChatID'],'authorityWorkspaceID':result['authorityWorkspaceID'],
                    'mappingRevision':result['mappingRevision'],'workspaceRevision':result['workspaceRevision'],
                    'mapRevision':result['mapRevision'],'policyRevision':result['policyRevision']}
        except Exception as exc:
            raise HTTPException(status_code=409, detail="Skill qualification unavailable") from exc

    if name in ("control_lease", "control_open_resource", "control_capture_observation", "control_selected_snapshot", "control_selected_current"):
        # Host-side target state for desktop control (leases, observations, holds, takeover epoch); owner comes from the authenticated request.
        if type(arguments) is not dict or type(arguments.get('identity')) is not dict or type(arguments.get('request', {})) is not dict:
            raise HTTPException(status_code=400, detail="Invalid control lease request")
        try:
            supervisor=request.app.state.mimo_supervisor
            worker=supervisor.worker_for_owner(owner); bridge=worker.bridge if worker else None
            if bridge is None or not worker.is_alive(): raise ValueError('worker unavailable')
            async with _account_request_barrier.admitted(owner):
                if name == "control_lease" and arguments['request'].get('op') == 'helper_takeover':
                    # Token-bound OS takeover only: revokes existing authority and never grants action access.
                    account = auth_manager.account_id(owner)
                    if (not account or auth_manager.username_for_account_id(account) != owner or _owner_request_fenced(owner)
                        or supervisor.worker_for_owner(owner) is not worker or worker.bridge is not bridge or not worker.is_alive()):
                        raise ValueError('control owner changed')
                    from src.openclank.computer_control import hoststate as _hoststate
                    return {'version': 1, 'identity': arguments['identity'], **_hoststate.handle_broker(
                        _hoststate.STATE, owner, arguments['identity'], arguments['request'], account=account, holder=worker)}
                turn=await bridge.resolve_managed_control_turn(arguments['identity'])
                account=auth_manager.account_id(owner)
                if (turn.get('owner')!=owner or not account or auth_manager.username_for_account_id(account)!=owner
                    or turn.get('ownerAccountID')!=account or _owner_request_fenced(owner)):
                    raise ValueError('control scope changed')
                if name == "control_capture_observation":
                    import re as _re, tempfile as _tempfile
                    from src.openclank.computer_control import hoststate as _hoststate
                    from src.openclank.computer_control.control_frame import acquire as _acquire_control_frame
                    from src.openclank.file_policy import FilePolicyRepository
                    from src.openclank.workspace_policy_service import resolve_owned_workspace
                    from src.desktop_capture import capture_enabled, host_files_importer
                    from src.settings import get_user_setting
                    frame_token=arguments['request'].get('frame_token')
                    if (set(arguments['request'])!={'frame_token'} or not isinstance(frame_token,str)
                        or _re.fullmatch(r'[0-9a-f]{64}',frame_token) is None or not capture_enabled(owner)
                        or get_user_setting('desktop_control_enabled',owner,False) is not True):
                        raise ValueError('control frame unavailable')
                    identity=arguments['identity'];key=bridge._managed_turn_key(identity)
                    async with bridge._managed_execution_lock:
                        entry=bridge._managed_actor_turns.get(key)
                        if not isinstance(entry,dict) or entry.get('state')!='active':raise ValueError('control frame unavailable')
                        snapshot={k:entry.get(k) for k in ('owner','ownerAccountID','stableChatID','authorityWorkspaceID','mappingRevision','workspaceRevision','mapRevision','policyRevision','holderID')}
                    execution=await bridge.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
                    binding=await bridge._handle_session_binding_read({'sessionID':execution['rootSessionID']})
                    if (execution.get('authority') or binding.get('authority') or execution.get('holderID')!=snapshot['holderID']
                        or any(binding.get(k)!=snapshot[k] for k in ('owner','stableChatID','authorityWorkspaceID','mappingRevision','workspaceRevision'))
                        or type(snapshot['mapRevision']) is not int or type(binding.get('mapRevision')) is not int or snapshot['mapRevision']>binding['mapRevision']
                        or not isinstance(binding.get('canonicalCwd'),str) or not snapshot['authorityWorkspaceID']):raise ValueError('control frame unavailable')
                    await bridge.resolve_managed_control_turn(identity,tool_name='capture_desktop')
                    from src.constants import APP_DB
                    repo=FilePolicyRepository(str(os.environ.get('OPEN_CLANK_AUTHORITY_DB_PATH') or APP_DB))
                    workspace=resolve_owned_workspace(repo,workspace_id=snapshot['authorityWorkspaceID'],owner_username=owner,auth_manager=auth_manager,purpose='agent_workspace')
                    if os.path.realpath(workspace.path)!=os.path.realpath(binding['canonicalCwd']):raise ValueError('control frame unavailable')
                    policy_generation=repo.generation();scope=(owner,identity['sessionID'],identity['actorID'],account,identity['rootOperationID'],identity['executionRevision'],identity['innerTurnID'])
                    waiter=_hoststate.STATE.control_frame_waiter(frame_token,scope,worker)
                    claim=None;admitted=False;dispatched=False
                    async def current():
                        try:
                            if (claim is None or not _hoststate.STATE.control_frame_current(claim) or not capture_enabled(owner)
                                or get_user_setting('desktop_control_enabled',owner,False) is not True
                                or _owner_request_fenced(owner) or auth_manager.account_id(owner)!=account or auth_manager.username_for_account_id(account)!=owner
                                or supervisor.worker_for_owner(owner) is not worker or worker.bridge is not bridge or not worker.is_alive() or repo.generation()!=policy_generation):return False
                            if await bridge.resolve_managed_control_turn(identity)!=turn:return False
                            await bridge.resolve_managed_control_turn(identity,tool_name='capture_desktop')
                            live=await bridge.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
                            current_binding=await bridge._handle_session_binding_read({'sessionID':live['rootSessionID']})
                            current_workspace=resolve_owned_workspace(repo,workspace_id=snapshot['authorityWorkspaceID'],owner_username=owner,auth_manager=auth_manager,purpose='agent_workspace')
                            async with bridge._managed_execution_lock:
                                actual=bridge._managed_actor_turns.get(key)
                                return (actual is entry and entry.get('state')=='active' and all(entry.get(k)==v for k,v in snapshot.items())
                                    and live==execution and current_binding==binding and current_workspace==workspace)
                        except Exception:return False
                    async def capture_work():
                        nonlocal claim,admitted,dispatched
                        try:
                            if not await asyncio.to_thread(waiter.wait,3):return {'state':'unavailable','code':'control_frame_unavailable'}
                            claim=_hoststate.STATE.claim_control_frame(frame_token,scope,worker)
                            data=await _acquire_control_frame(claim,current)
                            if data is None or not await current():return {'state':'unavailable','code':'control_frame_unavailable'}
                            importer=host_files_importer(owner=owner,account_id=account,workspace=workspace.path,repository=repo,current=current)
                            with _tempfile.TemporaryDirectory(prefix='openclank-control-frame-') as directory:
                                path=Path(directory)/'frame.png';path.write_bytes(data)
                                if not await current():return {'state':'unavailable','code':'control_frame_unavailable'}
                                dispatched=True
                                resource=await importer(path,owner=owner,metadata={'kind':'desktop_capture','owner':owner,'source':'control_stream','target':None,'ocr':None})
                            if not await current():return {'state':'unknown','code':'control_frame_outcome_unknown'}
                            note=_hoststate.STATE.admit_control_frame(claim,owner,identity['sessionID'],identity['actorID'],*claim['size'],resource.get('resource_key'))
                            if note is None:return {'state':'unknown','code':'control_frame_outcome_unknown'}
                            admitted=True
                            return {'state':'captured','resource':resource,'source':'control_stream','target':None,'ocr':None,'pixel_size':list(claim['size']),
                                'observation_id':note['observationId'],'observation_valid_s':note['maxAgeS']}
                        except Exception:
                            return {'state':'unknown' if dispatched else 'unavailable','code':'control_frame_unavailable'}
                        finally:
                            if not admitted:
                                if claim is not None:_hoststate.STATE.cancel_control_frame(claim)
                                else:_hoststate.STATE.cancel_control_frame_token(frame_token,scope,worker)
                    task=asyncio.create_task(capture_work())
                    try: result=await asyncio.shield(task)
                    except asyncio.CancelledError:
                        _hoststate.STATE.cancel_control_frame_token(frame_token,scope,worker)
                        while not task.done():
                            try: await asyncio.shield(task)
                            except asyncio.CancelledError: continue
                            except Exception: break
                        try: task.result()
                        except BaseException: pass
                        raise
                    return {'version':1,'identity':identity,'result':result}
                if name == "control_selected_current":
                    from src.openclank.computer_control import hoststate as _hoststate
                    body=arguments['request'];identity=arguments['identity'];registration=body.get('registration')
                    if set(body)!={'registration'} or not isinstance(registration,str):raise ValueError('selected file unavailable')
                    scope=(owner,identity['sessionID'],identity['actorID'],account,identity['rootOperationID'],identity['executionRevision'],identity['innerTurnID'])
                    reservation=_hoststate.STATE.selected_snapshot_for(registration,scope,worker)
                    recheck=reservation.get('recheck') if reservation else None
                    valid=bool(callable(recheck) and await recheck())
                    return {'version':1,'identity':identity,'current':valid}
                if name == "control_selected_snapshot":
                    # The authenticated child has consumed the exact action approval;
                    # the Host independently binds the actor, workspace and Agent Files lane.
                    import re as _re
                    from src.openclank.computer_control import hoststate as _hoststate
                    from src.openclank.computer_control.selected_file_snapshot import acquire as _selected_snapshot, materialize as _materialize_selected
                    from src.openclank.files_facade import ProviderContext
                    from src.openclank.files_service_client import AgentFilesClient
                    from src.openclank.workspace_policy_service import resolve_owned_workspace
                    from src.settings import get_user_setting
                    body=arguments['request']; identity=arguments['identity']
                    if (set(body)!={'registration','resource_ref','expected_revision'}
                        or not isinstance(body['registration'],str) or _re.fullmatch(r'[0-9a-f]{64}',body['registration']) is None
                        or not isinstance(body['resource_ref'],str) or not isinstance(body['expected_revision'],dict)
                        or get_user_setting('desktop_control_enabled',owner,False) is not True
                        or get_user_setting('desktop_accessibility_enabled',owner,False) is not True):
                        raise ValueError('selected file unavailable')
                    await bridge.resolve_managed_control_turn(identity,tool_name='control_desktop')
                    key=bridge._managed_turn_key(identity)
                    async with bridge._managed_execution_lock:
                        entry=bridge._managed_actor_turns.get(key)
                        if not isinstance(entry,dict) or entry.get('state')!='active':raise ValueError('selected file unavailable')
                        snapshot={k:entry.get(k) for k in ('owner','ownerAccountID','stableChatID','authorityWorkspaceID','mappingRevision','workspaceRevision','mapRevision','policyRevision','holderID')}
                    execution=await bridge.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
                    binding=await bridge._handle_session_binding_read({'sessionID':execution['rootSessionID']})
                    repo=request.app.state.files_policy_repository
                    manager=request.app.state.session_manager
                    factory=getattr(request.app.state,'files_facade_state_factory',None)
                    chat=snapshot['stableChatID'];workspace_id=snapshot['authorityWorkspaceID']
                    session=manager.get_session(chat) if chat else None
                    if (execution.get('authority') or binding.get('authority') or execution.get('holderID')!=snapshot['holderID']
                        or any(binding.get(k)!=snapshot[k] for k in ('owner','stableChatID','authorityWorkspaceID','mappingRevision','workspaceRevision'))
                        or type(snapshot['mapRevision']) is not int or type(binding.get('mapRevision')) is not int or snapshot['mapRevision']>binding['mapRevision']
                        or not isinstance(binding.get('canonicalCwd'),str) or not workspace_id or not callable(factory)
                        or session is None or session.owner!=owner or getattr(session,'workspace_id',None)!=workspace_id
                        or getattr(session,'incognito',False) or getattr(session,'archived',False)):
                        raise ValueError('selected file unavailable')
                    workspace=resolve_owned_workspace(repo,workspace_id=workspace_id,owner_username=owner,auth_manager=auth_manager,purpose='agent_workspace',observation_only=True)
                    if os.path.realpath(workspace.path)!=os.path.realpath(binding['canonicalCwd']):raise ValueError('selected file unavailable')
                    generation=repo.generation()
                    scope=(owner,identity['sessionID'],identity['actorID'],account,identity['rootOperationID'],identity['executionRevision'],identity['innerTurnID'])
                    reservation=_hoststate.STATE.reserve_selected_snapshot(body['registration'],scope,worker)
                    cancelled=asyncio.Event()
                    async def current_selected():
                        try:
                            if (cancelled.is_set() or not _hoststate.STATE.selected_snapshot_current(reservation)
                                or _owner_request_fenced(owner) or auth_manager.account_id(owner)!=account or auth_manager.username_for_account_id(account)!=owner
                                or supervisor.worker_for_owner(owner) is not worker or worker.bridge is not bridge or not worker.is_alive()
                                or repo.generation()!=generation or request.app.state.files_facade_state_factory is not factory
                                or get_user_setting('desktop_control_enabled',owner,False) is not True
                                or get_user_setting('desktop_accessibility_enabled',owner,False) is not True
                                or manager.get_session(chat) is not session or session.owner!=owner or getattr(session,'workspace_id',None)!=workspace_id
                                or getattr(session,'incognito',False) or getattr(session,'archived',False)):return False
                            if await bridge.resolve_managed_control_turn(identity)!=turn:return False
                            await bridge.resolve_managed_control_turn(identity,tool_name='control_desktop')
                            live=await bridge.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
                            live_binding=await bridge._handle_session_binding_read({'sessionID':live['rootSessionID']})
                            live_workspace=resolve_owned_workspace(repo,workspace_id=workspace_id,owner_username=owner,auth_manager=auth_manager,purpose='agent_workspace',observation_only=True)
                            async with bridge._managed_execution_lock:
                                actual=bridge._managed_actor_turns.get(key)
                                return (actual is entry and entry.get('state')=='active' and all(entry.get(k)==v for k,v in snapshot.items())
                                    and live==execution and live_binding==binding and live_workspace==workspace)
                        except Exception:return False
                    async def selected_thread(fn,*args):
                        task=asyncio.create_task(asyncio.to_thread(fn,*args))
                        try:return await asyncio.shield(task)
                        except asyncio.CancelledError:
                            while not task.done():
                                try:await asyncio.shield(task)
                                except asyncio.CancelledError:continue
                                except Exception:break
                            try:
                                abandoned=task.result()
                                if callable(getattr(abandoned,'close',None)):abandoned.close()
                            except BaseException:pass
                            raise
                    async def selected_work():
                        fd=None;client=None;named=None
                        try:
                            if not await current_selected():return None
                            context=ProviderContext(account,owner,generation,bool(auth_manager.is_admin(owner)),workspace_id)
                            facade=factory(workspace.path)
                            client=AgentFilesClient(owner,workspace.path,workspace_id=workspace_id,chat_id=chat)
                            result=await _selected_snapshot(facade,context,client,body['resource_ref'],body['expected_revision'],current_selected)
                            fd=result.fd
                            if not await current_selected():return None
                            named=await selected_thread(_materialize_selected,result)
                            os.close(fd);fd=None
                            if not await current_selected() or not await selected_thread(named.current):return None
                            async def source_recheck():
                                probe=None
                                try:
                                    if not await current_selected():return False
                                    held=reservation.get('snapshot')
                                    if held is None or not await selected_thread(held.current):return False
                                    from src.openclank.files_host_provider import _path
                                    provider,ref=facade._provider_for_ref(context,body['resource_ref'],capability='download')
                                    stat_provider,stat_ref=facade._provider_for_ref(context,body['resource_ref'],capability='stat')
                                    if provider is not stat_provider or ref!=stat_ref or ref.provider!='host':return False
                                    probe=AgentFilesClient(owner,workspace.path,workspace_id=workspace_id,chat_id=chat)
                                    response=await probe.request('stat',_path(ref.origin_id),{'include_fingerprint':False,'object_identity':True})
                                    data=response.get('data') if isinstance(response,dict) else None
                                    return bool(isinstance(data,dict) and isinstance(data.get('kind'),str) and data['kind'].lower()=='file' and type(data.get('size')) is int
                                        and data['size']==result.size and data.get('object_tag')==result.object_tag and await current_selected())
                                except Exception:return False
                                finally:
                                    if probe is not None:probe.close()
                            reservation['recheck']=source_recheck
                            private_path=_hoststate.STATE.admit_selected_snapshot(reservation,named);named=None
                            return {'path':private_path,'lease_max_seconds':60}
                        except Exception:return None
                        finally:
                            if client is not None:client.close()
                            if fd is not None:os.close(fd)
                            if named is not None:named.close()
                    task=asyncio.create_task(selected_work())
                    try: result=await asyncio.shield(task)
                    except asyncio.CancelledError:
                        cancelled.set()
                        while not task.done():
                            try:await asyncio.shield(task)
                            except asyncio.CancelledError:continue
                            except Exception:break
                        # If the path was issued, the owner keeps its bounded lease;
                        # otherwise retirement closes the empty reservation.
                        if reservation.get('snapshot') is None:_hoststate.STATE.retire_selected_snapshot(reservation)
                        raise
                    if result is None:_hoststate.STATE.retire_selected_snapshot(reservation)
                    return {'version':1,'identity':identity,'result':result}
                if name == "control_open_resource":
                    # The Files facade's open-on-host authority, for a resource the person chose; the Host wires the callable (see report).
                    opener=getattr(request.app.state,'agent_open_on_host',None)
                    ref,app_id=arguments['request'].get('resource_ref'),arguments['request'].get('app_id')
                    result={'ok':False,'code':'files_open_unavailable'} if not callable(opener) else await opener(owner,ref,app_id)
                    return {'version':1,'identity':arguments['identity'],'result':result}
                from src.openclank.computer_control import hoststate as _hoststate
                request_op = arguments['request'].get('op')
                if request_op in ('complete_windows_launch', 'complete_macos_launch'):
                    identity = arguments['identity']
                    token = arguments['request'].get('reservation')
                    session, actor = str(identity.get('sessionID', '')), str(identity.get('actorID', ''))
                    turn_scope = (account, identity.get('rootOperationID'), identity.get('executionRevision'), identity.get('innerTurnID'))
                    if not isinstance(token, str): raise ValueError('invalid launch reservation')
                    record = _hoststate.STATE.prepare_launch(owner, session, actor, token, turn_scope)
                    try:
                        if not await asyncio.to_thread(_hoststate.STATE.wait_launch_ready, record):
                            raise ValueError('launch proof expired')
                        current_turn = await bridge.resolve_managed_control_turn(identity)
                        if (current_turn != turn or auth_manager.account_id(owner) != account
                            or auth_manager.username_for_account_id(account) != owner or _owner_request_fenced(owner)
                            or supervisor.worker_for_owner(owner) is not worker or worker.bridge is not bridge or not worker.is_alive()):
                            raise ValueError('control authority changed during launch proof')
                        _hoststate.STATE.commit_launch(owner, session, actor, token, record, turn_scope)
                    except BaseException:
                        _hoststate.STATE.cancel_windows_launch(owner, session, actor, token, turn_scope)
                        raise
                    return {'version':1, 'identity':identity, 'ok':True}
                return {'version':1,'identity':arguments['identity'],**_hoststate.handle_broker(_hoststate.STATE,owner,arguments['identity'],arguments['request'],account=account,holder=worker,holder_current=lambda: supervisor.worker_for_owner(owner) is worker and worker.bridge is bridge and worker.is_alive() and auth_manager.account_id(owner) == account and auth_manager.username_for_account_id(account) == owner and not _owner_request_fenced(owner))}
        except Exception as exc:
            raise HTTPException(status_code=409, detail="Control lease unavailable") from exc

    if name in ("consume_control_approval", "control_settings"):
        # consume: one human approval bound to this exact desktop-control call (never mints one).
        # control_settings: read-only answer to "are control / accessibility reading switched on for this owner".
        import re as _re
        from src.openclank.computer_control.protocol import REGISTRY as _CONTROL_APPROVALS
        _settings_op = name == "control_settings"
        if _settings_op:
            if type(arguments) is not dict or set(arguments) != {'identity'} or type(arguments['identity']) is not dict:
                raise HTTPException(status_code=400, detail="Invalid control settings request")
            arguments = {**arguments, 'toolName': 'control_desktop'}
        elif (type(arguments) is not dict or set(arguments) != {'identity','toolName','argsSHA256'} or type(arguments['identity']) is not dict
            or arguments['toolName'] not in ('control_desktop', 'inspect_desktop') or type(arguments['argsSHA256']) is not str
            or not _re.fullmatch(r'[0-9a-f]{64}', arguments['argsSHA256'])):
            raise HTTPException(status_code=400, detail="Invalid control approval request")
        try:
            supervisor=request.app.state.mimo_supervisor
            worker=supervisor.worker_for_owner(owner); bridge=worker.bridge if worker else None
            if bridge is None or not worker.is_alive(): raise ValueError('worker unavailable')
            async with _account_request_barrier.admitted(owner):
                turn=await bridge.resolve_managed_control_turn(arguments['identity'], arguments['toolName'])
                account=auth_manager.account_id(owner)
                if (turn.get('owner')!=owner or not account or auth_manager.username_for_account_id(account)!=owner
                    or turn.get('ownerAccountID')!=account or _owner_request_fenced(owner)
                    or supervisor.worker_for_owner(owner) is not worker or worker.bridge is not bridge or not worker.is_alive()):
                    raise ValueError('control scope changed')
                from src.settings import get_user_setting
                # Control no longer needs the capture setting; reading accessibility trees has its own opt-in.
                control_on=bool(get_user_setting('desktop_control_enabled',owner,False))
                access_on=bool(get_user_setting('desktop_accessibility_enabled',owner,False))
                if _settings_op:
                    return {'version':1,'identity':arguments['identity'],'control':control_on,'accessibility':access_on,
                            'action_log':bool(get_user_setting('desktop_action_log_enabled',owner,False)),
                            'multi_agent':bool(get_user_setting('desktop_multi_agent_same_app_enabled',owner,False))}
                enabled=access_on if arguments['toolName']=='inspect_desktop' else control_on
                approved=_CONTROL_APPROVALS.consume(owner,arguments['identity']['sessionID'],arguments['argsSHA256'],enabled=enabled)
                return {'version':1,'identity':arguments['identity'],'argsSHA256':arguments['argsSHA256'],'approved':approved}
        except Exception as exc:
            raise HTTPException(status_code=409, detail="Control approval unavailable") from exc

    if name == "publish_requirement_check":
        # Read-only: answers whether a current standing requirement denies a model's public file link.
        if type(arguments) is not dict or set(arguments) != {'identity'} or type(arguments['identity']) is not dict:
            raise HTTPException(status_code=400, detail="Invalid publish requirement request")
        try:
            supervisor=request.app.state.mimo_supervisor
            worker=supervisor.worker_for_owner(owner); bridge=worker.bridge if worker else None
            if bridge is None or not worker.is_alive(): raise ValueError('worker unavailable')
            async with _account_request_barrier.admitted(owner):
                turn=await bridge.resolve_managed_control_turn(arguments['identity'],'publish_file')
                account=auth_manager.account_id(owner)
                if (turn.get('owner')!=owner or not account or auth_manager.username_for_account_id(account)!=owner
                    or turn.get('ownerAccountID')!=account or _owner_request_fenced(owner)
                    or supervisor.worker_for_owner(owner) is not worker or worker.bridge is not bridge or not worker.is_alive()):
                    raise ValueError('publish scope changed')
                return {'identity':arguments['identity'],**await _publish_requirement_answer(owner)}
        except Exception as exc:
            raise HTTPException(status_code=409, detail="Publish requirement check unavailable") from exc

    if name not in BROKER_MEMORY_TOOLS or not isinstance(arguments, dict):
        raise HTTPException(status_code=400, detail="Invalid memory tool request")
    if name == "code_query":
        # Code authority is purpose-tagged and session-bound in addition to
        # the existing global-memory transport credential. Model input never
        # supplies root, private native scope or an arbitrary other chat.
        from src.openclank.code_query import code_binding_token, query_code, finalize_error
        from src.openclank.transcript_projection import get_managed_binding
        from src.openclank.file_policy import FilePolicyRepository
        if set(arguments) not in ({"query", "session_id", "binding_revision"}, {"query", "session_id", "binding_revision", "outline_request"}) or not isinstance(arguments.get("query"), dict):
            raise HTTPException(status_code=400, detail="Invalid code query request")
        sid, revision = arguments.get("session_id"), arguments.get("binding_revision")
        if not isinstance(sid, str) or not sid or len(sid) > 256 or type(revision) is not int:
            raise HTTPException(status_code=403, detail="Code chat binding unavailable")
        try:
            managed = get_managed_binding(sid, owner=owner)
            authority_workspace = managed.get("authorityWorkspaceID")
            if (managed.get("owner") != owner or managed.get("stableChatID") != sid
                    or managed.get("workspaceRevision") != revision or managed.get("transition") is not None
                    or not isinstance(authority_workspace, str) or not authority_workspace):
                raise ValueError("binding unavailable")
            expected_code = code_binding_token(INTERNAL_TOOL_TOKEN, owner=owner, session_id=sid, workspace_id=authority_workspace, binding_revision=revision)
            received_code = request.headers.get("X-OpenClank-Code-Binding", "")
            if not received_code or not secrets.compare_digest(received_code, expected_code):
                raise ValueError("binding unavailable")
        except Exception:
            raise HTTPException(status_code=403, detail="Code chat binding unavailable") from None
        result = None
        try:
            async with _account_request_barrier.admitted(owner):
                session = session_manager.get_session(sid)
                if (session is None or session.owner != owner or session.workspace_id != authority_workspace
                        or getattr(session, "archived", False)):
                    raise ValueError("session unavailable")
                if arguments['query'].get('operation')=='library_api':
                    from src.openclank.code_query import validate_library_query, observe_library_scoped, unavailable_library
                    from src.openclank.account_request_barrier import _current_admission
                    import copy
                    try:
                        selected=validate_library_query(arguments['query'])
                        original=copy.deepcopy(managed); account=auth_manager.account_id(owner)
                        admission=_current_admission.get();provider_identity=memory_provider
                        def library_current():
                            try:
                                _account_request_barrier.assert_live_admission(owner,admission)
                                return bool(account and auth_manager.account_id(owner)==account and auth_manager.username_for_account_id(account)==owner
                                    and app.state.memory_provider is provider_identity and get_managed_binding(sid,owner=owner)==original
                                    and session_manager.get_session(sid) is session and session.owner==owner
                                    and session.workspace_id==authority_workspace and not getattr(session,'archived',False))
                            except Exception:return False
                        if not library_current():return unavailable_library()
                        deadline=time.monotonic()+15
                        async def library_disconnect():
                            while True:
                                if (await request.receive()).get('type')=='http.disconnect':return
                        task=asyncio.create_task(observe_library_scoped(repository=FilePolicyRepository(),provider=provider_identity,
                            auth_manager=auth_manager,owner=owner,workspace=authority_workspace,purpose='agent_workspace',session=session,
                            package=selected['query'],deadline=deadline,current=library_current,max_output_bytes=15000))
                        disconnect=asyncio.create_task(library_disconnect())
                        try:
                            done,_=await asyncio.wait({task,disconnect},return_when=asyncio.FIRST_COMPLETED)
                            if disconnect in done:
                                task.cancel();await asyncio.gather(task,return_exceptions=True);return unavailable_library()
                            observed=await task
                        finally:
                            disconnect.cancel()
                            if not task.done():task.cancel()
                            await asyncio.gather(disconnect,task,return_exceptions=True)
                        return observed if observed is not None and library_current() else unavailable_library()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        return unavailable_library()
                # Optional outline metadata is only a request, never authority.
                # The actual actor, account, tool policy and current preference
                # are independently rechecked before and after the extra read.
                outline_request = arguments.get("outline_request")
                outline_values = []
                outline_current = None
                if isinstance(outline_request, dict) and set(outline_request) == {"version", "turn", "toolCallID"} and type(outline_request.get("version")) is int and outline_request["version"] == 1:
                    turn = outline_request.get("turn")
                    call_id = outline_request.get("toolCallID")
                    fields = {"sessionID", "rootOperationID", "actorID", "executionRevision", "innerTurnID"}
                    if (type(turn) is dict and set(turn) == fields and type(call_id) is str and 0 < len(call_id) <= 256
                            and type(turn.get("executionRevision")) is int and turn["executionRevision"] >= 0
                            and all(type(turn.get(k)) is str and 0 < len(turn[k]) <= 256 for k in fields - {"executionRevision"})):
                        try:
                            from routes.prefs_routes import _load_for_user
                            from src.agent_settings import validate_agent_settings, agent_settings_revision
                            def read_outline_preference():
                                settings = validate_agent_settings((_load_for_user(owner) or {}).get("agent_settings", {}))
                                return settings.get("tokenomics", {}).get("source_outlines") is True, agent_settings_revision(settings)
                            enabled, settings_revision = await asyncio.to_thread(read_outline_preference)
                            account = auth_manager.account_id(owner)
                            supervisor = app.state.mimo_supervisor
                            worker = supervisor.worker_for_owner(owner)
                            bridge = worker.bridge if worker is not None else None
                            holder = getattr(bridge, "_managed_provider_generation", None)
                            def durable_outline_current():
                                enabled_now, revision_now = read_outline_preference()
                                return (enabled_now and revision_now == settings_revision and not _owner_request_fenced(owner)
                                    and get_managed_binding(sid, owner=owner) == managed
                                    and session_manager.get_session(sid) is session and session.owner == owner
                                    and session.workspace_id == authority_workspace and not getattr(session, "archived", False))
                            async def current_outline():
                                try:
                                    if (not enabled or not account or not await asyncio.to_thread(durable_outline_current)
                                            or auth_manager.account_id(owner) != account or auth_manager.username_for_account_id(account) != owner
                                            or app.state.mimo_supervisor is not supervisor or supervisor.worker_for_owner(owner) is not worker
                                            or worker.bridge is not bridge or getattr(bridge, "_managed_provider_generation", None) != holder):
                                        return False
                                    resolved = await bridge.resolve_managed_actor_execution(turn["sessionID"], turn["rootOperationID"], turn["actorID"], turn["executionRevision"])
                                    if (resolved.get("owner") != owner or resolved.get("stableChatID") != sid
                                            or resolved.get("authorityWorkspaceID") != authority_workspace or resolved.get("holderID") != holder):
                                        return False
                                    key = bridge._managed_turn_key(turn)
                                    async with bridge._managed_execution_lock:
                                        entry = bridge._managed_actor_turns.get(key)
                                        policy_revision = entry.get("policyRevision") if entry is not None else None
                                    if entry is None: return False
                                    decision = await bridge._handle_managed_tool_admit({**turn, "toolName": "search_code", "kind": "tool", "toolCallID": call_id})
                                    if not await asyncio.to_thread(durable_outline_current): return False
                                    async with bridge._managed_execution_lock:
                                        return (decision.get("allowed") is True and bridge._managed_actor_turns.get(key) is entry
                                            and entry.get("policyRevision") == policy_revision and auth_manager.account_id(owner) == account
                                            and auth_manager.username_for_account_id(account) == owner and app.state.mimo_supervisor is supervisor
                                            and supervisor.worker_for_owner(owner) is worker and worker.bridge is bridge
                                            and getattr(bridge, "_managed_provider_generation", None) == holder
                                            and session_manager.sessions.get(sid) is session and session.owner == owner
                                            and session.workspace_id == authority_workspace and not getattr(session, "archived", False))
                                except Exception:
                                    return False
                            if enabled and worker is not None and bridge is not None and await current_outline(): outline_current = current_outline
                        except Exception:
                            pass
                async def disconnected():
                    while True:
                        if (await request.receive()).get("type") == "http.disconnect":
                            return
                query_task = asyncio.create_task(query_code(repository=FilePolicyRepository(), provider=memory_provider, auth_manager=auth_manager,
                    owner=owner, workspace_id=authority_workspace, purpose="agent_workspace", session=session, query=arguments["query"],
                    **({"_outline_current": outline_current, "_outline_sink": outline_values.append} if outline_current is not None else {})))
                disconnect_task = asyncio.create_task(disconnected())
                try:
                    done, _ = await asyncio.wait({query_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
                    if disconnect_task in done:
                        query_task.cancel()
                        await asyncio.gather(query_task, return_exceptions=True)
                        return finalize_error(arguments["query"], "cancelled", "Code request disconnected")
                    result = await query_task
                finally:
                    disconnect_task.cancel()
                    if not query_task.done():
                        query_task.cancel()
                    await asyncio.gather(disconnect_task, query_task, return_exceptions=True)
                current = get_managed_binding(sid, owner=owner)
                if (current != managed or session_manager.get_session(sid) is not session or session.owner != owner
                        or session.workspace_id != authority_workspace or getattr(session, "archived", False)):
                    return finalize_error(arguments["query"], "denied", "Code chat binding changed", observed_cost=result.get("cost"))
                if outline_current is not None and len(outline_values) == 1 and not result.get("error"):
                    candidate = outline_values[0]
                    if candidate.get("result") is None or await outline_current() is True:
                        return {"result": result, "outline": candidate}
                return result
        except AccountRequestFenced:
            if arguments["query"].get("operation")=="library_api":
                from src.openclank.code_query import unavailable_library
                return unavailable_library()
            return finalize_error(arguments["query"], "denied", "Code account lifecycle unavailable")
        except Exception:
            if arguments["query"].get("operation")=="library_api":
                from src.openclank.code_query import unavailable_library
                return unavailable_library()
            return finalize_error(arguments["query"], "denied", "Code chat binding unavailable", observed_cost=(result or {}).get("cost"))
    if name == "memory_quality":
        # Scoped broker credentials only need the read-only startup handshake.
        # Never let them project global rebuild/maintenance controls into the
        # app-owned Memery process.
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
        raise HTTPException(status_code=503, detail="Memery broker unavailable")
    try:
        async with _account_request_barrier.admitted(owner):
            if name == "memory_quality":
                # Broker connect health: answered from the Host provider's own state; no Native call.
                reply = getattr(memory_provider, "broker_health", lambda: None)()
                if type(reply) is not dict or reply.get("ready") is not True:
                    raise HTTPException(status_code=503, detail="Memery broker unavailable")
                return reply
            return await invoke(name, scoped_arguments)
    except AccountRequestFenced as exc:
        raise HTTPException(
            status_code=409,
            detail="Account lifecycle operation in progress",
        ) from exc
    except MemoryRequestRejectedError as exc:
        from src.memory_provider import code_node_forget_refusal_detail
        detail = code_node_forget_refusal_detail(exc) if name == "memory_forget" else None
        if detail is not None:
            raise HTTPException(status_code=409, detail=detail) from exc
        logger.info("internal Memery tool %s rejected: %s", name, exc)
        raise HTTPException(
            status_code=409,
            detail="Memery rejected the memory operation",
        ) from exc
    except Exception as exc:
        logger.warning("internal Memery tool %s failed: %s", name, exc)
        raise HTTPException(status_code=502, detail="Memery tool failed") from exc


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
        or owner not in getattr(auth_manager, "users", {})
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


async def _quiesce_account_owner_writers(owner: str, *, subject_id: str) -> dict[str, object]:
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
    if canonical_memory_lifecycle is not None and canonical_memory_lifecycle is not memory_lifecycle:
        canonical_drain = getattr(canonical_memory_lifecycle, "drain_owner_reconcile_tasks", None)
        if not callable(canonical_drain):
            raise RuntimeError("canonical memory writer drain is unavailable")
        receipts["canonical_memory_reconcile"] = await canonical_drain(owner)
    receipts["memory_groom"] = await drain_owner_grooms(owner)

    subject = str(subject_id or '').strip()
    current_subject = auth_manager.account_id(owner)
    if not subject or (current_subject and current_subject != subject):
        raise RuntimeError('account Files authority is unavailable')
    from src.openclank.cookbook_state import transaction as cookbook_transaction, subject_inventory
    from src.constants import COOKBOOK_STATE_FILE
    def _check_cookbook():
        with cookbook_transaction(COOKBOOK_STATE_FILE) as state:
            return len(subject_inventory(state, subject))
    receipts["cookbook_acquisitions"] = await asyncio.to_thread(_check_cookbook)
    files_policy = getattr(app.state, 'files_policy_repository', None)
    if files_policy is None:
        raise RuntimeError('account Files claim authority is unavailable')
    if await asyncio.to_thread(files_policy.has_unsettled_host_mutation, owner_subject_id=subject):
        raise RuntimeError('account Files operation requires reconciliation')
    # Deep-research quiescence persists the owner fence. Run it last so no
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

from routes.stats_routes import setup_stats_routes
app.include_router(setup_stats_routes())
from routes.stats_analysis_routes import setup_stats_analysis_routes
app.include_router(setup_stats_analysis_routes())
from routes.stats_activity_routes import setup_stats_activity_routes
app.include_router(setup_stats_activity_routes())
from routes.stats_preferences_routes import setup_stats_preferences_routes
app.include_router(setup_stats_preferences_routes())

# Core database admission owns logging schema; pending capture recovery is lifecycle repair.
from src.openclank.logging_models import recover_pending_captures
from core.database import engine as _logging_schema_engine
recover_pending_captures(_logging_schema_engine)
from routes.logging_routes import setup_logging_routes
from routes.logging_settings_routes import setup_logging_settings_routes
from src.openclank.logging_capture_store import CAPTURE as _logging_capture
from services.logging.projection import resolve_archive_owner
app.include_router(setup_logging_routes(session_manager=session_manager, advanced_pruner=_logging_capture.prune,
                                      archive_owner_resolver=resolve_archive_owner,
                                      semantic_index=rag_manager))
app.include_router(setup_logging_settings_routes())

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
from routes.provider_v1_routes import setup_provider_v1_routes, shutdown_provider_oauth_flows
from src.openclank.provider_store import ProviderStore
app.state.provider_store = ProviderStore()
app.include_router(setup_provider_v1_routes(provider_store=app.state.provider_store))

# Copal's first-party Redb workspace adapter.
from routes.copal_routes import setup_copal_routes
from src.openclank.file_policy import FilePolicyRepository

# Files and semantic attachment consumers share one application-scoped policy
# authority. A route-local repository can observe a different generation after
# a reset/revocation and would make preparation/final CAS inconsistent.
def _files_identity_users():
    users = auth_manager.users
    return {name: record for name, record in users.items()
            if normalize_known_username(users, name) and not auth_manager.is_account_lifecycle_fenced(name)}


def _active_files_lifecycle_aliases():
    from core.operation_models import AccountLifecycleOperation as Operation
    with SessionLocal() as db:
        rows = db.query(Operation.source_owner, Operation.target_owner, Operation.tombstone_owner).filter(
            Operation.active_account_id.is_not(None)
        )
        return {name.strip().lower() for row in rows for name in row if name}


from src.openclank.file_policy_compat import configure_lifecycle_alias_lookup
configure_lifecycle_alias_lookup(_active_files_lifecycle_aliases)
files_policy_repository = FilePolicyRepository()
files_policy_repository.sync_subjects(_files_identity_users())
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

# Persisted image-editor drafts (server-backed projects)
from routes.editor_draft_routes import setup_editor_draft_routes
app.include_router(setup_editor_draft_routes())

# Imps managed image projects (editable state + Lore-backed Save)
from routes.image_project_routes import setup_image_project_routes
app.include_router(setup_image_project_routes())
from routes.imps_routes import setup_imps_routes
app.include_router(setup_imps_routes())

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
app.include_router(setup_files_facade_routes(policy_repository=files_policy_repository, application_state=app.state))
from routes.git_workbench_routes import setup_git_workbench_routes
app.include_router(setup_git_workbench_routes(repository=files_policy_repository, session_manager=session_manager))

# Canonical Location/Workspace/People/Agent policy state. Compatibility root
# routes remain during migration, but scoped resets write only this ledger.
from routes.file_policy_routes import setup_file_policy_routes
app.include_router(setup_file_policy_routes(repository=files_policy_repository))

# Interactive Browser previews reuse the same Files facade factory and policy
# authority. Constructing the service does not bind its listener; an authorized
# preview request opens it lazily, and shutdown closes it before Files clients.
from routes.browser_preview_routes import setup_browser_preview_routes
_browser_preview_router = setup_browser_preview_routes(
    facade_factory=app.state.files_facade_factory,
    policy_repository=files_policy_repository,
    session_cookie=SESSION_COOKIE,
)
app.state.browser_preview_service = _browser_preview_router.state.preview_service
app.include_router(_browser_preview_router)

# General Hexes follow the active provider's pinned SQLite authority. Broker
# instances without a local path report unavailable rather than open a default.
from routes.general_hex_routes import setup_general_hex_routes, _owner as _hex_owner
from routes.general_hex_exchange_routes import setup_general_hex_exchange_routes
from src.general_hex_runtime import active_hex_db_path
from src.openclank.workspace_policy_service import resolve_owned_workspace, WorkspacePolicyServiceError
_general_hex_db_resolver = lambda: active_hex_db_path(app.state.memory_provider)


def _general_hex_workspace_binding(request, workspace_id):
    """Use an existing owned App or Agent binding without granting access."""
    for purpose in ("app_folder", "agent_workspace"):
        try:
            binding = resolve_owned_workspace(
                files_policy_repository,
                workspace_id=workspace_id,
                owner_username=_hex_owner(request),
                auth_manager=request.app.state.auth_manager,
                purpose=purpose,
            )
            return binding.path, purpose
        except WorkspacePolicyServiceError as error:
            last_error = error
    status = 403 if last_error.code == "workspace_denied" else 404
    raise HTTPException(status, detail={"code": last_error.code, "message": str(last_error)}) from last_error


def _general_hex_workspace_resolver(request, workspace_id):
    return _general_hex_workspace_binding(request, workspace_id)[0]


def _general_hex_workspace_lister(request, offset, limit, search):
    """Include first-use workspaces, even before a Hex project exists."""
    owner = _hex_owner(request)
    subject_id = request.app.state.auth_manager.account_id(owner)
    if not subject_id:
        raise HTTPException(403, detail="Workspace owner is unavailable")
    query = str(search or "").strip().casefold()
    rows = []
    for workspace in files_policy_repository.list_workspaces(owner_subject_id=str(subject_id)):
        if query and query not in workspace.name.casefold() and query not in workspace.id.casefold():
            continue
        rows.append(workspace)
    offset = max(0, int(offset))
    limit = max(1, min(100, int(limit)))
    page = []
    for workspace in rows[offset:offset + limit]:
        item = {"workspace_id": workspace.id, "name": workspace.name}
        try:
            path, purpose = _general_hex_workspace_binding(request, workspace.id)
            item.update(path=path, purpose=purpose, availability="available")
        except HTTPException as error:
            if error.status_code not in (403, 404):
                raise
            item["availability"] = "unavailable"
        page.append(item)
    next_offset = offset + len(page)
    return {"workspaces": page, "total": len(rows), "next_offset": next_offset if next_offset < len(rows) else None}


app.include_router(setup_general_hex_routes(db_path_resolver=_general_hex_db_resolver))
app.include_router(setup_general_hex_exchange_routes(
    db_path_resolver=_general_hex_db_resolver,
    workspace_resolver=_general_hex_workspace_resolver,
    workspace_lister=_general_hex_workspace_lister,
))

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

from routes.theme_emoji_routes import router as theme_emoji_router
app.include_router(theme_emoji_router)


# MCP (Model Context Protocol)
from src.mcp_manager import McpManager
from src.agent_tools import set_mcp_manager
from routes.mcp_routes import setup_mcp_routes

mcp_manager = McpManager()
app.state.mcp_manager = mcp_manager
set_mcp_manager(mcp_manager)
app.include_router(setup_mcp_routes(mcp_manager))
from src.openclank.browser_control_service import BrowserControlService, configure_browser_control_service
from routes.browser_control_routes import setup_browser_control_routes
app.state.browser_control_service = BrowserControlService(mcp_manager)
configure_browser_control_service(app.state.browser_control_service)
app.include_router(setup_browser_control_routes(
    dispatch=lambda **kwargs: app.state.browser_control_service.call(**kwargs, require_active_policy=True),
    auth_manager=auth_manager,
    session_manager=session_manager,
    account_request_barrier=_account_request_barrier,
    internal_secret=INTERNAL_TOOL_TOKEN,
    trusted_loopback=_is_trusted_loopback,
    policy_checker=app.state.browser_control_service.turn_policy_allows,
    policy_revision=app.state.browser_control_service.current_turn_policy_revision,
    service=app.state.browser_control_service,
))
logger.info("MCP routes initialized")

# AI Interaction tools (debates, pipelines, self-managing AI, UI control)
from src.ai_interaction import set_session_manager as set_ai_session_manager, set_memory_manager as set_ai_memory_manager
set_ai_session_manager(session_manager)
set_ai_memory_manager(
    memory_manager,
    memory_vector,
    provider=memory_provider,
    lifecycle=memory_lifecycle,
)
logger.info("AI interaction tools initialized (session, memory, RAG, UI control)")

# Webhooks
from routes.webhook_routes import setup_webhook_routes
app.include_router(setup_webhook_routes(webhook_manager, auth_manager, session_manager, api_key_manager))

# API Tokens
from routes.api_token_routes import setup_api_token_routes
app.include_router(setup_api_token_routes())

# Chat channels (Telegram, Matrix): Off by default. Nothing is constructed or contacted until the global switch,
# a channel's switch and its secrets are all present (src/openclank/channels/runner.py).
def _build_channels():
    import httpx
    from core.constants import DATA_DIR as _CHANNEL_DATA_DIR
    from src.settings import load_settings as _load_channel_settings
    from src.openclank.channels.access import Access
    from src.openclank.channels.bridge import ChannelBridge
    from src.openclank.channels.discord import DiscordAdapter
    from src.openclank.channels.feishu import FeishuAdapter
    from src.openclank.channels.wecom import WeComAdapter
    from src.openclank.channels.matrix import MatrixAdapter
    from src.openclank.channels.runner import ChannelRunner
    from src.openclank.channels.store import ChannelStore
    from src.openclank.channels.telegram import TelegramAdapter
    from routes.channel_routes import setup_channel_routes
    store = ChannelStore(os.path.join(_CHANNEL_DATA_DIR, "channels.json"))
    access = Access(store)
    def bridge():
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://channel.local",
                                   headers={"Authorization": "Bearer " + (store.token() or "")})
        return ChannelBridge(store, access, client)
    factories = {
        "telegram": lambda st: TelegramAdapter(lambda: st.secret("telegram", "token") or "", httpx.AsyncClient()),
        "discord": lambda st: DiscordAdapter(lambda: st.secret("discord", "token") or "", httpx.AsyncClient()),
        "feishu": lambda st: FeishuAdapter(lambda: st.setting("feishu", "app_id") or "", lambda: st.secret("feishu", "app_secret") or "",
                                           lambda: st.setting("feishu", "domain") or "feishu", httpx.AsyncClient()),
        "wecom": lambda st: WeComAdapter(lambda: st.setting("wecom", "bot_id") or "", lambda: st.secret("wecom", "bot_secret") or ""),
        "matrix": lambda st: MatrixAdapter(lambda: st.setting("matrix", "homeserver") or "", lambda: st.secret("matrix", "access_token") or "", httpx.AsyncClient()),
    }
    def revoke_idle():
        from src.openclank.channels.channel_token import revoke
        if store.token():                       # nothing to do (and nothing written) while channels were never on
            if store.owner():
                revoke(store.owner(), lambda: app.state.invalidate_token_cache())
            store.set_token(None)
    runner = ChannelRunner(store, global_enabled=lambda: bool(_load_channel_settings().get("channels_enabled")),
                           adapter_factories=factories, bridge_factory=bridge, on_idle=revoke_idle)
    app.state.channel_runner = runner
    app.include_router(setup_channel_routes(store, runner, invalidate=lambda: app.state.invalidate_token_cache()))
_build_channels()

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
    orchestration_router=orchestration_router,
    session_router=session_router,
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
    orchestration_router=orchestration_router,
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
    "/wiki",
    "/timeline",
    "/todo",
    "/graph",
    "/mind",        # legacy alias -> Graph (mind mode)
    "/galaxy",      # legacy alias -> Graph (galaxy mode)
    "/treehouse",
    "/clankshop",
    "/files",
    "/browser",
    "/calendar",
    "/cookbook",
    "/usage",
    "/email",
    "/memory",
    "/tasks",
    "/library",
)
for _shell_path in _SHELL_APPLET_PATHS:
    app.add_api_route(
        _shell_path, serve_index, methods=["GET"], include_in_schema=False,
        name=f"serve_shell{_shell_path.replace('/', '_')}",
    )


@app.get("/editor", include_in_schema=False)
async def serve_editor_entry(request: Request):
    # Bare browser visits enter the app shell. Explicit document and applet
    # intents still restore through the same authenticated Editor shell.
    if not request.url.query:
        return RedirectResponse(url="/", status_code=302, headers={"Cache-Control":"no-store"})
    return await serve_index(request)


def _legacy_shell_redirect(request: Request, target: str) -> RedirectResponse:
    query = str(request.url.query or "")
    return RedirectResponse(url=f"{target}?{query}" if query else target, status_code=302)


@app.get("/imps", include_in_schema=False)
@app.get("/image-editor", include_in_schema=False)
async def serve_image_editor_alias(request: Request) -> RedirectResponse:
    if not request.url.query:
        return RedirectResponse(url="/editor?open=editor", status_code=302)
    return _legacy_shell_redirect(request, "/editor")


@app.get("/gallery", include_in_schema=False)
async def serve_gallery_alias(request: Request) -> RedirectResponse:
    return _legacy_shell_redirect(request, "/files")

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
    "imps": "/editor",
    "image-editor": "/editor",
    "bases": "/editor",
    "wiki": "/wiki",
    "timeline": "/timeline",
    "todo": "/todo",
    "graph": "/graph",
    "mind": "/graph",
    "galaxy": "/graph",
    "treehouse": "/treehouse",
    "clankshop": "/clankshop",
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
    if target == "/editor" and not query:
        query["open"] = "editor"
    suffix = ("?" + urlencode(query)) if query else ""
    return RedirectResponse(url=f"{target}{suffix}", status_code=302)

@app.get("/backgrounds")
async def serve_backgrounds(request: Request):
    """Sandbox page for prototyping background effects. No auth required."""
    return serve_html_with_nonce(request, abs_join(BASE_DIR, "static/backgrounds.html"))

@app.get("/login")
async def serve_login(request: Request):
    return serve_html_with_nonce(request, abs_join(BASE_DIR, "static/login.html"))

@app.get("/api/version")
async def get_version(request: Request):
    from core.constants import APP_VERSION

    verification = getattr(request.app.state, "engine_verification", None)
    engine = {"state": "unavailable"}
    if verification is not None and verification.ok:
        engine = {
            "state": "verified",
            "version": verification.version,
            "target": verification.target,
            "sourceSha256": verification.source_sha256,
        }
    return {
        "version": APP_VERSION,
        "engine": engine,
        "releaseDiscovery": {
            "state": "unavailable",
            "reason": "publisher_feed_unconfigured",
        },
    }

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
            auth_enabled=True,
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
    from src.openclank.server_manager import acquire_server_lifetime_guard, acquire_database_lifetime_guard
    requested = os.environ.get('OPENCLANK_S05_SINGLE_HOST', '')
    if requested not in ('', '0', '1'):
        raise RuntimeError('invalid S05 single-host configuration')
    guarded = os.environ.get('OPENCLANK_MEMORY_STORE_PROFILE', 'legacy-v16') in ('guarded-v17', 'guarded-v18')
    if guarded and requested == '0':
        raise RuntimeError('guarded memory profile requires single-host qualification')
    exclusive = guarded or requested == '1'
    if exclusive:
        args = sys.argv[1:]
        if ('--reload' in args or any(arg.startswith('--reload=') for arg in args)
            or any(arg == '--workers' or arg.startswith('--workers=') for arg in args)
            or os.environ.get('WEB_CONCURRENCY', '1') != '1'):
            raise RuntimeError(
                'S05 single-host profile requires one non-reloading process: the guarded memory store '
                '(or an explicit OPENCLANK_S05_SINGLE_HOST=1) admits only one writer. Start without '
                '--reload/--workers and with WEB_CONCURRENCY unset or 1.')
    guard = acquire_server_lifetime_guard(DATA_DIR, exclusive=exclusive)
    app.state.s05_profile = None
    database_guard = None
    started = False
    archive_profile_bound = False
    from src.openclank.python_kernel import KernelManager
    app.state.python_kernel_manager = KernelManager()
    try:
        profile_provider = canonical_memory_provider or memory_provider
        resolver = getattr(profile_provider, '_local_db_path', None)
        path = resolver() if callable(resolver) else None
        if path and Path(path).resolve(strict=False).parent.is_dir():
            database_guard = acquire_database_lifetime_guard(path, exclusive=exclusive)
        if exclusive:
            from src.openclank.memory_job_admission import create_single_host_profile
            if not path or database_guard is None:
                raise RuntimeError('S05 single-host profile requires an existing local memory store')
            app.state.s05_profile = create_single_host_profile(guard, profile_provider, database_guard)
            app.state.s05_profile.assert_current()
            from src.openclank.conversation_archive import configure_conversation_archive_origin_profile
            archive_profile_bound = configure_conversation_archive_origin_profile(app.state.s05_profile)
        await _startup_event()
        started = True
        from src.openclank.memory_policy import _install_runtime_capability_reader, _remove_runtime_capability_reader
        _policy_capability_token = None
        if app.state.s05_profile is not None:
            _policy_capability_token = _install_runtime_capability_reader(app.state, _live_memory_policy_capabilities)
        # Installation is conditional; the callback repeats these checks and
        # exact actor/source authorization at each admitted observation.
        app.state.s06_admitted_callback = None
        try:
            supervisor = getattr(app.state, 'mimo_supervisor', None)
            if supervisor is not None and await asyncio.to_thread(_s06_admitted_ready, app.state, supervisor):
                app.state.s06_admitted_callback = _s06_admitted_callback
        except Exception:
            logger.info('S06 admitted observation is unavailable')
        yield
    finally:
        if started and _policy_capability_token is not None:
            _remove_runtime_capability_reader(_policy_capability_token)
        app.state.s06_admitted_callback = None
        app.state.s08_rules_callback = None
        app.state.s08_original_status_callback = None
        app.state.maintenance_reconcile_callback = None
        try:
            if started:
                await _shutdown_event()
        finally:
            await asyncio.to_thread(app.state.python_kernel_manager.close_all)
            if archive_profile_bound:
                from src.openclank.conversation_archive import release_conversation_archive_origin_profile
                release_conversation_archive_origin_profile(app.state.s05_profile)
            coordinator = getattr(app.state, 'memory_policy_coordinator', None)
            if coordinator is not None:
                try:
                    from src.openclank.memory_policy_projection import unregister_policy_coordinator
                    unregister_policy_coordinator(coordinator)
                    app.state.memory_policy_coordinator = None
                    app.state.memory_policy_projection = None
                except ValueError:
                    logger.error('Policy coordinator remains unavailable during shutdown')
            if database_guard is not None:
                database_guard.close()
            if guard is not None:
                guard.close()

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

        if getattr(sys, "frozen", False):
            from scripts.openclank_bootstrap import packaged_engine_expectations
            _engine_expectations = packaged_engine_expectations()
        else:
            _engine_expectations = {"expected_source_sha256": source_fingerprint(
                Path(__file__).resolve().parent / "packages" / "mimo-code")}
        app.state.engine_verification = verify_install(
            expected_version=APP_VERSION,
            **_engine_expectations,
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
    # Admit the canonical source before serving ordinary Logs, including a
    # genuinely fresh archive with no conversations. The existing owner creates
    # only a fresh current schema; nonempty historical stores still require
    # explicit offline conversion and cannot be treated as empty read results.
    await asyncio.to_thread(get_conversation_archive)
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

    # The current Copal authority is the qualified Files store.
    try:
        from src.openclank.copal_storage import configured_bridge
        app.state.copal_bridge = configured_bridge()
        await app.state.copal_bridge.start()
    except Exception as e:
        logger.error("Copal Files bridge failed to start: %s", e, exc_info=True)
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
    _startup_warmups_enabled = str(os.getenv("OPEN_CLANK_STARTUP_WARMUPS", "")).lower() in {"1", "true", "yes", "on"}
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
        logger.info("Startup warmups disabled (set OPEN_CLANK_STARTUP_WARMUPS=1 to enable)")

    # Keep-alive is opt-in. The ping path performs model discovery, and when
    # stale LAN endpoints are configured it can add periodic backend pressure
    # that delays unrelated UI requests such as Notes/Documents.
    _keepalive_enabled = str(os.getenv("OPEN_CLANK_MODEL_KEEPALIVE", "")).lower() in {"1", "true", "yes", "on"}
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


    # Memory lifecycle recovery is a readiness boundary: do not serve traffic
    # while provider state and memory-derived skills may still be split.
    if memory_provider:
        await memory_provider.initialize()
        if canonical_memory_provider is not None and canonical_memory_provider is not memory_provider:
            await canonical_memory_provider.initialize()
        from services.memory.forget_coordinator import require_memory_recovery_provider_coverage
        recovery_coverage = await require_memory_recovery_provider_coverage((memory_lifecycle, canonical_memory_lifecycle))
        if memory_lifecycle is not None:
            reconciled = await memory_lifecycle.reconcile(coverage=recovery_coverage)
            require_memory_lifecycle_convergence(reconciled)
        if canonical_memory_lifecycle is not None:
            reconciled = await canonical_memory_lifecycle.reconcile(coverage=recovery_coverage)
            require_memory_lifecycle_convergence(reconciled)

    # Memery maintenance is deliberately one small task, not another
    # scheduler framework. Zero (the default) disables it for deployments that
    # groom externally; an explicit interval runs one pass per real owner.
    from src.memory_maintenance import groom_interval_hours, groom_loop
    from src.memory_scope import chat_workspace, memory_owner
    _groom_hours = groom_interval_hours()
    if memory_provider and getattr(memory_provider, "provider_id", "") == "memery":
        if _groom_hours <= 0:
            logger.info("Memery auto-groom disabled (FM_GROOM_INTERVAL_HOURS=0)")
        else:
            def _memory_groom_owners():
                return list(_files_identity_users())

            _startup_tasks.append(asyncio.create_task(groom_loop(
                memory_provider,
                _groom_hours,
                owners=_memory_groom_owners,
                workspace_id=chat_workspace(),
                owner_is_fenced=auth_manager.is_account_lifecycle_fenced,
            )))
            logger.info("Memery auto-groom scheduled every %.2fh", _groom_hours)

    async def _ensure_default_tasks():
        # A scheduled row can outlive its account. Only the current auth
        # binding may create built-ins or the personal assistant at startup.
        for owner, record in _files_identity_users().items():
            try:
                async with _account_request_barrier.admitted(owner):
                    await task_scheduler.ensure_defaults(owner, account_id=record["account_id"])
            except AccountRequestFenced:
                continue
            except Exception as exc:
                logger.debug("ensure_defaults(%s): %s", owner, exc)

    # Seed missing current built-in tasks before the runner starts.
    await _ensure_default_tasks()

    # Start scheduled task runner — skip when running under a cron-driven
    # deployment where an external worker drives task firing. Mirrors
    # `OPEN_CLANK_INPROCESS_POLLERS` from the email pollers.
    _tasks_inprocess = os.environ.get("OPEN_CLANK_INPROCESS_TASKS", "1").strip().lower()
    if _tasks_inprocess not in ("0", "false", "no", "off", ""):
        await task_scheduler.start()
    try:
        await app.state.channel_runner.reload()      # builds nothing unless channels are switched on and configured
    except Exception as _channel_exc:
        logger.warning("Chat channels did not start: %s", type(_channel_exc).__name__)
    else:
        logger.info(
            "In-process task scheduler disabled (OPEN_CLANK_INPROCESS_TASKS=0); "
            "drive task firing externally (e.g. cron)."
        )


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
    app.state.history_supervisor = None
    _history_binary = os.environ.get("OPENCLANK_HISTORY_SERVICE_BIN", "").strip()
    if not _history_binary:
        _app_resources = os.environ.get("APP_RESOURCES", "").strip()
        # A packaged release is authoritative.  The repository release target
        # is only a development fallback and is health-negotiated by the
        # supervisor before it becomes reachable from the app.
        _history_candidates = []
        _history_executable = "openclank-history-service.exe" if os.name == "nt" else "openclank-history-service"
        from src.runtime_paths import get_app_root
        _history_candidates.append(Path(get_app_root()) / "bin" / _history_executable)
        if _app_resources:
            _history_candidates.append(
                Path(_app_resources) / "libexec/openclank/history" / _history_executable
            )
        _history_candidates.append(
            Path(__file__).resolve().parent / "packages/openclank-history/target/release" / _history_executable,
        )
        _history_binary = next(
            (str(_candidate) for _candidate in _history_candidates if _candidate.is_file() and (os.name == "nt" or os.access(_candidate, os.X_OK))),
            "",
        )
    if _history_binary:
        try:
            from src.openclank.history_client import (
                HistoryServiceSupervisor,
                ScopedHistoryCredential,
            )
            from src.openclank.history_paths import history_root, settings_path

            _history_credentials = []
            for _username, _record in _files_identity_users().items():
                _username = str(_username).strip()
                if not _username or not isinstance(_record, dict):
                    continue
                _account_id = str(_record.get("account_id") or "").strip()
                if not _account_id:
                    continue
                _history_credentials.append(ScopedHistoryCredential(
                    actor_id="service:memery-history:" + _account_id, account_id=_account_id,
                    capabilities=frozenset({"memery-history"}),
                ))
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
                _history_credentials.append(
                    ScopedHistoryCredential(
                        actor_id=_username,
                        account_id=_account_id,
                        capabilities=frozenset({"read"}),
                    )
                )
            if not _history_credentials:
                # Readiness only: this private supervisor binding has no data
                # or administrator capability and owns no historical account.
                _history_credentials.append(
                    ScopedHistoryCredential(
                        actor_id="supervisor",
                        account_id="service:health",
                        capabilities=frozenset(),
                    )
                )
            # The Files root registry is the provider authority for physical
            # destinations.  Publish only enabled, available roots with the
            # immutable accounts that own or were assigned each root; the
            # history worker rejects a path that is not paired with one of
            # these trusted root identities.
            from src.openclank.filesystem_registry import FilesystemRootRegistry

            def _history_project_roots(_filesystem_data: dict[str, object]) -> list[dict[str, object]]:
                """Project canonical roots using current immutable auth identities."""
                users = _files_identity_users()
                _history_account_by_owner = {str(name): str(record['account_id'])
                    for name, record in users.items() if isinstance(record, dict) and record.get('account_id')}
                _history_admin_accounts = {str(record['account_id']) for record in users.values()
                    if isinstance(record, dict) and record.get('account_id') and record.get('is_admin') is True}
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
                # Broad physical roots are transport projections. Bind each
                # enabled lane to its own immutable account and exact actor.
                from routes.odysseus_files_routes import project_navigation_roots
                host_roots: dict[str, dict[str, object]] = {}
                for owner, record in users.items():
                    if not isinstance(record, dict):
                        continue
                    account = str(record.get("account_id") or "").strip()
                    actor = str(owner).strip()
                    if not account or not actor or not auth_manager.filesystem_access_eligible(actor, account):
                        continue
                    choices = _filesystem_registry.repository.full_filesystem_access(account)
                    if choices["agent"]:
                        for root in _filesystem_registry.full_agent_roots(actor, account).values():
                            projected.append({
                                "root_id": root["id"], "canonical_path": root["canonical_path"],
                                "kind": root["kind"], "account_ids": [account], "actor_ids": [f"agent:{actor}"],
                                "workspace_ids": [],
                            })
                    if not choices["human"]:
                        continue
                    scope = _filesystem_registry.app_scope(actor, is_admin=True)
                    projection = project_navigation_roots(_filesystem_registry, actor, scope)
                    for anchor in projection.get("roots") or []:
                        if "write" not in set(anchor.get("capabilities") or []):
                            continue
                        root_id = str(anchor.get("id") or "").strip()
                        path = str(anchor.get("path") or "").strip()
                        if not root_id or not path:
                            continue
                        # Pair each immutable account with its exact human
                        # actor. A merged account/actor cross product could
                        # accidentally authorize another administrator actor.
                        binding_id = f"{root_id}:human:{account}"
                        binding = host_roots.setdefault(binding_id, {
                            "root_id": binding_id,
                            "canonical_path": path,
                            "kind": str(anchor.get("kind") or "recursive_directory"),
                            "account_ids": [account],
                            "actor_ids": [],
                            "workspace_ids": [],
                        })
                        if actor not in binding["actor_ids"]:
                            binding["actor_ids"].append(actor)
                projected.extend(host_roots.values())
                return projected

            _history_authorized_roots: list[dict[str, object]] = []
            _history_root_generation = 0
            try:
                _filesystem_registry = FilesystemRootRegistry(
                    os.environ.get("OPEN_CLANK_FILES_REGISTRY") or None
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
            from services.memory.memery_history import publish_loop
            _startup_tasks.append(asyncio.create_task(publish_loop(app.state, _files_identity_users)))
            os.environ["OPENCLANK_HISTORY_SOCKET"] = str(_history_socket)
            from src.openclank.files_service_client import set_history_binding_provider

            def _files_history_binding(
                owner: str,
                lane: str,
                scope: dict[str, object] | None = None,
                *, files_child: bool = False,
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
                selected_root = str((scope or {}).get("selected_history_root_id") or "").strip()
                if selected_root and requested_ids != {selected_root}:
                    raise ValueError("selected History root is not the exact narrowed scope")
                allow_implicit_host = lane == "human" and (scope or {}).get("host") is True
                bindings = [
                    dict(root)
                    for root in _history_authorized_roots
                    if account in set(root.get("account_ids") or [])
                    and (
                        str(root.get("root_id")) in requested_ids
                        or (allow_implicit_host and not requested_ids)
                    )
                ]
                if selected_root:
                    bindings = [root for root in bindings if str(root.get("root_id")) == selected_root]
                    if len(bindings) != 1:
                        raise ValueError("selected History root is unavailable")
                workspace_root = str(bindings[0]["canonical_path"]) if bindings else ""
                workspace_id = (
                    (f"files:{selected_root}" if selected_root else str((scope or {}).get("history_workspace_id") or "").strip())
                    or (f"files:{bindings[0]['root_id']}" if bindings else "")
                )
                result = {
                    "socket": str(_history_socket),
                    "account_id": account,
                    "actor_id": actor,
                    "token": _history_supervisor.credential_token(actor, account),
                    "workspace_id": workspace_id,
                    "workspace_root": workspace_root,
                    "root_bindings": bindings,
                }
                if files_child:
                    checkpoint_tokens = _history_supervisor.files_checkpoint_tokens(
                        actor, account, [str(root.get("root_id") or "") for root in bindings]
                    )
                    if checkpoint_tokens:
                        result["checkpoint_tokens"] = checkpoint_tokens
                    if lane == "human":
                        worktree_tokens = _history_supervisor.files_worktree_tokens(
                            actor, account, [str(root.get("root_id") or "") for root in bindings]
                        )
                        if worktree_tokens:
                            result["worktree_tokens"] = worktree_tokens
                return result

            set_history_binding_provider(_files_history_binding)
            from src.openclank.skill_history_scope import SkillHistoryScopes
            _skill_history_scopes = SkillHistoryScopes(_history_supervisor, _history_authorized_roots)
            app.state.skill_history_scopes = _skill_history_scopes
            async def _history_root_refresh_loop():
                nonlocal _history_authorized_roots, _history_root_generation
                while True:
                    await asyncio.sleep(1.0)
                    try:
                        _latest = await asyncio.to_thread(_filesystem_registry.snapshot)
                        _latest_generation = int(_latest.get("generation") or 0)
                        _latest_roots = _history_project_roots(_latest)
                        if _latest_generation == _history_root_generation and _latest_roots == _history_authorized_roots:
                            continue
                        # The supervisor restarts one worker generation with
                        # both credentials and roots as a single publication.
                        await _skill_history_scopes.update_files(_latest_roots)
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
    else:
        logger.warning("Open Clank history writer unavailable: no service executable was found")

    # ── Open Clank managed-engine supervisor ──
    _agent_drive = (
        os.environ.get("OPEN_CLANK_AGENT_DRIVE")
        or "acp"
    )
    if _agent_drive == "acp":
        async def _startup_mimo():
            try:
                import sys as _sys
                _agent_src = (
                    os.environ.get("OPEN_CLANK_AGENT_SRC")
                    or str(Path(__file__).resolve().parent / "src")
                )
                if _agent_src not in _sys.path:
                    _sys.path.insert(0, _agent_src)
                from src.openclank.agent_supervisor import select_host_provider_owner
                from src.openclank.agent_supervisor_factory import build_agent_supervisor
                from src.openclank.local_model_executors import (
                    build_local_executor_broker,
                )

                # Parse OPEN_CLANK_SAFE_DIRS (colon-separated, ~ expanded).
                _raw = (
                    os.environ.get("OPEN_CLANK_SAFE_DIRS")
                    or ""
                )
                _safe_dirs = [os.path.expanduser(d.strip()) for d in _raw.split(":") if d.strip()] if _raw else []

                # If OPEN_CLANK_AGENT_HOME is set, auto-include the agent
                # home in safe dirs so the agent can read/write its own files.
                _agent_home = os.environ.get("OPEN_CLANK_AGENT_HOME")
                if _agent_home:
                    _agent_home = os.path.expanduser(_agent_home)
                    if _agent_home not in _safe_dirs:
                        _safe_dirs.append(_agent_home)

                _safe_dirs = _safe_dirs or None

                # Runtime partitions require real owners from process start,
                # including the account-bootstrap window.
                _initial_owner = ""
                _host_provider_owner = ""
                _admin_owners = [
                    str(name)
                    for name, record in _files_identity_users().items()
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
                    canonical_source_provider=canonical_memory_provider,
                    safe_dirs=_safe_dirs,
                    initial_owner=_initial_owner,
                    host_provider_owner=_host_provider_owner,
                    local_executor_broker=_local_executor_broker,
                )
                if getattr(app.state, 's05_profile', None) is not None:
                    if getattr(_sup, 'backend_name', None) != 'acp':
                        raise RuntimeError('S05 single-host profile requires the ACP supervisor')
                    _sup._s05_profile = app.state.s05_profile
                    from src.model_dispatch import set_agent_supervisor
                    from src.openclank.memory_job_admission import qualify_single_host
                    set_agent_supervisor(_sup)
                    try:
                        qualify_single_host(app.state.s05_profile, _sup)
                    except Exception:
                        if _sup._s05_foreground_runtime is None:
                            set_agent_supervisor(None)
                            logger.warning('S05 qualification unavailable; ordinary foreground remains available')
                        else:
                            logger.warning('S05 qualification outcome uncertain; foreground fence retained')
                _reap_orphaned_mimo_workers()
                await _sup.start()
                app.state.agent_supervisor = _sup
                app.state.mimo_supervisor = _sup
                if getattr(_sup,'backend_name',None)=='acp':
                    _sup.owner_lifecycle_pool().set_queued_retirement_handler(_queued_holder_retirement)
                task_scheduler._mimo_supervisor = _sup
                from src.model_dispatch import set_agent_supervisor
                set_agent_supervisor(_sup)
                if getattr(app.state, 's05_profile', None) is not None:
                    from src.openclank.analysis_snapshot_host import _owned as join_policy_initialization
                    from src.openclank.memory_job_admission import prepare_manual_policy_projection
                    try:
                        await join_policy_initialization(prepare_manual_policy_projection, state=app.state,
                                                profile=app.state.s05_profile, supervisor=_sup)
                    except Exception:
                        logger.warning('Manual memory policy projection unavailable', exc_info=True)
                if (getattr(app.state, 's05_profile', None) is not None
                    and getattr(_sup, '_s05_foreground_runtime', None) is not None):
                    try:
                        task_scheduler.configure_completed_turn_registry(state=app.state,profile=app.state.s05_profile)
                        task_scheduler.request_completed_turn_wake()
                    except Exception:
                        logger.warning('Completed-turn registry unavailable')
                logger.info("Open Clank agent supervisor started")
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
    try:
        await app.state.channel_runner.stop()
    except Exception:
        pass

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
        if canonical_memory_provider is not None and canonical_memory_provider is not memory_provider:
            await canonical_memory_provider.shutdown()
        shutdown = getattr(memory_provider, "shutdown", None)
        if callable(shutdown):
            await shutdown()

    async def _stop_browser_preview():
        service = getattr(app.state, "browser_preview_service", None)
        if service is not None:
            await asyncio.to_thread(service.close)

    async def _stop_files():
        from src.openclank.files_service_client import close_all_clients
        # Files owns native children outside the MCP manager. Closing cached
        # clients also releases their private transports on non-Linux hosts.
        await asyncio.to_thread(close_all_clients)

    await _run_shutdown_phase("startup_tasks", _cancel_startup_tasks, timeout=2.0)
    await _run_shutdown_phase("agent_runs", _stop_agent_runs, timeout=2.0)
    await _run_shutdown_phase("task_scheduler", task_scheduler.stop, timeout=2.0)
    await _run_shutdown_phase("copal_bridge", _stop_copal, timeout=2.0)
    await _run_shutdown_phase("history_supervisor", _stop_history, timeout=12.0)
    await _run_shutdown_phase("provider_oauth_flows", shutdown_provider_oauth_flows, timeout=4.0)
    await _run_shutdown_phase("mimo_supervisor", _stop_mimo, timeout=7.0)
    await _run_shutdown_phase("upload_cleanup", _cancel_upload_cleanup, timeout=2.0)
    await _run_shutdown_phase("webhooks", webhook_manager.close, timeout=2.0)
    await _run_shutdown_phase("memory_provider", _stop_memory_provider, timeout=5.0)
    await _run_shutdown_phase("browser_control", app.state.browser_control_service.close_all, timeout=6.0)
    await _run_shutdown_phase("mcp_servers", mcp_manager.disconnect_all, timeout=6.0)
    await _run_shutdown_phase("browser_preview", _stop_browser_preview, timeout=4.0)
    await _run_shutdown_phase("files_services", _stop_files, timeout=6.0)
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

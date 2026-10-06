"""Open Clank TUI control-plane and browser device approval routes.

The TUI uses the same application-token authority as every other Open Clank
client, but receives a path-confined ``oct_`` credential.  Provider secrets
never cross this API.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from core.database import (
    AgentTurn,
    ApiToken,
    ChatMessage,
    ScheduledTask,
    Session,
    TurnActor,
    TuiTurnSubmission,
    get_db_session,
    utcnow_naive,
)
from src.auth_helpers import require_user
from src.openclank.account_request_barrier import AccountRequestBarrier, AccountRequestFenced
from src.openclank.provider_store import (
    NoEligibleAccount,
    ProviderConflict,
    ProviderNotFound,
    ProviderRevisionConflict,
    ProviderStore,
    ProviderStoreError,
    ProviderValidationError,
    ShareDenied,
)


DEVICE_FLOW_TTL_SECONDS = 600
DEVICE_TOKEN_TTL_DAYS = 90
DEVICE_POLL_INTERVAL_SECONDS = 5
DEVICE_STARTS_PER_MINUTE = 8
TUI_SCOPES = frozenset(
    {
        "tui:sessions",
        "tui:providers",
        "tui:shares",
        "tui:tasks",
        "tui:workspaces",
        "tui:diagnostics",
    }
)
DEFAULT_TUI_SCOPES = tuple(sorted(TUI_SCOPES))


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceStartRequest(_StrictModel):
    device_label: str = Field(default="Open Clank TUI", max_length=120)
    scopes: list[str] | None = None


class DeviceTokenRequest(_StrictModel):
    device_code: str = Field(min_length=32, max_length=256)


class TurnStartRequest(_StrictModel):
    message: str = Field(min_length=1, max_length=200_000)
    idempotency_key: str = Field(min_length=16, max_length=128)
    attachments: list[str] = Field(default_factory=list, max_length=64)
    allow_bash: bool = False
    allow_web_search: bool = False


class PoolAdvanceRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=200)
    model_route_id: str | None = Field(default=None, max_length=128)


@dataclass
class _DeviceFlow:
    device_digest: str
    user_code: str
    approval_nonce: str
    device_label: str
    scopes: tuple[str, ...]
    remote_address: str
    created_at: float
    expires_at: float
    status: str = "pending"
    owner: str | None = None


class _DeviceFlowStore:
    """Small process-local store; flows are short-lived and credentials are DB-backed."""

    def __init__(self, *, clock=time.time) -> None:
        self._clock = clock
        self._by_device: dict[str, _DeviceFlow] = {}
        self._by_user: dict[str, str] = {}
        self._starts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    @staticmethod
    def digest(device_code: str) -> str:
        import hashlib

        return hashlib.sha256(device_code.encode("utf-8")).hexdigest()

    def _prune_locked(self, now: float) -> None:
        expired = [
            digest
            for digest, flow in self._by_device.items()
            if flow.expires_at <= now or flow.status == "consumed"
        ]
        for digest in expired:
            flow = self._by_device.pop(digest, None)
            if flow is not None:
                self._by_user.pop(flow.user_code, None)
        for address, starts in list(self._starts.items()):
            while starts and starts[0] <= now - 60:
                starts.popleft()
            if not starts:
                self._starts.pop(address, None)

    def create(
        self,
        *,
        device_label: str,
        scopes: tuple[str, ...],
        remote_address: str,
    ) -> tuple[str, _DeviceFlow]:
        now = float(self._clock())
        with self._lock:
            self._prune_locked(now)
            starts = self._starts[remote_address]
            if len(starts) >= DEVICE_STARTS_PER_MINUTE:
                raise HTTPException(429, "Too many device authorization requests")
            starts.append(now)
            while True:
                user_code = "-".join(
                    (secrets.token_hex(2).upper(), secrets.token_hex(2).upper())
                )
                if user_code not in self._by_user:
                    break
            device_code = secrets.token_urlsafe(48)
            flow = _DeviceFlow(
                device_digest=self.digest(device_code),
                user_code=user_code,
                approval_nonce=secrets.token_urlsafe(32),
                device_label=device_label,
                scopes=scopes,
                remote_address=remote_address,
                created_at=now,
                expires_at=now + DEVICE_FLOW_TTL_SECONDS,
            )
            self._by_device[flow.device_digest] = flow
            self._by_user[user_code] = flow.device_digest
            return device_code, flow

    def by_user_code(self, user_code: str) -> _DeviceFlow:
        code = user_code.strip().upper()
        now = float(self._clock())
        with self._lock:
            self._prune_locked(now)
            digest = self._by_user.get(code)
            flow = self._by_device.get(digest or "")
            if flow is None:
                raise HTTPException(404, "Unknown or expired device code")
            return flow

    def decide(
        self,
        *,
        user_code: str,
        approval_nonce: str,
        owner: str,
        approve: bool,
    ) -> _DeviceFlow:
        with self._lock:
            digest = self._by_user.get(user_code.strip().upper())
            flow = self._by_device.get(digest or "")
            if flow is None or flow.expires_at <= float(self._clock()):
                raise HTTPException(404, "Unknown or expired device code")
            if not secrets.compare_digest(flow.approval_nonce, approval_nonce):
                raise HTTPException(403, "Invalid device approval")
            if flow.status != "pending":
                raise HTTPException(409, "Device request was already decided")
            flow.owner = str(owner or "").strip().lower() if approve else None
            flow.status = "approved" if approve else "denied"
            return flow

    def begin_consume(self, device_code: str) -> _DeviceFlow:
        now = float(self._clock())
        with self._lock:
            digest = self.digest(device_code)
            flow = self._by_device.get(digest)
            if flow is None:
                raise HTTPException(404, "Unknown device authorization")
            if flow.expires_at <= now:
                self._by_device.pop(digest, None)
                self._by_user.pop(flow.user_code, None)
                raise HTTPException(410, "Device authorization expired")
            if flow.status == "pending":
                raise HTTPException(428, "Authorization pending")
            if flow.status == "denied":
                raise HTTPException(403, "Device authorization denied")
            if flow.status in {"issuing", "consumed"}:
                raise HTTPException(409, "Device authorization already consumed")
            if flow.status != "approved" or not flow.owner:
                raise HTTPException(409, "Device authorization is not usable")
            flow.status = "issuing"
            return flow

    def finish_consume(self, flow: _DeviceFlow, *, success: bool) -> None:
        with self._lock:
            current = self._by_device.get(flow.device_digest)
            if current is None:
                return
            current.status = "consumed" if success else "approved"
            if success:
                self._by_user.pop(current.user_code, None)

    def invalidate_owners(self, owners: set[str] | list[str] | tuple[str, ...]) -> int:
        """Retire approved or issuing flows for accounts being renamed/deleted."""
        owner_keys = {str(owner or "").strip().lower() for owner in owners}
        owner_keys.discard("")
        if not owner_keys:
            return 0
        retired = 0
        with self._lock:
            for digest, flow in list(self._by_device.items()):
                if flow.owner not in owner_keys or flow.status not in {"approved", "issuing"}:
                    continue
                flow.status = "consumed"
                self._by_device.pop(digest, None)
                self._by_user.pop(flow.user_code, None)
                retired += 1
        return retired


_device_flows = _DeviceFlowStore()


def _normalize_requested_scopes(values: list[str] | None) -> tuple[str, ...]:
    requested = DEFAULT_TUI_SCOPES if values is None else tuple(values)
    normalized: list[str] = []
    for raw in requested:
        scope = str(raw or "").strip()
        if scope not in TUI_SCOPES:
            raise HTTPException(400, f"Unsupported TUI scope: {scope}")
        if scope not in normalized:
            normalized.append(scope)
    if not normalized:
        raise HTTPException(400, "At least one TUI scope is required")
    return tuple(normalized)


async def _finish_mint_task(task: asyncio.Task[Any]) -> None:
    """Join a worker even when the request task is cancelled repeatedly."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            if task.done():
                return


def _owner_exists(auth_manager: Any, owner: str) -> bool:
    users = getattr(auth_manager, "users", {}) if auth_manager is not None else {}
    return any(str(name).strip().lower() == owner for name in users)


def _tui_owner(request: Request, *required_scopes: str) -> str:
    if not getattr(request.state, "api_token", False):
        raise HTTPException(401, "TUI device token required")
    if getattr(request.state, "api_token_client_kind", None) != "tui":
        raise HTTPException(403, "TUI device token required")
    owner = str(getattr(request.state, "api_token_owner", "") or "").strip()
    if not owner:
        raise HTTPException(403, "TUI token has no owner")
    granted = set(getattr(request.state, "api_token_scopes", ()) or ())
    missing = [scope for scope in required_scopes if scope not in granted]
    if missing:
        raise HTTPException(403, f"TUI token requires scope: {missing[0]}")
    return owner


def _mint_tui_token(flow: _DeviceFlow) -> tuple[str, str, Any]:
    raw_token = "oct_" + secrets.token_urlsafe(32)
    token_id = str(uuid.uuid4())
    expires_at = utcnow_naive() + timedelta(days=DEVICE_TOKEN_TTL_DAYS)
    token_hash = bcrypt.hashpw(raw_token.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    with get_db_session() as db:
        db.add(
            ApiToken(
                id=token_id,
                owner=flow.owner,
                name=flow.device_label,
                token_hash=token_hash,
                token_prefix=raw_token[:8],
                scopes=",".join(flow.scopes),
                client_kind="tui",
                expires_at=expires_at,
                device_label=flow.device_label,
                is_active=True,
            )
        )
    return token_id, raw_token, expires_at


def _owned_session(db, owner: str, session_id: str) -> Session:
    row = (
        db.query(Session)
        .filter(Session.id == session_id, Session.owner == owner)
        .first()
    )
    if row is None:
        raise HTTPException(404, "Session not found")
    return row


def _message_payload(row: ChatMessage) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    try:
        parsed = json.loads(row.meta_data or "{}")
        if isinstance(parsed, dict):
            metadata = parsed
    except (TypeError, ValueError):
        pass
    blocks = metadata.get("content_blocks")
    if not isinstance(blocks, list):
        blocks = [{"type": "text", "text": row.content}]
    return {
        "id": row.id,
        "session_id": row.session_id,
        "role": row.role,
        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        "blocks": blocks,
        "content": row.content,
        "status": metadata.get("status") or "complete",
        "turn_id": metadata.get("turn_id"),
        "source": metadata.get("source"),
        "model": metadata.get("model"),
    }


def _turn_submission_identity(owner: str, idempotency_key: str) -> str:
    return hashlib.sha256(
        f"openclank-tui-turn-v1\0{owner}\0{idempotency_key}".encode("utf-8")
    ).hexdigest()


def _turn_request_hash(session_id: str, body: TurnStartRequest) -> str:
    canonical = json.dumps(
        {
            "session": session_id,
            "message": body.message,
            "attachments": body.attachments,
            "allow_bash": body.allow_bash,
            "allow_web_search": body.allow_web_search,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _raise_provider_error(exc: ProviderStoreError) -> None:
    if isinstance(exc, ProviderRevisionConflict):
        raise HTTPException(
            412,
            str(exc),
            headers={"ETag": f'"{exc.current}"'},
        ) from exc
    if isinstance(exc, ProviderNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, ShareDenied):
        raise HTTPException(403, str(exc)) from exc
    if isinstance(exc, ProviderValidationError):
        raise HTTPException(422, str(exc)) from exc
    if isinstance(exc, (NoEligibleAccount, ProviderConflict)):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(500, "Provider control operation failed") from exc


def _idempotent_provider_mutation(
    store: ProviderStore,
    *,
    owner: str,
    operation: str,
    idempotency_key: str,
    material: dict[str, Any],
) -> tuple[str, Any | None]:
    digest = store.idempotency_request_digest(
        owner=owner,
        operation=operation,
        payload=material,
    )
    replay = store.lookup_idempotency(
        owner=owner,
        operation=operation,
        idempotency_key=idempotency_key,
        request_digest=digest,
    )
    return digest, replay


async def _json_request_for_chat(request: Request, payload: dict[str, Any]) -> Request:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    scope = dict(request.scope)
    scope.update(
        {
            "method": "POST",
            "path": "/api/chat_stream",
            "raw_path": b"/api/chat_stream",
            "query_string": b"",
            "headers": [
                (name, value)
                for name, value in request.scope.get("headers", [])
                if name.lower() not in {b"content-type", b"content-length"}
            ]
            + [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(encoded)).encode("ascii")),
            ],
        }
    )
    return Request(scope, receive)


def setup_tui_routes(
    provider_store: ProviderStore | None = None,
    *,
    auth_manager: Any | None = None,
    request_barrier: AccountRequestBarrier | None = None,
) -> APIRouter:
    provider_store = provider_store or ProviderStore()
    router = APIRouter(tags=["tui-control-plane"])

    @router.get("/api/tui/v1/info")
    def public_info():
        from core.constants import APP_VERSION

        engine: dict[str, Any] = {"verified": False}
        try:
            from src.openclank.engine_build import verify_install

            verification = verify_install()
            engine = {
                "verified": bool(verification.ok),
                "version": verification.version,
                "target": verification.target,
                "source_sha256": verification.source_sha256,
                "errors": list(verification.errors),
            }
        except Exception:
            pass
        return {
            "product": "Open Clank",
            "version": APP_VERSION,
            "api_version": 1,
            "device_authorization": True,
            "engine": engine,
        }

    @router.post("/api/tui/v1/device/start")
    def start_device(request: Request, body: DeviceStartRequest):
        scopes = _normalize_requested_scopes(body.scopes)
        address = request.client.host if request.client else "unknown"
        label = body.device_label.strip() or "Open Clank TUI"
        device_code, flow = _device_flows.create(
            device_label=label,
            scopes=scopes,
            remote_address=address,
        )
        origin = str(request.base_url).rstrip("/")
        return {
            "device_code": device_code,
            "user_code": flow.user_code,
            "verification_uri": f"{origin}/api/tui/device",
            "verification_uri_complete": (
                f"{origin}/api/tui/device?user_code={flow.user_code}"
            ),
            "expires_in": DEVICE_FLOW_TTL_SECONDS,
            "interval": DEVICE_POLL_INTERVAL_SECONDS,
            "scopes": list(scopes),
        }

    @router.post("/api/tui/v1/device/token")
    async def exchange_device(request: Request, body: DeviceTokenRequest):
        flow = _device_flows.begin_consume(body.device_code)
        admission = None
        try:
            owner = str(flow.owner or "").strip().lower()
            if not owner or not _owner_exists(auth_manager, owner):
                _device_flows.invalidate_owners({owner})
                raise HTTPException(410, "Device authorization owner no longer exists")
            if request_barrier is not None:
                admission = await request_barrier.acquire(owner)
                if not _owner_exists(auth_manager, owner):
                    _device_flows.invalidate_owners({owner})
                    raise HTTPException(410, "Device authorization owner no longer exists")
            mint_task = asyncio.create_task(asyncio.to_thread(_mint_tui_token, flow))
            try:
                token_id, raw_token, expires_at = await asyncio.shield(mint_task)
            except BaseException:
                await _finish_mint_task(mint_task)
                raise
        except AccountRequestFenced as exc:
            _device_flows.finish_consume(flow, success=False)
            raise HTTPException(409, "Account lifecycle operation in progress") from exc
        except Exception:
            _device_flows.finish_consume(flow, success=False)
            raise
        finally:
            if admission is not None:
                await request_barrier.release(admission)
        _device_flows.finish_consume(flow, success=True)
        invalidator = getattr(request.app.state, "invalidate_token_cache", None)
        if callable(invalidator):
            invalidator()
        return {
            "token_type": "Bearer",
            "access_token": raw_token,
            "token_id": token_id,
            "expires_at": expires_at.isoformat() + "Z",
            "scopes": list(flow.scopes),
        }

    def invalidate_owner_runtime(owners: Any) -> int:
        if isinstance(owners, str):
            owners = (owners,)
        return _device_flows.invalidate_owners(owners)

    router.invalidate_owner_runtime = invalidate_owner_runtime

    @router.get("/api/tui/device", response_class=HTMLResponse)
    def device_approval_page(request: Request, user_code: str = ""):
        owner = require_user(request)
        flow = _device_flows.by_user_code(user_code)
        scope_items = "".join(
            f"<li>{html.escape(scope.removeprefix('tui:').replace('_', ' '))}</li>"
            for scope in flow.scopes
        )
        return HTMLResponse(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Approve Open Clank TUI</title>"
            "<style>body{font-family:system-ui;max-width:38rem;margin:4rem auto;padding:0 1rem}"
            ".card{border:1px solid #5555;border-radius:14px;padding:1.5rem}"
            "button{padding:.7rem 1rem;margin-right:.5rem}</style></head><body>"
            "<div class='card'><h1>Approve Open Clank TUI</h1>"
            f"<p><strong>{html.escape(flow.device_label)}</strong> requests access as "
            f"<strong>{html.escape(owner)}</strong>.</p>"
            f"<p>Request origin: {html.escape(flow.remote_address)}</p><ul>{scope_items}</ul>"
            "<form method='post' action='/api/tui/device'>"
            f"<input type='hidden' name='user_code' value='{html.escape(flow.user_code)}'>"
            f"<input type='hidden' name='approval_nonce' value='{html.escape(flow.approval_nonce)}'>"
            "<button name='action' value='approve' type='submit'>Approve</button>"
            "<button name='action' value='deny' type='submit'>Deny</button>"
            "</form></div></body></html>"
        )

    @router.post("/api/tui/device", response_class=HTMLResponse)
    def decide_device(
        request: Request,
        user_code: str = Form(...),
        approval_nonce: str = Form(...),
        action: str = Form(...),
    ):
        owner = require_user(request)
        if action not in {"approve", "deny"}:
            raise HTTPException(400, "Invalid device decision")
        flow = _device_flows.decide(
            user_code=user_code,
            approval_nonce=approval_nonce,
            owner=owner,
            approve=action == "approve",
        )
        message = "Device approved. You can return to the TUI." if flow.status == "approved" else "Device request denied."
        return HTMLResponse(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Open Clank TUI</title></head><body>"
            f"<h1>{html.escape(message)}</h1></body></html>"
        )

    @router.get("/api/tui/v1/bootstrap")
    def bootstrap(request: Request):
        owner = _tui_owner(request, "tui:sessions")
        with get_db_session() as db:
            sessions = (
                db.query(Session)
                .filter(Session.owner == owner)
                .order_by(Session.last_accessed.desc())
                .limit(100)
                .all()
            )
            session_rows = [
                {
                    "id": row.id,
                    "name": row.name,
                    "model": row.model,
                    "archived": bool(row.archived),
                    "mode": row.mode,
                    "last_accessed": row.last_accessed.isoformat() if row.last_accessed else None,
                    "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
                    "transcript_revision": row.transcript_revision,
                }
                for row in sessions
            ]
        return {
            "principal": {"owner": owner},
            "capabilities": {
                "sessions": True,
                "providers": "tui:providers" in set(request.state.api_token_scopes),
                "shares": "tui:shares" in set(request.state.api_token_scopes),
                "tasks": "tui:tasks" in set(request.state.api_token_scopes),
                "workspaces": "tui:workspaces" in set(request.state.api_token_scopes),
                "diagnostics": "tui:diagnostics" in set(request.state.api_token_scopes),
            },
            "sessions": session_rows,
        }

    @router.get("/api/tui/v1/sessions")
    def list_sessions(
        request: Request,
        archived: bool = False,
        offset: int = 0,
        limit: int = 100,
    ):
        owner = _tui_owner(request, "tui:sessions")
        safe_offset = max(0, offset)
        safe_limit = min(200, max(1, limit))
        with get_db_session() as db:
            rows = (
                db.query(Session)
                .filter(Session.owner == owner, Session.archived == bool(archived))
                .order_by(Session.last_accessed.desc())
                .offset(safe_offset)
                .limit(safe_limit)
                .all()
            )
            return {
                "items": [row.to_dict() for row in rows],
                "offset": safe_offset,
                "limit": safe_limit,
            }

    @router.get("/api/tui/v1/sessions/{session_id}/messages")
    def list_messages(
        request: Request,
        session_id: str,
        before: str | None = None,
        limit: int = 100,
    ):
        owner = _tui_owner(request, "tui:sessions")
        safe_limit = min(200, max(1, limit))
        with get_db_session() as db:
            session = _owned_session(db, owner, session_id)
            query = db.query(ChatMessage).filter(ChatMessage.session_id == session.id)
            if before:
                cursor = db.query(ChatMessage).filter(
                    ChatMessage.id == before,
                    ChatMessage.session_id == session.id,
                ).first()
                if cursor is None:
                    raise HTTPException(400, "Invalid message cursor")
                query = query.filter(ChatMessage.timestamp < cursor.timestamp)
            rows = query.order_by(ChatMessage.timestamp.desc()).limit(safe_limit + 1).all()
            has_more = len(rows) > safe_limit
            rows = list(reversed(rows[:safe_limit]))
            return {
                "session_id": session.id,
                "transcript_revision": session.transcript_revision,
                "items": [_message_payload(row) for row in rows],
                "has_more": has_more,
                "next_before": rows[0].id if has_more and rows else None,
            }

    @router.get("/api/tui/v1/sessions/{session_id}/turns/active")
    def active_turn(request: Request, session_id: str):
        owner = _tui_owner(request, "tui:sessions")
        with get_db_session() as db:
            _owned_session(db, owner, session_id)
        from src import agent_runs

        return {
            "session_id": session_id,
            "status": agent_runs.get_status(session_id),
            "active": agent_runs.is_active(session_id),
            "run_id": agent_runs.get_run_id(session_id),
        }

    @router.post("/api/tui/v1/sessions/{session_id}/turns")
    async def start_turn(
        request: Request,
        session_id: str,
        body: TurnStartRequest,
    ):
        owner = _tui_owner(request, "tui:sessions")
        if len(set(body.attachments)) != len(body.attachments):
            raise HTTPException(400, "attachments must not contain duplicates")
        with get_db_session() as db:
            _owned_session(db, owner, session_id)

        from src import agent_runs

        row_id = _turn_submission_identity(owner, body.idempotency_key)
        request_hash = _turn_request_hash(session_id, body)
        replay = False
        with get_db_session() as db:
            row = db.query(TuiTurnSubmission).filter(TuiTurnSubmission.id == row_id).first()
            if row is not None:
                if row.request_hash != request_hash or row.session_id != session_id:
                    raise HTTPException(409, "idempotency key was used for a different turn")
                replay = True
            else:
                row = TuiTurnSubmission(
                    id=row_id,
                    owner=owner,
                    session_id=session_id,
                    idempotency_key=body.idempotency_key,
                    request_hash=request_hash,
                    state="submitting",
                )
                db.add(row)

        if replay:
            run = agent_runs.get_run(session_id)
            if run is not None:
                return StreamingResponse(
                    agent_runs.subscribe(session_id, expected_run=run),
                    media_type="text/event-stream",
                    headers={"X-Agent-Run-ID": run.run_id},
                )

            async def completed_replay():
                yield "event: replay\ndata: {\"status\":\"complete\"}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(completed_replay(), media_type="text/event-stream")

        handler = getattr(request.app.state, "openclank_chat_stream_handler", None)
        if not callable(handler):
            raise HTTPException(503, "canonical turn service is unavailable")
        synthetic = await _json_request_for_chat(
            request,
            {
                "session": session_id,
                "message": body.message,
                "attachments": body.attachments,
                "allow_bash": body.allow_bash,
                "allow_web_search": body.allow_web_search,
                "mode": "agent",
            },
        )
        try:
            response = await handler(synthetic)
        except Exception:
            with get_db_session() as db:
                row = db.query(TuiTurnSubmission).filter(TuiTurnSubmission.id == row_id).first()
                if row is not None:
                    row.state = "failed"
                    row.error_code = "TURN_REJECTED"
                    db.add(row)
            raise
        with get_db_session() as db:
            row = db.query(TuiTurnSubmission).filter(TuiTurnSubmission.id == row_id).first()
            if row is not None:
                row.state = "started"
                db.add(row)
        return response

    @router.get("/api/tui/v1/sessions/{session_id}/turns/active/stream")
    async def stream_active_turn(request: Request, session_id: str, after: int = 0):
        owner = _tui_owner(request, "tui:sessions")
        with get_db_session() as db:
            _owned_session(db, owner, session_id)
        from src import agent_runs

        run = agent_runs.get_run(session_id)
        if run is None:
            raise HTTPException(404, "No retained turn for this session")
        return StreamingResponse(
            agent_runs.subscribe(session_id, expected_run=run, after_seq=max(0, after)),
            media_type="text/event-stream",
            headers={"X-Agent-Run-ID": run.run_id},
        )

    @router.post("/api/tui/v1/sessions/{session_id}/turns/active/stop")
    def stop_active_turn(request: Request, session_id: str):
        owner = _tui_owner(request, "tui:sessions")
        with get_db_session() as db:
            _owned_session(db, owner, session_id)
        from src import agent_runs

        expected_run_id = request.headers.get("X-Agent-Run-ID") or None
        return {
            "session_id": session_id,
            "stopped": agent_runs.stop(session_id, expected_run_id=expected_run_id),
            "run_id": agent_runs.get_run_id(session_id),
        }

    @router.get("/api/tui/v1/sessions/{session_id}/actors")
    def session_actors(request: Request, session_id: str):
        owner = _tui_owner(request, "tui:sessions")
        with get_db_session() as db:
            _owned_session(db, owner, session_id)
            message_ids = [
                row[0]
                for row in db.query(ChatMessage.id)
                .filter(ChatMessage.session_id == session_id)
                .all()
            ]
            if not message_ids:
                return {"items": []}
            roots = {
                row.root_turn_id: row
                for row in db.query(AgentTurn)
                .filter(AgentTurn.root_turn_id.in_(message_ids))
                .all()
            }
            actors = (
                db.query(TurnActor)
                .filter(TurnActor.root_turn_id.in_(list(roots)))
                .order_by(TurnActor.created_at.asc())
                .all()
                if roots
                else []
            )
            return {
                "items": [
                    {
                        "root_turn_id": row.root_turn_id,
                        "actor_id": row.actor_id,
                        "parent_actor_id": row.parent_actor_id,
                        "agent": row.agent,
                        "mode": row.mode,
                        "description": row.description,
                        "background": bool(row.background),
                        "status": row.status,
                        "outcome": row.outcome,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                    }
                    for row in actors
                ]
            }

    @router.get("/api/tui/v1/providers")
    def provider_pools(request: Request):
        """Return owner-visible provider pools without credential material."""

        owner = _tui_owner(request, "tui:providers")
        from routes.provider_v1_routes import (
            _account_json,
            _connection_json,
            _iso,
            _model_json,
        )

        try:
            connections = provider_store.list_connections(owner=owner)
            result: list[dict[str, Any]] = []
            for connection in connections:
                accounts = provider_store.list_accounts(
                    owner=owner,
                    connection_id=connection.id,
                )
                models = provider_store.list_model_routes(
                    owner=owner,
                    connection_id=connection.id,
                )
                account_rows = [_account_json(row) for row in accounts]
                model_rows: list[dict[str, Any]] = []
                available_accounts: set[str] = set()
                for model in models:
                    eligibility = provider_store.model_eligibility(
                        owner=owner,
                        model_route_id=model.id,
                    )
                    safe_eligibility = []
                    for item in eligibility:
                        account_id = str(item.get("account_id") or "")
                        if item.get("eligible"):
                            available_accounts.add(account_id)
                        safe_eligibility.append(
                            {
                                "account_id": account_id,
                                "entitled": bool(item.get("entitled")),
                                "eligible": bool(item.get("eligible")),
                                "account_state": item.get("account_state"),
                                "model_state": item.get("model_state"),
                                "cooldown_until": _iso(
                                    item.get("model_cooldown_until")
                                    or item.get("account_cooldown_until")
                                ),
                                "quota_reset_at": _iso(
                                    item.get("model_quota_reset_at")
                                    or item.get("account_quota_reset_at")
                                ),
                                "last_error_code": (
                                    item.get("model_last_error_code")
                                    or item.get("account_last_error_code")
                                ),
                            }
                        )
                    model_item = _model_json(model)
                    model_item["eligibility"] = safe_eligibility
                    model_rows.append(model_item)
                item = _connection_json(connection)
                item.update(
                    {
                        "accounts": account_rows,
                        "models": model_rows,
                        "account_count": len(account_rows),
                        "healthy_count": len(available_accounts),
                        "keyless": not account_rows,
                    }
                )
                result.append(item)
        except ProviderStoreError as exc:
            _raise_provider_error(exc)
        return {"connections": result}

    @router.get("/api/tui/v1/provider-bindings")
    def provider_bindings(request: Request):
        owner = _tui_owner(request, "tui:providers")
        from routes.provider_v1_routes import _binding_group

        try:
            rows = provider_store.list_route_bindings(owner=owner)
        except ProviderStoreError as exc:
            _raise_provider_error(exc)
        grouped: dict[str, list[Any]] = {}
        for row in rows:
            grouped.setdefault(row.purpose, []).append(row)
        return {
            "bindings": [
                _binding_group(purpose, grouped[purpose])
                for purpose in sorted(grouped)
            ]
        }

    @router.post("/api/tui/v1/providers/{connection_id}/use-next")
    def provider_use_next(
        connection_id: str,
        body: PoolAdvanceRequest,
        request: Request,
    ):
        owner = _tui_owner(request, "tui:providers")
        operation = f"tui.pool.use-next:{connection_id}"
        material = {
            "connection_id": connection_id,
            "expected_revision": body.expected_revision,
            "model_route_id": body.model_route_id,
        }
        try:
            digest, replay = _idempotent_provider_mutation(
                provider_store,
                owner=owner,
                operation=operation,
                idempotency_key=body.idempotency_key,
                material=material,
            )
            if replay is not None:
                result = dict(replay.response_body)
                result["idempotency_replayed"] = True
                return result
            connection, account = provider_store.advance_pool_cursor(
                owner=owner,
                connection_id=connection_id,
                expected_revision=body.expected_revision,
                model_route_id=body.model_route_id,
            )
            from routes.provider_v1_routes import _account_json

            result = {
                "connection_id": connection.id,
                "revision": int(connection.revision),
                "next_account": _account_json(account),
            }
            provider_store.record_idempotency(
                owner=owner,
                operation=operation,
                idempotency_key=body.idempotency_key,
                request_digest=digest,
                status_code=200,
                response_body=result,
                resource_id=connection.id,
            )
        except ProviderStoreError as exc:
            _raise_provider_error(exc)
        result["idempotency_replayed"] = False
        return result

    @router.get("/api/tui/v1/shares")
    def provider_shares(request: Request):
        owner = _tui_owner(request, "tui:shares")
        from routes.provider_v1_routes import (
            _owned_share_json,
            _received_share_json,
        )

        try:
            owned = provider_store.list_share_grants(owner=owner)
            received = provider_store.received_share_projections(recipient=owner)
            return {
                "owned": [_owned_share_json(row) for row in owned],
                "received": [
                    _received_share_json(row) for row in received
                ],
            }
        except ProviderStoreError as exc:
            _raise_provider_error(exc)

    @router.get("/api/tui/v1/diagnostics")
    def tui_diagnostics(request: Request):
        owner = _tui_owner(request, "tui:diagnostics")
        verification = getattr(request.app.state, "engine_verification", None)
        engine = {
            "verified": bool(getattr(verification, "ok", False)),
            "version": getattr(verification, "version", None),
            "target": getattr(verification, "target", None),
            "source_sha256": getattr(verification, "source_sha256", None),
            "issue_count": len(getattr(verification, "errors", ()) or ()),
        }
        supervisor_result: dict[str, Any] = {"ok": False, "status": "unavailable"}
        supervisor = getattr(request.app.state, "mimo_supervisor", None)
        if supervisor is not None:
            try:
                raw = dict(supervisor.readiness())
                owner_state = dict((raw.get("owners") or {}).get(owner) or {})
                supervisor_result = {
                    "ok": bool(raw.get("ok")),
                    "event_loop": bool(raw.get("event_loop")),
                    "owner_worker_count": int(raw.get("owner_workers") or 0),
                    "share_worker_count": int(raw.get("share_workers") or 0),
                    "current_owner": {
                        "status": owner_state.get("status") or "not_started",
                        "generation": owner_state.get("generation"),
                    },
                }
            except Exception:
                supervisor_result = {"ok": False, "status": "unavailable"}
        try:
            connections = provider_store.list_connections(owner=owner)
            account_count = sum(
                len(
                    provider_store.list_accounts(
                        owner=owner,
                        connection_id=connection.id,
                    )
                )
                for connection in connections
            )
        except ProviderStoreError as exc:
            _raise_provider_error(exc)
        return {
            "ready": bool(engine["verified"] and supervisor_result.get("ok")),
            "engine": engine,
            "runtime": supervisor_result,
            "providers": {
                "connection_count": len(connections),
                "account_count": account_count,
            },
        }

    @router.get("/api/tui/v1/tasks")
    def list_tasks(request: Request, limit: int = 100):
        owner = _tui_owner(request, "tui:tasks")
        with get_db_session() as db:
            rows = (
                db.query(ScheduledTask)
                .filter(ScheduledTask.owner == owner)
                .order_by(ScheduledTask.created_at.desc())
                .limit(min(200, max(1, limit)))
                .all()
            )
            return {
                "items": [
                    {
                        "id": row.id,
                        "name": row.name,
                        "status": row.status,
                        "task_type": row.task_type,
                        "schedule": row.schedule,
                        "next_run": row.next_run.isoformat() if row.next_run else None,
                        "last_run": row.last_run.isoformat() if row.last_run else None,
                        "run_count": row.run_count or 0,
                        "session_id": row.session_id,
                        "model": row.model,
                    }
                    for row in rows
                ]
            }

    @router.get("/api/tui/v1/events")
    async def events(request: Request, after: int = 0):
        _tui_owner(request, "tui:sessions")

        async def stream():
            sequence = max(0, int(after))
            while True:
                if await request.is_disconnected():
                    return
                sequence += 1
                yield f"id: {sequence}\nevent: heartbeat\ndata: {{\"seq\":{sequence}}}\n\n"
                await asyncio.sleep(15)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @router.delete("/api/tui/v1/device/current")
    def revoke_current_device(request: Request):
        _tui_owner(request)
        token_id = getattr(request.state, "api_token_id", None)
        with get_db_session() as db:
            token = db.query(ApiToken).filter(ApiToken.id == token_id).first()
            if token is None or token.client_kind != "tui":
                raise HTTPException(404, "TUI device token not found")
            token.is_active = False
            token.revoked_at = utcnow_naive()
            db.add(token)
        invalidator = getattr(request.app.state, "invalidate_token_cache", None)
        if callable(invalidator):
            invalidator()
        return {"status": "revoked"}

    return router


__all__ = [
    "DEFAULT_TUI_SCOPES",
    "TUI_SCOPES",
    "_DeviceFlowStore",
    "_normalize_requested_scopes",
    "setup_tui_routes",
]

"""Narrow Open Clank adapter for the HTTP server already owned by `mimo acp`."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from src.auth_helpers import require_user


_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FLOW_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SESSION_COOKIE = "odysseus_session"
_FLOW_TTL_SECONDS = 600
_FLOW_SECRET = secrets.token_bytes(32)
_AUTH_CAPABILITIES: dict[str, dict[int, str]] = {
    "openai": {0: "device_code", 1: "api_key"},
    "xiaomi": {0: "paste_code"},
    "github-copilot": {0: "device_code"},
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OAuthStart(_StrictModel):
    method: int = Field(ge=0, le=50)
    inputs: dict[str, str] | None = None


class OAuthCallback(_StrictModel):
    flow_id: str = Field(min_length=16, max_length=128)
    code: str = Field(min_length=1, max_length=16_384)


class OAuthFlowAction(_StrictModel):
    flow_id: str = Field(min_length=16, max_length=128)


class ApiKeyCredential(_StrictModel):
    key: str = Field(min_length=1, max_length=131_072)


@dataclass
class _OAuthFlow:
    flow_id: str
    provider_id: str
    method: int
    capability: str
    owner: str
    browser_session: str
    state: str
    expires_at: float
    status: str = "pending"
    error: str = ""
    catalog_refreshed: bool = False
    task: asyncio.Task | None = None


class _OAuthFlowStore:
    def __init__(self, *, time_func=time.time):
        self._flows: dict[str, _OAuthFlow] = {}
        self._lock = threading.Lock()
        self._time = time_func

    def _now(self) -> float:
        return float(self._time())

    def now(self) -> float:
        return self._now()

    def _prune(self, now: float) -> None:
        cutoff = now - 60
        for flow_id in [
            key
            for key, flow in self._flows.items()
            if flow.expires_at < cutoff
        ]:
            self._flows.pop(flow_id, None)

    def add(self, flow: _OAuthFlow) -> None:
        with self._lock:
            self._prune(self._now())
            if flow.flow_id in self._flows:
                raise RuntimeError("Duplicate provider login flow")
            self._flows[flow.flow_id] = flow

    def discard(self, flow_id: str) -> None:
        with self._lock:
            self._flows.pop(flow_id, None)

    def require(
        self,
        flow_id: str,
        *,
        owner: str,
        browser_session: str,
        provider_id: str | None = None,
    ) -> _OAuthFlow:
        if not _FLOW_ID.fullmatch(flow_id):
            raise HTTPException(404, "Unknown provider login flow")
        with self._lock:
            flow = self._flows.get(flow_id)
            if flow is None or flow.owner != owner:
                raise HTTPException(404, "Unknown provider login flow")
            if flow.browser_session != browser_session:
                raise HTTPException(404, "Unknown provider login flow")
            if provider_id is not None and flow.provider_id != provider_id:
                raise HTTPException(404, "Unknown provider login flow")
            if flow.expires_at <= self._now() and flow.status not in {"connected", "failed"}:
                flow.status = "expired"
                flow.error = "Provider login expired"
            if flow.status == "expired":
                raise HTTPException(410, "Provider login expired")
            return flow

    def consume(
        self,
        flow_id: str,
        *,
        owner: str,
        browser_session: str,
        provider_id: str,
        capability: str,
    ) -> _OAuthFlow:
        flow = self.require(
            flow_id,
            owner=owner,
            browser_session=browser_session,
            provider_id=provider_id,
        )
        with self._lock:
            if flow.capability != capability:
                raise HTTPException(400, "Wrong completion method for provider login")
            if flow.status != "pending":
                raise HTTPException(409, "Provider login was already completed")
            flow.status = "processing"
            return flow

    def start_background(self, flow_id: str) -> _OAuthFlow:
        with self._lock:
            flow = self._flows[flow_id]
            if flow.status != "pending":
                raise RuntimeError("Provider login flow already started")
            flow.status = "processing"
            return flow

    def finish(
        self,
        flow_id: str,
        *,
        connected: bool,
        error: str = "",
        catalog_refreshed: bool = False,
    ) -> None:
        with self._lock:
            flow = self._flows.get(flow_id)
            if flow is None or flow.status in {"cancelled", "expired"}:
                return
            flow.status = "connected" if connected else "failed"
            flow.error = _bounded(error, 512)
            flow.catalog_refreshed = bool(catalog_refreshed)

    def attach_task(self, flow_id: str, task: asyncio.Task) -> None:
        with self._lock:
            flow = self._flows.get(flow_id)
            if flow is not None:
                flow.task = task

    def cancel(
        self,
        flow_id: str,
        *,
        owner: str,
        browser_session: str,
    ) -> tuple[_OAuthFlow, asyncio.Task | None]:
        flow = self.require(
            flow_id,
            owner=owner,
            browser_session=browser_session,
        )
        with self._lock:
            if flow.status in {"connected", "failed", "cancelled"}:
                raise HTTPException(409, "Provider login is already finished")
            flow.status = "cancelled"
            flow.error = ""
            return flow, flow.task

    def cancel_owner(self, owner: str) -> list[tuple[_OAuthFlow, asyncio.Task | None]]:
        cancelled: list[tuple[_OAuthFlow, asyncio.Task | None]] = []
        with self._lock:
            for flow in self._flows.values():
                if flow.owner != owner or flow.status in {"connected", "failed", "cancelled", "expired"}:
                    continue
                flow.status = "cancelled"
                flow.error = ""
                cancelled.append((flow, flow.task))
        return cancelled


_oauth_flows = _OAuthFlowStore()


def _validate_supervisor(supervisor):
    if not supervisor or not supervisor.is_alive():
        raise HTTPException(503, "Open Clank agent is not running")
    base_url = supervisor.http_base_url
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        raise HTTPException(503, "Open Clank agent provider service is not loopback-only")
    return supervisor


def _supervisor(request: Request):
    supervisor = getattr(request.app.state, "mimo_supervisor", None)
    return _validate_supervisor(supervisor)


async def _owner_supervisor(request: Request, owner: str | None = None):
    supervisor = getattr(request.app.state, "mimo_supervisor", None)
    if supervisor and hasattr(supervisor, "for_owner"):
        supervisor = await supervisor.for_owner(owner if owner is not None else require_user(request))
    return _validate_supervisor(supervisor)


def _provider_id(value: str) -> str:
    if not _PROVIDER_ID.fullmatch(value):
        raise HTTPException(400, "Invalid provider ID")
    return value


def _bounded(value: Any, limit: int = 512) -> str:
    return str(value or "")[:limit]


def _browser_identity(request: Request) -> tuple[str, str]:
    owner = require_user(request)
    cookies = getattr(request, "cookies", {}) or {}
    session_token = str(cookies.get(_SESSION_COOKIE) or "")
    auth_manager = getattr(getattr(request, "app", None), "state", None)
    auth_manager = getattr(auth_manager, "auth_manager", None)
    if owner and getattr(auth_manager, "is_configured", False) and not session_token:
        raise HTTPException(401, "Browser session required for provider login")
    raw = session_token if session_token else f"auth-disabled:{owner}"
    return owner, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _public_origin(request: Request) -> str:
    configured = (
        os.getenv("OAUTH_REDIRECT_BASE_URL", "").strip()
        or os.getenv("APP_PUBLIC_URL", "").strip()
    )
    if not configured:
        try:
            from src.settings import get_setting

            configured = str(get_setting("app_public_url", "") or "").strip()
        except Exception:
            configured = ""
    if configured:
        parsed = urlsplit(configured.rstrip("/"))
    else:
        request_url = getattr(request, "url", None)
        parsed = urlsplit(str(request_url or ""))
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise HTTPException(400, "Set APP_PUBLIC_URL before using remote browser login")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HTTPException(400, "Invalid configured Open Clank public URL")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(400, "Remote browser login requires HTTPS")
    return f"{parsed.scheme}://{parsed.netloc}"


def _new_flow_id() -> str:
    return secrets.token_urlsafe(32)


def _flow_state(
    flow_id: str,
    provider_id: str,
    owner: str,
    browser_session: str,
) -> str:
    nonce = secrets.token_urlsafe(24)
    signature = _flow_state_signature(
        flow_id,
        provider_id,
        owner,
        browser_session,
        nonce,
    )
    return f"{flow_id}.{nonce}.{signature}"


def _valid_flow_state(flow: _OAuthFlow, state: str) -> bool:
    try:
        flow_id, nonce, _ = state.split(".", 2)
    except ValueError:
        return False
    if flow_id != flow.flow_id:
        return False
    expected = _flow_state_signature(
        flow.flow_id,
        flow.provider_id,
        flow.owner,
        flow.browser_session,
        nonce,
    )
    return hmac.compare_digest(state, f"{flow.flow_id}.{nonce}.{expected}")


def _state_flow_id(state: str) -> str:
    flow_id = state.split(".", 1)[0]
    if not _FLOW_ID.fullmatch(flow_id):
        raise HTTPException(400, "Invalid provider login state")
    return flow_id


def _flow_state_signature(
    flow_id: str,
    provider_id: str,
    owner: str,
    browser_session: str,
    nonce: str,
) -> str:
    message = "\0".join((flow_id, provider_id, owner, browser_session, nonce)).encode()
    return base64.urlsafe_b64encode(
        hmac.new(_FLOW_SECRET, message, hashlib.sha256).digest()
    ).rstrip(b"=").decode()


def _method_capability(provider_id: str, index: int, method: dict[str, Any]) -> str:
    configured = _AUTH_CAPABILITIES.get(provider_id, {}).get(index)
    if configured:
        return configured
    return "api_key" if method.get("type") == "api" else "redirect"


def _sanitize_methods(provider_id: str, value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for index, method in enumerate(value[:51]):
        if not isinstance(method, dict) or method.get("type") not in {"oauth", "api"}:
            continue
        clean: dict[str, Any] = {
            "index": index,
            "type": method["type"],
            "label": _bounded(method.get("label") or ("API key" if method["type"] == "api" else "OAuth")),
            "capability": _method_capability(provider_id, index, method),
        }
        prompts = []
        for prompt in method.get("prompts") or []:
            if not isinstance(prompt, dict) or prompt.get("type") not in {"text", "select"}:
                continue
            item: dict[str, Any] = {
                "type": prompt["type"],
                "key": _bounded(prompt.get("key"), 128),
                "message": _bounded(prompt.get("message"), 2_048),
            }
            if prompt.get("placeholder") is not None:
                item["placeholder"] = _bounded(prompt["placeholder"], 512)
            if prompt["type"] == "select":
                item["options"] = [
                    {
                        "label": _bounded(option.get("label")),
                        "value": _bounded(option.get("value"), 256),
                        **({"hint": _bounded(option.get("hint"))} if option.get("hint") else {}),
                    }
                    for option in (prompt.get("options") or [])[:100]
                    if isinstance(option, dict)
                ]
            when = prompt.get("when")
            if isinstance(when, dict) and when.get("op") in {"eq", "neq"}:
                item["when"] = {
                    "key": _bounded(when.get("key"), 128),
                    "op": when["op"],
                    "value": _bounded(when.get("value"), 256),
                }
            prompts.append(item)
        if prompts:
            clean["prompts"] = prompts
        result.append(clean)
    return result


def _capability_flags(methods: list[dict[str, Any]]) -> dict[str, bool]:
    enabled = {str(method.get("capability") or "") for method in methods}
    return {
        "redirect": "redirect" in enabled,
        "device_code": "device_code" in enabled,
        "paste_code": "paste_code" in enabled,
        "api_key": "api_key" in enabled,
    }


def _safe_oauth_url(value: Any) -> str:
    url = _bounded(value, 8_192)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(502, "Provider returned an unsafe authorization URL")
    return url


def _flow_page(success: bool, message: str, *, status_code: int = 200) -> HTMLResponse:
    title = "Provider connected" if success else "Provider login failed"
    safe_title = html.escape(title)
    safe_message = html.escape(_bounded(message, 512))
    color = "#5bd89a" if success else "#ff6b6b"
    return HTMLResponse(
        f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title></head>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#181a20;color:#f5f1e8;font-family:system-ui,sans-serif">
<main style="max-width:32rem;padding:2rem;text-align:center"><h1 style="color:{color}">{safe_title}</h1>
<p>{safe_message}</p><p>You can close this tab and return to Open Clank.</p></main>
<script>setTimeout(() => window.close(), 1800)</script></body></html>""",
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'",
        },
    )


async def _native(supervisor, method: str, path: str, body: dict | None = None, *, timeout: float = 20.0):
    try:
        async with supervisor.internal_http_client(timeout=timeout) as client:
            response = await client.request(method, path, json=body)
        response.raise_for_status()
        return response.json()
    except asyncio.CancelledError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Open Clank agent provider operation failed") from exc


async def _catalog(supervisor) -> tuple[dict[str, Any], dict[str, Any]]:
    providers, methods = await asyncio.gather(
        _native(supervisor, "GET", "/provider"),
        _native(supervisor, "GET", "/provider/auth"),
    )
    if not isinstance(providers, dict) or not isinstance(methods, dict):
        raise HTTPException(502, "Open Clank agent returned an invalid provider catalog")
    return providers, methods


def _known_provider(provider_id: str, providers: dict[str, Any], methods: dict[str, Any]) -> tuple[dict, list]:
    all_providers = providers.get("all") if isinstance(providers.get("all"), list) else []
    provider = next(
        (item for item in all_providers if isinstance(item, dict) and item.get("id") == provider_id),
        None,
    )
    if provider is None and provider_id not in methods:
        raise HTTPException(404, "Unknown Open Clank agent provider")
    return provider or {"id": provider_id, "name": provider_id}, methods.get(provider_id) or []


def _check_method(index: int, provider_methods: list, expected: str) -> None:
    if index >= len(provider_methods) or not isinstance(provider_methods[index], dict):
        raise HTTPException(400, "Invalid authentication method")
    if provider_methods[index].get("type") != expected:
        raise HTTPException(400, f"Selected method is not {expected}")


async def _refresh(supervisor, owner: str) -> bool:
    # Every connect/disconnect ends here: mirror the runtime's auth store
    # into app.db so Open Clank owns the credentials, not the child's files.
    try:
        sync = getattr(supervisor, "sync_auth_to_db", None)
        if callable(sync):
            sync()
    except Exception:
        pass
    refreshed = False
    try:
        await supervisor.refresh_model_catalog()
        refreshed = True
    except Exception:
        pass
    try:
        from routes.model_routes import invalidate_model_catalogue_revision

        invalidate_model_catalogue_revision(owner)
    except Exception:
        pass
    return refreshed


async def _finish_native_flow(
    flow: _OAuthFlow,
    supervisor,
    *,
    code: str = "",
) -> bool:
    result = await _native(
        supervisor,
        "POST",
        f"/provider/{flow.provider_id}/oauth/callback",
        {
            "method": flow.method,
            "flowID": flow.flow_id,
            **({"code": code} if code else {}),
        },
        timeout=min(float(_FLOW_TTL_SECONDS), 300.0),
    )
    if result is not True:
        raise HTTPException(502, "Open Clank agent did not accept the authorization")
    return await _refresh(supervisor, flow.owner)


async def _finish_background_flow(flow: _OAuthFlow, supervisor) -> None:
    try:
        refreshed = await _finish_native_flow(flow, supervisor)
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        _oauth_flows.finish(flow.flow_id, connected=False, error=str(exc.detail))
    except Exception:
        _oauth_flows.finish(flow.flow_id, connected=False, error="Provider login failed")
    else:
        _oauth_flows.finish(
            flow.flow_id,
            connected=True,
            catalog_refreshed=refreshed,
        )


async def purge_owner_provider_flows(supervisor_pool, owner: str) -> None:
    """Cancel and drain all pending native-provider logins for one owner."""

    pending = _oauth_flows.cancel_owner(owner)
    if not pending:
        return
    tasks = [task for _, task in pending if task and not task.done()]
    cancel_error: BaseException | None = None
    try:
        supervisor = supervisor_pool
        if supervisor and hasattr(supervisor, "for_owner"):
            supervisor = await supervisor.for_owner(owner)
        supervisor = _validate_supervisor(supervisor)
        results = await asyncio.gather(
            *(
                _native(
                    supervisor,
                    "DELETE",
                    f"/provider/{flow.provider_id}/oauth/{flow.flow_id}",
                )
                for flow, _ in pending
            ),
            return_exceptions=True,
        )
        cancel_error = next(
            (result for result in results if isinstance(result, BaseException)),
            None,
        )
    except BaseException as exc:
        cancel_error = exc
    finally:
        # Every completion route attaches its request/background task before
        # entering the native callback. Drain those tasks even when the worker
        # cannot be reached, so account mutation never races a Python-side
        # refresh or credential mirror.
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    if cancel_error is not None:
        raise cancel_error


def setup_mimo_provider_routes() -> APIRouter:
    router = APIRouter(prefix="/api/mimo/providers", tags=["mimo-providers"])

    @router.get("")
    async def list_providers(request: Request):
        owner, _ = _browser_identity(request)
        supervisor = await _owner_supervisor(request, owner)
        providers, methods = await _catalog(supervisor)
        connected = set(providers.get("connected") or [])
        # Integration state from the catalog boundary: model counts and which
        # direct endpoint (if any) suppresses each connected provider.
        from routes.model_routes import (
            _canonical_mimo_provider_id,
            _mimo_provider_breakdown,
        )
        try:
            breakdown = {}
            for entry in _mimo_provider_breakdown(supervisor, owner):
                breakdown[entry["id"]] = entry
                breakdown[_canonical_mimo_provider_id(entry["id"])] = entry
        except Exception:
            breakdown = {}
        clean = []
        for item in providers.get("all") or []:
            if not isinstance(item, dict):
                continue
            provider_id = item.get("id")
            if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
                continue
            # ``mimo`` is the bundled runtime's private ID for Xiaomi's
            # included MiMo Auto model. ``ody-*`` rows are projections of
            # endpoint cards already managed by Open Clank. Neither is a
            # provider a user can connect.
            if provider_id == "mimo" or provider_id.startswith("ody-"):
                continue
            native_methods = _sanitize_methods(provider_id, methods.get(provider_id))
            is_connected = provider_id in connected
            # models.dev contains many catalogue-only records with no supported
            # sign-in method. The prior fallback turned all 173 into fake
            # providers. Keep a generic API-key action only for an existing
            # connection so it can still be replaced or disconnected.
            if not native_methods and not is_connected:
                continue
            if not native_methods:
                native_methods = [{
                    "index": 0,
                    "type": "api",
                    "label": "API key",
                    "capability": "api_key",
                }]
            state = (
                breakdown.get(provider_id)
                or breakdown.get(_canonical_mimo_provider_id(provider_id))
                or {}
            )
            capabilities = _capability_flags(native_methods)
            provider = {
                "id": provider_id,
                "name": _bounded(item.get("name") or provider_id),
                "connected": is_connected,
                "connection_id": (
                    f"mimo:{state.get('id') or _canonical_mimo_provider_id(provider_id)}"
                ),
                "methods": native_methods,
                "capabilities": capabilities,
                **({
                    "auth_note": "API key only — this provider does not expose browser login."
                } if not any((
                    capabilities["redirect"],
                    capabilities["device_code"],
                    capabilities["paste_code"],
                )) else {}),
                "family": state.get("family"),
                "chat_models": state.get("chat_models", 0),
                "active": state.get("active", provider_id in connected),
                "served_by": state.get("served_by"),
            }
            model_ids = state.get("model_ids")
            if isinstance(model_ids, list) and model_ids:
                provider["models"] = model_ids
            if provider_id == "xiaomi" and "xiaomi/mimo-auto" in (model_ids or []):
                provider["included_free_models"] = 1
            clean.append(provider)
        clean.sort(key=lambda value: (not value["connected"], value["name"].casefold()))
        return {
            "available": True,
            "endpoint": "mimo",
            "storage": "Open Clank account-isolated agent runtime",
            "providers": clean,
        }

    @router.post("/{provider_id}/oauth/authorize")
    async def oauth_authorize(provider_id: str, payload: OAuthStart, request: Request):
        owner, browser_session = _browser_identity(request)
        supervisor = await _owner_supervisor(request, owner)
        provider_id = _provider_id(provider_id)
        providers, methods = await _catalog(supervisor)
        _, provider_methods = _known_provider(provider_id, providers, methods)
        _check_method(payload.method, provider_methods, "oauth")
        native_method = provider_methods[payload.method]
        capability = _method_capability(provider_id, payload.method, native_method)
        inputs = dict(payload.inputs or {})
        if inputs and (len(inputs) > 100 or any(len(k) > 128 or len(v) > 16_384 for k, v in inputs.items())):
            raise HTTPException(400, "OAuth prompt input is too large")
        flow_id = _new_flow_id()
        state = _flow_state(flow_id, provider_id, owner, browser_session)
        native_body: dict[str, Any] = {
            "method": payload.method,
            "inputs": inputs,
            "flowID": flow_id,
            "state": state,
        }
        if capability == "redirect":
            native_body["redirectURI"] = (
                f"{_public_origin(request)}/api/mimo/providers/oauth/callback"
            )
        try:
            result = await _native(
                supervisor,
                "POST",
                f"/provider/{provider_id}/oauth/authorize",
                native_body,
                timeout=60.0,
            )
        except Exception:
            _oauth_flows.discard(flow_id)
            raise
        if not isinstance(result, dict) or result.get("method") not in {"auto", "code"}:
            raise HTTPException(502, "Open Clank agent returned an invalid authorization response")
        expected_method = "auto" if capability == "device_code" else "code"
        if result["method"] != expected_method:
            raise HTTPException(
                502,
                f"Provider does not support remote {capability.replace('_', ' ')} login",
            )
        flow = _OAuthFlow(
            flow_id=flow_id,
            provider_id=provider_id,
            method=payload.method,
            capability=capability,
            owner=owner,
            browser_session=browser_session,
            state=state,
            expires_at=_oauth_flows.now() + _FLOW_TTL_SECONDS,
        )
        _oauth_flows.add(flow)
        if capability == "device_code":
            _oauth_flows.start_background(flow_id)
            task = asyncio.create_task(_finish_background_flow(flow, supervisor))
            _oauth_flows.attach_task(flow_id, task)
        return {
            "flow_id": flow_id,
            "capability": capability,
            "url": _safe_oauth_url(result.get("url")),
            "instructions": _bounded(result.get("instructions"), 4_096),
            "expires_in": _FLOW_TTL_SECONDS,
        }

    @router.get("/oauth/status")
    async def oauth_status(flow_id: str, request: Request):
        owner, browser_session = _browser_identity(request)
        flow = _oauth_flows.require(
            flow_id,
            owner=owner,
            browser_session=browser_session,
        )
        result = {"status": flow.status}
        if flow.error:
            result["error"] = flow.error
        if flow.status == "connected":
            result["catalog_refreshed"] = flow.catalog_refreshed
        return result

    @router.post("/oauth/cancel")
    async def oauth_cancel(payload: OAuthFlowAction, request: Request):
        owner, browser_session = _browser_identity(request)
        flow, task = _oauth_flows.cancel(
            payload.flow_id,
            owner=owner,
            browser_session=browser_session,
        )
        supervisor = await _owner_supervisor(request, owner)
        try:
            await _native(
                supervisor,
                "DELETE",
                f"/provider/{flow.provider_id}/oauth/{flow.flow_id}",
            )
        except HTTPException:
            pass
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return {"status": "cancelled"}

    @router.get("/oauth/callback")
    async def oauth_redirect_callback(
        request: Request,
        state: str = "",
        code: str | None = None,
        error: str | None = None,
    ):
        owner, browser_session = _browser_identity(request)
        flow_id = _state_flow_id(state)
        flow = _oauth_flows.require(
            flow_id,
            owner=owner,
            browser_session=browser_session,
        )
        if not _valid_flow_state(flow, state):
            raise HTTPException(400, "Invalid provider login state")
        flow = _oauth_flows.consume(
            flow_id,
            owner=owner,
            browser_session=browser_session,
            provider_id=flow.provider_id,
            capability="redirect",
        )
        if error:
            _oauth_flows.finish(flow_id, connected=False, error="Provider denied login")
            return _flow_page(False, "The provider denied this login.", status_code=400)
        if not code:
            _oauth_flows.finish(flow_id, connected=False, error="Missing authorization code")
            return _flow_page(False, "Missing authorization code", status_code=400)
        task = asyncio.current_task()
        if task is not None:
            _oauth_flows.attach_task(flow_id, task)
        supervisor = await _owner_supervisor(request, owner)
        try:
            refreshed = await _finish_native_flow(flow, supervisor, code=code)
        except HTTPException as exc:
            _oauth_flows.finish(flow_id, connected=False, error=str(exc.detail))
            return _flow_page(False, str(exc.detail), status_code=exc.status_code)
        _oauth_flows.finish(
            flow_id,
            connected=True,
            catalog_refreshed=refreshed,
        )
        return _flow_page(True, "Your provider is connected.")

    @router.post("/{provider_id}/oauth/callback")
    async def oauth_callback(provider_id: str, payload: OAuthCallback, request: Request):
        owner, browser_session = _browser_identity(request)
        provider_id = _provider_id(provider_id)
        flow = _oauth_flows.consume(
            payload.flow_id,
            owner=owner,
            browser_session=browser_session,
            provider_id=provider_id,
            capability="paste_code",
        )
        task = asyncio.current_task()
        if task is not None:
            _oauth_flows.attach_task(payload.flow_id, task)
        supervisor = await _owner_supervisor(request, owner)
        try:
            refreshed = await _finish_native_flow(flow, supervisor, code=payload.code)
        except Exception as exc:
            detail = str(exc.detail) if isinstance(exc, HTTPException) else "Provider login failed"
            _oauth_flows.finish(payload.flow_id, connected=False, error=detail)
            raise
        _oauth_flows.finish(
            payload.flow_id,
            connected=True,
            catalog_refreshed=refreshed,
        )
        return {"connected": True, "catalog_refreshed": refreshed}

    @router.put("/{provider_id}/api-key")
    async def set_api_key(provider_id: str, payload: ApiKeyCredential, request: Request):
        owner, _ = _browser_identity(request)
        supervisor = await _owner_supervisor(request, owner)
        provider_id = _provider_id(provider_id)
        providers, methods = await _catalog(supervisor)
        _known_provider(provider_id, providers, methods)
        result = await _native(
            supervisor,
            "PUT",
            f"/auth/{provider_id}",
            {"type": "api", "key": payload.key},
        )
        if result is not True:
            raise HTTPException(502, "Open Clank agent did not save the credential")
        return {"connected": True, "catalog_refreshed": await _refresh(supervisor, owner)}

    @router.delete("/{provider_id}")
    async def disconnect(provider_id: str, request: Request):
        owner, _ = _browser_identity(request)
        supervisor = await _owner_supervisor(request, owner)
        provider_id = _provider_id(provider_id)
        providers, methods = await _catalog(supervisor)
        _known_provider(provider_id, providers, methods)
        result = await _native(supervisor, "DELETE", f"/auth/{provider_id}")
        if result is not True:
            raise HTTPException(502, "Open Clank agent did not remove the credential")
        revoked = 0
        revoked_workers: list[tuple[str, str]] = []
        try:
            from core.database import (
                ModelShare,
                ModelShareSubscription,
                SessionLocal,
            )

            db = SessionLocal()
            try:
                share_ids = [
                    row.id
                    for row in db.query(ModelShare).filter(
                        ModelShare.owner == owner,
                        ModelShare.source_kind == "native",
                        ModelShare.source_id == f"mimo:{provider_id}",
                    ).all()
                ]
                if share_ids:
                    revoked_workers = [
                        (grant.subscriber, grant.share_id)
                        for grant in db.query(ModelShareSubscription).filter(
                            ModelShareSubscription.share_id.in_(share_ids),
                            ModelShareSubscription.enabled == True,  # noqa: E712
                        ).all()
                    ]
                    db.query(ModelShareSubscription).filter(
                        ModelShareSubscription.share_id.in_(share_ids)
                    ).delete(synchronize_session=False)
                    revoked = db.query(ModelShare).filter(
                        ModelShare.id.in_(share_ids)
                    ).delete(synchronize_session=False)
                    db.commit()
            finally:
                db.close()
        except Exception:
            revoked = 0
        refreshed = await _refresh(supervisor, owner)
        pool = getattr(request.app.state, "mimo_supervisor", None)
        revoke = getattr(pool, "revoke_shared_access", None)
        if callable(revoke) and revoked_workers:
            await asyncio.gather(
                *(
                    revoke(recipient, share_id)
                    for recipient, share_id in revoked_workers
                ),
                return_exceptions=True,
            )
        if revoked:
            try:
                from routes.model_routes import invalidate_model_catalogue_revision

                invalidate_model_catalogue_revision()
            except Exception:
                pass
        reproject = getattr(pool, "refresh_endpoint_projection", None)
        if callable(reproject):
            asyncio.create_task(reproject())
        return {
            "connected": False,
            "catalog_refreshed": refreshed,
            "revoked_model_shares": revoked,
        }

    return router

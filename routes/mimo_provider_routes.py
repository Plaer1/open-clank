"""Narrow Odysseus adapter for the HTTP server already owned by `mimo acp`."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from core.middleware import require_admin
from src.auth_helpers import effective_user


_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OAuthStart(_StrictModel):
    method: int = Field(ge=0, le=50)
    inputs: dict[str, str] | None = None


class OAuthCallback(_StrictModel):
    method: int = Field(ge=0, le=50)
    code: str | None = Field(default=None, max_length=16_384)


class ApiKeyCredential(_StrictModel):
    key: str = Field(min_length=1, max_length=131_072)


def _validate_supervisor(supervisor):
    if not supervisor or not supervisor.is_alive():
        raise HTTPException(503, "MiMo is not running")
    base_url = supervisor.http_base_url
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        raise HTTPException(503, "MiMo provider service is not loopback-only")
    return supervisor


def _supervisor(request: Request):
    supervisor = getattr(request.app.state, "mimo_supervisor", None)
    return _validate_supervisor(supervisor)


async def _owner_supervisor(request: Request):
    supervisor = getattr(request.app.state, "mimo_supervisor", None)
    if supervisor and hasattr(supervisor, "for_owner"):
        supervisor = await supervisor.for_owner(effective_user(request))
    return _validate_supervisor(supervisor)


def _provider_id(value: str) -> str:
    if not _PROVIDER_ID.fullmatch(value):
        raise HTTPException(400, "Invalid provider ID")
    return value


def _bounded(value: Any, limit: int = 512) -> str:
    return str(value or "")[:limit]


def _sanitize_methods(value: Any) -> list[dict[str, Any]]:
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


async def _native(supervisor, method: str, path: str, body: dict | None = None, *, timeout: float = 20.0):
    try:
        async with httpx.AsyncClient(
            base_url=supervisor.http_base_url,
            follow_redirects=False,
            timeout=timeout,
            trust_env=False,
        ) as client:
            response = await client.request(method, path, json=body)
        response.raise_for_status()
        return response.json()
    except asyncio.CancelledError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "MiMo provider operation failed") from exc


async def _catalog(supervisor) -> tuple[dict[str, Any], dict[str, Any]]:
    providers, methods = await asyncio.gather(
        _native(supervisor, "GET", "/provider"),
        _native(supervisor, "GET", "/provider/auth"),
    )
    if not isinstance(providers, dict) or not isinstance(methods, dict):
        raise HTTPException(502, "MiMo returned an invalid provider catalog")
    return providers, methods


def _known_provider(provider_id: str, providers: dict[str, Any], methods: dict[str, Any]) -> tuple[dict, list]:
    all_providers = providers.get("all") if isinstance(providers.get("all"), list) else []
    provider = next(
        (item for item in all_providers if isinstance(item, dict) and item.get("id") == provider_id),
        None,
    )
    if provider is None and provider_id not in methods:
        raise HTTPException(404, "Unknown MiMo provider")
    return provider or {"id": provider_id, "name": provider_id}, methods.get(provider_id) or []


def _check_method(index: int, provider_methods: list, expected: str) -> None:
    if index >= len(provider_methods) or not isinstance(provider_methods[index], dict):
        raise HTTPException(400, "Invalid authentication method")
    if provider_methods[index].get("type") != expected:
        raise HTTPException(400, f"Selected method is not {expected}")


async def _refresh(supervisor) -> bool:
    try:
        await supervisor.refresh_model_catalog()
        return True
    except Exception:
        return False


def setup_mimo_provider_routes() -> APIRouter:
    router = APIRouter(prefix="/api/mimo/providers", tags=["mimo-providers"])

    @router.get("")
    async def list_providers(request: Request):
        require_admin(request)
        supervisor = await _owner_supervisor(request)
        providers, methods = await _catalog(supervisor)
        connected = set(providers.get("connected") or [])
        # Integration state from the catalog boundary: model counts and which
        # direct endpoint (if any) suppresses each connected provider.
        from routes.model_routes import _mimo_provider_breakdown
        from src.auth_helpers import effective_user
        try:
            breakdown = {
                entry["id"]: entry
                for entry in _mimo_provider_breakdown(supervisor, effective_user(request))
            }
        except Exception:
            breakdown = {}
        clean = []
        for item in providers.get("all") or []:
            if not isinstance(item, dict):
                continue
            provider_id = item.get("id")
            if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
                continue
            native_methods = _sanitize_methods(methods.get(provider_id))
            if not native_methods:
                native_methods = [{"index": 0, "type": "api", "label": "API key"}]
            state = breakdown.get(provider_id) or {}
            clean.append({
                "id": provider_id,
                "name": _bounded(item.get("name") or provider_id),
                "connected": provider_id in connected,
                "methods": native_methods,
                "family": state.get("family"),
                "chat_models": state.get("chat_models", 0),
                "active": state.get("active", provider_id in connected),
                "served_by": state.get("served_by"),
            })
        clean.sort(key=lambda value: (not value["connected"], value["name"].casefold()))
        return {
            "available": True,
            "endpoint": "mimo",
            "storage": "MiMo's isolated MIMOCODE_HOME",
            "providers": clean,
        }

    @router.post("/{provider_id}/oauth/authorize")
    async def oauth_authorize(provider_id: str, payload: OAuthStart, request: Request):
        require_admin(request)
        supervisor = await _owner_supervisor(request)
        provider_id = _provider_id(provider_id)
        providers, methods = await _catalog(supervisor)
        _, provider_methods = _known_provider(provider_id, providers, methods)
        _check_method(payload.method, provider_methods, "oauth")
        inputs = payload.inputs or None
        if inputs and (len(inputs) > 100 or any(len(k) > 128 or len(v) > 16_384 for k, v in inputs.items())):
            raise HTTPException(400, "OAuth prompt input is too large")
        result = await _native(
            supervisor,
            "POST",
            f"/provider/{provider_id}/oauth/authorize",
            {"method": payload.method, **({"inputs": inputs} if inputs else {})},
            timeout=60.0,
        )
        if not isinstance(result, dict) or result.get("method") not in {"auto", "code"}:
            raise HTTPException(502, "MiMo returned an invalid authorization response")
        return {
            "url": _bounded(result.get("url"), 8_192),
            "method": result["method"],
            "instructions": _bounded(result.get("instructions"), 4_096),
        }

    @router.post("/{provider_id}/oauth/callback")
    async def oauth_callback(provider_id: str, payload: OAuthCallback, request: Request):
        require_admin(request)
        supervisor = await _owner_supervisor(request)
        provider_id = _provider_id(provider_id)
        providers, methods = await _catalog(supervisor)
        _, provider_methods = _known_provider(provider_id, providers, methods)
        _check_method(payload.method, provider_methods, "oauth")
        body = {"method": payload.method}
        if payload.code is not None:
            body["code"] = payload.code
        result = await _native(
            supervisor,
            "POST",
            f"/provider/{provider_id}/oauth/callback",
            body,
            timeout=300.0,
        )
        if result is not True:
            raise HTTPException(502, "MiMo did not accept the authorization")
        return {"connected": True, "catalog_refreshed": await _refresh(supervisor)}

    @router.put("/{provider_id}/api-key")
    async def set_api_key(provider_id: str, payload: ApiKeyCredential, request: Request):
        require_admin(request)
        supervisor = await _owner_supervisor(request)
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
            raise HTTPException(502, "MiMo did not save the credential")
        return {"connected": True, "catalog_refreshed": await _refresh(supervisor)}

    @router.delete("/{provider_id}")
    async def disconnect(provider_id: str, request: Request):
        require_admin(request)
        supervisor = await _owner_supervisor(request)
        provider_id = _provider_id(provider_id)
        providers, methods = await _catalog(supervisor)
        _known_provider(provider_id, providers, methods)
        result = await _native(supervisor, "DELETE", f"/auth/{provider_id}")
        if result is not True:
            raise HTTPException(502, "MiMo did not remove the credential")
        return {"connected": False, "catalog_refreshed": await _refresh(supervisor)}

    return router

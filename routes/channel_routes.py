"""Owner-only settings API for chat channels. Secrets are write-only: no response ever contains one."""
from __future__ import annotations
from typing import Callable, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.openclank.channels.store import REQUIRED

SECRET_KEYS = {"telegram": {"token"}, "matrix": {"access_token"}, "discord": {"token"}, "feishu": {"app_secret"}, "wecom": {"bot_secret"}}
_ROUTE = {"endpoint_id", "model"}      # the provider route chat sessions are created with
SETTING_KEYS = {"telegram": {"attachments_in"} | _ROUTE, "matrix": {"homeserver", "attachments_in"} | _ROUTE, "discord": {"attachments_in"} | _ROUTE,
                "feishu": {"app_id", "domain", "attachments_in"} | _ROUTE, "wecom": {"bot_id", "attachments_in"} | _ROUTE}
CHANNELS = tuple(REQUIRED)


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    secrets: dict[str, str] = Field(default_factory=dict, max_length=4)
    settings: dict[str, str] = Field(default_factory=dict, max_length=4)


class PairingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "dismiss"]
    code: str = Field(min_length=4, max_length=16)


class AllowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["sender", "group"]
    id: str = Field(min_length=1, max_length=256)
    allowed: bool


class ApproveInChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender: Optional[str] = Field(default=None, max_length=256)


class GlobalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def setup_channel_routes(store, runner, *, require_user: Callable | None = None, load_settings: Callable | None = None,
                         save_settings: Callable | None = None, provision: Callable | None = None, invalidate: Callable | None = None) -> APIRouter:
    if require_user is None:
        from src.auth_helpers import require_user as _require_user
        require_user = _require_user
    if load_settings is None or save_settings is None:
        from src.settings import load_settings as _load, save_settings as _save
        load_settings, save_settings = load_settings or _load, save_settings or _save
    if provision is None:
        from src.openclank.channels.channel_token import provision as _provision
        provision = _provision
    router = APIRouter(prefix="/api/channels", tags=["channels"])

    def owner(request: Request) -> str:
        user = require_user(request)               # browser session only: API tokens are refused here
        try:
            store.set_owner(user)
        except PermissionError:
            raise HTTPException(403, "Channels belong to another account") from None
        return user

    def known(name: str) -> str:
        if name not in CHANNELS:
            raise HTTPException(404, "Unknown channel")
        return name

    def view() -> dict:
        return {"global_enabled": bool(load_settings().get("channels_enabled")), "running": runner.running(),
                "channels": {n: store.public_status(n) for n in CHANNELS}}

    @router.get("")
    async def get_channels(request: Request):
        owner(request)
        return view()

    @router.post("/global")
    async def set_global(request: Request, body: GlobalBody):
        user = owner(request)
        settings = load_settings(); settings["channels_enabled"] = body.enabled; save_settings(settings)
        if body.enabled and not store.token():
            store.set_token(provision(user, invalidate))
        await runner.reload()
        return view()

    @router.post("/{name}/config")
    async def configure(request: Request, name: str, body: ConfigBody):
        user = owner(request); known(name)
        if set(body.secrets) - SECRET_KEYS[name] or set(body.settings) - SETTING_KEYS[name]:
            raise HTTPException(422, "Unknown field")
        if body.settings.get("domain", "feishu") not in ("feishu", "lark"):
            raise HTTPException(422, "domain must be feishu or lark")
        for key, value in body.secrets.items():
            store.set_secret(name, key, value.strip())
        for key, value in body.settings.items():
            store.set_setting(name, key, value.strip())
        if body.enabled is not None:
            store.set_enabled(name, body.enabled)
            if body.enabled and not store.token():
                store.set_token(provision(user, invalidate))
        await runner.reload()
        return view()

    @router.post("/{name}/pairing")
    async def pairing(request: Request, name: str, body: PairingBody):
        owner(request); known(name)
        sender = store.resolve_pending(name, body.code, body.action == "approve")
        if sender is None:
            raise HTTPException(404, "No such pending request")
        return view()

    @router.post("/{name}/allow")
    async def allow(request: Request, name: str, body: AllowBody):
        owner(request); known(name)
        store.allow(name, body.kind, body.id, body.allowed)
        return view()

    @router.post("/{name}/approve-in-chat")
    async def approve_in_chat(request: Request, name: str, body: ApproveInChatBody):
        owner(request); known(name)
        if body.sender is not None and body.sender not in store.senders(name):
            raise HTTPException(422, "Only a paired identity can be allowed to approve")
        store.set_approve_in_chat(name, body.sender)
        return view()

    return router

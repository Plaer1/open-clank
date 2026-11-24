"""The `occ_` channel token through the REAL authentication middleware (install_request_auth), real AuthManager, real database
rows and the real channel/token routes. Stub handlers stand in only for the other route families: the middleware answers
before routing, so a 403 for the token against a path a cookie user reaches (200) proves the confinement."""
import asyncio
import logging
import secrets as _secrets
import uuid

import bcrypt
import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from core.auth import AuthManager
from core.database import ApiToken, Base, SessionLocal, engine
from routes.api_token_routes import setup_api_token_routes
from routes.auth_routes import SESSION_COOKIE, setup_auth_routes
from routes.channel_routes import setup_channel_routes
from src.auth_helpers import effective_user
from src.openclank.account_request_barrier import AccountRequestBarrier, AccountRequestFenced
from src.openclank.channels.base import Limits
from src.openclank.channels.channel_token import path_allowed, revoke
from src.openclank.channels.runner import ChannelRunner
from src.openclank.channels.store import ChannelStore
from src.openclank.request_auth import install_request_auth
from src.settings import load_settings, save_settings

# Exactly what the channel bridge needs. Nothing else may answer a channel token.
ALLOWED = [("POST", "/api/chat_stream"), ("POST", "/api/session"), ("POST", "/api/session/abc123/permission"),
           ("POST", "/api/upload"), ("GET", "/api/generated-image/img-1.png")]
# One or more representative paths per route family; each answers 200 to a cookie user (the stubs) and must be 403 for a channel token.
FAMILIES = {
    "settings": [("GET", "/api/settings"), ("POST", "/api/settings")],
    "files": [("GET", "/api/files-v1/roots"), ("POST", "/api/files-v1/resources/children")],
    "host files": [("GET", "/api/files/library"), ("POST", "/api/files/chat-snapshots")],
    "memory": [("GET", "/api/memory"), ("POST", "/api/memory/add"), ("POST", "/api/memory/nuke")],
    "history": [("GET", "/api/history/some-session"), ("POST", "/api/history/restore")],
    "providers and credentials": [("GET", "/api/v1/providers/connections"), ("POST", "/api/v1/providers/credentials")],
    "admin and users": [("GET", "/api/auth/users"), ("POST", "/api/auth/users"), ("GET", "/api/admin/system")],
    "api-token management": [("GET", "/api/tokens"), ("POST", "/api/tokens"), ("DELETE", "/api/tokens/abc")],
    "published files and shares": [("POST", "/api/files/share"), ("POST", "/api/files/abc/grants"), ("POST", "/api/files/abc/revoke")],
    "diagnostics and logs": [("GET", "/api/logging/v1/sessions"), ("POST", "/api/logging/v1/export"), ("GET", "/api/admin/logs")],
    "orchestration and worker control": [("POST", "/api/orchestration/spawn"), ("GET", "/api/mimo/status"), ("POST", "/api/mimo/permission-grants/reset")],
    "engine and acp internals": [("POST", "/api/tui/v1/turns"), ("POST", "/api/session/abc/mimo-operation"), ("POST", "/api/internal/anything-else")],
    "channels settings": [("GET", "/api/channels"), ("POST", "/api/channels/global")],
    "session management beyond create": [("DELETE", "/api/session/abc"), ("GET", "/api/session/abc"), ("POST", "/api/sessions/bulk-delete")],
    "wrong method on an allowed path": [("GET", "/api/chat_stream"), ("DELETE", "/api/session"), ("POST", "/api/generated-image/img-1.png"), ("GET", "/api/upload/some-file")],
    "static non-bootstrap data": [("GET", "/static/private/notes.txt"), ("GET", "/")],
}


class FakeAdapter:
    name = "telegram"; limits = Limits(max_text=100, max_file_bytes=100)
    async def run(self, on_inbound): await asyncio.Event().wait()
    async def send_text(self, *a): pass
    async def send_file(self, *a): pass
    async def aclose(self): pass


def run(coro): return asyncio.run(coro)


def _insert_token(owner, raw, kind):
    with SessionLocal() as db:
        db.add(ApiToken(id=uuid.uuid4().hex, owner=owner, name=kind, token_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode(),
                        token_prefix=raw[:8], scopes="chat", client_kind=kind))
        db.commit()


async def world(tmp_path, extra=None):
    Base.metadata.create_all(engine)
    auth = AuthManager(str(tmp_path / "auth.json"))
    barrier = AccountRequestBarrier(auth.is_account_lifecycle_fenced)
    app = FastAPI(); app.state.auth_manager = auth; app.state.drain_account_owner_requests = barrier.drain
    async def account_call_next(request, next_call, owner):
        try: return await barrier.track_response(owner, lambda: next_call(request))
        except AccountRequestFenced: return JSONResponse(status_code=409, content={"error": "fenced"})
    install_request_auth(app, auth_manager=auth, session_factory=SessionLocal, api_token_model=ApiToken, account_call_next=account_call_next,
                         trusted_loopback=lambda r: False, desktop_ready_path="/ready", session_cookie=SESSION_COOKIE)
    store = ChannelStore(str(tmp_path / "channels.json"))
    invalidate = lambda: app.state.invalidate_token_cache()
    def idle():
        if store.token():
            if store.owner(): revoke(store.owner(), invalidate)
            store.set_token(None)
    runner = ChannelRunner(store, global_enabled=lambda: bool(load_settings().get("channels_enabled")),
                           adapter_factories={"telegram": lambda st: FakeAdapter()}, bridge_factory=lambda: object(), on_idle=idle)
    app.include_router(setup_auth_routes(auth)); app.include_router(setup_api_token_routes())
    app.include_router(setup_channel_routes(store, runner, invalidate=invalidate))
    if extra is not None: extra(app)                      # real routers go in BEFORE the catch-all stub
    async def reached(request: Request, full_path: str = ""):
        return {"reached": True, "user": effective_user(request), "owner_header_seen": request.headers.get("x-open-clank-owner")}
    app.add_api_route("/{full_path:path}", reached, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    transport = httpx.ASGITransport(app=app, client=("203.0.113.19", 54321))
    return app, auth, store, runner, transport


async def login_two(transport, auth):
    alice = httpx.AsyncClient(transport=transport, base_url="https://channels.test")
    pw = {n: _secrets.token_urlsafe(24) for n in ("alice", "bob")}
    assert (await alice.post("/api/auth/setup", json={"username": "alice", "password": pw["alice"]})).status_code == 200
    assert (await alice.post("/api/auth/login", json={"username": "alice", "password": pw["alice"]})).status_code == 200
    assert (await alice.post("/api/auth/users", json={"username": "bob", "password": pw["bob"]})).status_code == 200
    return alice


def bearer(transport, token):
    return httpx.AsyncClient(transport=transport, base_url="https://channels.test", headers={"Authorization": f"Bearer {token}"})


def test_path_table_is_exactly_the_bridge_routes():
    for method, path in ALLOWED: assert path_allowed(path, method), (method, path)
    for family, rows in FAMILIES.items():
        for method, path in rows: assert not path_allowed(path, method), (family, method, path)


def test_channel_token_through_the_real_middleware(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    original = dict(load_settings())
    async def scenario():
        app, auth, store, runner, transport = await world(tmp_path)
        try:
            alice = await login_two(transport, auth)
            # switching channels on through the real routes provisions the owner's channel token
            assert (await alice.post("/api/channels/global", json={"enabled": True})).status_code == 200
            cfg = await alice.post("/api/channels/telegram/config", json={"enabled": True, "secrets": {"token": "TELEGRAM-SECRET-XYZ"}})
            assert cfg.status_code == 200 and "TELEGRAM-SECRET-XYZ" not in cfg.text
            token = store.token(); assert token and token.startswith("occ_") and runner.running() == ["telegram"]
            ch = bearer(transport, token)

            # 1) allowed: exactly the bridge routes
            for method, path in ALLOWED:
                r = await ch.request(method, path)
                assert r.status_code == 200 and r.json()["user"] == "alice", (method, path, r.status_code)
            # 2) every other family: 403 for the token while the same path answers 200 for the cookie user
            for family, rows in FAMILIES.items():
                for method, path in rows:
                    control = await alice.request(method, path)
                    denied = await ch.request(method, path)
                    assert denied.status_code == 403, (family, method, path, denied.status_code)
                    assert control.status_code < 500, (family, path)       # the cookie user is answered by a stub or a real router, never blocked as a channel
            # 3) bound to the owner who provisioned it: identity cannot be switched by header, query or body
            r = await ch.post("/api/chat_stream?user=bob&owner=bob", headers={"X-Open-Clank-Owner": "bob", "X-Open-Clank-Internal": "x"}, json={"user": "bob"})
            assert r.json()["user"] == "alice"
            # 4) never returned after creation
            listing = await alice.get("/api/tokens")
            assert token not in listing.text and token not in (await alice.get("/api/channels")).text
            assert any(t["client_kind"] == "channel" for t in listing.json()) and token[:8] in listing.text   # only the display prefix
            # 5) refused while chat channels are off globally (flag flipped directly; the token itself is still valid)
            flags = load_settings(); flags["channels_enabled"] = False; save_settings(flags)
            assert (await ch.post("/api/chat_stream")).status_code == 403
            flags["channels_enabled"] = True; save_settings(flags)
            assert (await ch.post("/api/chat_stream")).status_code == 200
            # 6) disabling the channel revokes it: the next request fails
            assert (await alice.post("/api/channels/telegram/config", json={"enabled": False})).status_code == 200
            assert store.token() is None and (await ch.post("/api/chat_stream")).status_code == 401
            # 7) removing the secret also revokes (fresh token first, then the secret is cleared)
            await alice.post("/api/channels/telegram/config", json={"enabled": True, "secrets": {"token": "TELEGRAM-SECRET-XYZ"}})
            second = store.token(); assert second and second != token
            ch2 = bearer(transport, second); assert (await ch2.post("/api/chat_stream")).status_code == 200
            assert (await bearer(transport, token).post("/api/chat_stream")).status_code == 401           # the old one stays dead
            await alice.post("/api/channels/telegram/config", json={"secrets": {"token": ""}})
            assert (await ch2.post("/api/chat_stream")).status_code == 401
            # 8) ody_ (api) and oct_ (tui) behave as before: not confined by the channel rule
            ody, oct_ = "ody_" + _secrets.token_urlsafe(24), "oct_" + _secrets.token_urlsafe(24)
            _insert_token("alice", ody, "api"); _insert_token("alice", oct_, "tui"); app.state.invalidate_token_cache()
            assert (await bearer(transport, ody).get("/api/memory")).status_code == 200          # api tokens: unchanged, not channel-confined
            assert (await bearer(transport, oct_).get("/api/memory")).status_code == 403            # tui is confined to its own API
            assert (await bearer(transport, oct_).get("/api/tui/v1/info")).status_code in (200, 404)
            # a channel-looking secret in the wrong kind never authenticates
            fake = "occ_" + _secrets.token_urlsafe(24); _insert_token("alice", fake, "api"); app.state.invalidate_token_cache()
            assert (await bearer(transport, fake).post("/api/chat_stream")).status_code == 401
            return second
        finally:
            save_settings(original)
    second = run(scenario())
    assert "TELEGRAM-SECRET-XYZ" not in caplog.text and second not in caplog.text                      # no secret or token in any log line

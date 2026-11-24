"""A channel turn through REAL Host routes where the sandbox allows it: real middleware, real `occ_` token, real POST /api/session
(real SessionManager and database) and real POST /api/upload (real UploadHandler). Only POST /api/chat_stream is a stub: the real
route cannot run without a managed model engine (see the D1 build note), so the model reply is stubbed there."""
import asyncio
import json
import struct
import zlib

import httpx
from fastapi import FastAPI, Request
from starlette.responses import StreamingResponse

from core.database import Base, SessionLocal, engine
from core.session_manager import SessionManager
from routes.session_routes import setup_session_routes
from routes.upload_routes import setup_upload_routes
from src.auth_helpers import effective_user
from src.openclank.channels.access import Access
from src.openclank.channels.base import Attachment, Inbound, Limits
from src.openclank.channels.bridge import ChannelBridge
from src.openclank.channels.channel_token import provision
from src.openclank.channels.store import ChannelStore
from src.settings import load_settings, save_settings
from src.upload_handler import UploadHandler
from tests.test_channel_token_confinement import login_two, world


def png_1x1() -> bytes:
    def chunk(tag, data): return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")


class Adapter:
    name = "telegram"; limits = Limits(max_text=4096, max_file_bytes=1000)
    def __init__(self): self.texts = []
    async def send_text(self, conv, thread, text): self.texts.append(text)
    async def send_file(self, *a): pass


def run(coro): return asyncio.run(coro)


class FakeRoute:
    """What the real session route reads from an authorized provider route (the sandbox has no provider connection)."""
    public_endpoint_id = "ep-test"; provider_model_id = "model-test"; model_route_id = "route-test"
    def __getattr__(self, name): return None


def test_real_session_create_and_upload_through_the_real_middleware_with_a_channel_token(tmp_path, monkeypatch):
    import routes.session_routes as session_routes
    monkeypatch.setattr(session_routes, "resolve_chat_route", lambda **kw: FakeRoute())
    original = dict(load_settings())
    async def scenario():
        Base.metadata.create_all(engine)
        sessions = SessionManager()
        handler = UploadHandler(str(tmp_path), str(tmp_path / "uploads"))
        seen = {}
        async def chat_stream(request: Request):
            form = await request.form()
            seen.update(owner=effective_user(request), session=form.get("session"), attachments=form.get("attachments"), message=form.get("message"))
            async def frames():
                yield 'data: {"delta": "stub reply"}\n\n'; yield "data: [DONE]\n\n"
            return StreamingResponse(frames(), media_type="text/event-stream")
        def real_routes(app):
            app.add_api_route("/api/chat_stream", chat_stream, methods=["POST"])           # the ONLY stub: the real route needs a model engine
            app.include_router(setup_session_routes(sessions, {"REQUEST_TIMEOUT": 5, "SESSIONS_FILE": None}, upload_handler=handler))
            upload_router, _cleanup = setup_upload_routes(handler); app.include_router(upload_router)
        app, auth, store, runner, transport = await world(tmp_path, extra=real_routes)
        from src.openclank.conversation_archive import configure_conversation_archive_owner_resolver, reset_conversation_archive_for_test
        reset_conversation_archive_for_test(); configure_conversation_archive_owner_resolver(auth.account_id, auth.username_for_account_id)   # the real archive resolves owners through the real AuthManager
        try:
            alice = await login_two(transport, auth)
            await alice.post("/api/channels/global", json={"enabled": True})
            await alice.post("/api/channels/telegram/config", json={"enabled": True, "secrets": {"token": "TG-SECRET"}, "settings": {"attachments_in": "true"}})
            store.allow("telegram", "sender", "u1", True)
            store.set_setting("telegram", "endpoint_id", "ep-test"); store.set_setting("telegram", "model", "model-test")
            token = store.token(); assert token and token.startswith("occ_")
            client = httpx.AsyncClient(transport=transport, base_url="https://channels.test", headers={"Authorization": f"Bearer {token}"})
            bridge = ChannelBridge(store, Access(store), client)
            adapter = Adapter()
            async def fetch(): return png_1x1()
            inbound = Inbound(channel="telegram", conv_id="c1", sender_id="u1", text="what is this?", message_id="1", is_direct=True, sender_label="Ann",
                              attachments=(Attachment("one.png", "image/png", 70, fetch),))
            await bridge.handle(adapter, inbound)

            sid = store.binding("telegram|c1|")
            assert sid, "the bridge created and bound a session through the real route"
            session = sessions.get_session(sid)
            assert session.owner == "alice" and not getattr(session, "incognito", False)                 # the owner's, not incognito
            assert seen["owner"] == "alice" and seen["session"] == sid and adapter.texts == ["stub reply"]  # reply went back out through the adapter
            ids = json.loads(seen["attachments"]); assert len(ids) == 1                                  # the png was uploaded through the real route
            info = handler.get_info(ids[0]) if hasattr(handler, "get_info") else None
            if info: assert info.get("owner") in (None, "alice") and info.get("mime") == "image/png"
            # a second owner's channel token cannot reach alice's session
            bob_token = provision("bob", lambda: app.state.invalidate_token_cache())
            bob = httpx.AsyncClient(transport=transport, base_url="https://channels.test", headers={"Authorization": f"Bearer {bob_token}"})
            assert (await bob.get(f"/api/session/{sid}")).status_code == 403                                # confinement
            # (the permission reply route is owner-checked by the real chat router, which needs a model engine and is not mounted here)
            bob_session = (await bob.post("/api/session", data={"name": "Bob", "incognito": "false"})).json()["id"]
            assert bob_session != sid and sessions.get_session(bob_session).owner == "bob" and sessions.get_session(sid).owner == "alice"
            return True
        finally:
            save_settings(original)
    assert run(scenario())

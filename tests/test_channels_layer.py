"""Chat channel layer with fake services only: no real account, no network."""
import asyncio
import json
import logging

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.openclank.channels.access import Access, MAX_PENDING, PAIR_TTL
from src.openclank.channels.base import Inbound, Limits
from src.openclank.channels.bridge import ChannelBridge, ChannelError
from src.openclank.channels.channel_token import path_allowed
from src.openclank.channels.chunking import split_text
from src.openclank.channels.runner import ChannelRunner
from src.openclank.channels.store import MAX_CONVERSATIONS, ChannelStore
from routes.channel_routes import setup_channel_routes


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def make_store(tmp_path, clock=None):
    return ChannelStore(str(tmp_path / "channels.json"), enc=lambda v: "enc:" + v[::-1], dec=lambda v: v[4:][::-1], clock=clock or Clock())


class FakeAdapter:
    name = "fake"
    def __init__(self, max_text=4096):
        self.limits = Limits(max_text=max_text, max_file_bytes=1000)
        self.texts, self.files, self.ran, self.closed = [], [], False, False
    async def run(self, on_inbound): self.ran = True; await asyncio.Event().wait()
    async def send_text(self, conv, thread, text): self.texts.append(text)
    async def send_file(self, conv, thread, name, data, mime): self.files.append((name, data, mime))
    async def aclose(self): self.closed = True


def msg(text="hello", *, channel="fake", conv="c1", sender="u1", direct=True, mentioned=False, bot=False, mid="m1", **kw):
    return Inbound(channel=channel, conv_id=conv, sender_id=sender, text=text, message_id=mid, is_direct=direct, mentioned=mentioned,
                   sender_is_bot=bot, sender_label="Ann", **kw)


class FakeApp:
    """The chat route, as seen through the in-process client."""
    def __init__(self, reply=("Hi ", "there"), events=(), incognito=False):
        self.calls, self.reply, self.events, self.incognito = [], reply, list(events), incognito
    def handler(self, request: httpx.Request) -> httpx.Response:
        path, body = request.url.path, request.content.decode()
        self.calls.append((request.method, path, body))
        if path == "/api/session":
            return httpx.Response(200, json={"id": "S1", "incognito": self.incognito})
        if path == "/api/chat_stream":
            frames = [f"data: {json.dumps(e)}\n\n" for e in self.events] + [f'data: {json.dumps({"delta": d})}\n\n' for d in self.reply] + ["data: [DONE]\n\n"]
            return httpx.Response(200, content="".join(frames).encode(), headers={"content-type": "text/event-stream"})
        if path.startswith("/api/session/") and path.endswith("/permission"):
            return httpx.Response(200, json={"ok": True})
        if path == "/api/generated-image/pic.png":
            return httpx.Response(200, content=b"PNGDATA", headers={"content-type": "image/png"})
        if path == "/api/upload":
            self.uploads = getattr(self, "uploads", []) + [request.content]
            return httpx.Response(200, json={"files": [{"id": f"U{len(self.uploads)}"}]})
        return httpx.Response(404)
    def form(self, path):
        return [dict(p.split("=", 1) for p in b.split("&")) for m, pth, b in self.calls if pth == path]


def bridge_for(tmp_path, app=None, clock=None):
    clock = clock or Clock()
    store = make_store(tmp_path, clock); app = app or FakeApp()
    store.set_setting("fake", "endpoint_id", "ep1"); store.set_setting("fake", "model", "m1")      # sessions need an authorized provider route
    client = httpx.AsyncClient(transport=httpx.MockTransport(app.handler), base_url="http://channel.local")
    slept = []
    async def sleep(s): slept.append(s)
    return store, app, ChannelBridge(store, Access(store, clock, code_factory=iter(["AAAA1111", "BBBB2222", "CCCC3333", "DDDD4444", "EEEE5555"]).__next__), client, sleep=sleep), clock, slept


def run(coro): return asyncio.run(coro)


def test_message_in_reply_out_and_split_at_the_service_limit(tmp_path):
    store, app, bridge, _c, slept = bridge_for(tmp_path, FakeApp(reply=("word " * 10,)))
    store.allow("fake", "sender", "u1", True)
    adapter = FakeAdapter(max_text=20); adapter.limits = Limits(max_text=20, max_file_bytes=1000, send_gap_seconds=1.0)
    run(bridge.handle(adapter, msg("question")))
    assert app.form("/api/chat_stream")[0]["message"] == "question" and app.form("/api/chat_stream")[0]["allow_bash"] == "false"
    assert adapter.texts and all(len(t) <= 20 for t in adapter.texts) and " ".join(adapter.texts).split() == ("word " * 10).split()
    assert len(slept) == len(adapter.texts) - 1                      # polite gap between chunks
    assert store.binding("fake|c1|") == "S1"


def test_file_out_from_a_generated_image_and_a_foreign_url_is_never_fetched(tmp_path):
    events = [{"type": "generated_image", "url": "/api/generated-image/pic.png"}, {"type": "generated_image", "url": "/api/session/S1/permission"}, {"type": "generated_image", "url": "http://evil.example/x.png"}]
    store, app, bridge, *_ = bridge_for(tmp_path, FakeApp(events=events))
    store.allow("fake", "sender", "u1", True); adapter = FakeAdapter()
    run(bridge.handle(adapter, msg()))
    assert adapter.files == [("pic.png", b"PNGDATA", "image/png")]
    assert [p for m, p, b in app.calls if "generated-image" in p or "evil" in p or p.endswith("permission")] == ["/api/generated-image/pic.png"]   # only the generated-image read path is ever fetched


def test_pairing_code_flow_expiry_and_pending_cap(tmp_path):
    store, app, bridge, clock, _s = bridge_for(tmp_path)
    adapter = FakeAdapter()
    run(bridge.handle(adapter, msg(sender="u1", mid="1")))
    assert "AAAA1111" in adapter.texts[0] and not app.calls                      # code sent, nothing reached the model
    run(bridge.handle(adapter, msg(sender="u1", mid="2")))
    assert len(adapter.texts) == 1                                               # code only once
    for i, who in enumerate(("u2", "u3", "u4"), start=2):
        run(bridge.handle(adapter, msg(sender=who, mid=str(i))))
    assert len(store.pending("fake")) == MAX_PENDING and len(adapter.texts) == 3  # the 4th request is ignored (cap)
    clock.t += PAIR_TTL + 1
    assert store.pending("fake") == []                                           # expired
    run(bridge.handle(adapter, msg(sender="u1", mid="9")))
    assert "DDDD4444" in adapter.texts[-1]                                       # a fresh code after expiry
    assert store.resolve_pending("fake", "dddd4444", True) == "u1" and store.senders("fake") == ["u1"]
    run(bridge.handle(adapter, msg(sender="u1", mid="10")))
    assert app.form("/api/chat_stream")                                          # approved: now processed
    assert store.resolve_pending("fake", "NOPE", True) is None


def test_group_rules_allow_list_mention_and_no_memory(tmp_path):
    store, app, bridge, *_ = bridge_for(tmp_path)
    adapter = FakeAdapter()
    run(bridge.handle(adapter, msg(direct=False, conv="g1", mentioned=True, mid="1")))
    assert not app.calls                                                         # group not allow-listed
    store.allow("fake", "group", "g1", True)
    run(bridge.handle(adapter, msg(direct=False, conv="g1", mentioned=False, mid="2")))
    assert not app.calls                                                         # allowed but not mentioned
    run(bridge.handle(adapter, msg(direct=False, conv="g1", mentioned=True, mid="3")))
    assert app.form("/api/chat_stream")[0]["no_memory"] == "true"                # group turns run without memory
    store.allow("fake", "sender", "u1", True)
    run(bridge.handle(adapter, msg(direct=True, conv="d1", mid="4")))
    assert "no_memory" not in app.form("/api/chat_stream")[1]                    # direct follows the owner's memory mode


def test_loop_guard_and_bot_senders(tmp_path):
    store, app, bridge, clock, _s = bridge_for(tmp_path)
    store.allow("fake", "sender", "u1", True); adapter = FakeAdapter()
    run(bridge.handle(adapter, msg(sender="bot1", bot=True, mid="b")))
    assert not app.calls                                                         # other bots are never answered
    for i in range(25):
        run(bridge.handle(adapter, msg(mid=f"x{i}")))
    assert len(app.form("/api/chat_stream")) == 20                               # 21st event in the window trips the guard
    clock.t += 30
    run(bridge.handle(adapter, msg(mid="during")))
    assert len(app.form("/api/chat_stream")) == 20                               # still cooling down
    clock.t += 31
    run(bridge.handle(adapter, msg(mid="after")))
    assert len(app.form("/api/chat_stream")) == 21                               # cooldown over


def test_a_channel_never_binds_to_an_incognito_session(tmp_path):
    store, app, bridge, *_ = bridge_for(tmp_path, FakeApp(incognito=True))
    store.allow("fake", "sender", "u1", True); adapter = FakeAdapter()
    run(bridge.handle(adapter, msg()))
    assert store.binding("fake|c1|") is None and not app.form("/api/chat_stream")
    assert adapter.texts == ["Something went wrong with that message. Check Open Clank."]
    assert app.form("/api/session")[0]["incognito"] == "false"                   # sessions are created as ordinary visible sessions


def test_permission_prompt_is_auto_rejected_and_only_a_paired_direct_identity_can_approve_once(tmp_path):
    ev = [{"type": "permission_request", "data": {"request_id": "R1", "options": [{"id": "once"}, {"id": "always"}], "detail": "rm x"}}]
    store, app, bridge, *_ = bridge_for(tmp_path, FakeApp(events=ev))
    store.allow("fake", "sender", "u1", True); adapter = FakeAdapter()
    run(bridge.handle(adapter, msg()))
    assert app.form("/api/session/S1/permission") == [{"request_id": "R1", "option_id": "reject"}]
    assert any("approval in the Open Clank app" in t for t in adapter.texts)
    # turned on for u1 only: the prompt goes to the chat and "yes" allows ONCE
    store.set_approve_in_chat("fake", "u1"); app.calls.clear(); adapter.texts.clear()
    async def scenario():
        task = asyncio.create_task(bridge.handle(adapter, msg(mid="m2")))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if any("Allow once" in t for t in adapter.texts): break
        await bridge.handle(adapter, msg("yes", mid="m3"))
        await task
    run(scenario())
    assert app.form("/api/session/S1/permission")[-1]["option_id"] == "once"
    # a group never gets the chat approval path, even for the same sender
    store.allow("fake", "group", "g1", True); app.calls.clear()
    run(bridge.handle(adapter, msg(direct=False, conv="g1", mentioned=True, mid="m4")))
    assert app.form("/api/session/S1/permission")[-1]["option_id"] == "reject"


def test_secrets_never_appear_in_store_file_status_routes_or_logs(tmp_path, caplog):
    store = make_store(tmp_path)
    store.set_secret("telegram", "token", "SECRET-TOKEN-123"); store.set_enabled("telegram", True)
    assert "SECRET-TOKEN-123" not in (tmp_path / "channels.json").read_text()               # encrypted at rest
    assert "SECRET-TOKEN-123" not in json.dumps(store.public_status("telegram")) and store.public_status("telegram")["configured"] is True
    runner = ChannelRunner(store, global_enabled=lambda: True, adapter_factories={}, bridge_factory=lambda: None)
    def user(request):
        if request.headers.get("x-api") : raise HTTPException(403, "API tokens must use a scope-aware API route")
        return "alice"
    settings = {"channels_enabled": False}
    router = setup_channel_routes(store, runner, require_user=user, load_settings=lambda: dict(settings), save_settings=lambda s: settings.update(s),
                                  provision=lambda owner, inv: "occ_x")
    app = FastAPI(); app.include_router(router); client = TestClient(app)
    out = client.post("/api/channels/telegram/config", json={"secrets": {"token": "SECRET-TOKEN-123"}, "enabled": False})
    assert out.status_code == 200 and "SECRET-TOKEN-123" not in out.text and "SECRET-TOKEN-123" not in client.get("/api/channels").text
    assert client.post("/api/channels/telegram/config", json={"secrets": {"bogus": "x"}}).status_code == 422
    assert client.post("/api/channels/nope/config", json={}).status_code == 404
    assert client.get("/api/channels", headers={"x-api": "1"}).status_code == 403
    store2 = ChannelStore(str(tmp_path / "channels.json"), enc=lambda v: "enc:" + v[::-1], dec=lambda v: v[4:][::-1])
    assert store2.secret("telegram", "token") == "SECRET-TOKEN-123" and store2.owner() == "alice"
    caplog.set_level(logging.DEBUG)
    from src.openclank.channels.telegram import TelegramAdapter
    def boom(request): raise httpx.ConnectError("failed for " + str(request.url))
    adapter = TelegramAdapter(lambda: "SECRET-TOKEN-123", httpx.AsyncClient(transport=httpx.MockTransport(boom)), sleep=_stop_after(1))
    with pytest.raises(_Stop): run(adapter.run(lambda i: asyncio.sleep(0)))
    assert "SECRET-TOKEN-123" not in caplog.text and "SECRET-TOKEN-123" not in repr(adapter)


class _Stop(Exception): pass


def _stop_after(n):
    seen = []
    async def sleep(s):
        seen.append(s)
        if len(seen) >= n: raise _Stop()
    sleep.seen = seen
    return sleep


def test_store_bounds_conversations_and_keeps_secrets_out_of_status(tmp_path):
    store = make_store(tmp_path)
    for i in range(MAX_CONVERSATIONS + 5): store.bind(f"k{i}", f"s{i}")
    assert store.binding_count() == MAX_CONVERSATIONS and store.binding("k0") is None and store.binding(f"k{MAX_CONVERSATIONS + 4}")
    assert not store.configured("telegram"); store.set_secret("telegram", "token", "x"); assert store.configured("telegram")
    store.set_secret("matrix", "access_token", "y"); assert not store.configured("matrix"); store.set_setting("matrix", "homeserver", "https://h"); assert store.configured("matrix")


def test_off_means_zero_network_and_nothing_constructed(tmp_path):
    seen, built = [], []
    transport = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={}))
    store = make_store(tmp_path); flag = {"on": False}
    def factory(st): built.append(1); return FakeAdapter()
    runner = ChannelRunner(store, global_enabled=lambda: flag["on"], adapter_factories={"telegram": factory}, bridge_factory=lambda: ChannelBridge(store, Access(store), httpx.AsyncClient(transport=transport)))
    async def scenario():
        await runner.reload(); assert runner.running() == []                      # global off
        flag["on"] = True; await runner.reload(); assert runner.running() == []   # global on, channel not switched on
        store.set_enabled("telegram", True); await runner.reload(); assert runner.running() == []   # switched on, no secret
        store.set_secret("telegram", "token", "t"); await runner.reload(); await asyncio.sleep(0)
        assert runner.running() == ["telegram"] and built == [1]                  # only now is anything constructed
        flag["on"] = False; await runner.reload(); assert runner.running() == []; await runner.stop()
    run(scenario())
    assert seen == []                                                             # the transport saw nothing at any point


def test_channel_token_is_confined_to_chat_and_session_routes():
    for method, ok in (("POST", "/api/chat_stream"), ("POST", "/api/session"), ("POST", "/api/session/abc/permission"), ("POST", "/api/upload"), ("GET", "/api/generated-image/a.png")):
        assert path_allowed(ok, method), ok
    for method, bad in (("GET", "/api/memory"), ("GET", "/api/tokens"), ("POST", "/api/channels"), ("GET", "/api/sessions"), ("GET", "/api/settings"), ("POST", "/api/sessionx"),
                        ("GET", "/api/chat_stream"), ("GET", "/api/upload/abc"), ("POST", "/api/generated-image/a.png"), ("GET", "/api/generated-image/../x"), ("DELETE", "/api/session"), ("GET", "/")):
        assert not path_allowed(bad, method), (method, bad)


def test_attachments_in_are_off_by_default_limited_to_images_and_plain_documents_and_go_through_the_upload_route(tmp_path):
    from src.openclank.channels.base import Attachment
    async def blob(data):
        return data
    def att(name, mime, data, size=None):
        return Attachment(name, mime, len(data) if size is None else size, lambda d=data: blob(d))
    store, app, bridge, *_ = bridge_for(tmp_path)
    store.allow("fake", "sender", "u1", True); adapter = FakeAdapter()
    adapter.limits = Limits(max_text=4096, max_file_bytes=1000, max_inbound_bytes=100)
    run(bridge.handle(adapter, msg("look", inbound_attachments=None) if False else Inbound(channel="fake", conv_id="c1", sender_id="u1", text="look", message_id="1", is_direct=True, attachments=(att("a.png", "image/png", b"PNG"),))))
    assert any("Attachments are turned off" in t for t in adapter.texts) and not app.form("/api/upload") and "attachments" not in app.form("/api/chat_stream")[0]
    store.set_setting("fake", "attachments_in", "true"); adapter.texts.clear()
    run(bridge.handle(adapter, Inbound(channel="fake", conv_id="c1", sender_id="u1", text="", message_id="2", is_direct=True, attachments=(
        att("a.png", "image/png", b"PNGDATA"), att("evil.exe", "application/x-msdownload", b"MZ"), att("big.txt", "text/plain", b"x" * 500),
        att("lie.txt", "text/plain", b"y" * 500, size=5)))))
    from urllib.parse import unquote
    form = app.form("/api/chat_stream")[-1]
    assert json.loads(unquote(form["attachments"])) == ["U1"] and len(app.uploads) == 1 and b"PNGDATA" in app.uploads[0]     # only the png: exe, oversize and a lying size are refused
    assert "sent+an+attachment" in form["message"]
    run(bridge.handle(adapter, Inbound(channel="fake", conv_id="c1", sender_id="u1", text="read", message_id="3", is_direct=True, attachments=(att("../../etc/passwd.txt", "text/plain", b"ok"),))))
    assert b'filename="passwd.txt"' in app.uploads[1]                                                                        # path parts are stripped from the name
    many = tuple(att(f"{i}.png", "image/png", b"P") for i in range(6))
    run(bridge.handle(adapter, Inbound(channel="fake", conv_id="c1", sender_id="u1", text="many", message_id="4", is_direct=True, attachments=many)))
    assert len(app.uploads) == 2 + 4                                                                                          # at most 4 per message
    skipped = " ".join(adapter.texts)
    assert "evil.exe" in skipped and "big.txt" in skipped and "lie.txt" in skipped


def test_without_a_chosen_model_the_bridge_says_so_and_creates_no_session(tmp_path):
    store, app, bridge, *_ = bridge_for(tmp_path)
    store.set_setting("fake", "model", ""); store.allow("fake", "sender", "u1", True); adapter = FakeAdapter()
    run(bridge.handle(adapter, msg()))
    assert adapter.texts == ["Choose a model for this channel in the Open Clank settings first."] and not app.calls and store.binding("fake|c1|") is None
    store.set_setting("fake", "model", "m1"); run(bridge.handle(adapter, msg(mid="2")))
    assert app.form("/api/session")[0]["endpoint_id"] == "ep1" and app.form("/api/session")[0]["model"] == "m1"

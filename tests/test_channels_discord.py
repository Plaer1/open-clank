"""Discord adapter against a fake gateway (the ws_client test server) and a fake REST API (httpx.MockTransport)."""
import asyncio
import json
import struct

import httpx
import pytest

from src.openclank.channels import ws_client as wsc
from src.openclank.channels.discord import INTENTS, DiscordAdapter, DiscordFatal
from tests.test_channels_ws_client import frame, read_client_frame, run, with_server

TOKEN = "DISCORD-SECRET-TOKEN"


def send(w, payload): w.write(frame(wsc.OP_TEXT, json.dumps(payload).encode()))


def rest(handler_log, gateway_url):
    async def handler(request):
        handler_log.append((request.method, request.url.path, request.headers.get("authorization"), request.content))
        if request.url.path.endswith("/gateway/bot"): return httpx.Response(200, json={"url": gateway_url})
        if request.url.path == "/cdn/a.png": return httpx.Response(200, content=b"PNG")
        return httpx.Response(200, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def message(mid, author, content="hi", guild=None, mentions=(), attachments=(), ref=None, bot=False, webhook=None):
    d = {"id": mid, "channel_id": "ch1", "author": {"id": author, "bot": bot, "username": "u" + author}, "content": content, "mentions": [{"id": m} for m in mentions],
         "attachments": list(attachments)}
    if guild: d["guild_id"] = guild
    if ref: d["referenced_message"] = {"author": {"id": ref}}
    if webhook: d["webhook_id"] = webhook
    return d


def test_identify_ready_messages_resume_after_reconnect_and_fatal_close():
    async def gateway(r, w, s):
        if s.connections == 1:
            send(w, {"op": 10, "d": {"heartbeat_interval": 40}}); await w.drain()
            _op, data = await read_client_frame(r); s.seen.append(json.loads(data))                  # identify
            send(w, {"op": 0, "s": 1, "t": "READY", "d": {"user": {"id": "99"}, "session_id": "SESS", "resume_gateway_url": s.url}})
            send(w, {"op": 0, "s": 2, "t": "MESSAGE_CREATE", "d": message("m1", "5")})                                    # direct message
            send(w, {"op": 0, "s": 3, "t": "MESSAGE_CREATE", "d": message("m2", "6", "hey <@99>", guild="g", mentions=["99"])})
            send(w, {"op": 0, "s": 4, "t": "MESSAGE_CREATE", "d": message("m3", "7", bot=True)})                          # another bot
            send(w, {"op": 0, "s": 5, "t": "MESSAGE_CREATE", "d": message("m4", "99")})                                   # its own message
            send(w, {"op": 0, "s": 6, "t": "MESSAGE_CREATE", "d": message("m5", "8", webhook="w")})                       # a webhook
            send(w, {"op": 0, "s": 7, "t": "MESSAGE_CREATE", "d": message("m6", "9", "reply", guild="g", ref="99")})      # a reply to the bot
            send(w, {"op": 7, "d": None}); await w.drain()                                                                # please reconnect
            await asyncio.sleep(0.3)
        else:
            send(w, {"op": 10, "d": {"heartbeat_interval": 40}}); await w.drain()
            _op, data = await read_client_frame(r); s.seen.append(json.loads(data))                  # resume
            send(w, {"op": 0, "s": 8, "t": "MESSAGE_CREATE", "d": message("m7", "5", "after resume")}); await w.drain()
            await asyncio.sleep(0.05)
            w.write(frame(wsc.OP_CLOSE, struct.pack("!H", 4004) + b"auth")); await w.drain()
    async def client(url, server):
        server.url = url; log = []; got = []; slept = []
        async def sleep(d): slept.append(d)
        adapter = DiscordAdapter(lambda: TOKEN, rest(log, url), sleep=sleep, jitter=lambda: 0.1)
        async def on(inbound): got.append(inbound)
        with pytest.raises(DiscordFatal): await adapter.run(on)
        return server.seen, got, slept, log
    seen, got, slept, log = run(with_server(gateway, client))
    identify, resume = seen
    assert identify["op"] == 2 and identify["d"]["token"] == TOKEN and identify["d"]["intents"] == INTENTS and INTENTS & (1 << 15)
    assert resume["op"] == 6 and resume["d"] == {"token": TOKEN, "session_id": "SESS", "seq": 7}                 # resumed, not re-identified
    assert [(g.message_id, g.is_direct, g.mentioned, g.sender_is_bot) for g in got] == [("m1", True, False, False), ("m2", False, True, False), ("m3", True, False, True), ("m6", False, True, False), ("m7", True, False, False)]   # bots are flagged (the access layer ignores them); own messages and webhooks never arrive
    assert slept == [1.0] and all(auth == f"Bot {TOKEN}" for m, p, auth, c in log if p.endswith("/gateway/bot"))


def test_zombie_connection_without_heartbeat_ack_is_dropped_and_a_token_rejection_is_fatal():
    async def gateway(r, w, s):
        send(w, {"op": 10, "d": {"heartbeat_interval": 20}}); await w.drain()
        frames = []
        for _ in range(3):
            try: frames.append(await asyncio.wait_for(read_client_frame(r), 1))
            except Exception: break
        s.seen.append([(op, d) for op, d in frames])
        await asyncio.sleep(0.2)
    async def client(url, server):
        adapter = DiscordAdapter(lambda: TOKEN, rest([], url), sleep=lambda d: asyncio.sleep(0), jitter=lambda: 0.0)
        task = asyncio.create_task(adapter.run(lambda i: asyncio.sleep(0)))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if server.seen: break
        task.cancel()
        return server.seen[0]
    frames = run(with_server(gateway, client))
    ops = [(op, json.loads(d)["op"]) if op == wsc.OP_TEXT else (op, None) for op, d in frames]
    assert (wsc.OP_TEXT, 2) in ops and (wsc.OP_TEXT, 1) in ops and any(op == wsc.OP_CLOSE and struct.unpack("!H", d[:2])[0] == 4000 for op, d in frames)   # no ack: closed with 4000
    async def rejected():
        async def handler(request): return httpx.Response(401, json={})
        adapter = DiscordAdapter(lambda: TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with pytest.raises(DiscordFatal): await adapter.run(lambda i: asyncio.sleep(0))
    run(rejected())


def test_rest_sends_text_without_pings_files_429_retry_and_attachments_download():
    log = []
    async def scenario():
        slept = []
        async def sleep(d): slept.append(d)
        state = {"n": 0}
        async def handler(request):
            log.append((request.method, request.url.path, request.content, request.headers.get("content-type", "")))
            if request.url.path == "/api/v10/channels/c1/messages":
                state["n"] += 1
                if state["n"] == 1: return httpx.Response(429, json={"retry_after": 2.5})
            return httpx.Response(200, json={})
        adapter = DiscordAdapter(lambda: TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=sleep)
        await adapter.send_text("c1", None, "hello @everyone")
        await adapter.send_file("c2", None, "a.txt", b"data", "text/plain")
        att = adapter._parse(message("m9", "5", "", attachments=[{"filename": "a.png", "content_type": "image/png", "size": 3, "url": "https://cdn.example/cdn/a.png"}]))
        return slept, att
    slept, att = run(scenario())
    assert slept == [2.5]                                                                       # the service's retry_after, once
    sends = [c for m, p, c, t in log if p.endswith("/messages")]
    assert json.loads(sends[1])["content"] == "hello @everyone" and json.loads(sends[1])["allowed_mentions"] == {"parse": []}   # model output cannot ping anyone
    assert b'filename="a.txt"' in [c for m, p, c, t in log if p == "/api/v10/channels/c2/messages"][0]
    assert att.text == "" and att.attachments[0].name == "a.png" and att.attachments[0].mime == "image/png"
    assert TOKEN not in repr(DiscordAdapter(lambda: TOKEN, httpx.AsyncClient()))

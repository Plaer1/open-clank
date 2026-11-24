"""Feishu/Lark adapter: codec round trip, fake endpoint and fake long connection, domain switch. UNVERIFIED against the real service."""
import asyncio
import json

import httpx
import pytest

from src.openclank.channels import ws_client as wsc
from src.openclank.channels.feishu import FeishuAdapter, FeishuFatal
from src.openclank.channels.feishu_codec import Frame, ack_frame, decode_frame, encode_frame, ping_frame, service_id
from tests.test_channels_ws_client import frame as ws_frame, read_client_frame, run, with_server

SECRET = "APP-SECRET-VALUE"


def event(eid, chat_type="p2p", text="hello", mtype="text", mentions=(), sender_type="user", key=None, chat="oc_1", mid="om_1"):
    content = {"text": text} if mtype == "text" else {"image_key": key} if mtype == "image" else {"file_key": key, "file_name": "a.txt"}
    return json.dumps({"header": {"event_id": eid, "event_type": "im.message.receive_v1"},
                       "event": {"sender": {"sender_id": {"open_id": "ou_user"}, "sender_type": sender_type},
                                 "message": {"message_id": mid, "chat_id": chat, "chat_type": chat_type, "message_type": mtype, "content": json.dumps(content),
                                             "mentions": [{"id": {"open_id": m}} for m in mentions]}}}).encode()


def data_frame(payload, seq=1):
    return encode_frame(Frame(seq_id=seq, log_id=9, service=7, method=1, headers={"type": "event", "message_id": f"x{seq}"}, payload=payload))


def test_codec_round_trip_and_known_bytes():
    f = Frame(seq_id=5, log_id=300, service=7, method=1, headers={"type": "event", "k": "v"}, payload_type="json", payload=b"\x00\xff", log_id_new=b"z")
    assert decode_frame(encode_frame(f)) == f
    assert encode_frame(Frame(service=1, method=0, headers={"type": "ping"})) == bytes.fromhex("0800" "1000" "1801" "2000" "2a0c0a0474797065120470696e67")
    assert decode_frame(ping_frame(3)).headers == {"type": "ping"} and decode_frame(ping_frame(3)).service == 3
    ack = decode_frame(ack_frame(Frame(seq_id=1, log_id=2, service=7, method=1, headers={"type": "event"}, payload=b"zzz")))
    assert json.loads(ack.payload) == {"code": 200} and ack.headers == {"type": "event"} and ack.seq_id == 1
    assert service_id("wss://h/ws?device_id=1&service_id=42") == 42 and service_id("wss://h/ws") == 0
    for bad in (b"\x08", b"\x2a\x10abc", b"\x0b"):
        with pytest.raises(ValueError): decode_frame(bad)
    with pytest.raises(ValueError): decode_frame(b"x" * 20, max_size=10)


def rest(log, ws_url, domain_hits=None, token_code=0):
    async def handler(request):
        log.append((request.method, request.url.host, request.url.path, dict(request.url.params), request.content))
        p = request.url.path
        if p.endswith("/tenant_access_token/internal"): return httpx.Response(200, json={"code": token_code, "tenant_access_token": "t-1", "expire": 7200})
        if p.endswith("/bot/v3/info"): return httpx.Response(200, json={"bot": {"open_id": "ou_bot"}})
        if p == "/callback/ws/endpoint": return httpx.Response(200, json={"code": 0, "data": {"URL": ws_url.split("?")[0] + "?service_id=7", "ClientConfig": {"PingInterval": 0.03}}})
        if p.endswith("/images"): return httpx.Response(200, json={"data": {"image_key": "img_k"}})
        if p.endswith("/files"): return httpx.Response(200, json={"data": {"file_key": "file_k"}})
        if "/resources/" in p: return httpx.Response(200, content=b"RESOURCE")
        return httpx.Response(200, json={"code": 0})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_events_in_acknowledged_after_handover_deduplicated_and_pinged():
    async def gateway(r, w, s):
        for i, payload in enumerate([event("e1"), event("e2", chat_type="group", mentions=["ou_bot"], chat="oc_g"), event("e1"), event("e3", sender_type="app"),
                                     event("e4", mtype="image", key="ik", mid="om_img")], start=1):
            w.write(ws_frame(wsc.OP_BINARY, data_frame(payload, seq=i)))
        await w.drain()
        for _ in range(8):
            try: s.seen.append(await asyncio.wait_for(read_client_frame(r), 1))
            except Exception: break
    async def client(url, server):
        log, got = [], []
        adapter = FeishuAdapter(lambda: "cli_app", lambda: SECRET, lambda: "feishu", rest(log, url))
        async def on(inbound): got.append(inbound)
        task = asyncio.create_task(adapter.run(on))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(got) >= 4: break
        await asyncio.sleep(0.2)                                       # let the ping interval elapse
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        return got, server.seen, log, adapter
    got, seen, log, adapter = run(with_server(gateway, client))
    assert [(g.message_id, g.is_direct, g.mentioned, g.sender_is_bot, bool(g.attachments)) for g in got] == [
        ("om_1", True, False, False, False), ("om_1", False, True, False, False), ("om_1", True, False, True, False), ("om_img", True, False, False, True)]
    assert all(g.channel == "feishu" for g in got) and got[1].conv_id == "oc_g"
    acks = [decode_frame(d) for op, d in seen if op == wsc.OP_BINARY]
    assert any(json.loads(a.payload) == {"code": 200} for a in acks if a.method == 1), "events are acknowledged"
    assert any(a.method == 0 and a.headers.get("type") == "ping" and a.service == 7 for a in acks), "pings are sent on the configured interval"
    assert SECRET not in repr(adapter)


def test_domain_switch_token_cache_credentials_rejection_and_sends():
    async def scenario():
        for domain, host in (("feishu", "open.feishu.cn"), ("lark", "open.larksuite.com")):
            log = []
            adapter = FeishuAdapter(lambda: "cli_app", lambda: SECRET, lambda: domain, rest(log, "ws://x"), clock=lambda: 1000.0)
            await adapter.send_text("oc_1", None, "hi"); await adapter.send_text("oc_1", None, "again")
            assert {h for _m, h, *_ in log} == {host}
            assert sum(1 for _m, _h, p, *_ in log if p.endswith("tenant_access_token/internal")) == 1          # the token is cached
            sent = [json.loads(c) for _m, _h, p, q, c in log if p == "/open-apis/im/v1/messages"]
            assert sent[0] == {"receive_id": "oc_1", "msg_type": "text", "content": json.dumps({"text": "hi"})} and [q for *_x, p, q, c in log if p == "/open-apis/im/v1/messages"][0] == {"receive_id_type": "chat_id"}
        log = []; adapter = FeishuAdapter(lambda: "cli_app", lambda: SECRET, lambda: "lark", rest(log, "ws://x"))
        await adapter.send_file("oc_1", None, "p.png", b"PNG", "image/png"); await adapter.send_file("oc_1", None, "a.txt", b"T", "text/plain")
        kinds = [json.loads(c)["msg_type"] for _m, _h, p, q, c in log if p == "/open-apis/im/v1/messages"]
        assert kinds == ["image", "file"]
        with pytest.raises(FeishuFatal):
            await FeishuAdapter(lambda: "cli_app", lambda: SECRET, lambda: "feishu", rest([], "ws://x", token_code=10003)).run(lambda i: asyncio.sleep(0))
        fetcher = FeishuAdapter(lambda: "a", lambda: "b", lambda: "feishu", rest([], "ws://x"))._parse(event("e9", mtype="file", key="fk"))
        assert fetcher.attachments[0].name == "a.txt" and await fetcher.attachments[0].fetch() == b"RESOURCE"
    run(scenario())


def test_routes_accept_feishu_and_discord_fields_and_reject_a_bad_domain(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.channel_routes import setup_channel_routes
    from src.openclank.channels.store import ChannelStore
    store = ChannelStore(str(tmp_path / "c.json"), enc=lambda v: "enc:" + v[::-1], dec=lambda v: v[4:][::-1])
    class R:
        def running(self): return []
        async def reload(self): pass
    app = FastAPI(); app.include_router(setup_channel_routes(store, R(), require_user=lambda r: "alice", load_settings=lambda: {}, save_settings=lambda s: None, provision=lambda o, i: "occ_x"))
    c = TestClient(app)
    ok = c.post("/api/channels/feishu/config", json={"secrets": {"app_secret": SECRET}, "settings": {"app_id": "cli_1", "domain": "lark"}})
    assert ok.status_code == 200 and SECRET not in ok.text and ok.json()["channels"]["feishu"]["configured"] is True and ok.json()["channels"]["feishu"]["settings"]["domain"] == "lark"
    assert c.post("/api/channels/feishu/config", json={"settings": {"domain": "evil.example"}}).status_code == 422
    assert c.post("/api/channels/discord/config", json={"secrets": {"token": "D"}}).json()["channels"]["discord"]["configured"] is True
    assert c.post("/api/channels/discord/config", json={"secrets": {"app_secret": "x"}}).status_code == 422

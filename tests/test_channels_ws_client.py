"""ws_client against a standard-library fake server: frames, fragmentation, ping/pong, close, masking, bounds, reconnect, TLS defaults."""
import asyncio
import base64
import hashlib
import ssl
import struct

import pytest

from src.openclank.channels import ws_client as wsc
from src.openclank.channels.ws_client import Closed, FrameTooLarge, WebSocket, WebSocketError

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def run(coro): return asyncio.run(coro)


def frame(opcode, payload=b"", fin=True, mask=None):
    b1 = (0x80 if fin else 0) | opcode
    n = len(payload)
    head = bytes([b1]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack("!H", n) if n < 65536 else bytes([127]) + struct.pack("!Q", n))
    if mask is not None:
        head = head[:1] + bytes([head[1] | 0x80]) + head[2:] + mask
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return head + payload


async def read_client_frame(reader):
    b1, b2 = await reader.readexactly(2)
    assert b2 & 0x80, "client frames must be masked"
    n = b2 & 0x7F
    if n == 126: (n,) = struct.unpack("!H", await reader.readexactly(2))
    elif n == 127: (n,) = struct.unpack("!Q", await reader.readexactly(8))
    key = await reader.readexactly(4); data = await reader.readexactly(n)
    return b1 & 0x0F, bytes(b ^ key[i % 4] for i, b in enumerate(data))


class Server:
    def __init__(self, script, accept_ok=True):
        self.script, self.accept_ok, self.seen, self.connections = script, accept_ok, [], 0
    async def handle(self, reader, writer):
        self.connections += 1
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        key = [l.split(":", 1)[1].strip() for l in head.split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode() if self.accept_ok else "bad"
        writer.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        await writer.drain()
        try:
            await self.script(reader, writer, self)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def with_server(script, client, accept_ok=True):
    server = Server(script, accept_ok)
    srv = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        return await client(f"ws://127.0.0.1:{port}/path?x=1", server)
    finally:
        srv.close(); await srv.wait_closed()


def test_text_binary_fragmentation_and_masked_client_frames():
    async def script(r, w, s):
        w.write(frame(wsc.OP_TEXT, "héllo".encode()))
        w.write(frame(wsc.OP_TEXT, b"frag", fin=False) + frame(wsc.OP_CONT, b"ment", fin=False) + frame(wsc.OP_PING, b"p") + frame(wsc.OP_CONT, b"ed"))
        w.write(frame(wsc.OP_BINARY, b"\x00\x01"))
        w.write(frame(wsc.OP_TEXT, b"x" * 70000))
        await w.drain()
        for _ in range(4): s.seen.append(await read_client_frame(r))
        await asyncio.sleep(0.05)
    async def client(url, server):
        ws = await WebSocket.connect(url, max_frame=1 << 20)
        got = [await ws.recv(), await ws.recv(), await ws.recv(), await ws.recv()]
        await ws.send_text("hi ✓"); await ws.send_binary(b"\x01" * 200); await ws.ping(b"k")
        await asyncio.sleep(0.1)
        return got, server.seen
    got, seen = run(with_server(script, client))
    assert got[0] == "héllo" and got[1] == "fragmented" and got[2] == b"\x00\x01" and got[3] == "x" * 70000
    assert [o for o, _ in seen] == [wsc.OP_PONG, wsc.OP_TEXT, wsc.OP_BINARY, wsc.OP_PING]      # the ping inside the fragmented message was answered; every client frame was masked
    assert seen[0][1] == b"p" and seen[1][1] == "hi \u2713".encode()


def test_ping_is_answered_with_a_masked_pong_and_close_handshake_is_acknowledged():
    async def script(r, w, s):
        w.write(frame(wsc.OP_PING, b"hb")); await w.drain()
        s.seen.append(await read_client_frame(r))
        w.write(frame(wsc.OP_CLOSE, struct.pack("!H", 4004) + b"auth")); await w.drain()
        s.seen.append(await read_client_frame(r))                 # the client echoes the close
    async def client(url, server):
        ws = await WebSocket.connect(url)
        with pytest.raises(Closed) as info:
            await ws.recv()
        await asyncio.sleep(0.05)
        return info.value, server.seen
    closed, seen = run(with_server(script, client))
    assert (closed.code, closed.reason) == (4004, "auth")
    assert seen[0] == (wsc.OP_PONG, b"hb") and seen[1][0] == wsc.OP_CLOSE


def test_client_initiated_close_waits_for_the_peer_and_then_stops():
    async def script(r, w, s):
        op, data = await read_client_frame(r); s.seen.append((op, struct.unpack("!H", data[:2])[0]))
        w.write(frame(wsc.OP_CLOSE, data[:2])); await w.drain()
    async def client(url, server):
        ws = await WebSocket.connect(url); await ws.close(1000, "bye"); return server.seen
    assert run(with_server(script, client)) == [(wsc.OP_CLOSE, 1000)]


def test_oversized_frame_and_message_are_refused_with_1009_and_masked_server_frames_are_a_protocol_error():
    async def big(r, w, s):
        w.write(frame(wsc.OP_TEXT, b"x" * 5000)); await w.drain()
        try: s.seen.append(await read_client_frame(r))
        except Exception: pass
    async def client_frame(url, server):
        ws = await WebSocket.connect(url, max_frame=1024)
        with pytest.raises(FrameTooLarge): await ws.recv()
        await asyncio.sleep(0.05); return server.seen
    seen = run(with_server(big, client_frame))
    assert seen and seen[0][0] == wsc.OP_CLOSE and struct.unpack("!H", seen[0][1][:2])[0] == 1009
    async def frags(r, w, s):
        w.write(frame(wsc.OP_TEXT, b"a" * 600, fin=False) + frame(wsc.OP_CONT, b"b" * 600)); await w.drain(); await asyncio.sleep(0.1)
    async def client_msg(url, server):
        ws = await WebSocket.connect(url, max_frame=1024, max_message=1000)
        with pytest.raises(FrameTooLarge): await ws.recv()
    run(with_server(frags, client_msg))
    async def masked(r, w, s):
        w.write(frame(wsc.OP_TEXT, b"x", mask=b"abcd")); await w.drain(); await asyncio.sleep(0.1)
    async def client_masked(url, server):
        ws = await WebSocket.connect(url)
        with pytest.raises(WebSocketError): await ws.recv()
    run(with_server(masked, client_masked))


def test_handshake_with_a_wrong_accept_key_is_refused():
    async def script(r, w, s): await asyncio.sleep(0.1)
    async def client(url, server):
        with pytest.raises(WebSocketError): await WebSocket.connect(url)
    run(with_server(script, client, accept_ok=False))


def test_wss_uses_ssl_defaults_and_ws_uses_none():
    seen = {}
    async def fake_open(host, port, ssl=None, server_hostname=None):
        seen.update(host=host, port=port, ssl=ssl, hostname=server_hostname); raise ConnectionRefusedError()
    for url in ("wss://gateway.example/x", "ws://127.0.0.1:9/x"):
        with pytest.raises(ConnectionRefusedError): run(WebSocket.connect(url, open_connection=fake_open))
        if url.startswith("wss"):
            assert isinstance(seen["ssl"], ssl.SSLContext) and seen["ssl"].verify_mode == ssl.CERT_REQUIRED and seen["ssl"].check_hostname is True
            assert seen["port"] == 443 and seen["hostname"] == "gateway.example"
        else:
            assert seen["ssl"] is None and seen["port"] == 9
    with pytest.raises(WebSocketError): run(WebSocket.connect("http://x/"))


def test_reconnect_with_doubling_backoff_resets_after_success_and_stops_on_fatal():
    async def script(r, w, s):
        w.write(frame(wsc.OP_CLOSE, struct.pack("!H", 1001))); await w.drain()
    async def client(url, server):
        sleeps, state = [], {"n": 0}
        async def sleep(d): sleeps.append(d)
        async def connect():
            state["n"] += 1
            if state["n"] in (1, 2): raise ConnectionRefusedError()      # two failures: 1 s then 2 s
            return await WebSocket.connect(url)
        async def session(ws):
            try: await ws.recv()
            except Closed as exc:
                if state["n"] >= 5: raise RuntimeError("stop") from exc
                raise
        with pytest.raises(RuntimeError):
            await wsc.reconnecting(connect, session, sleep=sleep, fatal=lambda e: isinstance(e, RuntimeError))
        return sleeps
    assert run(with_server(script, client)) == [1.0, 2.0, 1.0, 1.0]     # reset to the start after the first successful connection

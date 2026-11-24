"""A small WebSocket client (RFC 6455) on the standard library: asyncio streams, `ssl` defaults for wss, no dependency.

Text and binary messages, fragmentation, ping/pong, the close handshake, client-side masking, bounded frames and
messages, and a reconnect loop with backoff. It never follows redirects and never sends credentials by itself."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import os
import ssl
import struct
from typing import Awaitable, Callable
from urllib.parse import urlsplit

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    pass


class FrameTooLarge(WebSocketError):
    pass


class Closed(WebSocketError):
    def __init__(self, code: int = 1006, reason: str = ""):
        super().__init__(f"websocket closed {code}")
        self.code, self.reason = code, reason


def _mask(data: bytes, key: bytes) -> bytes:
    if not data:
        return data
    block = (key * (len(data) // 4 + 1))[: len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(block, "big")).to_bytes(len(data), "big")


class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, max_frame: int, max_message: int):
        self._r, self._w, self._max_frame, self._max_message = reader, writer, max_frame, max_message
        self._closed = False
        self._close_sent = False

    @classmethod
    async def connect(cls, url: str, *, headers: dict[str, str] | None = None, ssl_context: ssl.SSLContext | None = None,
                      max_frame: int = 1 << 20, max_message: int = 4 << 20, connect_timeout: float = 15.0,
                      open_connection: Callable = asyncio.open_connection) -> "WebSocket":
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss") or not parts.hostname:
            raise WebSocketError("not a websocket url")
        secure = parts.scheme == "wss"
        port = parts.port or (443 if secure else 80)
        context = (ssl_context or ssl.create_default_context()) if secure else None     # certificate and host name are verified
        reader, writer = await asyncio.wait_for(open_connection(parts.hostname, port, ssl=context, server_hostname=parts.hostname if secure else None), connect_timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        lines = [f"GET {target} HTTP/1.1", f"Host: {parts.netloc}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"] + [f"{k}: {v}" for k, v in (headers or {}).items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), connect_timeout)
        status, *rest = head.decode("latin-1").split("\r\n")
        fields = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in rest if ":" in line)}
        expected = base64.b64encode(hashlib.sha1(key.encode() + _GUID).digest()).decode()
        if " 101" not in status or fields.get("sec-websocket-accept") != expected:
            writer.close()
            raise WebSocketError("websocket handshake refused")                           # redirects are not followed
        return cls(reader, writer, max_frame=max_frame, max_message=max_message)

    # -- sending (always masked)
    async def _send(self, opcode: int, payload: bytes) -> None:
        if self._closed and opcode != OP_CLOSE:
            raise Closed()
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        key = os.urandom(4)
        self._w.write(header + key + _mask(payload, key))
        await self._w.drain()

    async def send_text(self, text: str) -> None:
        await self._send(OP_TEXT, text.encode("utf-8"))

    async def send_binary(self, data: bytes) -> None:
        await self._send(OP_BINARY, data)

    async def ping(self, data: bytes = b"") -> None:
        await self._send(OP_PING, data[:125])

    # -- receiving
    async def _frame(self) -> tuple[bool, int, bytes]:
        try:
            b1, b2 = await self._r.readexactly(2)
            fin, opcode, masked, n = bool(b1 & 0x80), b1 & 0x0F, bool(b2 & 0x80), b2 & 0x7F
            if b1 & 0x70:
                raise WebSocketError("reserved bits set")
            if n == 126:
                (n,) = struct.unpack("!H", await self._r.readexactly(2))
            elif n == 127:
                (n,) = struct.unpack("!Q", await self._r.readexactly(8))
            if masked:
                raise WebSocketError("server frames must not be masked")
            if n > self._max_frame:
                await self.close(1009, "frame too large")
                raise FrameTooLarge(f"frame of {n} bytes refused")
            if opcode >= 0x8 and (n > 125 or not fin):
                raise WebSocketError("bad control frame")
            return fin, opcode, await self._r.readexactly(n)
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            self._closed = True
            raise Closed() from exc

    async def recv(self) -> str | bytes:
        """The next complete message. Pings are answered, a close is acknowledged and raised as Closed."""
        parts: list[bytes] = []
        kind = None
        while True:
            fin, opcode, payload = await self._frame()
            if opcode == OP_PING:
                await self._send(OP_PONG, payload); continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                reason = payload[2:].decode("utf-8", "replace")
                if not self._close_sent:
                    self._close_sent = True
                    try: await self._send(OP_CLOSE, payload[:2])
                    except Exception: pass
                self._closed = True
                self._w.close()
                raise Closed(code, reason)
            if opcode in (OP_TEXT, OP_BINARY):
                if kind is not None:
                    raise WebSocketError("new message inside a fragmented one")
                kind = opcode
            elif opcode == OP_CONT:
                if kind is None:
                    raise WebSocketError("continuation without a start")
            else:
                raise WebSocketError("unknown opcode")
            parts.append(payload)
            if sum(map(len, parts)) > self._max_message:
                await self.close(1009, "message too large")
                raise FrameTooLarge("message too large")
            if fin:
                data = b"".join(parts)
                if kind == OP_TEXT:
                    try: return data.decode("utf-8")
                    except UnicodeDecodeError as exc: raise WebSocketError("invalid utf-8 in a text message") from exc
                return data

    async def close(self, code: int = 1000, reason: str = "", wait: float = 5.0) -> None:
        """Send close, wait for the peer's close (bounded), then drop the connection."""
        if not self._close_sent:
            self._close_sent = True
            try: await self._send(OP_CLOSE, struct.pack("!H", code) + reason.encode()[:100])
            except Exception: pass
        try:
            while not self._closed:
                fin, opcode, payload = await asyncio.wait_for(self._frame(), wait)
                if opcode == OP_CLOSE:
                    break
        except (asyncio.TimeoutError, WebSocketError, Closed):
            pass
        self._closed = True
        self._w.close()


async def reconnecting(connect: Callable[[], Awaitable[WebSocket]], session: Callable[[WebSocket], Awaitable[None]], *,
                       sleep: Callable[[float], Awaitable] = asyncio.sleep, start: float = 1.0, maximum: float = 60.0,
                       fatal: Callable[[BaseException], bool] = lambda exc: False) -> None:
    """Run `session` on a fresh connection, again and again, with doubling backoff; stop on cancel or a fatal error."""
    delay = start
    while True:
        try:
            ws = await connect()
            delay = start
            try:
                await session(ws)
            finally:
                await ws.close(wait=1.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if fatal(exc):
                raise
            await sleep(delay)
            delay = min(delay * 2, maximum)

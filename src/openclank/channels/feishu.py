"""Feishu/Lark outbound long connection and Open API; not live-service qualified."""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Awaitable, Callable

import httpx

from src.openclank.channels.base import Attachment, Inbound, Limits, OnInbound
from src.openclank.channels.feishu_codec import METHOD_CONTROL, METHOD_DATA, ack_frame, decode_frame, ping_frame, service_id
from src.openclank.channels.ws_client import Closed, WebSocket, reconnecting

log = logging.getLogger(__name__)
DOMAINS = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}
SEEN_EVENTS = 500


class FeishuFatal(Exception):
    pass


class FeishuAdapter:
    name = "feishu"
    limits = Limits(max_text=4000, max_file_bytes=30 * 1024 * 1024, send_gap_seconds=0.3, max_inbound_bytes=10 * 1024 * 1024)

    def __init__(self, app_id: Callable[[], str], app_secret: Callable[[], str], domain: Callable[[], str], client: httpx.AsyncClient, *,
                 connect: Callable = WebSocket.connect, sleep: Callable[[float], Awaitable] = asyncio.sleep, clock: Callable[[], float] | None = None):
        import time
        self._app_id, self._app_secret, self._domain, self._client = app_id, app_secret, domain, client
        self._connect_ws, self._sleep, self._clock = connect, sleep, clock or time.time
        self._tenant: tuple[str, float] | None = None
        self._bot_open_id: str | None = None
        self._seen: list[str] = []
        self._parts: dict[str, tuple[float, int, dict[int, bytes]]] = {}

    def __repr__(self) -> str:
        return "FeishuAdapter()"

    def _base(self) -> str:
        return DOMAINS.get((self._domain() or "feishu").lower(), DOMAINS["feishu"])

    async def _token(self) -> str:
        if self._tenant and self._tenant[1] > self._clock() + 60:
            return self._tenant[0]
        response = await self._client.post(f"{self._base()}/open-apis/auth/v3/tenant_access_token/internal", json={"app_id": self._app_id(), "app_secret": self._app_secret()})
        body = response.json()
        if response.status_code != 200 or body.get("code") != 0:
            raise FeishuFatal("credentials rejected")
        self._tenant = (body["tenant_access_token"], self._clock() + float(body.get("expire", 3600)))
        return self._tenant[0]

    async def _api(self, method: str, path: str, **kw):
        headers = {"Authorization": f"Bearer {await self._token()}"}
        response = await self._client.request(method, f"{self._base()}{path}", headers=headers, timeout=60, **kw)
        if response.status_code == 401:
            self._tenant = None
        response.raise_for_status()
        return response

    # -- inbound
    async def run(self, on_inbound: OnInbound) -> None:
        await reconnecting(self._open, lambda ws: self._session(ws, on_inbound), sleep=self._sleep, fatal=lambda exc: isinstance(exc, FeishuFatal))

    async def _open(self) -> WebSocket:
        if self._bot_open_id is None:
            info = (await self._api("GET", "/open-apis/bot/v3/info")).json()
            self._bot_open_id = str(((info.get("bot") or {}).get("open_id")) or "")
        response = await self._client.post(f"{self._base()}/callback/ws/endpoint", json={"AppID": self._app_id(), "AppSecret": self._app_secret()})
        body = response.json()
        if response.status_code != 200 or body.get("code") != 0 or not (body.get("data") or {}).get("URL"):
            raise FeishuFatal("long connection refused") if response.status_code in (400, 401, 403) else RuntimeError("endpoint unavailable")
        self._ping_interval = float(((body["data"].get("ClientConfig") or {}).get("PingInterval")) or 120)
        self._service = service_id(body["data"]["URL"])
        return await self._connect_ws(body["data"]["URL"])

    async def _session(self, ws: WebSocket, on_inbound: OnInbound) -> None:
        pinger = asyncio.create_task(self._pinger(ws))
        try:
            while True:
                raw = await ws.recv()
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                frame = decode_frame(bytes(raw))
                if frame.method == METHOD_CONTROL:
                    if frame.headers.get("type") == "pong" and frame.payload:
                        try:
                            interval = float(json.loads(frame.payload).get("PingInterval"))
                            if 1 <= interval <= 3600: self._ping_interval = interval
                        except (ValueError, TypeError, AttributeError): pass
                    continue
                if frame.method != METHOD_DATA or frame.headers.get("type") != "event":
                    continue
                payload = self._combine(frame)
                if payload is None: continue
                inbound = self._parse(payload)
                try:
                    if inbound is not None: await on_inbound(inbound)
                except Exception:
                    await ws.send_binary(ack_frame(frame, 500))
                    continue
                if inbound is not None:
                    event_id = str((json.loads(payload).get("header") or {}).get("event_id") or "")
                    if event_id: self._seen.append(event_id); del self._seen[:-SEEN_EVENTS]
                await ws.send_binary(ack_frame(frame))
        finally:
            pinger.cancel()
            await asyncio.gather(pinger, return_exceptions=True)

    async def _pinger(self, ws: WebSocket) -> None:
        while True:
            await self._sleep(self._ping_interval)
            await ws.send_binary(ping_frame(self._service))

    def _combine(self, frame) -> bytes | None:
        """Reassemble SDK sum/seq fragments; incomplete or malformed pages never ack."""
        import time
        now = time.monotonic()
        self._parts = {key:value for key,value in self._parts.items() if now-value[0] < 5}
        try:
            total = int(frame.headers.get("sum", "1")); index = int(frame.headers.get("seq", "0"))
        except (ValueError, TypeError): raise ValueError("invalid fragment headers")
        if not 1 <= total <= 32 or not 0 <= index < total: raise ValueError("invalid fragment bounds")
        if total == 1: return frame.payload
        key = frame.headers.get("message_id")
        if not isinstance(key,str) or not key or len(key)>256: raise ValueError("missing fragment identity")
        if key not in self._parts and len(self._parts) >= 32: raise ValueError("fragment capacity")
        first, expected, pieces = self._parts.get(key, (now,total,{}))
        if expected != total or index in pieces and pieces[index] != frame.payload: raise ValueError("fragment conflict")
        pieces[index] = frame.payload
        if sum(map(len,pieces.values())) > 4 << 20: raise ValueError("fragment payload too large")
        self._parts[key] = (first,total,pieces)
        if len(pieces) != total: return None
        del self._parts[key]
        return b"".join(pieces[i] for i in range(total))

    def _parse(self, payload: bytes) -> Inbound | None:
        try:
            event = json.loads(payload)
        except ValueError:
            return None
        header, body = event.get("header") or {}, event.get("event") or {}
        if header.get("event_type") != "im.message.receive_v1":
            return None
        event_id = str(header.get("event_id") or "")
        if event_id in self._seen:
            return None                                                   # the service redelivers
        message, sender = body.get("message") or {}, body.get("sender") or {}
        sender_id = str((sender.get("sender_id") or {}).get("open_id") or "")
        mtype = message.get("message_type")
        try:
            content = json.loads(message.get("content") or "{}")
        except ValueError:
            return None
        text, attachments = "", ()
        mid = str(message.get("message_id"))
        if mtype == "text":
            text = str(content.get("text") or "")
        elif mtype == "image" and content.get("image_key"):
            attachments = (Attachment("image.png", "image/png", None, self._resource(mid, str(content["image_key"]), "image")),)
        elif mtype == "file" and content.get("file_key"):
            attachments = (Attachment(str(content.get("file_name") or "file"), "application/octet-stream", None, self._resource(mid, str(content["file_key"]), "file")),)
        else:
            return None
        mentions = [str(((m.get("id") or {}).get("open_id"))) for m in message.get("mentions") or []]
        return Inbound(channel="feishu", conv_id=str(message.get("chat_id")), sender_id=sender_id, text=text, message_id=mid, is_direct=message.get("chat_type") == "p2p",
                       sender_is_bot=sender.get("sender_type") not in (None, "user"), mentioned=bool(self._bot_open_id) and self._bot_open_id in mentions, attachments=attachments)

    def _resource(self, message_id: str, key: str, kind: str):
        async def fetch() -> bytes:
            return (await self._api("GET", f"/open-apis/im/v1/messages/{message_id}/resources/{key}", params={"type": kind})).content
        return fetch

    # -- outbound
    async def send_text(self, conv_id: str, thread_id: str | None, text: str) -> None:
        await self._api("POST", "/open-apis/im/v1/messages", params={"receive_id_type": "chat_id"},
                        json={"receive_id": conv_id, "msg_type": "text", "content": json.dumps({"text": text})})

    async def send_file(self, conv_id: str, thread_id: str | None, name: str, data: bytes, mime: str) -> None:
        if mime.startswith("image/"):
            key = (await self._api("POST", "/open-apis/im/v1/images", data={"image_type": "message"}, files={"image": (name, data, mime)})).json()["data"]["image_key"]
            msg_type, content = "image", {"image_key": key}
        else:
            key = (await self._api("POST", "/open-apis/im/v1/files", data={"file_type": "stream", "file_name": name}, files={"file": (name, data, mime)})).json()["data"]["file_key"]
            msg_type, content = "file", {"file_key": key}
        await self._api("POST", "/open-apis/im/v1/messages", params={"receive_id_type": "chat_id"},
                        json={"receive_id": conv_id, "msg_type": msg_type, "content": json.dumps(content)})

    async def aclose(self) -> None:
        await self._client.aclose()

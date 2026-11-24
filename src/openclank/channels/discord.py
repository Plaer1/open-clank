"""Discord: gateway websocket (outbound, no public address) plus REST sends. Rebuilt from the public API; the wire facts here
(opcodes, intents, close codes) are the documented gateway protocol, tested only against a fake gateway, not a real account."""
from __future__ import annotations
import asyncio
import json
import logging
import random
from typing import Awaitable, Callable

import httpx

from src.openclank.channels.base import Attachment, Inbound, Limits, OnInbound
from src.openclank.channels.ws_client import Closed, WebSocket, reconnecting

log = logging.getLogger(__name__)
API = "https://discord.com/api/v10"
# guilds | guild messages | direct messages | message content (a privileged intent the owner enables in the developer portal)
INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)
FATAL_CLOSE = {4004, 4010, 4011, 4012, 4013, 4014}      # bad token, bad shard/intents: reconnecting cannot help


class DiscordFatal(Exception):
    pass


class _Reconnect(Exception):
    pass


class DiscordAdapter:
    name = "discord"
    limits = Limits(max_text=2000, max_file_bytes=10 * 1024 * 1024, send_gap_seconds=0.5)

    def __init__(self, token: Callable[[], str], client: httpx.AsyncClient, *, api: str = API, connect: Callable = WebSocket.connect,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep, hb_sleep: Callable[[float], Awaitable] = asyncio.sleep, jitter: Callable[[], float] = random.random):
        self._token, self._client, self._api, self._connect_ws, self._sleep, self._hb_sleep, self._jitter = token, client, api, connect, sleep, hb_sleep, jitter
        self._gateway: str | None = None
        self._resume_url: str | None = None
        self._session_id: str | None = None
        self._seq: int | None = None
        self._me: str | None = None

    def __repr__(self) -> str:
        return "DiscordAdapter()"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self._token()}"}

    async def run(self, on_inbound: OnInbound) -> None:
        await reconnecting(self._open, lambda ws: self._session(ws, on_inbound), sleep=self._sleep, fatal=lambda exc: isinstance(exc, DiscordFatal))

    async def _open(self) -> WebSocket:
        if self._gateway is None:
            response = await self._client.get(f"{self._api}/gateway/bot", headers=self._headers())
            if response.status_code == 401:
                raise DiscordFatal("token rejected")
            response.raise_for_status()
            self._gateway = response.json()["url"]
        return await self._connect_ws((self._resume_url or self._gateway) + "?v=10&encoding=json")

    async def _session(self, ws: WebSocket, on_inbound: OnInbound) -> None:
        try:
            hello = json.loads(await ws.recv())
        except Closed as exc:
            if exc.code in FATAL_CLOSE: raise DiscordFatal(f"gateway closed {exc.code}") from exc
            if exc.code in (4007, 4009): self._session_id = self._seq = self._resume_url = None
            raise
        if hello.get("op") != 10:
            raise _Reconnect("no hello")
        interval = hello["d"]["heartbeat_interval"] / 1000
        acked = {"ok": True}
        heartbeat = asyncio.create_task(self._heartbeat(ws, interval, acked))
        try:
            if self._session_id and self._seq is not None:
                await ws.send_text(json.dumps({"op": 6, "d": {"token": self._token(), "session_id": self._session_id, "seq": self._seq}}))
            else:
                await ws.send_text(json.dumps({"op": 2, "d": {"token": self._token(), "intents": INTENTS,
                                                              "properties": {"os": "linux", "browser": "open-clank", "device": "open-clank"}}}))
            while True:
                try:
                    event = json.loads(await ws.recv())
                except Closed as exc:
                    if exc.code in FATAL_CLOSE:
                        raise DiscordFatal(f"gateway closed {exc.code}") from exc
                    if exc.code in (4007, 4009):
                        self._session_id = self._seq = self._resume_url = None
                    raise
                op = event.get("op")
                if event.get("s") is not None:
                    self._seq = event["s"]
                if op == 0:
                    await self._dispatch(event, on_inbound)
                elif op == 11:
                    acked["ok"] = True
                elif op == 1:
                    acked["ok"] = False
                    await ws.send_text(json.dumps({"op": 1, "d": self._seq}))
                elif op in (7, 9):
                    if op == 9 and not event.get("d"):
                        self._session_id = self._seq = self._resume_url = None      # not resumable: identify afresh
                    raise _Reconnect("gateway asked to reconnect")
        finally:
            heartbeat.cancel()

    async def _heartbeat(self, ws: WebSocket, interval: float, acked: dict) -> None:
        await self._hb_sleep(interval * self._jitter())
        while True:
            if not acked["ok"]:
                await ws.close(4000, "no heartbeat ack")        # a zombie connection: drop it and resume
                return
            acked["ok"] = False
            await ws.send_text(json.dumps({"op": 1, "d": self._seq}))
            await self._hb_sleep(interval)

    async def _dispatch(self, event: dict, on_inbound: OnInbound) -> None:
        kind, data = event.get("t"), event.get("d") or {}
        if kind == "READY":
            self._me = str((data.get("user") or {}).get("id"))
            self._session_id, self._resume_url = data.get("session_id"), data.get("resume_gateway_url")
        elif kind == "MESSAGE_CREATE":
            inbound = self._parse(data)
            if inbound is not None:
                await on_inbound(inbound)

    def _parse(self, data: dict) -> Inbound | None:
        author = data.get("author") or {}
        if str(author.get("id")) == self._me or data.get("webhook_id"):
            return None                                           # own messages and webhooks
        text = data.get("content") if isinstance(data.get("content"), str) else ""
        attachments = tuple(Attachment(str(a.get("filename") or "file"), str(a.get("content_type") or "application/octet-stream"),
                                       a.get("size") if isinstance(a.get("size"), int) else None, self._downloader(str(a["url"])))
                            for a in data.get("attachments") or [] if isinstance(a, dict) and str(a.get("url", "")).startswith("https://"))
        if not text and not attachments:
            return None
        mentioned = any(str(m.get("id")) == self._me for m in data.get("mentions") or []) or \
            str(((data.get("referenced_message") or {}).get("author") or {}).get("id")) == self._me
        return Inbound(channel="discord", conv_id=str(data.get("channel_id")), sender_id=str(author.get("id")), text=text, message_id=str(data.get("id")),
                       is_direct=data.get("guild_id") is None, sender_label=str(author.get("global_name") or author.get("username") or ""),
                       sender_is_bot=bool(author.get("bot")), mentioned=mentioned, attachments=attachments)

    def _downloader(self, url: str):
        async def fetch() -> bytes:
            response = await self._client.get(url, timeout=60)
            response.raise_for_status()
            return response.content
        return fetch

    async def _post(self, path: str, **kw) -> None:
        for attempt in range(2):
            response = await self._client.post(f"{self._api}{path}", headers=self._headers(), timeout=60, **kw)
            if response.status_code == 429 and attempt == 0:
                await self._sleep(min(float(response.json().get("retry_after", 1)), 30))
                continue
            response.raise_for_status()
            return

    async def send_text(self, conv_id: str, thread_id: str | None, text: str) -> None:
        await self._post(f"/channels/{conv_id}/messages", json={"content": text, "allowed_mentions": {"parse": []}})

    async def send_file(self, conv_id: str, thread_id: str | None, name: str, data: bytes, mime: str) -> None:
        await self._post(f"/channels/{conv_id}/messages", data={"payload_json": json.dumps({"allowed_mentions": {"parse": []}})}, files={"files[0]": (name, data, mime)})

    async def aclose(self) -> None:
        await self._client.aclose()

"""Matrix client-server API, unencrypted rooms only: /sync long poll over httpx. Rebuilt from the documented API.

An encrypted room produces one Inbound with encrypted=True (the bridge answers once, then stays silent). Invites are
never accepted: joining a room is the owner's act."""
from __future__ import annotations
import asyncio
import logging
import uuid
from typing import Awaitable, Callable
from urllib.parse import quote

import httpx

from src.openclank.channels.base import Attachment, Inbound, Limits, OnInbound

log = logging.getLogger(__name__)
BACKOFF_START, BACKOFF_MAX = 1.0, 60.0


class MatrixAdapter:
    name = "matrix"
    limits = Limits(max_text=20000, max_file_bytes=50 * 1024 * 1024, send_gap_seconds=0.5)

    def __init__(self, homeserver: Callable[[], str], token: Callable[[], str], client: httpx.AsyncClient, *,
                 sync_timeout_ms: int = 30000, sleep: Callable[[float], Awaitable] = asyncio.sleep):
        self._homeserver, self._token, self._client, self._timeout, self._sleep = homeserver, token, client, sync_timeout_ms, sleep
        self._since: str | None = None
        self._me: str | None = None

    def __repr__(self) -> str:
        return "MatrixAdapter()"

    def _url(self, path: str) -> str:
        return self._homeserver().rstrip("/") + path

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    async def _request(self, method: str, path: str, **kw) -> dict:
        response = await self._client.request(method, self._url(path), headers=self._headers(), timeout=self._timeout / 1000 + 20, **kw)
        if response.status_code != 200:
            retry = None
            try:
                retry = response.json().get("retry_after_ms")
            except ValueError:
                pass
            raise _ApiError(response.status_code, retry)
        return response.json()

    async def run(self, on_inbound: OnInbound) -> None:
        backoff = BACKOFF_START
        while True:
            try:
                if self._me is None:
                    self._me = (await self._request("GET", "/_matrix/client/v3/account/whoami"))["user_id"]
                params = {"timeout": self._timeout if self._since else 0}
                if self._since:
                    params["since"] = self._since
                body = await self._request("GET", "/_matrix/client/v3/sync", params=params)
                backoff = BACKOFF_START
                first = self._since is None
                for inbound in ([] if first else self._parse(body)):     # the first sync only sets the position: no old history
                    await on_inbound(inbound)
                self._since = body["next_batch"]                         # advanced only after the hand-over returned
            except asyncio.CancelledError:
                raise
            except _ApiError as exc:
                delay = (exc.retry_after / 1000) if exc.retry_after else backoff
                log.warning("matrix sync error: http %s", exc.status)
                await self._sleep(min(delay, BACKOFF_MAX)); backoff = min(backoff * 2, BACKOFF_MAX)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                log.warning("matrix sync error: %s", type(exc).__name__)
                await self._sleep(backoff); backoff = min(backoff * 2, BACKOFF_MAX)

    def _parse(self, body: dict) -> list[Inbound]:
        out: list[Inbound] = []
        for room_id, room in ((body.get("rooms") or {}).get("join") or {}).items():
            summary = room.get("summary") or {}
            direct = summary.get("m.joined_member_count") == 2
            for event in (room.get("timeline") or {}).get("events", []):
                sender = str(event.get("sender") or "")
                if sender == self._me:
                    continue                                              # own messages
                content = event.get("content") or {}
                if event.get("type") == "m.room.encrypted":
                    out.append(Inbound("matrix", room_id, sender, "", str(event.get("event_id")), direct, encrypted=True))
                elif event.get("type") == "m.room.message" and content.get("msgtype") in ("m.image", "m.file") and str(content.get("url") or "").startswith("mxc://"):
                    info = content.get("info") or {}
                    out.append(Inbound("matrix", room_id, sender, "", str(event.get("event_id")), direct, sender_label=sender,
                                       mentioned=bool(self._me) and self._me in (content.get("m.mentions") or {}).get("user_ids", []),
                                       attachments=(Attachment(str(content.get("body") or "file"), str(info.get("mimetype") or "application/octet-stream"),
                                                               info.get("size") if isinstance(info.get("size"), int) else None, self._downloader(content["url"])),)))
                elif event.get("type") == "m.room.message" and content.get("msgtype") in ("m.text", "m.notice") and isinstance(content.get("body"), str):
                    if content.get("msgtype") == "m.notice":
                        continue                                          # notices are bot output by convention
                    text = content["body"]
                    mentions = (content.get("m.mentions") or {}).get("user_ids") or []
                    mentioned = bool(self._me) and (self._me in mentions or self._me in text)
                    out.append(Inbound("matrix", room_id, sender, text, str(event.get("event_id")), direct, sender_label=sender, mentioned=mentioned))
        return out

    def _downloader(self, mxc: str):
        async def fetch() -> bytes:
            server, _, media = mxc[len("mxc://"):].partition("/")
            response = await self._client.get(self._url(f"/_matrix/media/v3/download/{quote(server, safe='')}/{quote(media, safe='')}"), headers=self._headers(), timeout=60)
            if response.status_code != 200:
                raise _ApiError(response.status_code, None)
            return response.content
        return fetch

    async def send_text(self, conv_id: str, thread_id: str | None, text: str) -> None:
        await self._request("PUT", f"/_matrix/client/v3/rooms/{quote(conv_id, safe='')}/send/m.room.message/{uuid.uuid4().hex}",
                            json={"msgtype": "m.text", "body": text})

    async def send_file(self, conv_id: str, thread_id: str | None, name: str, data: bytes, mime: str) -> None:
        response = await self._client.post(self._url("/_matrix/media/v3/upload"), headers={**self._headers(), "Content-Type": mime},
                                           params={"filename": name}, content=data, timeout=60)
        if response.status_code != 200:
            raise _ApiError(response.status_code, None)
        await self._request("PUT", f"/_matrix/client/v3/rooms/{quote(conv_id, safe='')}/send/m.room.message/{uuid.uuid4().hex}",
                            json={"msgtype": "m.image" if mime.startswith("image/") else "m.file", "body": name, "url": response.json()["content_uri"],
                                  "info": {"mimetype": mime, "size": len(data)}})

    async def aclose(self) -> None:
        await self._client.aclose()


class _ApiError(Exception):
    def __init__(self, status: int, retry_after):
        super().__init__(f"matrix api {status}")
        self.status, self.retry_after = status, retry_after

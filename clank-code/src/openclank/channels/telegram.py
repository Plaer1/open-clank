"""Telegram over the Bot API: long polling with httpx (no webhook, no public address). Rebuilt from the documented API."""
from __future__ import annotations
import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from src.openclank.channels.base import Attachment, Inbound, Limits, OnInbound

log = logging.getLogger(__name__)
API = "https://api.telegram.org"
BACKOFF_START, BACKOFF_MAX = 1.0, 60.0


class TelegramAdapter:
    name = "telegram"
    limits = Limits(max_text=4096, max_file_bytes=50 * 1024 * 1024, send_gap_seconds=1.0)

    def __init__(self, token: Callable[[], str], client: httpx.AsyncClient, *, base: str = API,
                 poll_timeout: int = 30, sleep: Callable[[float], Awaitable] = asyncio.sleep):
        self._token, self._client, self._base, self._poll_timeout, self._sleep = token, client, base, poll_timeout, sleep
        self._offset: int | None = None
        self._me: dict | None = None

    def __repr__(self) -> str:             # never carries the token
        return "TelegramAdapter()"

    def _url(self, method: str) -> str:
        return f"{self._base}/bot{self._token()}/{method}"

    async def _call(self, method: str, **kw) -> dict:
        response = await self._client.post(self._url(method), timeout=self._poll_timeout + 15, **kw)
        body = response.json()
        if response.status_code != 200 or not body.get("ok"):
            raise _ApiError(response.status_code, (body.get("parameters") or {}).get("retry_after"))
        return body["result"]

    async def run(self, on_inbound: OnInbound) -> None:
        backoff = BACKOFF_START
        while True:
            try:
                if self._me is None:
                    self._me = await self._call("getMe")
                updates = await self._call("getUpdates", data={"timeout": self._poll_timeout, **({"offset": self._offset} if self._offset is not None else {})})
                backoff = BACKOFF_START
                for update in updates:
                    inbound = self._parse(update)
                    if inbound is not None:
                        await on_inbound(inbound)
                    self._offset = int(update["update_id"]) + 1       # acknowledged only after the hand-over returned
            except asyncio.CancelledError:
                raise
            except _ApiError as exc:
                delay = float(exc.retry_after) if exc.retry_after else (BACKOFF_MAX if exc.status == 409 else backoff)
                log.warning("telegram poll error: http %s", exc.status)
                await self._sleep(min(delay, BACKOFF_MAX)); backoff = min(backoff * 2, BACKOFF_MAX)
            except (httpx.HTTPError, ValueError, KeyError) as exc:      # class name only: the URL holds the token
                log.warning("telegram poll error: %s", type(exc).__name__)
                await self._sleep(backoff); backoff = min(backoff * 2, BACKOFF_MAX)

    def _parse(self, update: dict) -> Inbound | None:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        text = message.get("text") if isinstance(message.get("text"), str) else (message.get("caption") if isinstance(message.get("caption"), str) else "")
        attachments = self._attachments(message)
        if not text and not attachments:
            return None
        chat, sender = message.get("chat") or {}, message.get("from") or {}
        username = str((self._me or {}).get("username") or "")
        mentioned = bool(username) and f"@{username}".lower() in text.lower()
        reply = message.get("reply_to_message") or {}
        mentioned = mentioned or (reply.get("from") or {}).get("id") == (self._me or {}).get("id")
        return Inbound(channel="telegram", conv_id=str(chat.get("id")), sender_id=str(sender.get("id")), text=text,
                       message_id=str(message.get("message_id")), is_direct=chat.get("type") == "private",
                       thread_id=str(message["message_thread_id"]) if message.get("is_topic_message") else None,
                       sender_label=str(sender.get("username") or sender.get("first_name") or ""),
                       sender_is_bot=bool(sender.get("is_bot")), mentioned=mentioned, attachments=attachments)

    def _attachments(self, message: dict) -> tuple:
        out = []
        photos = message.get("photo")
        if isinstance(photos, list) and photos and isinstance(photos[-1], dict) and photos[-1].get("file_id"):
            best = photos[-1]                                                  # the largest size
            out.append(Attachment(f"photo-{best.get('file_unique_id', 'x')}.jpg", "image/jpeg", best.get("file_size"), self._downloader(best["file_id"])))
        doc = message.get("document")
        if isinstance(doc, dict) and doc.get("file_id"):
            out.append(Attachment(str(doc.get("file_name") or "document"), str(doc.get("mime_type") or "application/octet-stream"), doc.get("file_size"), self._downloader(doc["file_id"])))
        return tuple(out)

    def _downloader(self, file_id: str):
        async def fetch() -> bytes:
            info = await self._call("getFile", data={"file_id": file_id})
            response = await self._client.get(f"{self._base}/file/bot{self._token()}/{info['file_path']}", timeout=60)
            if response.status_code != 200:
                raise _ApiError(response.status_code, None)
            return response.content
        return fetch

    async def send_text(self, conv_id: str, thread_id: str | None, text: str) -> None:
        await self._call("sendMessage", data={"chat_id": conv_id, "text": text, **({"message_thread_id": thread_id} if thread_id else {})})

    async def send_file(self, conv_id: str, thread_id: str | None, name: str, data: bytes, mime: str) -> None:
        await self._call("sendDocument", data={"chat_id": conv_id, **({"message_thread_id": thread_id} if thread_id else {})},
                         files={"document": (name, data, mime)})

    async def aclose(self) -> None:
        await self._client.aclose()


class _ApiError(Exception):
    def __init__(self, status: int, retry_after):
        super().__init__(f"telegram api {status}")
        self.status, self.retry_after = status, retry_after

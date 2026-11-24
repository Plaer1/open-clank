"""Inbound message to an owner session and the reply back out.

The turn goes through the existing chat route by an in-process HTTP call with the owner's path-confined ``channel`` API
token, so every middleware check, admission barrier and memory rule applies unchanged. Inbound text is untrusted input
for the model; it is sent as the user's message and never interpreted by the Host."""
from __future__ import annotations
import asyncio
import json
import logging
import mimetypes
import posixpath
import re
import time
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx

from src.openclank.channels.access import Access
from src.openclank.channels.base import ChannelAdapter, Inbound
from src.openclank.channels.chunking import split_text

log = logging.getLogger(__name__)
APPROVAL_WAIT_SECONDS = 120
GENERATED_IMAGE = re.compile(r"^/api/generated-image/[A-Za-z0-9._-]{1,200}$")   # the one read path a channel token may use for files
ALLOWED_ATTACHMENTS = {"image/png", "image/jpeg", "image/webp", "image/gif", "text/plain", "text/markdown", "application/pdf"}
MAX_ATTACHMENTS = 4
SEEN_MESSAGES = 50
_YES = {"yes", "y", "approve", "ok", "allow"}


def _sniff_channel_attachment(data: bytes):
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png', '.png'
    if data.startswith(b'\xff\xd8\xff'): return 'image/jpeg', '.jpg'
    if data.startswith((b'GIF87a',b'GIF89a')): return 'image/gif', '.gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP': return 'image/webp', '.webp'
    if data.startswith(b'%PDF-'): return 'application/pdf', '.pdf'
    try:
        text=data.decode('utf-8')
        if text and '\x00' not in text: return 'text/plain', '.txt'
    except UnicodeDecodeError: pass
    return None, ''


class ChannelError(RuntimeError):
    pass


class ModelNotChosen(ChannelError):
    pass


class ChannelBridge:
    def __init__(self, store, access: Access, client: httpx.AsyncClient, *, sleep: Callable[[float], Awaitable] = asyncio.sleep):
        self.store, self.access, self.client, self._sleep = store, access, client, sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._seen: dict[str, list[str]] = {}
        self._approvals: dict[str, tuple[str, asyncio.Future]] = {}

    @staticmethod
    def key(inbound: Inbound) -> str:
        return f"{inbound.channel}|{inbound.conv_id}|{inbound.thread_id or ''}"

    async def handle(self, adapter: ChannelAdapter, inbound: Inbound) -> None:
        owner = self.store.owner()
        if not owner or not self.store.enabled(inbound.channel): return
        decision = self.access.decide(inbound)
        send = lambda text: adapter.send_text(inbound.conv_id, inbound.thread_id, text)
        if decision.kind == "ignore":
            return
        if decision.kind == "pair":
            await send(f"Pairing code {decision.code}. Ask the owner to approve it in Open Clank; it is valid for one hour.")
            return
        if decision.kind == "encrypted_notice":
            await send("Encrypted rooms are not supported. I will not reply here.")
            return
        key = self.key(inbound)
        waiting = self._approvals.get(key)
        if waiting and waiting[0] == inbound.sender_id and not waiting[1].done():
            waiting[1].set_result(inbound.text.strip().lower() in _YES)       # the answer to a permission question
            return
        seen = self._seen.setdefault(key, [])
        if inbound.message_id in seen:
            return
        seen.append(inbound.message_id); del seen[:-SEEN_MESSAGES]
        async with self._locks.setdefault(key, asyncio.Lock()):
            if not self._current(inbound, owner): return
            try:
                await self._turn(adapter, inbound, key, owner)
            except Exception as exc:                  # class name only: error text may carry a URL with a token
                log.warning("channel turn failed: %s", type(exc).__name__)
                try:
                    await send("Choose a model for this channel in the Open Clank settings first." if isinstance(exc, ModelNotChosen)
                               else "Something went wrong with that message. Check Open Clank.")
                except Exception as inner:
                    log.warning("channel notice failed: %s", type(inner).__name__)

    def _current(self, inbound: Inbound, owner: str) -> bool:
        if self.store.owner() != owner or not self.store.enabled(inbound.channel) or inbound.sender_is_bot or inbound.encrypted:
            return False
        if inbound.is_direct: return inbound.sender_id in self.store.senders(inbound.channel)
        return inbound.conv_id in self.store.groups(inbound.channel) and inbound.mentioned

    # -- session
    async def _session(self, inbound: Inbound, key: str) -> str:
        existing = self.store.binding(key)
        if existing:
            return existing
        label = (inbound.sender_label or inbound.conv_id)[:40]
        endpoint, model = self.store.setting(inbound.channel, "endpoint_id"), self.store.setting(inbound.channel, "model")
        if not endpoint or not model:
            raise ModelNotChosen("the real session route needs an authorized provider route")      # found by the real-route test
        response = await self.client.post("/api/session", data={"name": f"{inbound.channel.title()} · {label}", "incognito": "false",
                                                                "endpoint_id": endpoint, "model": model})
        response.raise_for_status()
        body = response.json()
        if body.get("incognito") is True or not isinstance(body.get("id"), str):
            raise ChannelError("a channel never binds to an incognito session")
        self.store.bind(key, body["id"])
        return body["id"]

    # -- one turn
    async def _turn(self, adapter: ChannelAdapter, inbound: Inbound, key: str, owner: str) -> None:
        session = await self._session(inbound, key)
        form = {"message": inbound.text, "session": session, "allow_bash": "false"}
        ids = await self._attachments(adapter, inbound, session, owner)
        if ids:
            form["attachments"] = json.dumps(ids)
            if not form["message"].strip():
                form["message"] = "(sent an attachment)"
        if not form["message"].strip() and not ids:
            return
        if not inbound.is_direct:
            form["no_memory"] = "true"                 # other people's words are not the owner's memories
        if not self._current(inbound, owner): raise ChannelError("channel authorization changed")
        deltas: list[str] = []
        images: list[str] = []
        async with self.client.stream("POST", "/api/chat_stream", data=form, timeout=None) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    event = json.loads(line[6:])
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                if kind in ("permission_request", "ask_user"):
                    await self._permission(adapter, inbound, key, session, event)
                elif kind == "generated_image" and isinstance(event.get("url"), str):
                    images.append(event["url"])
                elif isinstance(event.get("delta"), str) and not event.get("thinking"):
                    deltas.append(event["delta"])
        text = "".join(deltas).strip() or "(no reply)"
        limits = adapter.limits
        for index, chunk in enumerate(split_text(text, limits.max_text)):
            if index and limits.send_gap_seconds:
                await self._sleep(limits.send_gap_seconds)
            await adapter.send_text(inbound.conv_id, inbound.thread_id, chunk)
        for url in images[:4]:
            await self._send_generated(adapter, inbound, url)

    async def _attachments(self, adapter: ChannelAdapter, inbound: Inbound, session: str, owner: str) -> list[str]:
        """Photos and documents go through the existing upload route (its size, type and image limits and the media input
        policy apply there). Off unless the channel's setting allows it; images and plain documents only."""
        if not inbound.attachments:
            return []
        if self.store.setting(inbound.channel, "attachments_in") != "true":
            await adapter.send_text(inbound.conv_id, inbound.thread_id, "Attachments are turned off for this channel; I only read the text.")
            return []
        ids: list[str] = []
        cap = adapter.limits.max_inbound_bytes
        for att in inbound.attachments[:MAX_ATTACHMENTS]:
            mime = (att.mime or "").split(";")[0].strip().lower()
            wecom_cipher = inbound.channel == 'wecom' and mime == 'application/x-wecom-encrypted'
            if (mime not in ALLOWED_ATTACHMENTS and not wecom_cipher) or (att.size is not None and att.size > cap):
                await adapter.send_text(inbound.conv_id, inbound.thread_id, f"I skipped {att.name[:60]}: only images and plain documents under {cap // (1024 * 1024)} MB are accepted.")
                continue
            if not self._current(inbound, owner) or self.store.setting(inbound.channel, "attachments_in") != "true":
                raise ChannelError("channel attachment authorization changed")
            data = await att.fetch()
            if not self._current(inbound, owner) or self.store.setting(inbound.channel, "attachments_in") != "true":
                raise ChannelError("channel attachment authorization changed")
            if wecom_cipher:
                mime, suffix = _sniff_channel_attachment(data)
                if mime is None:
                    await adapter.send_text(inbound.conv_id, inbound.thread_id, 'I skipped an unsupported attachment.')
                    continue
                upload_name = 'attachment' + suffix
            else:
                upload_name = posixpath.basename(att.name) or 'file'
            if len(data) > cap:
                await adapter.send_text(inbound.conv_id, inbound.thread_id, f"I skipped {att.name[:60]}: it is too large.")
                continue
            response = await self.client.post("/api/upload", files={"files": (upload_name, data, mime)}, data={"session_id": session})
            response.raise_for_status()
            ids += [f["id"] for f in response.json().get("files", []) if isinstance(f.get("id"), str)]
        return ids

    async def _permission(self, adapter, inbound, key, session, event) -> None:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        request_id = data.get("request_id")
        if event.get("type") == "ask_user" or not isinstance(request_id, str):
            await adapter.send_text(inbound.conv_id, inbound.thread_id, "That needs an answer in the Open Clank app.")
            return
        option = "reject"
        options = data.get("options") if isinstance(data.get("options"), list) else []
        if self.access.approvals_over_chat(inbound) and any((o.get("id") if isinstance(o, dict) else o) == "once" for o in options):
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._approvals[key] = (inbound.sender_id, future)
            try:
                detail = str(data.get("detail") or data.get("permission_type") or "an action")[:300]
                await adapter.send_text(inbound.conv_id, inbound.thread_id, f"Allow once: {detail}? Reply yes to allow; anything else rejects.")
                option = "once" if await asyncio.wait_for(future, APPROVAL_WAIT_SECONDS) else "reject"
            except asyncio.TimeoutError:
                option = "reject"
            finally:
                self._approvals.pop(key, None)
        else:
            await adapter.send_text(inbound.conv_id, inbound.thread_id, "That needs approval in the Open Clank app, so I rejected it.")
        response = await self.client.post(f"/api/session/{session}/permission", data={"request_id": request_id, "option_id": option})
        response.raise_for_status()

    async def _send_generated(self, adapter, inbound, url: str) -> None:
        path = urlparse(url).path
        if not GENERATED_IMAGE.match(url):
            return                                        # only the generated-image read path; never a URL a model chose
        response = await self.client.get(url)
        if response.status_code != 200 or len(response.content) > adapter.limits.max_file_bytes:
            await adapter.send_text(inbound.conv_id, inbound.thread_id, "A generated file is available in the Open Clank app.")
            return
        name = posixpath.basename(path) or "file"
        mime = response.headers.get("content-type", "").split(";")[0] or mimetypes.guess_type(name)[0] or "application/octet-stream"
        await adapter.send_file(inbound.conv_id, inbound.thread_id, name, response.content, mime)

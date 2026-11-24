"""The contract a channel adapter implements, in Open Clank's terms."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol


@dataclass(frozen=True)
class Limits:
    max_text: int                  # characters per outbound text message
    max_file_bytes: int            # outbound file size cap
    send_gap_seconds: float = 0.0  # polite gap between consecutive sends to one conversation
    max_inbound_bytes: int = 10 * 1024 * 1024   # largest attachment the layer will download


@dataclass(frozen=True)
class Attachment:
    """An inbound file, not yet downloaded. `fetch` downloads it on demand; size/mime are the service's claims (untrusted)."""
    name: str
    mime: str
    size: int | None
    fetch: Callable[[], Awaitable[bytes]]


@dataclass(frozen=True)
class Inbound:
    """One message from a service. Every field is untrusted input: text is data for the model, never a Host instruction."""
    channel: str
    conv_id: str
    sender_id: str
    text: str
    message_id: str
    is_direct: bool
    thread_id: str | None = None
    sender_label: str = ""
    sender_is_bot: bool = False
    mentioned: bool = False
    encrypted: bool = False
    attachments: tuple = ()


OnInbound = Callable[[Inbound], Awaitable[None]]


class ChannelAdapter(Protocol):
    name: str
    limits: Limits

    async def run(self, on_inbound: OnInbound) -> None:
        """Own the inbound loop (poll or websocket), reconnect with backoff, until cancelled. Acknowledge a message
        to the service only after on_inbound returned for it."""

    async def send_text(self, conv_id: str, thread_id: str | None, text: str) -> None: ...

    async def send_file(self, conv_id: str, thread_id: str | None, name: str, data: bytes, mime: str) -> None: ...

    async def aclose(self) -> None: ...

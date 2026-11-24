"""Who may talk to the bot: pairing, allow-lists, group rules, bot-sender and flood guard. Pure decisions over the store."""
from __future__ import annotations
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable

from src.openclank.channels.base import Inbound

PAIR_TTL = 3600           # a pairing code is valid for one hour
MAX_PENDING = 3           # pending pairing requests per channel
WINDOW_EVENTS, WINDOW_SECONDS, COOLDOWN_SECONDS = 20, 60, 60
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # no 0 O 1 I


@dataclass(frozen=True)
class Decision:
    kind: str                  # process | pair | encrypted_notice | ignore
    reason: str = ""
    code: str | None = None


class Access:
    def __init__(self, store, clock: Callable[[], float] = time.time, code_factory: Callable[[], str] | None = None):
        self.store, self._clock = store, clock
        self._code = code_factory or (lambda: "".join(secrets.choice(_ALPHABET) for _ in range(8)))
        self._events: dict[tuple, deque] = defaultdict(deque)
        self._cooldown: dict[tuple, float] = {}

    def _flooding(self, inbound: Inbound) -> bool:
        key = (inbound.channel, inbound.conv_id, inbound.sender_id)
        now = self._clock()
        if self._cooldown.get(key, 0) > now:
            return True
        window = self._events[key]
        window.append(now)
        while window and window[0] <= now - WINDOW_SECONDS:
            window.popleft()
        if len(window) > WINDOW_EVENTS:
            self._cooldown[key] = now + COOLDOWN_SECONDS
            window.clear()
            return True
        return False

    def decide(self, inbound: Inbound) -> Decision:
        name = inbound.channel
        if inbound.sender_is_bot:
            return Decision("ignore", "bot_sender")            # other bots are never answered
        if self._flooding(inbound):
            return Decision("ignore", "rate_guard")
        if inbound.encrypted:
            key = f"{name}|{inbound.conv_id}|encrypted"
            if self.store.notified(key):
                return Decision("ignore", "encrypted_silent")
            self.store.mark_notified(key)
            return Decision("encrypted_notice")
        if inbound.is_direct:
            if inbound.sender_id in self.store.senders(name):
                return Decision("process")
            pending = self.store.pending(name)
            if any(p["sender"] == inbound.sender_id for p in pending):
                return Decision("ignore", "pairing_pending")   # the code was already sent once
            if len(pending) >= MAX_PENDING:
                return Decision("ignore", "pairing_full")
            code = self._code()
            self.store.add_pending(name, inbound.sender_id, inbound.sender_label or inbound.sender_id, code, self._clock() + PAIR_TTL)
            return Decision("pair", code=code)
        # group: off until the owner allow-lists it, and the bot must be mentioned
        if inbound.conv_id not in self.store.groups(name):
            return Decision("ignore", "group_not_allowed")
        if not inbound.mentioned:
            return Decision("ignore", "no_mention")
        return Decision("process")

    def approvals_over_chat(self, inbound: Inbound) -> bool:
        """Only the one named paired identity, only in a direct chat."""
        return inbound.is_direct and self.store.approve_in_chat(inbound.channel) == inbound.sender_id

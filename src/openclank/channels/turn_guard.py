"""Private channel credential fence; never a human/tool authority."""
from dataclasses import dataclass
from typing import Callable

@dataclass(frozen=True)
class ChannelTurnGuard:
    owner: str
    account: str
    credential_current: Callable[[], bool]

    def assert_current(self):
        from src.settings import load_settings
        if load_settings().get('channels_enabled') is not True or self.credential_current() is not True:
            raise PermissionError('Channel credential is no longer current')

    def prepare(self, session, message):
        self.assert_current()
        if session is None or session.owner != self.owner or message.role != 'user':
            raise PermissionError('Channel session binding changed')

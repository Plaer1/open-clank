"""The owner's path-confined ``channel`` API token (kind ``channel``, prefix ``occ_``, scope ``chat``)."""
from __future__ import annotations
import re
import secrets
import uuid
from typing import Callable

CHANNEL_PREFIX = "occ_"
CHANNEL_KIND = "channel"
# Exactly what the bridge needs (method, path pattern); everything else answers 403.
CHANNEL_ROUTES = (
    ("POST", re.compile(r"^/api/chat_stream$")),                         # the chat turn
    ("POST", re.compile(r"^/api/session$")),                             # create the session
    ("POST", re.compile(r"^/api/session/[A-Za-z0-9_-]{1,128}/permission$")),   # reply to a permission prompt
    ("POST", re.compile(r"^/api/upload$")),                              # attachments in (photos, documents)
    ("GET", re.compile(r"^/api/generated-image/[A-Za-z0-9._-]{1,200}$")),      # a generated image, owner-scoped by the route itself
)


def path_allowed(path: str, method: str = "POST") -> bool:
    method = method.upper()
    return any(m == method and pattern.match(path) for m, pattern in CHANNEL_ROUTES)


def revoke(owner: str, invalidate: Callable[[], None] | None = None) -> None:
    """Deactivate every channel token of this owner (disabling the channels does this)."""
    from datetime import datetime
    from core.database import ApiToken, get_db_session
    with get_db_session() as db:
        for row in db.query(ApiToken).filter(ApiToken.owner == owner, ApiToken.client_kind == CHANNEL_KIND, ApiToken.revoked_at.is_(None)).all():
            row.is_active = False
            row.revoked_at = datetime.utcnow()
        db.commit()
    if invalidate:
        invalidate()


def provision(owner: str, invalidate: Callable[[], None] | None = None) -> str:
    """Revoke earlier channel tokens of this owner and mint a new one. The raw value is returned once."""
    import bcrypt
    from datetime import datetime
    from core.database import ApiToken, get_db_session
    raw = CHANNEL_PREFIX + secrets.token_urlsafe(32)
    with get_db_session() as db:
        for row in db.query(ApiToken).filter(ApiToken.owner == owner, ApiToken.client_kind == CHANNEL_KIND, ApiToken.revoked_at.is_(None)).all():
            row.is_active = False
            row.revoked_at = datetime.utcnow()
        db.add(ApiToken(id=uuid.uuid4().hex, owner=owner, name="Chat channels", token_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode(),
                        token_prefix=raw[:8], scopes="chat", client_kind=CHANNEL_KIND))
        db.commit()
    if invalidate:
        invalidate()
    return raw

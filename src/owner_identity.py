"""Owner/identity predicates for auth-disabled and domain-adapter scopes.

Open Clank keeps named account identity authoritative (O01). This module
centralizes the *existing* domain adapters for the unnamed scope so call sites
stop re-deriving them inconsistently. It never migrates stored rows and never
creates synthetic human users named ``Default`` or ``Local``.

Astra mapping shape (host/runtime audit 08):

- auth-disabled / first-run unnamed scope: ``require_user`` returns ``""``
- provider routing maps that scope to ``local-installation``
- Copal maps that scope to ``local``
- a delegated API token attributes data to its owner but never inherits that
  owner's interactive admin or dangerous-tool authority
- explicit ``AUTH_ENABLED=false`` and unconfigured first-run loopback are
  different cases and must stay distinguishable
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

# Existing domain identities. These are adapter scopes, not user accounts.
LOCAL_INSTALLATION_OWNER = "local-installation"
COPAL_LOCAL_OWNER = "local"

# Never allowed as a stored human identity. Mirrors core.auth reserved names
# plus the upstream Default/Local migration targets we are explicitly not
# adopting (O01).
FORBIDDEN_STORED_IDENTITIES = frozenset(
    {
        "default",
        "local",
        "__odysseus_local__",
        "internal-tool",
        "api",
        "demo",
        "system",
        LOCAL_INSTALLATION_OWNER,
    }
)
FORBIDDEN_STORED_PREFIXES = ("user:", "deleted:", "agent:")


def auth_disabled() -> bool:
    """True when the operator explicitly disabled auth via ``AUTH_ENABLED``."""
    return os.getenv("AUTH_ENABLED", "true").lower() == "false"


def localhost_bypass_enabled() -> bool:
    """True when the documented loopback dev bypass is on."""
    return os.getenv("LOCALHOST_BYPASS", "false").lower() == "true"


def is_unnamed_scope(user: Optional[str]) -> bool:
    """True for the empty auth-disabled / first-run identity."""
    return not str(user or "").strip()


def provider_owner_for(user: Optional[str]) -> str:
    """Map a resolved request identity onto the provider/account owner key.

    Named owners keep their own key. The unnamed scope resolves to the
    existing ``local-installation`` installation partition — never to a
    synthetic ``Default``/``Local`` user.
    """
    key = str(user or "").strip().lower()
    return key if key else LOCAL_INSTALLATION_OWNER


def copal_owner_for(user: Optional[str]) -> str:
    """Map a resolved request identity onto the Copal owner key."""
    key = str(user or "").strip().lower()
    return key if key else COPAL_LOCAL_OWNER


def actor_for(user: Optional[str], *, lane: str = "human") -> str:
    """Actor id for history/Lore binding. Agent lanes are prefixed."""
    key = str(user or "").strip().lower() or LOCAL_INSTALLATION_OWNER
    if lane == "agent":
        return f"agent:{key}"
    return key


def account_id_for(user_record: Mapping[str, Any] | None, user: Optional[str] = None) -> str:
    """Durable account id for a named owner record, else the installation key."""
    if isinstance(user_record, Mapping):
        account = str(user_record.get("account_id") or "").strip()
        if account:
            return account
    key = str(user or "").strip().lower()
    return key if key else LOCAL_INSTALLATION_OWNER


def is_reserved_stored_identity(username: Optional[str]) -> bool:
    """True when a name must never become a stored human account identity."""
    key = str(username or "").strip().lower()
    if not key:
        return False
    return key in FORBIDDEN_STORED_IDENTITIES or key.startswith(FORBIDDEN_STORED_PREFIXES)


def token_implies_admin(owner: Optional[str]) -> bool:
    """A delegated token's attributed owner is never administrator authority.

    Scoped API tokens may act inside their owner's data scope where a route
    explicitly permits it. They must never be treated as the owner's
    interactive admin or dangerous-tool session (O01).
    """
    return False


def is_internal_tool_identity(user: Optional[str]) -> bool:
    return str(user or "").strip() == "internal-tool"


def is_api_pseudo_user(user: Optional[str]) -> bool:
    """Bearer-token pseudo-user; must not wander into cookie/user routes."""
    return str(user or "").strip() == "api"

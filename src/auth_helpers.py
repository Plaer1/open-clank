"""Shared auth helpers used by all route files."""

import os
from typing import Optional
from fastapi import Request, HTTPException
from core.middleware import INTERNAL_TOOL_OWNER_HEADER
from src.owner_identity import (
    copal_owner_for as _owner_identity_copal_owner,
    is_internal_tool_identity,
)


_COPAL_RESERVED_OWNERS = frozenset(
    {
        "shared",
        "local",
        "__copal_unclaimed_owner__",
        "__copal_unclaimed_workspace__",
    }
)
_COPAL_RESERVED_OWNER_PREFIXES = ("user:", "deleted:")


def copal_owner_for_user(username: Optional[str]) -> str:
    """Resolve a human username without colliding with Copal sentinels.

    Historical unnamed scope retains its recovery adapter. Protected routes
    require a verified account before using it. Reserved names are namespaced
    so a real human account can never impersonate a Copal sentinel.
    """
    owner = str(username or "").strip().lower()
    if not owner:
        return _owner_identity_copal_owner(owner)
    if owner in _COPAL_RESERVED_OWNERS or owner.startswith(_COPAL_RESERVED_OWNER_PREFIXES):
        return f"user:{owner}"
    return owner


def get_current_user(request: Request) -> Optional[str]:
    """Get current username from request state (set by auth middleware)."""
    return getattr(getattr(request, 'state', None), 'current_user', None)


def effective_user(request: Request) -> Optional[str]:
    """The real human behind the request, for ownership/attribution.

    Cookie sessions resolve to the logged-in username. Bearer ``ody_`` callers
    come through as the sandboxed pseudo-user "api" so they can't wander into
    cookie/user routes by default, but their token was minted by, and belongs
    to, a real owner stamped on ``request.state.api_token_owner``. Routes that
    should attribute a token's actions to that owner (sessions, chat history)
    call this instead of :func:`get_current_user`, so a paired client sees and
    creates the SAME data as the owner's desktop UI rather than a separate
    "api"-owned silo.

    For cookie sessions this is identical to :func:`get_current_user`, so
    swapping a route over is a no-op for browser users. A bearer token with no
    owner falls back to :func:`get_current_user` (the "api" pseudo-user), so it
    never escalates.
    """
    state = getattr(request, "state", None)
    if getattr(state, "api_token", False):
        owner = getattr(state, "api_token_owner", None)
        if owner:
            return owner
    user = get_current_user(request)
    # In-process agent tools authenticate as the reserved ``internal-tool``
    # identity and forward the human session owner separately.  Keep model and
    # other owner-scoped lookups on that human account instead of accidentally
    # creating globally ownerless rows or an unusable internal-tool catalogue.
    if is_internal_tool_identity(user):
        headers = getattr(request, "headers", None)
        owner = (headers.get(INTERNAL_TOOL_OWNER_HEADER) if headers is not None else None)
        manager = getattr(request.app.state, "auth_manager", None)
        owner = str(owner or "").strip().lower()
        from core.auth import normalize_known_username
        if manager is not None and normalize_known_username(manager.users, owner):
            return owner
        return None
    return user


def _is_api_token_request(request: Request) -> bool:
    """Return True when middleware authenticated a bearer API token."""
    return bool(getattr(request.state, "api_token", False))


def require_authenticated_request(request: Request) -> str:
    """Allow either a browser session or a valid bearer API token.

    This is intentionally narrower than :func:`require_user`: use it only for
    routes that need authentication but do not read or mutate owner-scoped
    user data. Owner-scoped routes should use ``require_user`` for browser
    sessions or their own API-token scope/owner gate.
    """
    if _is_api_token_request(request):
        owner = str(getattr(request.state, "api_token_owner", "") or "").strip().lower()
        manager = getattr(request.app.state, "auth_manager", None)
        from core.auth import normalize_known_username
        if not getattr(request.state, "authenticated", False) or manager is None or normalize_known_username(manager.users, owner) is None:
            raise HTTPException(401, "Not authenticated")
        return owner
    return require_user(request)


def require_user(request: Request) -> str:
    """Require a middleware-verified browser or internal capability identity."""
    if _is_api_token_request(request):
        raise HTTPException(403, "API tokens must use a scope-aware API route")
    state = getattr(request, "state", None)
    if not getattr(state, "authenticated", False):
        raise HTTPException(401, "Not authenticated")
    user = get_current_user(request)
    if is_internal_tool_identity(user) and getattr(state, "internal_tool_authenticated", False):
        return user
    manager = getattr(request.app.state, "auth_manager", None)
    if manager is None:
        raise HTTPException(503, "Authentication unavailable")
    from core.auth import normalize_known_username
    if normalize_known_username(manager.users, user) is None:
        raise HTTPException(401, "Not authenticated")
    return user


def require_privilege(request: Request, key: str) -> str:
    """Require an explicit true privilege from the verified identity authority."""
    user = require_user(request)
    if is_internal_tool_identity(user):
        # This lane is admitted only by loopback plus the internal capability.
        return user
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if auth_mgr is None:
        raise HTTPException(503, "Privilege authority unavailable")
    try:
        privs = auth_mgr.get_privileges(user)
    except Exception as exc:
        raise HTTPException(503, "Privilege authority unavailable") from exc
    if not isinstance(privs, dict) or privs.get(key) is not True:
        raise HTTPException(403, f"Your account is not allowed to {key.replace('_', ' ')}.")
    return user


def owner_filter(query, model_cls, user: str, *, include_shared: bool = True):
    """Filter to a named owner, with explicit optional shared catalog rows."""
    if not user:
        raise HTTPException(401, "Owner identity required")
    if include_shared:
        return query.filter((model_cls.owner == user) | (model_cls.owner == None))  # noqa: E711
    return query.filter(model_cls.owner == user)

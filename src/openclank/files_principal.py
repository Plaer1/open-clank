"""Shared human principal resolution for the Files and file-policy routes."""

from __future__ import annotations

import os

from fastapi import HTTPException, Request

from src.auth_helpers import require_user


def files_principal(request: Request, *, repository=None) -> tuple[str, str, bool]:
    username = str(require_user(request) or "").strip().lower()
    if not username:
        raise HTTPException(401, "Authentication required")
    auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
    subject_id = auth_manager.account_id(username) if auth_manager and hasattr(auth_manager, "account_id") else None
    if not subject_id:
        # Mutable usernames must not inherit refs after account recreation.
        raise HTTPException(503, "Immutable account identity is unavailable")
    # Only the anonymous local installation gets the local administrator lane;
    # named users and agent identities retain their existing account checks.
    is_admin = bool(auth_manager and auth_manager.is_admin(username))
    if repository is not None:
        if auth_manager is not None and hasattr(auth_manager, "users"):
            repository.sync_subjects(auth_manager.users)
    return str(subject_id), username, is_admin

"""Shared privilege policy for scheduled task actions."""

from __future__ import annotations

ADMIN_ONLY_TASK_ACTIONS = frozenset({
    "run_local",
    "run_script",
    "ssh_command",
    "cookbook_serve",
})


def is_admin_only_task_action(task_type: str | None, action: str | None) -> bool:
    return (task_type or "llm") == "action" and (action or "") in ADMIN_ONLY_TASK_ACTIONS


def owner_has_admin_task_privileges(owner: str | None) -> bool:
    """Persisted task owners must resolve to a real configured administrator."""
    owner = str(owner or "").strip()
    if not owner:
        return False
    try:
        from core.auth import AuthManager
        auth = AuthManager()
        if not auth.is_configured:
            return False
        return auth.is_admin(owner.lower()) is True
    except Exception:
        return False

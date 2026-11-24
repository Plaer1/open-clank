"""Per-owner permission mode: manual / yolo / auto (QOL-pass slice 5).

manual — current behavior: sensitive actions raise an interactive approval
         card and questions reach the user.
yolo   — permission requests auto-approve with once semantics (no durable
         grant is written; audit records still stand); the agent may still
         ask the user questions.
auto   — permission requests auto-approve AND the question channels are
         suppressed, so the turn runs fully autonomously.

The mode never widens scope: non-admin ceilings, incognito caps,
fail_on_interaction workloads, and durable-grant matching are all consulted
first and stay authoritative.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MODES = ("manual", "yolo", "auto")
DEFAULT_MODE = "manual"


def permission_mode_for_owner(owner: object) -> str:
    """Read the owner's permission mode preference. Fails closed to manual."""
    owner_key = str(owner or "").strip()
    if not owner_key:
        return DEFAULT_MODE
    try:
        from routes.prefs_routes import _load_for_user

        prefs = _load_for_user(owner_key) or {}
    except Exception as exc:
        logger.warning("permission_mode: prefs unreadable for owner: %s", exc)
        return DEFAULT_MODE
    mode = str(prefs.get("permission_mode") or DEFAULT_MODE).strip().lower()
    return mode if mode in MODES else DEFAULT_MODE


def auto_approves(owner: object) -> bool:
    """yolo/auto: permission requests resolve immediately, once semantics."""
    return permission_mode_for_owner(owner) in ("yolo", "auto")


def suppresses_questions(owner: object) -> bool:
    """auto: ask_user / worker question channels never block the turn."""
    return permission_mode_for_owner(owner) == "auto"

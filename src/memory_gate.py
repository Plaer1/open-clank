"""Memory capture policy gate — one decision point for every memory writer.

Modes (per-user pref ``memory_mode``):
  - ``"off"``        : no memory writes at all (capture, extraction, tool add).
  - ``"automatic"``  : current behaviour — capture enters candidates (pending),
                       manual/tool writes go to curated directly.
  - ``"manual"``     : automatic capture enters candidates as pending and STAYS
                       there until the user explicitly approves.  Manual/tool
                       writes are refused with a clear message telling the user
                       to approve pending candidates first.

The gate is consumed by two seams:
  1. ``chat_helpers.run_post_response_tasks`` — decides whether per-turn
     capture/extraction fires.
  2. ``ai_interaction.do_manage_memory`` + ``routes/memory/memory_routes.api_add_memory``
     — decides whether a manual/tool write is allowed.

Incognito and compare_mode always block capture regardless of mode (they are
per-request overrides, not per-user prefs).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

VALID_MODES = frozenset({"off", "automatic", "manual"})
DEFAULT_MODE = "manual"
PREF_KEY = "memory_mode"


def memory_mode(prefs: Any) -> str:
    """Return the effective memory mode for a user's prefs.

    Existing accounts are backfilled to their prior effective mode at startup.
    Missing or malformed values therefore belong to new/degraded state and
    fail to the review-first ``manual`` default.
    """
    if not isinstance(prefs, Mapping):
        return DEFAULT_MODE
    raw = str(prefs.get(PREF_KEY, DEFAULT_MODE)).strip().lower()
    return raw if raw in VALID_MODES else DEFAULT_MODE


def capture_allowed(
    prefs: Any,
    *,
    incognito: bool = False,
    compare_mode: bool = False,
) -> bool:
    """True when automatic per-turn capture/extraction may fire.

    Incognito and compare_mode are hard overrides — they block capture in
    every mode.  When ``memory_mode`` is explicitly set it is canonical;
    otherwise the legacy ``auto_memory`` pref (default True) controls capture
    so existing deployments keep their behaviour.
    """
    if incognito or compare_mode:
        return False
    if not isinstance(prefs, Mapping):
        return True
    if prefs.get(PREF_KEY) is not None:
        return memory_mode(prefs) != "off"
    return prefs.get("auto_memory", True)


def capture_mode(prefs: Any) -> str:
    """Return the mode automatic turn capture should pass to Frankenmemory.

    Persisted users are backfilled with ``memory_mode``. A compatibility caller
    can still provide only the legacy ``auto_memory`` flag, though, and that
    flag must not silently fall through to the review-first default used by
    direct writes. Keep this conversion beside ``capture_allowed`` so the
    decision to queue capture and the admission mode sent to the provider
    cannot disagree.
    """
    if not isinstance(prefs, Mapping) or PREF_KEY not in prefs:
        legacy_enabled = (
            not isinstance(prefs, Mapping)
            or prefs.get("auto_memory", True)
        )
        return "automatic" if legacy_enabled else "off"
    return memory_mode(prefs)


def write_allowed(prefs: Any) -> tuple[bool, str]:
    """True when a manual or tool-initiated memory write may proceed.

    Returns ``(allowed, reason)``.  In ``manual`` mode the write is refused
    because the user has chosen to review every memory before it lands —
    a direct-to-curated write would bypass that contract.
    """
    mode = memory_mode(prefs)
    if mode == "off":
        return False, "Memory is off — no writes allowed."
    if mode == "manual":
        return False, (
            "Memory is in manual mode — approve pending candidates "
            "instead of adding directly."
        )
    return True, ""

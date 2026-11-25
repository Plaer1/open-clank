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

from typing import Any, Mapping

VALID_MODES = frozenset({"off", "automatic", "manual"})
DEFAULT_MODE = "automatic"
PREF_KEY = "memory_mode"


def memory_mode(prefs: Any) -> str:
    """Resolve an explicit choice, the older capture switch, or the factory default."""
    if not isinstance(prefs, Mapping):
        return "manual"
    if PREF_KEY not in prefs:
        if "auto_memory" not in prefs:
            return DEFAULT_MODE
        legacy = prefs["auto_memory"]
        if type(legacy) is bool:
            return "automatic" if legacy else "off"
        return "manual"
    raw = str(prefs[PREF_KEY]).strip().lower()
    return raw if raw in VALID_MODES else "manual"


def capture_allowed(
    prefs: Any,
    *,
    incognito: bool = False,
    compare_mode: bool = False,
) -> bool:
    """Capture and extraction obey the account setting and per-turn overrides."""
    return not incognito and not compare_mode and capture_mode(prefs) != "off"


def capture_mode(prefs: Any) -> str:
    if isinstance(prefs, Mapping) and prefs.get("memory_enabled") is False:
        return "off"
    return memory_mode(prefs)


def write_allowed(prefs: Any) -> tuple[bool, str]:
    """True when a manual or tool-initiated memory write may proceed.

    Returns ``(allowed, reason)``.  In ``manual`` mode the write is refused
    because the user has chosen to review every memory before it lands —
    a direct-to-curated write would bypass that contract.
    """
    mode = capture_mode(prefs)
    if mode == "off":
        return False, "Memory is off — no writes allowed."
    if mode == "manual":
        return False, (
            "Memory is in manual mode — approve pending candidates "
            "instead of adding directly."
        )
    return True, ""

"""Path and scope identities for the canonical History worker policy service.

Policy reads, revision checks, writes and usage measurements belong to the
HistoryClient GetPolicy/SetPolicy/GetUsage interfaces, never a JSON adapter.
Existing history-settings.json files remain untouched as historical input.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

_LEGACY_HISTORY_DATA_DIR = (
    os.environ.get("OPENCLANK_DATA_DIR")
    if not (os.environ.get("OPEN_CLANK_DATA_DIR") or os.environ.get("ODYSSEUS_DATA_DIR"))
    else None
)


def _data_root() -> Path:
    # Preserve the adapter's previous alias if it was supplied before the
    # shared constants module normalized environment aliases. Canonical names
    # and DATA_DIR remain the normal application authority.
    from src.constants import DATA_DIR

    return Path(_LEGACY_HISTORY_DATA_DIR or DATA_DIR)


def settings_path() -> Path:
    override = os.environ.get("OPENCLANK_HISTORY_SETTINGS_FILE")
    if override:
        return Path(override)
    return _data_root() / "history-settings.json"


def history_root() -> Path:
    return Path(os.environ.get("OPENCLANK_HISTORY_ROOT", str(settings_path().parent / "history")))


def scope_id(
    kind: str,
    value: str,
    workspace_id: str | None = None,
    owner_account_id: str | None = None,
) -> str:
    """Return a stable server-derived scope id; callers cannot choose owner ids."""
    raw = "\0".join((kind.strip().lower(), owner_account_id or "", workspace_id or "", value.strip()))
    return f"{kind.strip().lower()}:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


__all__ = ["history_root", "scope_id", "settings_path"]

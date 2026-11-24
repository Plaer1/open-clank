"""The current Files-backed Copal authority for app and agent callers."""
from __future__ import annotations

import os
from pathlib import Path

from src.constants import DATA_DIR


def configured_bridge():
    from src.openclank.copal_loose import LooseCopalBridge
    return LooseCopalBridge(os.environ.get("COPAL_LOOSE_ROOT") or str(Path(DATA_DIR) / "copal-vaults"))

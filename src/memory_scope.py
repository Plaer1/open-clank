"""Canonical frankenmemory scope convention.

Conversational memory lives in ONE workspace — the engine's own default,
"global" — no matter which host, session, or filesystem path the turn came
from. The workspace axis itself is kept: fm reads union the caller's
workspace with "global", code_index scopes per repo, and the axis stays
reserved for genuinely project-scoped memory later.

Nothing outside this module may spell the workspace literal. Every fm
write site imports chat_workspace() instead, so the convention cannot
drift back into path-derived workspace ids one call site at a time.

FM_WORKSPACE_ID remains an explicit operator/test override; unset, the
canonical value applies.
"""

import os

CHAT_WORKSPACE = "global"


def chat_workspace() -> str:
    """Workspace id that every conversational-memory write must carry."""
    return os.environ.get("FM_WORKSPACE_ID", "").strip() or CHAT_WORKSPACE

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
from dataclasses import dataclass
from typing import List, Optional

CHAT_WORKSPACE = "global"
LOCAL_MEMORY_OWNER = "local"


def chat_workspace() -> str:
    """Workspace id that every conversational-memory write must carry."""
    return os.environ.get("FM_WORKSPACE_ID", "").strip() or CHAT_WORKSPACE


def memory_owner(owner: object = None) -> str:
    """Return the durable owner used when authentication has no username."""
    return str(owner or "").strip() or LOCAL_MEMORY_OWNER


@dataclass(frozen=True)
class CanonicalScope:
    """One exact storage scope; empty keys represent API nulls."""

    owner_id: str
    workspace_id: Optional[str] = None
    project_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.owner_id.strip():
            raise ValueError("owner_id is required")
        if self.workspace_id is not None and not self.workspace_id.strip():
            raise ValueError("workspace_id cannot be blank")
        if self.project_id is not None and not self.project_id.strip():
            raise ValueError("project_id cannot be blank")
        if self.project_id is not None and self.workspace_id is None:
            raise ValueError("project_id requires workspace_id")

    @property
    def storage_keys(self) -> tuple[str, str, str]:
        return (self.owner_id, self.workspace_id or "", self.project_id or "")


def effective_scope_set(
    owner_id: object,
    workspace_id: object = None,
    project_id: object = None,
    *,
    owner_only: bool = False,
) -> List[CanonicalScope]:
    """Return the one exact-scope list used by every v2 read path.

    Results are least-specific to most-specific so callers can apply the
    singleton shadow rule deterministically while set-valued predicates union.
    Session IDs and browser-provided scope are intentionally not accepted.
    """
    owner = memory_owner(owner_id)
    workspace = str(workspace_id).strip() if workspace_id is not None else None
    project = str(project_id).strip() if project_id is not None else None
    requested = CanonicalScope(owner, workspace or None, project or None)
    scopes = [CanonicalScope(owner)]
    if not owner_only and requested.workspace_id:
        scopes.append(CanonicalScope(owner, requested.workspace_id))
        if requested.project_id:
            scopes.append(CanonicalScope(owner, requested.workspace_id, requested.project_id))
    return scopes

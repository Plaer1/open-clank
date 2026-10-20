"""On-disk plan files for approved plans.

Owner convention (2026-08-14, canonicalized in S01): a plan's metaplan (the
outline/guide, built as stages meant to be done sequentially) lives at
``<workspace>/.clanker/futures/<metaplan>.md``; the primary content of each
stage is split into slices at ``<workspace>/.clanker/futures/<metaplan>/slice_N.md``;
and parallelizable work inside a stage goes one level deeper at
``<workspace>/.clanker/futures/<metaplan>/slice_N/slice_Nx.md``.

Plan mode itself is read-only, so nothing is written while proposing. The
server materializes the metaplan file when the user approves (Execute sends
``approved_plan``), and ``update_plan`` mirrors progress back to the same
file while the agent executes. Slice files are authored by the executing
agent through the ordinary brokered write tools.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from core.atomic_io import atomic_write_text
from src.clanker_paths import ClankerPathError, canonical_plan_path

logger = logging.getLogger(__name__)

FUTURES_DIR = ".clanker/futures"

_SLUG_MAX = 60


def plan_slug(plan_text: str) -> str:
    """Derive a stable kebab-case slug from the plan's first heading or item."""
    for line in (plan_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        heading = re.match(r"^#{1,4}\s+(.*)$", line)
        text = heading.group(1) if heading else re.sub(r"^[-*]\s+\[[ xX\-]\]\s+", "", line)
        text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
        if text:
            return text[:_SLUG_MAX].strip("-")
    return "plan"


def metaplan_relpath(plan_text: str) -> str:
    """Workspace-relative path of the plan's metaplan file."""
    return f"{FUTURES_DIR}/{plan_slug(plan_text)}.md"


def _safe_relative_plan_path(relative_path: str) -> Optional[str]:
    try:
        value = canonical_plan_path(str(relative_path or ""), allow_legacy=True)
    except ClankerPathError:
        return None
    if not value.endswith(".md"):
        return None
    return value


def materialize_plan(
    workspace: str,
    plan_text: str,
    *,
    relative_path: Optional[str] = None,
) -> Optional[str]:
    """Write the plan to ``<workspace>/.clanker/futures/<slug>.md``.

    Returns the workspace-relative path, or ``None`` when there is nothing to
    write. This is a user-approved artifact write at the plan approval/sync
    boundary — best-effort: callers log failures rather than fail the turn.
    """
    plan = (plan_text or "").strip()
    if not workspace or not plan:
        return None
    base = os.path.realpath(workspace)
    if not os.path.isdir(base):
        return None
    if relative_path is not None:
        rel = _safe_relative_plan_path(relative_path)
        if rel is None:
            logger.warning("Refused invalid plan artifact path: %r", relative_path)
            return None
    else:
        rel = metaplan_relpath(plan)
    target = os.path.realpath(os.path.join(base, rel))
    if os.path.normcase(os.path.commonpath([base, target])) != os.path.normcase(base):
        logger.warning("Refused plan write outside the workspace: %s", rel)
        return None
    atomic_write_text(target, plan + "\n")
    return rel

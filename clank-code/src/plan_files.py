"""Canonical paths for task-bound Clanker plan artifacts.

Planning drafts and notes may precede implementation approval; only the plan
approval owner grants execution. Metaplans and concept-named slices are written
through the admitted Files/History workflow owner, never a raw file mirror.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

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


class PlanArtifactUnavailable(RuntimeError):
    """The caller does not hold an admitted Files/History workflow writer."""


def materialize_plan(workspace: str, plan_text: str, *, relative_path: Optional[str] = None) -> Optional[str]:
    """Legacy raw writer retired; callers must use the admitted workflow owner."""
    if not workspace or not (plan_text or "").strip():
        return None
    if relative_path is not None and _safe_relative_plan_path(relative_path) is None:
        raise ValueError("invalid plan artifact path")
    raise PlanArtifactUnavailable("Plan artifact requires an admitted Files and History workflow")

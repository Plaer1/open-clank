"""Server-owned plan draft/approval state.

The browser may display a plan, but it must not be able to make arbitrary
markdown executable.  Approval is a compare-and-swap over the plan revision
and digest persisted in the canonical session's ``mimo_state``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from src.openclank.transcript_projection import get_mimo_state, save_mimo_state
from src.clanker_paths import ClankerPathError, canonical_plan_path


def _state_plan(state: dict[str, Any]) -> dict[str, Any]:
    value = state.get("plan_state")
    return dict(value) if isinstance(value, dict) else {}


def approved_plan_state(session_id: str, owner: str) -> dict[str, Any]:
    """Return the current plan only when its exact revision is approved."""
    state = get_mimo_state(session_id, owner=owner)
    plan = _state_plan(state)
    revision = int(plan.get("revision") or 0)
    approved_revision = plan.get("approved_revision")
    digest = str(plan.get("digest") or "")
    approved_digest = str(plan.get("approved_digest") or "")
    if not revision or not digest or approved_revision != revision or approved_digest != digest:
        return {}
    if str(plan.get("status") or "approved") in {"cleared", "draft", "revised"}:
        return {}
    return plan


def approve_plan(
    session_id: str,
    owner: str,
    *,
    revision: int,
    digest: str,
) -> dict[str, Any]:
    """CAS-approve a displayed plan revision for one owned session."""
    state = get_mimo_state(session_id, owner=owner)
    plan = _state_plan(state)
    current_revision = int(plan.get("revision") or 0)
    current_digest = str(plan.get("digest") or "")
    if current_revision != int(revision) or not current_digest or current_digest != str(digest):
        raise ValueError("plan revision is stale or unavailable")
    plan["approved_revision"] = current_revision
    plan["approved_digest"] = current_digest
    plan["status"] = "approved"
    state["plan_state"] = plan
    saved = save_mimo_state(session_id, state, owner=owner)
    return dict(saved.get("plan_state") or plan)


def save_plan_draft(session_id: str, owner: str, plan_text: str) -> dict[str, Any]:
    """Persist a draft and explicitly clear any prior approval."""
    plan_text = str(plan_text or "").strip()[:8192]
    if not plan_text:
        raise ValueError("plan text is required")
    state = get_mimo_state(session_id, owner=owner)
    previous = _state_plan(state)
    material = json.dumps({"plan": plan_text}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    revision = int(previous.get("revision") or 0)
    if previous.get("digest") != digest:
        revision += 1
    plan = {
        "plan": plan_text,
        "digest": digest,
        "revision": revision,
        "approved_revision": None,
        "approved_digest": None,
        "status": "draft",
        "artifact_relpath": previous.get("artifact_relpath"),
        "artifact_path_revision": int(previous.get("artifact_path_revision") or 0),
        "artifact_path_events": list(previous.get("artifact_path_events") or [])[-32:],
    }
    state["plan_state"] = plan
    saved = save_mimo_state(session_id, state, owner=owner)
    return dict(saved.get("plan_state") or plan)


def bind_artifact_path(session_id: str, owner: str, relative_path: str) -> str:
    """Bind the first approved artifact path and keep it stable thereafter.

    A legacy ``.futures/`` binding is translated exactly once to the canonical
    ``.clanker/futures/`` namespace.  The translation is retained as a bounded
    state event so it cannot silently change the CAS-bound artifact identity.
    """
    try:
        value = canonical_plan_path(str(relative_path or ""), allow_legacy=True)
    except ClankerPathError as exc:
        raise ValueError("invalid plan artifact path") from exc
    state = get_mimo_state(session_id, owner=owner)
    plan = _state_plan(state)
    existing_raw = str(plan.get("artifact_relpath") or "").strip()
    if existing_raw:
        try:
            existing = canonical_plan_path(existing_raw, allow_legacy=True)
        except ClankerPathError as exc:
            raise ValueError("invalid previously bound plan artifact path") from exc
        if existing != existing_raw:
            revision = int(plan.get("artifact_path_revision") or 0) + 1
            events = list(plan.get("artifact_path_events") or [])
            events.append({
                "kind": "legacy_path_migration",
                "from": existing_raw,
                "to": existing,
                "revision": revision,
            })
            plan["artifact_path_revision"] = revision
            plan["artifact_path_events"] = events[-32:]
            plan["artifact_relpath"] = existing
            state["plan_state"] = plan
            save_mimo_state(session_id, state, owner=owner)
        return existing
    if value != str(relative_path or "").strip():
        plan["artifact_path_revision"] = 1
        plan["artifact_path_events"] = [{
            "kind": "legacy_path_migration",
            "from": str(relative_path or "").strip(),
            "to": value,
            "revision": 1,
        }]
    plan["artifact_relpath"] = value
    state["plan_state"] = plan
    save_mimo_state(session_id, state, owner=owner)
    return value


def clear_plan(session_id: str, owner: str) -> dict[str, Any]:
    """Clear a draft/approval without deleting any already-written artifact."""
    state = get_mimo_state(session_id, owner=owner)
    plan = _state_plan(state)
    plan.update({
        "plan": "",
        "todos": [],
        "approved_revision": None,
        "approved_digest": None,
        "status": "cleared",
    })
    state["plan_state"] = plan
    saved = save_mimo_state(session_id, state, owner=owner)
    return dict(saved.get("plan_state") or plan)

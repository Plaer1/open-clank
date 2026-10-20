"""Typed associations for open questions.

Questions stay in the existing v2 knowledge block.  This helper validates and
normalizes their association metadata so prose pronouns are never treated as
identity resolution.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

CONTRACT = "openclank.question-context/v1"
MODES = {"missing_slot", "inquiry"}
TARGET_KINDS = {"entity", "claim", "claim_slot", "source", "evidence", "media_asset", "associated_text"}


class QuestionContextError(ValueError):
    pass


def normalize_question_context(value: Any, *, owner: Optional[str] = None, workspace_id: Optional[str] = None, project_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise QuestionContextError("question_context must be an object")
    mode = str(value.get("mode") or "missing_slot").strip()
    if mode not in MODES:
        raise QuestionContextError("question_context.mode must be missing_slot or inquiry")
    target = value.get("target")
    if not isinstance(target, Mapping):
        raise QuestionContextError("question_context.target is required")
    kind = str(target.get("kind") or "").strip()
    target_id = str(target.get("id") or "").strip()
    if kind not in TARGET_KINDS or not target_id:
        raise QuestionContextError("question_context.target kind and id are required")
    result: dict[str, Any] = {
        "contract": CONTRACT,
        "mode": mode,
        "target": {"kind": kind, "id": target_id},
    }
    for key in ("predicate", "predicate_version", "claim_slot", "expected_value_type", "question_id"):
        if value.get(key) is not None:
            result[key] = str(value[key]).strip()
    if owner:
        result["scope"] = {
            "owner": str(owner),
            "workspace_id": str(workspace_id or "global"),
            "project_id": str(project_id or ""),
        }
    return result

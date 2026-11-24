"""Typed associations for open questions.

Questions stay in the existing v2 knowledge block.  This helper validates and
normalizes their association metadata so prose pronouns are never treated as
identity resolution.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

CONTRACT = "openclank.question-context/v1"
MODES = {"missing_slot", "inquiry"}
SYSTEM_SEED_KEYS = frozenset({"handler.preferred_address", "assistant.operating_boundary"})
TARGET_KINDS = {"entity", "claim", "claim_slot", "source", "evidence", "media_asset", "associated_text"}


class QuestionContextError(ValueError):
    pass


def normalize_question_review(value: Any) -> Optional[dict[str, Any]]:
    """Validate a persisted Human choice; absence retains legacy eligibility."""
    if value is None:
        return None
    if (type(value) is not dict or set(value) != {"version", "disposition"}
        or type(value.get("version")) is not int or value["version"] != 1
        or value.get("disposition") not in ("eligible", "deferred", "dismissed")):
        raise QuestionContextError("question review unavailable")
    return {"version": 1, "disposition": value["disposition"]}


def normalize_question_context(value: Any, *, owner: Optional[str] = None, workspace_id: Optional[str] = None, project_id: Optional[str] = None, allow_system_seed: bool = False) -> Optional[dict[str, Any]]:
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
    provenance = value.get("provenance")
    if provenance is not None:
        # This is descriptive metadata only. Generic callers cannot attest that
        # a question was system-seeded; the eventual host issuer must bind the
        # scoped principal and slot under its own authority.
        if (allow_system_seed is not True or not isinstance(provenance, Mapping)
            or set(provenance) != {"version", "kind", "seed_key"} or type(provenance.get("version")) is not int or provenance["version"] != 1 or provenance.get("kind") != "system_seed"
            or type(provenance.get("seed_key")) is not str or provenance["seed_key"] not in SYSTEM_SEED_KEYS or kind != "entity"
            or not owner or not workspace_id or not all(result.get(key) for key in ("predicate", "claim_slot", "question_id"))):
            raise QuestionContextError("system seed provenance unavailable")
        result["provenance"] = {"version": 1, "kind": "system_seed", "seed_key": provenance["seed_key"]}
    if owner:
        result["scope"] = {
            "owner": str(owner),
            "workspace_id": str(workspace_id or "global"),
            "project_id": str(project_id or ""),
        }
    return result

"""Versioned deterministic quality projection for Stats."""
from __future__ import annotations

import time
from collections.abc import Iterable, Mapping

FORMULA_VERSION = "s09-quality-v2"
MIN_COVERAGE = 0.5
MAX_QUALITY_EVENTS = 100_000
FAMILIES = ("prompt_maturity", "context_health", "workflow_hygiene", "tool_reliability")
POLICY = {"prompt_maturity": 0.25, "context_health": 0.25, "workflow_hygiene": 0.25, "tool_reliability": 0.25}
SIGNALS = {
    "prompt_maturity": ("prompt_unverified", "prompt_missing_verification"),
    "context_health": ("context_pressure", "compacted", "mid_task_compaction"),
    "workflow_hygiene": ("abandoned", "edit_churn"),
    "tool_reliability": ("tool_failure", "retry", "streak_failure"),
}


class QualityError(ValueError):
    pass


def _check(cancel_event, deadline):
    if cancel_event is not None and cancel_event.is_set():
        raise QualityError("cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise QualityError("deadline_exceeded")


def capability(name: str) -> dict:
    if name not in {"vcs", "insight"}:
        raise QualityError("unknown optional capability")
    return {"capability": name, "state": "unavailable", "reason": "explicit_gate_required", "version": FORMULA_VERSION}


def _status(rate: float | None) -> str:
    if rate is None:
        return "unavailable"
    if rate >= 0.35:
        return "critical"
    if rate >= 0.18:
        return "warning"
    if rate > 0:
        return "watch"
    return "clear"


def _evidence(value: object, ordinal: int) -> dict:
    identifier = str(value or f"ordinal-{ordinal}")
    if len(identifier) > 128 or any(ord(char) < 32 for char in identifier):
        raise QualityError("invalid evidence id")
    return {"id": identifier, "ordinal": ordinal}


def project_quality(events: Iterable[Mapping[str, object]], *, owner: str,
                    coverage: Mapping[str, int] | None = None,
                    cancel_event=None, deadline: float | None = None) -> dict:
    if not str(owner or "").strip():
        raise QualityError("owner is required")
    _check(cancel_event, deadline)
    rows = []
    for event in events:
        _check(cancel_event, deadline)
        if len(rows) >= MAX_QUALITY_EVENTS:
            raise QualityError("quality event scan exceeds bound")
        if not isinstance(event, Mapping):
            raise QualityError("events must be mappings")
        row_owner = event.get("owner") or event.get("owner_id")
        if not isinstance(row_owner, str) or not row_owner.strip():
            raise QualityError("event owner is required")
        if row_owner.strip().lower() != str(owner).strip().lower():
            raise QualityError("event owner does not match scope")
        rows.append(event)
    denominator = len(rows)
    covered = int((coverage or {}).get("covered", denominator))
    total = int((coverage or {}).get("total", denominator))
    if total < 0 or covered < 0 or covered > total:
        raise QualityError("invalid coverage")
    sufficient = total > 0 and covered / total >= MIN_COVERAGE
    drivers = {family: 0 for family in FAMILIES}
    evidence = {family: [] for family in FAMILIES}
    outcome = {"success": 0, "failure": 0, "unknown": 0}
    for ordinal, event in enumerate(rows, 1):
        _check(cancel_event, deadline)
        evidence_id = _evidence(event.get("evidence_id"), ordinal)
        status = event.get("outcome")
        outcome[status if status in outcome else "unknown"] += 1
        checks = {
            "prompt_maturity": event.get("prompt_unverified") or event.get("prompt_missing_verification"),
            "context_health": event.get("context_pressure") or event.get("compacted") or event.get("mid_task_compaction"),
            "workflow_hygiene": event.get("abandoned") or event.get("edit_churn"),
            "tool_reliability": event.get("tool_failure") or event.get("retry") or event.get("streak_failure"),
        }
        for family, affected in checks.items():
            if affected:
                drivers[family] += 1
                if len(evidence[family]) < 8:
                    evidence[family].append(evidence_id)
    families = {}
    penalty = 0.0
    family_sufficient = True
    for family in FAMILIES:
        rate = drivers[family] / denominator if denominator else None
        signal_rows = sum(any(signal in event for signal in SIGNALS[family]) for event in rows)
        signal_coverage = signal_rows / denominator if denominator else 0.0
        family_sufficient = family_sufficient and signal_coverage >= MIN_COVERAGE
        state = _status(rate) if sufficient and signal_coverage >= MIN_COVERAGE else "unavailable"
        sparkline = []
        if rows:
            step = max(1, (len(rows) + 15) // 16)
            for index in range(0, len(rows), step):
                window = rows[index:index + step]
                sparkline.append(round(sum(bool(window_event.get("outcome") == "failure") for window_event in window) / len(window), 4))
            sparkline = sparkline[:16]
        families[family] = {"state": state, "affected": drivers[family], "share": rate,
                            "weight": POLICY[family], "evidence": evidence[family],
                            "sparkline": sparkline,
                            "coverage": {"covered": covered, "total": total,
                                         "signal_covered": signal_rows,
                                         "signal_coverage": signal_coverage}}
        if rate is not None:
            penalty += rate * POLICY[family] * 100
    sufficient = sufficient and family_sufficient
    score = round(max(0.0, 100.0 - penalty), 2) if sufficient else None
    grade = ("A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "F") if score is not None else None
    return {"formula_version": FORMULA_VERSION, "weights": POLICY, "state": "scored" if sufficient else "unscored",
            "score": score, "grade": grade, "coverage": {"covered": covered, "total": total},
            "outcomes": outcome, "families": families}

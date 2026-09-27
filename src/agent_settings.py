"""Validated owner-scoped native agent settings and effective budget math."""
from __future__ import annotations
from typing import Any, Mapping
from dataclasses import dataclass
import hashlib, json, re

@dataclass(frozen=True)
class AdmittedAgentSettings:
    """Immutable settings captured when a turn is admitted."""
    owner: str
    generation: int
    effective: Mapping[str, Any]

def admit_agent_settings(owner: str, settings: Mapping[str, Any], *, generation: int, context_window: int, max_output: int) -> AdmittedAgentSettings:
    effective = effective_agent_settings(settings, context_window=context_window, max_output=max_output)
    return AdmittedAgentSettings(str(owner), int(generation), json.loads(json.dumps(effective, sort_keys=True)))

DEFAULT_AGENT_SETTINGS = {
    "compaction": {"auto": True, "prune": True, "tail_turns": 2, "preserve_recent_tokens": None, "reserved": None, "max_context": None},
    "checkpoint": {"reserved": 13000, "max_writer_failures": 3, "fork": False, "push_caps": {}},
}

def validate_agent_settings(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("agent_settings must be an object")
    out = {"compaction": {}, "checkpoint": {}}
    c, k = value.get("compaction", {}), value.get("checkpoint", {})
    if not isinstance(c, Mapping) or not isinstance(k, Mapping):
        raise ValueError("compaction and checkpoint must be objects")
    bools = ((c, "auto"), (c, "prune"), (k, "fork"))
    for section, name in bools:
        if name in section and type(section[name]) is not bool: raise ValueError(f"{name} must be boolean")
        if name in section: out["compaction" if section is c else "checkpoint"][name] = section[name]
    ranges = ((c, "tail_turns", 0, 1000), (c, "preserve_recent_tokens", 0, 8_000_000), (c, "reserved", 0, 2_000_000), (k, "reserved", 0, 2_000_000), (k, "max_writer_failures", 1, 100))
    for section, name, low, high in ranges:
        if name in section:
            if type(section[name]) is not int or not low <= section[name] <= high: raise ValueError(f"{name} is out of range")
            out["compaction" if section is c else "checkpoint"][name] = section[name]
    def valid_quantity(item: Any) -> bool:
        if type(item) in (int, float): return item >= 0
        if not isinstance(item, str): return False
        return bool(re.fullmatch(r"(?:\d+(?:\.\d+)?\s*[KMGkmg]|\d+(?:\.\d+)?%|\d+)", item.strip()))
    if "max_context" in c:
        mc = c["max_context"]
        if not (mc is None or valid_quantity(mc) or (isinstance(mc, Mapping) and all(isinstance(k, str) and valid_quantity(v) for k, v in mc.items()))): raise ValueError("max_context is invalid")
        out["compaction"]["max_context"] = mc
    if "push_caps" in k:
        allowed_caps = {"tasks_ledger", "focus_task", "actor_ledger", "memory_titles", "global", "checkpoint", "memory", "notes", "design_decisions", "open_notes", "recent_user", "recent_user_per_msg"}
        if not isinstance(k["push_caps"], Mapping) or any(name not in allowed_caps or type(v) is not int or v <= 0 for name, v in k["push_caps"].items()): raise ValueError("push_caps is invalid")
        out["checkpoint"]["push_caps"] = dict(k["push_caps"])
    if "thresholds" in k:
        thresholds = k["thresholds"]
        def valid_threshold(item: Any) -> bool:
            if not isinstance(item, str): return False
            match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(%|[KMGkmg])?", item.strip())
            if not match: return False
            number, suffix = float(match.group(1)), match.group(2)
            return number > 0 and (suffix != "%" or number <= 100)
        if not isinstance(thresholds, list) or not thresholds or any(not valid_threshold(item) for item in thresholds):
            raise ValueError("thresholds must be a non-empty list of strings")
        out["checkpoint"]["thresholds"] = [item.strip() for item in thresholds]
    return out

def effective_agent_settings(settings: Mapping[str, Any], *, context_window: int, max_output: int) -> dict[str, Any]:
    c = {**DEFAULT_AGENT_SETTINGS["compaction"], **dict(settings.get("compaction", {}))}
    k = {**DEFAULT_AGENT_SETTINGS["checkpoint"], **dict(settings.get("checkpoint", {}))}
    usable = max(0, int(context_window))
    recent = c["preserve_recent_tokens"] if c["preserve_recent_tokens"] is not None else max(2000, min(8000, usable // 4))
    # Native overflow.py keeps an explicitly configured reserve verbatim. Its
    # output-headroom rule is applied separately, so do not approximate it here.
    reserve = c["reserved"]
    max_context = c["max_context"]
    if isinstance(max_context, str) and max_context.lower().endswith("k"): max_context = int(float(max_context[:-1]) * 1000)
    if isinstance(max_context, (int, float)) and max_context > 0: usable = min(usable, int(max_context))
    return {"compaction": {**c, "preserve_recent_tokens": recent, "reserved": reserve, "effective_window": usable}, "checkpoint": k}

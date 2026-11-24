"""Metadata projection over the existing Files-authorized Lore activity page."""
from __future__ import annotations

import hashlib
import itertools
import json
import re
from pathlib import Path

from src.openclank.history_change_sets import parse_locator


def code_resource_id(checkout: str, relative: str) -> str:
    """Match Memery's canonical length-framed file identity."""
    import blake3

    digest = blake3.blake3()
    for field in ("file", checkout, relative):
        encoded = field.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)
    return "file:" + digest.hexdigest()


def project_qualified_history_member(candidate, public, *, path, graph_root, graph_scope):
    """The owning route establishes current Files and Lore authority first."""
    evidence = candidate.get("graph_witness")
    if candidate["state"] != "complete" or evidence is None:
        return None
    locator = parse_locator(candidate["locator"])
    if locator["actionID"] != candidate["action_id"]:
        return None
    version = evidence["after_version_id"]
    if locator["version"] == 1 and locator["afterVersionID"] != version:
        return None
    fingerprint = re.fullmatch(r"sha256:([0-9a-f]{64}):(0|[1-9][0-9]*)", evidence["after_fingerprint"])
    code_id = None
    if graph_scope is not None:
        relative = Path(path).relative_to(graph_root).as_posix()
        if relative == ".":
            return None
        code_id = code_resource_id(graph_scope["checkout_id"], relative)
    source = {
        "resource_id": public["resource_id"],
        "code_resource_id": code_id,
        "recorded_millis": public["recorded_millis"],
        "action_id": locator["actionID"],
        "origin": locator["origin"],
        "after_version_id": version,
        "after_sha256": fingerprint.group(1) if fingerprint else None,
        "locator": candidate["locator"],
    }
    material = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**public, **source, "witness_sha256": hashlib.sha256(material).hexdigest()}


def summarize_history_page(items, graph_scope):
    """Count each distinct eligible Lore action once within this bounded page."""
    actions = {}
    timestamps = []
    for item in items:
        if item["recorded_millis"] is not None:
            timestamps.append(int(item["recorded_millis"]))
        resource = item["code_resource_id"] if graph_scope else item["resource_id"]
        actions.setdefault(item["action_id"], {})[resource] = item["locator"]
    pairs = {}
    for action_id, members in actions.items():
        for left, right in itertools.combinations(sorted(members), 2):
            pairs.setdefault((left, right), []).append({
                "action_id": action_id, "left_locator": members[left], "right_locator": members[right],
            })
    ranked = sorted(pairs.items(), key=lambda pair: (-len(pair[1]), pair[0]))
    recurring = [(pair, witnesses) for pair, witnesses in ranked if len(witnesses) >= 2]
    changes = [
        {
            "kind": "recorded_cochange", "left": left, "right": right,
            "support": len(witnesses), "denominator": len(actions), "witnesses": witnesses,
        }
        for (left, right), witnesses in recurring[:20]
    ]
    return {
        "graph_scope": graph_scope,
        "window": {
            "first_millis": str(min(timestamps)) if timestamps else None,
            "last_millis": str(max(timestamps)) if timestamps else None,
        },
        "coverage": "partial_recorded_activity",
        "eligible_changesets": len(actions),
        "cochanges": changes,
        "omitted_cochanges": max(0, len(recurring) - len(changes)),
    }

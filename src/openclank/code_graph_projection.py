"""Pure C04 Graph projection; no reads, identity inference or new authority.

Keep the canonical envelope and source payloads once. Client nodes/edge IDs
are derived views. A deterministic reserve pays for projection/omission JSON;
never discard retained hits and forward a cursor past them.
"""
from __future__ import annotations

import copy
import json
import re
from src.openclank.code_query import MAX_QUERY_WORK

SCHEMA = "open-clank.code-query/v1"
RESERVE = 1024  # UTF-8 bytes AND conservative byte-token upper bound.
FIELDS = {"label", "source", "signature", "documentation", "occurrences", "resolution", "properties", "excerpt", "witnesses"}
OPERATIONS = {"capabilities", "overview", "catalog", "facets", "outline", "inspect", "neighbors", "path", "impact", "read", "search"}
QUERY_KEYS = {"schema", "operation", "query", "seed_ids", "target_id", "channels", "fields", "filter", "budgets", "expected_receipt", "read", "cursor", "stages", "request_id", "direction"}
DEFAULT_BUDGET = {"rows": 50, "bytes": 65536, "tokens": 8192, "hops": 4, "work": MAX_QUERY_WORK}
MAX_BUDGET = {"rows": 200, "bytes": 262144, "tokens": 8192, "hops": 8, "work": MAX_QUERY_WORK}
TOP_FIELDS = {"schema", "operation", "receipt", "capabilities", "operation_capabilities", "channels", "hits", "edges", "paths", "counts", "facets", "omissions", "partial", "degraded", "reasons", "next_cursor", "error", "cost", "source_window"}
IDENTITY_FIELDS = {"id", "kind", "matches", "evidence_state", "current_source_verified"}
RECEIPT_FIELDS = {"workspace_id", "registration_id", "source_snapshot_id", "checkout_id", "head_generation", "admission_epoch", "policy_generation", "config_fingerprint"}
CANONICAL_ID = re.compile(r"(?:file|decl|occ):[0-9a-fA-F]{1,128}\Z")


class CodeGraphProjectionError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def prepare_graph_query(query: dict) -> tuple[dict, dict]:
    if not isinstance(query, dict) or set(query) - QUERY_KEYS:
        raise CodeGraphProjectionError("Invalid code graph request")
    if query.get("schema", SCHEMA) != SCHEMA or query.get("operation", "overview") not in OPERATIONS:
        raise CodeGraphProjectionError("Unsupported code graph operation")
    fields = query.get("fields", ["label", "source", "properties"])
    if not isinstance(fields, list) or any(not isinstance(field, str) or field not in FIELDS for field in fields) or len(set(fields)) != len(fields):
        raise CodeGraphProjectionError("Invalid requested fields")
    supplied = query.get("budgets", {})
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_BUDGET):
        raise CodeGraphProjectionError("Invalid projection budget")
    caller = {**DEFAULT_BUDGET, **supplied}
    if any(type(value) is not int or value < (0 if key == "hops" else 1) or value > MAX_BUDGET[key] for key, value in caller.items()):
        raise CodeGraphProjectionError("Invalid projection budget")
    if caller["bytes"] < RESERVE * 2 or caller["tokens"] < RESERVE * 2:
        raise CodeGraphProjectionError("Graph envelope requires at least 2048 bytes and byte-tokens")
    try:
        json.dumps(query, allow_nan=False)
    except (ValueError, TypeError):
        raise CodeGraphProjectionError("Invalid code graph JSON") from None
    native = copy.deepcopy(query)
    native.update(schema=SCHEMA, fields=fields, operation=query.get("operation", "overview"))
    native["budgets"] = {**caller, "bytes": caller["bytes"] - RESERVE, "tokens": caller["tokens"] - RESERVE}
    return native, caller


def _known_nested(value, fields: set[str]):
    """Omit known C04 evidence fields, never arbitrary properties metadata."""
    if isinstance(value, list):
        return [_known_nested(row, fields) for row in value]
    if isinstance(value, dict):
        return {key: _known_nested(row, fields) for key, row in value.items()
                if key not in {"source", "witnesses"} or key in fields}
    return value


def _edge(edge: dict, fields: set[str]) -> dict:
    if not isinstance(edge, dict):
        raise CodeGraphProjectionError("Invalid code evidence edge", code="invalid_evidence")
    keys = {"from", "to", "occurrence_id", "resolution_state"} | (fields & {"source", "witnesses"})
    if (any(not isinstance(edge.get(key), str) for key in ("from", "to", "occurrence_id", "resolution_state"))
            or any(not CANONICAL_ID.fullmatch(edge[key]) for key in ("from", "to", "occurrence_id"))
            or not edge["occurrence_id"].startswith("occ:")
            or ("witnesses" in edge and (not isinstance(edge["witnesses"], list) or any(not isinstance(row, dict) for row in edge["witnesses"])))):
        raise CodeGraphProjectionError("Invalid code evidence edge identity", code="invalid_evidence")
    return {key: _known_nested(value, fields) for key, value in edge.items() if key in keys}


def _measure(result: dict) -> int:
    cost = result.setdefault("cost", {})
    for _ in range(8):
        size = len(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        if cost.get("serialized_bytes") == size and cost.get("token_upper_bound") == size:
            return size
        cost.update(serialized_bytes=size, token_upper_bound=size)
    raise CodeGraphProjectionError("Unstable code graph envelope", code="invalid_evidence")


def project_code_graph(result: dict, *, query: dict, caller_budget: dict) -> dict:
    """Retain C04 identity/receipts; truly omit unrequested fields, including edges."""
    if not isinstance(result, dict) or result.get("schema") != SCHEMA:
        raise CodeGraphProjectionError("Invalid code query envelope", code="invalid_evidence")
    if result.get("error") is not None:
        error = result["error"]
        if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
            raise CodeGraphProjectionError("Invalid code query error", code="invalid_evidence")
    else:
        mandatory = TOP_FIELDS - {"next_cursor", "source_window"}
        if not mandatory <= result.keys() or type(result["partial"]) is not bool or type(result["degraded"]) is not bool:
            raise CodeGraphProjectionError("Incomplete code query success", code="invalid_evidence")
    for key in ("hits", "edges", "paths", "counts", "omissions", "capabilities", "operation_capabilities", "channels", "reasons"):
        if key in result and not isinstance(result[key], list):
            raise CodeGraphProjectionError("Invalid code query collection", code="invalid_evidence")
    for key in ("cost", "facets"):
        if key in result and not isinstance(result[key], dict):
            raise CodeGraphProjectionError("Invalid code query metadata", code="invalid_evidence")
    for row in result.get("counts", []):
        if (not isinstance(row, dict) or "value" not in row or not isinstance(row.get("name"), str)
                or not isinstance(row.get("unit"), str) or row.get("exactness") not in {"exact", "unknown"}
                or (row.get("value") is not None and (type(row["value"]) is not int or row["value"] < 0))):
            raise CodeGraphProjectionError("Invalid code query count", code="invalid_evidence")
    if any(not isinstance(row, dict) for row in result.get("omissions", [])):
        raise CodeGraphProjectionError("Invalid field omissions", code="invalid_evidence")
    fields = set(query["fields"])
    public = {key: copy.deepcopy(value) for key, value in result.items() if key in TOP_FIELDS}
    public["domain"] = "code"
    if public.get("source_window") is not None:
        window = public["source_window"]
        window_keys = {"rel_path", "start_byte", "end_byte", "continuation", "continuity"}
        if (not isinstance(window, dict) or set(window) != window_keys or not isinstance(window["rel_path"], str)
                or any(type(window[key]) is not int or window[key] < 0 for key in ("start_byte", "end_byte"))
                or window["end_byte"] < window["start_byte"] or window["continuity"] not in {"full_sha256", "progress_only_nonatomic"}
                or window["continuation"] is not None and not isinstance(window["continuation"], str)):
            raise CodeGraphProjectionError("Invalid source window", code="invalid_evidence")
    receipt = public.get("receipt")
    if receipt is not None:
        if not isinstance(receipt, dict):
            raise CodeGraphProjectionError("Invalid code query receipt", code="invalid_evidence")
        if any((type(receipt.get(key)) is not int or receipt[key] < 0) if key in {"admission_epoch", "policy_generation"}
               else (not isinstance(receipt.get(key), str) or not receipt[key]) for key in RECEIPT_FIELDS):
            raise CodeGraphProjectionError("Incomplete code query receipt", code="invalid_evidence")
        public["receipt"] = {key: value for key, value in receipt.items() if key in RECEIPT_FIELDS}
    omissions = public.setdefault("omissions", [])
    removed: set[str] = set()
    hits = []
    for hit in public.get("hits", []):
        raw = (isinstance(hit, dict) and isinstance(hit.get("id"), str) and hit["id"].startswith("raw:")
               and hit.get("kind") == "raw_window" and result.get("operation") in {"read", "search"})
        if not isinstance(hit, dict) or not isinstance(hit.get("kind"), str) or not isinstance(hit.get("id"), str) or (not raw and not CANONICAL_ID.fullmatch(hit["id"])):
            raise CodeGraphProjectionError("Invalid canonical code identity", code="invalid_evidence")
        allowed = IDENTITY_FIELDS | (fields - {"witnesses"}) | ({"raw_source"} if "excerpt" in fields else set())
        removed.update((set(hit) & FIELDS) - fields)
        hits.append({key: _known_nested(value, fields) if key in {"resolution", "occurrences", "documentation"} else value
                     for key, value in hit.items() if key in allowed})
    public["hits"] = hits
    if receipt is None and not public.get("error") and (any(not hit["id"].startswith("raw:") for hit in hits) or public.get("edges") or public.get("paths")):
        raise CodeGraphProjectionError("Canonical evidence requires its receipt", code="invalid_evidence")
    public["edges"] = [_edge(edge, fields) for edge in public.get("edges", [])]
    paths = []
    for path in public.get("paths", []):
        if (not isinstance(path, dict) or not isinstance(path.get("node_ids"), list)
                or any(not isinstance(node, str) or not CANONICAL_ID.fullmatch(node) for node in path["node_ids"])
                or not isinstance(path.get("edges"), list)):
            raise CodeGraphProjectionError("Invalid code query path", code="invalid_evidence")
        paths.append({"node_ids": path["node_ids"], "edges": [_edge(edge, fields) for edge in path["edges"]]})
    public["paths"] = paths
    for field in sorted(removed):
        if not any(row.get("field") == field for row in omissions):
            omissions.append({"field": field, "reason": "not_requested"})
    try:
        size = _measure(public)
    except (ValueError, TypeError):
        raise CodeGraphProjectionError("Invalid public code evidence", code="invalid_evidence") from None
    if size <= caller_budget["bytes"] and size <= caller_budget["tokens"]:
        return public
    # Safety fallback, not a resumable truncated page: no advanced cursor survives.
    error = {"schema": SCHEMA, "domain": "code", "operation": result.get("operation"),
        "receipt": public.get("receipt"), "capabilities": [], "operation_capabilities": [], "channels": [],
        "hits": [], "edges": [], "paths": [], "counts": [{"name": "returned", "value": 0, "unit": "hits", "exactness": "exact"}],
        "facets": {}, "omissions": [], "partial": True, "degraded": True,
        "reasons": ["projection_budget_exceeded"], "next_cursor": None,
        "error": {"code": "projection_budget_exceeded", "message": "Projection exceeds its envelope budget; retry with fewer rows or fields."}, "cost": {}}
    if _measure(error) > min(caller_budget["bytes"], caller_budget["tokens"]):
        raise CodeGraphProjectionError("Code graph envelope budget exceeded", code="budget_exceeded")
    return error

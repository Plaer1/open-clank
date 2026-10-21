"""Strict app-only map boundary; no discovery, admission or source acquisition.

Native owns inventory/evidence authority. This module validates its public DTO,
rechecks the shared Workspace binding and measures the final HTTP envelope.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re

from src.openclank.code_query import resolve_query_binding

SCHEMA = "open-clank.code-map/v1"
REQUEST_LIMIT = 16 * 1024
MAX_BYTES = 256 * 1024
SELECTION_BYTES = 64 * 1024
MAX_INT = (1 << 53) - 1  # Public counters must remain exact in the browser.
CANONICAL_ID = re.compile(r"(file|decl|occ):[0-9a-fA-F]{1,128}\Z")
RELATIONS = ("containment", "import", "call", "value_reference", "type_reference", "inherits", "implements", "render", "route", "overrides")
LIMITS = dict(bytes=MAX_BYTES, nodes=200, edges=800, groups=50, work=4_000_000)
COST_FIELDS = {"inspected_entries", "sql_vm_steps", "metadata_bytes", "retained_summary_bytes",
    "serialized_bytes", "source_bytes_read", "policy_bytes_read", "summary_cache_hit",
    "native_rpc_calls_completed"}
RECEIPT_FIELDS = {"workspace_id", "registration_id", "source_snapshot_id", "checkout_id",
    "head_generation", "admission_epoch", "policy_generation", "config_fingerprint"}
ERROR_CODES = {"invalid", "invalid_schema", "denied", "erased", "missing", "stale_cursor",
    "reindex_required", "budget_exceeded", "busy", "cancelled", "unsupported", "unavailable",
    "invalid_evidence"}


class CodeVisualError(ValueError):
    def __init__(self, code="invalid"):
        super().__init__(code)
        self.code = code


def _require(condition, code="invalid"):
    if not condition:
        raise CodeVisualError(code)


def _obj(value, required, optional=()):
    _require(type(value) is dict and set(required) <= value.keys()
        and value.keys() <= set(required) | set(optional))
    return value


def _text(value, maximum=256, *, nullable=False):
    if nullable and value is None:
        return value
    _require(type(value) is str and bool(value))
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise CodeVisualError() from None
    _require(size <= maximum and not any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value))
    return value


def _int(value, maximum=MAX_INT, *, minimum=0, nullable=False):
    _require((nullable and value is None) or (type(value) is int and minimum <= value <= maximum))
    return value


def _bool(value):
    _require(type(value) is bool)


def _array(value, maximum):
    _require(type(value) is list and len(value) <= maximum)
    return value


def _canonical(value, kinds=("file", "decl", "occ")):
    _text(value)
    match = CANONICAL_ID.fullmatch(value)
    _require(match is not None and match[1] in kinds)


def _tree(value, *, max_bytes, max_items=12000):
    """Bound direct Python callers too; reject cycles/non-JSON values early."""
    remaining = [max_items, max_bytes]
    def visit(item, depth):
        remaining[0] -= 1
        _require(depth <= 16 and remaining[0] >= 0)
        if type(item) is dict:
            for key, child in item.items():
                _require(type(key) is str)
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif type(item) is str:
            try:
                remaining[1] -= len(item.encode("utf-8"))
            except UnicodeEncodeError:
                raise CodeVisualError() from None
            _require(remaining[1] >= 0)
        else:
            _require(item is None or type(item) is bool or (type(item) is int and -MAX_INT <= item <= MAX_INT))
    visit(value, 0)


def _receipt(value):
    _obj(value, RECEIPT_FIELDS)
    for key, item in value.items():
        (_int if key in {"admission_epoch", "policy_generation"} else _text)(item)


def _authority(value):
    _require(type(value) is dict)
    if value.get("variant") == "structure_preview":
        _obj(value, {"variant", "workspace_id", "registration_id", "checkout_id", "admission_epoch",
            "policy_generation", "workspace_revision", "selection_policy_digest", "inventory_id"})
        for key, item in value.items():
            (_int if key in {"admission_epoch", "policy_generation", "workspace_revision"} else _text)(item)
    else:
        _obj(value, {"variant", "receipt", "selection_policy_digest", "summary_id"})
        _require(value["variant"] == "published")
        _receipt(value["receipt"])
        _text(value["selection_policy_digest"])
        _text(value["summary_id"])
    return value


def _scope(authority, binding):
    fields = authority["receipt"] if authority["variant"] == "published" else authority
    _require(fields["workspace_id"] == binding["workspace_id"]
        and fields["policy_generation"] == binding["policy_generation"], "invalid_evidence")
    if authority["variant"] == "structure_preview":
        _require(authority["workspace_revision"] == binding["workspace_revision"], "invalid_evidence")


def validate_visual_request(value):
    _tree(value, max_bytes=REQUEST_LIMIT)
    _obj(value, {"mode", "request"})
    mode, request = value["mode"], value["request"]
    _require(mode in ("map", "selection"))
    if mode == "selection":
        _obj(request, {"schema", "authority", "selection_id"}, {"cursor", "max_refs"})
        _authority(request["authority"])
        _require(request["authority"]["variant"] == "published")
        _text(request["selection_id"])
        _text(request.get("cursor"), 4096, nullable=True)
        _int(request.get("max_refs", 50), 50, minimum=1)
        normalized = {**request, "cursor": request.get("cursor"), "max_refs": request.get("max_refs", 50)}
    else:
        _obj(request, {"schema", "operation"}, {"expected_authority", "root", "direction", "relations", "cursor", "limits"})
        _require(request["operation"] in ("overview", "children", "neighbors"))
        limits = request.get("limits", {})
        _obj(limits, (), LIMITS)
        limits = {**LIMITS, **limits}
        for key, maximum in LIMITS.items():
            _int(limits[key], maximum, minimum=512 if key == "bytes" else 1)
        direction = request.get("direction", "both")
        _require(direction in ("incoming", "outgoing", "both"))
        relations = request.get("relations", list(RELATIONS))
        _array(relations, len(RELATIONS))
        _require(bool(relations) and all(type(r) is str and r in RELATIONS for r in relations)
            and len(set(relations)) == len(relations))
        authority, root, cursor = request.get("expected_authority"), request.get("root"), request.get("cursor")
        if authority is not None:
            _authority(authority)
        _text(cursor, 4096, nullable=True)
        _require((request["operation"] == "overview" and cursor is None) or authority is not None)
        if request["operation"] == "overview":
            _require(root is None)
        else:
            _require(root is not None)
            _require(type(root) is dict)
            if root.get("kind") == "visual":
                _obj(root, {"kind", "id"})
                _text(root["id"])
            else:
                _obj(root, {"kind", "resource_id", "receipt"})
                _require(root["kind"] == "resource" and request["operation"] == "neighbors")
                _canonical(root["resource_id"])
                _receipt(root["receipt"])
                _require(authority["variant"] == "published" and root["receipt"] == authority["receipt"])
        normalized = {**request, "expected_authority": authority, "root": root, "direction": direction,
            "relations": relations, "cursor": cursor, "limits": limits}
    _require(request["schema"] == SCHEMA)
    _require(len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")) <= REQUEST_LIMIT)
    # Own nested containers across all awaits; never mutate caller input.
    return copy.deepcopy({"mode": mode, "request": normalized})


def parse_body(raw):
    _require(len(raw) <= REQUEST_LIMIT)
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result)
            result[key] = value
        return result
    def constant(_):
        raise CodeVisualError()
    try:
        body = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        _tree(body, max_bytes=REQUEST_LIMIT)
        _obj(body, {"workspace_id", "visual_request"})
        _text(body["workspace_id"])
        return body["workspace_id"], validate_visual_request(body["visual_request"])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CodeVisualError() from None


def _cost(value):
    _obj(value, COST_FIELDS - {"native_rpc_calls_completed"}, {"native_rpc_calls_completed"})
    for key, item in value.items():
        (_bool if key == "summary_cache_hit" else _int)(item)
    _require(value["source_bytes_read"] == 0)
    return value


def _cursor(value, prior):
    _text(value, 4096, nullable=True)
    _require(value is None or value != prior)


def _coverage(value, preview):
    _obj(value, {"inventory_complete", "observed_entries", "total_entries", "indexed_files",
        "parse_failed_files", "ignored_entries", "unsupported_entries", "unavailable_entries", "dependencies"})
    _bool(value["inventory_complete"])
    for key in {"observed_entries", "ignored_entries", "unsupported_entries", "unavailable_entries"}:
        _int(value[key])
    for key in {"total_entries", "indexed_files", "parse_failed_files"}:
        _int(value[key], nullable=True)
    total = value["total_entries"]
    _require(total is None or total >= value["observed_entries"])
    _require(not value["inventory_complete"] or total == value["observed_entries"])
    _require(value["dependencies"] in ("indexing", "complete", "partial"))
    if preview:
        _require(value["indexed_files"] is None and value["parse_failed_files"] is None
            and value["dependencies"] == "indexing")
    else:
        _require(value["indexed_files"] is not None and value["parse_failed_files"] is not None
            and value["dependencies"] != "indexing")
        _require(value["dependencies"] != "complete" or
            (value["parse_failed_files"] == 0 and value["unavailable_entries"] == 0))


def _map_result(value, request):
    _obj(value, {"schema", "operation", "authority", "root", "direction", "relations", "nodes", "edges",
        "coverage", "page", "selection", "cost"})
    _require(value["schema"] == SCHEMA and value["operation"] == request["operation"]
        and value["direction"] == request["direction"] and value["relations"] == request["relations"])
    authority = _authority(value["authority"])
    _require(request["expected_authority"] is None or authority == request["expected_authority"])
    preview = authority["variant"] == "structure_preview"
    _coverage(value["coverage"], preview)
    nodes = _array(value["nodes"], request["limits"]["nodes"])
    ids, parents = set(), {}
    for node in nodes:
        _obj(node, {"id", "label", "kind", "parent", "source_files", "asset_files", "languages"}, {"resource_id", "symbol_kind", "qualified_name", "file_resource_id"})
        _text(node["id"])
        _text(node["label"], 160)
        _require(node["kind"] in ("repository", "folder", "file", "symbol") and node["id"] not in ids)
        if node["kind"] != "symbol":
            _require(not any(key in node for key in ("symbol_kind", "qualified_name", "file_resource_id")))
        else:
            _require(not preview and all(key in node for key in ("resource_id", "symbol_kind", "qualified_name", "file_resource_id")))
        if "resource_id" in node:
            _canonical(node["resource_id"], ("file",) if node["kind"] == "file" else ("decl", "occ"))
        if "file_resource_id" in node:
            _canonical(node["file_resource_id"], ("file",))
        if "symbol_kind" in node:
            _text(node["symbol_kind"], 64)
        if "qualified_name" in node:
            _text(node["qualified_name"], 1024)
        ids.add(node["id"])
        _text(node["parent"], nullable=True)
        _require(node["parent"] != node["id"])
        parents[node["id"]] = node["parent"]
        _int(node["source_files"])
        _int(node["asset_files"])
        languages = _array(node["languages"], 8)
        seen_languages = set()
        for language in languages:
            _obj(language, {"language", "count"})
            _text(language["language"], 32)
            _int(language["count"])
            _require(language["language"] not in seen_languages)
            seen_languages.add(language["language"])
        _require(sum(language["count"] for language in languages) <= node["source_files"])
    _require(sum(node["kind"] in ("repository", "folder") for node in nodes) <= request["limits"]["groups"])
    # Parents may be in a previous hierarchy page. Local cycles are never valid.
    for node_id in ids:
        seen = set()
        current = node_id
        while current in parents:
            _require(current not in seen)
            seen.add(current)
            current = parents[current]
    root = value["root"]
    _text(root, nullable=True)
    if request["operation"] == "overview":
        _require(root is None or any(node["id"] == root and node["kind"] == "repository" for node in nodes))
    else:
        _require(root in ids)
        if request["root"]["kind"] == "visual":
            _require(root == request["root"]["id"])
    edges = _array(value["edges"], request["limits"]["edges"])
    edge_ids = set()
    for edge in edges:
        _obj(edge, {"id", "from", "to", "relation", "state", "count", "selection_id"})
        _text(edge["id"])
        _require(edge["id"] not in edge_ids and edge["from"] in ids and edge["to"] in ids)
        edge_ids.add(edge["id"])
        _require(edge["relation"] in request["relations"])
        _int(edge["count"], minimum=1)
        if edge["relation"] == "containment":
            _require(edge["state"] == "structure" and edge["selection_id"] is None
                and parents[edge["to"]] == edge["from"])
        else:
            _require(not preview and edge["state"] in ("exact", "inferred"))
            _text(edge["selection_id"])
        if request["operation"] == "neighbors":
            _require((value["direction"] == "incoming" and edge["to"] == root)
                or (value["direction"] == "outgoing" and edge["from"] == root)
                or (value["direction"] == "both" and root in (edge["from"], edge["to"])))
    selection = value["selection"]
    if selection is not None:
        _obj(selection, {"visual_id", "resource_id", "receipt"})
        _require(not preview and selection["visual_id"] in ids and selection["receipt"] == authority["receipt"])
        _receipt(selection["receipt"])
        _canonical(selection["resource_id"])
        _require(root is None or selection["visual_id"] == root)
    if request["root"] is not None and request["root"]["kind"] == "resource":
        _require(selection is not None and selection["visual_id"] == root)
    page = _obj(value["page"], {"returned_nodes", "returned_edges", "remaining_nodes", "remaining_edges",
        "complete", "next_cursor", "truncated"})
    _int(page["returned_nodes"])
    _int(page["returned_edges"])
    _require(page["returned_nodes"] == len(nodes) and page["returned_edges"] == len(edges))
    _int(page["remaining_nodes"], nullable=True)
    _int(page["remaining_edges"], nullable=True)
    _bool(page["complete"])
    _bool(page["truncated"])
    _cursor(page["next_cursor"], request["cursor"])
    if page["complete"]:
        _require(not page["truncated"] and page["next_cursor"] is None
            and page["remaining_nodes"] == 0 and page["remaining_edges"] == 0)
    else:
        _require(page["next_cursor"] is not None and
            (page["remaining_nodes"] != 0 or page["remaining_edges"] != 0))


def _selection_result(value, request):
    _obj(value, {"schema", "authority", "selection_id", "receipt", "references", "remaining", "complete", "next_cursor", "cost"})
    _require(value["schema"] == SCHEMA and value["authority"] == request["authority"]
        and value["selection_id"] == request["selection_id"])
    _authority(value["authority"])
    _receipt(value["receipt"])
    _require(value["receipt"] == value["authority"]["receipt"])
    references = _array(value["references"], request["max_refs"])
    seen = set()
    for ref in references:
        _obj(ref, {"occurrence_id", "caller_id", "target_id", "relation", "state"})
        _canonical(ref["occurrence_id"], ("occ",))
        _canonical(ref["target_id"], ("decl",))
        if ref["caller_id"] is not None:
            _canonical(ref["caller_id"], ("decl",))
        _require(ref["occurrence_id"] not in seen and ref["relation"] in ("import", "call")
            and ref["state"] in ("exact", "inferred"))
        seen.add(ref["occurrence_id"])
    _int(value["remaining"])
    _bool(value["complete"])
    _cursor(value["next_cursor"], request["cursor"])
    _require(value["complete"] == (value["next_cursor"] is None and value["remaining"] == 0))
    _require(value["complete"] or (bool(references) and value["next_cursor"] is not None and value["remaining"] > 0))


def _measure(value, maximum):
    # Fixed point: serialized_bytes includes its own decimal digits.
    for _ in range(8):
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        _require(size <= maximum, "budget_exceeded")
        if value["cost"]["serialized_bytes"] == size:
            return value
        value["cost"]["serialized_bytes"] = size
    raise CodeVisualError("invalid_evidence")


def project_visual_result(value, visual_request, binding):
    """Whitelist entire result, preserve receipts, then measure provider additions."""
    try:
        _tree(value, max_bytes=MAX_BYTES, max_items=30000)
        request = visual_request["request"]
        if visual_request["mode"] == "map":
            _map_result(value, request)
            maximum = request["limits"]["bytes"]
        else:
            _selection_result(value, request)
            maximum = SELECTION_BYTES
        _scope(value["authority"], binding)
        _cost(value["cost"])
        return _measure(copy.deepcopy(value), maximum)
    except CodeVisualError as exc:
        raise CodeVisualError("budget_exceeded" if exc.code == "budget_exceeded" else "invalid_evidence") from None
    except (TypeError, KeyError, ValueError, OverflowError, RecursionError):
        raise CodeVisualError("invalid_evidence") from None


def failure(code="unavailable", *, mode=None, observed_cost=None):
    """Errors have no success arrays, authority, receipt or opaque native text."""
    code = code if type(code) is str and code in ERROR_CODES else "unavailable"
    cost = {key: False if key == "summary_cache_hit" else 0 for key in COST_FIELDS}
    if type(observed_cost) is dict:
        for key, item in observed_cost.items():
            if key in COST_FIELDS and key != "serialized_bytes" and (
                (key == "summary_cache_hit" and type(item) is bool) or
                (key != "summary_cache_hit" and type(item) is int and 0 <= item <= MAX_INT)):
                cost[key] = item
    value = dict(schema=SCHEMA, mode=mode if mode in ("map", "selection") else None,
        error={"code": code, "message": "Code map unavailable"}, cost=cost)
    return _measure(value, 2048)


async def query_visual(*, repository, provider, auth_manager, owner, workspace_id, visual_request):
    from src.memory_provider import MemoryRequestRejectedError

    captured, binding, result, prepared, error = None, None, None, None, None
    try:
        _text(workspace_id)
        prepared = validate_visual_request(visual_request)
        binding, captured = resolve_query_binding(repository=repository, auth_manager=auth_manager,
            owner=owner, workspace_id=workspace_id, purpose="app_folder", session=None)
        authority = prepared["request"].get("authority", prepared["request"].get("expected_authority"))
        if authority is not None:
            _scope(authority, binding)
        invoke = getattr(provider, "code_visual", None)
        _require(callable(invoke), "unavailable")
        # Give provider its own copy; retain immutable expected request/binding.
        result = await invoke(binding=copy.deepcopy(binding), visual_request=copy.deepcopy(prepared))
    except asyncio.CancelledError:
        raise
    except CodeVisualError as exc:
        error = exc.code
    except MemoryRequestRejectedError as exc:
        error = exc.code if exc.code in ERROR_CODES else "unavailable"
    except Exception:
        error = "unavailable" if captured is not None else "denied"
    if captured is not None:
        try:
            _, current = resolve_query_binding(repository=repository, auth_manager=auth_manager,
                owner=owner, workspace_id=workspace_id, purpose="app_folder", session=None)
            _require(current == captured, "denied")
        except Exception:
            error = "denied"
    mode = prepared["mode"] if prepared else None
    observed = result.get("cost") if type(result) is dict else None
    if error is not None:
        return failure(error, mode=mode, observed_cost=observed)
    if type(result) is dict and result.get("error") is not None:
        native_error = result["error"]
        code = native_error.get("code") if type(native_error) is dict else "unavailable"
        return failure(code, mode=mode, observed_cost=observed)
    try:
        return project_visual_result(result, prepared, binding)
    except CodeVisualError as exc:
        return failure(exc.code, mode=mode, observed_cost=observed)

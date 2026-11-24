"""C04 trusted query accessor. No browser/model root or ambient authority.

Native code owns canonical metadata; Files owns bounded source bytes. This
module composes those owners, never creates an index or a parallel runner.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
from collections import OrderedDict
from pathlib import PurePosixPath

SCHEMA = "open-clank.code-query/v1"
OPERATIONS = frozenset("capabilities search overview catalog facets outline inspect neighbors path impact import_analysis dead_code_candidates graph_communities structural_twins backward_slice error_boundaries read pipeline".split())
CHANNELS = frozenset("exact identifier fuzzy lexical structural raw semantic".split())
FIELDS = frozenset("label source signature documentation occurrences resolution properties excerpt witnesses".split())
RELATION_KINDS = frozenset("call import value_reference type_reference inherits implements render route overrides manifest_dependency sql_table_reference callback_registration event_publish event_consume".split())
MAX_QUERY_WORK = 4_000_000
DEFAULT_BUDGETS = dict(rows=50, bytes=65536, tokens=8192, hops=4, work=MAX_QUERY_WORK)
LIMITS = dict(rows=(1, 200), bytes=(512, 262144), tokens=(512, 8192), hops=(0, 8), work=(1, MAX_QUERY_WORK))
PIPELINE_STAGE_LIMIT = 4
PIPELINE_RESERVE = 1024
PIPELINE_REASONS = frozenset({
    "row_limit", "partial_evidence", "budget_exceeded", "missing", "stale_cursor", "denied",
    "cancelled", "unavailable", "invalid_evidence", "unsupported", "erased", "busy",
})
_PIPELINE_LEAVES = {"find": "search", "deps": "neighbors", "outline": "outline"}
_PIPELINE_SEARCH_CHANNELS = frozenset({"exact", "identifier", "fuzzy", "lexical"})
_PIPELINE_RESOURCE = re.compile(r"(?:file|decl|occ):[0-9a-fA-F]{1,128}\Z")


class CodeQueryError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def code_binding_token(secret: str, *, owner: str, session_id: str, workspace_id: str, binding_revision: int) -> str:
    """Distinct purpose and unambiguous tuple; global memory token is invalid."""
    if not all(isinstance(v, str) and v for v in (secret, owner, session_id, workspace_id)) or type(binding_revision) is not int or binding_revision < 0:
        raise ValueError("Incomplete code binding")
    message = json.dumps(["open-clank-code-binding-v1", owner, session_id, workspace_id, binding_revision], separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def search_code_schema():
    """One leaf schema source used by native function and lifetools registry."""
    filter_schema = {"type": "object", "additionalProperties": False, "properties": {
        "rel_path": {"type": "string", "maxLength": 4096}, "path_prefix": {"type": "string", "maxLength": 4096},
        **{key: {"type": "array", "maxItems": 64, "items": {"type": "string", "maxLength": 128}} for key in ("kinds", "symbol_kinds", "languages", "states")},
        "relation_kinds": {"type":"array", "maxItems":len(RELATION_KINDS), "items":{"type":"string", "enum":sorted(RELATION_KINDS)}}}}
    receipt = {"type": "object", "additionalProperties": False, "required": ["workspace_id", "registration_id", "source_snapshot_id", "checkout_id", "head_generation", "admission_epoch", "policy_generation", "config_fingerprint"],
        "properties": {**{k: {"type": "string", "minLength": 1, "maxLength": 256} for k in ("workspace_id", "registration_id", "source_snapshot_id", "checkout_id", "head_generation", "config_fingerprint")},
            "admission_epoch": {"type": "integer", "minimum": 0}, "policy_generation": {"type": "integer", "minimum": 0}}}
    return {"type": "function", "function": {"name": "search_code", "description": "Read-only authorized active Workspace code: exact/identifier/metadata lexical search, conservative typo suggestions, catalog/outline, witnessed neighbors, incoming impact, directed paths, bounded exact-import cycles and hubs, and bounded straight-line Python backward value slices and heuristic same-dialect structural twins for one declaration seed (not semantic equivalence); occurrence spans inherit their declaration source locator. Explicit relative read windows use the existing Files owner. Search with channels=[raw] and no read descriptor performs bounded case-sensitive literal Source contents search of current admitted catalog file bytes; new paths require ordinary index refresh, partial results are explicit, no body FTS or source-proof claim. Metadata is not current source proof. Optional semantic ranking requires account consent for the configured Embeddings route and scores at most six current matching declaration excerpts on the first page. For library_api, query is one npm package name; it observes an on-disk declaration file in the active workspace, not a complete export or runtime-loading claim. No owner/root/index/erase inputs.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {
            "schema": {"type": "string", "enum": [SCHEMA]},
            "operation": {"type": "string", "enum": sorted(OPERATIONS | {"library_api"}), "default": "search"},
            "stages": {
                "type": "array", "minItems": 1, "maxItems": PIPELINE_STAGE_LIMIT,
                "description": "Pipeline stages: find first, then deps, outline or limit. Outline is terminal except limit.",
                "items": {"type": "object", "additionalProperties": False, "required": ["operation"], "properties": {
                    "operation": {"enum": ["find", "deps", "outline", "limit"]},
                    "query": {"type": "string", "maxLength": 1024},
                    "filter": filter_schema,
                    "direction": {"enum": ["incoming", "outgoing", "both"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                }},
            },
            "query": {"type": "string", "maxLength": 1024},
            "seed_ids": {"type": "array", "maxItems": 200, "items": {"type": "string", "pattern": "^(file|decl|occ):[0-9a-fA-F]{1,128}$"}},
            "target_id": {"type": "string", "pattern": "^(file|decl|occ):[0-9a-fA-F]{1,128}$"},
            "channels": {"type": "array", "maxItems": 7, "uniqueItems": True, "items": {"type": "string", "enum": sorted(CHANNELS)}},
            "fields": {"type": "array", "maxItems": 9, "uniqueItems": True, "items": {"type": "string", "enum": sorted(FIELDS)}},
            "filter": filter_schema, "direction": {"type": "string", "enum": ["incoming", "outgoing", "both"]},
            "budgets": {"type": "object", "additionalProperties": False, "properties": {k: {"type": "integer", "minimum": bounds[0], "maximum": bounds[1]} for k, bounds in LIMITS.items()}},
            "expected_receipt": receipt, "cursor": {"type": "string", "maxLength": 4096},
            "read": {"type": "object", "additionalProperties": False, "required": ["rel_path", "start_byte", "length"], "properties": {
                "rel_path": {"type": "string", "maxLength": 4096}, "start_byte": {"type": "integer", "minimum": 0}, "length": {"type": "integer", "minimum": 1, "maximum": 32768}, "continuation": {"type": "string", "maxLength": 4096}}},
            "request_id": {"type": "string", "maxLength": 128},
            "community_algorithm": {"type": "string", "enum": ["label_propagation", "modularity_refinement"],
                                    "description": "Optional algorithm for graph_communities. Defaults to label propagation."}}}}}



def validate_library_query(value):
    """Closed model input; the broker alone supplies its active workspace."""
    import re
    if (type(value) is not dict or set(value)!={'operation','query'} or value.get('operation')!='library_api'
            or type(value.get('query')) is not str or len(value['query'])>214 or '..' in value['query']
            or re.fullmatch(r'(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*',value['query']) is None):
        raise CodeQueryError('invalid','Invalid on-disk package request')
    return {'operation':'library_api','query':value['query']}


def unavailable_library():
    return {'schema':'open-clank.library-observation/v1','state':'unavailable','partial':True}

def _relative(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 4096 or "\0" in value or "\\" in value or value.startswith("/") or any(p in {"", ".", ".."} for p in value.rstrip("/").split("/")):
        raise CodeQueryError("invalid", "Invalid relative code resource")
    return value


def validate_query(query):
    if not isinstance(query, dict) or set(query) - {"schema", "operation", "query", "seed_ids", "target_id", "channels", "fields", "filter", "budgets", "expected_receipt", "read", "direction", "cursor", "stages", "request_id", "community_algorithm"}:
        raise CodeQueryError("invalid", "Invalid code query fields")
    q = copy.deepcopy(query)
    q.setdefault("schema", SCHEMA)
    q.setdefault("operation", "search")
    if q["schema"] != SCHEMA or not isinstance(q["operation"], str) or q["operation"] not in OPERATIONS:
        raise CodeQueryError("invalid", "Unsupported code operation")
    if "community_algorithm" in q:
        if (q["operation"] != "graph_communities"
                or q["community_algorithm"] not in ("label_propagation", "modularity_refinement")):
            raise CodeQueryError("unsupported", "Community algorithm requires a supported graph_communities algorithm")
    if q["operation"] != "pipeline" and q.get("stages"):
        raise CodeQueryError("unsupported", "Stages require the pipeline operation")
    for key, allowed, default in (("channels", CHANNELS, ["exact", "identifier"]), ("fields", FIELDS, ["label", "source", "properties"])):
        q.setdefault(key, default)
        if not isinstance(q[key], list) or len(q[key]) > len(allowed) or any(not isinstance(v, str) or v not in allowed for v in q[key]):
            raise CodeQueryError("invalid", "Invalid code field/channel selection")
    budgets = q.get("budgets", {})
    if not isinstance(budgets, dict) or set(budgets) - LIMITS.keys():
        raise CodeQueryError("invalid", "Invalid code budgets")
    budgets = {**DEFAULT_BUDGETS, **budgets}
    if any(type(v) is not int or not LIMITS[k][0] <= v <= LIMITS[k][1] for k, v in budgets.items()):
        raise CodeQueryError("invalid", "Code budget exceeds finite limits")
    q["budgets"] = budgets
    f = q.setdefault("filter", {})
    _filter(f)
    if f.get("relation_kinds"):
        if q["operation"] not in {"neighbors", "path", "impact"}:
            raise CodeQueryError("unsupported", "Relation filters require neighbors, path or impact")
    else:
        f.pop("relation_kinds", None)
    for key, maximum in (("query", 1024), ("cursor", 4096), ("request_id", 128)):
        if q.get(key) is not None and (not isinstance(q[key], str) or len(q[key].encode()) > maximum):
            raise CodeQueryError("invalid", "Invalid code query text")
    ids = q.setdefault("seed_ids", [])
    if not isinstance(ids, list) or len(ids) > 200:
        raise CodeQueryError("invalid", "Invalid code seeds")
    for value in ids + ([q["target_id"]] if q.get("target_id") is not None else []):
        if not isinstance(value, str) or not value.startswith(("file:", "decl:", "occ:")) or not 1 <= len(value.split(":", 1)[1]) <= 128 or any(c not in "0123456789abcdefABCDEF" for c in value.split(":", 1)[1]):
            raise CodeQueryError("invalid", "Expected typed code identity")
    if q["operation"] in {"dead_code_candidates", "graph_communities"} and (any(v for v in f.values()) or q.get("query") is not None
            or ids or q.get("target_id") is not None or q.get("direction") is not None or q.get("read") is not None):
        raise CodeQueryError("unsupported", "This analysis requires the full admitted snapshot")
    if q["operation"] == "error_boundaries":
        if (any(v for v in f.values()) or q.get("query") is not None or len(ids)!=1 or not ids[0].startswith("decl:") or q.get("expected_receipt") is None or q.get("target_id") is not None or q.get("direction") is not None or q.get("read") is not None):
            raise CodeQueryError("unsupported", "Error boundaries require one current-receipt function")
        q["fields"] = list(dict.fromkeys([*q["fields"], "label", "source", "properties"]))
    if q["operation"] == "backward_slice":
        if (any(v for v in f.values()) or q.get("query") is not None or len(ids)!=1 or not ids[0].startswith("occ:") or q.get("expected_receipt") is None or q.get("target_id") is not None or q.get("direction") is not None or q.get("read") is not None):
            raise CodeQueryError("unsupported", "Backward slice requires one current-receipt return-use occurrence")
        q["fields"] = list(dict.fromkeys([*q["fields"], "label", "source", "properties"]))
    if q["operation"] == "structural_twins":
        if (any(v for v in f.values()) or q.get("query") is not None or len(ids) != 1
                or not ids[0].startswith("decl:") or q.get("target_id") is not None
                or q.get("direction") is not None or q.get("read") is not None):
            raise CodeQueryError("unsupported", "Structural twins require one declaration seed and the full admitted snapshot")
        q["fields"] = list(dict.fromkeys([*q["fields"], "label", "source", "properties"]))
    if q.get("direction") is not None and (not isinstance(q["direction"], str) or q["direction"] not in {"incoming", "outgoing", "both"}):
        raise CodeQueryError("invalid", "Invalid code direction")
    if not isinstance(q.setdefault("stages", []), list):
        raise CodeQueryError("invalid", "Invalid code stages")
    if q["operation"] == "pipeline":
        _validate_pipeline(q)
    if q.get("read") is not None:
        r = q["read"]
        if not isinstance(r, dict) or set(r) - {"rel_path", "start_byte", "length", "continuation"}:
            raise CodeQueryError("invalid", "Invalid source window")
        _relative(r.get("rel_path"))
        if type(r.get("start_byte")) is not int or r["start_byte"] < 0 or type(r.get("length")) is not int or not 1 <= r["length"] <= 32768 or r["start_byte"] + r["length"] > 2**64 - 1:
            raise CodeQueryError("invalid", "Invalid bounded source window")
        if r.get("continuation") is not None and (not isinstance(r["continuation"], str) or not r["continuation"] or len(r["continuation"].encode()) > 4096):
            raise CodeQueryError("invalid", "Invalid source continuation")
    expected = q.get("expected_receipt")
    receipt_keys = {"workspace_id", "registration_id", "source_snapshot_id", "checkout_id", "head_generation", "admission_epoch", "policy_generation", "config_fingerprint"}
    if expected is not None and (not isinstance(expected, dict) or set(expected) != receipt_keys or any(not isinstance(expected[k], str) or not expected[k] or len(expected[k].encode()) > 256 for k in receipt_keys - {"admission_epoch", "policy_generation"}) or any(type(expected[k]) is not int or expected[k] < 0 for k in ("admission_epoch", "policy_generation"))):
        raise CodeQueryError("invalid", "Invalid expected code receipt")
    return q


def _validate_pipeline(query):
    stages = query["stages"]
    if not 1 <= len(stages) <= PIPELINE_STAGE_LIMIT:
        raise CodeQueryError("invalid", "A code pipeline needs one to four stages")
    if (query["seed_ids"] or query["filter"] or any(query.get(key) is not None for key in
            ("query", "target_id", "direction", "read", "cursor"))
            or not query["channels"]
            or any(channel not in _PIPELINE_SEARCH_CHANNELS for channel in query["channels"])):
        raise CodeQueryError("unsupported", "Pipeline input belongs to its typed stages")
    allowed = {
        "find": {"operation", "query", "filter"},
        "deps": {"operation", "direction", "filter"},
        "outline": {"operation"},
        "limit": {"operation", "limit"},
    }
    outline_seen = False
    for index, stage in enumerate(stages):
        operation = stage.get("operation") if isinstance(stage, dict) else None
        if (not isinstance(operation, str) or operation not in allowed or set(stage) - allowed[operation]
                or (operation == "find") != (index == 0) or (outline_seen and operation != "limit")):
            raise CodeQueryError("invalid", "Invalid code pipeline stage sequence")
        if operation in {"find", "deps"}:
            filter_value = stage.get("filter", {})
            _filter(filter_value)
            if operation == "find":
                text = stage.get("query")
                if not isinstance(text, str) or not text.strip() or len(text.encode()) > 1024 or filter_value.get("relation_kinds"):
                    raise CodeQueryError("invalid", "Find needs bounded search text and metadata filters")
            elif (filter_value.keys() - {"relation_kinds"}
                  or stage.get("direction", "outgoing") not in ("incoming", "outgoing", "both")):
                raise CodeQueryError("invalid", "Dependencies accept direction and relationship filters")
        if operation == "limit" and (type(stage.get("limit")) is not int or not 1 <= stage["limit"] <= 200):
            raise CodeQueryError("invalid", "Invalid code pipeline limit")
        outline_seen |= operation == "outline"
    leaves = sum(stage["operation"] in _PIPELINE_LEAVES for stage in stages)
    budget = query["budgets"]
    dependency_hops = sum(stage["operation"] == "deps" for stage in stages)
    if (min(budget["bytes"], budget["tokens"]) < PIPELINE_RESERVE + 512 * leaves
            or budget["work"] < len(stages) or budget["hops"] < dependency_hops):
        raise CodeQueryError("budget_exceeded", "Code pipeline budget is below its bounded minimum")


def _filter(value):
    if not isinstance(value, dict) or set(value) - {"rel_path", "path_prefix", "kinds", "symbol_kinds", "languages", "states", "relation_kinds"}:
        raise CodeQueryError("invalid", "Invalid code filter")
    relations=value.get("relation_kinds", [])
    if not isinstance(relations,list) or len(relations)>len(RELATION_KINDS) or any(not isinstance(v,str) or v not in RELATION_KINDS for v in relations):
        raise CodeQueryError("invalid", "Unknown code relation kind")
    for key in ("rel_path", "path_prefix"):
        if value.get(key) is not None:
            _relative(value[key])
    for key in ("kinds", "symbol_kinds", "languages", "states"):
        if key in value and (not isinstance(value[key], list) or len(value[key]) > 64 or any(not isinstance(v, str) or len(v.encode()) > 128 for v in value[key])):
            raise CodeQueryError("invalid", "Invalid code filter values")


def error_result(query, code, message):
    operation = query.get("operation", "search") if isinstance(query, dict) else "search"
    return dict(schema=SCHEMA, operation=operation if isinstance(operation, str) and operation in OPERATIONS else "search", receipt=None,
        capabilities=[], operation_capabilities=[], channels=[], hits=[], edges=[], paths=[], counts=[], facets={}, omissions=[],
        partial=False, degraded=True, reasons=[code], next_cursor=None, error={"code": code, "message": message},
        cost=dict(inspected_records=0, serialized_bytes=0, token_upper_bound=0, metadata_bytes=0, files_bytes_read=0, validation_bytes_read=0))


def measure_envelope(result, budgets):
    """All serialized UTF8 bytes upper-bound tokens, including wrappers."""
    cost = result.setdefault("cost", {})
    for _ in range(12):
        n = len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
        if cost.get("serialized_bytes") == n and cost.get("token_upper_bound") == n:
            if n > min(budgets["bytes"], budgets["tokens"]):
                raise CodeQueryError("budget_exceeded", "Complete code envelope exceeds budget")
            return result
        cost.update(serialized_bytes=n, token_upper_bound=n)
    raise CodeQueryError("unavailable", "Unstable code envelope size")


def finalize_error(query, code, message, *, observed_cost=None):
    """Typed errors obey the caller's valid budget, including late failures."""
    try:
        budgets = validate_query(query)["budgets"]
    except CodeQueryError:
        budgets = DEFAULT_BUDGETS
    failure = error_result(query, code, message)
    for key, value in (observed_cost or {}).items():
        if key not in {"serialized_bytes", "token_upper_bound"} and type(value) is int and value >= 0:
            failure["cost"][key] = value
    try:
        return measure_envelope(failure, budgets)
    except CodeQueryError:
        compact = {k: failure[k] for k in ("schema", "operation", "receipt", "error", "partial", "degraded")}
        compact["cost"] = {k: v for k, v in failure["cost"].items() if v and k not in {"serialized_bytes", "token_upper_bound"}}
        return measure_envelope(compact, budgets)


def resolve_query_binding(*, repository, auth_manager, owner, workspace_id, purpose, session=None):
    """Capture current query authority without admitting or indexing anything.

    Evidence and app-only visual reads share this resolver. Call it again after
    awaited work and compare the returned identity, including error outcomes.
    Client roots and account-wide administrator visibility are never inputs.
    """
    from src.openclank.workspace_policy_service import resolve_owned_workspace

    if not owner or purpose not in {"app_folder", "agent_workspace"}:
        raise CodeQueryError("denied", "Code workspace unavailable")
    if purpose == "agent_workspace" and (session is None or getattr(session, "owner", None) != owner or getattr(session, "workspace_id", None) != workspace_id or getattr(session, "archived", False)):
        raise CodeQueryError("denied", "Code chat binding unavailable")
    bound = resolve_owned_workspace(repository, workspace_id=workspace_id,
        owner_username=owner, auth_manager=auth_manager, purpose=purpose)
    generation = repository.generation()
    identity = (bound.workspace.id, bound.workspace.location_id, bound.workspace.revision, bound.path, generation)
    native = dict(owner=owner, owner_subject_id=bound.workspace.owner_subject_id,
        workspace_id=bound.workspace.id, location_id=bound.workspace.location_id,
        workspace_revision=bound.workspace.revision, policy_generation=generation, root=bound.path,
        subscriber_id="query:" + str(getattr(session, "id", "app")),
        origin="agent" if purpose == "agent_workspace" else "app")
    return native, identity


async def _run_pipeline(query, binding, invoke, assert_current):
    """Run bounded Native metadata leaves under one Host binding and receipt."""
    budgets = query["budgets"]
    stages = query["stages"]
    leaves = sum(stage["operation"] in _PIPELINE_LEAVES for stage in stages)
    leaf_budget = {
        **budgets,
        **{key: (budgets[key] - PIPELINE_RESERVE) // leaves for key in ("bytes", "tokens")},
    }
    receipt = query.get("expected_receipt")
    totals = error_result(query, "unavailable", "Code pipeline unavailable")["cost"]
    reports = []
    output = None
    failure = None
    any_partial = False
    any_degraded = False
    for index, stage in enumerate(stages, 1):
        assert_current()
        operation = stage["operation"]
        hits = output["hits"] if output is not None else []
        work_left = budgets["work"] - totals["inspected_records"]
        stage_work = work_left // (len(stages) - index + 1)
        observed_work = None
        reason = None
        if stage_work < 1:
            failure = "budget_exceeded"
        elif operation == "limit":
            observed_work = len(hits) + len(output["edges"])
            if observed_work > stage_work:
                observed_work = 0
                failure = "budget_exceeded"
            elif len(hits) > stage["limit"]:
                output["hits"] = hits[:stage["limit"]]
                retained = {hit["id"] for hit in output["hits"]}
                output["edges"] = [edge for edge in output["edges"]
                                   if edge.get("from") in retained and edge.get("to") in retained]
                reason = "row_limit"
            totals["inspected_records"] += observed_work
        else:
            leaf = {
                "schema": SCHEMA, "operation": _PIPELINE_LEAVES[operation],
                "channels": query["channels"] if operation == "find" else [],
                "fields": query["fields"],
                "filter": stage.get("filter", {}),
                "budgets": {**leaf_budget, "work": stage_work},
                "expected_receipt": receipt,
            }
            if operation == "find":
                leaf["query"] = stage["query"]
            else:
                seeds = list(dict.fromkeys(hit["id"] for hit in hits if hit["id"].startswith(("file:", "decl:"))))
                if not seeds or (operation == "outline" and len(seeds) != 1):
                    failure = "missing" if not seeds else "unsupported"
                leaf["seed_ids"] = seeds
                if operation == "deps":
                    leaf["direction"] = stage.get("direction", "outgoing")
            if failure is None:
                try:
                    reply = await invoke(binding=binding, query=validate_query(leaf))
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    failure = code if isinstance(code, str) and code in PIPELINE_REASONS else "unavailable"
                    reply = None
                assert_current()
                if isinstance(reply, dict):
                    cost = reply.get("cost")
                    work = cost.get("inspected_records") if isinstance(cost, dict) else None
                    if type(work) is int and 0 <= work <= MAX_QUERY_WORK:
                        observed_work = work
                        for key in (*totals.keys(), "metadata_records_validated", "native_rpc_calls_completed"):
                            amount = cost.get(key)
                            if key not in {"serialized_bytes", "token_upper_bound"} and type(amount) is int and amount >= 0:
                                totals[key] = totals.get(key, 0) + amount
                    error = reply.get("error")
                    if error is not None:
                        code = error.get("code") if isinstance(error, dict) else None
                        failure = code if isinstance(code, str) and code in PIPELINE_REASONS else "unavailable"
                    elif (reply.get("schema") != SCHEMA or reply.get("operation") != leaf["operation"]
                          or observed_work is None or observed_work > stage_work):
                        failure = "invalid_evidence"
                    elif (not isinstance(reply.get("receipt"), dict)
                          or reply["receipt"].get("workspace_id") != binding["workspace_id"]
                          or (receipt is not None and reply["receipt"] != receipt)):
                        failure = "stale_cursor"
                    elif (not isinstance(reply.get("hits"), list) or len(reply["hits"]) > budgets["rows"]
                          or any(not isinstance(hit, dict) or not isinstance(hit.get("id"), str)
                                 or not _PIPELINE_RESOURCE.fullmatch(hit["id"]) for hit in reply["hits"])
                          or not isinstance(reply.get("edges"), list)
                          or any(not isinstance(edge, dict) for edge in reply["edges"])):
                        failure = "invalid_evidence"
                    else:
                        receipt = reply["receipt"]
                        if reply.get("next_cursor"):
                            reason = "row_limit"
                        elif reply.get("partial") or reply.get("degraded"):
                            reason = "partial_evidence"
                        any_degraded |= bool(reply.get("degraded"))
                        output = reply
                elif failure is None:
                    failure = "invalid_evidence"
        any_partial |= reason is not None
        reports.append({
            "index": index, "operation": operation,
            "state": "unavailable" if failure else "truncated" if reason else "complete",
            "reason": failure or reason, "inspected_records": observed_work,
            "hits": len(output["hits"]) if output is not None and failure is None else 0,
        })
        if failure:
            break

    if output is None:
        output = error_result(query, failure or "unavailable", "Code pipeline unavailable")
    elif failure:
        output["error"] = {"code": failure, "message": "Code pipeline stopped at an unavailable stage"}
    output.pop("outline_completeness", None)
    output.update(operation="pipeline", pipeline_stages=reports, cost=totals, next_cursor=None,
                  partial=any_partial or (failure is not None and len(reports) > 1),
                  degraded=any_degraded or failure is not None)
    if any(report["inspected_records"] is None for report in reports):
        output["reasons"] = [*output.get("reasons", []), "pipeline_work_not_observed"]
    return output


_PROJECT_SEARCH_PLANS = OrderedDict()


def _merge_search_cost(page, other):
    for name, value in (other.get("cost") or {}).items():
        if type(value) is int and value >= 0 and name not in {"serialized_bytes", "token_upper_bound"}:
            cost = page.setdefault("cost", {})
            cost[name] = cost.get(name, 0) + value
    return page


async def _indexed_search_page(invoke, binding, query, current):
    """Keep a bounded declaration page when broader project matching runs out."""
    cursor = query.get("cursor") or ""
    broad_prefix = "project-page:"
    declarations_prefix = "project-decls:"
    budgets = query["budgets"]
    if (query["operation"] != "search" or any(query["filter"].values())
            or set(query["channels"]) != {"exact", "identifier", "fuzzy", "lexical"}
            or not query.get("query") or budgets["work"] < 40 or min(budgets["bytes"], budgets["tokens"]) < 2048
            or (cursor and not cursor.startswith((broad_prefix, declarations_prefix)))):
        return await invoke(binding=binding, query=query)

    reserve = min(100_000, budgets["work"] // 40)
    if cursor in {broad_prefix, declarations_prefix}:
        raise CodeQueryError("stale_cursor", "Missing project page continuation")
    shape = {k: v for k, v in query.items() if k not in {"cursor", "request_id", "expected_receipt"}}
    key = hashlib.sha256(json.dumps([binding, shape], sort_keys=True, default=str).encode()).digest()
    hint = _PROJECT_SEARCH_PLANS.get(key) if not cursor else None
    if query.get("expected_receipt") is not None and hint != query["expected_receipt"]:
        hint = None
    # The hint stores a receipt, never rows. Read declarations again against
    # current authority, and use the plan only if this read proves the same head.
    declarations = {**query, "filter": {**query["filter"], "kinds": ["declaration"]}, "channels": ["exact", "fuzzy"], "budgets": {
        **budgets, "work": reserve, "bytes": budgets["bytes"] - 512, "tokens": budgets["tokens"] - 512}}
    if cursor.startswith(declarations_prefix):
        declarations["cursor"] = cursor[len(declarations_prefix):]
    limited = {}
    if not cursor.startswith(broad_prefix):
        limited = await invoke(binding=binding, query=declarations)
        if not isinstance(limited, dict):
            return limited
        failure = limited.get("error") or {}
        if failure and (failure.get("code") != "budget_exceeded" or cursor):
            return limited
    use_hint = bool(hint and limited.get("hits") and limited.get("receipt") == hint)
    result = {}
    if not use_hint and not cursor.startswith(declarations_prefix):
        if hint:
            _PROJECT_SEARCH_PLANS.pop(key, None)
        if limited and not current():
            raise CodeQueryError("denied", "Code workspace authority changed")
        broad = {**query, "budgets": {**budgets, "work": budgets["work"] - reserve,
                                     "bytes": budgets["bytes"] - 32, "tokens": budgets["tokens"] - 32}}
        if limited.get("receipt"):
            broad["expected_receipt"] = limited["receipt"]
        if cursor:
            broad["cursor"] = cursor[len(broad_prefix):]
        result = await invoke(binding=binding, query=broad)
        if not isinstance(result, dict):
            return result
        if result.get("next_cursor"):
            result["next_cursor"] = broad_prefix + result["next_cursor"]
        if cursor or (result.get("error") or {}).get("code") != "budget_exceeded" or not limited.get("hits"):
            return _merge_search_cost(result, limited)
    if not cursor and isinstance(limited.get("receipt"), dict):
        _PROJECT_SEARCH_PLANS[key] = limited["receipt"]
        _PROJECT_SEARCH_PLANS.move_to_end(key)
        if len(_PROJECT_SEARCH_PLANS) > 128:
            _PROJECT_SEARCH_PLANS.popitem(last=False)
    limited.update(partial=True, degraded=True)
    if limited.get("next_cursor"):
        limited["next_cursor"] = declarations_prefix + limited["next_cursor"]
    limited.setdefault("reasons", []).append("exact_and_fuzzy_declarations_only_under_project_search_budget")
    for channel in limited.get("channels", []):
        if channel.get("channel") in query["channels"] and channel.get("channel") not in declarations["channels"]:
            channel.update(requested=True, error={"code": "budget_exceeded", "message": "Broad matching exceeded its work budget"})
    for count in limited.get("counts", []):
        if count.get("name") in {"eligible", "matched", "matched_units", "omitted"}:
            count.update(value=None, exactness="unknown")
    return _merge_search_cost(limited, result)


async def query_code(*, repository, provider, auth_manager, owner, workspace_id, purpose, session=None, query, _outline_current=None, _outline_sink=None):
    """Public trusted accessor shared by Graph and friendly tools.

    A caller supplies authenticated objects, never a model-selected root. Every
    outcome, including Files/native errors, rechecks the original binding.
    """
    from src.openclank.workspace_policy_service import WorkspacePolicyServiceError
    from src.memory_provider import MemoryRequestRejectedError
    captured = None
    q = None
    result = None
    def resolve():
        return resolve_query_binding(repository=repository, auth_manager=auth_manager,
            owner=owner, workspace_id=workspace_id, purpose=purpose, session=session)
    try:
        q = validate_query(query)
        native_binding, captured = resolve()
        invoke = getattr(provider, "code_query", None)
        if not callable(invoke):
            raise CodeQueryError("unavailable", "Code query provider unavailable")
        wants_contents = q["operation"] == "search" and "raw" in q["channels"] and q.get("read") is None
        if q["operation"] == "pipeline":
            def assert_current():
                if resolve() != (native_binding, captured):
                    raise CodeQueryError("denied", "Code workspace authority changed")
            result = await _run_pipeline(q, native_binding, invoke, assert_current)
        elif wants_contents:
            from src.openclank.code_source_search import search_contents
            result = error_result(q, "unavailable", "Source contents unavailable")
            await search_contents(result=result, query=q, binding=native_binding,
                repository=repository, provider=provider, auth_manager=auth_manager,
                session=session, purpose=purpose)
        else:
            native_query = copy.deepcopy(q)
            wants_read = q["operation"] == "read" or ("raw" in q["channels"] and q.get("read") is not None)
            if wants_read:
                if q.get("read") is None:
                    raise CodeQueryError("invalid", "A bounded relative source window is required")
                f = q["filter"]
                rel = q["read"]["rel_path"]
                if (f.get("rel_path") is not None and f["rel_path"] != rel) or (f.get("path_prefix") is not None and not rel.startswith(f["path_prefix"])):
                    raise CodeQueryError("missing", "Source window is outside selected filters")
                if any(f.get(key) for key in ("kinds", "symbol_kinds", "languages", "states")):
                    raise CodeQueryError("unsupported", "Raw windows support relative path filters only")
                # Metadata locator only, internal fields are projected away below.
                native_query.update(operation="catalog", fields=["label", "properties"], cursor=None)
                native_query["filter"]["rel_path"] = q["read"]["rel_path"]
            if q["operation"] in {"import_analysis", "dead_code_candidates", "graph_communities", "structural_twins"}:
                # Older providers cannot parse additive operations. Probe the
                # existing capability contract and bind the analysis to its head.
                capability_query = copy.deepcopy(native_query)
                capability_query.update(operation="capabilities", cursor=None)
                capabilities = await invoke(binding=native_binding, query=capability_query)
                if not isinstance(capabilities, dict) or capabilities.get("schema") != SCHEMA or capabilities.get("error"):
                    raise CodeQueryError("unavailable", "Code provider capability proof unavailable")
                if not any(isinstance(item, dict) and item.get("operation") == q["operation"] and item.get("available") is True for item in capabilities.get("operation_capabilities", [])):
                    raise CodeQueryError("unsupported", "This code provider does not support the requested analysis")
                if not isinstance(capabilities.get("receipt"), dict):
                    raise CodeQueryError("unavailable", "Code provider receipt unavailable")
                native_query["expected_receipt"] = capabilities["receipt"]
            wants_semantic = q["operation"] == "search" and "semantic" in q["channels"] and not wants_read
            if wants_semantic and not any(lane in native_query["channels"] for lane in ("exact", "identifier", "lexical", "fuzzy")):
                native_query["channels"].extend(("exact", "identifier", "lexical"))
            result = await _indexed_search_page(invoke, native_binding, native_query, lambda: resolve() == (native_binding, captured))
            if not isinstance(result, dict):
                raise CodeQueryError("unavailable", "Invalid code query response")
            failure = result.get("error")
            if failure is not None:
                code = failure.get("code", "unavailable") if isinstance(failure, dict) else "unavailable"
                if not (wants_read and code == "missing"):
                    raise CodeQueryError(code, "Code query unavailable")
            if result.get("schema") != SCHEMA:
                failure = result.get("error") or {}
                if wants_read and failure.get("code") == "missing":
                    result = error_result(q, "missing", "No canonical code index; independent authorized raw window")
                else:
                    raise CodeQueryError(failure.get("code", "unavailable"), "Code query unavailable")
            if wants_semantic:
                from src.openclank.code_vector_retrieval import rerank_code_page
                result = await rerank_code_page(baseline=result, query=native_query, binding=native_binding,
                    repository=repository, provider=provider, auth_manager=auth_manager, session=session,
                    purpose=purpose, current_binding=lambda: resolve() == (native_binding, captured))
            if wants_read:
                result = await _read_source(result=result, query=q, binding=native_binding, repository=repository, provider=provider, auth_manager=auth_manager, session=session, purpose=purpose, _outline_current=_outline_current, _outline_sink=_outline_sink)
            elif q["operation"] == "inspect":
                from src.openclank.code_compiler_evidence import enrich_compiler_scope
                await enrich_compiler_scope(result=result, query=q, binding=native_binding,
                    repository=repository, provider=provider, auth_manager=auth_manager, session=session, purpose=purpose)
                from src.openclank.code_scip import enrich_scip
                await enrich_scip(result=result, query=q, binding=native_binding,
                    repository=repository, provider=provider, auth_manager=auth_manager, session=session, purpose=purpose)
    except asyncio.CancelledError:
        raise
    except CodeQueryError as exc:
        prior_cost = (result or {}).get("cost", {})
        result = error_result(q or query, exc.code, str(exc))
        for key, value in prior_cost.items():
            if key not in {"serialized_bytes", "token_upper_bound"} and type(value) is int and value >= 0:
                result["cost"][key] = value
    except WorkspacePolicyServiceError:
        observed = (result or {}).get("cost", {})
        result = error_result(q or query, "denied", "Code workspace unavailable")
        result["cost"].update({k: v for k, v in observed.items() if k not in {"serialized_bytes", "token_upper_bound"} and type(v) is int and v >= 0})
    except MemoryRequestRejectedError as exc:
        observed = (result or {}).get("cost", {})
        result = error_result(q or query, exc.code if exc.code in {"busy", "denied", "erased", "stale_cursor", "cancelled", "invalid"} else "denied", "Code workspace unavailable")
        result["cost"].update({k: v for k, v in observed.items() if k not in {"serialized_bytes", "token_upper_bound"} and type(v) is int and v >= 0})
    except Exception:
        observed = (result or {}).get("cost", {})
        result = error_result(q or query, "unavailable", "Code workspace query unavailable")
        result["cost"].update({k: v for k, v in observed.items() if k not in {"serialized_bytes", "token_upper_bound"} and type(v) is int and v >= 0})
    if captured is not None:
        try:
            _, current = resolve()
            if captured != current:
                raise CodeQueryError("stale_cursor", "Code workspace authority changed")
        except Exception:
            return finalize_error(q or query, "denied", "Code workspace authority changed", observed_cost=(result or {}).get("cost"))
    try:
        return measure_envelope(result, (q or {}).get("budgets", DEFAULT_BUDGETS))
    except CodeQueryError as exc:
        budgets = (q or {}).get("budgets", DEFAULT_BUDGETS)
        return finalize_error(q or query, exc.code, "Code result exceeds envelope budget", observed_cost=result.get("cost"))


_RAW_CURSORS = OrderedDict()
_RAW_CURSOR_LIMIT = 256
_FILES_IO_LIMIT = 2 * 1024 * 1024
_FULL_FILE_LIMIT = 1024 * 1024


def _files_client(*, binding, repository, auth_manager, session, purpose):
    from src.openclank.files_service_client import agent_client_for, client_for_owner
    if purpose == "agent_workspace":
        return agent_client_for(binding["owner"], binding["root"], workspace_id=binding["workspace_id"], chat_id=str(session.id))
    # Current canonical read resolution has already admitted this Location and
    # Workspace. Never use account-wide visibility or the admin host bypass:
    # the Files owner must contain every resolved target in this exact folder.
    location = binding["location_id"]
    scope = dict(host=False, visible_root_ids=[location], capabilities=["read"],
        root_capabilities={location: ["read"]}, generation=binding["policy_generation"],
        active_folder=dict(id="code-" + binding["workspace_id"], root_id=location,
            canonical_path=binding["root"], capabilities=["read"]))
    return client_for_owner(binding["owner"], app_scope=scope)


async def _source_guard(*, binding, repository, provider, auth_manager, session, purpose, expected, captured_fence, cost, accounting=None, budgets=None):
    from src.openclank.workspace_policy_service import resolve_owned_workspace, WorkspacePolicyServiceError
    if purpose == "agent_workspace" and (session is None or getattr(session, "owner", None) != binding["owner"] or getattr(session, "workspace_id", None) != binding["workspace_id"] or getattr(session, "archived", False)):
        raise CodeQueryError("denied", "Code chat binding changed")
    try:
        current = resolve_owned_workspace(repository, workspace_id=binding["workspace_id"], owner_username=binding["owner"], auth_manager=auth_manager, purpose=purpose)
    except WorkspacePolicyServiceError:
        raise CodeQueryError("denied", "Code source authority unavailable") from None
    if (current.workspace.owner_subject_id != binding["owner_subject_id"] or current.workspace.location_id != binding["location_id"] or current.workspace.revision != binding["workspace_revision"] or current.path != binding["root"] or repository.generation() != binding["policy_generation"]):
        raise CodeQueryError("denied", "Code source binding changed")
    fence = await provider.code_read_fence(binding=binding, expected_receipt=expected, **({"budgets": budgets} if budgets is not None else {}))
    if accounting is not None: accounting(fence.get("cost") if isinstance(fence, dict) else None)
    cost["source_fence_checks_completed"] = cost.get("source_fence_checks_completed", 0) + 1
    if isinstance(fence, dict):
        rpcs = (fence.get("cost") or {}).get("native_rpc_calls_completed", 0)
        if type(rpcs) is int and rpcs >= 0:
            cost["native_rpc_calls_completed"] = cost.get("native_rpc_calls_completed", 0) + rpcs
        if isinstance(fence.get("fence"), str):
            fence = fence["fence"]
    if not isinstance(fence, str):
        error = fence.get("error", {}) if isinstance(fence, dict) else {}
        raise CodeQueryError(error.get("code", "unavailable"), "Code source admission unavailable")
    if captured_fence is not None and fence != captured_fence:
        raise CodeQueryError("stale_cursor", "Code source admission changed")
    return fence


def _window_bytes(body, start, length, *, base=0):
    """End-exclusive safe UTF8 window; never claim codepoint offsets are bytes."""
    begin = max(0, start - base)
    end = min(len(body), begin + length)
    while begin < end and body[begin] & 0xC0 == 0x80:
        begin += 1
    while end > begin:
        try:
            return body[begin:end].decode("utf-8"), base + begin, base + end
        except UnicodeDecodeError as exc:
            if exc.end == end - begin and end - begin - exc.start <= 3:
                end -= 1
            else:
                raise CodeQueryError("unsupported", "Source window is not valid UTF8") from None
    return "", base + begin, base + begin


def _span(body, start, end):
    def point(offset):
        prefix = body[:offset]
        return prefix.count(b"\n") + 1, offset - (prefix.rfind(b"\n") + 1)
    a, b = point(start), point(end)
    return dict(start_byte=start, end_byte=end, start_line=a[0], start_column=a[1], end_line=b[0], end_column=b[1])


def _raw_binding_key(binding, query, receipt):
    read = dict(query["read"])
    read.pop("start_byte", None)
    read.pop("continuation", None)
    public = {k: v for k, v in query.items() if k not in {"read", "cursor", "expected_receipt"}}
    return json.dumps([binding, public, read, receipt], sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def _read_source(*, result, query, binding, repository, provider, auth_manager, session, purpose, _outline_current=None, _outline_sink=None):
    read = query["read"]
    rel = _relative(read["rel_path"])
    expected = result.get("receipt")
    cost = result.setdefault("cost", {})
    totals = {"work": 0, "bytes": 0, "tokens": 0, "rpcs": 0}
    observed = [True]
    tracking = callable(_outline_current) and callable(_outline_sink) and query.get("operation") == "read"
    def account(value):
        if not tracking: return
        if not isinstance(value, dict): observed[0] = False; return
        fields = {"work": "inspected_records", "bytes": "serialized_bytes", "tokens": "token_upper_bound", "rpcs": "native_rpc_calls_completed"}
        for target, source_key in fields.items():
            n = value.get(source_key)
            if type(n) is not int or n < 0: observed[0] = False; return
            totals[target] += n
    account(dict(cost))
    captured_fence = None
    async def guard(extra_budgets=None):
        nonlocal captured_fence
        captured_fence = await _source_guard(binding=binding, repository=repository, provider=provider, auth_manager=auth_manager, session=session, purpose=purpose, expected=expected, captured_fence=captured_fence, cost=cost, accounting=account if tracking else None, budgets=extra_budgets)
    key = _raw_binding_key(binding, query, expected)
    old_cursor = None
    if read.get("continuation") is not None:
        old_cursor = _RAW_CURSORS.get(read["continuation"])
        if old_cursor is None or old_cursor["key"] != key or old_cursor["offset"] != read["start_byte"]:
            raise CodeQueryError("stale_cursor", "Invalid or stale source continuation")
        captured_fence = old_cursor["fence"]
    client = _files_client(binding=binding, repository=repository, auth_manager=auth_manager, session=session, purpose=purpose)
    path = os.path.join(binding["root"], *PurePosixPath(rel).parts)
    await guard()
    opened = await client.open_handle(path)
    handle, tag, size = opened.get("handle"), opened.get("object_tag"), opened.get("size")
    if not isinstance(handle, dict) or not isinstance(tag, str) or type(size) is not int or size < 0:
        raise CodeQueryError("unavailable", "Invalid authorized source handle")
    if old_cursor and (old_cursor["object_tag"] != tag or old_cursor["size"] != size or old_cursor["modified"] != opened.get("modified_unix_ms")):
        raise CodeQueryError("stale_cursor", "Source changed between windows")
    if read["start_byte"] > size:
        raise CodeQueryError("invalid", "Source offset is past EOF")
    full = size <= _FULL_FILE_LIMIT
    offset = 0 if full else read["start_byte"]
    length = size if full else min(read["length"], max(0, size - offset))
    io_bytes = 0
    async def acquire():
        nonlocal io_bytes
        await guard()
        response = await client.request_handle("read_range", handle, dict(offset=offset, length=length, include_fingerprint=False))
        data = response.get("data") or {}
        raw = data.get("bytes")
        if not isinstance(raw, list) or any(type(b) is not int or not 0 <= b <= 255 for b in raw) or len(raw) > length or data.get("offset") != offset:
            raise CodeQueryError("unavailable", "Invalid bounded Files response")
        body = bytes(raw)
        io_bytes += len(body)
        cost["files_bytes_read"] = io_bytes
        if io_bytes > _FILES_IO_LIMIT:
            raise CodeQueryError("budget_exceeded", "Files source acquisition budget exceeded")
        return body
    body = await acquire()
    result.setdefault("cost", {})["files_bytes_read"] = io_bytes
    if full and len(body) != size:
        raise CodeQueryError("stale_cursor", "Source size changed during read")
    if full and old_cursor and old_cursor.get("full_hash") != hashlib.sha256(body).hexdigest():
        raise CodeQueryError("stale_cursor", "Source content changed between windows")
    text, start, end = _window_bytes(body, read["start_byte"], read["length"], base=offset)
    if start == end and read["start_byte"] < size:
        raise CodeQueryError("unsupported", "No complete UTF8 character in selected window")
    # The second read is from the SAME authorized descriptor and counts toward
    # the cap. Full-file SHA256 is claimed only when both complete byte reads
    # match. Large files receive range identity only, never whole-file hash.
    second = await acquire()
    result["cost"]["files_bytes_read"] = io_bytes
    if second != body:
        raise CodeQueryError("stale_cursor", "Source changed during read")
    await guard()
    reopened = await client.open_handle(path)
    if reopened.get("object_tag") != tag or reopened.get("size") != size:
        raise CodeQueryError("stale_cursor", "Source path changed during read")
    file_row = next((h for h in result.get("hits", []) if h.get("kind") == "file"), None)
    canonical_hash = ((file_row or {}).get("properties") or {}).get("source_hash")
    canonical_id = (file_row or {}).get("id")
    source = None
    verified = False
    state = "observed_file" if full else "observed_range"
    validation_bytes = 0
    if expected and full and canonical_id and canonical_hash:
        await guard()
        source = dict(file_id=canonical_id.removeprefix("file:"), rel_path=rel, source_hash=canonical_hash, span=_span(body, start, end))
        claim = dict(source=source, predicate={"kind": "exact_text", "text": text})
        vquery = dict(schema=SCHEMA, operation="capabilities", expected_receipt=expected, fields=[], channels=[], budgets=query["budgets"])
        validation = await provider.code_query(binding=binding, query=vquery, source_claim=claim)
        account(validation.get("cost") if isinstance(validation, dict) else None)
        result["cost"]["native_rpc_calls_completed"] = result["cost"].get("native_rpc_calls_completed", 0) + (validation.get("cost") or {}).get("native_rpc_calls_completed", 0)
        validation_bytes = (validation.get("cost") or {}).get("validation_bytes_read", 0)
        result["cost"]["validation_bytes_read"] = validation_bytes
        if validation.get("error"):
            raise CodeQueryError(validation["error"].get("code", "unavailable"), "Code source validation unavailable")
        receipt = validation.get("receipt")
        if receipt != expected:
            raise CodeQueryError("stale_cursor", "Code source receipt changed")
        validation_state = (validation.get("source_validation") or {}).get("state")
        verified = validation_state == "current"
        state = "current" if verified else "stale" if validation_state == "stale" else "observed_file"
        if not verified:
            source = None
    await guard()
    continuation = None
    if end < size and end > read["start_byte"]:
        continuation = secrets.token_urlsafe(24)
        _RAW_CURSORS[continuation] = dict(key=key, fence=captured_fence, offset=end, object_tag=tag, size=size, modified=opened.get("modified_unix_ms"), full_hash=hashlib.sha256(body).hexdigest() if full else None)
        while len(_RAW_CURSORS) > _RAW_CURSOR_LIMIT:
            _RAW_CURSORS.popitem(last=False)
    if old_cursor is not None:
        _RAW_CURSORS.pop(read["continuation"], None)
    if full:
        digest = hashlib.sha256(body).hexdigest()
        hash_scope = "file"
    else:
        digest = hashlib.sha256(text.encode()).hexdigest()
        hash_scope = "byte_range"
    # Explicit-window literal search preserves literal '*' bytes. It does not
    # claim corpus completeness or regex support from a selected range.
    matches = []
    if query["operation"] == "search":
        needle = query.get("query") or ""
        if needle and needle in text:
            matches = [dict(channel="raw", rank=1, ordering_score=300, explanation="literal case-sensitive bytes in selected Files UTF8 window; not corpus search")]
    hit = dict(id=canonical_id or "raw:" + hashlib.sha256(json.dumps([binding["owner_subject_id"], binding["workspace_id"], binding["location_id"], binding["workspace_revision"], rel]).encode()).hexdigest(),
        kind="raw_window", evidence_state=state, matches=matches, current_source_verified=verified)
    fields = query["fields"]
    if "label" in fields:
        hit["label"] = rel
    if "source" in fields and source is not None:
        hit["source"] = source
    if "excerpt" in fields:
        hit["excerpt"] = text
        hit["raw_source"] = dict(rel_path=rel, start_byte=start, end_byte=end, hash_algorithm="sha256", hash_scope=hash_scope, hash=digest, state="observed_file" if full else "observed_range", file_size=size, continuation=continuation)
    if "properties" in fields:
        hit["properties"] = dict(rel_path=rel, window_complete=end >= size, indexed_source_hash=canonical_hash, source_observation="bounded_nonatomic_read")
    result.update(operation=query["operation"], hits=[hit] if query["operation"] == "read" or matches else [], edges=[], paths=[], next_cursor=None, partial=end < size, error=None)
    result.setdefault("reasons", []).append("selected_window_only_not_corpus_search")
    if not full:
        result["reasons"].append("large_range_continuation_is_nonatomic_progress_only")
    # Source progression remains available on an empty literal-match page.
    # It is not a graph/native cursor and never implies a complete corpus.
    if continuation:
        result["reasons"].append("source_window_has_more")
    result["source_window"] = {"rel_path": rel, "start_byte": start, "end_byte": end, "continuation": continuation, "continuity": "full_sha256" if full else "progress_only_nonatomic"}
    result["reasons"] = [r for r in result["reasons"] if r != "authorized_files_owner_required"]
    result["counts"] = [dict(name="returned", value=len(result["hits"]), unit="source_window", exactness="exact"), dict(name="corpus_matches", value=None, unit="source_window", exactness="unknown")]
    result.setdefault("cost", {}).update(files_bytes_read=io_bytes, validation_bytes_read=validation_bytes)
    for capability in result.get("capabilities", []):
        if capability.get("channel") == "raw":
            capability.update(available=True, reason=None)
    for capability in result.get("operation_capabilities", []):
        if capability.get("operation") == "read":
            capability.update(available=True, reason=None)
    result["channels"] = [dict(channel=channel, requested=True, enabled=channel == "raw", executed=channel == "raw", cancelled=False, error=None if channel == "raw" else "selected_window_raw_only", producer="authorized-files-read-range-v1" if channel == "raw" else None, config_fingerprint=None, candidate_count=1 if channel == "raw" else 0, inspected_records=1 if channel == "raw" else 0, elapsed_micros=None) for channel in dict.fromkeys([*query["channels"], "raw"])]
    prior_capabilities = {c["channel"]: c for c in result.get("capabilities", [])}
    result["capabilities"] = [dict(channel=channel, available=True, reason=None) if channel == "raw" else prior_capabilities.get(channel, dict(channel=channel, available=False, reason="no_canonical_index")) for channel in sorted(CHANNELS)]
    prior_operations = {c["operation"]: c for c in result.get("operation_capabilities", [])}
    result["operation_capabilities"] = [dict(operation=operation, available=True, reason="selected_window_only") if operation in {"read", "search"} and not prior_operations.get(operation, {}).get("available") else prior_operations.get(operation, dict(operation=operation, available=False, reason="no_canonical_index")) for operation in sorted(OPERATIONS)]
    result["omissions"] = [dict(field=f, reason="not_requested") for f in sorted(FIELDS - set(fields))]
    if tracking and verified and full and start == 0 and end == size and size > 0 and {"source", "excerpt", "properties"}.issubset(fields) and canonical_id and canonical_id.startswith("file:") and expected:
        # Optional work is strictly subordinate to the ordinary read result.
        # Keep its private cost/accounting out of the canonical result.
        saved_cost = dict(cost)
        baseline = dict(totals)
        baseline_io = io_bytes
        attempted = False
        accepted_outline = None
        try:
            async def current():
                value = _outline_current()
                return await value if hasattr(value, "__await__") else value
            reserved = [0]
            def residual():
                if not observed[0]: raise ValueError("outline cost unavailable")
                budgets = dict(query["budgets"])
                budgets["work"] -= totals["work"]
                budgets["bytes"] -= totals["bytes"] + reserved[0]
                budgets["tokens"] -= totals["tokens"] + reserved[0]
                budgets["rows"] -= 1
                if any(budgets[k] < LIMITS[k][0] for k in ("work", "bytes", "tokens", "rows")):
                    raise ValueError("outline budget unavailable")
                return budgets
            if await current() is not True: raise ValueError("outline authority unavailable")
            # Reserve the actual serialized original envelope plus sidecar framing.
            original_bytes = len(json.dumps({"output": json.dumps(result, ensure_ascii=False, separators=(",", ":")), "exit_code": 0}, ensure_ascii=False, separators=(",", ":")).encode())
            reserved[0] = original_bytes + 512
            outline_query = dict(schema=SCHEMA, operation="outline", seed_ids=[canonical_id], filter={"rel_path": rel},
                fields=["label", "source", "signature", "properties"], channels=[], budgets=residual(), expected_receipt=expected)
            attempted = True
            outline = await provider.code_query(binding=binding, query=outline_query)
            account(outline.get("cost") if isinstance(outline, dict) else None)
            if (not isinstance(outline, dict) or outline.get("schema") != SCHEMA or outline.get("operation") != "outline"
                    or outline.get("error") is not None or outline.get("receipt") != expected
                    or outline.get("degraded") is not False or outline.get("next_cursor") is not None
                    or not isinstance(outline.get("hits"), list) or not outline["hits"] or len(outline["hits"]) + 1 > query["budgets"]["rows"]):
                raise ValueError("outline evidence unavailable")
            from src.openclank.code_outline_witness import verify_outline_completeness
            if not verify_outline_completeness(outline, receipt=expected, file_id=canonical_id, rel_path=rel, source_hash=canonical_hash):
                raise ValueError("outline completeness unavailable")
            for declaration in outline["hits"]:
                locator = declaration.get("source") if isinstance(declaration, dict) else None
                if (not isinstance(declaration, dict) or declaration.get("kind") != "declaration" or not isinstance(declaration.get("signature"), str)
                        or not isinstance(locator, dict) or locator.get("file_id") != canonical_id.removeprefix("file:")
                        or locator.get("rel_path") != rel or locator.get("source_hash") != canonical_hash):
                    raise ValueError("outline source mismatch")
            await guard(residual())
            if io_bytes + size > _FILES_IO_LIMIT or await acquire() != body: raise ValueError("outline source changed")
            await guard(residual())
            latest = await client.open_handle(path)
            if latest.get("object_tag") != tag or latest.get("size") != size: raise ValueError("outline path changed")
            check_query = dict(schema=SCHEMA, operation="capabilities", expected_receipt=expected, fields=[], channels=[], budgets=residual())
            checked = await provider.code_query(binding=binding, query=check_query, source_claim=claim)
            account(checked.get("cost") if isinstance(checked, dict) else None)
            if (not isinstance(checked, dict) or checked.get("error") or checked.get("receipt") != expected
                    or (checked.get("source_validation") or {}).get("state") != "current" or await current() is not True):
                raise ValueError("outline source authority changed")
            residual()  # All observed native work and serialized responses fit.
            accepted_outline = outline
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed private operation may have consumed unobserved work.
            observed[0] = False
        finally:
            cost.clear(); cost.update(saved_cost)
            if attempted:
                extra = {key: totals[key]-baseline[key] for key in totals}
                extra["files_bytes_read"] = io_bytes-baseline_io
                extra["complete"] = observed[0]
                try: _outline_sink({"result": accepted_outline, "extraCost": extra})
                except Exception: pass
    return result


async def observe_library_scoped(*, repository, provider, auth_manager, owner, workspace, purpose, session, package, deadline, current, max_output_bytes=65536):
    """One current bounded ephemeral package observation under existing Files authority."""
    import re
    import time
    if (purpose not in {'app_folder','agent_workspace'} or not callable(current) or type(deadline) not in (int,float)
            or type(max_output_bytes) is not int or not 1024<=max_output_bytes<=65536 or type(package) is not str
            or len(package)>214 or re.fullmatch(r'(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*',package) is None or '..' in package):return None
    try:
        async with asyncio.timeout(max(0,deadline-time.monotonic())):
            binding,identity=resolve_query_binding(repository=repository,auth_manager=auth_manager,owner=owner,workspace_id=workspace,purpose=purpose,session=session)
            def live():
                try:return current() is True and resolve_query_binding(repository=repository,auth_manager=auth_manager,owner=owner,workspace_id=workspace,purpose=purpose,session=session)[1]==identity
                except Exception:return False
            if not live():return None
            observed=await provider._host_code_library_observe(owner=owner,binding=binding,package=package,deadline=deadline,current=live)
            if (not live() or type(observed) is not dict or set(observed)!={'schema','state','workspace_id','package','manifest','declaration','partial','reasons'}
                    or observed.get('schema')!='open-clank.library-observation/v1' or observed.get('state')!='current'
                    or observed.get('workspace_id')!=workspace or observed.get('package')!=package or observed.get('partial') is not True
                    or observed.get('reasons')!=['manifest_declared_types_only','exports_and_runtime_unproved']):return None
            manifest,decl=observed['manifest'],observed['declaration']; prefix='node_modules/'+package+'/'
            if (type(manifest) is not dict or set(manifest)!={'rel_path','sha256','size','name','version'} or manifest.get('rel_path')!=prefix+'package.json'
                    or manifest.get('name')!=package or (manifest.get('version') is not None and (type(manifest['version']) is not str or len(manifest['version'].encode())>512 or any(ord(c)<32 or ord(c)==127 for c in manifest['version'])))
                    or type(decl) is not dict or set(decl)!={'rel_path','sha256','size','items','omitted'}):return None
            rel=decl.get('rel_path'); tail=rel[len(prefix):] if type(rel) is str and rel.startswith(prefix) else ''
            if (not tail.endswith('.d.ts') or len(tail)>512 or not tail.isascii() or len(tail.split('/'))>32
                    or any(not part or part.startswith('.') or part=='node_modules' or re.fullmatch(r'[A-Za-z0-9_.-]+',part) is None for part in tail.split('/'))):return None
            for item in (manifest,decl):
                if type(item.get('sha256')) is not str or re.fullmatch(r'[0-9a-f]{64}',item['sha256']) is None or type(item.get('size')) is not int or not 0<=item['size']<=131072:return None
            if type(decl['items']) is not list or len(decl['items'])>128 or type(decl['omitted']) is not int or not 0<=decl['omitted']<=20000:return None
            kinds={'function','method','class','struct','enum','trait','interface','type_alias','module','constant','static','implementation','function_value'}
            span_keys={'start_byte','end_byte','start_line','end_line','start_column','end_column'}
            for item in decl['items']:
                if (type(item) is not dict or set(item)!={'kind','name','signature','span'} or item.get('kind') not in kinds
                        or type(item.get('name')) is not str or len(item['name'].encode())>256 or any(ord(c)<32 or ord(c)==127 for c in item['name'])
                        or type(item.get('signature')) is not str or len(item['signature'].encode())>1024
                        or any((ord(c)<32 and c!='\t') or ord(c)==127 for c in item['signature'])):return None
                span=item['span']
                if (type(span) is not dict or set(span)!=span_keys or any(type(span[k]) is not int or span[k]<0 for k in span_keys)
                        or not 0<=span['start_byte']<=span['end_byte']<=decl['size'] or span['start_line']<1 or span['end_line']<span['start_line']):return None
            client=_files_client(binding=binding,repository=repository,auth_manager=auth_manager,session=session,purpose=purpose)
            async def verify(item):
                if not live():return False
                path=os.path.join(binding['root'],*item['rel_path'].split('/'))
                opened=await client.open_handle(path);handle=opened.get('handle');tag=opened.get('object_tag');size=opened.get('size')
                if type(handle) is not dict or type(tag) is not str or type(size) is not int or size!=item['size']:return False
                async def acquire():
                    if not live():raise ValueError('authority changed')
                    if size==0:return b''
                    result=await client.request_handle('read_range',handle,dict(offset=0,length=size,include_fingerprint=False))
                    data=result.get('data') or {};raw=data.get('bytes')
                    if data.get('offset')!=0 or type(raw) is not list or len(raw)!=size or any(type(x) is not int or not 0<=x<=255 for x in raw):raise ValueError('file changed')
                    return bytes(raw)
                first=await acquire();second=await acquire();reopened=await client.open_handle(path)
                return live() and first==second and hashlib.sha256(first).hexdigest()==item['sha256'] and reopened.get('object_tag')==tag and reopened.get('size')==size
            if not await verify(manifest) or not await verify(decl) or not live():return None
            # Agent results must fit the ordinary tool envelope; preserve explicit omission.
            encoded=lambda:len(json.dumps(observed,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
            truncated=False
            while encoded()>max_output_bytes and decl['items']:
                decl['items'].pop();decl['omitted']+=1;truncated=True
            if truncated:observed['reasons'].append('declaration_budget_truncated')
            while encoded()>max_output_bytes and decl['items']:
                decl['items'].pop();decl['omitted']+=1
            if encoded()>max_output_bytes or not live():return None
            return observed
    except asyncio.CancelledError:
        raise
    except Exception:
        return None

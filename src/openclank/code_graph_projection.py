"""Pure C04 Graph projection; no reads, identity inference or new authority.

Keep the canonical envelope and source payloads once. Client nodes/edge IDs
are derived views. A deterministic reserve pays for projection/omission JSON;
never discard retained hits and forward a cursor past them.
"""
from __future__ import annotations

import copy
import json
import re
from src.openclank.code_query import MAX_QUERY_WORK, PIPELINE_REASONS, PIPELINE_STAGE_LIMIT

SCHEMA = "open-clank.code-query/v1"
RESERVE = 1024  # UTF-8 bytes AND conservative byte-token upper bound.
FIELDS = {"label", "source", "signature", "documentation", "occurrences", "resolution", "properties", "excerpt", "witnesses"}
OPERATIONS = {"capabilities", "overview", "catalog", "facets", "outline", "inspect", "neighbors", "path", "impact", "read", "search", "structural_twins", "backward_slice", "error_boundaries", "pipeline"}
QUERY_KEYS = {"schema", "operation", "query", "seed_ids", "target_id", "channels", "fields", "filter", "budgets", "expected_receipt", "read", "cursor", "stages", "request_id", "direction"}
DEFAULT_BUDGET = {"rows": 50, "bytes": 65536, "tokens": 8192, "hops": 4, "work": MAX_QUERY_WORK}
MAX_BUDGET = {"rows": 200, "bytes": 262144, "tokens": 8192, "hops": 8, "work": MAX_QUERY_WORK}
TOP_FIELDS = {"schema", "operation", "receipt", "capabilities", "operation_capabilities", "channels", "hits", "edges", "paths", "counts", "facets", "omissions", "partial", "degraded", "reasons", "next_cursor", "error", "cost", "source_window", "outline_completeness", "slice_analysis", "error_boundary_analysis", "pipeline_stages"}
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
    if query.get("operation") == "error_boundaries":
        seeds=query.get("seed_ids")
        if not isinstance(seeds,list) or len(seeds)!=1 or not isinstance(seeds[0],str) or not seeds[0].startswith("decl:") or not CANONICAL_ID.fullmatch(seeds[0]) or query.get("expected_receipt") is None:
            raise CodeGraphProjectionError("Error boundaries require a current-receipt function seed")
        fields=list(dict.fromkeys([*fields,"label","source","properties"]))
    if query.get("operation") == "backward_slice":
        seeds=query.get("seed_ids")
        if not isinstance(seeds,list) or len(seeds)!=1 or not isinstance(seeds[0],str) or not seeds[0].startswith("occ:") or not CANONICAL_ID.fullmatch(seeds[0]) or query.get("expected_receipt") is None:
            raise CodeGraphProjectionError("Backward slice requires a current-receipt occurrence seed")
        fields=list(dict.fromkeys([*fields,"label","source","properties"]))
    if query.get("operation") == "structural_twins":
        seeds = query.get("seed_ids")
        if (not isinstance(seeds, list) or len(seeds) != 1 or not isinstance(seeds[0], str)
                or not seeds[0].startswith("decl:") or not CANONICAL_ID.fullmatch(seeds[0])):
            raise CodeGraphProjectionError("Structural twins require one declaration seed")
        fields = list(dict.fromkeys([*fields, "label", "source", "properties"]))
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
    keys = {"from", "to", "occurrence_id", "relation_kind", "resolution_state"} | (fields & {"source", "witnesses"})
    if (any(not isinstance(edge.get(key), str) for key in ("from", "to", "occurrence_id", "resolution_state"))
            or any(not CANONICAL_ID.fullmatch(edge[key]) for key in ("from", "to", "occurrence_id"))
            or not edge["occurrence_id"].startswith("occ:")
            or not isinstance(edge.get("relation_kind"), str)
            or edge["relation_kind"] not in {"call", "import", "value_reference", "type_reference", "inherits", "implements", "render", "route", "overrides", "manifest_dependency", "sql_table_reference", "callback_registration", "event_publish", "event_consume"}
            or ("witnesses" in edge and (not isinstance(edge["witnesses"], list) or any(not isinstance(row, dict) for row in edge["witnesses"])))):
        raise CodeGraphProjectionError("Invalid code evidence edge identity", code="invalid_evidence")
    return {key: _known_nested(value, fields) for key, value in edge.items() if key in keys}


def _structural_hit(hit: dict, seed: str) -> None:
    """Keep this heuristic's typed witness envelope closed and source-bound."""
    value = hit.get("properties", {}).get("structuralTwin") if isinstance(hit.get("properties"), dict) else None
    source = hit.get("source")
    if (hit.get("kind") != "declaration" or not isinstance(hit.get("id"), str) or not hit["id"].startswith("decl:")
            or not isinstance(value, dict) or set(value) != {"version", "role", "dialect", "grammarVersion", "shapeSHA256", "occurrences"}
            or type(value.get("version")) is not int or value["version"] != 1 or value.get("role") not in {"seed", "candidate"}
            or (value.get("role") == "seed") != (hit.get("id") == seed)
            or value.get("dialect") not in {"rust", "python", "typescript", "tsx", "javascript"}
            or not isinstance(value.get("grammarVersion"), str) or not 1 <= len(value["grammarVersion"]) <= 128
            or not isinstance(value.get("shapeSHA256"), str) or re.fullmatch(r"[0-9a-f]{64}", value["shapeSHA256"]) is None
            or not isinstance(value.get("occurrences"), list) or len(value["occurrences"]) > 200
            or not _structural_locator(source)):
        raise CodeGraphProjectionError("Invalid structural evidence", code="invalid_evidence")
    ids = set()
    for occurrence in value["occurrences"]:
        if (not isinstance(occurrence, dict) or set(occurrence) != {"occurrenceID", "kind", "syntaxKind", "span"}
                or not isinstance(occurrence.get("occurrenceID"), str) or re.fullmatch(r"[0-9a-f]{64}", occurrence["occurrenceID"]) is None
                or occurrence["occurrenceID"] in ids or occurrence.get("kind") not in {"call", "value_reference", "type_reference", "import_reference"}
                or not isinstance(occurrence.get("syntaxKind"), str) or not 1 <= len(occurrence["syntaxKind"]) <= 128
                or not _structural_span(occurrence.get("span"))):
            raise CodeGraphProjectionError("Invalid structural witness", code="invalid_evidence")
        witness = occurrence["span"]
        if (witness["start_byte"] < source["span"]["start_byte"]
                or witness["end_byte"] > source["span"]["end_byte"]):
            raise CodeGraphProjectionError("Structural witness is outside its declaration", code="invalid_evidence")
        ids.add(occurrence["occurrenceID"])


def _structural_locator(value):
    if not isinstance(value, dict) or set(value) != {"file_id", "rel_path", "source_hash", "span"}:
        return False
    if any(not isinstance(value.get(k), str) or re.fullmatch(r"[0-9a-f]{64}", value[k]) is None for k in ("file_id", "source_hash")):
        return False
    path = value.get("rel_path")
    if not isinstance(path, str) or not path or len(path.encode("utf-8")) > 4096 or path.startswith("/") or "\\" in path or "\0" in path or any(p in {"", ".", ".."} for p in path.split("/")):
        return False
    return _structural_span(value.get("span"))


def _structural_span(span):
    keys = {"start_byte", "end_byte", "start_line", "start_column", "end_line", "end_column"}
    return (isinstance(span, dict) and set(span) == keys and all(type(span[k]) is int and span[k] >= 0 for k in keys)
            and span["start_byte"] <= span["end_byte"] and span["start_line"] >= 1 and span["end_line"] >= span["start_line"])


_FLOW_EDGES = {
    "statement": ("next",),
    "condition": ("true", "false"),
    "return": ("return",),
    "raise": ("raise",),
    "exit": (),
}


def _function_flow(hit, query):
    properties = hit["properties"]
    flow = properties["functionFlow"]
    source = hit.get("source")
    invalid = CodeGraphProjectionError("Invalid lexical function flow", code="invalid_evidence")
    if (query.get("operation") != "inspect" or hit["id"] not in query.get("seed_ids", ())
            or hit["kind"] != "declaration" or not _structural_locator(source)
            or properties.get("language") != "python" or properties.get("declaration_kind") not in ("function", "method", "route")
            or properties.get("source_hash") != source["source_hash"] or properties.get("rel_path") != source["rel_path"]
            or not isinstance(flow, dict) or type(flow.get("version")) is not int or flow["version"] != 1
            or flow.get("profile") != "python_lexical_flow_v1"):
        raise invalid
    base = {"version", "profile", "status", "blocks", "edges"}
    status = flow.get("status")
    if status != "complete":
        reasons = {
            "unsupported": ("unsupported_syntax", "profile_limit", "ambiguous_binding"),
            "unavailable": ("indexed_profile_unavailable", "response_budget"),
        }
        if (set(flow) != base | {"reason"} or not isinstance(status, str)
                or flow["reason"] not in reasons.get(status, ())
                or flow["blocks"] != [] or flow["edges"] != []):
            raise invalid
        return
    blocks, edges = flow.get("blocks"), flow.get("edges")
    if (set(flow) != base | {"entry"} or not isinstance(blocks, list) or not 1 <= len(blocks) <= 24
            or not isinstance(edges, list) or len(edges) > 48 or type(flow["entry"]) is not int
            or not 0 <= flow["entry"] < len(blocks)):
        raise invalid
    outer = source["span"]
    previous_end = outer["start_byte"]
    expected_edges = []
    exit_id = len(blocks) - 1
    for index, block in enumerate(blocks):
        if (not isinstance(block, dict) or set(block) != {"id", "kind", "span"}
                or type(block["id"]) is not int or block["id"] != index
                or not isinstance(block["kind"], str) or block["kind"] not in _FLOW_EDGES
                or not _structural_span(block["span"])):
            raise invalid
        span = block["span"]
        is_exit = block["kind"] == "exit"
        if (is_exit != (index == exit_id) or is_exit != (span["start_byte"] == span["end_byte"])
                or is_exit and span["end_byte"] != outer["end_byte"]
                or not previous_end <= span["start_byte"] <= span["end_byte"] <= outer["end_byte"]
                or not outer["start_line"] <= span["start_line"] <= span["end_line"] <= outer["end_line"]
                or (span["start_line"], span["start_column"]) > (span["end_line"], span["end_column"])):
            raise invalid
        previous_end = span["end_byte"]
        expected_edges.extend((index, kind) for kind in _FLOW_EDGES[block["kind"]])
    if len(edges) != len(expected_edges):
        raise invalid
    for edge, (index, kind) in zip(edges, expected_edges):
        if (not isinstance(edge, dict) or set(edge) != {"from", "to", "kind"}
                or type(edge["from"]) is not int or edge["from"] != index or edge["kind"] != kind
                or type(edge["to"]) is not int or not 0 <= edge["to"] < len(blocks)
                or kind in ("return", "raise") and edge["to"] != exit_id):
            raise invalid


def _scip_observation(hit, query):
    observation = hit["properties"]["scipObservation"]
    error = CodeGraphProjectionError("Invalid SCIP evidence", code="invalid_evidence")
    if (query.get("operation") != "inspect" or hit["kind"] != "occurrence"
            or query.get("seed_ids") != [hit["id"]] or not _structural_locator(hit.get("source"))
            or not isinstance(observation, dict)
            or set(observation) != {"version", "profile", "status", "assertion", "index_sha256", "producer", "candidates", "coverage"}
            or type(observation["version"]) is not int or observation["version"] != 1
            or observation["profile"] != "selected_scip_binary_v1" or observation["assertion"] != "index_declared"
            or observation["status"] not in ("observed", "ambiguous", "unresolved")
            or observation["coverage"] not in ("partial", "selected_index_only")
            or not isinstance(observation["index_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", observation["index_sha256"])):
        raise error
    producer, rows = observation["producer"], observation["candidates"]
    if (not isinstance(producer, dict) or set(producer) - {"name", "version"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.@/+ -]{1,96}", value)
                   for value in producer.values())
            or not isinstance(rows, list) or len(rows) > 6
            or observation["status"] == "observed" and (len(rows) != 1 or observation["coverage"] != "selected_index_only")
            or observation["status"] == "ambiguous" and len(rows) < 2
            or observation["status"] == "unresolved" and (len(rows) > 1 or observation["coverage"] != "partial")):
        raise error
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"kind", "id", "source"} or row["kind"] != "definition"
                or not _structural_locator(row["source"]) or row["id"] != "file:" + row["source"]["file_id"]
                or row["source"]["span"]["start_byte"] >= row["source"]["span"]["end_byte"]):
            raise error


def _slice_analysis(value, query):
    fail=lambda: CodeGraphProjectionError("Invalid local slice evidence", code="invalid_evidence")
    keys={"version","profile","function_id","seed_occurrence_id","source","return_span","root_definition_id","units"}
    if not isinstance(value,dict) or set(value)!=keys or type(value["version"]) is not int or value["version"]!=1 or value["profile"]!="python_straight_line_v1" or query.get("seed_ids")!=["occ:"+str(value.get("seed_occurrence_id"))] or not _structural_locator(value.get("source")) or not _structural_span(value.get("return_span")) or not isinstance(value.get("units"),list) or len(value["units"])>64:
        raise fail()
    for key in ("function_id","seed_occurrence_id","root_definition_id"):
        if not isinstance(value[key],str) or re.fullmatch(r"[0-9a-f]{64}",value[key]) is None: raise fail()
    parent=value["source"]["span"]
    def inside(span): return _structural_span(span) and parent["start_byte"]<=span["start_byte"]<=span["end_byte"]<=parent["end_byte"]
    if not inside(value["return_span"]): raise fail()
    seen=set(); use_ids=set(); count=0
    for unit in value["units"]:
        if not isinstance(unit,dict) or set(unit) not in ({"id","kind","name","target_span","uses"},{"id","kind","name","target_span","statement_span","uses"}) or not isinstance(unit.get("id"),str) or re.fullmatch(r"[0-9a-f]{64}",unit["id"]) is None or unit["id"] in seen or unit.get("kind") not in {"parameter","assignment"} or not isinstance(unit.get("name"),str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}",unit["name"]) is None or not inside(unit.get("target_span")) or not isinstance(unit.get("uses"),list) or len(unit["uses"])>16: raise fail()
        seen.add(unit["id"]); statement=unit.get("statement_span")
        if unit["kind"]=="parameter":
            if statement is not None or unit["uses"]: raise fail()
        elif not inside(statement) or not (statement["start_byte"]<=unit["target_span"]["start_byte"]<=unit["target_span"]["end_byte"]<=statement["end_byte"]): raise fail()
        for use in unit["uses"]:
            if not isinstance(use,dict) or set(use)!={"occurrence_id","span","definition_id"} or any(not isinstance(use.get(k),str) or re.fullmatch(r"[0-9a-f]{64}",use[k]) is None for k in ("occurrence_id","definition_id")) or use["occurrence_id"] in use_ids or not inside(use.get("span")) or statement is None or not (unit["target_span"]["end_byte"]<=use["span"]["start_byte"]<=use["span"]["end_byte"]<=statement["end_byte"]): raise fail()
            use_ids.add(use["occurrence_id"]);count+=1
    if count>256: raise fail()

def _error_boundaries(value, query):
    fail=lambda: CodeGraphProjectionError("Invalid error boundary evidence",code="invalid_evidence")
    if not isinstance(value,dict) or set(value)!={"version","profile","function_id","source","regions"} or value.get("version")!=1 or type(value.get("version")) is not int or value.get("profile")!="python_try_except_v1" or query.get("seed_ids")!=["decl:"+str(value.get("function_id"))] or re.fullmatch(r"[0-9a-f]{64}",str(value.get("function_id"))) is None or not _structural_locator(value.get("source")) or not isinstance(value.get("regions"),list) or len(value["regions"])>64: raise fail()
    parent=value["source"]["span"]
    def inside(span,outer=parent): return _structural_span(span) and outer["start_byte"]<=span["start_byte"]<span["end_byte"]<=outer["end_byte"]
    starts=set();handlers=0;calls=0
    for region in value["regions"]:
        if not isinstance(region,dict) or not {"region","body","handlers","calls"}<=set(region) or set(region)-{"region","body","handlers","calls","else_clause","finally_clause"} or not inside(region.get("region")) or not inside(region.get("body"),region["region"]) or region["region"]["start_byte"] in starts or not isinstance(region.get("handlers"),list) or not region["handlers"] or not isinstance(region.get("calls"),list): raise fail()
        starts.add(region["region"]["start_byte"]);previous=region["body"]["end_byte"]
        for clause in [*region["handlers"], *([region["else_clause"]] if "else_clause" in region else []), *([region["finally_clause"]] if "finally_clause" in region else [])]:
            if not inside(clause,region["region"]) or clause["start_byte"]<previous: raise fail()
            previous=clause["end_byte"]
        handlers+=len(region["handlers"]); calls+=len(region["calls"]);ids=set()
        for call in region["calls"]:
            if not isinstance(call,dict) or set(call)!={"id","span"} or re.fullmatch(r"[0-9a-f]{64}",str(call.get("id"))) is None or call["id"] in ids or not inside(call.get("span"),region["body"]): raise fail()
            ids.add(call["id"])
    if handlers>128 or calls>256: raise fail()

def _measure(result: dict) -> int:
    cost = result.setdefault("cost", {})
    for _ in range(8):
        size = len(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        if cost.get("serialized_bytes") == size and cost.get("token_upper_bound") == size:
            return size
        cost.update(serialized_bytes=size, token_upper_bound=size)
    raise CodeGraphProjectionError("Unstable code graph envelope", code="invalid_evidence")


def _pipeline_stages(value, requested, failed):
    fields = {"index", "operation", "state", "reason", "inspected_records", "hits"}
    if (not isinstance(value, list) or not isinstance(requested, list)
            or not 1 <= len(value) <= min(len(requested), PIPELINE_STAGE_LIMIT)
            or (not failed and len(value) != len(requested))):
        raise CodeGraphProjectionError("Invalid code pipeline stages", code="invalid_evidence")
    for index, stage in enumerate(value, 1):
        if (not isinstance(stage, dict) or set(stage) != fields
                or type(stage["index"]) is not int or stage["index"] != index
                or stage["operation"] != requested[index - 1].get("operation")
                or type(stage["hits"]) is not int or not 0 <= stage["hits"] <= 200
                or (stage["inspected_records"] is not None and (
                    type(stage["inspected_records"]) is not int or not 0 <= stage["inspected_records"] <= MAX_QUERY_WORK))):
            raise CodeGraphProjectionError("Invalid code pipeline stage result", code="invalid_evidence")
        state, reason = stage["state"], stage["reason"]
        last_failed = index == len(value) and failed
        if ((state == "unavailable") != last_failed
                or state not in ("complete", "truncated", "unavailable")
                or (state == "complete" and reason is not None)
                or (state == "truncated" and reason not in ("row_limit", "partial_evidence"))
                or (state == "unavailable" and (not isinstance(reason, str) or reason not in PIPELINE_REASONS or stage["hits"]))
                or (state != "unavailable" and stage["inspected_records"] is None)):
            raise CodeGraphProjectionError("Invalid code pipeline stage outcome", code="invalid_evidence")


def project_code_graph(result: dict, *, query: dict, caller_budget: dict) -> dict:
    """Retain C04 identity/receipts; truly omit unrequested fields, including edges."""
    if not isinstance(result, dict) or result.get("schema") != SCHEMA:
        raise CodeGraphProjectionError("Invalid code query envelope", code="invalid_evidence")
    if result.get("error") is not None:
        error = result["error"]
        if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
            raise CodeGraphProjectionError("Invalid code query error", code="invalid_evidence")
    else:
        mandatory = TOP_FIELDS - {"next_cursor", "source_window", "outline_completeness", "slice_analysis", "error_boundary_analysis", "pipeline_stages"}
        if not mandatory <= result.keys() or type(result["partial"]) is not bool or type(result["degraded"]) is not bool:
            raise CodeGraphProjectionError("Incomplete code query success", code="invalid_evidence")
    if query.get("operation") == "pipeline":
        reports = result.get("pipeline_stages")
        if reports is None and result.get("error") is None:
            raise CodeGraphProjectionError("Missing code pipeline stages", code="invalid_evidence")
        if reports is not None:
            _pipeline_stages(reports, query.get("stages"), result.get("error") is not None)
    elif "pipeline_stages" in result:
        raise CodeGraphProjectionError("Unexpected code pipeline stages", code="invalid_evidence")
    if result.get("error") is None and query.get("operation")=="backward_slice":
        if result.get("partial") is not True or "syntactic_straight_line_only" not in result.get("reasons",[]): raise CodeGraphProjectionError("Invalid slice coverage",code="invalid_evidence")
        _slice_analysis(result.get("slice_analysis"),query)
    elif "slice_analysis" in result:
        raise CodeGraphProjectionError("Unexpected slice evidence",code="invalid_evidence")
    if result.get("error") is None and query.get("operation")=="error_boundaries":
        if result.get("partial") is not True or "syntactic_error_boundaries_only" not in result.get("reasons",[]) or query.get("expected_receipt")!=result.get("receipt"): raise CodeGraphProjectionError("Invalid error boundary coverage",code="invalid_evidence")
        _error_boundaries(result.get("error_boundary_analysis"),query)
    elif "error_boundary_analysis" in result:
        raise CodeGraphProjectionError("Unexpected error boundary evidence",code="invalid_evidence")
    if "outline_completeness" in result:
        from src.openclank.code_outline_witness import verify_outline_completeness
        witness=result["outline_completeness"]
        if (not isinstance(witness,dict) or query.get("operation")!="outline" or query.get("expected_receipt")!=result.get("receipt") or query.get("seed_ids")!=[witness.get("file_id")] or query.get("filter",{}).get("rel_path")!=witness.get("rel_path") or not verify_outline_completeness(result,receipt=result.get("receipt"),file_id=witness.get("file_id"),rel_path=witness.get("rel_path"),source_hash=witness.get("source_hash"))):
            raise CodeGraphProjectionError("Invalid selected-file outline witness", code="invalid_evidence")
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
               and (hit.get("kind") == "raw_window" and result.get("operation") in {"read", "search"}
                    or hit.get("kind") == "raw_file_match" and result.get("operation") == "search" and query.get("channels") == ["raw"] and query.get("read") is None))
        if not isinstance(hit, dict) or not isinstance(hit.get("kind"), str) or not isinstance(hit.get("id"), str) or (not raw and not CANONICAL_ID.fullmatch(hit["id"])):
            raise CodeGraphProjectionError("Invalid canonical code identity", code="invalid_evidence")
        candidate=hit.get("properties",{}).get("backwardSlice") if isinstance(hit.get("properties"),dict) else None
        if candidate is not None:
            if not isinstance(candidate,dict) or type(candidate.get("version")) is not int or candidate.get("version")!=1 or candidate.get("status") not in {"available","unsupported","unavailable"} or set(candidate)!=( {"version","status","seed"} if candidate.get("status")=="available" else {"version","status"}) or candidate.get("status")=="available" and (not isinstance(candidate.get("seed"),str) or re.fullmatch(r"occ:[0-9a-f]{64}",candidate["seed"]) is None): raise CodeGraphProjectionError("Invalid slice capability",code="invalid_evidence")
        if isinstance(hit.get("properties"), dict) and "functionFlow" in hit["properties"]:
            _function_flow(hit, query)
        if isinstance(hit.get("properties"), dict) and "scipObservation" in hit["properties"]:
            _scip_observation(hit, query)
        if result.get("operation") == "structural_twins":
            _structural_hit(hit, query["seed_ids"][0])
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

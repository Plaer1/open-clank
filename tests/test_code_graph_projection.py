import copy
import json

import pytest

from src.openclank.code_graph_projection import (
    CodeGraphProjectionError, prepare_graph_query, project_code_graph,
)


def envelope():
    return {"schema": "open-clank.code-query/v1", "operation": "overview",
        "receipt": {"workspace_id": "ws", "registration_id": "registration", "source_snapshot_id": "snapshot", "checkout_id": "checkout", "head_generation": "g1", "admission_epoch": 1, "policy_generation": 1, "config_fingerprint": "config", "owner": "private-owner", "root": "/private"},
        "hits": [{"id": "decl:abc", "kind": "function", "label": "target", "source": {"rel_path": "src/a.py", "source_hash": "hash"}, "excerpt": "secret body", "properties": {"custom": {"unknown": [1, 2]}}, "matches": [], "evidence_state": "current", "current_source_verified": False, "root": "/private"}],
        "edges": [{"from": "decl:a", "to": "decl:abc", "occurrence_id": "occ:1", "resolution_state": "exact", "source": {"rel_path": "src/a.py"}, "witnesses": [{"detail": "witness"}]}],
        "paths": [], "counts": [{"name": "eligible", "value": None, "unit": "files", "exactness": "unknown"}],
        "capabilities": [], "operation_capabilities": [], "channels": [], "facets": {}, "omissions": [], "partial": False, "degraded": False, "reasons": [], "next_cursor": "real-cursor", "error": None, "cost": {"inspected_records": 4}, "private_root": "/private"}


def test_projection_omits_fields_without_reconstructing_identity_or_metadata():
    original = envelope()
    before = copy.deepcopy(original)
    query, caller = prepare_graph_query({"operation": "overview", "fields": ["properties"]})
    result = project_code_graph(original, query=query, caller_budget=caller)
    assert original == before
    assert result["domain"] == "code"
    assert result["hits"][0]["id"] == original["hits"][0]["id"]
    assert result["hits"][0]["properties"] == original["hits"][0]["properties"]
    assert not {"source", "label", "excerpt", "root"} & result["hits"][0].keys()
    assert "source" not in result["edges"][0] and "witnesses" not in result["edges"][0]
    assert "owner" not in result["receipt"] and "root" not in result["receipt"]
    assert "private_root" not in result
    assert result["next_cursor"] == "real-cursor"
    assert result["cost"]["serialized_bytes"] == len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
    assert result["cost"]["token_upper_bound"] == result["cost"]["serialized_bytes"]


def test_reserve_is_deterministic_and_caller_budget_is_not_mutated():
    source = {"operation": "catalog", "budgets": {"bytes": 8192, "tokens": 4096}}
    query, caller = prepare_graph_query(source)
    assert source["budgets"] == {"bytes": 8192, "tokens": 4096}
    assert caller["bytes"] == 8192 and caller["tokens"] == 4096
    assert query["budgets"]["bytes"] == 7168 and query["budgets"]["tokens"] == 3072
    assert prepare_graph_query(source) == (query, caller)


@pytest.mark.parametrize("query", [{"owner": "bob"}, {"root": "/private"}, {"operation": "remove"}, {"fields": ["owner"]}, {"fields": ["source", "source"]}, {"budgets": {"rows": True}}, {"budgets": {"bytes": 2047}}, {"budgets": {"tokens": 2047}}, {"budgets": {"work": 4_000_001}}])
def test_invalid_or_too_small_requests_fail_before_backend(query):
    with pytest.raises(CodeGraphProjectionError):
        prepare_graph_query(query)


def test_budget_failure_does_not_forward_an_advanced_cursor_past_lost_rows():
    query, caller = prepare_graph_query({"fields": ["excerpt"], "budgets": {"bytes": 4096, "tokens": 4096}})
    source = envelope()
    source["hits"][0]["excerpt"] = "x" * 5000
    result = project_code_graph(source, query=query, caller_budget=caller)
    assert result["error"]["code"] == "projection_budget_exceeded"
    assert result["hits"] == [] and result["next_cursor"] is None
    assert result["partial"] and result["degraded"]
    assert result["cost"]["serialized_bytes"] <= 4096


def test_missing_or_cancelled_evidence_remains_an_error_not_empty_success():
    query, caller = prepare_graph_query({})
    source = envelope()
    source.update(receipt=None, hits=[], edges=[], error={"code": "cancelled", "message": "Cancelled"})
    result = project_code_graph(source, query=query, caller_budget=caller)
    assert result["error"]["code"] == "cancelled" and result["receipt"] is None


def test_nested_omissions_preserve_arbitrary_properties_and_operation_capabilities():
    source = envelope()
    source["operation_capabilities"] = [{"operation": "catalog", "available": True, "reason": None}]
    source["hits"][0].update(resolution={"source": {"rel_path": "a"}, "witnesses": [{"source": {"rel_path": "b"}}], "state": "exact"}, raw_source={"hash": "sha"})
    source["hits"][0]["properties"] = {"source": "arbitrary", "witnesses": ["metadata"]}
    query, caller = prepare_graph_query({"fields": ["resolution", "properties", "witnesses"], "budgets": {"hops": 0}})
    result = project_code_graph(source, query=query, caller_budget=caller)
    hit = result["hits"][0]
    assert hit["resolution"] == {"witnesses": [{}], "state": "exact"}
    assert hit["properties"] == source["hits"][0]["properties"]
    assert "raw_source" not in hit
    assert result["operation_capabilities"] == source["operation_capabilities"]
    query, caller = prepare_graph_query({"fields": ["resolution", "excerpt"]})
    result = project_code_graph(source, query=query, caller_budget=caller)
    assert result["hits"][0]["resolution"] == {"state": "exact"}
    assert result["hits"][0]["raw_source"] == {"hash": "sha"}


@pytest.mark.parametrize("key,value", [("hits", {}), ("edges", None), ("paths", [None]), ("paths", [{"node_ids": [], "edges": None}]), ("counts", [None]), ("counts", [{"name": "x", "unit": "file", "exactness": "exact", "value": True}]), ("omissions", [None]), ("cost", []), ("facets", []), ("edges", [{}])])
def test_malformed_native_envelopes_are_typed_invalid_evidence(key, value):
    query, caller = prepare_graph_query({})
    source = envelope()
    source[key] = value
    with pytest.raises(CodeGraphProjectionError) as caught:
        project_code_graph(source, query=query, caller_budget=caller)
    assert caught.value.code == "invalid_evidence"


def test_independent_raw_window_is_explicit_not_a_canonical_graph_identity():
    query, caller = prepare_graph_query({"operation": "read", "fields": ["excerpt"]})
    source = envelope()
    source.update(operation="read", receipt=None, hits=[{"id": "raw:opaque", "kind": "raw_window", "excerpt": "window", "raw_source": {"hash_algorithm": "sha256"}}], edges=[])
    result = project_code_graph(source, query=query, caller_budget=caller)
    assert result["receipt"] is None and result["hits"][0]["id"] == "raw:opaque"
    assert result["hits"][0]["raw_source"]["hash_algorithm"] == "sha256"
    source["operation"] = "overview"
    with pytest.raises(CodeGraphProjectionError):
        project_code_graph(source, query=query, caller_budget=caller)
    source.update(operation="read", hits=[{"id": "file:abc", "kind": "raw_window", "excerpt": "window"}])
    with pytest.raises(CodeGraphProjectionError):
        project_code_graph(source, query=query, caller_budget=caller)


@pytest.mark.parametrize("receipt", [{"workspace_id": "ws"}, {"workspace_id": "ws", "admission_epoch": True}, None])
def test_incomplete_canonical_receipt_is_not_invented(receipt):
    query, caller = prepare_graph_query({})
    source = envelope()
    source["receipt"] = receipt
    if receipt is None:
        # Receipt absent is permitted for capabilities/errors/raw, not validated current graph.
        source["error"] = {"code": "missing", "message": "Missing"}
        source["hits"] = []
        source["edges"] = []
        assert project_code_graph(source, query=query, caller_budget=caller)["receipt"] is None
    else:
        with pytest.raises(CodeGraphProjectionError):
            project_code_graph(source, query=query, caller_budget=caller)


@pytest.mark.parametrize("change", ["opaque_id", "raw_edge", "witness_list", "witness_row", "path_id"])
def test_malformed_typed_graph_identity_or_witness_is_rejected(change):
    query, caller = prepare_graph_query({"fields": ["witnesses"]})
    source = envelope()
    if change == "opaque_id": source["hits"][0]["id"] = "decl:not-hex"
    if change == "raw_edge": source["edges"][0]["from"] = "raw:abc"
    if change == "witness_list": source["edges"][0]["witnesses"] = {}
    if change == "witness_row": source["edges"][0]["witnesses"] = [None]
    if change == "path_id": source["paths"] = [{"node_ids": ["raw:abc"], "edges": []}]
    with pytest.raises(CodeGraphProjectionError):
        project_code_graph(source, query=query, caller_budget=caller)


def test_compact_typed_failure_is_not_reclassified_as_malformed_success():
    query, caller = prepare_graph_query({})
    source = {"schema": "open-clank.code-query/v1", "operation": "overview", "receipt": None,
              "error": {"code": "budget_exceeded", "message": "Budget exceeded"}, "partial": False, "degraded": True, "cost": {}}
    result = project_code_graph(source, query=query, caller_budget=caller)
    assert result["error"]["code"] == "budget_exceeded" and result["receipt"] is None
    source["error"] = None
    with pytest.raises(CodeGraphProjectionError):
        project_code_graph(source, query=query, caller_budget=caller)


def test_empty_raw_search_preserves_window_pagination_without_canonical_receipt():
    query, caller = prepare_graph_query({"operation": "search", "channels": ["raw"], "fields": ["excerpt"]})
    source = envelope()
    window = {"rel_path": "unsupported.js", "start_byte": 0, "end_byte": 100, "continuation": "files-window", "continuity": "progress_only_nonatomic"}
    source.update(operation="search", receipt=None, hits=[], edges=[], source_window=window, next_cursor=None)
    result = project_code_graph(source, query=query, caller_budget=caller)
    assert result["source_window"] == window and result["receipt"] is None and result["next_cursor"] is None


def test_documentation_text_is_retained_without_unrequested_source_locator():
    query, caller = prepare_graph_query({"fields": ["documentation", "properties"]})
    source = envelope()
    source["hits"][0]["documentation"] = [{"text": "A real documentation block", "source": {"rel_path": "src/a.py", "source_hash": "hash"}}]
    source["hits"][0]["properties"] = {"documentation": {"source": "arbitrary requested metadata"}}
    result = project_code_graph(source, query=query, caller_budget=caller)
    assert result["hits"][0]["documentation"] == [{"text": "A real documentation block"}]
    assert result["hits"][0]["properties"] == source["hits"][0]["properties"]

"""Explicit literal search of current bytes of policy-admitted catalog files.

Files owns reads/search; Frankenmemory owns catalog selection and receipt.
There is no directory crawl, body index, synthetic declaration or source proof.
"""
from __future__ import annotations

import hashlib
import json
import os
import re


async def search_contents(*, result, query, binding, repository, provider,
                          auth_manager, session, purpose):
    from src.openclank.code_query import (
        CodeQueryError, SCHEMA, _FILES_IO_LIMIT, _FULL_FILE_LIMIT,
        _files_client, _relative, _source_guard,
    )
    from src.openclank.files_host_provider import _origin
    from src.openclank.resource_refs import issue_resource_ref

    if query["channels"] != ["raw"] or query.get("cursor") or query.get("stages") or query.get("seed_ids") or query.get("target_id"):
        raise CodeQueryError("unsupported", "Source contents is a separate literal search; refine the relative path instead of using a continuation")
    filters = query["filter"]
    if any(filters.get(key) for key in ("kinds", "symbol_kinds", "languages", "states")):
        raise CodeQueryError("unsupported", "Source contents supports relative path filters only")
    literal = query.get("query")
    if not isinstance(literal, str) or not literal:
        raise CodeQueryError("invalid", "Source contents needs a nonempty literal")

    cost = result["cost"]
    result.update(error=None, degraded=False, partial=False, receipt=None,
                  reasons=["source_contents_current_admitted_files_not_body_fts"],
                  hits=[], next_cursor=None, capabilities=[], operation_capabilities=[])
    client = _files_client(binding=binding, repository=repository,
                           auth_manager=auth_manager, session=session, purpose=purpose)
    expected = query.get("expected_receipt")
    captured_fence = None
    captured_policy = None

    async def guard():
        nonlocal captured_fence
        captured_fence = await _source_guard(binding=binding, repository=repository,
            provider=provider, auth_manager=auth_manager, session=session,
            purpose=purpose, expected=expected, captured_fence=captured_fence, cost=cost)

    async def policy_guard():
        nonlocal captured_policy
        if expected is None:
            raise CodeQueryError("invalid_evidence", "Policy check requires an admitted catalog receipt")
        check = await provider.code_read_fence(binding=binding, expected_receipt=expected, selection_policy=True)
        for key, value in (check.get("cost") or {}).items():
            if key in ("native_rpc_calls_completed", "metadata_bytes", "sql_vm_steps") and type(value) is int and value >= 0:
                cost[key] = cost.get(key, 0) + value
        fence = check.get("fence")
        if not isinstance(fence, str) or not fence or captured_policy is not None and fence != captured_policy:
            raise CodeQueryError("stale_cursor", "Code selection policy changed during source search")
        captured_policy = fence

    await guard()
    capability = await client.request("capabilities", binding["root"])
    cost["files_rpc_calls_completed"] = 1
    if "content_search" not in (capability.get("data") or {}).get("operations", []):
        raise CodeQueryError("unsupported", "Authorized Files literal content search is unavailable")
    # This reports the broker operation after actual Files capability readback;
    # it does not rewrite the native raw/semantic channel capabilities.
    result["operation_capabilities"] = [dict(operation="source_contents", available=True,
        producer="authorized-files-literal-admitted-v1",
        scope="current bytes of policy-admitted catalog files", reason=None)]

    file_limit = 200  # Existing native CodeQuery/Files result-row ceiling.
    native_budget = min(query["budgets"]["work"], 4_000_000)
    # Cursor identity requires a fixed per-page budget. Reserve that entire
    # allowance before each call; unused work is available to later pages.
    catalog_budget = max(1, native_budget // 2)
    catalog_cursor = None
    seen_cursors = set()
    seen_files = set()
    scanned = 0
    matched = 0
    inspected = 0
    byte_count = 0
    complete = True
    exhausted = False
    # Every catalog page must yield a new file or terminal result. The number
    # of logical catalog/search requests is bounded by the selected-file cap.
    for _ in range(file_limit):
        if scanned >= file_limit or byte_count >= _FILES_IO_LIMIT or inspected + catalog_budget > native_budget:
            complete = False
            break
        catalog_query = dict(schema=SCHEMA, operation="catalog", fields=["label", "properties"],
            channels=[], filter={**filters, "kinds": ["file"]},
            budgets=dict(rows=200, bytes=262144, tokens=8192,
                         hops=0, work=catalog_budget))
        if expected is not None:
            catalog_query["expected_receipt"] = expected
        if catalog_cursor:
            catalog_query["cursor"] = catalog_cursor
        catalog = await provider.code_query(binding=binding, query=catalog_query)
        cost["catalog_calls_completed"] = cost.get("catalog_calls_completed", 0) + 1
        native_cost = catalog.get("cost") or {}
        for key in ("native_rpc_calls_completed", "metadata_bytes", "sql_vm_steps"):
            n = native_cost.get(key, 0)
            if type(n) is int and n >= 0:
                cost[key] = cost.get(key, 0) + n
        failure = catalog.get("error")
        if failure:
            raise CodeQueryError(failure.get("code", "unavailable"), "Source contents requires an available policy-validated code catalog; refresh the index for new paths")
        receipt = catalog.get("receipt")
        if not isinstance(receipt, dict) or expected is not None and receipt != expected:
            raise CodeQueryError("stale_cursor", "Code catalog changed during source search")
        expected = receipt
        await guard()
        if captured_policy is None:
            await policy_guard()
        native_inspected = native_cost.get("inspected_records", 0)
        if type(native_inspected) is not int or not 0 <= native_inspected <= catalog_budget:
            raise CodeQueryError("invalid_evidence", "Invalid catalog work count")
        inspected += native_inspected
        cost["inspected_records"] = inspected
        if inspected >= native_budget:
            complete = False
            break
        complete = complete and catalog.get("partial") is not True
        rows = catalog.get("hits")
        if not isinstance(rows, list):
            raise CodeQueryError("invalid_evidence", "Invalid admitted file catalog")
        novel = 0
        for hit in rows:
            props = hit.get("properties") or {}
            rel = _relative(props.get("rel_path"))
            if hit.get("kind") != "file" or rel in seen_files:
                continue
            seen_files.add(rel)
            novel += 1
            # Coverage-only ignored/private/unsupported/oversize catalog rows
            # are discoverable metadata, not admitted content-search targets.
            if props.get("state") not in {"indexed", "unchanged", "no_source_units", "no_units", "parse_failed"} or not re.fullmatch(r"[0-9a-f]{64}", str(props.get("source_hash") or "")):
                continue
            if scanned >= file_limit or byte_count >= _FILES_IO_LIMIT or inspected >= native_budget:
                complete = False
                break
            path = os.path.join(binding["root"], *rel.split("/"))
            await guard()
            stat = (await client.request("stat", path)).get("data") or {}
            cost["files_rpc_calls_completed"] += 1
            canonical = stat.get("path")
            if stat.get("kind") != "file" or not isinstance(canonical, str) or _origin(canonical) != _origin(path):
                raise CodeQueryError("stale_cursor", "Admitted source target changed")
            limit = min(_FULL_FILE_LIMIT, _FILES_IO_LIMIT-byte_count)
            response = await client.request("content_search", canonical, dict(
                query=literal, mode="literal", max_results=2, max_entries=1,
                max_depth=0, max_bytes_per_file=limit,
                include_hidden=False, case_sensitive=True))
            cost["files_rpc_calls_completed"] += 1
            data = response.get("data") or {}
            work = data.get("work") or {}
            read_bytes = work.get("bytes_read")
            if type(read_bytes) is not int or not 0 <= read_bytes <= limit or type(data.get("complete")) is not bool or not isinstance(data.get("matches"), list) or len(data["matches"]) > 1:
                raise CodeQueryError("invalid_evidence", "Invalid bounded Files content-search result")
            byte_count += read_bytes
            scanned += 1
            inspected += 1
            cost.update(files_bytes_read=byte_count, inspected_records=inspected)
            complete = complete and data["complete"]
            await guard()
            if data["matches"]:
                if _origin(str(data["matches"][0])) != _origin(canonical):
                    raise CodeQueryError("stale_cursor", "Files search target differs from admitted source")
                matched += 1
                ref = issue_resource_ref(owner_subject_id=binding["owner_subject_id"],
                    provider="host", origin_id=_origin(canonical), kind="file",
                    capabilities=("stat", "open"), policy_generation=binding["policy_generation"],
                    location_id=binding["location_id"], workspace_id=binding["workspace_id"])
                raw_id = hashlib.sha256(json.dumps([binding, expected, rel, literal], sort_keys=True).encode()).hexdigest()
                result["hits"].append(dict(id="raw:"+raw_id, kind="raw_file_match", label=rel,
                    properties=dict(rel_path=rel, match_mode="literal", scope="admitted_files_current_bytes",
                        destination=dict(provider="host", resource=ref.public_dict())),
                    matches=[dict(channel="raw", rank=matched-1, ordering_score=0,
                        explanation="literal found in bounded current file bytes; not an indexed symbol or source-proof claim")],
                    evidence_state="observed_file_match", current_source_verified=False))
                if len(result["hits"]) >= query["budgets"]["rows"]:
                    complete = False
                    break
        next_cursor = catalog.get("next_cursor")
        if not complete and (scanned >= file_limit or byte_count >= _FILES_IO_LIMIT or len(result["hits"]) >= query["budgets"]["rows"]):
            break
        if next_cursor is None:
            exhausted = True
            break
        if not isinstance(next_cursor, str) or next_cursor in seen_cursors or novel == 0:
            raise CodeQueryError("invalid_evidence", "Admitted catalog made no search progress")
        seen_cursors.add(next_cursor)
        catalog_cursor = next_cursor
    complete = complete and exhausted
    await guard()
    if expected is not None:
        await policy_guard()
    result.update(partial=not complete, degraded=not complete)
    if not complete:
        result["reasons"].append("source_contents_bounded_partial_refine_path_or_refresh_new_files")
    result["counts"] = [dict(name="scanned", value=scanned, unit="admitted_file", exactness="exact"),
        dict(name="matched_in_scan", value=matched, unit="file", exactness="exact"),
        dict(name="corpus_matches", value=matched if complete else None, unit="admitted_file", exactness="exact" if complete else "unknown")]
    result["channels"] = [dict(channel="raw", requested=True, enabled=True, executed=True,
        cancelled=False, error=None, producer="authorized-files-literal-admitted-v1",
        config_fingerprint=None, candidate_count=matched, inspected_records=inspected, elapsed_micros=None)]
    # Complete envelope bounds remain the existing broker's owner. Dropping a
    # hit never creates a pretend continuation or a false complete count.
    ceiling = min(query["budgets"]["bytes"], query["budgets"]["tokens"])
    while result["hits"] and len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode())+1024 > ceiling:
        result["hits"].pop()
        result.update(partial=True, degraded=True)
        if "source_contents_output_limited" not in result["reasons"]:
            result["reasons"].append("source_contents_output_limited")
